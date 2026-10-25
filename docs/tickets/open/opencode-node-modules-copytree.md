---
id: opencode-node-modules-copytree
status: 設計待ち
priority: 低
opened: null
closed: null
related: []
backfilled: true
source_section: 未完了
kind: 保守
---
# [opencode-node-modules-copytree]

**状態**: 設計待ち / **優先**: 低 / **種別**: 保守

## 現象・原因・処置案 (tickets.md からの移行、原文)

[opencode-node-modules-copytree] (低、CR7) mission 毎に node_modules 63MB/3648 file を copytree。symlink + Landlock read allowlist 案 (os.link は EXDEV で不可)

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。
- 2026-10-25: 種別: 保守 — 種別を付与
