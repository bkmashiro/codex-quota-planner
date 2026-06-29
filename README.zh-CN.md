# 速蹬 / Codex Quota Planner

[English](README.md)

**速蹬** 是一个非官方 CLI，用来规划 ChatGPT/Codex 配额在滚动 **5 小时 primary 窗口** 和 **weekly secondary 窗口** 之间的消耗节奏。

它可以：

- 读取本机 Codex CLI OAuth 会话（`~/.codex/auth.json`）并查询当前配额；
- 根据本地 Codex session logs 估算“每 5 小时窗口消耗多少 weekly”；
- 生成两种计划：
  - **latest-safe**：尽量晚开始，但保证来得及；
  - **eager / 速蹬模式**：从当前可用窗口尽早开始消耗；
- 模拟 reset credits：重置 weekly + 5h 窗口；
- 输出 text、JSON 或 ASCII timeline，方便人看，也方便其他工具二次开发。

> 非官方项目：接口和本地日志格式来自对 Codex CLI 行为的观察，未来如果上游变化，本工具可能需要同步更新。

## 安装

从 checkout 安装：

```bash
python -m pip install -e .
```

或者直接运行：

```bash
python src/codex_quota_planner.py --status
```

## 快速使用

查看当前状态：

```bash
codex-quota-planner --status
```

生成默认计划：latest-safe，也就是“尽量晚开始，但不误事”：

```bash
codex-quota-planner --plan
```

开启“速蹬模式”：从现在开始尽量早消耗，并显示 timeline：

```bash
codex-quota-planner --plan --eager --timeline --target 2026-07-01
```

只看某个 lane / limit：

```bash
codex-quota-planner --plan --limit Spark --target 2026-07-01
```

考虑 reset credit：

```bash
codex-quota-planner --plan --limit Spark --reset-cards 1
```

机器可读输出，适合接给其他脚本或 UI：

```bash
codex-quota-planner --plan --format json --target 2026-07-01 > plan.json
codex-quota-planner --status --format json > status.json
```

ASCII calendar/timeline 输出：

```bash
codex-quota-planner --plan --format timeline --timeline-width 96 --target 2026-07-01
```

图例：

- `D` = 消耗配额
- `R` = 使用 reset credit
- `.` = 等待
- `|` = 00/06/12/18 时间刻度

## 脱敏输出样例

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

## 配置

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `CODEX_QUOTA_TZ` | 用户/系统默认时区，失败时 fallback `UTC` | 覆盖 sleep block 和输出时间用的时区 |
| `CODEX_QUOTA_SLEEP` | `02:00-10:00` | 从可用容量里排除的睡眠时段 |
| `CODEX_QUOTA_STATE` | `~/.cache/codex-quota-planner/state.json` | 本地 snapshot 缓存路径 |
| `CODEX_QUOTA_HISTORY_DAYS` | `21` | 用多少天本地 Codex logs 估算速率 |
| `CODEX_QUOTA_WARN_SURPLUS_PCT` | `8` | warn 阈值 |
| `CODEX_QUOTA_URGENT_SURPLUS_PCT` | `2` | urgent 阈值 |
| `CODEX_QUOTA_MIN_REMAINING_PCT` | `3` | 忽略很小的 weekly 剩余额 |

时区解析顺序：

1. 显式 `CODEX_QUOTA_TZ`；
2. 进程/用户 `TZ`；
3. 系统 `/etc/localtime` symlink（如果能解析出 IANA zone name）；
4. fallback 到 `UTC`。

兼容旧的 `CODEX_WEEKLY_DRAIN_*` 环境变量。

## 隐私与安全

本工具只在本地读取 Codex OAuth access token 来发起 API 请求；不会打印 token、refresh token、cookie、account ID 或完整用户标识。JSON 输出也只保留规划和状态字段，避免暴露鉴权信息。

## 开发

```bash
python -m unittest -v tests/test_planner.py
python -m py_compile src/codex_quota_planner.py
```

## License

MIT
