#!/usr/bin/env python3
"""Codex quota planner.

Queries ChatGPT/Codex quota usage with the local Codex OAuth token, estimates
weekly-vs-5h burn rates from local Codex session logs, and renders drain plans
as text, JSON, or an ASCII timeline.

Important model:
- Capacity is *usable awake capacity*, not wall-clock capacity.
- Default sleep block is 02:00-10:00 in the selected timezone; override with
  CODEX_QUOTA_SLEEP=HH:MM-HH:MM[,HH:MM-HH:MM]
- No secrets, cookies, refresh tokens, or full IDs are printed.
"""
from __future__ import annotations

import argparse
import glob
import json
import math
import os
import statistics
import sys
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

HOME = Path.home()
CODEX_AUTH = HOME / ".codex" / "auth.json"
CODEX_SESSIONS = HOME / ".codex" / "sessions"
CACHE_HOME = Path(os.environ.get("XDG_CACHE_HOME", str(HOME / ".cache")))
STATE_PATH = Path(os.environ.get("CODEX_QUOTA_STATE", str(CACHE_HOME / "codex-quota-planner" / "state.json")))
USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"


def _valid_timezone_name(name: str | None) -> str | None:
    if not name:
        return None
    # POSIX TZ strings such as "PST8PDT" are not IANA zone names; keep the
    # planner output stable by accepting only names ZoneInfo can load.
    try:
        ZoneInfo(name)
    except Exception:
        return None
    return name


def _iana_from_localtime_symlink(path: Path) -> str | None:
    try:
        if not path.is_symlink():
            return None
        parts = path.resolve(strict=False).parts
    except OSError:
        return None
    if "zoneinfo" not in parts:
        return None
    idx = parts.index("zoneinfo") + 1
    if idx >= len(parts):
        return None
    if parts[idx] in {"posix", "right"}:
        idx += 1
    return _valid_timezone_name("/".join(parts[idx:]))


def resolve_timezone_name(environ: dict[str, str] | None = None, localtime_path: Path = Path("/etc/localtime")) -> str:
    """Resolve the most convenient display/planning timezone.

    Explicit `CODEX_QUOTA_TZ` wins. Otherwise prefer the user's process/system
    timezone (`TZ`, then `/etc/localtime` symlink). Fall back to UTC instead of a
    project-specific timezone so new users do not have to configure anything.
    """
    env = os.environ if environ is None else environ
    for key in ("CODEX_QUOTA_TZ", "TZ"):
        if tz := _valid_timezone_name(env.get(key)):
            return tz
    if tz := _iana_from_localtime_symlink(localtime_path):
        return tz
    return "UTC"


LOCAL_TZ = ZoneInfo(resolve_timezone_name())
LONDON = LOCAL_TZ  # backward-compatible internal name
USER_AGENT = "Mozilla/5.0 (codex-quota-planner)"

DEFAULT_WEEKLY_PER_PRIMARY_RATIO = 1.0
WARN_SURPLUS_PCT = float(os.environ.get("CODEX_QUOTA_WARN_SURPLUS_PCT", os.environ.get("CODEX_WEEKLY_DRAIN_WARN_SURPLUS_PCT", "8")))
URGENT_SURPLUS_PCT = float(os.environ.get("CODEX_QUOTA_URGENT_SURPLUS_PCT", os.environ.get("CODEX_WEEKLY_DRAIN_URGENT_SURPLUS_PCT", "2")))
MIN_REMAINING_PCT = float(os.environ.get("CODEX_QUOTA_MIN_REMAINING_PCT", os.environ.get("CODEX_WEEKLY_DRAIN_MIN_REMAINING_PCT", "3")))
HISTORY_DAYS = int(os.environ.get("CODEX_QUOTA_HISTORY_DAYS", os.environ.get("CODEX_WEEKLY_DRAIN_HISTORY_DAYS", "21")))
SNAPSHOT_KEEP_DAYS = int(os.environ.get("CODEX_QUOTA_SNAPSHOT_KEEP_DAYS", os.environ.get("CODEX_WEEKLY_DRAIN_SNAPSHOT_KEEP_DAYS", "30")))
SLEEP_SPEC = os.environ.get("CODEX_QUOTA_SLEEP", os.environ.get("CODEX_WEEKLY_DRAIN_SLEEP", "02:00-10:00"))


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def safe_float(x: Any, default: float = 0.0) -> float:
    try:
        return float(x)
    except Exception:
        return default


def fmt_dt(ts: int | float | None) -> str:
    if not isinstance(ts, (int, float)):
        return "unknown"
    return datetime.fromtimestamp(ts, timezone.utc).astimezone(LONDON).strftime("%Y-%m-%d %H:%M:%S %Z")


def human_seconds(seconds: float | int | None) -> str:
    if seconds is None:
        return "unknown"
    seconds = max(0, int(seconds))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    parts: list[str] = []
    if days:
        parts.append(f"{days}d")
    if hours or days:
        parts.append(f"{hours}h")
    parts.append(f"{minutes}m")
    return " ".join(parts)


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def load_state() -> dict[str, Any]:
    try:
        return load_json(STATE_PATH)
    except Exception:
        return {}


