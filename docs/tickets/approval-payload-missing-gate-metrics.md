---
id: approval-payload-missing-gate-metrics
title: 承認 payload に gate の測定値が載らない問題は是正済
status: 是正済
priority: 未設定
opened: 2026-09-11
closed: 2026-10-02
related: []
backfilled: true
source_section: 未完了
---
# [approval-payload-missing-gate-metrics]

**状態**: 是正済 / **優先**: 未設定

## 現象・原因・処置案 (tickets.md からの移行、原文)

**[approval-payload-missing-gate-metrics] (重要、A4 10 回目 claude #69 観測 A、2026-09-11)** approval payload の `in_sample` / `holdout` / `eval_timeframe` が構造的に必ず None — `gate_metrics` に代入されるのは `baseline` (メタ情報のみ) だけで、`_run_strategy_gate` が測った in-sample (`StrategyGateVerdict.candidate_metrics`) と holdout (`gate_rows` の `scope='holdout_gate'` 行) は payload に載らない。approval #6 は holdout pf 0.904 / avg_r −0.037 (場外で負け) なのに承認材料は agent の自己申告 (in-sample pf 1.47) のみ。是正案: gate 直後に `gate_metrics["in_sample"]` / `["holdout"]` / `["meta"]` を埋め、`commands.py` の approval 表示に holdout を出す。T3 で親ゲート行が残るようになって初めて突合できた欠陥。**strategy gate は可測性ゲート (trades ≥ 30) で収益性を見ない (観測 F) ので、A を直さないと実質の防御が無い**。報告 `tmp/a4-run10-claude-20260911.md` §7 **→ 是正済 `dfbe40f` で in_sample/holdout/meta + `afx> approval <id>`、実機未確認** **→ A4 12 回目 #74 で実機 OK (payload の in_sample pf 1.498 / holdout pf 0.805 avg_r −0.070 が gate 行と一致、`afx> approval 7` 表示) → クローズ**

## 修正内容

- 2026-10-02: 仕分け (2026-10-02) で是正済と確認。in_sample/holdout/meta を payload に載せ afx> approval で表示、実機確認済み。 根拠: 是正 dfbe40f、A4 12 回目 #74 で実機確認、本文に『クローズ』。

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。
- 2026-10-02: 題名: 承認 payload に gate の測定値が載らない問題は是正済 — 仕分け (2026-10-02): 題名を付与
- 2026-10-02: 是正内容 — 仕分け (2026-10-02) で是正済と確認。in_sample/holdout/meta を payload に載せ afx> approval で表示、実機確認済み。 根拠: 是正 dfbe40f、A4 12 回目 #74 で実機確認、本文に『クローズ』。
