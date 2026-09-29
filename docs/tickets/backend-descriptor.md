---
id: backend-descriptor
status: 設計待ち
priority: 未設定
opened: null
closed: null
related: []
backfilled: true
source_section: 完了ログ
---
# [backend-descriptor]

**状態**: 設計待ち / **優先**: 未設定

## 現象・原因・処置案 (tickets.md からの移行、原文)

[backend-descriptor] (Minor): backend 別 if-ladder ×5 (service/factory/worker_runner/mission_worker/verify_backend) を per-backend descriptor に。llama_swap_verified gate は service.py のみ。

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。

