#!/usr/bin/env python3
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import codex_quota_planner as w


def ts(s: str) -> float:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


class TimezoneResolutionTests(unittest.TestCase):
    def test_codex_quota_tz_overrides_everything(self):
        self.assertEqual(
            w.resolve_timezone_name({"CODEX_QUOTA_TZ": "Asia/Tokyo", "TZ": "America/New_York"}, localtime_path=Path("/missing")),
            "Asia/Tokyo",
        )

    def test_tz_env_is_default_when_codex_quota_tz_absent(self):
        self.assertEqual(
            w.resolve_timezone_name({"TZ": "America/Los_Angeles"}, localtime_path=Path("/missing")),
            "America/Los_Angeles",
        )

    def test_iana_zone_is_inferred_from_localtime_symlink(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            target = root / "usr" / "share" / "zoneinfo" / "Europe" / "Paris"
            target.parent.mkdir(parents=True)
            target.write_text("")
            link = root / "etc" / "localtime"
            link.parent.mkdir()
            link.symlink_to(target)
            self.assertEqual(w.resolve_timezone_name({}, localtime_path=link), "Europe/Paris")


class HistoryModelTests(unittest.TestCase):
    def test_token_history_estimates_weekly_per_primary_ratio_from_local_logs(self):
        with tempfile.TemporaryDirectory() as td:
            log = Path(td) / "session.jsonl"
            events = [
                # 100k tokens moves primary by 10%, but weekly by only 4%.
                (100_000, 0.0, 0.0),
                (100_000, 10.0, 4.0),
                (100_000, 20.0, 8.0),
            ]
            with log.open("w", encoding="utf-8") as f:
                for toks, primary_used, weekly_used in events:
                    f.write(
                        json.dumps(
                            {
                                "type": "event_msg",
                                "payload": {
                                    "type": "token_count",
                                    "info": {"last_token_usage": {"total_tokens": toks}},
                                    "rate_limits": {
                                        "limit_id": "codex",
                                        "primary": {"used_percent": primary_used, "resets_at": 1},
                                        "secondary": {"used_percent": weekly_used, "resets_at": 2},
                                    },
                                },
                            }
                        )
                        + "\n"
                    )

            old_recent = w.recent_session_paths
            try:
                w.recent_session_paths = lambda days: [str(log)]
                history = w.estimate_token_history(21)
            finally:
                w.recent_session_paths = old_recent

        self.assertAlmostEqual(history["tokens_per_primary_pct"], 10_000.0)
        self.assertAlmostEqual(history["tokens_per_weekly_pct"], 25_000.0)
        self.assertAlmostEqual(history["token_derived_weekly_per_primary_ratio"], 0.4)

    def test_build_history_uses_token_derived_ratio_when_snapshot_samples_are_missing(self):
        now = datetime.fromisoformat("2026-06-29T08:00:00+00:00")
        lim = w.LimitView(
            "default",
            None,
            {
                "primary_window": {"used_percent": 0.0, "limit_window_seconds": 18000, "reset_at": now.timestamp() + 18000},
                "secondary_window": {"used_percent": 10.0, "limit_window_seconds": 604800, "reset_at": now.timestamp() + 604800},
            },
        )
        old_estimate = w.estimate_token_history
        try:
            w.estimate_token_history = lambda days: {
                "events": 2,
                "total_last_tokens": 200_000,
                "tokens_per_primary_pct": 10_000.0,
                "tokens_per_primary_pct_samples": 2,
                "tokens_per_weekly_pct": 25_000.0,
                "tokens_per_weekly_pct_samples": 2,
                "token_derived_weekly_per_primary_ratio": 0.4,
            }
            history = w.build_history({}, [lim], now, 21)
        finally:
            w.estimate_token_history = old_estimate

        self.assertAlmostEqual(history["global_weekly_per_primary_ratio"], 0.4)


class PlanModelTests(unittest.TestCase):
    def make_limit(self, *, now="2026-06-29T08:00:00+00:00", primary_used=0.0, weekly_used=0.0):
        now_ts = ts(now)
        return w.LimitView(
            "default",
            None,
            {
                "primary_window": {
                    "used_percent": primary_used,
                    "limit_window_seconds": 18000,
                    "reset_at": now_ts + 18000,
                },
                "secondary_window": {
                    "used_percent": weekly_used,
                    "limit_window_seconds": 604800,
                    "reset_at": now_ts + 7 * 86400,
                },
            },
        )

    def test_min_windows_accounts_for_ratio_and_current_primary_remaining(self):
        now = datetime.fromisoformat("2026-06-29T08:00:00+00:00")
        lim = self.make_limit(primary_used=40.0, weekly_used=20.0)
        sleep = w.parse_sleep_spec("")
        plan = w.plan_limit(lim, now, sleep, ratio=0.2, target_ts=now.timestamp() + 7 * 86400, reset_cards=0)
        self.assertEqual(plan["summary"]["weekly_target_pct"], 80.0)
        # With a hard weekly reset cutting the last primary bucket, four full
        # buckets are not quite enough; one truncated bucket is also needed.
        self.assertEqual(plan["summary"]["minimum_5h_windows"], 5)
        self.assertGreaterEqual(plan["summary"]["planned_weekly_pct"], 80.0)

    def test_latest_safe_plan_uses_late_buckets_not_early_ones(self):
        now = datetime.fromisoformat("2026-06-29T08:00:00+00:00")
        lim = self.make_limit(primary_used=0.0, weekly_used=80.0)
        sleep = w.parse_sleep_spec("")
        plan = w.plan_limit(lim, now, sleep, ratio=0.2, target_ts=now.timestamp() + 7 * 86400, reset_cards=0)
        actions = [a for a in plan["actions"] if a["kind"] == "drain"]
        self.assertGreaterEqual(len(actions), 1)
        self.assertLessEqual(len(actions), 2)
        self.assertGreater(actions[0]["start_ts"], now.timestamp() + 5 * 86400)
        self.assertAlmostEqual(sum(a["weekly_pct"] for a in actions), 20.0, places=3)

    def test_reset_card_is_scheduled_after_draining_a_full_cycle(self):
        now = datetime.fromisoformat("2026-06-29T08:00:00+00:00")
        lim = self.make_limit(primary_used=0.0, weekly_used=0.0)
        sleep = w.parse_sleep_spec("")
        plan = w.plan_limit(lim, now, sleep, ratio=1.0, target_ts=now.timestamp() + 10 * 86400, reset_cards=1)
        kinds = [a["kind"] for a in plan["actions"]]
        self.assertIn("reset_card", kinds)
        card = next(a for a in plan["actions"] if a["kind"] == "reset_card")
        first_cycle_drain_end = max(a["end_ts"] for a in plan["actions"] if a["kind"] == "drain" and a["cycle"] == 0)
        self.assertEqual(card["start_ts"], first_cycle_drain_end)
        self.assertEqual(plan["summary"]["reset_cards_used"], 1)
        self.assertEqual(plan["summary"]["weekly_cycles_planned"], 2)
    def test_partial_future_bucket_capacity_scales_by_full_5h_window(self):
        now = datetime.fromisoformat("2026-06-29T08:00:00+00:00")
        lim = self.make_limit(primary_used=0.0, weekly_used=0.0)
        sleep = w.parse_sleep_spec("")
        # Target cuts the second future bucket after 2.5h, so capacity is
        # 100% current + 50% future, not 200%.
        buckets = w.primary_buckets(lim, now.timestamp(), now.timestamp() + 7.5 * 3600, sleep)
        self.assertEqual(len(buckets), 2)
        self.assertAlmostEqual(sum(b["primary_pct"] for b in buckets), 150.0, places=3)

    def test_timeline_renders_calendar_rows_and_legend(self):
        now = datetime.fromisoformat("2026-06-29T08:00:00+00:00")
        lim = self.make_limit(primary_used=0.0, weekly_used=80.0)
        plan = w.plan_limit(lim, now, w.parse_sleep_spec(""), ratio=0.2, target_ts=now.timestamp() + 7 * 86400, reset_cards=0)
        text = w.render_timeline([plan], width=48)
        self.assertIn("Legend", text)
        self.assertIn("D=drain", text)
        self.assertIn("default", text)
        self.assertRegex(text, r"\d{2}-\d{2}")
        self.assertIn("D", text)

    def test_eager_plan_starts_with_current_bucket(self):
        now = datetime.fromisoformat("2026-06-29T08:00:00+00:00")
        lim = self.make_limit(primary_used=40.0, weekly_used=20.0)
        plan = w.plan_limit(lim, now, w.parse_sleep_spec(""), ratio=0.2, target_ts=now.timestamp() + 7 * 86400, reset_cards=0, strategy="eager")
        actions = [a for a in plan["actions"] if a["kind"] == "drain"]
        self.assertEqual(actions[0]["start_ts"], now.timestamp())
        self.assertEqual(plan["summary"]["strategy"], "eager")
        self.assertLess(plan["summary"]["finish_ts"], now.timestamp() + 2 * 86400)


if __name__ == "__main__":
    unittest.main()
