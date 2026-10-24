---
id: web-basic-username-not-shown-to-user
title: web の Basic 認証ユーザー名 afx が利用者に示されない
status: 是正済
priority: 中
opened: 2026-10-24
closed: 2026-10-24
related: [web-lan-access-bind-allow-auth]
---
# [web-basic-username-not-shown-to-user] web の Basic 認証ユーザー名 afx が利用者に示されない

**状態**: 是正済 / **優先**: 中

## 現象

Basic 認証のユーザー名は固定 afx (設計書 §3.3) だが、ウィザードは尋ねず、生成 password の 1 回表示にも status の web-access 行にもユーザー名が出ない。利用者はブラウザのダイアログに何を入れるか分からない (2026-10-24 ユーザー報告)。処置案: password 表示行に「ユーザー名 afx」を添える、要約と status の auth=basic に user=afx を添える、運用文書 §10.2 / §10.5 と first-run-acceptance に明記、逐語 pin を追従

## 原因

## 処置案・裁定

## 修正内容

- 2026-10-24: web-lan T3 (設計書 v0.11a §10.2) に吸収。Basic のユーザー名を入力式 (初期値 afx、ops.toml の user key) にし、生成 password の表示行・要約・status にユーザー名を出す。あわせて待受アドレスの説明と interface 名、「既定」表記の除去、「アクセス制限」の 4 択、password 8〜32 (空で自動生成、手入力は再確認)、既存 Basic の維持 / 再設定の 1 問、運用文書と逐語 pin。codex r1 2 件 + /code-review 8 件 (2 件は spec 裁定で却下) を是正、閉鎖確認通過。判定 本体 2454 / afx-ops 3070 green

## 経緯

- 2026-10-24: 起票。
- 2026-10-24: 是正内容 — web-lan T3 (設計書 v0.11a §10.2) に吸収。Basic のユーザー名を入力式 (初期値 afx、ops.toml の user key) にし、生成 password の表示行・要約・status にユーザー名を出す。あわせて待受アドレスの説明と interface 名、「既定」表記の除去、「アクセス制限」の 4 択、password 8〜32 (空で自動生成、手入力は再確認)、既存 Basic の維持 / 再設定の 1 問、運用文書と逐語 pin。codex r1 2 件 + /code-review 8 件 (2 件は spec 裁定で却下) を是正、閉鎖確認通過。判定 本体 2454 / afx-ops 3070 green
