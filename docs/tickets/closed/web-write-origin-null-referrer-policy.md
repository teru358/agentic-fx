---
id: web-write-origin-null-referrer-policy
title: web の書込みが Referrer-Policy の no-referrer で常に 403 になる
status: 是正済
priority: 高
kind: 不具合
opened: 2026-11-15
closed: 2026-11-15
related: [web-plugin-improve-hub]
---
# [web-write-origin-null-referrer-policy] web の書込みが Referrer-Policy の no-referrer で常に 403 になる

**状態**: 是正済 / **優先**: 高 / **種別**: 不具合

## 現象

書込みを有効にした web で承認ボタンを押すと Forbidden になる。web は全応答に Referrer-Policy: no-referrer を付けており、その文書から送る form の POST にはブラウザが Origin: null を付ける (Fetch の append a request Origin header)。書込みは Origin が自分の origin と完全一致することを要求するので必ず 403 になる。test は Origin header を手で付けて送っていたので通っていた

## 原因

## 処置案・裁定

## 修正内容

- 2026-11-15: 書込みが有効な web の応答だけ Referrer-Policy を same-origin にした (読取専用は no-referrer のまま)。外部の origin には Referer を送らず、web は Referer を見ない、確認 token は hidden field で URL に載らないので安全性は保たれる。test は応答 header の分岐、承認詳細 GET (form を載せた文書) と確認画面の header を固定。レビューは sol (Minor 2 を採用)。実ブラウザでの確認は再起動後に利用者が行う

## 経緯

- 2026-11-15: 起票。
- 2026-11-15: 是正内容 — 書込みが有効な web の応答だけ Referrer-Policy を same-origin にした (読取専用は no-referrer のまま)。外部の origin には Referer を送らず、web は Referer を見ない、確認 token は hidden field で URL に載らないので安全性は保たれる。test は応答 header の分岐、承認詳細 GET (form を載せた文書) と確認画面の header を固定。レビューは sol (Minor 2 を採用)。実ブラウザでの確認は再起動後に利用者が行う
