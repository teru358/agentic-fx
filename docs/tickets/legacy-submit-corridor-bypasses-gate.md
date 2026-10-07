---
id: legacy-submit-corridor-bypasses-gate
status: 是正済
priority: 中
opened: 2026-09-12
closed: 2026-10-05
related: [profitability-floor]
backfilled: true
source_section: 未完了
---
# [legacy-submit-corridor-bypasses-gate]

**状態**: 是正済 / **優先**: 中

## 現象・原因・処置案 (tickets.md からの移行、原文)

[legacy-submit-corridor-bypasses-gate] (中、2026-09-12 opus 調査) `afx plugin submit <name>` (live plugins/ の既存 plugin) は `approval.submit_plugin` → `_validate_strategy` の独自実装で、共有ゲート `evaluate_strategy_adoption_gate` を通らず holdout も回さない (設計書「全経路に同じ規則」と乖離、[D-5] の旧回廊と同根)。[profitability-floor] では in_sample 段のみ課す。統合 (共有ゲートへ一本化) か廃止かは別裁定

## 修正内容

- 2026-10-05: afx plugin submit の旧経路を全 kind で plugin 探索・gate の前に拒否 (--from _human だけが switch.submit_candidate の共有 gate を通る)。tests/plugin/test_legacy_submit_rejected.py で pending 行が増えないことを実 SQLite で pin

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。
- 2026-10-04: 状態: 実装待ち — 2026-10-04: 操作 API spec v1.3 (docs/superpowers/specs/2026-10-04-ops-api-design.md) の T1 の範囲に含めてユーザー承認。実装は plugin worker 隔離の main 投入と脱出の実測の後。
- 2026-10-05: 是正内容 — afx plugin submit の旧経路を全 kind で plugin 探索・gate の前に拒否 (--from _human だけが switch.submit_candidate の共有 gate を通る)。tests/plugin/test_legacy_submit_rejected.py で pending 行が増えないことを実 SQLite で pin
