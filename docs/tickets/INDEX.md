# チケット一覧 (自動生成: `python3 scripts/tickets_index.py`。手で編集しない)

件数: 173。状態は `docs/tickets/<id>.md` の frontmatter が正。出来事の記録は `archive-*.md`。

## 実装中 (7)

| id | 優先 | 起票 | 完了 | 概要 |
|---|---|---|---|---|
| [strategy-gate-data-error-crashes-commit](strategy-gate-data-error-crashes-commit.md) | 未設定 | 2026-09-02 |  | [strategy-gate-data-error-crashes-commit]/[run-backtest-error-opaque]/[staging-snapshot-src-leak]/[staging-she |
| [backtest-rpc-timeout-15s](backtest-rpc-timeout-15s.md) | 未設定 | 2026-09-03 |  | [backtest-rpc-timeout-15s]/[gate-failed-rows-not-persisted] **是正済 0a453af 2026-09-03、未レビュー** — WorkerRunner に  |
| [eval-source-consolidation](eval-source-consolidation.md) | 未設定 | 2026-09-03 |  | [eval-source-consolidation] **完了 442f5f6 2026-09-03、未レビュー** — `_EVAL_SOURCE`×3 + `ANALYSIS_SOURCE` → `settings |
| [rpc-result-not-json-serializable](rpc-result-not-json-serializable.md) | 未設定 | 2026-09-03 |  | [rpc-result-not-json-serializable]/[opencode-mcp-timeout-60s] **是正済 81a2158 2026-09-03、未レビュー** — run_backtest  |
| [unreviewed-bundle-2026-09-05](unreviewed-bundle-2026-09-05.md) | 未設定 | 2026-09-03 |  | **束 = bc75ad6 / 442f5f6 / 0a453af / 81a2158 / f978eb6 (段 0 pin)**。段 0 完了 2026-09-03: 変異 10 件、生存 2 件 (M1 commit |
| [ledger-never-populated-in-production](ledger-never-populated-in-production.md) | 未設定 | 2026-09-04 |  | [ledger-never-populated-in-production]/[no-history-typed-exception] **是正済 f67820d 2026-09-04 (2 周目 /code-revie |
| [trade-consume-before-record](trade-consume-before-record.md) | 未設定 | 2026-09-05 |  | [trade-consume-before-record]/[notifier-under-core-lock-in-tick] 是正済 44f6244 → 段 0 f9c247a → 1 周目是正 `1bffa97`  |

## 実装待ち (8)

| id | 優先 | 起票 | 完了 | 概要 |
|---|---|---|---|---|
| [legacy-2026-08-23-c-0](legacy-2026-08-23-c-0.md) | 未設定 | 2026-08-23 |  | [2026-08-23 束 C 段 0] `tests/datafeed/test_fetchers.py` 等 11 件が `('ex.com', 443)` へ実 TCP 接続を試みる (socket guard で |
| [flake-trade-claude-real-process](flake-trade-claude-real-process.md) | 未設定 | 2026-09-20 |  | [flake-trade-claude-real-process] 起票 2026-09-20 ([switch-ops-hardening] 1 周目是正のフルスイート): `tests/runners/test_wo |
| [switch-ops-r2b-low-unpinned](switch-ops-r2b-low-unpinned.md) | 未設定 | 2026-09-20 |  | [switch-ops-r2b-low-unpinned] 起票 2026-09-20 ([switch-ops-hardening] 2 周目やり直しローカル LLM、束の diff 外の既存コード、変異は未実測 =  |
| [flake-rpc-timeout-by-kind](flake-rpc-timeout-by-kind.md) | 未設定 |  |  | [flake-rpc-timeout-by-kind] `tests/runners/test_worker_runner.py:1129` 種別 timeout テストが sleep 0.5 vs 0.2/2.0 秒の |
| [flake-shell-interrupt-timing](flake-shell-interrupt-timing.md) | 未設定 |  |  | [起票] `tests/test_shell_interrupt.py` は別 pytest 並走でタイミング依存 fail (単独 11 passed) — 負荷耐性のある待ち方に直す (プラン 10 外)。同ファイル |
| [flake](flake.md) | 未設定 |  |  | [flake] test_service_app.py::test_interactive_mode_actually_stops_via_stop_event_end_to_end が全スイート負荷下でのみ fail  |
| [flaky-tool-schemas](flaky-tool-schemas.md) | 未設定 |  |  | [flaky-tool-schemas] `tests/tools/test_tool_impls.py::test_all_tools_have_schemas` がフル suite 下で ERROR (setup 段 |
| [test-isolation](test-isolation.md) | 未設定 |  |  | [test-isolation] tests/loops/conftest.py の loop_min/loop_full が特定 4 ファイル同時の部分実行で不可視化し 18 本消える (全スイートでは発生せず)。ve |

## 裁定待ち (9)

| id | 優先 | 起票 | 完了 | 概要 |
|---|---|---|---|---|
| [legacy-2026-08-23-e-11-m3-m7](legacy-2026-08-23-e-11-m3-m7.md) | 未設定 | 2026-08-23 |  | [2026-08-23 E-11 検収 m3/m7] `apply_decision` のキー方向 (設計 §5.1-2 との差) / crash point A の縮退 — 設計裁定が要る。束 E レビューで扱う |
| [backtest-base-interval](backtest-base-interval.md) | 未設定 | 2026-09-05 |  | **[backtest-base-interval] 設計承認 2026-09-05** (ユーザー): バックテスト基底足を `backtest.base_interval` (1m/5m/15m、既定 1m) で可変 |
| [sl-gap-fill-ignores-gap](sl-gap-fill-ignores-gap.md) | 未設定 | 2026-09-06 |  | **[sl-gap-fill-ignores-gap]** (2026-09-06、ローカル 1 周目 C8b-1 の副産物、設計判断待ち): 訂正 (09-06 codex 2 周目): `paper_fills.ch |
| [approval-payload-missing-gate-metrics](approval-payload-missing-gate-metrics.md) | 未設定 | 2026-09-11 |  | **[approval-payload-missing-gate-metrics] (重要、A4 10 回目 claude #69 観測 A、2026-09-11)** approval payload の `in_sa |
| [mission-prompt-in-argv-readable-via-proc](mission-prompt-in-argv-readable-via-proc.md) | 未設定 | 2026-09-11 |  | **[mission-prompt-in-argv-readable-via-proc] (重要、設計判断待ち、codex 1 周目 backend-fix I1、2026-09-11)** claude/codex/o |
| [claude-builtin-tools-exposed](claude-builtin-tools-exposed.md) | 未設定 | 2026-09-12 |  | **[claude-builtin-tools-exposed] (重要、同 観測 C)** claude backend の子プロセスは `--allowedTools mcp__afx__*` でも組み込み 27 本 |
| [unprofitable-note-hygiene](unprofitable-note-hygiene.md) | 未設定 | 2026-09-13 |  | **[unprofitable-note-hygiene]** (2026-09-13 起票、小束、設計待ち): フロア不合格 (`unprofitable`) で終わった mission が起票した agent not |
| [gate-noop-followup](gate-noop-followup.md) | 未設定 |  |  | [gate-noop-followup] 現行 note 化されていない fact 行が残る間は選択され得る (裁定待ち)。次回 E2E で `kind` 必須化にモデルが追随するか (schema 不遵守 → outp |
| [improve-mission-zero-tool-calls-undetected](improve-mission-zero-tool-calls-undetected.md) | 未設定 |  |  | **[improve-mission-zero-tool-calls-undetected] (重要、同 観測 B)** registry の tool 呼び出し 0 件で終端した improve mission が「a |

## 設計待ち (108)

| id | 優先 | 起票 | 完了 | 概要 |
|---|---|---|---|---|
| [indicator-consumption-wiring](indicator-consumption-wiring.md) | 高 | 2026-09-12 |  | **[indicator-consumption-wiring] (高、ユーザー方針「指標層 → 戦略層」の本丸、2026-09-12)** plugin 契約 `evaluate(df, indicators, sig |
| [backtest-worker-cpu-budget-shrinks-with-timeframe](backtest-worker-cpu-budget-shrinks-with-timeframe.md) | 高 | 2026-09-20 |  | **[backtest-worker-cpu-budget-shrinks-with-timeframe] 起票 2026-09-20 (重要度: 高、案 A の実機 mission #87 で発覚、再現済 `tmp/p |
| [backtest-rpc-timeout-does-not-stop-parent-work](backtest-rpc-timeout-does-not-stop-parent-work.md) | 高 | 2026-09-21 |  | **[backtest-rpc-timeout-does-not-stop-parent-work]** (重要度: 高、2026-09-21 B1 設計レビュー r1 codex sol、未実測): `backtest |
| [outage-stalled-on-broker-daily-rollover-gap](outage-stalled-on-broker-daily-rollover-gap.md) | 高 | 2026-09-29 |  | **[outage-stalled-on-broker-daily-rollover-gap] 起票 2026-09-29 (重要度: 高、実機 9/28 21:07 UTC、C0 下書き = `tmp/design-r |
| [live-signal-eval-blocks-protection-tick](live-signal-eval-blocks-protection-tick.md) | 高 |  |  | **[live-signal-eval-blocks-protection-tick]** (重要度: 高、同上、未実測): signal maintenance は scheduler tick 内の同期 hook。例 |
| [backtest-dedup-cache](backtest-dedup-cache.md) | 中 | 2026-09-08 |  | [backtest-dedup-cache] (中、[mission-abort-on-tool-budget] から分離 2026-09-08) 同一 (候補, config) の backtest 再実行で予算を消費 |
| [baseline-replay-unimplemented](baseline-replay-unimplemented.md) | 中 | 2026-09-12 |  | [baseline-replay-unimplemented] (中、設計書との乖離、2026-09-12 opus 調査) 設計書 2026-08-16 §4.2-4「(pair, timeframe) ごとに現在 l |
| [candidates-converge-to-example-sma](candidates-converge-to-example-sma.md) | 中 | 2026-09-12 |  | [candidates-converge-to-example-sma] (中・設計観測、A4 12〜13 回目、2026-09-12) codex #74 / ornith #75 / muse #76 の提出候補は  |
| [human-corridor-gate-rows-null-outcome](human-corridor-gate-rows-null-outcome.md) | 中 | 2026-09-12 |  | [human-corridor-gate-rows-null-outcome] (中、2026-09-12 opus 調査、codex 設計 r1 I2 の副産物) 人間回廊 (`submit_candidate` /  |
| [improve-targeted-run](improve-targeted-run.md) | 中 | 2026-09-12 |  | [improve-targeted-run] (中、ユーザー要望 2026-09-12「戦略作成の準備として CLI から indicator を準備させる経路」) 現状は `improve add <text>` +  |
| [legacy-submit-corridor-bypasses-gate](legacy-submit-corridor-bypasses-gate.md) | 中 | 2026-09-12 |  | [legacy-submit-corridor-bypasses-gate] (中、2026-09-12 opus 調査) `afx plugin submit <name>` (live plugins/ の既存 pl |
| [policy-add-unwired-in-service](policy-add-unwired-in-service.md) | 中 | 2026-09-20 |  | **[policy-add-unwired-in-service] 起票 2026-09-20 (重要度: 中、案 A 準備の実機で発覚)**: `service.py:1054` の `Commands(...)` に |
| [secret-env-guard-false-positive](secret-env-guard-false-positive.md) | 中 | 2026-09-20 |  | **[secret-env-guard-false-positive] 起票 2026-09-20 (重要度: 中、案 A 準備の実機で発覚)**: 改善 backend を CLI 系 (claude / codex) |
| [db-healthcheck-continuous-session-freshness](db-healthcheck-continuous-session-freshness.md) | 中 | 2026-09-28 |  | **[db-healthcheck-continuous-session-freshness] 起票 2026-09-28 (中、`/code-review high` stalled r1)**: `service.p |
| [live-storage-source-mapping-scattered](live-storage-source-mapping-scattered.md) | 中 | 2026-09-28 |  | **[live-storage-source-mapping-scattered] 起票 2026-09-28 (中、整理)**: primary → ライブ保存名 (`mt5` → `mt5-live`) の写像が 8 |
| [signal-producer-catchup-requires-latest-tail](signal-producer-catchup-requires-latest-tail.md) | 中 | 2026-09-28 |  | **[signal-producer-catchup-requires-latest-tail] 起票 2026-09-28 (中、要再現、astra 指摘)**: producer は過去 bucket を順に評価 ( |
| [trade-cron-hybrid-mode](trade-cron-hybrid-mode.md) | 中 | 2026-09-28 |  | **[trade-cron-hybrid-mode] 起票 2026-09-28 (中〜高、ユーザー設計論点、C0 v0.1 = `tmp/design-cron-hybrid/C0.md`、astra 途中)**: 取 |
| [harness-failure-becomes-fact](harness-failure-becomes-fact.md) | 中 |  |  | [harness-failure-becomes-fact] (中) harness 由来の失敗 (timeout 等) をモデルが「環境制約 fact」として note 化し続ける (#24/#28/#29) → 次回 |
| [refused-errors-double-count](refused-errors-double-count.md) | 中 |  |  | [refused-errors-double-count] (中、run9 観測 C) 予算拒否は terminal streak と tool_error streak の両方に入り `refused=10 error |
| [reject-revert-without-live-classification](reject-revert-without-live-classification.md) | 低 | 2026-09-20 |  | **[reject-revert-without-live-classification] 起票 2026-09-20 (重要度: 低〜中、[switch-ops-hardening] 1 周目 codex terra/ |
| [improve-add-tokenizer-collapses-whitespace](improve-add-tokenizer-collapses-whitespace.md) | 低 | 2026-09-21 |  | [improve-add-tokenizer-collapses-whitespace] (低、2026-09-21 設計レビュー r2): シェルの tokenizer (`line.strip().split()`  |
| [trade-prompt-says-hourly](trade-prompt-says-hourly.md) | 低 | 2026-09-28 |  | **[trade-prompt-says-hourly] 起票 2026-09-28 (低)**: 取引判断 prompt の冒頭「1 時間毎に呼び出され」が固定文言のまま (判断足 15m では 15 分毎)。`dec |
| [outage-observe-closed-guard-unpinned](outage-observe-closed-guard-unpinned.md) | 低 | 2026-09-29 |  | **[outage-observe-closed-guard-unpinned] 起票 2026-09-29 (低、テスト強度)**: `OutageStateMachine.observe` の「閉場中は観測しない」e |
| [signal-producer-hourly-warning-before-confirmation](signal-producer-hourly-warning-before-confirmation.md) | 低 | 2026-09-29 |  | **[signal-producer-hourly-warning-before-confirmation] 起票 2026-09-29 (低、ノイズ)**: producer が毎時 xx:00:07 の tick で |
| [approval-no-history-passthrough](approval-no-history-passthrough.md) | 低 |  |  | [approval-no-history-passthrough] (低) `plugin/approval.py` の `_validate_kind`/`_validate_strategy` (holdout.ru |
| [backtest-scheduler-log-leak](backtest-scheduler-log-leak.md) | 低 |  |  | [backtest-scheduler-log-leak] (低) backtest 内部 Scheduler の `maintain_reservations failed: no completed 1m bar f |
| [credentials-file-copied-unused](credentials-file-copied-unused.md) | 低 |  |  | [credentials-file-copied-unused] (低、CR10) `ClaudeRunner.__init__(credentials_file_copied)` が未使用 |
| [graceful-stop-waits-mission](graceful-stop-waits-mission.md) | 低 |  |  | [graceful-stop-waits-mission] (低) graceful stop が走行中 improve mission の timeout (最大 60 分) を待つ (m53 で 17.5 分)。[s |
| [improvement-runs-backlog-id-on-failure](improvement-runs-backlog-id-on-failure.md) | 低 |  |  | [improvement-runs-backlog-id-on-failure] (低、run7 観測 E) 失敗 mission では `improvement_runs.backlog_id` が NULL のままで |
| [latest-in-sample-metrics-requires-row-factory](latest-in-sample-metrics-requires-row-factory.md) | 低 |  |  | [latest-in-sample-metrics-requires-row-factory] (低、同 観測 E) `latest_in_sample_metrics` は `row_factory=sqlite3.R |
| [loader-double-read-hash](loader-double-read-hash.md) | 低 |  |  | [loader-double-read-hash] (低、CR6) `_discover_one` が bytes を持ちながら path 版 `content_hash(entry)` で再読込 → artifact_ |
| [missions-finished-at-is-logical](missions-finished-at-is-logical.md) | 低 |  |  | [missions-finished-at-is-logical] (低、同 観測 D) `missions.finished_at` は終端メソッドに渡す `now` で、親ゲート 3 本 (≒70 秒) の前に束縛さ |
| [normalize-reason-duplicated](normalize-reason-duplicated.md) | 低 |  |  | [normalize-reason-duplicated] (低、CR5) `cli_runner._normalize_reason` が local_runner.py:39-56 の逐語複製、`_MAX_REASO |
| [opencode-as-mb-hardcode](opencode-as-mb-hardcode.md) | 低 |  |  | [opencode-as-mb-hardcode] (低、CR8) mission_worker.py:703 の `max(as_mb, 262144)` を settings 化 (テスト pin 無し) |
| [opencode-node-modules-copytree](opencode-node-modules-copytree.md) | 低 |  |  | [opencode-node-modules-copytree] (低、CR7) mission 毎に node_modules 63MB/3648 file を copytree。symlink + Landlock  |
| [primary-transcript-lost-on-timeout](primary-transcript-lost-on-timeout.md) | 低 |  |  | [primary-transcript-lost-on-timeout] (低) timeout→段B 追撃経路で primary の transcript (tool 64 件) が保存されず消える |
| [sandbox-pycache-prefix](sandbox-pycache-prefix.md) | 低 |  |  | [sandbox-pycache-prefix] (低、CR4) `sandbox._build_env` が PYTHONPYCACHEPREFIX 未設定 → 候補 dir に __pycache__、gate_py |
| [settings-hash-excludes-improve-gate](settings-hash-excludes-improve-gate.md) | 低 |  |  | [settings-hash-excludes-improve-gate] (低、同 調査) `settings_snapshot_hash` は `risk` + `backtest` のみ → `improve.ga |
| [legacy-2026-08-22-a-1](legacy-2026-08-22-a-1.md) | 未設定 | 2026-08-22 |  | [2026-08-22 A-1] 起動時検査④ (codex サブスク期限) の変異が段 0 で未注入 |
| [legacy-2026-08-22-a-4](legacy-2026-08-22-a-4.md) | 未設定 | 2026-08-22 |  | [2026-08-22 A-4]/[T13-5c] protocolVersion 実測 pin (Task 13 裁定 5)。claude CLI 2.1.251 の "2025-11-25" は `14029cc`  |
| [fail-observability](fail-observability.md) | 未設定 | 2026-08-30 |  | [E2E-observability]/[fail-observability] (同系) 手動発火 improve mission の失敗が missions.status 以外どこにも出ない (stderr 無し・a |
| [legacy-e2e-diag](legacy-e2e-diag.md) | 未設定 | 2026-08-30 |  | [E2E-diag] codex_runner の失敗 reason が stderr 最終行のみで、実エラー (stdout JSON events の turn.failed / error) を拾わない。PATH  |
| [legacy-e2e-egress](legacy-e2e-egress.md) | 未設定 | 2026-08-30 |  | [E2E-egress] provider=llama_swap + `--disable apps` でも codex 子が chatgpt.com / ab.chatgpt.com へ TCP 443 ×1 (str |
| [legacy-e2e-local-mcp](legacy-e2e-local-mcp.md) | 未設定 | 2026-08-30 |  | [E2E-local-mcp]/[E2E-qwen3.8→確定 2026-08-30] codex+llama_swap で MCP tool が使われない (qwen3-coder が write_staging_fi |
| [legacy-t13-f9](legacy-t13-f9.md) | 未設定 | 2026-08-30 |  | [T13-F9]/[T13-F9 改訂] codex realbackend プローブ失敗: 真因 = codex 0.150.x が `model_providers.<id>.name` を必須化 (2026-08- |
| [pair-rules-vs-data-availability](pair-rules-vs-data-availability.md) | 未設定 | 2026-09-03 |  | [pair-rules-vs-data-availability] **裁定 2026-09-03: MT5 bridge 前倒し** (ユーザー承認: ① MT5 import + UTC 検証 / ② `backte |
| [gate-admission-helper](gate-admission-helper.md) | 未設定 | 2026-09-06 |  | **[gate-admission-helper]** (2026-09-06、設計): submit/bless/improve の 3 経路が gate チェックリストを各自再導出し drift (max_bars_ |
| [scheduler-bars-per-tick-memo](scheduler-bars-per-tick-memo.md) | 未設定 | 2026-09-06 |  | **[scheduler-bars-per-tick-memo]** (2026-09-06 /code-review): live tick が open 行・pending 行ごとに `bars_fn(pair)`  |
| [jinja-templating-deferred](jinja-templating-deferred.md) | 未設定 | 2026-09-07 |  | [jinja-templating-deferred] 2026-09-07 相談: プロンプトは `str.format` のまま。jinja 化は改善 mission の種別分岐 (strategy/indicato |
| [news-source-route-omitted](news-source-route-omitted.md) | 未設定 | 2026-09-07 |  | **[news-source-route-omitted] 裁定 2026-09-07 (ユーザー承認)**: 改善ループ経由のニュースソース追加ルートを廃止。実態: 設計書 §6 にあるだけで未実装 (artifact |
| [selftest-loop-no-cutoff](selftest-loop-no-cutoff.md) | 未設定 | 2026-09-07 |  | **[selftest-loop-no-cutoff] 設計承認 2026-09-07** (ユーザー裁定: ① Tier F 分離起票 / ② Tier B「1 回目の self-test は許す」/ ③ codex  |
| [indicator-first-seeding](indicator-first-seeding.md) | 未設定 | 2026-09-12 |  | [indicator-first-seeding] (設計待ち、ユーザー方針 2026-09-12) 初期 indicator plugin セットを人間が用意し (MACD / ボリンジャー / ATR / ADX 等 |
| [pine-script-to-plugin](pine-script-to-plugin.md) | 未設定 | 2026-09-12 |  | [pine-script-to-plugin] (設計待ち、ユーザー要望 2026-09-12) TradingView Pine script を agentic-fx plugin (indicator/strate |
| [floor-path-skips-duplicate-metrics](floor-path-skips-duplicate-metrics.md) | 未設定 | 2026-09-13 |  | **[floor-path-skips-duplicate-metrics] 裁定済 (c) 現状維持 (2026-09-13、ユーザー承認)**: 質検査はフロア経路に入れない・母集団も広げない。根拠 = (a) はフ |
| [indicator-initial-set](indicator-initial-set.md) | 未設定 | 2026-09-15 |  | **[indicator-initial-set]** (2026-09-15 ユーザー方針、[indicator-consumption-wiring] 完了直後に着手): 標準指標を人間提供で初期導入する — sma |
| [indicator-reference-oracle-gate](indicator-reference-oracle-gate.md) | 未設定 | 2026-09-15 |  | **[indicator-reference-oracle-gate]** (2026-09-15 起票、設計待ち): indicator には strategy の backtest / フロアに相当する正しさの門が無 |
| [multi-timeframe-indicator-deps](multi-timeframe-indicator-deps.md) | 未設定 | 2026-09-15 |  | **[multi-timeframe-indicator-deps]** (2026-09-15 起票、[indicator-consumption-wiring] §1 でスコープ外にした項目): 1 strategy |
| [fake-run-context-consolidation](fake-run-context-consolidation.md) | 未設定 | 2026-09-17 |  | [fake-run-context-consolidation] 起票 2026-09-17 (段 0 束 3 提案 5): `tests/runners/test_worker_runner.py` のフェイク Mis |
| [first-run-setup](first-run-setup.md) | 未設定 | 2026-09-17 |  | **[first-run-setup] 方針 2026-09-17 (ユーザー)**: 初回起動は対話的な設定を必須にする (LLM 接続先 / トレード足 / 戦略作成のきっかけ / 戦略改善周期 等)。設定完了をもっ |
| [backtest-available-lists-over-max-bars](backtest-available-lists-over-max-bars.md) | 未設定 | 2026-09-18 |  | [backtest-available-lists-over-max-bars] 起票 2026-09-18 (同 T-4、低): `run_backtest_handler` の `available` は `outp |
| [indicator-result-wire-validation-unify](indicator-result-wire-validation-unify.md) | 未設定 | 2026-09-18 |  | [indicator-result-wire-validation-unify] 起票 2026-09-18 (iw 2 周目 CR8 見送り): `plugin/sandbox._validate_indicator_ |
| [subprocess-allowlist-by-line-number](subprocess-allowlist-by-line-number.md) | 未設定 | 2026-09-18 |  | [subprocess-allowlist-by-line-number] 起票 2026-09-18 (同 T-3): `tests/test_subprocess_stdin_policy.py` の allowli |
| [wire-list-branch-unreachable](wire-list-branch-unreachable.md) | 未設定 | 2026-09-18 |  | [wire-list-branch-unreachable] 起票 2026-09-18 (同 T-2): `worker._indicator_result_to_wire` の `list` 分岐は唯一の呼び出し元が |
| [iw-r3-unverified-residuals](iw-r3-unverified-residuals.md) | 未設定 | 2026-09-19 |  | [iw-r3-unverified-residuals] 起票 2026-09-19 (iw 3 周目で「未検証」のまま残った 3 点、`tmp/review-20260919-iw-r3/review-r3.md`): |
| [ops-ui](ops-ui.md) | 未設定 | 2026-09-19 |  | **[ops-ui] 方針 2026-09-19 (ユーザー)**: 操作 API (設計書 §7) + Discord 承認 bot + 読み取り専用 web ダッシュボードの束。**配布形では汎用 bot (`~/p |
| [retire-symlink-deployed-plugin](retire-symlink-deployed-plugin.md) | 未設定 | 2026-09-19 |  | [retire-symlink-deployed-plugin] 起票 2026-09-19 ([indicator-initial-set] プラン起草時に判明): `afx plugin retire` (`swit |
| [improve-catchup-runs-at-startup](improve-catchup-runs-at-startup.md) | 未設定 | 2026-09-20 |  | [improve-catchup-runs-at-startup] 起票 2026-09-20 (観測、要確認): サービス起動の直後に improve mission #85 が自動起動した (`schedule.im |
| [switch-ops-code-review-r2-altitude](switch-ops-code-review-r2-altitude.md) | 未設定 | 2026-09-20 |  | [switch-ops-code-review-r2-altitude] 起票 2026-09-20 ([switch-ops-hardening] 2 周目 /code-review #3,4,6〜10、いずれも保守性 |
| [live-confirmed-bars-into-history](live-confirmed-bars-into-history.md) | 未設定 | 2026-09-21 |  | **[live-confirmed-bars-into-history]** (2026-09-21 ユーザー論点 + codex 確認 `tmp/design-a2/unify-check.md`、A2-1 の後):  |
| [multi-decision-timeframes](multi-decision-timeframes.md) | 未設定 | 2026-09-21 |  | **[multi-decision-timeframes]** (2026-09-21 ユーザー構想、着手は優先 ticket の後 = 初回設定ウィザード前後が目安): 基準 tick を最小足にし、各 tick で確 |
| [policy-path-literal-in-four-places](policy-path-literal-in-four-places.md) | 未設定 | 2026-09-21 |  | [policy-path-literal-in-four-places] (Minor、2026-09-21 [ops-first-contact-fixes] 1 周目 codex): `root / "policy" |
| [abandoned-signal-observability](abandoned-signal-observability.md) | 未設定 | 2026-09-27 |  | **[abandoned-signal-observability] 起票 2026-09-27 (B-2 論点、観測待ち)**: 建玉ゼロの間は signal 起動 mission を起こさない規則 (D2) のため、 |
| [improve-add-fullwidth-placeholder](improve-add-fullwidth-placeholder.md) | 未設定 | 2026-09-27 |  | **[improve-add-fullwidth-placeholder] 起票 2026-09-27 (`/code-review high` ofc r2、低)**: `improve add` のプレースホルダ判定 |
| [intent-evidence-timeframe-gate](intent-evidence-timeframe-gate.md) | 未設定 | 2026-09-27 |  | **[intent-evidence-timeframe-gate] 起票 2026-09-27 (B-2 段 d の spec §2 で範囲外に切り出し、設計待ち)**: `role` (primary / conte |
| [market-tool-budget-persist-via-alert-state](market-tool-budget-persist-via-alert-state.md) | 未設定 | 2026-09-27 |  | **[market-tool-budget-persist-via-alert-state] 起票 2026-09-27 (B-2 段 d、見送り、低)**: `context_daily_call_budget` 超過 |
| [realbackend-tests-fail-under-claude-code](realbackend-tests-fail-under-claude-code.md) | 未設定 | 2026-09-27 |  | **[realbackend-tests-fail-under-claude-code] 起票 2026-09-27 (環境)**: `tests/loops/test_verify_backend_realbacken |
| [service-secret-env-leaked-matches-redundant](service-secret-env-leaked-matches-redundant.md) | 未設定 | 2026-09-27 |  | **[service-secret-env-leaked-matches-redundant] 起票 2026-09-27 (同、整理級)**: `service.py` 検査⑤の `leaked` list と `ma |
| [test-init-offline-unreachable-bridge-flake](test-init-offline-unreachable-bridge-flake.md) | 未設定 | 2026-09-27 |  | **[test-init-offline-unreachable-bridge-flake] 起票 2026-09-27 (flake)**: `tests/test_init_and_guard.py::test_in |
| [trade-timeout-on-startup](trade-timeout-on-startup.md) | 未設定 | 2026-09-28 |  | [trade-timeout-on-startup] 起動直後の cron trade mission が stop 時に timeout で終わる (m41/m43)。 **再観測 2026-09-28: 再起動時に走 |
| [backend-descriptor](backend-descriptor.md) | 未設定 |  |  | [backend-descriptor] (Minor): backend 別 if-ladder ×5 (service/factory/worker_runner/mission_worker/verify_back |
| [backlog-dup](backlog-dup.md) | 未設定 |  |  | [backlog-dup]/[backlog-dedup-miss] (統合) discoveries 由来の backlog #12/#13 が実質重複 (「config 許可キーは kind と params」)、# |
| [db-rebuild-helper](db-rebuild-helper.md) | 未設定 |  |  | [db-rebuild-helper] (Minor): store/db.py の table rebuild migration 7 本を parameterised helper に (fk_check 範囲の食い |
| [improve-add-quotes](improve-add-quotes.md) | 未設定 |  |  | [improve-add-quotes] cosmetic: `improve add` がクォートを剥がさない (`commands.py:189`)。[sweep-empty-dirs] cosmetic: swee |
| [kind-read-duplication](kind-read-duplication.md) | 未設定 |  |  | [kind-read-duplication] `_read_candidate_kind` が `candidate_meta.kind` を使わず config.yaml を再パース (CR6 の一本化が及んでいない |
| [legacy-d-5](legacy-d-5.md) | 未設定 |  |  | [D-5] afx plugin submit → submit_plugin → _validate_strategy の旧回廊が §8.1-41 fail closed を素通り (閾値未満でも approval 行 |
| [legacy-d-stage0-e](legacy-d-stage0-e.md) | 未設定 |  |  | [D-stage0→束E] switch.py resume 経路が .resolve() 無しで symlink 版ディレクトリを受理 (fresh 経路と非対称)。stage0-bundle-D.md 特例節参照。束 |
| [legacy-e2e-codex-sigtrap](legacy-e2e-codex-sigtrap.md) | 未設定 |  |  | [E2E-codex-sigtrap] codex+chatgpt の improve worker 環境下で codex の tool 実行基盤 (exec_command) が SIGTRAP 死し MCP tool |
| [legacy-e2e-daemon-quiet](legacy-e2e-daemon-quiet.md) | 未設定 |  |  | [E2E-daemon-quiet] stdin 非 TTY だと `afx` が黙って daemon モードになる (entry.py `not sys.stdin.isatty()`)。プロンプトも警告も出ず 30  |
| [legacy-e2e-econ-429](legacy-e2e-econ-429.md) | 未設定 |  |  | [E2E-econ-429] サービス起動毎に econ fetch が走り、再起動連打で HTTP 429 (実測)。起動時 fetch のレート制御/クールダウンが無い — outbound-request-budg |
| [legacy-e2e-model](legacy-e2e-model.md) | 未設定 |  |  | [E2E-model] `runner.<lane>.model` が backend 非分離 — backend 切替のたびにモデル名の手編集が要る (chatgpt=codex 系 / claude=claude 系 |
| [legacy-e2e-ttl](legacy-e2e-ttl.md) | 未設定 |  |  | [E2E-ttl] llama-swap TTL=120s と improve mission の相性: アイドル 2 分でモデル unload、30B 再ロード 1〜2 分がターン毎に挟まり得る。TTL 延長 (lla |
| [legacy-l72](legacy-l72.md) | 未設定 |  |  | [L72] holdout の空期間分岐 (束 C 由来でない既存コードの穴)。tmp/review-bundleC/verified-round1.md 参照 |
| [legacy-oc-example-leak](legacy-oc-example-leak.md) | 未設定 |  |  | [OC-example-leak] プロンプト実例の idea「RSI の期間を 14 から 21 に…」が discoveries へ逐語コピーされ backlog #3 として実在化 (m13) — 実例の内容汚染。 |
| [legacy-oc-qwen3-8-toolcall](legacy-oc-qwen3-8-toolcall.md) | 未設定 |  |  | [OC-qwen3.8-toolcall] m20 primary 960 秒の中身 (transcript 初観測): tool_use 94 件中 82 失敗 (87%)。afx_analyze_corr を 67  |
| [legacy-oc-resume-discard](legacy-oc-resume-discard.md) | 未設定 |  |  | [OC-resume-discard 残] M4 の report 降格経路は未発火 (m28 はモデルが自発的に observation を出したため) — 別途 report 型で確認要 |
| [legacy-oc-tool-not-found](legacy-oc-tool-not-found.md) | 未設定 |  |  | [OC-tool-not-found] m24/m26 で `read_staging_file`/`read_plugin_source` の `not found` が 7-11 回 (staging に書く前に読む |
| [legacy-oc-websearch-ssl](legacy-oc-websearch-ssl.md) | 未設定 |  |  | [OC-websearch-ssl] m26 で `afx_web_search` 10 回中 5 回 `DDGSException: SSL: CERTIFICATE_VERIFY_FAILED` (同 mission |
| [legacy-t13-step7](legacy-t13-step7.md) | 未設定 |  |  | [T13-Step7] 実機 E2E 手順 3 以降 (承認申請生成・unshare・strace egress・継続実測 7 件) 未実施。**注: runbook (task13-real-backend-runbo |
| [legacy-t9b-go-not-sent-parent-runaway](legacy-t9b-go-not-sent-parent-runaway.md) | 未設定 |  |  | [起票] t9b 変異「go を送らない」で親がスレッド暴走 (67 本・12 分超、変異下のみ) → 子が進まないときの親 timeout 経路を D-9 追補検収で実測 |
| [mission-output-not-persisted](mission-output-not-persisted.md) | 未設定 |  |  | [mission-output-not-persisted] missions.output_json NULL / transcript_json `[]` (m34〜m42 全部) — 既存の観測性負債 |
| [mission46-selftest-loop](mission46-selftest-loop.md) | 未設定 |  |  | [mission46-selftest-loop] 観測: qwen3.8 が self-test 修正に 53 write / 51 run_plugin_tests を費やし run_backtest 0 回。プロン |
| [mt5-import-window-before-data-start](mt5-import-window-before-data-start.md) | 未設定 |  |  | [mt5-import-window-before-data-start] `--from` がデータ開始より前だと bridge が最古バー 1 本を要求窓外で返し importer が fail-closed で停止 |
| [observation-carryover-mismatch](observation-carryover-mismatch.md) | 未設定 |  |  | [observation-carryover-mismatch] observation の申し送り (「staging 書込済・再開可能」) と実態 (`afx_list_staging` 空 — staging は  |
| [rpc-call-helper](rpc-call-helper.md) | 未設定 |  |  | [rpc-call-helper] (Minor): mission_worker の `_RagRpcProxy._call` / `_make_rpc_client.call` の framing 2 重実装を 1  |
| [scheduler-fires-on-startup](scheduler-fires-on-startup.md) | 未設定 |  |  | [scheduler-fires-on-startup] (観測、run8 C) trade mission は毎時 hh:12 だけでなく afx 起動直後にも発火する — E2E の時間帯回避は「起動 → trade |
| [seed-priority](seed-priority.md) | 未設定 |  |  | [seed-priority] ユーザー種まき #26 (open) より過去 observation #18 が選ばれ #26 の指示 (5/20 固定) が無視された。規約上正当だが source=user の op |
| [selection-rationale-unverified](selection-rationale-unverified.md) | 未設定 |  |  | [selection-rationale-unverified] selection_rationale が未検証で payload に載る (m40 は attempts を捏造)。attempts と突き合わせで検出 |
| [tier-a-directive-ignored](tier-a-directive-ignored.md) | 未設定 |  |  | [tier-a-directive-ignored] (重要、run9 観測 A) Tier A の repeated_failure directive は 9 回連続で無視された。Tier A には abort tr |

## 是正済 (41)

| id | 優先 | 起票 | 完了 | 概要 |
|---|---|---|---|---|
| [a23c-lite-auto-resume-when-flat](a23c-lite-auto-resume-when-flat.md) | 高 | 2026-09-29 | 2026-09-29 | **A2-3c-lite (建玉も指値も無い episode の自動 ready) 完結 main (2026-09-29、push 済)** — `datafeed.outage.auto_resume_when_fl |
| [expire-stale-activity-in-store](expire-stale-activity-in-store.md) | 未設定 | 2026-09-27 | 2026-09-28 | [expire-stale-activity-in-store] / [friday-cutoff-validation] / [cli-bless-runtime-error-traceback] **是正済 (bra |
| [outage-stalled-ignores-closed-hours](outage-stalled-ignores-closed-hours.md) | 未設定 | 2026-09-28 | 2026-09-28 | [outage-stalled-ignores-closed-hours] / [settings-example-deprecated-keys] **是正済 main `ed2de56` (2026-09-28、pu |
| [signal-producer-reads-import-table-under-mt5-primary](signal-producer-reads-import-table-under-mt5-primary.md) | 高 | 2026-09-28 | 2026-09-28 | [signal-producer-reads-import-table-under-mt5-primary] **是正済 main `bb4961d` (2026-09-28、push 済)** — `_run_sign |
| [yfinance-source-calendar](yfinance-source-calendar.md) | 中 | 2026-09-28 | 2026-09-28 | 上記の第 2 段 **[yfinance-source-calendar] 起票 2026-09-28 (中、ユーザー要望)**: yfinance は日曜 21:00〜23:00 UTC の足を持たない。source  |
| [dispatcher-join-budget-pin](dispatcher-join-budget-pin.md) | 未設定 | 2026-09-27 | 2026-09-27 | [dispatcher-join-budget-pin] 是正済 `e753324` (2026-09-27、小物 2 件の束、pin `aa08310`) — worker_runner の kind 別 RPC ti |
| [graceful-stop-hang](graceful-stop-hang.md) | 未設定 | 2026-09-27 | 2026-09-27 | [graceful-stop-hang] **閉 2026-09-27** — 停止 461 秒 (9/27 08:39、b0de51f 以前の supervisor join) は B-2 段 a の supervis |
| [signal-producer-closed-market-warning](signal-producer-closed-market-warning.md) | 未設定 | 2026-09-27 | 2026-09-27 | [signal-producer-closed-market-warning] 是正済 `e753324` (2026-09-27) — 閉場中は signal producer の評価を止め (cursor・DB に触 |
| [writable-provider-quote-chain-ignores-primary](writable-provider-quote-chain-ignores-primary.md) | 中 | 2026-09-24 | 2026-09-24 | **[writable-provider-quote-chain-ignores-primary]** (**解消 2026-09-24、A2-3a、main squash**。重要度: 中〜高、2026-09-24 A |
| [paper-fill-misses-intrabar-touch-on-forming-bar](paper-fill-misses-intrabar-touch-on-forming-bar.md) | 中 | 2026-09-22 | 2026-09-21 | **[paper-fill-misses-intrabar-touch-on-forming-bar]** (**解消 2026-09-22、A2-1a、main squash commit。導入時に ohlcv_cac |
| [retry-switched-approves-without-deploy](retry-switched-approves-without-deploy.md) | 未設定 | 2026-09-20 | 2026-09-19 | [retry-switched-approves-without-deploy] / [cli-bless-unresolved-journal] / [switch-not-required-skips-hash-ve |
| [bootstrap-probe-tests-mkdir-real-logs-dir](bootstrap-probe-tests-mkdir-real-logs-dir.md) | 中 | 2026-09-12 | 2026-09-12 | [bootstrap-probe-tests-mkdir-real-logs-dir] **是正済 test-hygiene T1 (2026-09-12、値コピー import 解消 + env `AGENTIC_FX |
| [codex-subscription-expiry-check-never-fires](codex-subscription-expiry-check-never-fires.md) | 低 | 2026-09-12 | 2026-09-12 | [codex-subscription-expiry-check-never-fires] **是正済 test-hygiene T4 (2026-09-12、検査撤去 + stderr fatal `usage lim |
| [plugin-locks-accumulate](plugin-locks-accumulate.md) | 低 | 2026-09-12 | 2026-09-12 | [plugin-locks-accumulate] **是正済 test-hygiene T2 (2026-09-12、startup sweep ⑧、flock 確認付き、name 正規形検証)** — 旧: (低)  |
| [profitability-floor](profitability-floor.md) | 高 | 2026-09-13 | 2026-09-12 | [profitability-floor] **完結 cd19457 (2026-09-13、spec v1.4 / plan v1.9、実機未観測)** — 旧: (高、2026-09-12 ユーザー承認、順序 = t |
| [reject-reason-leak](reject-reason-leak.md) | 高 | 2026-09-12 | 2026-09-12 | [reject-reason-leak] **是正済 [profitability-floor] T0 (cd19457、`rejected_by_human` 固定 + 除染 7 行)** — 旧: (高、2026-0 |
| [codex-backend-no-proc-landlock](codex-backend-no-proc-landlock.md) | 未設定 | 2026-09-11 | 2026-09-11 | **[codex-backend-no-proc-landlock] (Critical、A4 10 回目 codex #71 観測 A、2026-09-11、裁定待ち)** `mission_worker.py:161 |
| [gate-failed-ledger-discarded](gate-failed-ledger-discarded.md) | 未設定 | 2026-09-09 | 2026-09-10 | **[gate-failed-ledger-discarded] 設計中 (2026-09-09、`tmp/design-ledger-preserve/design.md` v4、codex 4 周目走行中)**: L |
| [analyze-corr-rpc-double-unwrap](analyze-corr-rpc-double-unwrap.md) | 未設定 | 2026-09-09 | 2026-09-09 | **[analyze-corr-rpc-double-unwrap] 是正済み 2026-09-09** `f432dbf` (子が `{"request": …}` を送る + 全 RPC 種別の契約テスト) → co |
| [mission-abort-on-tool-budget](mission-abort-on-tool-budget.md) | 高 | 2026-09-08 | 2026-09-09 | **[mission-abort-on-tool-budget] (高) 設計 v4 `tmp/design-mission-abort/design.md` (2026-09-08、codex 3 周 `codex-d |
| [system-note-type-by-cause](system-note-type-by-cause.md) | 未設定 | 2026-09-09 | 2026-09-09 | **[system-note-type-by-cause] 是正済み 2026-09-09** `e048c4f`: backtest 0 → 型 A / abort → 型 B / timeout・max_turns  |
| [tier-b-release-requires-evaluable-backtest](tier-b-release-requires-evaluable-backtest.md) | 未設定 | 2026-09-09 | 2026-09-09 | **[tier-b-release-requires-evaluable-backtest] 是正済み 2026-09-09** `e048c4f` → codex Important (evaluable は gate |
| [tool-exception-bypasses-refusal-streak](tool-exception-bypasses-refusal-streak.md) | 未設定 | 2026-09-09 | 2026-09-09 | **[tool-exception-bypasses-refusal-streak] 是正済み 2026-09-09** `f432dbf`/`7e0fc16` (on_result フック、(tool, 失敗種別) の |
| [backtest-kill-switch-latch-truncates-in-sample](backtest-kill-switch-latch-truncates-in-sample.md) | 未設定 | 2026-09-06 | 2026-09-06 | **[backtest-kill-switch-latch-truncates-in-sample]** (2026-09-06 CP8 診断、Important) **裁定 (a) → 設計 v5 承認 2026-09 |
| [market-calendar-broker-mismatch](market-calendar-broker-mismatch.md) | 未設定 | 2026-09-06 | 2026-09-06 | **[market-calendar-broker-mismatch] 是正済 `6cd4934` + `74a23ec` (祝日 = 取引日ラベル)、1 周目レビュー済** (2026-09-06 CP8 で発見、Cr |
| [pytest-basetemp-garbage](pytest-basetemp-garbage.md) | 未設定 | 2026-09-05 | 2026-09-05 | [pytest-basetemp-garbage] **是正済 b0d27a9** — テストが残す 0500/0400 ツリーで pytest の basetemp 掃除が失敗し `/tmp/pytest-of-<us |
| [activity-success-silent](activity-success-silent.md) | 未設定 | 2026-09-01 | 2026-09-01 | [activity-success-silent]/[observation-activity-silent] 裁定 2026-09-01: IMPROVE 成功系 3 行 (`approval_requested` / |
| [gate-accepts-noop-artifact](gate-accepts-noop-artifact.md) | 未設定 | 2026-09-01 | 2026-09-01 | [gate-accepts-noop-artifact]/[research-facts-are-selectable-work] 是正済 `cd3ba1c` (2026-09-01) — noop_gate (AST+ |
| [holder-pid-format](holder-pid-format.md) | 未設定 | 2026-09-01 | 2026-09-01 | [holder-pid-format] 是正済 (2026-09-01) — holder.pid を bare PID に統一 |
| [legacy-oc-context-limit-hardcoded](legacy-oc-context-limit-hardcoded.md) | 未設定 | 2026-09-01 | 2026-09-01 | [OC-context-limit-hardcoded] 裁定 2026-09-01: `runner.opencode.context_limit` (初期値 0、improve.backend=opencode なら |
| [news-seed-on-start](news-seed-on-start.md) | 未設定 | 2026-09-01 | 2026-09-01 | [news-seed-on-start]/[backtest-tool-offered-to-indicator] 是正済 `b76f1c7` (2026-09-01) — build_app で DEFAULT_SOU |
| [review-material-added-names](review-material-added-names.md) | 未設定 | 2026-09-01 | 2026-09-01 | [review-material-added-names] 是正済 (2026-09-01) — `review_material.py` の `_added_names` を hunk 行番号→block 写像に (0 |
| [examples-unreachable](examples-unreachable.md) | 未設定 | 2026-08-31 | 2026-08-31 | [examples-unreachable] 是正済 `ed0f0f1` (2026-08-31) — list_examples / read_example_plugin 新設 + not found hint +  |
| [legacy-oc-run-diag](legacy-oc-run-diag.md) | 未設定 | 2026-08-31 | 2026-08-31 | [OC-run-diag]/[run_plugin_tests-EACCES] 是正済 `a59c0be` → `2a08e34` (2026-08-31) — run_plugin_tests 失敗を stderr 込 |
| [loader-none-crash](loader-none-crash.md) | 未設定 | 2026-08-31 | 2026-08-31 | [loader-none-crash] 是正済 `14ca38a`/`b513d91`/`0cd4efb` (2026-08-31) — `commit()` の `_discover_one` None を Attri |
| [parser-first-object-decoy](parser-first-object-decoy.md) | 未設定 | 2026-08-31 | 2026-08-31 | [parser-first-object-decoy] 是正済 `c17e46d` (2026-08-31) — parse_json_output に prefer_keys (opencode 経路のみ schema |
| [report-tx-crash](report-tx-crash.md) | 未設定 | 2026-08-31 | 2026-08-31 | [report-tx-crash] 是正済 `71e342d` (2026-08-31) — report_state='prepared' UPDATE の暗黙 tx を独立 tx 化 (M4 実装時に発見) |
| [test-fixtures-from-real-transcripts](test-fixtures-from-real-transcripts.md) | 未設定 | 2026-08-31 | 2026-08-31 | [OC-resume-discard] 是正済 `f0e9f87`/`5705f55`/`5dd7fd0` (2026-08-31) — 追撃回収の discard 理由をマーカー記録、追撃 stderr を保持、`_l |
| [legacy-2026-08-30](legacy-2026-08-30.md) | 未設定 | 2026-08-30 | 2026-08-30 | [解決 2026-08-30] gate_pytest テストが実 data/agentic.db を上書き→unlink していた Critical、`72df94d` 是正 + conftest session ガー |
| [legacy-oc-e2e](legacy-oc-e2e.md) | 未設定 | 2026-08-30 | 2026-08-30 | [OC-E2E] 是正済 `da5bb04` (2026-08-30) — 最終 JSON 実例 + 「JSON 1 個のみ」/ 「ファイル操作は afx MCP tool のみ」を improve_mission.md |
| [legacy-oc-websearch](legacy-oc-websearch.md) | 未設定 | 2026-08-30 | 2026-08-30 | [OC-websearch] 是正済 `dda60f9` (2026-08-30) — ddgs 9.15.0 の keyword-only 化に追随 + シグネチャ pin 2 本 |

