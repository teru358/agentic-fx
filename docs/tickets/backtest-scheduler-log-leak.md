---
id: backtest-scheduler-log-leak
status: 設計待ち
priority: 低
opened: null
closed: null
related: []
backfilled: true
source_section: 未完了
---
# [backtest-scheduler-log-leak]

**状態**: 設計待ち / **優先**: 低

## 現象・原因・処置案 (tickets.md からの移行、原文)

[backtest-scheduler-log-leak] (低) backtest 内部 Scheduler の `maintain_reservations failed: no completed 1m bar for rate conversion at ...` が本番 logger に混入 (run_backtest/gate 実行時刻に 21 本)。`backtest/runner.py:212-217` rate_fn → `scheduler.py:199-207`。欠損 tick で `fills_allowed=False` → backtest 指標が「約定しない」側に歪む可能性は要確認

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。

