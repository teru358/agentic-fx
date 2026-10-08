---
id: backtest-kill-switch-latch-truncates-in-sample
status: 是正済
priority: 未設定
opened: 2026-09-06
closed: 2026-09-06
related: []
backfilled: true
source_section: 完了ログ
---
# [backtest-kill-switch-latch-truncates-in-sample]

**状態**: 是正済 / **優先**: 未設定

## 現象・原因・処置案 (tickets.md からの移行、原文)

**[backtest-kill-switch-latch-truncates-in-sample]** (2026-09-06 CP8 診断、Important) **裁定 (a) → 設計 v5 承認 2026-09-06 (`tmp/design-base-interval/kill-switch-replay-design.md`、codex 4 周) → **実装 `ec46902`** (段 0 K1〜K19 全 red、golden-v3、3394 passed) → CP8 再実行: 13 ヶ月 in-sample で trades 251 / pf 0.80 / latches 27 / 59 秒 (backtest_runs #4) → **1 周目完了・クローズ (2026-09-06)**: codex Critical 0 / Major 2 (1 は diff 範囲の誤検出、1 は replay-level pin 不足 → `dea044d`)、ローカル 3 本 本番欠陥 0・pin 5 `2d284a2` (`tmp/stage4/verified-ks-round1.md`)、3402 passed。旧記述: replay 内の drawdown kill switch は -2% 到達で latch し「解除は明示操作」のため in-sample 終了まで発注ゼロ。13 ヶ月の in-sample が実質 2 週間で打ち切られ、-2% を一度でも踏む戦略は全て `insufficient_trades` になる (1m でも同じ、5m 固有ではない)。選択肢: (a) replay では翌取引日に自動解除し `kill_switch_events` に latch 回数を残す (評価継続、live の不可解除規律は不変) (b) latch した run は「評価不能 (kill switch)」として明示的に gate 不合格にする (c) 現状維持。

## 修正内容

原文に commit・レビュー結果・実機受入が含まれる場合はそれが正。当時の詳細記録は `tmp/review-*/`・`tmp/design-*/` (gitignore) と git log。

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。

