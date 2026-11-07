---
id: first-run-acceptance-doc-quotes-drift
title: 初回受入手順書の逐語引用 test 3 件が main で落ちる
status: 設計待ち
priority: 低
kind: 不具合
opened: 2026-11-03
closed: null
related: [first-run-acceptance-doc-lacks-reference-symbols-stage]
---
# [first-run-acceptance-doc-quotes-drift] 初回受入手順書の逐語引用 test 3 件が main で落ちる

**状態**: 設計待ち / **優先**: 低 / **種別**: 不具合

## 現象

tests/acceptance/test_first_run_acceptance_doc.py の 3 件が main (d00c1db0) で落ちる。wizard の文言と手順書の逐語引用がずれている。原因は未調査

## 原因

## 処置案・裁定

## 修正内容

## 経緯

- 2026-11-03: 起票。
- 2026-11-07: 関連: [first-run-acceptance-doc-lacks-reference-symbols-stage] — 原因: wizard に段 3 参照銘柄が加わった後、docs/operations/first-run-acceptance.md が「[段 3 参照銘柄]」の見出し・選択肢「参照銘柄」・飛ばすときの案内文 2 種を引用していない (2026-11-07 確認、重複起票 first-run-acceptance-doc-lacks-reference-symbols-stage を統合)
