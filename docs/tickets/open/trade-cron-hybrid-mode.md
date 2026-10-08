---
id: trade-cron-hybrid-mode
status: 設計待ち
priority: 中
opened: 2026-09-28
closed: null
related: [backtest-worker-cpu-budget-shrinks-with-timeframe]
backfilled: true
source_section: 未完了
---
# [trade-cron-hybrid-mode]

**状態**: 設計待ち / **優先**: 中

## 現象・原因・処置案 (tickets.md からの移行、原文)

**[trade-cron-hybrid-mode] 起票 2026-09-28 (中〜高、ユーザー設計論点、C0 v0.1 = `tmp/design-cron-hybrid/C0.md`、astra 途中)**: 取引判断 LLM の起動をハイブリッドにする — 建玉・未約定指値が**ある**間だけ判断足ごとの cron mission (建玉管理: close/cancel の提案)、建玉が**ない**間は strategy signal が出たときだけ mission を起こす (現行 D2「建玉ゼロの間は signal で起こさない」を反転)。見回りの低頻度 cron (1h / 1 日 1 回 / なし) は利用者設定 (`trade.cron_mode: every_bar | positions_only | off` 案、既定は安全側 = positions_only)。根拠 = 9/20〜 132 mission 中 hold 118、9/28 の 11 mission 全部で「signal 無く一致対象なし」が hold の第一理由、GPU 稼働 15〜20% と改善ループとの取り合い。前提 = 改善ループが 15m 戦略を出せること ([backtest-worker-cpu-budget-shrinks-with-timeframe])。B-2 spec の D2 / AC-B1-12 を変える設計変更 → astra advise → 短い C0 → ユーザー承認 → 実装。**順序: ② B1 束 A + CPU 予算 束 B の後、③ A2-3c 残段の前**。directives.md の「signal との一致を確認」文言の見直しはユーザー判断

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。

