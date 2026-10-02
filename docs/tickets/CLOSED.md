# 完了チケット一覧

自動生成: `ticket.py index` (tickets skill)。手で編集しない。

## 2026-10 (1)

| id | 状態 | 優先 | 完了 | 概要 |
|---|---|---|---|---|
| [outage-stalled-on-broker-daily-rollover-gap](outage-stalled-on-broker-daily-rollover-gap.md) | 是正済 | 高 | 2026-10-02 | 静かな市場で 1m 足が欠けると degraded になる (restricted 状態を追加) |

## 2026-09 (34)

| id | 状態 | 優先 | 完了 | 概要 |
|---|---|---|---|---|
| [ticket-cli-and-index-split](ticket-cli-and-index-split.md) | 是正済 | 中 | 2026-09-30 | チケット操作を plugin tickets に移し、完了分を CLOSED.md へ分離 |
| [worker-death-cause-observed-by-parent](worker-death-cause-observed-by-parent.md) | 是正済 | 高 | 2026-09-30 | plugin worker の死因を親プロセスで固定して読めるようにする |
| [a23c-lite-auto-resume-when-flat](a23c-lite-auto-resume-when-flat.md) | 是正済 | 高 | 2026-09-29 | 建玉・指値ゼロの episode は degraded から自動で ready に戻す |
| [expire-stale-activity-in-store](expire-stale-activity-in-store.md) | 是正済 | 未設定 | 2026-09-28 | / [friday-cutoff-validation] / [cli-bless-runtime-error-tra… |
| [outage-stalled-ignores-closed-hours](outage-stalled-ignores-closed-hours.md) | 是正済 | 未設定 | 2026-09-28 | / [settings-example-deprecated-keys] **是正済 main `ed2de56` (… |
| [signal-producer-reads-import-table-under-mt5-primary](signal-producer-reads-import-table-under-mt5-primary.md) | 是正済 | 高 | 2026-09-28 | `_run_signal_maintenance` が producer にライブ保存名 (mt5 → mt5-liv… |
| [yfinance-source-calendar](yfinance-source-calendar.md) | 是正済 | 中 | 2026-09-28 | 上記の第 2 段 **[yfinance-source-calendar] 起票 2026-09-28 (中、ユーザー… |
| [dispatcher-join-budget-pin](dispatcher-join-budget-pin.md) | 是正済 | 未設定 | 2026-09-27 | 是正済 `e753324` (2026-09-27、小物 2 件の束、pin `aa08310`) — worker_… |
| [graceful-stop-hang](graceful-stop-hang.md) | 是正済 | 未設定 | 2026-09-27 | 停止 461 秒 (9/27 08:39、b0de51f 以前の supervisor join) は B-2 段 a… |
| [signal-producer-closed-market-warning](signal-producer-closed-market-warning.md) | 是正済 | 未設定 | 2026-09-27 | 是正済 `e753324` (2026-09-27) — 閉場中は signal producer の評価を止め (c… |
| [writable-provider-quote-chain-ignores-primary](writable-provider-quote-chain-ignores-primary.md) | 是正済 | 中 | 2026-09-24 | A2-1b で primary-only にしたのは readonly provider だけで、書き込み可能 pro… |
| [paper-fill-misses-intrabar-touch-on-forming-bar](paper-fill-misses-intrabar-touch-on-forming-bar.md) | 是正済 | 中 | 2026-09-21 | ライブの paper 約定判定は `latest_1m_bar()` の末尾 = 形成中の 1m 足を読み、同じ `b… |
| [retry-switched-approves-without-deploy](retry-switched-approves-without-deploy.md) | 是正済 | 未設定 | 2026-09-19 | / [cli-bless-unresolved-journal] / [switch-not-required-ski… |
| [bootstrap-probe-tests-mkdir-real-logs-dir](bootstrap-probe-tests-mkdir-real-logs-dir.md) | 是正済 | 中 | 2026-09-12 | 旧: (中) `tests/test_mission_worker*.py` の `_run_bootstrap_pr… |
| [codex-subscription-expiry-check-never-fires](codex-subscription-expiry-check-never-fires.md) | 是正済 | 低 | 2026-09-12 | 旧: (低、同 観測 D) `_check_codex_subscription_expiry` は実 auth.js… |
| [plugin-locks-accumulate](plugin-locks-accumulate.md) | 是正済 | 低 | 2026-09-12 | 旧: (低) `plugins/.locks/<name>.lock` が名前ごとに蓄積 (reject 止まりの `… |
| [profitability-floor](profitability-floor.md) | 是正済 | 高 | 2026-09-12 | 旧: (高、2026-09-12 ユーザー承認、順序 = test-hygiene の後・backtest-dedup… |
| [reject-reason-leak](reject-reason-leak.md) | 是正済 | 高 | 2026-09-12 | 旧: (高、2026-09-12 実機 `tmp/approval-20260912b.md`、ユーザー承認済「すべて… |
| [codex-backend-no-proc-landlock](codex-backend-no-proc-landlock.md) | 是正済 | 未設定 | 2026-09-11 | `mission_worker.py:161` の Landlock read_only allowlist に `/… |
| [gate-failed-ledger-discarded](gate-failed-ledger-discarded.md) | 是正済 | 未設定 | 2026-09-10 | L0 = 台帳の記録主体を dispatcher の期限内受理後 (ImproveLoop の `on_rpc_acc… |
| [analyze-corr-rpc-double-unwrap](analyze-corr-rpc-double-unwrap.md) | 是正済 | 未設定 | 2026-09-09 | `f432dbf` (子が `{"request": …}` を送る + 全 RPC 種別の契約テスト) → code… |
| [mission-abort-on-tool-budget](mission-abort-on-tool-budget.md) | 是正済 | 高 | 2026-09-09 | ユーザー承認 2026-09-08**: v4 実装 + 受入 probe 許可、Tier G 分離、`remaini… |
| [system-note-type-by-cause](system-note-type-by-cause.md) | 是正済 | 未設定 | 2026-09-09 | `e048c4f`: backtest 0 → 型 A / abort → 型 B / timeout・max_tur… |
| [tier-b-release-requires-evaluable-backtest](tier-b-release-requires-evaluable-backtest.md) | 是正済 | 未設定 | 2026-09-09 | `e048c4f` → codex Important (evaluable は gate 用 trades ≥ 30… |
| [tool-exception-bypasses-refusal-streak](tool-exception-bypasses-refusal-streak.md) | 是正済 | 未設定 | 2026-09-09 | `f432dbf`/`7e0fc16` (on_result フック、(tool, 失敗種別) の streak、E2… |
| [backtest-kill-switch-latch-truncates-in-sample](backtest-kill-switch-latch-truncates-in-sample.md) | 是正済 | 未設定 | 2026-09-06 | 実装 `ec46902`** (段 0 K1〜K19 全 red、golden-v3、3394 passed) → C… |
| [market-calendar-broker-mismatch](market-calendar-broker-mismatch.md) | 是正済 | 未設定 | 2026-09-06 | OANDA Japan MT5 は UTC+3 固定で週末境界 21:00 UTC (DST 非追従)。`market… |
| [pytest-basetemp-garbage](pytest-basetemp-garbage.md) | 是正済 | 未設定 | 2026-09-05 | テストが残す 0500/0400 ツリーで pytest の basetemp 掃除が失敗し `/tmp/pytest… |
| [activity-success-silent](activity-success-silent.md) | 是正済 | 未設定 | 2026-09-01 | /[observation-activity-silent] 裁定 2026-09-01: IMPROVE 成功系 3… |
| [gate-accepts-noop-artifact](gate-accepts-noop-artifact.md) | 是正済 | 未設定 | 2026-09-01 | /[research-facts-are-selectable-work] 是正済 `cd3ba1c` (2026-0… |
| [holder-pid-format](holder-pid-format.md) | 是正済 | 未設定 | 2026-09-01 | 是正済 (2026-09-01) — holder.pid を bare PID に統一 |
| [legacy-oc-context-limit-hardcoded](legacy-oc-context-limit-hardcoded.md) | 是正済 | 未設定 | 2026-09-01 | [OC-context-limit-hardcoded] 裁定 2026-09-01: `runner.opencod… |
| [news-seed-on-start](news-seed-on-start.md) | 是正済 | 未設定 | 2026-09-01 | /[backtest-tool-offered-to-indicator] 是正済 `b76f1c7` (2026-0… |
| [review-material-added-names](review-material-added-names.md) | 是正済 | 未設定 | 2026-09-01 | 是正済 (2026-09-01) — `review_material.py` の `_added_names` を … |

## 2026-08 (9)

| id | 状態 | 優先 | 完了 | 概要 |
|---|---|---|---|---|
| [examples-unreachable](examples-unreachable.md) | 是正済 | 未設定 | 2026-08-31 | 是正済 `ed0f0f1` (2026-08-31) — list_examples / read_example_p… |
| [legacy-oc-run-diag](legacy-oc-run-diag.md) | 是正済 | 未設定 | 2026-08-31 | [OC-run-diag]/[run_plugin_tests-EACCES] 是正済 `a59c0be` → `2a… |
| [loader-none-crash](loader-none-crash.md) | 是正済 | 未設定 | 2026-08-31 | 是正済 `14ca38a`/`b513d91`/`0cd4efb` (2026-08-31) — `commit()`… |
| [parser-first-object-decoy](parser-first-object-decoy.md) | 是正済 | 未設定 | 2026-08-31 | 是正済 `c17e46d` (2026-08-31) — parse_json_output に prefer_key… |
| [report-tx-crash](report-tx-crash.md) | 是正済 | 未設定 | 2026-08-31 | 是正済 `71e342d` (2026-08-31) — report_state='prepared' UPDATE… |
| [test-fixtures-from-real-transcripts](test-fixtures-from-real-transcripts.md) | 是正済 | 未設定 | 2026-08-31 | [OC-resume-discard] 是正済 `f0e9f87`/`5705f55`/`5dd7fd0` (2026… |
| [legacy-2026-08-30](legacy-2026-08-30.md) | 是正済 | 未設定 | 2026-08-30 | [解決 2026-08-30] gate_pytest テストが実 data/agentic.db を上書き→unli… |
| [legacy-oc-e2e](legacy-oc-e2e.md) | 是正済 | 未設定 | 2026-08-30 | [OC-E2E] 是正済 `da5bb04` (2026-08-30) — 最終 JSON 実例 + 「JSON 1 … |
| [legacy-oc-websearch](legacy-oc-websearch.md) | 是正済 | 未設定 | 2026-08-30 | [OC-websearch] 是正済 `dda60f9` (2026-08-30) — ddgs 9.15.0 の k… |

