---
id: observation-carryover-mismatch
title: observation の申し送りと staging の実態が食い違う
status: 設計待ち
priority: 低
opened: null
closed: null
related: []
backfilled: true
source_section: 未完了
---
# [observation-carryover-mismatch]

**状態**: 設計待ち / **優先**: 低

## 現象・原因・処置案 (tickets.md からの移行、原文)

[observation-carryover-mismatch] observation の申し送り (「staging 書込済・再開可能」) と実態 (`afx_list_staging` 空 — staging は mission 終了で drop) が食い違う。申し送り文を staging 非永続前提に直すか、observation 時は staging を保持するか (設計)

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。
- 2026-10-02: 状態: 設計待ち、優先: 低、題名: observation の申し送りと staging の実態が食い違う — 仕分け (2026-10-02、要確認 (再現・実機観測などが要る)): 文言が既に消えている可能性があり、現行の出力を見て決める。 根拠: 申し送り文『staging 書込済・再開可能』は src に見当たらず、現行文言と staging 保持の挙動を確認する必要がある。
