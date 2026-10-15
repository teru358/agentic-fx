---
id: bot-readonly-slash-commands
title: Discord bot に読み取り専用の slash command を足す
status: 設計待ち
priority: 中
opened: 2026-10-15
closed: null
related: [cli-surface-principle-and-reorg, news-source-cli-and-429-backoff]
---
# [bot-readonly-slash-commands] Discord bot に読み取り専用の slash command を足す

**状態**: 設計待ち / **優先**: 中

## 現象

運用状態を確認する手立てが少ない (2026-10-15 ユーザー指摘)。bot は event 通知と承認ボタンだけで、状態を自分から見に行けない。web は読み取り専用の設計があるが未実装。設計 (ユーザー承認): bot は afx ctl と同じ操作面 (操作 API) に乗るので、読み取り系を slash command で写す: /status (= afx ctl status: mode・残高・直近 mission・data・未完の手順)、/approvals (承認待ち一覧。決定はボタンの二段操作に限定し slash からは決めない)、/activity [n] [category]、/backlog。既存 BotClient を使い scope は読み取りだけ (status.read / events.read 等)、message_content intent は不要のまま、応答は ephemeral、guild / channel / allowlist 確認の後に defer してから worker thread で API を呼ぶ (afx-ops spec §3.2 の規律)。書き込み系 (policy add / backlog add) は載せない (別判断)。順序: afx news 束の後、web の前。afx-ops spec §3.2 に slash command の節と scope 表を追記

## 原因

## 処置案・裁定

## 修正内容

## 経緯

- 2026-10-15: 起票。
