---
id: backtest-dedup-cache
status: 設計待ち
priority: 中
opened: 2026-09-08
closed: null
related: [mission-abort-on-tool-budget, gate-failed-ledger-discarded]
backfilled: true
source_section: 未完了
---
# [backtest-dedup-cache]

**状態**: 設計待ち / **優先**: 中

## 現象・原因・処置案 (tickets.md からの移行、原文)

[backtest-dedup-cache] (中、[mission-abort-on-tool-budget] から分離 2026-09-08) 同一 (候補, config) の backtest 再実行で予算を消費しない (run6: 6 枠中 3 枠がビット同一)。確定要件は design v4 §7 (親 handler / 成功のみ / キーに履歴内容の同一性 — rowid 不可、import revision の採番が要る / 両 ledger skip / trial_count = 実計算数)。[gate-failed-ledger-discarded] と同時設計

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。

