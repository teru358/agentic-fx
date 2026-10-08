---
id: settings-hash-excludes-improve-gate
status: 設計待ち
priority: 低
opened: null
closed: null
related: [profitability-floor]
backfilled: true
source_section: 未完了
---
# [settings-hash-excludes-improve-gate]

**状態**: 設計待ち / **優先**: 低

## 現象・原因・処置案 (tickets.md からの移行、原文)

[settings-hash-excludes-improve-gate] (低、同 調査) `settings_snapshot_hash` は `risk` + `backtest` のみ → `improve.gate` 閾値を緩めても `backtest_runs.settings_hash` は変わらない。[profitability-floor] T3 では適用閾値をレポート/activity に逐語で書いて回避。hash に足すと既存行の identity が変わるため別裁定

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。

