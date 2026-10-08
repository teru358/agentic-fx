---
id: improve-add-quotes
title: improve add がクォートを剥がさず表示が崩れる
status: 実装待ち
priority: 低
opened: null
closed: null
related: [sweep-empty-dirs]
backfilled: true
source_section: 未完了
---
# [improve-add-quotes]

**状態**: 実装待ち / **優先**: 低

## 現象・原因・処置案 (tickets.md からの移行、原文)

[improve-add-quotes] cosmetic: `improve add` がクォートを剥がさない (`commands.py:189`)。[sweep-empty-dirs] cosmetic: sweep 後に空の `_staging/{34,38,40}` が残る

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。
- 2026-10-02: 状態: 実装待ち、優先: 低、題名: improve add がクォートを剥がさず表示が崩れる — 仕分け (2026-10-02、現物で成立を確認): cosmetic、sweep 後の空 _staging も残る可能性。 根拠: commands.py:36-46 _normalize_idea_display は制御文字除去と strip のみでクォートを剥がさない。
