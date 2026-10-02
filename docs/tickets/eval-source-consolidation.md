---
id: eval-source-consolidation
title: 評価 source が 4 箇所に散る問題は是正済
status: 是正済
priority: 未設定
opened: 2026-09-03
closed: 2026-10-02
related: []
backfilled: true
source_section: 未レビュー束
---
# [eval-source-consolidation]

**状態**: 是正済 / **優先**: 未設定

## 現象・原因・処置案 (tickets.md からの移行、原文)

[eval-source-consolidation] **完了 442f5f6 2026-09-03、未レビュー** — `_EVAL_SOURCE`×3 + `ANALYSIS_SOURCE` → `settings.backtest.eval_source` (既定 dukascopy、IMPORT_SOURCES 検証、example 同期)。実装 codex、フルスイート 3181 passed (fail は既知 flake のみ)。個人設定は `holdout_months: 1` + `eval_source: "mt5"` (バックアップ `tmp/settings.yaml.bak-20260903`)

## 修正内容

- 2026-10-02: 仕分け (2026-10-02) で是正済と確認。評価 source を backtest.eval_source に集約 (442f5f6)、レビュー済み。 根拠: 442f5f6 (settings.backtest.eval_source に集約)。束レビュー完了 cf23fa2。

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。
- 2026-10-02: 題名: 評価 source が 4 箇所に散る問題は是正済 — 仕分け (2026-10-02): 題名を付与
- 2026-10-02: 是正内容 — 仕分け (2026-10-02) で是正済と確認。評価 source を backtest.eval_source に集約 (442f5f6)、レビュー済み。 根拠: 442f5f6 (settings.backtest.eval_source に集約)。束レビュー完了 cf23fa2。
