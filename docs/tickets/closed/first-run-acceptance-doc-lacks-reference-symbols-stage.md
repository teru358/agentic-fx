---
id: first-run-acceptance-doc-lacks-reference-symbols-stage
title: 初回設定の受入手順書がウィザードの参照銘柄の段を引用していない
status: 見送り
priority: 低
kind: 保守
opened: 2026-11-07
closed: 2026-11-07
related: []
---
# [first-run-acceptance-doc-lacks-reference-symbols-stage] 初回設定の受入手順書がウィザードの参照銘柄の段を引用していない

**状態**: 見送り / **優先**: 低 / **種別**: 保守

## 現象

tests/acceptance/test_first_run_acceptance_doc.py の 3 件 (見出し・選択肢と問い・案内文の逐語引用) が main で落ちる。docs/operations/first-run-acceptance.md に「[段 3 参照銘柄]」の見出しと、参照銘柄を飛ばすときの案内文 2 種が無い。ウィザードに段が加わった後、手順書が追いついていない (2026-11-07 の全範囲判定で発見)

## 原因

## 処置案・裁定

## 修正内容

## 経緯

- 2026-11-07: 起票。
- 2026-11-07: 見送り理由 — 既存チケット first-run-acceptance-doc-quotes-drift と同じ件 (重複起票)。原因の記録はそちらへ移した
