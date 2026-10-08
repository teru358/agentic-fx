---
id: service-secret-env-leaked-matches-redundant
title: 秘密 env 検査の leaked と matches が冗長
status: 是正済
priority: 低
opened: 2026-09-27
closed: 2026-10-03
related: [switch-ops-code-review-r2-altitude]
backfilled: true
source_section: 未完了
---
# [service-secret-env-leaked-matches-redundant]

**状態**: 是正済 / **優先**: 低

## 現象・原因・処置案 (tickets.md からの移行、原文)

**[service-secret-env-leaked-matches-redundant] 起票 2026-09-27 (同、整理級)**: `service.py` 検査⑤の `leaked` list と `matches` dict が常に同期して埋まり冗長。`matches` に一本化 ([switch-ops-code-review-r2-altitude] と同種)

## 修正内容

- 2026-10-03: leaked list を削除し matches dict に一本化

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。
- 2026-10-02: 状態: 実装待ち、優先: 低、題名: 秘密 env 検査の leaked と matches が冗長 — 仕分け (2026-10-02、現物で成立を確認): matches に一本化する整理。 根拠: service.py:298-311 の leaked list と matches dict が常に同期して冗長。
- 2026-10-03: 是正内容 — leaked list を削除し matches dict に一本化
