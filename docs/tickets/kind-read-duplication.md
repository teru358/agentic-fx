---
id: kind-read-duplication
title: 候補の kind 読み出しが config.yaml 再パースで重複
status: 実装待ち
priority: 低
opened: null
closed: null
related: []
backfilled: true
source_section: 未完了
---
# [kind-read-duplication]

**状態**: 実装待ち / **優先**: 低

## 現象・原因・処置案 (tickets.md からの移行、原文)

[kind-read-duplication] `_read_candidate_kind` が `candidate_meta.kind` を使わず config.yaml を再パース (CR6 の一本化が及んでいない既存重複)。小、3 周目 F6 起票

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。
- 2026-10-02: 状態: 実装待ち、優先: 低、題名: 候補の kind 読み出しが config.yaml 再パースで重複 — 仕分け (2026-10-02、現物で成立を確認): candidate_meta.kind を使う一本化が未実施。 根拠: improve_loop.py:2902 _read_candidate_kind が config.yaml を直接再パースしている。
