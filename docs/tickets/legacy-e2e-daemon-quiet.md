---
id: legacy-e2e-daemon-quiet
status: 設計待ち
priority: 未設定
opened: null
closed: null
related: []
backfilled: true
source_section: 未完了
---
# [legacy-e2e-daemon-quiet]

**状態**: 設計待ち / **優先**: 未設定

## 現象・原因・処置案 (tickets.md からの移行、原文)

[E2E-daemon-quiet] stdin 非 TTY だと `afx` が黙って daemon モードになる (entry.py `not sys.stdin.isatty()`)。プロンプトも警告も出ず 30 分誤診した。fallback 時に stderr へ 1 行出すべき (Minor)

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。

