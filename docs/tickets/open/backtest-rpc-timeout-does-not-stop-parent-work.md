---
id: backtest-rpc-timeout-does-not-stop-parent-work
title: backtest の待ち時間超過後も親の処理と worker が走り続ける
status: 設計待ち
priority: 中
opened: 2026-09-21
closed: null
related: [backtest-cpu-budget-by-call]
backfilled: true
source_section: 完了ログ
---
# [backtest-rpc-timeout-does-not-stop-parent-work]

**状態**: 設計待ち / **優先**: 中

## 現象・原因・処置案 (tickets.md からの移行、原文)

**[backtest-rpc-timeout-does-not-stop-parent-work]** (重要度: 高、2026-09-21 B1 設計レビュー r1 codex sol、未実測): `backtest_rpc_timeout_sec=600` は agent への応答を打ち切るだけで、親サービス内の daemon RPC thread とそれが所有する plugin worker は走り続ける。mission timeout が殺すのは mission worker 側。human CLI の replay には全体 wall timeout が無い。束 B [backtest-cpu-budget-by-call] の設計課題 (`tmp/design-b1/design-B-notes.md`)。

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。
- 2026-10-02: 優先: 中、題名: backtest の待ち時間超過後も親の処理と worker が走り続ける — 実測 (2026-10-02): 主張は確認。待ち時間超過の後、親の thread と plugin worker は backtest の完了・CPU 上限 60 秒・呼び出し上限 10 秒のいずれかまで走る (最長 55.9 秒を観測)。資金保護とは lock も DB 接続も共有せず、現行の 600 秒設定での実発生は 0 件のため中に下げる。未計測: mission 終了後に最大 605 秒 improve の枠を占有し得る点。最小対処 = 超過時に親側も協調して中断する
