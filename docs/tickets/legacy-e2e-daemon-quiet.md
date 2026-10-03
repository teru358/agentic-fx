---
id: legacy-e2e-daemon-quiet
title: stdin が非 TTY だと afx が黙って daemon 動作になる
status: 是正済
priority: 低
opened: null
closed: 2026-10-03
related: []
backfilled: true
source_section: 未完了
---
# [legacy-e2e-daemon-quiet]

**状態**: 是正済 / **優先**: 低

## 現象・原因・処置案 (tickets.md からの移行、原文)

[E2E-daemon-quiet] stdin 非 TTY だと `afx` が黙って daemon モードになる (entry.py `not sys.stdin.isatty()`)。プロンプトも警告も出ず 30 分誤診した。fallback 時に stderr へ 1 行出すべき (Minor)

## 修正内容

- 2026-10-03: 非 TTY fallback 時に stderr へ 1 行出すようにした (entry.py)

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。
- 2026-10-02: 状態: 実装待ち、優先: 低、題名: stdin が非 TTY だと afx が黙って daemon 動作になる — 仕分け (2026-10-02、現物で成立を確認): fallback 時に stderr へ 1 行出す処置が未実施。 根拠: entry.py:28 が not sys.stdin.isatty() で黙って daemon になり、警告を出さない。
- 2026-10-03: 是正内容 — 非 TTY fallback 時に stderr へ 1 行出すようにした (entry.py)
