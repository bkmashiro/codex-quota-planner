#!/usr/bin/env python3
import unittest
from datetime import datetime, timezone

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import codex_quota_planner as w


def ts(s: str) -> float:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


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