def save_state(state: dict[str, Any]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=STATE_PATH.name + ".", dir=str(STATE_PATH.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2, sort_keys=True)
            f.write("\n")
        os.replace(tmp, STATE_PATH)
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass


def get_access_token() -> str:
    auth = load_json(CODEX_AUTH)
    token = (auth.get("tokens") or {}).get("access_token") or auth.get("access_token")
    if not token:
        raise RuntimeError("tokens.access_token not found in ~/.codex/auth.json")
    return str(token)


def fetch_usage() -> dict[str, Any]:
    token = get_access_token()
    req = urllib.request.Request(
        USAGE_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
            "OpenAI-Beta": "codex-1",
            "originator": "Codex CLI",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        if e.code == 401:
            raise RuntimeError("HTTP 401: Codex credentials are invalid or the Authorization: Bearer *** header was not accepted") from e
        raise RuntimeError(f"HTTP {e.code} from usage endpoint") from e


@dataclass
class LimitView:
    name: str
    metered_feature: str | None
    rate_limit: dict[str, Any]

    @property
    def key(self) -> str:
        return self.name

    @property
    def primary(self) -> dict[str, Any]:
        return self.rate_limit.get("primary_window") or {}

    @property
    def secondary(self) -> dict[str, Any]:
        return self.rate_limit.get("secondary_window") or {}

    @property
    def primary_used(self) -> float:
        return safe_float(self.primary.get("used_percent"), 0.0)

    @property
    def weekly_used(self) -> float:
        return safe_float(self.secondary.get("used_percent"), 0.0)

    @property
    def weekly_remaining(self) -> float:
        return max(0.0, 100.0 - self.weekly_used)

    @property
    def primary_remaining(self) -> float:
        return max(0.0, 100.0 - self.primary_used)


def limits_from_usage(data: dict[str, Any]) -> list[LimitView]:
    out = [LimitView("default", None, data.get("rate_limit") or {})]
    for item in data.get("additional_rate_limits") or []:
        if not isinstance(item, dict):
            continue
        out.append(
            LimitView(
                str(item.get("limit_name") or item.get("metered_feature") or "additional"),
                item.get("metered_feature"),
                item.get("rate_limit") or {},
            )
        )
    return out


# ---- Awake-time model -----------------------------------------------------

def parse_hhmm(s: str) -> time:
    h, m = s.split(":", 1)
    return time(int(h), int(m))


def parse_sleep_spec(spec: str) -> list[tuple[time, time]]:
    blocks: list[tuple[time, time]] = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        a, b = part.split("-", 1)
        blocks.append((parse_hhmm(a.strip()), parse_hhmm(b.strip())))
    return blocks


def local_day_bounds(start_utc: float, end_utc: float) -> list[datetime]:
    start_local = datetime.fromtimestamp(start_utc, timezone.utc).astimezone(LONDON).date() - timedelta(days=1)
    end_local = datetime.fromtimestamp(end_utc, timezone.utc).astimezone(LONDON).date() + timedelta(days=1)
    days = []
    d = start_local
    while d <= end_local:
        days.append(datetime.combine(d, time(0, 0), LONDON))
        d += timedelta(days=1)
    return days


def overlap_seconds(a0: float, a1: float, b0: float, b1: float) -> float:
    return max(0.0, min(a1, b1) - max(a0, b0))


def sleep_intervals_utc(start_utc: float, end_utc: float, blocks: list[tuple[time, time]]) -> list[tuple[float, float]]:
    intervals: list[tuple[float, float]] = []
    for day_start in local_day_bounds(start_utc, end_utc):
        for s, e in blocks:
            st = datetime.combine(day_start.date(), s, LONDON)
            en = datetime.combine(day_start.date(), e, LONDON)
            if en <= st:
                en += timedelta(days=1)
            st_ts = st.astimezone(timezone.utc).timestamp()
            en_ts = en.astimezone(timezone.utc).timestamp()
            if overlap_seconds(start_utc, end_utc, st_ts, en_ts) > 0:
                intervals.append((st_ts, en_ts))
    return intervals


def awake_seconds(start_utc: float, end_utc: float, blocks: list[tuple[time, time]]) -> float:
    if end_utc <= start_utc:
        return 0.0
    asleep = sum(overlap_seconds(start_utc, end_utc, s, e) for s, e in sleep_intervals_utc(start_utc, end_utc, blocks))
    return max(0.0, (end_utc - start_utc) - asleep)


def drain_capacity_awake_primary_pct(limit: LimitView, now_ts: float, sleep_blocks: list[tuple[time, time]]) -> dict[str, Any]:
    """Return primary-percent capacity available while awake before weekly reset.

    We model primary windows as 5h buckets aligned to the current primary reset.
    Current bucket capacity is capped by the remaining 5h percent and scaled by
    the awake fraction between now and its reset. Future bucket capacity is
    100% * awake_fraction(bucket).
    """
    weekly_reset = safe_float(limit.secondary.get("reset_at"), 0.0)
    primary_reset = safe_float(limit.primary.get("reset_at"), 0.0)
    primary_window = safe_float(limit.primary.get("limit_window_seconds"), 18000.0)
    if weekly_reset <= now_ts or primary_window <= 0:
        return {"primary_pct": 0.0, "awake_seconds": 0.0, "wall_seconds": 0.0, "full_windows": 0, "partial_windows": 0}

    total_pct = 0.0
    total_awake = 0.0
    total_wall = max(0.0, weekly_reset - now_ts)
    full_windows = 0
    partial_windows = 0

    # Current window: from now until current primary reset.
    cur_end = min(primary_reset, weekly_reset)
    if cur_end > now_ts:
        wall = cur_end - now_ts
        awake = awake_seconds(now_ts, cur_end, sleep_blocks)
        total_awake += awake
        current_window_remaining_seconds = max(1.0, primary_reset - now_ts)
        frac = awake / current_window_remaining_seconds
        total_pct += limit.primary_remaining * frac
        partial_windows += 1

    # Future windows after primary_reset.
    t = max(primary_reset, now_ts)
    while t < weekly_reset:
        end = min(t + primary_window, weekly_reset)
        wall = end - t
        awake = awake_seconds(t, end, sleep_blocks)
        total_awake += awake
        total_pct += 100.0 * (awake / primary_window if primary_window > 0 else 0.0)
        if abs(wall - primary_window) < 1:
            full_windows += 1
        else:
            partial_windows += 1
        t = end

    return {
        "primary_pct": total_pct,
        "awake_seconds": total_awake,
        "wall_seconds": total_wall,
        "full_windows": full_windows,
        "partial_windows": partial_windows,
    }


# ---- History model -------------------------------------------------------

def snapshot_from_limits(limits: list[LimitView], now: datetime) -> dict[str, Any]:
    snap = {"ts": now.isoformat(), "limits": {}}
    for lim in limits:
        snap["limits"][lim.key] = {
            "primary_used": lim.primary_used,
            "primary_reset": lim.primary.get("reset_at"),
            "weekly_used": lim.weekly_used,
            "weekly_reset": lim.secondary.get("reset_at"),
        }
    return snap


def update_snapshots(state: dict[str, Any], limits: list[LimitView], now: datetime) -> list[dict[str, Any]]:
    snapshots = list(state.get("usage_snapshots") or [])
    snapshots.append(snapshot_from_limits(limits, now))
    cutoff = now - timedelta(days=SNAPSHOT_KEEP_DAYS)
    kept = []
    for s in snapshots:
        try:
            ts = datetime.fromisoformat(str(s.get("ts")).replace("Z", "+00:00"))
        except Exception:
            continue
        if ts >= cutoff:
            kept.append(s)
    # De-duplicate near-identical cron double-runs by timestamp minute.
    state["usage_snapshots"] = kept[-2000:]
    return state["usage_snapshots"]


def ratio_from_snapshots(snapshots: list[dict[str, Any]], key: str) -> tuple[float | None, int]:
    rows = []
    for s in snapshots:
        lim = (s.get("limits") or {}).get(key)
        if not isinstance(lim, dict):
            continue
        try:
            ts = datetime.fromisoformat(str(s.get("ts")).replace("Z", "+00:00"))
        except Exception:
            continue
        rows.append((ts, lim))
    rows.sort(key=lambda x: x[0])
    ratios: list[float] = []
    prev = None
    for _ts, cur in rows:
        if prev and cur.get("weekly_reset") == prev.get("weekly_reset"):
            ds = safe_float(cur.get("weekly_used"), math.nan) - safe_float(prev.get("weekly_used"), math.nan)
            dp = math.nan
            if cur.get("primary_reset") == prev.get("primary_reset"):
                dp = safe_float(cur.get("primary_used"), math.nan) - safe_float(prev.get("primary_used"), math.nan)
            if ds > 0 and dp > 0:
                r = ds / dp
                if 0.01 <= r <= 10:
                    ratios.append(r)
        prev = cur
    return (statistics.median(ratios), len(ratios)) if ratios else (None, 0)


def recent_session_paths(days: int) -> list[str]:
    cutoff = utcnow() - timedelta(days=days)
    paths = glob.glob(str(CODEX_SESSIONS / "**" / "*.jsonl"), recursive=True)
    out = []
    for p in paths:
        try:
            if datetime.fromtimestamp(os.path.getmtime(p), timezone.utc) >= cutoff:
                out.append(p)
        except OSError:
            pass
    return sorted(out)


def estimate_token_history(days: int, plan_type: str | None = None) -> dict[str, Any]:
    events = 0
    total_last_tokens = 0
    tokens_per_primary_pct: list[float] = []
    tokens_per_weekly_pct: list[float] = []
    for path in recent_session_paths(days):
        prev_by_limit: dict[str, dict[str, Any]] = {}
        primary_token_acc_by_limit: dict[str, int] = {}
        weekly_token_acc_by_limit: dict[str, int] = {}
        try:
            for line in open(path, "r", encoding="utf-8"):
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                if obj.get("type") != "event_msg":
                    continue
                payload = obj.get("payload") or {}
                if payload.get("type") != "token_count":
                    continue
                info = payload.get("info") or {}
                last = info.get("last_token_usage") or {}
                toks = max(0, int(safe_float(last.get("total_tokens"), 0.0)))
                rl = payload.get("rate_limits") or {}
                if not rl:
                    continue
                if plan_type and rl.get("plan_type") != plan_type:
                    continue
                total_last_tokens += toks
                events += 1
                key = str(rl.get("limit_name") or rl.get("limit_id") or "default")
                p = rl.get("primary") or {}
                s = rl.get("secondary") or {}
                cur = {
                    "primary_used": safe_float(p.get("used_percent"), math.nan),
                    "primary_reset": p.get("resets_at"),
                    "weekly_used": safe_float(s.get("used_percent"), math.nan),
                    "weekly_reset": s.get("resets_at"),
                }
                prev = prev_by_limit.get(key)
                if not prev:
                    prev_by_limit[key] = cur
                    primary_token_acc_by_limit[key] = 0
                    weekly_token_acc_by_limit[key] = 0
                    continue
                primary_token_acc_by_limit[key] = primary_token_acc_by_limit.get(key, 0) + toks
                weekly_token_acc_by_limit[key] = weekly_token_acc_by_limit.get(key, 0) + toks
                if prev and cur["primary_reset"] == prev.get("primary_reset"):
                    dp = cur["primary_used"] - safe_float(prev.get("primary_used"), math.nan)
                    acc = primary_token_acc_by_limit.get(key, 0)
                    if dp > 0 and acc > 0:
                        tokens_per_primary_pct.append(acc / dp)
                        primary_token_acc_by_limit[key] = 0
                if prev and cur["weekly_reset"] == prev.get("weekly_reset"):
                    ds = cur["weekly_used"] - safe_float(prev.get("weekly_used"), math.nan)
                    acc = weekly_token_acc_by_limit.get(key, 0)
                    if ds > 0 and acc > 0:
                        tokens_per_weekly_pct.append(acc / ds)
                        weekly_token_acc_by_limit[key] = 0
                prev_by_limit[key] = cur
        except OSError:
            continue
    primary_median = statistics.median(tokens_per_primary_pct) if tokens_per_primary_pct else None
    weekly_median = statistics.median(tokens_per_weekly_pct) if tokens_per_weekly_pct else None
    token_ratio = None
    if primary_median and weekly_median and weekly_median > 0:
        token_ratio = primary_median / weekly_median
    return {
        "events": events,
        "total_last_tokens": total_last_tokens,
        "tokens_per_primary_pct": primary_median,
        "tokens_per_primary_pct_samples": len(tokens_per_primary_pct),
        "tokens_per_weekly_pct": weekly_median,
        "tokens_per_weekly_pct_samples": len(tokens_per_weekly_pct),
        "token_derived_weekly_per_primary_ratio": token_ratio,
    }


def build_history(state: dict[str, Any], limits: list[LimitView], now: datetime, days: int, plan_type: str | None = None) -> dict[str, Any]:
    snapshots = update_snapshots(state, limits, now)
    token_history = estimate_token_history(days, plan_type=plan_type)
    ratios_by_limit: dict[str, dict[str, Any]] = {}
    all_ratios: list[float] = []
    for lim in limits:
        ratio, samples = ratio_from_snapshots(snapshots, lim.key)
        if ratio is not None and samples >= 2:
            all_ratios.append(ratio)
        ratios_by_limit[lim.key] = {"weekly_per_primary_ratio": ratio, "ratio_samples": samples}
    token_ratio = token_history.get("token_derived_weekly_per_primary_ratio")
    if all_ratios:
        global_ratio = statistics.median(all_ratios)
    elif isinstance(token_ratio, (int, float)) and token_ratio > 0:
        global_ratio = float(token_ratio)
    else:
        global_ratio = DEFAULT_WEEKLY_PER_PRIMARY_RATIO
    return {**token_history, "snapshots": len(snapshots), "global_weekly_per_primary_ratio": global_ratio, "ratios_by_limit": ratios_by_limit}


def ratio_for_limit(history: dict[str, Any], key: str) -> tuple[float, int, str]:
    item = (history.get("ratios_by_limit") or {}).get(key) or {}
    ratio = item.get("weekly_per_primary_ratio")
    samples = int(item.get("ratio_samples") or 0)
    if isinstance(ratio, (int, float)) and samples >= 2:
        return float(ratio), samples, "per-limit snapshots"
    return safe_float(history.get("global_weekly_per_primary_ratio"), DEFAULT_WEEKLY_PER_PRIMARY_RATIO), samples, "global/fallback"


# ---- Analysis ------------------------------------------------------------

def primary_buckets(
    limit: LimitView,
    now_ts: float,
    target_ts: float,
    sleep_blocks: list[tuple[time, time]],
) -> list[dict[str, Any]]:
    """Primary-window buckets available between now and target.

    Each bucket reports usable primary-percent capacity after excluding sleep.
    The current bucket starts at now and is capped by current primary_remaining;
    future buckets are aligned to the observed 5h reset cadence and capped at 100%.
    """
    primary_window = safe_float(limit.primary.get("limit_window_seconds"), 18000.0)
    primary_reset = safe_float(limit.primary.get("reset_at"), now_ts + primary_window)
    if target_ts <= now_ts or primary_window <= 0:
        return []
    out: list[dict[str, Any]] = []

    cur_end = min(primary_reset, target_ts)
    if cur_end > now_ts:
        wall = cur_end - now_ts
        awake = awake_seconds(now_ts, cur_end, sleep_blocks)
        current_window_remaining_seconds = max(1.0, primary_reset - now_ts)
        cap = limit.primary_remaining * (awake / current_window_remaining_seconds)
        out.append({"start_ts": now_ts, "end_ts": cur_end, "primary_pct": cap, "awake_seconds": awake, "is_current": True})

    t = max(primary_reset, now_ts)
    while t < target_ts:
        end = min(t + primary_window, target_ts)
        wall = end - t
        awake = awake_seconds(t, end, sleep_blocks)
        cap = 100.0 * (awake / primary_window if primary_window > 0 else 0.0)
        out.append({"start_ts": t, "end_ts": end, "primary_pct": cap, "awake_seconds": awake, "is_current": False})
        t = end
    return out


def select_buckets(buckets: list[dict[str, Any]], weekly_needed: float, ratio: float, strategy: str = "latest") -> tuple[list[dict[str, Any]], float, bool]:
    """Pick buckets to burn weekly_needed.

    latest: choose the latest possible buckets, minimizing babysitting before the deadline.
    eager: choose the earliest possible buckets, starting with the current bucket.
    """
    remaining = max(0.0, weekly_needed)
    selected: list[dict[str, Any]] = []
    iterable = reversed(buckets) if strategy != "eager" else iter(buckets)
    for b in iterable:
        weekly_cap = max(0.0, safe_float(b.get("primary_pct"), 0.0) * ratio)
        if weekly_cap <= 0 or remaining <= 1e-9:
            continue
        use_weekly = min(weekly_cap, remaining)
        use_primary = use_weekly / ratio if ratio > 0 else 0.0
        item = dict(b)
        item["weekly_pct"] = use_weekly
        item["primary_pct_to_use"] = use_primary
        selected.append(item)
        remaining -= use_weekly
    if strategy != "eager":
        selected = list(reversed(selected))
    return selected, max(0.0, weekly_needed - remaining), remaining <= 1e-6


def select_latest_buckets(buckets: list[dict[str, Any]], weekly_needed: float, ratio: float) -> tuple[list[dict[str, Any]], float, bool]:
    """Backward-compatible wrapper for latest-safe bucket selection."""
    return select_buckets(buckets, weekly_needed, ratio, strategy="latest")


def synth_limit_after_reset(name: str, reset_ts: float) -> LimitView:
    return LimitView(
        name,
        None,
        {
            "allowed": True,
            "limit_reached": False,
            "primary_window": {"used_percent": 0.0, "limit_window_seconds": 18000.0, "reset_at": reset_ts + 18000.0},
            "secondary_window": {"used_percent": 0.0, "limit_window_seconds": 604800.0, "reset_at": reset_ts + 604800.0},
        },
    )


def plan_limit(
    limit: LimitView,
    now: datetime,
    sleep_blocks: list[tuple[time, time]],
    ratio: float,
    target_ts: float | None = None,
    reset_cards: int = 0,
    strategy: str = "latest",
) -> dict[str, Any]:
    """Build a constrained drain plan with optional 7d+5h reset cards.

    Optimization objective, in order:
    1. Burn as much weekly quota as possible before the hard target.
    2. Use the fewest 5h primary buckets for each weekly cycle.
    3. Place work as late as safely possible, so the user can wait until the
       latest-safe start instead of babysitting every early bucket.
    4. Use reset cards only after a cycle is fully drained; earlier use wastes
       unburned weekly/primary headroom. Each card resets both windows at use time.
    """
    now_ts = now.timestamp()
    hard_target = target_ts if target_ts is not None else safe_float(limit.secondary.get("reset_at"), now_ts + 604800.0)
    hard_target = max(now_ts, hard_target)
    ratio = max(0.000001, ratio)
    cards_left = max(0, int(reset_cards))
    cur = limit
    cycle = 0
    actions: list[dict[str, Any]] = []
    total_planned = 0.0
    total_needed = 0.0
    reset_cards_used = 0
    exhausted_all_cycles = True

    while True:
        cycle_target = min(hard_target, safe_float(cur.secondary.get("reset_at"), hard_target))
        weekly_needed = cur.weekly_remaining
        total_needed += weekly_needed
        buckets = primary_buckets(cur, now_ts, cycle_target, sleep_blocks)
        selected, planned, exhausted = select_buckets(buckets, weekly_needed, ratio, strategy=strategy)
        exhausted_all_cycles = exhausted_all_cycles and exhausted
        total_planned += planned
        for b in selected:
            actions.append(
                {
                    "kind": "drain",
                    "cycle": cycle,
                    "start_ts": b["start_ts"],
                    "end_ts": b["end_ts"],
                    "weekly_pct": b["weekly_pct"],
                    "primary_pct": b["primary_pct_to_use"],
                    "is_current": b.get("is_current", False),
                }
            )
        if not exhausted or cards_left <= 0 or not selected:
            break
        card_ts = max(b["end_ts"] for b in selected)
        if card_ts >= hard_target:
            break
        cards_left -= 1
        reset_cards_used += 1
        actions.append({"kind": "reset_card", "cycle": cycle, "start_ts": card_ts, "end_ts": card_ts, "weekly_pct": 0.0, "primary_pct": 0.0})
        now_ts = card_ts
        cycle += 1
        cur = synth_limit_after_reset(limit.name, card_ts)
        # If caller did not pass a hard target, a reset card opens a fresh 7d cycle.
        if target_ts is None:
            hard_target = safe_float(cur.secondary.get("reset_at"), hard_target)

    drain_actions = [a for a in actions if a["kind"] == "drain"]
    min_windows = len(drain_actions)
    latest_start = min((a["start_ts"] for a in drain_actions), default=None)
    finish_ts = max((a["end_ts"] for a in drain_actions), default=now.timestamp())
    return {
        "name": limit.name,
        "actions": sorted(actions, key=lambda a: (a["start_ts"], 0 if a["kind"] == "drain" else 1)),
        "summary": {
            "weekly_target_pct": round(limit.weekly_remaining, 6),
            "planned_weekly_pct": round(total_planned, 6),
            "total_weekly_target_pct_including_cards": round(total_needed, 6),
            "minimum_5h_windows": min_windows,
            "latest_safe_start_ts": latest_start,
            "finish_ts": finish_ts,
            "exhausts_planned_target": exhausted_all_cycles,
            "reset_cards_used": reset_cards_used,
            "weekly_cycles_planned": 1 + reset_cards_used,
            "ratio": ratio,
            "strategy": strategy,
            "hard_target_ts": hard_target,
        },
    }


def analyze_limit(limit: LimitView, history: dict[str, Any], now: datetime, sleep_blocks: list[tuple[time, time]]) -> dict[str, Any]:
    ratio, ratio_samples, ratio_source = ratio_for_limit(history, limit.key)
    cap = drain_capacity_awake_primary_pct(limit, now.timestamp(), sleep_blocks)
    max_weekly_burn = cap["primary_pct"] * ratio
    remaining = limit.weekly_remaining
    surplus = max_weekly_burn - remaining
    weekly_reset = safe_float(limit.secondary.get("reset_at"), 0.0)
    primary_reset = safe_float(limit.primary.get("reset_at"), 0.0)
    seconds_to_weekly = weekly_reset - now.timestamp() if weekly_reset else None
    seconds_to_primary = primary_reset - now.timestamp() if primary_reset else None

    level = "ok"
    reasons: list[str] = []
    if remaining >= MIN_REMAINING_PCT:
        if surplus < 0:
            level = "urgent"
            reasons.append("After sleep exclusions, even max awake-time usage may not drain the weekly remainder")
        elif surplus <= URGENT_SURPLUS_PCT:
            level = "urgent"
            reasons.append("Usable surplus after sleep exclusions is near zero; schedule long jobs immediately")
        elif surplus <= WARN_SURPLUS_PCT:
            level = "warn"
            reasons.append("Usable surplus after sleep exclusions is small; start scheduling long jobs")
        if cap["awake_seconds"] <= safe_float(limit.primary.get("limit_window_seconds"), 18000.0) and remaining > max(3.0, max_weekly_burn * 0.8):
            level = "urgent"
            reasons.append("Usable awake time before weekly reset is less than one 5h window")

    return {
        "name": limit.name,
        "allowed": limit.rate_limit.get("allowed"),
        "limit_reached": limit.rate_limit.get("limit_reached"),
        "primary_used": limit.primary_used,
        "weekly_used": limit.weekly_used,
        "weekly_remaining": remaining,
        "primary_remaining": limit.primary_remaining,
        "primary_reset_london": fmt_dt(limit.primary.get("reset_at")),
        "weekly_reset_london": fmt_dt(limit.secondary.get("reset_at")),
        "seconds_to_primary": seconds_to_primary,
        "seconds_to_weekly": seconds_to_weekly,
        "awake_seconds_to_weekly": cap["awake_seconds"],
        "wall_seconds_to_weekly": cap["wall_seconds"],
        "future_full_5h_windows": cap["full_windows"],
        "partial_windows": cap["partial_windows"],
        "weekly_per_primary_ratio": ratio,
        "ratio_samples": ratio_samples,
        "ratio_source": ratio_source,
        "max_weekly_burn_pct_est": max_weekly_burn,
        "surplus_pct_est": surplus,
        "level": level,
        "reasons": reasons,
    }


def parse_target_datetime(spec: str | None, now: datetime) -> float | None:
    if not spec:
        return None
    s = spec.strip()
    if not s:
        return None
    # Accept YYYY-MM-DD as end-of-day London; ISO datetimes may include offset.
    try:
        if len(s) == 10 and s[4] == "-" and s[7] == "-":
            d = datetime.fromisoformat(s)
            return datetime.combine(d.date(), time(23, 59), LONDON).astimezone(timezone.utc).timestamp()
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=LONDON)
        return dt.astimezone(timezone.utc).timestamp()
    except Exception as e:
        raise ValueError(f"invalid --target {spec!r}; use YYYY-MM-DD or ISO datetime") from e


def fmt_ts(ts: float | int | None) -> str:
    if not isinstance(ts, (int, float)):
        return "unknown"
    return datetime.fromtimestamp(ts, timezone.utc).astimezone(LONDON).strftime("%a %m-%d %H:%M %Z")


def render_plan(plans: list[dict[str, Any]], history: dict[str, Any], usage: dict[str, Any], sleep_spec: str, reset_cards: int, target_label: str | None) -> str:
    lines: list[str] = []
    lines.append("Codex drain plan")
    lines.append(f"plan_type: {usage.get('plan_type')} | sleep excluded: {sleep_spec or 'none'} {LOCAL_TZ} | reset cards considered: {reset_cards}")
    lines.append(
        f"model: weekly_per_5h≈{safe_float(history.get('global_weekly_per_primary_ratio'), DEFAULT_WEEKLY_PER_PRIMARY_RATIO):.3f} "
        f"(from history; per-lane value used below when available)"
    )
    strategy = (plans[0].get("summary") or {}).get("strategy", "latest") if plans else "latest"
    if target_label:
        lines.append(f"hard target: {target_label}")
    if strategy == "eager":
        lines.append("Principle: start as early as possible; use the current available window first; after each 5h window, wait for primary reset; use reset credits only after a weekly cycle is drained.")
    else:
        lines.append("Principle: start as late as safely possible; after each 5h window, wait for primary reset; use reset credits only after a weekly cycle is drained, otherwise remaining quota is wasted.")

    for p in plans:
        s = p["summary"]
        lines.append("")
        lines.append(f"## {p['name']}")
        lines.append(
            f"Target: current cycle {s['weekly_target_pct']:.1f} weekly-pct"
            f"; total target incl. credits {s['total_weekly_target_pct_including_cards']:.1f}"
            f"; planned {s['planned_weekly_pct']:.1f}"
        )
        start_label = "start time" if s.get("strategy") == "eager" else "latest safe start"
        lines.append(
            f"minimum 5h windows: {s['minimum_5h_windows']}; "
            f"{start_label}: {fmt_ts(s['latest_safe_start_ts'])}; "
            f"estimated finish: {fmt_ts(s['finish_ts'])}; "
            f"window/weekly ratio≈{s['ratio']:.3f}"
        )
        if s["reset_cards_used"]:
            lines.append(f"reset credits: use {s['reset_cards_used']}; timing = immediately after the previous weekly cycle is drained.")
        if not s["exhausts_planned_target"]:
            lines.append("⚠️ Cannot finish before target under current constraints; start earlier, reduce sleep exclusions, use reset credits, or extend the target.")

        actions = p["actions"]
        if not actions:
            lines.append("Action: no scheduling needed, or no available window.")
            continue
        last_end = None
        idx = 1
        for a in actions:
            if a["kind"] == "reset_card":
                lines.append(f"  - {fmt_ts(a['start_ts'])}: use reset credit (resets weekly + 5h)")
                last_end = a["end_ts"]
                continue
            wait = ""
            if last_end is not None and a["start_ts"] > last_end + 60:
                wait = f"; waited {human_seconds(a['start_ts'] - last_end)}"
            lines.append(
                f"  {idx}. {fmt_ts(a['start_ts'])} → {fmt_ts(a['end_ts'])}: "
                f"use about {a['primary_pct']:.0f}% primary ≈ {a['weekly_pct']:.1f}% weekly{wait}"
            )
            last_end = a["end_ts"]
            idx += 1
    return "\n".join(lines)


def _local_label(ts: float, fmt: str) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).astimezone(LONDON).strftime(fmt)


