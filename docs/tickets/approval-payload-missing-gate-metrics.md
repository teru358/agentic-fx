---
id: approval-payload-missing-gate-metrics
status: 裁定待ち
priority: 未設定
opened: 2026-09-11
closed: null
related: []
backfilled: true
source_section: 未完了
---
# [approval-payload-missing-gate-metrics]

**状態**: 裁定待ち / **優先**: 未設定

## 現象・原因・処置案 (tickets.md からの移行、原文)

**[approval-payload-missing-gate-metrics] (重要、A4 10 回目 claude #69 観測 A、2026-09-11)** approval payload の `in_sample` / `holdout` / `eval_timeframe` が構造的に必ず None — `gate_metrics` に代入されるのは `baseline` (メタ情報のみ) だけで、`_run_strategy_gate` が測った in-sample (`StrategyGateVerdict.candidate_metrics`) と holdout (`gate_rows` の `scope='holdout_gate'` 行) は payload に載らない。approval #6 は holdout pf 0.904 / avg_r −0.037 (場外で負け) なのに承認材料は agent の自己申告 (in-sample pf 1.47) のみ。是正案: gate 直後に `gate_metrics["in_sample"]` / `["holdout"]` / `["meta"]` を埋め、`commands.py` の approval 表示に holdout を出す。T3 で親ゲート行が残るようになって初めて突合できた欠陥。**strategy gate は可測性ゲート (trades ≥ 30) で収益性を見ない (観測 F) ので、A を直さないと実質の防御が無い**。報告 `tmp/a4-run10-claude-20260911.md` §7 **→ 是正済 `dfbe40f` で in_sample/holdout/meta + `afx> approval <id>`、実機未確認** **→ A4 12 回目 #74 で実機 OK (payload の in_sample pf 1.498 / holdout pf 0.805 avg_r −0.070 が gate 行と一致、`afx> approval 7` 表示) → クローズ**

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。

