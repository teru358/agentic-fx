---
id: web-basic-username-not-shown-to-user
title: web の Basic 認証ユーザー名 afx が利用者に示されない
status: 設計待ち
priority: 中
opened: 2026-10-24
closed: null
related: [web-lan-access-bind-allow-auth]
---
# [web-basic-username-not-shown-to-user] web の Basic 認証ユーザー名 afx が利用者に示されない

**状態**: 設計待ち / **優先**: 中

## 現象

Basic 認証のユーザー名は固定 afx (設計書 §3.3) だが、ウィザードは尋ねず、生成 password の 1 回表示にも status の web-access 行にもユーザー名が出ない。利用者はブラウザのダイアログに何を入れるか分からない (2026-10-24 ユーザー報告)。処置案: password 表示行に「ユーザー名 afx」を添える、要約と status の auth=basic に user=afx を添える、運用文書 §10.2 / §10.5 と first-run-acceptance に明記、逐語 pin を追従

## 原因

## 処置案・裁定

## 修正内容

## 経緯

- 2026-10-24: 起票。
