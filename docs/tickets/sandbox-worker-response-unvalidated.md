---
id: sandbox-worker-response-unvalidated
title: worker 応答の pid と error 文字列が無検証で session とログに入る
status: 設計待ち
priority: 中
opened: 2026-09-30
closed: null
related: [worker-death-cause-observed-by-parent]
---
# [sandbox-worker-response-unvalidated] worker 応答の pid と error 文字列が無検証で session とログに入る

**状態**: 設計待ち / **優先**: 中

## 現象

plugin worker の応答に含まれる pid が検証なしで session.pid に入り、orphan 回収ログの行を偽造できる。応答の error 文字列は escape も長さ制限もなく SandboxError のメッセージになり、改行や制御文字が呼び出し側のログや提案レポートへ渡る。

## 原因

応答の各フィールドを型・範囲で検査していない (dict 以外の JSON は protocol_error に是正済)。

## 処置案・裁定

## 修正内容

## 経緯

- 2026-09-30: 起票。
