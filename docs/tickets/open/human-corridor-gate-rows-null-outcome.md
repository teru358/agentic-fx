---
id: human-corridor-gate-rows-null-outcome
status: 設計待ち
priority: 中
opened: 2026-09-12
closed: null
related: [profitability-floor]
backfilled: true
source_section: 未完了
---
# [human-corridor-gate-rows-null-outcome]

**状態**: 設計待ち / **優先**: 中

## 現象・原因・処置案 (tickets.md からの移行、原文)

[human-corridor-gate-rows-null-outcome] (中、2026-09-12 opus 調査、codex 設計 r1 I2 の副産物) 人間回廊 (`submit_candidate` / `bless_candidate` → `_run_full_gate`) は `record_fn` を渡さず gate 行が `mission_outcome=NULL` で即時 commit される → `latest_in_sample_metrics` の live 絞り (`IS NULL OR approval`) に混入 (実 DB 4 行)。[profitability-floor] T1-f で新規行は明示 outcome を付ける。既存 4 行の遡及修正 (UPDATE) は別裁定

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。

