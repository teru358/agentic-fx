---
id: backend-descriptor
title: backend 別の if 分岐が 5 ファイルに散在
status: 設計待ち
priority: 低
opened: null
closed: null
related: []
backfilled: true
source_section: 完了ログ
---
# [backend-descriptor]

**状態**: 設計待ち / **優先**: 低

## 現象・原因・処置案 (tickets.md からの移行、原文)

[backend-descriptor] (Minor): backend 別 if-ladder ×5 (service/factory/worker_runner/mission_worker/verify_backend) を per-backend descriptor に。llama_swap_verified gate は service.py のみ。

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。
- 2026-10-02: 状態: 設計待ち、優先: 低、題名: backend 別の if 分岐が 5 ファイルに散在 — 仕分け (2026-10-02、現物で成立を確認): per-backend descriptor にまとめる整理 (Minor)。 根拠: チケットが挙げる 5 箇所の backend 別分岐は、grep で整理済みの形跡を確認できていない。
