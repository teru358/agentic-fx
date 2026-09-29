---
id: eval-source-consolidation
status: 実装中
priority: 未設定
opened: 2026-09-03
closed: null
related: []
backfilled: true
source_section: 未レビュー束
---
# [eval-source-consolidation]

**状態**: 実装中 / **優先**: 未設定

## 現象・原因・処置案 (tickets.md からの移行、原文)

[eval-source-consolidation] **完了 442f5f6 2026-09-03、未レビュー** — `_EVAL_SOURCE`×3 + `ANALYSIS_SOURCE` → `settings.backtest.eval_source` (既定 dukascopy、IMPORT_SOURCES 検証、example 同期)。実装 codex、フルスイート 3181 passed (fail は既知 flake のみ)。個人設定は `holdout_months: 1` + `eval_source: "mt5"` (バックアップ `tmp/settings.yaml.bak-20260903`)

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。

