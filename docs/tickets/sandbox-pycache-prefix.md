---
id: sandbox-pycache-prefix
status: 是正済
priority: 低
opened: null
closed: 2026-10-05
related: []
backfilled: true
source_section: 未完了
---
# [sandbox-pycache-prefix]

**状態**: 是正済 / **優先**: 低

## 現象・原因・処置案 (tickets.md からの移行、原文)

[sandbox-pycache-prefix] (低、CR4) `sandbox._build_env` が PYTHONPYCACHEPREFIX 未設定 → 候補 dir に __pycache__、gate_pytest の `_IGNORED_DIR_NAMES` で吸収している。gate worker (gate_pytest.py:183) と同じ設定に

## 修正内容

- 2026-10-05: worker は -B 起動 + sys.dont_write_bytecode、source-only loader で pyc を読まない。候補 dir と venv に __pycache__ が増えないことを E2E で確認

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。
- 2026-10-05: 是正内容 — worker は -B 起動 + sys.dont_write_bytecode、source-only loader で pyc を読まない。候補 dir と venv に __pycache__ が増えないことを E2E で確認
