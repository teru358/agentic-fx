---
id: web-write-origin-null-referrer-policy
title: web の書込みが Referrer-Policy の no-referrer で常に 403 になる
status: 設計待ち
priority: 高
kind: 不具合
opened: 2026-11-15
closed: null
related: [web-plugin-improve-hub]
---
# [web-write-origin-null-referrer-policy] web の書込みが Referrer-Policy の no-referrer で常に 403 になる

**状態**: 設計待ち / **優先**: 高 / **種別**: 不具合

## 現象

書込みを有効にした web で承認ボタンを押すと Forbidden になる。web は全応答に Referrer-Policy: no-referrer を付けており、その文書から送る form の POST にはブラウザが Origin: null を付ける (Fetch の request origin の直列化)。書込みは Origin が自分の origin と完全一致することを要求するので必ず 403 になる。test は Origin header を手で付けて送っていたので通っていた

## 原因

## 処置案・裁定

## 修正内容

## 経緯

- 2026-11-15: 起票。