def render_timeline(plans: list[dict[str, Any]], width: int = 72) -> str:
    """Render a compact calendar-like ASCII timeline for plan actions."""
    actions = []
    for p in plans:
        for a in p.get("actions") or []:
            item = dict(a)
            item["lane"] = p.get("name", "unknown")
            actions.append(item)
    if not actions:
        return "Timeline\n(no scheduled actions)"

    start_ts = min(a["start_ts"] for a in actions)
    end_ts = max(a["end_ts"] for a in actions)
    width = max(24, min(120, int(width)))
    start_local = datetime.fromtimestamp(start_ts, timezone.utc).astimezone(LONDON)
    end_local = datetime.fromtimestamp(end_ts, timezone.utc).astimezone(LONDON)

    lines: list[str] = []
    lines.append("Timeline")
    lines.append("Legend: D=drain  R=reset-card  .=wait/idle  | = 00/06/12/18")
    lines.append(f"Range: {start_local.strftime('%m-%d %H:%M %Z')} → {end_local.strftime('%m-%d %H:%M %Z')}")

    by_lane: dict[str, list[dict[str, Any]]] = {}
    for a in actions:
        by_lane.setdefault(str(a["lane"]), []).append(a)

    for lane, lane_actions in by_lane.items():
        lines.append("")
        lines.append(f"{lane}")
        first_day = start_local.date()
        last_day = end_local.date()
        day = first_day
        while day <= last_day:
            day_start = datetime.combine(day, time(0, 0), LONDON).timestamp()
            day_end = day_start + 86400
            row = ["."] * width
            # Quarter-day guide marks.
            for h in (0, 6, 12, 18):
                idx = min(width - 1, max(0, int((h * 3600) / 86400 * width)))
                row[idx] = "|"
            for a in lane_actions:
                overlap = overlap_seconds(day_start, day_end, a["start_ts"], a["end_ts"])
                if overlap <= 0 and not (a["kind"] == "reset_card" and day_start <= a["start_ts"] < day_end):
                    continue
                sym = "R" if a["kind"] == "reset_card" else "D"
                if a["kind"] == "reset_card":
                    i0 = i1 = min(width - 1, max(0, int(((a["start_ts"] - day_start) / 86400) * width)))
                else:
                    i0 = min(width - 1, max(0, int(((max(a["start_ts"], day_start) - day_start) / 86400) * width)))
                    i1 = min(width - 1, max(0, int(math.ceil(((min(a["end_ts"], day_end) - day_start) / 86400) * width)) - 1))
                for i in range(i0, i1 + 1):
                    row[i] = sym
            lines.append(f"  {day.strftime('%a %m-%d')} 00 06 12 18 24 |{''.join(row)}|")
            day += timedelta(days=1)
        lines.append("  actions:")
        for i, a in enumerate(lane_actions, 1):
            if a["kind"] == "reset_card":
                lines.append(f"    - {fmt_ts(a['start_ts'])}: R reset credit")
            else:
                lines.append(
                    f"    {i}. {fmt_ts(a['start_ts'])} → {fmt_ts(a['end_ts'])}: "
                    f"D {a['weekly_pct']:.1f}% weekly / {a['primary_pct']:.0f}% primary"
                )
    return "\n".join(lines)


