---
id: seed-priority
title: ユーザーの種まきより古い observation が選ばれ指示が無視された
status: 裁定待ち
priority: 低
opened: null
closed: null
related: []
backfilled: true
source_section: 未完了
---
# [seed-priority]

**状態**: 裁定待ち / **優先**: 低

## 現象・原因・処置案 (tickets.md からの移行、原文)

[seed-priority] ユーザー種まき #26 (open) より過去 observation #18 が選ばれ #26 の指示 (5/20 固定) が無視された。規約上正当だが source=user の open を優先させるかは設計判断

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。
- 2026-10-02: 状態: 裁定待ち、優先: 低、題名: ユーザーの種まきより古い observation が選ばれ指示が無視された — 仕分け (2026-10-02、現物で成立を確認): source=user の open を優先させるかの判断が要る。 根拠: store/backlog.py:26,60 は ORDER BY id で選び、source=user を優先する規則は見当たらない。
