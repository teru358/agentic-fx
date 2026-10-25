---
id: loader-double-read-hash
status: 設計待ち
priority: 低
opened: null
closed: null
related: []
backfilled: true
source_section: 未完了
kind: 保守
---
# [loader-double-read-hash]

**状態**: 設計待ち / **優先**: 低 / **種別**: 保守

## 現象・原因・処置案 (tickets.md からの移行、原文)

[loader-double-read-hash] (低、CR6) `_discover_one` が bytes を持ちながら path 版 `content_hash(entry)` で再読込 → artifact_hash と別スナップショット。`content_hash_bytes` に

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。
- 2026-10-25: 種別: 保守 — 種別を付与
