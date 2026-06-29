# Codex Quota Planner

A small CLI for planning ChatGPT/Codex quota usage across the rolling 5-hour
primary window and the weekly secondary window.

It can:

- query the local Codex CLI OAuth session in `~/.codex/auth.json`;
- estimate weekly burn per 5-hour-window burn from local Codex session logs;
- plan either **latest-safe** usage (wait until the latest safe start) or
  **eager** usage (start draining from the current available window);
- model reset credits that reset both the weekly and 5-hour windows;
- render text, JSON, or an ASCII calendar/timeline.

> This project is unofficial. It uses endpoints and local log formats observed
> from Codex CLI behavior and may need updates if those change.

## Install

From a checkout:

```bash
python -m pip install -e .
```

Or run directly:

```bash
python src/codex_quota_planner.py --status
```

## Usage

Current status:

```bash
codex-quota-planner --status
```

Plan until the current weekly reset, latest-safe mode:

```bash
codex-quota-planner --plan
```

Start draining as soon as possible and include a timeline:

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

Machine-readable output for other tools:

```bash
codex-quota-planner --plan --format json --target 2026-07-01 > plan.json
codex-quota-planner --status --format json > status.json
```

ASCII calendar output:

```bash
codex-quota-planner --plan --format timeline --timeline-width 96 --target 2026-07-01
```

Legend:

- `D` = drain/use quota
- `R` = reset credit use
- `.` = wait/idle
- `|` = 00/06/12/18 guide marks

## Configuration

Environment variables:

| Variable | Default | Meaning |
| --- | --- | --- |
| `CODEX_QUOTA_TZ` | `Europe/London` | Time zone for sleep blocks and rendered times |
| `CODEX_QUOTA_SLEEP` | `02:00-10:00` | Sleep blocks excluded from usable capacity |
| `CODEX_QUOTA_STATE` | `~/.cache/codex-quota-planner/state.json` | Local snapshot cache path |
| `CODEX_QUOTA_HISTORY_DAYS` | `21` | Days of local Codex logs used for estimates |
| `CODEX_QUOTA_WARN_SURPLUS_PCT` | `8` | Alert threshold |
| `CODEX_QUOTA_URGENT_SURPLUS_PCT` | `2` | Urgent alert threshold |
| `CODEX_QUOTA_MIN_REMAINING_PCT` | `3` | Ignore tiny remaining weekly balances below this |

Legacy `CODEX_WEEKLY_DRAIN_*` variables are also accepted for compatibility.

## Privacy and security

The CLI reads the local Codex OAuth access token only to make the API request.
It does **not** print tokens, refresh tokens, cookies, account IDs, or full user
identifiers. JSON output intentionally exposes only public planning/status data.

Before publishing logs or outputs, still review them for project names or other
context you may consider private.

## Development

```bash
python -m unittest -v tests/test_planner.py
python -m py_compile src/codex_quota_planner.py
```

## License

MIT
