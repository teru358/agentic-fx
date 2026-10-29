---
id: strategy-attribution-and-shadow-evaluation
title: 戦略ごとの帰属と影の評価 (共有建玉のまま戦略別の成績を出す)
status: 設計待ち
priority: 中
opened: 2026-10-16
closed: null
related: [improve-objective-filtered-signal-quality, bot-readonly-slash-commands, per-strategy-virtual-books, plugin-naming-lifecycle-policy]
---
# [strategy-attribution-and-shadow-evaluation] 戦略ごとの帰属と影の評価 (共有建玉のまま戦略別の成績を出す)

**状態**: 設計待ち / **優先**: 中 / **種別**: 機能

## 現象

戦略 plugin は複数配備でき signal は全部 LLM に渡るが、orders / TradeIntent に発注元の戦略・signal の帰属が無く、ライブで「どの戦略が勝っているか」が測れない (改善ループの選別材料がバックテストだけ)。設計 (2026-10-16 ユーザー承認): 建玉は 1 冊のまま、(1) TradeIntent と orders に採用 signal id と主戦略の帰属 tag を持たせる、(2) 全戦略の signal を「建てていたら」で機械的に採点する影の帳簿 (entry = バー終値、exit = 戦略の規則か SL/TP、確定足で判定、スプレッド相当の固定コストを引く。LLM が採用しなかった signal も含む)、(3) 戦略別の勝率・PF・DD・採用率を afx ctl status (後に web / bot) で表示。影の成績は実約定を含まないので楽観側に出る旨を表示に添える。exit 規則が無い戦略の評価定義は設計で決める。これが per-strategy-virtual-books の着手条件 (勝ち筋 2 本以上) を測る土台

## 原因

## 処置案・裁定

## 修正内容

## 経緯

- 2026-10-16: 起票。
- 2026-10-29: 関連: [improve-objective-filtered-signal-quality, bot-readonly-slash-commands, per-strategy-virtual-books, plugin-naming-lifecycle-policy] — 2026-10-29 棚卸し: 関連を束ねる先に張る
