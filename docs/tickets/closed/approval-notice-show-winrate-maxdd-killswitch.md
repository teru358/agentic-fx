---
id: approval-notice-show-winrate-maxdd-killswitch
title: 新戦略の通知と承認詳細に勝率・max DD・kill switch 回数を載せる
status: 是正済
priority: 中
kind: 機能
opened: 2026-11-01
closed: 2026-11-01
related: []
---
# [approval-notice-show-winrate-maxdd-killswitch] 新戦略の通知と承認詳細に勝率・max DD・kill switch 回数を載せる

**状態**: 是正済 / **優先**: 中 / **種別**: 機能

## 現象

承認 #84 (us500_lagged_return_fx) の提出通知は「成績 (in-sample): USDJPY / trades 864 / PF 1.002 / avg R 0.004158」だけで、利益が最低限出ていることは分かるが危うい箇所 (勝率 42.9%、max DD 26.3%、kill switch 発火 55 回) が見えない (2026-11-01 ユーザー指摘)。metrics_json には win_rate / max_drawdown / kill_switch_latches が既にある。処置: Discord の提出通知と afx ctl approval show / web / ops DTO の成績行に勝率・max DD・kill switch 回数を in-sample と holdout の両方で追加 (holdout 数値は Discord と人間面のみ、agent 面には出さない既存規律を維持)。表示の語と桁を 1 か所に

## 原因

## 処置案・裁定

## 修正内容

- 2026-11-01: metric_words (勝率 / max DD / kill switch 回) を 1 か所で定義し、CLI / ops client / ops DTO / web の in-sample と holdout の成績行、Discord の提出通知 (in-sample のみ、holdout 非表示の既存 pin を維持) に追加。旧 payload は項目を省く。本体 2889 + 是正後 188、afx-ops 1447 緑

## 経緯

- 2026-11-01: 起票。
- 2026-11-01: 是正内容 — metric_words (勝率 / max DD / kill switch 回) を 1 か所で定義し、CLI / ops client / ops DTO / web の in-sample と holdout の成績行、Discord の提出通知 (in-sample のみ、holdout 非表示の既存 pin を維持) に追加。旧 payload は項目を省く。本体 2889 + 是正後 188、afx-ops 1447 緑
