---
id: indicator-consumption-wiring
status: 設計待ち
priority: 高
opened: 2026-09-12
closed: null
related: [indicator-first-seeding]
backfilled: true
source_section: 未完了
---
# [indicator-consumption-wiring]

**状態**: 設計待ち / **優先**: 高

## 現象・原因・処置案 (tickets.md からの移行、原文)

**[indicator-consumption-wiring] (高、ユーザー方針「指標層 → 戦略層」の本丸、2026-09-12)** plugin 契約 `evaluate(df, indicators, signals, params)` は indicators を受け取る形だが、backtest (`strategy_adapter.py:121`) も live signal 生成 (`signal_producer.py:229`) も `indicators=None` を渡す → strategy は配備済 indicator (`rsi_wilder` 等) を再利用できず自前計算している (#80 `ema_rsi_pullback`)。設計論点: 渡す indicator の選択 (配備済全部 / strategy config で宣言した依存のみ)、backtest コスト、warmup と `max_bars` の整合、indicator 失敗時の fail closed、改善 worker への indicator 一覧の露出 (`source_snapshot_dir` 経由)。[indicator-first-seeding] と対

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。

