---
id: legacy-submit-corridor-bypasses-gate
status: 設計待ち
priority: 中
opened: 2026-09-12
closed: null
related: [profitability-floor]
backfilled: true
source_section: 未完了
---
# [legacy-submit-corridor-bypasses-gate]

**状態**: 設計待ち / **優先**: 中

## 現象・原因・処置案 (tickets.md からの移行、原文)

[legacy-submit-corridor-bypasses-gate] (中、2026-09-12 opus 調査) `afx plugin submit <name>` (live plugins/ の既存 plugin) は `approval.submit_plugin` → `_validate_strategy` の独自実装で、共有ゲート `evaluate_strategy_adoption_gate` を通らず holdout も回さない (設計書「全経路に同じ規則」と乖離、[D-5] の旧回廊と同根)。[profitability-floor] では in_sample 段のみ課す。統合 (共有ゲートへ一本化) か廃止かは別裁定

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。

