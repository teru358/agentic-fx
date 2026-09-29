---
id: backtest-rpc-timeout-15s
status: 実装中
priority: 未設定
opened: 2026-09-03
closed: null
related: [gate-failed-rows-not-persisted]
backfilled: true
source_section: 未レビュー束
---
# [backtest-rpc-timeout-15s]

**状態**: 実装中 / **優先**: 未設定

## 現象・原因・処置案 (tickets.md からの移行、原文)

[backtest-rpc-timeout-15s]/[gate-failed-rows-not-persisted] **是正済 0a453af 2026-09-03、未レビュー** — WorkerRunner に `rpc_timeout_sec_by_kind` を追加し improve/verify_backend で子と同じ map を親にも配線 (run_backtest/analyze_corr = backtest_rpc_timeout_sec)。gate_failed でも `_persist_gate_rows` で backtest_runs に in-sample 行を残す。実装 codex、変異 1 件 red、3182 passed。旧内容: 親側 `worker_runner.py:299` が種別を問わず 15 秒 → run_backtest 必ず timeout、モデルが「backtest 不可」誤認 (m50)

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。

