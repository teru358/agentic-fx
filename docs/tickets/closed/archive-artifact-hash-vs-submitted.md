---
id: archive-artifact-hash-vs-submitted
status: 是正済
priority: 未設定
opened: null
closed: 2026-10-23
related: []
backfilled: true
source_section: 未完了
---
# [archive-artifact-hash-vs-submitted]

**状態**: 是正済 / **優先**: 未設定

## 現象・原因・処置案 (tickets.md からの移行、原文)

[archive-artifact-hash-vs-submitted] (軽微、A4 12 回目 #74 観測 C) `candidate_archives.artifact_hash` (backtest 時点) と approval payload の `artifact_hash` (提出時点、その後 test_plugin.py を直した) が不一致。archive の引き当ては `(mission_id, content_hash)` で行う (artifact_hash は test_plugin.py を含むため提出前の修正でずれる) **→ 是正 `7837fe9`+`d2c103e` (content_hash 引き当て)、A4 15 回目で #6〜#9 の archive= 解決を確認 → クローズ**

## 修正内容

- 2026-10-23: 本文に記録のとおり 7837fe9 + d2c103e で是正済 (状態更新が漏れていた)

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。
- 2026-10-23: 是正内容 — 本文に記録のとおり 7837fe9 + d2c103e で是正済 (状態更新が漏れていた)
