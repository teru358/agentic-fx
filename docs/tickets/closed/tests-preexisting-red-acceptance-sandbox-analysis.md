---
id: tests-preexisting-red-acceptance-sandbox-analysis
title: 素の main でも落ちる test が 3 群 5 件ある
status: 是正済
priority: 中
kind: 保守
opened: 2026-11-10
closed: 2026-11-11
related: []
---
# [tests-preexisting-red-acceptance-sandbox-analysis] 素の main でも落ちる test が 3 群 5 件ある

**状態**: 是正済 / **優先**: 中 / **種別**: 保守

## 現象

基点 59476f1b の tests/ 全体で次の 5 件が落ちる (2026-11-09 判定)。(1) tests/acceptance/test_first_run_acceptance_doc.py の 3 件: docs/operations/first-run-acceptance.md がウィザードの「[段 3 参照銘柄]」の見出し・選択肢「参照銘柄」・案内文 2 つを引用していない。段 3 は 79599f1c (参照系列 T4) で足され、手順書が追随していない。(2) tests/plugin/test_plugin_worker_protocol.py::test_parent_with_a_real_inherited_filter_never_calls_popen: 期待 inherited_seccomp_filter / popen_calls 0 に対し runtime_fingerprint_selftest_failed / popen_calls 1。runtime_admission (sandbox.py:435-) が継承 filter の検査より前に fingerprint を判定し、手元の fingerprint が登録集合の外だと代表自己試験 (Popen) に入るため。test が登録集合に依存し、環境で結果が変わる。(3) tests/test_mission_worker.py::test_every_improve_rpc_crosses_child_frame_and_parent_wrapper: 履歴分析の request の window=90 が窓幅の enum (backtest/analysis.py:42 の WINDOWS = 20, 60, 120) に無く invalid arguments になる。test の値が古い。

## 原因

## 処置案・裁定

## 修正内容

- 2026-11-11: 受入手順書に段 3 (参照銘柄) の手順を足した。plugin worker protocol の test は admission を許可済みに固定し (autouse fixture と継承 filter の子 script)、手元の fingerprint が登録集合の外でも結果が変わらないようにした (2026-11-11 に two_stage_startup も同じ原因で落ちていた)。mission_worker の test は履歴分析の request を現行 schema (rolling_corr_summary、window 60) に直した。

## 経緯

- 2026-11-10: 起票。
- 2026-11-11: 是正内容 — 受入手順書に段 3 (参照銘柄) の手順を足した。plugin worker protocol の test は admission を許可済みに固定し (autouse fixture と継承 filter の子 script)、手元の fingerprint が登録集合の外でも結果が変わらないようにした (2026-11-11 に two_stage_startup も同じ原因で落ちていた)。mission_worker の test は履歴分析の request を現行 schema (rolling_corr_summary、window 60) に直した。
