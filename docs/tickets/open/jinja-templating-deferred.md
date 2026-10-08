---
id: jinja-templating-deferred
title: 改善プロンプトの {{ }} 脱出が読みにくい (jinja 化は保留)
status: 設計待ち
priority: 低
opened: 2026-09-07
closed: null
related: []
backfilled: true
source_section: 未完了
---
# [jinja-templating-deferred]

**状態**: 設計待ち / **優先**: 低

## 現象・原因・処置案 (tickets.md からの移行、原文)

[jinja-templating-deferred] 2026-09-07 相談: プロンプトは `str.format` のまま。jinja 化は改善 mission の種別分岐 (strategy/indicator/signal でプロンプトを出し分け) を入れるときに `improve_mission.md` だけ移行 (JSON 例の `{{ }}` 脱出が読みにくい実害あり)

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。
- 2026-10-02: 状態: 設計待ち、優先: 低、題名: 改善プロンプトの {{ }} 脱出が読みにくい (jinja 化は保留) — 仕分け (2026-10-02、現物で成立を確認): 種別分岐を入れる時に improve_mission.md だけ移行する。 根拠: プロンプトは str.format のまま (improve_mission.md の JSON 例が {{ }} 脱出)。種別分岐を入れるまで移行しない方針。
