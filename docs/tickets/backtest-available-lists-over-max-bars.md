---
id: backtest-available-lists-over-max-bars
status: 設計待ち
priority: 未設定
opened: 2026-09-18
closed: null
related: []
backfilled: true
source_section: 未完了
---
# [backtest-available-lists-over-max-bars]

**状態**: 設計待ち / **優先**: 未設定

## 現象・原因・処置案 (tickets.md からの移行、原文)

[backtest-available-lists-over-max-bars] 起票 2026-09-18 (同 T-4、低): `run_backtest_handler` の `available` は `outputs is not None` だけで絞るので、`over_max_bars_limit` で拒否した indicator 自身が候補として返る。agent への軽い誤誘導、遮断 8 違反ではない

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。

