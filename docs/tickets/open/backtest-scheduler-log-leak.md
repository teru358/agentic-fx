---
id: backtest-scheduler-log-leak
status: 設計待ち
priority: 低
opened: null
closed: null
related: [reevaluation-approval-detail-display-polish]
backfilled: true
source_section: 未完了
kind: 不具合
---
# [backtest-scheduler-log-leak]

**状態**: 設計待ち / **優先**: 低 / **種別**: 不具合

## 現象・原因・処置案 (tickets.md からの移行、原文)

[backtest-scheduler-log-leak] (低) backtest 内部 Scheduler の `maintain_reservations failed: no completed 1m bar for rate conversion at ...` が本番 logger に混入 (run_backtest/gate 実行時刻に 21 本)。`backtest/runner.py:212-217` rate_fn → `scheduler.py:199-207`。欠損 tick で `fills_allowed=False` → backtest 指標が「約定しない」側に歪む可能性は要確認

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。
- 2026-10-25: 種別: 不具合 — 種別を付与
- 2026-11-07: 関連: [reevaluation-approval-detail-display-polish] — 2026-11-07 にも観測: 配備中 strategy の再評価 (afx plugin reevaluate、CLI process) と daemon の backtest で maintain_reservations failed: no completed 5m bar for rate conversion。原因は過去の 5m 足の欠け (USDJPY 2026-06-02 21:20、2026-07-01 21:50 が無い)。replay の rate_fn (backtest/runner.py:401-406) が欠けを ValueError にし、OPEN の建玉は scheduler.py:317-326 の tick 全体の catch でしか拾われない。再評価の表示チケット reevaluation-approval-detail-display-polish の 4 点目をこちらに統合
