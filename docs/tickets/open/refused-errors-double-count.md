---
id: refused-errors-double-count
status: 設計待ち
priority: 中
opened: null
closed: null
related: []
backfilled: true
source_section: 未完了
---
# [refused-errors-double-count]

**状態**: 設計待ち / **優先**: 中

## 現象・原因・処置案 (tickets.md からの移行、原文)

[refused-errors-double-count] (中、run9 観測 C) 予算拒否は terminal streak と tool_error streak の両方に入り `refused=10 errors=10` と同一事象を二重表示。summary の意味を「refused = 予算拒否」「errors = 例外・引数不正・業務エラー (予算拒否を除く)」に分ける

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。

