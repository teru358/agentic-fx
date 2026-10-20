---
id: trade-order-and-close-notices
title: 発注内容と決済情報を Discord に通知する
status: 設計待ち
priority: 中
opened: 2026-10-20
closed: null
related: [bot-event-notices-are-raw-log-lines, approval-notice-lacks-change-content]
---
# [trade-order-and-close-notices] 発注内容と決済情報を Discord に通知する

**状態**: 設計待ち / **優先**: 中

## 現象

操作 API の event は操作要求・承認・job・guard・健全性の 6 種だけで、発注 (pair / 方向 / 数量 / 価格 / SL・TP / 戦略) と決済 (約定価格 / 損益 / 保有時間 / 理由) の event が無い。利用者は Discord で取引が起きたこと・結果を知れない (web と afx ctl で後から見るだけ)。ユーザー要望 2026-10-20。設計で決める点: event 種別と値 (code・識別子・数値のみ)、daemon のどこで emit するか (executor / state / 決済検知)、ペーパーと実発注の区別、改善 agent に成績が戻らない遮断との関係、bot の文面と既定 (出す / 出さない)。

## 原因

## 処置案・裁定

## 修正内容

## 経緯

- 2026-10-20: 起票。
