---
id: candidates-converge-to-example-sma
status: 設計待ち
priority: 中
opened: 2026-09-12
closed: null
related: []
backfilled: true
source_section: 未完了
---
# [candidates-converge-to-example-sma]

**状態**: 設計待ち / **優先**: 中

## 現象・原因・処置案 (tickets.md からの移行、原文)

[candidates-converge-to-example-sma] (中・設計観測、A4 12〜13 回目、2026-09-12) codex #74 / ornith #75 / muse #76 の提出候補は content_hash が異なるだけで **in_sample・holdout の metrics がビット一致** (pf 1.4981 / 194 trades、holdout pf 0.8047 / 53 trades) = 全 backend が example `sma_cross` の 10/30 USDJPY に収束。`noop_copy_of` (バイト一致) は素通り。approval #7〜#9 は同一戦略の 3 重申請で、いずれも holdout 負け。是正案: 質検査に「gate metrics が既存 approval / example と一致する候補は observation に落とす」(backtest_runs の metrics_json 突合、T2 の行があるので実装可能)、または backlog 選択時に同 kind の重複を抑止 **→ 是正 `7837fe9`+`d2c103e` (成績一致の質検査、母集団 = approval_requests の content_hash)。実機では条件未発生 (run15 は新規戦略)、配線の実証は次の収束時** 2026-09-12 16:30: approval #6〜#9 は全 reject 済 (backlog は observation に戻り探索継続)。質検査の配線実証は次の収束時

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。

