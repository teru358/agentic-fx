---
id: weekly-improve-rebacktests-deployed-strategies
title: 週次改善の冒頭で稼働中の戦略を再 backtest し、成績を改善の方向性の材料にする
status: 設計待ち
priority: 中
kind: 機能
opened: 2026-10-25
closed: null
related: [improve-daily-params-weekly-algorithm, baseline-replay-unimplemented, improve-feedback-structure]
---
# [weekly-improve-rebacktests-deployed-strategies] 週次改善の冒頭で稼働中の戦略を再 backtest し、成績を改善の方向性の材料にする

**状態**: 設計待ち / **優先**: 中 / **種別**: 機能

## 現象

稼働中 (配備済み・有効) の戦略は配備時の backtest 成績しか持たず、相場が変わっても気づけない (陳腐化)。週次の戦略改善 mission は新候補を探すだけで、いま動いている戦略の最新成績を見ていない。週次 (日次改善が入れば日次とも兼ね合う) の改善の冒頭で、稼働中の戦略を最新の履歴で再 backtest し、成績の推移 (配備時 → 今) で改善の方向性 (どの族を伸ばす・直す・止める) を定め、調整の材料にしたい (2026-10-25 ユーザー依頼)。

## 原因

## 処置案・裁定

設計で決める点: (a) 再 backtest は決定論的コア側で mission の前に走らせ、結果は族別試行結果 (改善 feedback、2026-10-24 承認) と同じ表に「稼働中の最新成績」として載せる。agent には in_sample 相当の数値だけ (遮断 8: holdout 側の数値・条件は見せない。再 backtest の区間の切り方をここで決める)。(b) 人間向けには親専有レポートと Discord 通知 (improve-start-and-result-to-discord の開始通知に 1 行足す案)。(c) 成績の低下を機械的にどう扱うか (自動停止はしない。drawdown kill switch は別系。方向性の提示と人間承認の材料に留める) はユーザー裁定。(d) baseline-replay-unimplemented (比較基準の replay) と実装を共有できるか。(e) cadence は improve-daily-params-weekly-algorithm の分割に合わせる。

## 修正内容

## 経緯

- 2026-10-25: 起票。
