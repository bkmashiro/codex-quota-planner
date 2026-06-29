# Sudoeng / Codex Quota Planner

[简体中文](README.zh-CN.md)

**Sudoeng** (速蹬, “quota sprint”) is an unofficial CLI for planning ChatGPT/Codex quota usage across the rolling **5-hour primary window** and the **weekly secondary window**.

It can:

- read the local Codex CLI OAuth session (`~/.codex/auth.json`) to query quota usage;
- estimate weekly burn per 5-hour window from local Codex session logs;
- generate either **latest-safe** plans or **eager / Sudoeng** plans;
- model reset credits that reset both weekly and 5-hour limits;
- render text, JSON, or ASCII timelines for automation and reuse.

> Unofficial project: it relies on observed Codex CLI endpoints and local log formats, which may change upstream.

## Install

From a checkout:

```bash
python -m pip install -e .
```

Or run directly:

```bash
python src/codex_quota_planner.py --status
```

## Quick usage

Current status:

```bash
codex-quota-planner --status
```

Latest-safe plan: wait until the latest safe start time, but still leave enough usable capacity:

```bash
codex-quota-planner --plan
```

Eager / Sudoeng mode: start draining as soon as possible and include a timeline:

```bash
codex-quota-planner --plan --eager --timeline --target 2026-07-01
```

Only show a specific lane/limit:

```bash
codex-quota-planner --plan --limit Spark --target 2026-07-01
```

Consider reset credits:

```bash
codex-quota-planner --plan --limit Spark --reset-cards 1
```

Machine-readable output:

```bash
codex-quota-planner --plan --format json --target 2026-07-01 > plan.json
codex-quota-planner --status --format json > status.json
```

ASCII calendar/timeline output:

```bash
codex-quota-planner --plan --format timeline --timeline-width 96 --target 2026-07-01
```

Legend:

- `D` = drain/use quota
- `R` = reset credit use
- `.` = wait/idle
- `|` = 00/06/12/18 guide marks

## Redacted output samples

### Text plan

```text
Codex drain plan
plan_type: example-plan | sleep excluded: 02:00-10:00 Europe/London | reset cards considered: 0
model: weekly_per_5h≈1.000 (from history; per-lane value used below when available)
hard target: Wed 07-01 23:59 BST
Principle: start as early as possible; use the current available window first; after each 5h window, wait for primary reset; use reset credits only after a weekly cycle is drained.

## Example-Codex-Lane
Target: current cycle 60.0 weekly-pct; total target incl. credits 60.0; planned 60.0
minimum 5h windows: 1; start time: Mon 06-29 13:30 BST; estimated finish: Mon 06-29 18:30 BST; window/weekly ratio≈1.000
  1. Mon 06-29 13:30 BST → Mon 06-29 18:30 BST: use about 100% primary ≈ 60.0% weekly
```

### Timeline

```text
Timeline
Legend: D=drain  R=reset-card  .=wait/idle  | = 00/06/12/18
Range: 06-29 13:30 BST → 07-01 23:59 BST

Example-Codex-Lane
  Mon 06-29 00 06 12 18 24 ||.................|.................|....DDDDDDDDDDDDDD|.................|
  Tue 06-30 00 06 12 18 24 ||.................|.................|.................|.................|
  Wed 07-01 00 06 12 18 24 ||.................|.................|.................|.................|
  actions:
    1. Mon 06-29 13:30 BST → Mon 06-29 18:30 BST: D 60.0% weekly / 100% primary
```

### JSON

```json
{
  "mode": "plan",
  "strategy": "eager",
  "reset_cards": 0,
  "sleep": {
    "spec": "02:00-10:00",
    "timezone": "Europe/London"
  },
  "usage": {
    "plan_type": "example-plan",
    "rate_limit_reset_credits": 0
  },
  "history": {
    "events": 123,
    "api_snapshots": 4,
    "global_weekly_per_primary_ratio": 1.0,
    "tokens_per_weekly_pct": 12345.6,
    "tokens_per_weekly_pct_samples": 42,
    "ratios_by_limit": {
      "Example-Codex-Lane": 1.0
    }
  },
  "target": {
    "input": "2026-07-01",
    "label": "Wed 07-01 23:59 BST",
    "timestamp": 1782946740
  },
  "plans": [
    {
      "name": "Example-Codex-Lane",
      "summary": {
        "strategy": "eager",
        "weekly_target_pct": 60.0,
        "planned_weekly_pct": 60.0,
        "minimum_5h_windows": 1,
        "start_ts": 1782736200,
        "finish_ts": 1782754200,
        "ratio": 1.0,
        "reset_cards_used": 0,
        "exhausts_planned_target": true
      },
      "actions": [
        {
          "kind": "drain",
          "start_ts": 1782736200,
          "end_ts": 1782754200,
          "weekly_pct": 60.0,
          "primary_pct": 100.0,
          "cycle": 0
        }
      ]
    }
  ]
}
```

## Configuration

Environment variables:

| Variable | Default | Meaning |
| --- | --- | --- |
| `CODEX_QUOTA_TZ` | system/user timezone, fallback `UTC` | Override timezone for sleep blocks and rendered times |
| `CODEX_QUOTA_SLEEP` | `02:00-10:00` | Sleep blocks excluded from usable capacity |
| `CODEX_QUOTA_STATE` | `~/.cache/codex-quota-planner/state.json` | Local snapshot cache path |
| `CODEX_QUOTA_HISTORY_DAYS` | `21` | Days of local Codex logs used for estimates |
| `CODEX_QUOTA_WARN_SURPLUS_PCT` | `8` | Alert threshold |
| `CODEX_QUOTA_URGENT_SURPLUS_PCT` | `2` | Urgent alert threshold |
| `CODEX_QUOTA_MIN_REMAINING_PCT` | `3` | Ignore tiny remaining weekly balances below this |

Timezone resolution order:

1. explicit `CODEX_QUOTA_TZ`;
2. process/user `TZ`;
3. system `/etc/localtime` symlink when it contains an IANA zone name;
4. `UTC` fallback.

Legacy `CODEX_WEEKLY_DRAIN_*` variables are also accepted for compatibility.

## Privacy and security

The CLI reads the local Codex OAuth access token only to make the API request. It does **not** print tokens, refresh tokens, cookies, account IDs, or full user identifiers. JSON output intentionally exposes only planning/status data.

## Development

```bash
python -m unittest -v tests/test_planner.py
python -m py_compile src/codex_quota_planner.py
```

## License

MIT
