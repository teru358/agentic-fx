---
id: legacy-e2e-egress
status: 設計待ち
priority: 未設定
opened: 2026-08-30
closed: null
related: []
backfilled: true
source_section: 未完了
---
# [legacy-e2e-egress]

**状態**: 設計待ち / **優先**: 未設定

## 現象・原因・処置案 (tickets.md からの移行、原文)

[E2E-egress] provider=llama_swap + `--disable apps` でも codex 子が chatgpt.com / ab.chatgpt.com へ TCP 443 ×1 (strace 実測 2026-08-30、`tmp/e2e-runbook/verify-backend-connect.txt`)。起票 §10「codex apps egress の停止手段」系 — 停止手段の調査要

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。

