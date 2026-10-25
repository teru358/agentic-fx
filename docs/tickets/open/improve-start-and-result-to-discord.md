---
id: improve-start-and-result-to-discord
title: improve の開始と結果を Discord に通知する
status: 設計待ち
priority: 中
opened: 2026-10-25
closed: null
related: [improve-run-summary-for-humans, bot-event-notices-are-raw-log-lines]
---
# [improve-start-and-result-to-discord] improve の開始と結果を Discord に通知する

**状態**: 設計待ち / **優先**: 中

## 現象

`afx ctl improve` や週次の改善 mission が始まったこと、終わったこと (候補を提出した / 床未達で見送った / 失敗した) が Discord (ops-notify / ops-bot) に出ない。利用者は backlog の note か approval list を自分で見るしかない (2026-10-25 ユーザー依頼)。案: 改善 mission の開始・終了を activity / event にし、notify がそれを「improve 開始 (job id、課題)」「improve 終了 (結果: submitted <plugin名 kind> / not_submitted (床未達) / failed (理由語)、所要時間)」の 2 通にして送る。本文は決定論的に組み立て、LLM の自由記述は引用しない。要約の中身 (仮説・指標・床との差) は improve-run-summary-for-humans が担当し、本件は開始・終了の通知だけ。

## 原因

## 処置案・裁定

## 修正内容

## 経緯

- 2026-10-25: 起票。
