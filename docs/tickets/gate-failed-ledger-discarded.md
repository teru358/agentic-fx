---
id: gate-failed-ledger-discarded
status: 是正済
priority: 未設定
opened: 2026-09-09
closed: 2026-09-10
related: [gate-failed-rows-not-persisted]
backfilled: true
source_section: 未完了
---
# [gate-failed-ledger-discarded]

**状態**: 是正済 / **優先**: 未設定

## 現象・原因・処置案 (tickets.md からの移行、原文)

**[gate-failed-ledger-discarded] 設計中 (2026-09-09、`tmp/design-ledger-preserve/design.md` v4、codex 4 周目走行中)**: L0 = 台帳の記録主体を dispatcher の期限内受理後 (ImproveLoop の `on_rpc_accepted` callback、`RpcOutcome(public, private)`、accept 予約と freeze の直列化) / L1 = 全終端で SAVEPOINT 永続化 (`accepted_entries` 共有、補償 2 種) / L2 = backtest 成功時に 3 ファイルを `plugins/_archive/<mission>/<artifact_hash>/` へ二段階 publish / migration `backtest_runs.mission_outcome` + `candidate_archives` (消費者 `latest_in_sample_metrics` を outcome で分離) / L3 note・INDEX / L4 GC。Critical は v1〜v4 で毎周 L0/L2 層 (終端コピー → payload 欠落 → freeze 競合 → 予約の非原子性/打ち切れない callback) → **v5 で publish を commit 相 (slot スレッド) に移し、5 周目 (2026-09-10) は新規 Critical 0 → v6 = 実装仕様、L2 は分離しない**。task = T1 (L0 + tmp snapshot) ∥ T2 (migration + store) → T3 (全終端 SAVEPOINT + commit 相 publish + 補償/PERSIST_FAILED) → T4 (note/INDEX/GC)。**ユーザー承認 2026-09-10** (v6 で実装、モデル切替 run は実装が落ち着いてから)。**実装進捗 2026-09-10**: T1 `696ea27` ∥ T2 `29f260e` → マージ `e063a5d`、段 0 16 変異 (`tmp/review-20260910-ledger/stage0.md`)、codex 1 周目 Important 1 (callback 例外で受理境界 fail-open) → `3dbc704`、ローカル 3 本 pin 15 → `dcd48c6` (`verified-local.md`)。T3 `df6220f` (codex + sonnet pin 11 + 指揮者是正 1)、T4 `1a9d4f1` (sonnet、段 0 11 変異)。**T1〜T4 全部 main にマージ済 (3628 passed)**。ローカル 1 周目 T3 `18b1701` / T4 `bda3bff` 完了 (本番欠陥 0)。codex 1 周目 T3+T4 `c0ddb15` (Important 2: INDEX 重複 / GC 未完了 tx)。1 周目全完了。**2 周目完了 (2026-09-11)**: /code-review high 10 件 → `10f4d8c` (must-fix 3: 親ゲート行 outcome 欠落 / result_queue 自由変数 / analyze_corr 台帳痩せ)、codex Important 1 → `c566598`、ローカル pin 5 → `60f6ab7`。3657 passed。3 周目 (sonnet、承認済) Minor 1 → `3151377`。**レビュー 3 周完了 (2026-09-11)、3658 passed。次 = モデル切替 A4 10 回目 (実機で永続化確認)** (旧: 中、A4 4 回目 2026-09-04) gate 不合格時に `_finalize_gate_failed` (`improve_loop.py:1908-`) が `ctx.ledger.mark_discarded()` し `_persist_gate_rows` のみ → モデルが run_backtest で見た結果 (RPC 台帳) は DB に残らない。`_persist_ledger_rows` は `_finalize_success` からのみ。設計書 phase2-10 :318 は「commit 相で永続化」で不合格除外の記述なし。対照実験 (10/30 pf=1.01) の一次記録が note 文面にしか無い。[gate-failed-rows-not-persisted] の残り半分 **→ 実機完結 2026-09-12: approval 経路 (#69/#74/#75/#76)、report 経路 (#72)、failed 経路 (#77、F1〜F7) で永続化・INDEX・note best・遮断を全部確認 → クローズ。spec は docs/superpowers/specs/2026-09-10-ledger-preserve-design.md**

## 修正内容

原文に commit・レビュー結果・実機受入が含まれる場合はそれが正。当時の詳細記録は `tmp/review-*/`・`tmp/design-*/` (gitignore) と git log。

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。

