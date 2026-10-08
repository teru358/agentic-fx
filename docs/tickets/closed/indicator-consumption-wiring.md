---
id: indicator-consumption-wiring
title: 戦略が配備済みの指標を受け取れず自前で計算している
status: 是正済
priority: 高
opened: 2026-09-12
closed: 2026-10-02
related: [indicator-first-seeding]
backfilled: true
source_section: 未完了
---
# [indicator-consumption-wiring]

**状態**: 是正済 / **優先**: 高

## 現象・原因・処置案 (tickets.md からの移行、原文)

**[indicator-consumption-wiring] (高、ユーザー方針「指標層 → 戦略層」の本丸、2026-09-12)** plugin 契約 `evaluate(df, indicators, signals, params)` は indicators を受け取る形だが、backtest (`strategy_adapter.py:121`) も live signal 生成 (`signal_producer.py:229`) も `indicators=None` を渡す → strategy は配備済 indicator (`rsi_wilder` 等) を再利用できず自前計算している (#80 `ema_rsi_pullback`)。設計論点: 渡す indicator の選択 (配備済全部 / strategy config で宣言した依存のみ)、backtest コスト、warmup と `max_bars` の整合、indicator 失敗時の fail closed、改善 worker への indicator 一覧の露出 (`source_snapshot_dir` 経由)。[indicator-first-seeding] と対

## 修正内容

- 2026-10-02: 仕分け (2026-10-02) で是正済と確認。実装 9 task 完了 (プラン v1.5、4eb3b7b) と段 0 の pin (3ce8c46・0d57e72・6bef2b4) が main にある。移行時に状態が設計待ちのまま取り込まれていた

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。
- 2026-10-02: 題名: 戦略が配備済みの指標を受け取れず自前で計算している — 仕分け (2026-10-02): 題名を付与
- 2026-10-02: 是正内容 — 仕分け (2026-10-02) で是正済と確認。実装 9 task 完了 (プラン v1.5、4eb3b7b) と段 0 の pin (3ce8c46・0d57e72・6bef2b4) が main にある。移行時に状態が設計待ちのまま取り込まれていた
