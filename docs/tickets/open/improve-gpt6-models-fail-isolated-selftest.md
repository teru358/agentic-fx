---
id: improve-gpt6-models-fail-isolated-selftest
title: gpt-6 系モデルで improve 隔離の自己試験が通らない
status: 設計待ち
priority: 中
kind: 不具合
opened: 2026-10-27
closed: null
related: [selftest-failure-detail-not-logged]
---
# [improve-gpt6-models-fail-isolated-selftest] gpt-6 系モデルで improve 隔離の自己試験が通らない

**状態**: 設計待ち / **優先**: 中 / **種別**: 不具合

## 現象

2026-10-27、runner.improve を codex / gpt-6-luna にして daemon を再起動すると 2 回とも `admitted=no reason=runtime_fingerprint_selftest_failed` で improve が sandbox_unavailable になる。gpt-5.6-luna に戻すと通る。隔離なしの疎通 (afx check backend --backend codex) は gpt-6-luna でも通る。transcript は 1 回目が list_staging を呼ばずに echo だけ返した (指示不遵守)、2 回目は list_staging 呼び出し + 正しい echo + turn.completed で 5.6 の合格時と同形なのに不合格。どの条件で落ちたかは記録されていない (selftest-failure-detail-not-logged を先に直す)。同日 codex CLI が 0.164.0 に更新されているが、更新後も gpt-5.6-luna の mission は隔離下で完走した。目標はどのモデルでも improve が動くこと (ユーザー、2026-10-27)。gpt-6.1-terra も対象。

## 原因

## 処置案・裁定

## 修正内容

## 経緯

- 2026-10-27: 起票。
