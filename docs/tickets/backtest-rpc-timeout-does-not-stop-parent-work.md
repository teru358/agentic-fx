---
id: backtest-rpc-timeout-does-not-stop-parent-work
status: 設計待ち
priority: 高
opened: 2026-09-21
closed: null
related: [backtest-cpu-budget-by-call]
backfilled: true
source_section: 完了ログ
---
# [backtest-rpc-timeout-does-not-stop-parent-work]

**状態**: 設計待ち / **優先**: 高

## 現象・原因・処置案 (tickets.md からの移行、原文)

**[backtest-rpc-timeout-does-not-stop-parent-work]** (重要度: 高、2026-09-21 B1 設計レビュー r1 codex sol、未実測): `backtest_rpc_timeout_sec=600` は agent への応答を打ち切るだけで、親サービス内の daemon RPC thread とそれが所有する plugin worker は走り続ける。mission timeout が殺すのは mission worker 側。human CLI の replay には全体 wall timeout が無い。束 B [backtest-cpu-budget-by-call] の設計課題 (`tmp/design-b1/design-B-notes.md`)。

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。

