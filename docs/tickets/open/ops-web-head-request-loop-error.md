---
id: ops-web-head-request-loop-error
title: 同梱 web が HEAD / 早期切断で web_loop_error AttributeError を 1 行出す
status: 設計待ち
priority: 低
opened: 2026-10-24
closed: null
related: [web-lan-access-bind-allow-auth]
---
# [ops-web-head-request-loop-error] 同梱 web が HEAD / 早期切断で web_loop_error AttributeError を 1 行出す

**状態**: 設計待ち / **優先**: 低

## 現象

稼働中の afx-ops web (main 7f63a4d 時点の venv) に 127.0.0.1:8788 へ GET と HEAD を 1 回ずつ送ると、journal に `afx-ops: web_loop_error AttributeError` が 1 行出る (2026-10-24 09:00 頃、2 回観測)。応答自体は 200。例外型名しか出ないので原因箇所が読めない。web-lan T1 の Host 検査実装で HEAD の扱いを固定する test (web/test_host_policy.py) が入るので、T1 merge 後に再現するかを確認し、残れば request.py の HEAD 経路を直す

## 原因

## 処置案・裁定

## 修正内容

## 経緯

- 2026-10-24: 起票。
