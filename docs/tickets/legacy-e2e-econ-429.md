---
id: legacy-e2e-econ-429
title: 再起動のたびに経済指標を取得し連打で 429 になる
status: 実装待ち
priority: 中
opened: null
closed: null
related: []
backfilled: true
source_section: 未完了
---
# [legacy-e2e-econ-429]

**状態**: 実装待ち / **優先**: 中

## 現象・原因・処置案 (tickets.md からの移行、原文)

[E2E-econ-429] サービス起動毎に econ fetch が走り、再起動連打で HTTP 429 (実測)。起動時 fetch のレート制御/クールダウンが無い — outbound-request-budget 案件

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。
- 2026-10-02: 状態: 実装待ち、優先: 中、題名: 再起動のたびに経済指標を取得し連打で 429 になる — 仕分け (2026-10-02、現物で成立を確認): 起動時 fetch のクールダウンが無く、外向きリクエスト予算の観点で要対策。 根拠: core/scheduler.py:323-325 で _last_econ が None の起動直後に必ず econ.refresh が走り、_last_econ はメモリのみで再起動連打を抑えない。
