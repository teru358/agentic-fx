---
id: plugin-sees-live-spread
title: plugin と判断 mission が実 spread を見られるようにする
status: 設計待ち
priority: 低
opened: 2026-10-01
closed: null
related: []
---
# [plugin-sees-live-spread] plugin と判断 mission が実 spread を見られるようにする

**状態**: 設計待ち / **優先**: 低

## 現象

spread が広い間に入るかどうかは戦略が判定することだが、判定の材料が渡っていない。bridge の気配応答は spread_points を返すのに、アプリ側の気配取得はそれを保持せず、plugin (strategy / indicator) と判断 mission の読み取りツールから実 spread が見えない。実測: USDJPY は平常 4〜6 points、ロールオーバー前後は 24〜72、最大 152 (2026-09-30)。

## 原因

気配の取り込みが bid / ask / 時刻だけを内部表現に写しており、spread を落としている (設計レビューでの指摘、現物の再確認は着手時に行う)。

## 処置案・裁定

本体は spread で発注を止めない (コストと期待値の比較は戦略の判断)。提供の形だけを決める: plugin の入力・判断 mission のツール応答に実 spread を載せる。無 tick の間は last-known 値である点を明記する。

## 修正内容

## 経緯

- 2026-10-01: 起票。