def render_report(analyses: list[dict[str, Any]], history: dict[str, Any], usage: dict[str, Any], status: bool, sleep_spec: str) -> str:
    lines: list[str] = []
    header = "Codex weekly drain monitor"
    if not status:
        header = "⚠️ " + header
    lines.append(header)
    lines.append(f"plan_type: {usage.get('plan_type')}")
    lines.append(f"sleep excluded: {sleep_spec} {LOCAL_TZ}")
    lines.append(
        "history: "
        f"{history.get('events')} token_count events, {history.get('snapshots')} API snapshots, "
        f"global weekly/5h ratio≈{safe_float(history.get('global_weekly_per_primary_ratio'), DEFAULT_WEEKLY_PER_PRIMARY_RATIO):.3f}, "
        f"tokens/weekly_pct≈{history.get('tokens_per_weekly_pct') or 'unknown'}"
    )
    for a in analyses:
        emoji = "🚨" if a["level"] == "urgent" else "⚠️" if a["level"] == "warn" else "✅"
        lines.append("")
        lines.append(f"{emoji} {a['name']} — {a['level']}")
        lines.append(
            f"  primary(5h): used {a['primary_used']:.1f}%, remaining {a['primary_remaining']:.1f}%, "
            f"reset {a['primary_reset_london']} ({human_seconds(a['seconds_to_primary'])})"
        )
        lines.append(
            f"  weekly: used {a['weekly_used']:.1f}%, remaining {a['weekly_remaining']:.1f}%, "
            f"reset {a['weekly_reset_london']} ({human_seconds(a['seconds_to_weekly'])})"
        )
        lines.append(
            f"  usable awake time before weekly reset: {human_seconds(a['awake_seconds_to_weekly'])} "
            f"/ wall {human_seconds(a['wall_seconds_to_weekly'])}"
        )
        lines.append(
            f"  estimated awake drain capacity: {a['max_weekly_burn_pct_est']:.1f} weekly-pct "
            f"(surplus {a['surplus_pct_est']:.1f}, full 5h buckets {a['future_full_5h_windows']}, "
            f"partial {a['partial_windows']}, ratio {a['weekly_per_primary_ratio']:.3f} via {a['ratio_source']} n={a['ratio_samples']})"
        )
        for r in a["reasons"]:
            lines.append(f"  reason: {r}")
    return "\n".join(lines)


