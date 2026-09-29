---
id: test-isolation
status: 実装待ち
priority: 未設定
opened: null
closed: null
related: []
backfilled: true
source_section: 未完了
---
# [test-isolation]

**状態**: 実装待ち / **優先**: 未設定

## 現象・原因・処置案 (tickets.md からの移行、原文)

[test-isolation] tests/loops/conftest.py の loop_min/loop_full が特定 4 ファイル同時の部分実行で不可視化し 18 本消える (全スイートでは発生せず)。verified-local-round1.md (束 F) 付録参照。原因未特定

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。

