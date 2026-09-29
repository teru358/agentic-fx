---
id: sandbox-pycache-prefix
status: 設計待ち
priority: 低
opened: null
closed: null
related: []
backfilled: true
source_section: 未完了
---
# [sandbox-pycache-prefix]

**状態**: 設計待ち / **優先**: 低

## 現象・原因・処置案 (tickets.md からの移行、原文)

[sandbox-pycache-prefix] (低、CR4) `sandbox._build_env` が PYTHONPYCACHEPREFIX 未設定 → 候補 dir に __pycache__、gate_pytest の `_IGNORED_DIR_NAMES` で吸収している。gate worker (gate_pytest.py:183) と同じ設定に

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。

