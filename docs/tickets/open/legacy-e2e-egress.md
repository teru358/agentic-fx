---
id: legacy-e2e-egress
title: codex 子プロセスが apps 無効でも chatgpt.com に接続する
status: 設計待ち
priority: 低
opened: 2026-08-30
closed: null
related: []
backfilled: true
source_section: 未完了
---
# [legacy-e2e-egress]

**状態**: 設計待ち / **優先**: 低

## 現象・原因・処置案 (tickets.md からの移行、原文)

[E2E-egress] provider=llama_swap + `--disable apps` でも codex 子が chatgpt.com / ab.chatgpt.com へ TCP 443 ×1 (strace 実測 2026-08-30、`tmp/e2e-runbook/verify-backend-connect.txt`)。起票 §10「codex apps egress の停止手段」系 — 停止手段の調査要

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。
- 2026-10-02: 状態: 設計待ち、優先: 低、題名: codex 子プロセスが apps 無効でも chatgpt.com に接続する — 仕分け (2026-10-02、要確認 (再現・実機観測などが要る)): --disable apps でも ×1 接続が残る (2026-08-30 実測)、停止手段の調査待ち。 根拠: codex_runner.py:63 で --disable apps は指定済みだが、chatgpt.com への TCP 443 が出る件は strace 実測のみで再確認が要る。停止手段の調査が決め手。
