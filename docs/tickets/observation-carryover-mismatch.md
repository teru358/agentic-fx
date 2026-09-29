---
id: observation-carryover-mismatch
status: 設計待ち
priority: 未設定
opened: null
closed: null
related: []
backfilled: true
source_section: 未完了
---
# [observation-carryover-mismatch]

**状態**: 設計待ち / **優先**: 未設定

## 現象・原因・処置案 (tickets.md からの移行、原文)

[observation-carryover-mismatch] observation の申し送り (「staging 書込済・再開可能」) と実態 (`afx_list_staging` 空 — staging は mission 終了で drop) が食い違う。申し送り文を staging 非永続前提に直すか、observation 時は staging を保持するか (設計)

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。

