---
id: candidates-converge-to-example-sma
status: 是正済
priority: 中
opened: 2026-09-12
closed: 2026-10-22
related: []
backfilled: true
source_section: 未完了
---
# [candidates-converge-to-example-sma]

**状態**: 是正済 / **優先**: 中

## 現象・原因・処置案 (tickets.md からの移行、原文)

[candidates-converge-to-example-sma] (中・設計観測、A4 12〜13 回目、2026-09-12) codex #74 / ornith #75 / muse #76 の提出候補は content_hash が異なるだけで **in_sample・holdout の metrics がビット一致** (pf 1.4981 / 194 trades、holdout pf 0.8047 / 53 trades) = 全 backend が example `sma_cross` の 10/30 USDJPY に収束。`noop_copy_of` (バイト一致) は素通り。approval #7〜#9 は同一戦略の 3 重申請で、いずれも holdout 負け。是正案: 質検査に「gate metrics が既存 approval / example と一致する候補は observation に落とす」(backtest_runs の metrics_json 突合、T2 の行があるので実装可能)、または backlog 選択時に同 kind の重複を抑止 **→ 是正 `7837fe9`+`d2c103e` (成績一致の質検査、母集団 = approval_requests の content_hash)。実機では条件未発生 (run15 は新規戦略)、配線の実証は次の収束時** 2026-09-12 16:30: approval #6〜#9 は全 reject 済 (backlog は observation に戻り探索継続)。質検査の配線実証は次の収束時

## 修正内容

- 2026-10-22: metrics 一致 (duplicate_metrics、7837fe9 系) に加え、spec 2026-10-19-improve-loop-quality-design §2.3 (B) を IQ-T2 / IQ-T2b (merge 2026-10-21〜22) で実装。logic_hash (リテラルを落とした AST 構造 hash) が example / 却下済み / 別名の配備済みと一致すれば duplicate_logic で拒否、仮説族 (indicator 構成 + 足 + pair) が配備済みと同じ別名は family_redirect で同名更新へ降格。母集団は tx 内で配備ガード (flock) の下に DB + live ファイルから確定。実ファイル #21/#22 (logic 一致)、#24/#25 (family 一致) で検証。残る穴 = indicator を使わない自前計算の戦略 (sma_cross 型) は族が空で AST hash も構造変更で避けられる (spec §11)。別途 improve-duplicate-self-computed-strategies に起票

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。
- 2026-10-22: 是正内容 — metrics 一致 (duplicate_metrics、7837fe9 系) に加え、spec 2026-10-19-improve-loop-quality-design §2.3 (B) を IQ-T2 / IQ-T2b (merge 2026-10-21〜22) で実装。logic_hash (リテラルを落とした AST 構造 hash) が example / 却下済み / 別名の配備済みと一致すれば duplicate_logic で拒否、仮説族 (indicator 構成 + 足 + pair) が配備済みと同じ別名は family_redirect で同名更新へ降格。母集団は tx 内で配備ガード (flock) の下に DB + live ファイルから確定。実ファイル #21/#22 (logic 一致)、#24/#25 (family 一致) で検証。残る穴 = indicator を使わない自前計算の戦略 (sma_cross 型) は族が空で AST hash も構造変更で避けられる (spec §11)。別途 improve-duplicate-self-computed-strategies に起票
