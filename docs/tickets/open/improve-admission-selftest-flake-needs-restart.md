---
id: improve-admission-selftest-flake-needs-restart
title: 改善 admission の自己試験が 1 回落ちると再起動まで改善が止まる
status: 設計待ち
priority: 中
kind: 不具合
opened: 2026-11-01
closed: null
related: []
---
# [improve-admission-selftest-flake-needs-restart] 改善 admission の自己試験が 1 回落ちると再起動まで改善が止まる

**状態**: 設計待ち / **優先**: 中 / **種別**: 不具合

## 現象

2026-11-01 17:53 の afx 再起動時、改善 isolation の admission 自己試験が gate=incomplete (mcp tools=13, call=0, steps=1: codex が tool を呼ばずに応答) で failed になり、admitted=no のまま固定。17:56 の手動 improve #2722 は runner_failed (sandbox_unavailable: runtime_fingerprint_selftest_failed) で 0 秒終了。fingerprint は同日の admitted=yes の 3 回と同一で、LLM 側の一時的な振る舞い (tool 呼出なし) が原因。18:0x の再起動で admitted=yes に戻った。admission は起動時 1 回だけで、失敗理由が自己試験の不完走 (fingerprint 不一致ではない) でも再判定の経路が無い。処置案: 失敗理由が selftest 不完走のときは改善 request 時 (または一定間隔) に自己試験を再実行し、成功すれば admitted に切り替える。fingerprint 不一致は従来どおり固定。回数上限と activity 固定語

## 原因

## 処置案・裁定

## 修正内容

## 経緯

- 2026-11-01: 起票。
- 2026-11-07: 再発 (3 回目)。07:05Z の再起動で同じ gate=incomplete (call=0) → admitted=no、16:27 / 16:33 JST の improve #3254・#3255 が sandbox_unavailable で 0 秒終了。fingerprint 15bd94e5… (codex 0.166.0) が未登録で毎起動に自己試験が走っていた → 登録を更新 (起動時の自己試験を減らす当面の策、本件の是正ではない)
