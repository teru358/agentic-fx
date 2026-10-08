---
id: backtest-base-interval
title: バックテスト基底足の可変化 (1m/5m/15m) は完了
status: 是正済
priority: 未設定
opened: 2026-09-05
closed: 2026-10-02
related: [analysis-boundary-grid, sl-gap-fill-ignores-gap, db-rebuild-helper, scheduler-bars-per-tick-memo, rpc-call-helper, backend-descriptor, gate-admission-helper, selftest-loop-no-cutoff, market-calendar-broker-mismatch, backtest-kill-switch-latch-truncates-in-sample, selftest-loop-no-cutoff]
backfilled: true
source_section: 未完了
---
# [backtest-base-interval]

**状態**: 是正済 / **優先**: 未設定

## 現象・原因・処置案 (tickets.md からの移行、原文)

**[backtest-base-interval] 設計承認 2026-09-05** (ユーザー): バックテスト基底足を `backtest.base_interval` (1m/5m/15m、既定 1m) で可変に。動機 = MT5 深さ (1m 3 ヶ月 / 5m 16 ヶ月 / 15m 4 年)、実測で 5m 基底の約定曖昧足 0.055%。設計 `tmp/design-base-interval/design.md`、1m 箇所全数 `explore-1m-sites.md`。設計レビュー codex 3 周 (v1→v2→v3+v4 追補、3 周連続 Critical なし、`codex-design-review-{,-2,-3}.md`)。**4 段階分割**: ① golden 基線 **完了 `b895069`** (段 0 変異 4 red、3213 passed、codex 1 周目レビュー中) → ② Dataset・DB・payload **完了 `3a461ad`** (変異 4 red、3232 passed)。ローカル 3 本レビュー完了 (`tmp/review-20260905-s2/verified-round1.md`: 本番欠陥 0、pin 9 件採用 → 段階 3 是正束へ) → ③ エンジン一般化 **完了 `8a05b44`** (段 0 変異 16、生存 2 pin、golden v1→v2 分類不能 0、3252 passed)。申し送り: 非収束 7 ケース未実装 / replay 収束の orders 比較 11 列 → 是正束 rfx **完了 `95c3fcb`** (非収束 6/7 + orders 全列 + pin 9、3267 passed。発見: payload 3 系統の eval_timeframe 写像未適用) → **codex 1 周目 (`tmp/review-20260905-s3/`): Critical 1 [analysis-boundary-grid] (analyze_for_agent の in_sample_until が 1m 格子固定 → 5m 基底で replay 境界とずれる) + Important 6 → 第 2 是正束 **完了 `73e0ab1`** (3280 passed、Critical 是正 + payload 4 系統統一 + pin 3)** → ローカル 3 本 **完了 (2026-09-06、`tmp/review-20260905-s3/verified-round1-local.md`): 本番欠陥 0、pin 14 件採用 → `1ac7f9f` + `a8de500` (codex 2 回、Critical 相当 4 件は変異 red 確認済、3303 passed + 既知 flake)。発見: SL gap 非収束テストの docstring が誤り (決済価格は両経路同値 147.795) → [sl-gap-fill-ignores-gap] 起票** → **/code-review high 完了 (09-06、sonnet、`tmp/review-20260906-cr/verified.md`。注意: 対象が origin 未 push 619 コミット全体だった)**: 8 件中 採用 4 → 是正 `6a57a05` (gate_pytest 永久停止 [本番 Important] / improve loop kind=signal 無 gate [本番 Important] / live_source 4 payload / backtest run --plugin 写像、3294 passed)、起票 4 ([db-rebuild-helper] / [scheduler-bars-per-tick-memo] / [rpc-call-helper] / [backend-descriptor])、[gate-admission-helper] 設計起票 → **codex 2 周目完了 (09-06、`tmp/review-20260906-cr/verified-round2.md`): Important 2 採用 → `c8cdb68` (gate_pytest 直接子 reap / C8b-1 SL 価格 pin、3294 passed) → ④ import/compare/運用: **設計 v4 `tmp/design-base-interval/stage4-design.md` (codex 3 周、Critical 0 × 3、`codex-stage4-review{,-2,-3}.md`)、前提実測済 (5m 深さ 16 ヶ月・native M5・1m 由来 OHLC 不一致 0/858) → **承認 2026-09-06 (CP1 5m 採用) → T1-T3 実装 `041e4fa`/`4fe9c47`、T4 `tmp/stage4/verify_native_vs_derived.py` → 1 周目: codex Important 2 是正 `561745c` + ローカル 3 本 pin 9 `a1cdb49` (`tmp/review-20260906-s4/verified-round1-local.md`、本番欠陥 0、3363 passed) → 2 周目完了: /code-review high 3 件是正 `4de418b` + codex Minor `9d01d7c` + ローカル pin 4 `1b54eac` (`tmp/review-20260906-cr2/verified.md`, `tmp/stage4/verified-round2-codex.md`, `tmp/review-20260906-s4r2/verified-round2-local.md`、本番欠陥 0、3367 passed、3 周目不要) → CP2/CP3a 承認 → **CP3d import 完了 (5m 99,897 行、conflict 0、`data/agentic.db.bak-20260906T080819Z`) → CP4 coverage → CP5 PASS (価格 100% / coverage 99.978%、M から NY 17 時台除外の裁定、`tmp/stage4/cp3-cp5-report.md`) → CP6 完了 (settings: base_interval=5m / holdout_months=3、`tmp/settings.yaml.bak-20260906`) → CP7 A4 5 回目 = mission #60 self-test ループ timeout で run_backtest 未到達 ([selftest-loop-no-cutoff] 再現、`tmp/a4-run5-20260906.md`) → **CP8 は gate 経路直接実行で達成**: 1 回目で本番欠陥 [market-calendar-broker-mismatch] 発見 → 是正 `6cd4934` → 2 回目完走 (backtest_runs #3 base_interval=5m、13 ヶ月 52 秒)。是正束 1 周目完了 (codex 重要 1 → `74a23ec` 祝日を取引日ラベルに / ローカル 3 本 Y 0、`tmp/stage4/verified-calendar-round1.md`、3371 passed)。**段階 4 完了 (2026-09-06)** — 運用状態: settings base_interval=5m / holdout 3、5m 99,897 行、gate 経路 5m 完走 (backtest_runs #3)。残課題: [backtest-kill-switch-latch-truncates-in-sample] (設計判断待ち)、[selftest-loop-no-cutoff] (改善ループ、2/2 全損)。事故: codex 変異実測が実 DB に migration を適用 (データ損失なし、`data/agentic.db.bak-trade-intents-observability` は migration の退避)****。各段階 段 0 + 1 周目、③のみ /code-review 追加

## 修正内容

- 2026-10-02: 仕分け (2026-10-02) で是正済と確認。base_interval 可変化を 4 段階で実装し 5m 運用中、残課題は別チケット。 根拠: 段階 1〜4 完了 (b895069/3a461ad/8a05b44/95c3fcb/73e0ab1、段階 4 は 041e4fa〜6cd4934・74a23ec)。本文に『段階 4 完了 2026-09-06』と明記。

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。
- 2026-10-02: 題名: バックテスト基底足の可変化 (1m/5m/15m) は完了 — 仕分け (2026-10-02): 題名を付与
- 2026-10-02: 是正内容 — 仕分け (2026-10-02) で是正済と確認。base_interval 可変化を 4 段階で実装し 5m 運用中、残課題は別チケット。 根拠: 段階 1〜4 完了 (b895069/3a461ad/8a05b44/95c3fcb/73e0ab1、段階 4 は 041e4fa〜6cd4934・74a23ec)。本文に『段階 4 完了 2026-09-06』と明記。
