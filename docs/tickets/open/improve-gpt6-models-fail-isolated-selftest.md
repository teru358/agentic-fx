---
id: improve-gpt6-models-fail-isolated-selftest
title: gpt-6 系モデルで improve 隔離の自己試験が通らない
status: 設計待ち
priority: 低
kind: 不具合
opened: 2026-10-27
closed: null
related: [selftest-failure-detail-not-logged]
---
# [improve-gpt6-models-fail-isolated-selftest] gpt-6 系モデルで improve 隔離の自己試験が通らない

**状態**: 設計待ち / **優先**: 低 / **種別**: 不具合

## 現象

2026-10-27、runner.improve を codex / gpt-6-luna にして daemon を再起動すると 2 回とも `admitted=no reason=runtime_fingerprint_selftest_failed` で improve が sandbox_unavailable になる。gpt-5.6-luna に戻すと通る。隔離なしの疎通 (afx check backend --backend codex) は gpt-6-luna でも通る。transcript は 1 回目が list_staging を呼ばずに echo だけ返した (指示不遵守)、2 回目は list_staging 呼び出し + 正しい echo + turn.completed で 5.6 の合格時と同形なのに不合格。どの条件で落ちたかは記録されていない (selftest-failure-detail-not-logged を先に直す)。同日 codex CLI が 0.164.0 に更新されているが、更新後も gpt-5.6-luna の mission は隔離下で完走した。目標はどのモデルでも improve が動くこと (ユーザー、2026-10-27)。gpt-6.1-terra も対象。

## 原因

## 処置案・裁定

## 修正内容

## 経緯

- 2026-10-27: 起票。
- 2026-10-28: 優先: 低 — 2026-10-28: 隔離の fingerprint は model を含まない。codex 0.164.0 の値 96cdd7f2… を登録した後、gpt-6-luna に切り替えると自己試験なしで admitted=yes になり、本番の改善 mission は隔離下で完走し指標を提出した (ユーザー確認)。不合格は自己試験 (verify_backend の 1 ターン probe) に限られる。原因は次に自己試験が走ったとき (fingerprint が集合外のとき) の `improve isolation: selftest detail:` 行で読む。別の論点: model を変えても自己試験が走らないので「その model で動く」ことは admission で保証されない (model を fingerprint に含めるか、model ごとの自己試験 cache にするかを決める)。優先度を低に。
- 2026-10-29: 状態: 設計待ち — 2026-10-29: 隔離 source の変更で fingerprint が集合外になり、gpt-6-luna で自己試験が走って合格した (admitted=yes、selftest detail 行なし)。10/27 の 2 回の不合格は再現せず、通ったり落ちたりする疑い (10/27 の 1 回目は list_staging を呼ばなかった)。
- 2026-10-29: 優先: 低 — 2026-10-29 棚卸し: 是正済みの疑い (isolated source 変更で自己試験が再走し gpt-6-luna で合格、detail ログは selftest-failure-detail-not-logged で追加済み)。残件: 再発時のみ再開
