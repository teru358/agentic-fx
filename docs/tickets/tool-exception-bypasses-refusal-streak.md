---
id: tool-exception-bypasses-refusal-streak
status: 是正済
priority: 未設定
opened: 2026-09-09
closed: 2026-09-09
related: []
backfilled: true
source_section: 未完了
---
# [tool-exception-bypasses-refusal-streak]

**状態**: 是正済 / **優先**: 未設定

## 現象・原因・処置案 (tickets.md からの移行、原文)

**[tool-exception-bypasses-refusal-streak] 是正済み 2026-09-09** `f432dbf`/`7e0fc16` (on_result フック、(tool, 失敗種別) の streak、E2E)。run9 で dict error 経路のみ実機確認、例外経路は未通過 (観測 E)。副作用: refused と errors の二重計上 (観測 C、要整理) (旧: 重要、run8 欠陥 B) `ToolRegistry.execute` の except 経路 (`{"error": …}`) は counters を通らず、壊れた tool の反復 (293 回・23 分) を `max_refusal_streak=10` が止めない。`refused=0` と過小申告。是正: execute の例外経路で tool 名単位の recoverable streak を記録 (同 tool の成功でリセット)

## 修正内容

原文に commit・レビュー結果・実機受入が含まれる場合はそれが正。当時の詳細記録は `tmp/review-*/`・`tmp/design-*/` (gitignore) と git log。

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。

