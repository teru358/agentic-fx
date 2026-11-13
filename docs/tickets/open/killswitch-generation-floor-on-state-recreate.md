---
id: killswitch-generation-floor-on-state-recreate
title: state を作り直したときの kill switch 世代の再利用を防ぐ
status: 設計待ち
priority: 低
kind: 保守
opened: 2026-11-14
closed: null
related: [auto-killswitch-latch-emits-no-event]
---
# [killswitch-generation-floor-on-state-recreate] state を作り直したときの kill switch 世代の再利用を防ぐ

**状態**: 設計待ち / **優先**: 低 / **種別**: 保守

## 現象

init と wizard が既存の state を引き継ぐ契約は保ったまま、state file が無く新しく作るときだけ、既存の ops_events にある kill switch の最大 generation を下限にする案を検討する。DB や table が無ければ 0。存在する table の読取りに失敗したときに新しい停止理由にする必要があるかと、その運用の復旧手順を評価する。通常は latched_at の時刻照合で通知の重複を防げるので、通知の event 化には入れない。時刻が古い event より前に戻る状況と state の消失が重なる fixture で、残る危険と効果を確かめる。

## 原因

## 処置案・裁定

## 修正内容

## 経緯

- 2026-11-14: 起票。