def public_history(history: dict[str, Any]) -> dict[str, Any]:
    return {
        "events": history.get("events"),
        "api_snapshots": history.get("snapshots"),
        "global_weekly_per_primary_ratio": history.get("global_weekly_per_primary_ratio"),
        "tokens_per_weekly_pct": history.get("tokens_per_weekly_pct"),
        "tokens_per_weekly_pct_samples": history.get("tokens_per_weekly_pct_samples"),
        "ratios_by_limit": history.get("ratios_by_limit"),
    }


def public_usage(usage: dict[str, Any]) -> dict[str, Any]:
    return {
        "plan_type": usage.get("plan_type"),
        "rate_limit_reset_credits": usage.get("rate_limit_reset_credits"),
    }


def print_json(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Codex quota planner")
    ap.add_argument("--status", action="store_true", help="always print current status")
    ap.add_argument("--format", choices=["text", "json", "timeline"], default="text", help="output format for --plan/--status")
    ap.add_argument("--plan", action="store_true", help="print a low-stress drain plan")
    ap.add_argument("--eager", action="store_true", help="with --plan, start draining as early as possible instead of latest-safe")
    ap.add_argument("--timeline", action="store_true", help="with --plan, also print calendar-like ASCII timeline")
    ap.add_argument("--timeline-width", type=int, default=72, help="timeline row width, 24-120 chars")
    ap.add_argument("--target", help="hard target date/time for --plan, e.g. 2026-07-03 or 2026-07-03T18:00")
    ap.add_argument("--reset-cards", type=int, default=0, help="number of 7d+5h reset cards to consider in --plan")
    ap.add_argument("--limit", help="only show one limit/lane name substring in --plan")
    ap.add_argument("--no-state", action="store_true", help="do not suppress duplicate alerts")
    ap.add_argument("--history-days", type=int, default=HISTORY_DAYS)
    ap.add_argument("--sleep", default=SLEEP_SPEC, help="London sleep blocks HH:MM-HH:MM[,HH:MM-HH:MM]")
    args = ap.parse_args(argv)

    state = load_state()
    now = utcnow()
    try:
        sleep_blocks = parse_sleep_spec(args.sleep)
    except Exception as e:
        print(f"Codex weekly drain monitor error: invalid sleep spec {args.sleep!r}: {e}")
        return 0

    try:
        usage = fetch_usage()
    except Exception as e:
        msg = str(e)
        state["last_error"] = msg
        state["last_checked_at"] = now.isoformat()
        save_state(state)
        print(f"Codex weekly drain monitor error: {msg}")
        return 0

    limits = limits_from_usage(usage)
    if args.limit:
        needle = args.limit.lower()
        limits = [lim for lim in limits if needle in lim.name.lower()]
        if not limits:
            print(f"Codex weekly drain monitor error: no limit/lane matching {args.limit!r}")
            return 0
    history = build_history(state, limits, now, args.history_days, plan_type=usage.get("plan_type"))
    analyses = [analyze_limit(lim, history, now, sleep_blocks) for lim in limits]
    state["last_checked_at"] = now.isoformat()
    state["last_error"] = ""
    state["sleep_spec"] = args.sleep
    state["last_levels"] = {a["name"]: a["level"] for a in analyses}

    if args.plan:
        try:
            target_ts = parse_target_datetime(args.target, now)
        except ValueError as e:
            print(f"Codex weekly drain monitor error: {e}")
            return 0
        plans = []
        strategy = "eager" if args.eager else "latest"
        for lim in limits:
            ratio, _samples, _source = ratio_for_limit(history, lim.key)
            plans.append(plan_limit(lim, now, sleep_blocks, ratio, target_ts=target_ts, reset_cards=args.reset_cards, strategy=strategy))
        save_state(state)
        target_label = fmt_ts(target_ts) if target_ts is not None else None
        payload = {
            "mode": "plan",
            "usage": public_usage(usage),
            "history": public_history(history),
            "sleep": {"spec": args.sleep, "timezone": str(LOCAL_TZ)},
            "target": {"input": args.target, "label": target_label, "timestamp": target_ts},
            "strategy": strategy,
            "reset_cards": max(0, args.reset_cards),
            "plans": plans,
        }
        if args.format == "json":
            print_json(payload)
        else:
            output = render_plan(plans, history, usage, sleep_spec=args.sleep, reset_cards=max(0, args.reset_cards), target_label=target_label)
            if args.timeline or args.format == "timeline":
                output += "\n\n" + render_timeline(plans, width=args.timeline_width)
            print(output)
        return 0

    if args.status:
        save_state(state)
        if args.format == "json":
            print_json({
                "mode": "status",
                "usage": public_usage(usage),
                "history": public_history(history),
                "sleep": {"spec": args.sleep, "timezone": str(LOCAL_TZ)},
                "analyses": analyses,
            })
        else:
            print(render_report(analyses, history, usage, status=True, sleep_spec=args.sleep))
        return 0

    alerting = [a for a in analyses if a["level"] in {"warn", "urgent"}]
    if not alerting:
        save_state(state)
        return 0

    keys = []
    for a in alerting:
        surplus_bucket = math.floor(a["surplus_pct_est"] / 2.0) * 2
        keys.append(f"{a['name']}|{a['level']}|{a['weekly_reset_london']}|{surplus_bucket}|sleep={args.sleep}")
    alert_key = ";".join(keys)
    if not args.no_state and state.get("last_alert_key") == alert_key:
        save_state(state)
        return 0
    state["last_alert_key"] = alert_key
    state["last_alerted_at"] = now.isoformat()
    save_state(state)
    print(render_report(analyses, history, usage, status=False, sleep_spec=args.sleep))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
