---
id: approval-notice-show-winrate-maxdd-killswitch
title: 新戦略の通知と承認詳細に勝率・max DD・kill switch 回数を載せる
status: 設計待ち
priority: 中
kind: 機能
opened: 2026-11-01
closed: null
related: []
---
# [approval-notice-show-winrate-maxdd-killswitch] 新戦略の通知と承認詳細に勝率・max DD・kill switch 回数を載せる

**状態**: 設計待ち / **優先**: 中 / **種別**: 機能

## 現象

承認 #84 (us500_lagged_return_fx) の提出通知は「成績 (in-sample): USDJPY / trades 864 / PF 1.002 / avg R 0.004158」だけで、利益が最低限出ていることは分かるが危うい箇所 (勝率 42.9%、max DD 26.3%、kill switch 発火 55 回) が見えない (2026-11-01 ユーザー指摘)。metrics_json には win_rate / max_drawdown / kill_switch_latches が既にある。処置: Discord の提出通知と afx ctl approval show / web / ops DTO の成績行に勝率・max DD・kill switch 回数を in-sample と holdout の両方で追加 (holdout 数値は Discord と人間面のみ、agent 面には出さない既存規律を維持)。表示の語と桁を 1 か所に

## 原因

## 処置案・裁定

## 修正内容

## 経緯

- 2026-11-01: 起票。
