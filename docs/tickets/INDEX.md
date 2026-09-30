# チケット一覧

自動生成: `ticket.py index` (tickets skill)。手で編集しない。
未完了: 134 件。完了分は CLOSED.md。

## 実装中 (7)

| id | 優先 | 起票 | 概要 |
|---|---|---|---|
| [strategy-gate-data-error-crashes-commit](strategy-gate-data-error-crashes-commit.md) | 未設定 | 2026-09-02 | /[run-backtest-error-opaque]/[staging-snapshot-src-leak]/[s… |
| [backtest-rpc-timeout-15s](backtest-rpc-timeout-15s.md) | 未設定 | 2026-09-03 | /[gate-failed-rows-not-persisted] **是正済 0a453af 2026-09-03、… |
| [eval-source-consolidation](eval-source-consolidation.md) | 未設定 | 2026-09-03 | `_EVAL_SOURCE`×3 + `ANALYSIS_SOURCE` → `settings.backtest.e… |
| [rpc-result-not-json-serializable](rpc-result-not-json-serializable.md) | 未設定 | 2026-09-03 | /[opencode-mcp-timeout-60s] **是正済 81a2158 2026-09-03、未レビュー*… |
| [unreviewed-bundle-2026-09-05](unreviewed-bundle-2026-09-05.md) | 未設定 | 2026-09-03 | 。段 0 完了 2026-09-03: 変異 10 件、生存 2 件 (M1 commit の全 ValueError… |
| [ledger-never-populated-in-production](ledger-never-populated-in-production.md) | 未設定 | 2026-09-04 | /[no-history-typed-exception] **是正済 f67820d 2026-09-04 (2 周… |
| [trade-consume-before-record](trade-consume-before-record.md) | 未設定 | 2026-09-05 | /[notifier-under-core-lock-in-tick] 是正済 44f6244 → 段 0 f9c24… |

## 実装待ち (8)

| id | 優先 | 起票 | 概要 |
|---|---|---|---|
| [legacy-2026-08-23-c-0](legacy-2026-08-23-c-0.md) | 未設定 | 2026-08-23 | [2026-08-23 束 C 段 0] `tests/datafeed/test_fetchers.py` 等 11… |
| [flake-trade-claude-real-process](flake-trade-claude-real-process.md) | 未設定 | 2026-09-20 | 起票 2026-09-20 ([switch-ops-hardening] 1 周目是正のフルスイート): `test… |
| [switch-ops-r2b-low-unpinned](switch-ops-r2b-low-unpinned.md) | 未設定 | 2026-09-20 | 起票 2026-09-20 ([switch-ops-hardening] 2 周目やり直しローカル LLM、束の d… |
| [flake](flake.md) | 未設定 |  | test_service_app.py::test_interactive_mode_actually_stops_v… |
| [flake-rpc-timeout-by-kind](flake-rpc-timeout-by-kind.md) | 未設定 |  | `tests/runners/test_worker_runner.py:1129` 種別 timeout テストが … |
| [flake-shell-interrupt-timing](flake-shell-interrupt-timing.md) | 未設定 |  | [起票] `tests/test_shell_interrupt.py` は別 pytest 並走でタイミング依存 f… |
| [flaky-tool-schemas](flaky-tool-schemas.md) | 未設定 |  | `tests/tools/test_tool_impls.py::test_all_tools_have_schema… |
| [test-isolation](test-isolation.md) | 未設定 |  | tests/loops/conftest.py の loop_min/loop_full が特定 4 ファイル同時の部… |

## 裁定待ち (9)

| id | 優先 | 起票 | 概要 |
|---|---|---|---|
| [legacy-2026-08-23-e-11-m3-m7](legacy-2026-08-23-e-11-m3-m7.md) | 未設定 | 2026-08-23 | [2026-08-23 E-11 検収 m3/m7] `apply_decision` のキー方向 (設計 §5.1-… |
| [backtest-base-interval](backtest-base-interval.md) | 未設定 | 2026-09-05 | バックテスト基底足を `backtest.base_interval` (1m/5m/15m、既定 1m) で可変に。… |
| [sl-gap-fill-ignores-gap](sl-gap-fill-ignores-gap.md) | 未設定 | 2026-09-06 | 訂正 (09-06 codex 2 周目): `paper_fills.check_exit` は**始値 gap は… |
| [approval-payload-missing-gate-metrics](approval-payload-missing-gate-metrics.md) | 未設定 | 2026-09-11 | approval payload の `in_sample` / `holdout` / `eval_timefram… |
| [mission-prompt-in-argv-readable-via-proc](mission-prompt-in-argv-readable-via-proc.md) | 未設定 | 2026-09-11 | claude/codex/opencode の worker は Landlock で `/proc` 全体を rea… |
| [claude-builtin-tools-exposed](claude-builtin-tools-exposed.md) | 未設定 | 2026-09-12 | claude backend の子プロセスは `--allowedTools mcp__afx__*` でも組み込み … |
| [unprofitable-note-hygiene](unprofitable-note-hygiene.md) | 未設定 | 2026-09-13 | フロア不合格 (`unprofitable`) で終わった mission が起票した agent note (#70… |
| [gate-noop-followup](gate-noop-followup.md) | 未設定 |  | 現行 note 化されていない fact 行が残る間は選択され得る (裁定待ち)。次回 E2E で `kind` 必須… |
| [improve-mission-zero-tool-calls-undetected](improve-mission-zero-tool-calls-undetected.md) | 未設定 |  | registry の tool 呼び出し 0 件で終端した improve mission が「agent が自主的に… |

## 設計待ち (110)

| id | 優先 | 起票 | 概要 |
|---|---|---|---|
| [indicator-consumption-wiring](indicator-consumption-wiring.md) | 高 | 2026-09-12 | plugin 契約 `evaluate(df, indicators, signals, params)` は ind… |
| [backtest-worker-cpu-budget-shrinks-with-timeframe](backtest-worker-cpu-budget-shrinks-with-timeframe.md) | 高 | 2026-09-20 | strategy の backtest は plugin worker 1 プロセスを再生全体で使い回すので、`plu… |
| [backtest-rpc-timeout-does-not-stop-parent-work](backtest-rpc-timeout-does-not-stop-parent-work.md) | 高 | 2026-09-21 | `backtest_rpc_timeout_sec=600` は agent への応答を打ち切るだけで、親サービス内の… |
| [outage-stalled-on-broker-daily-rollover-gap](outage-stalled-on-broker-daily-rollover-gap.md) | 高 | 2026-09-29 | MT5 (OANDA Japan、サーバ UTC+3) は日次ロールオーバー 21:00 UTC 前後に数分ティックが… |
| [live-signal-eval-blocks-protection-tick](live-signal-eval-blocks-protection-tick.md) | 高 |  | signal maintenance は scheduler tick 内の同期 hook。例外は握るが、「完了はする… |
| [backtest-dedup-cache](backtest-dedup-cache.md) | 中 | 2026-09-08 | 同一 (候補, config) の backtest 再実行で予算を消費しない (run6: 6 枠中 3 枠がビット… |
| [baseline-replay-unimplemented](baseline-replay-unimplemented.md) | 中 | 2026-09-12 | 設計書 2026-08-16 §4.2-4「(pair, timeframe) ごとに現在 live の D4-app… |
| [candidates-converge-to-example-sma](candidates-converge-to-example-sma.md) | 中 | 2026-09-12 | codex #74 / ornith #75 / muse #76 の提出候補は content_hash が異なるだ… |
| [human-corridor-gate-rows-null-outcome](human-corridor-gate-rows-null-outcome.md) | 中 | 2026-09-12 | 人間回廊 (`submit_candidate` / `bless_candidate` → `_run_full_g… |
| [improve-targeted-run](improve-targeted-run.md) | 中 | 2026-09-12 | 現状は `improve add <text>` + 引数なし `improve` で LLM が open から自分… |
| [legacy-submit-corridor-bypasses-gate](legacy-submit-corridor-bypasses-gate.md) | 中 | 2026-09-12 | `afx plugin submit <name>` (live plugins/ の既存 plugin) は `ap… |
| [policy-add-unwired-in-service](policy-add-unwired-in-service.md) | 中 | 2026-09-20 | `service.py:1054` の `Commands(...)` に `policy_path` を渡していない… |
| [secret-env-guard-false-positive](secret-env-guard-false-positive.md) | 中 | 2026-09-20 | 改善 backend を CLI 系 (claude / codex) にすると、起動時の検査⑤ (`service.… |
| [db-healthcheck-continuous-session-freshness](db-healthcheck-continuous-session-freshness.md) | 中 | 2026-09-28 | `service.py:1243` `db_healthcheck` の鮮度式 `bar start + 2×幅 + … |
| [live-storage-source-mapping-scattered](live-storage-source-mapping-scattered.md) | 中 | 2026-09-28 | primary → ライブ保存名 (`mt5` → `mt5-live`) の写像が 8 箇所に散在 (service… |
| [signal-producer-catchup-requires-latest-tail](signal-producer-catchup-requires-latest-tail.md) | 中 | 2026-09-28 | producer は過去 bucket を順に評価 (`signal_producer.py:195`) しながら、毎… |
| [trade-cron-hybrid-mode](trade-cron-hybrid-mode.md) | 中 | 2026-09-28 | 取引判断 LLM の起動をハイブリッドにする — 建玉・未約定指値が**ある**間だけ判断足ごとの cron miss… |
| [sandbox-worker-response-unvalidated](sandbox-worker-response-unvalidated.md) | 中 | 2026-09-30 | worker 応答の pid と error 文字列が無検証で session とログに入る |
| [harness-failure-becomes-fact](harness-failure-becomes-fact.md) | 中 |  | harness 由来の失敗 (timeout 等) をモデルが「環境制約 fact」として note 化し続ける (#… |
| [refused-errors-double-count](refused-errors-double-count.md) | 中 |  | 予算拒否は terminal streak と tool_error streak の両方に入り `refused=1… |
| [reject-revert-without-live-classification](reject-revert-without-live-classification.md) | 低 | 2026-09-20 | `reject_candidate` (`switch.py:1866-1875`) は自分の未完 journal が… |
| [improve-add-tokenizer-collapses-whitespace](improve-add-tokenizer-collapses-whitespace.md) | 低 | 2026-09-21 | シェルの tokenizer (`line.strip().split()` → `" ".join`) が全角空白・… |
| [trade-prompt-says-hourly](trade-prompt-says-hourly.md) | 低 | 2026-09-28 | 取引判断 prompt の冒頭「1 時間毎に呼び出され」が固定文言のまま (判断足 15m では 15 分毎)。`de… |
| [outage-observe-closed-guard-unpinned](outage-observe-closed-guard-unpinned.md) | 低 | 2026-09-29 | `OutageStateMachine.observe` の「閉場中は観測しない」early return を落として… |
| [signal-producer-hourly-warning-before-confirmation](signal-producer-hourly-warning-before-confirmation.md) | 低 | 2026-09-29 | producer が毎時 xx:00:07 の tick で「1h bucket not yet present」を … |
| [sandbox-lifecycle-minor-followups](sandbox-lifecycle-minor-followups.md) | 低 | 2026-09-30 | worker 観測境界の残り: ready 直後の死亡で code が割れる、細部の未 pin |
| [approval-no-history-passthrough](approval-no-history-passthrough.md) | 低 |  | `plugin/approval.py` の `_validate_kind`/`_validate_strategy… |
| [backtest-scheduler-log-leak](backtest-scheduler-log-leak.md) | 低 |  | backtest 内部 Scheduler の `maintain_reservations failed: no c… |
| [credentials-file-copied-unused](credentials-file-copied-unused.md) | 低 |  | `ClaudeRunner.__init__(credentials_file_copied)` が未使用 |
| [graceful-stop-waits-mission](graceful-stop-waits-mission.md) | 低 |  | graceful stop が走行中 improve mission の timeout (最大 60 分) を待つ … |
| [improvement-runs-backlog-id-on-failure](improvement-runs-backlog-id-on-failure.md) | 低 |  | 失敗 mission では `improvement_runs.backlog_id` が NULL のままで選択 b… |
| [latest-in-sample-metrics-requires-row-factory](latest-in-sample-metrics-requires-row-factory.md) | 低 |  | `latest_in_sample_metrics` は `row_factory=sqlite3.Row` を暗黙に… |
| [loader-double-read-hash](loader-double-read-hash.md) | 低 |  | `_discover_one` が bytes を持ちながら path 版 `content_hash(entry)`… |
| [missions-finished-at-is-logical](missions-finished-at-is-logical.md) | 低 |  | `missions.finished_at` は終端メソッドに渡す `now` で、親ゲート 3 本 (≒70 秒) … |
| [normalize-reason-duplicated](normalize-reason-duplicated.md) | 低 |  | `cli_runner._normalize_reason` が local_runner.py:39-56 の逐語複… |
| [opencode-as-mb-hardcode](opencode-as-mb-hardcode.md) | 低 |  | mission_worker.py:703 の `max(as_mb, 262144)` を settings 化 (… |
| [opencode-node-modules-copytree](opencode-node-modules-copytree.md) | 低 |  | mission 毎に node_modules 63MB/3648 file を copytree。symlink +… |
| [primary-transcript-lost-on-timeout](primary-transcript-lost-on-timeout.md) | 低 |  | timeout→段B 追撃経路で primary の transcript (tool 64 件) が保存されず消える |
| [sandbox-pycache-prefix](sandbox-pycache-prefix.md) | 低 |  | `sandbox._build_env` が PYTHONPYCACHEPREFIX 未設定 → 候補 dir に _… |
| [settings-hash-excludes-improve-gate](settings-hash-excludes-improve-gate.md) | 低 |  | `settings_snapshot_hash` は `risk` + `backtest` のみ → `improv… |

### 優先未設定 (70)

| id | 優先 | 起票 | 概要 |
|---|---|---|---|
| [legacy-2026-08-22-a-1](legacy-2026-08-22-a-1.md) | 未設定 | 2026-08-22 | [2026-08-22 A-1] 起動時検査④ (codex サブスク期限) の変異が段 0 で未注入 |
| [legacy-2026-08-22-a-4](legacy-2026-08-22-a-4.md) | 未設定 | 2026-08-22 | [2026-08-22 A-4]/[T13-5c] protocolVersion 実測 pin (Task 13 裁… |
| [fail-observability](fail-observability.md) | 未設定 | 2026-08-30 | [E2E-observability]/[fail-observability] (同系) 手動発火 improve … |
| [legacy-e2e-diag](legacy-e2e-diag.md) | 未設定 | 2026-08-30 | [E2E-diag] codex_runner の失敗 reason が stderr 最終行のみで、実エラー (st… |
| [legacy-e2e-egress](legacy-e2e-egress.md) | 未設定 | 2026-08-30 | [E2E-egress] provider=llama_swap + `--disable apps` でも code… |
| [legacy-e2e-local-mcp](legacy-e2e-local-mcp.md) | 未設定 | 2026-08-30 | [E2E-local-mcp]/[E2E-qwen3.8→確定 2026-08-30] codex+llama_swa… |
| [legacy-t13-f9](legacy-t13-f9.md) | 未設定 | 2026-08-30 | [T13-F9]/[T13-F9 改訂] codex realbackend プローブ失敗: 真因 = codex 0… |
| [pair-rules-vs-data-availability](pair-rules-vs-data-availability.md) | 未設定 | 2026-09-03 | 。①② は完了 (完了ログ参照)。旧状況: ohlcv_history 空 × Dukascopy 遮断 ([[duk… |
| [gate-admission-helper](gate-admission-helper.md) | 未設定 | 2026-09-06 | submit/bless/improve の 3 経路が gate チェックリストを各自再導出し drift (max… |
| [scheduler-bars-per-tick-memo](scheduler-bars-per-tick-memo.md) | 未設定 | 2026-09-06 | live tick が open 行・pending 行ごとに `bars_fn(pair)` (ネットワーク fet… |
| [jinja-templating-deferred](jinja-templating-deferred.md) | 未設定 | 2026-09-07 | 2026-09-07 相談: プロンプトは `str.format` のまま。jinja 化は改善 mission の… |
| [news-source-route-omitted](news-source-route-omitted.md) | 未設定 | 2026-09-07 | 改善ループ経由のニュースソース追加ルートを廃止。実態: 設計書 §6 にあるだけで未実装 (artifact は pl… |
| [selftest-loop-no-cutoff](selftest-loop-no-cutoff.md) | 未設定 | 2026-09-07 | 。設計 v4 `tmp/design-selftest-cutoff/design.md` (codex 3 周、`c… |
| [indicator-first-seeding](indicator-first-seeding.md) | 未設定 | 2026-09-12 | 初期 indicator plugin セットを人間が用意し (MACD / ボリンジャー / ATR / ADX 等… |
| [pine-script-to-plugin](pine-script-to-plugin.md) | 未設定 | 2026-09-12 | TradingView Pine script を agentic-fx plugin (indicator/stra… |
| [floor-path-skips-duplicate-metrics](floor-path-skips-duplicate-metrics.md) | 未設定 | 2026-09-13 | 質検査はフロア経路に入れない・母集団も広げない。根拠 = (a) はフロア不合格候補が approval 行を作らない… |
| [indicator-initial-set](indicator-initial-set.md) | 未設定 | 2026-09-15 | 標準指標を人間提供で初期導入する — sma / ema (`value`)、rsi Wilder (`rsi`)、m… |
| [indicator-reference-oracle-gate](indicator-reference-oracle-gate.md) | 未設定 | 2026-09-15 | indicator には strategy の backtest / フロアに相当する正しさの門が無い (check_… |
| [multi-timeframe-indicator-deps](multi-timeframe-indicator-deps.md) | 未設定 | 2026-09-15 | 1 strategy が複数時間足の指標を同時に使う (`rsi_1h: {plugin: rsi, timefram… |
| [fake-run-context-consolidation](fake-run-context-consolidation.md) | 未設定 | 2026-09-17 | 起票 2026-09-17 (段 0 束 3 提案 5): `tests/runners/test_worker_ru… |
| [first-run-setup](first-run-setup.md) | 未設定 | 2026-09-17 | 初回起動は対話的な設定を必須にする (LLM 接続先 / トレード足 / 戦略作成のきっかけ / 戦略改善周期 等)。… |
| [backtest-available-lists-over-max-bars](backtest-available-lists-over-max-bars.md) | 未設定 | 2026-09-18 | 起票 2026-09-18 (同 T-4、低): `run_backtest_handler` の `availabl… |
| [indicator-result-wire-validation-unify](indicator-result-wire-validation-unify.md) | 未設定 | 2026-09-18 | 起票 2026-09-18 (iw 2 周目 CR8 見送り): `plugin/sandbox._validate_… |
| [subprocess-allowlist-by-line-number](subprocess-allowlist-by-line-number.md) | 未設定 | 2026-09-18 | 起票 2026-09-18 (同 T-3): `tests/test_subprocess_stdin_policy.… |
| [wire-list-branch-unreachable](wire-list-branch-unreachable.md) | 未設定 | 2026-09-18 | 起票 2026-09-18 (同 T-2): `worker._indicator_result_to_wire` の… |
| [iw-r3-unverified-residuals](iw-r3-unverified-residuals.md) | 未設定 | 2026-09-19 | 起票 2026-09-19 (iw 3 周目で「未検証」のまま残った 3 点、`tmp/review-20260919… |
| [ops-ui](ops-ui.md) | 未設定 | 2026-09-19 | 操作 API (設計書 §7) + Discord 承認 bot + 読み取り専用 web ダッシュボードの束。**配… |
| [retire-symlink-deployed-plugin](retire-symlink-deployed-plugin.md) | 未設定 | 2026-09-19 | 起票 2026-09-19 ([indicator-initial-set] プラン起草時に判明): `afx plu… |
| [improve-catchup-runs-at-startup](improve-catchup-runs-at-startup.md) | 未設定 | 2026-09-20 | 起票 2026-09-20 (観測、要確認): サービス起動の直後に improve mission #85 が自動起… |
| [switch-ops-code-review-r2-altitude](switch-ops-code-review-r2-altitude.md) | 未設定 | 2026-09-20 | 起票 2026-09-20 ([switch-ops-hardening] 2 周目 /code-review #3,… |
| [live-confirmed-bars-into-history](live-confirmed-bars-into-history.md) | 未設定 | 2026-09-21 | ライブで集めた確定足をバックテスト履歴に不変で積み上げ、cache は形成中の最新値だけにする (二重保存の解消、MT… |
| [multi-decision-timeframes](multi-decision-timeframes.md) | 未設定 | 2026-09-21 | 基準 tick を最小足にし、各 tick で確定した足の集合ごとに発注可否を判断、建玉は足ごとの帳簿で管理する。設計… |
| [policy-path-literal-in-four-places](policy-path-literal-in-four-places.md) | 未設定 | 2026-09-21 | `root / "policy" / "directives.md"` が `service.py` 3 箇所 (tr… |
| [abandoned-signal-observability](abandoned-signal-observability.md) | 未設定 | 2026-09-27 | 建玉ゼロの間は signal 起動 mission を起こさない規則 (D2) のため、strategy signal… |
| [improve-add-fullwidth-placeholder](improve-add-fullwidth-placeholder.md) | 未設定 | 2026-09-27 | `improve add` のプレースホルダ判定は ASCII `<…>`/`[…]` のみで全角 `＜案1＞`・`［… |
| [intent-evidence-timeframe-gate](intent-evidence-timeframe-gate.md) | 未設定 | 2026-09-27 | `role` (primary / context / other) は market tool 応答に足の意味を表示… |
| [market-tool-budget-persist-via-alert-state](market-tool-budget-persist-via-alert-state.md) | 未設定 | 2026-09-27 | `context_daily_call_budget` 超過の WARNING は warned latch がプロセ… |
| [realbackend-tests-fail-under-claude-code](realbackend-tests-fail-under-claude-code.md) | 未設定 | 2026-09-27 | `tests/loops/test_verify_backend_realbackend.py` の claude /… |
| [service-secret-env-leaked-matches-redundant](service-secret-env-leaked-matches-redundant.md) | 未設定 | 2026-09-27 | `service.py` 検査⑤の `leaked` list と `matches` dict が常に同期して埋まり… |
| [test-init-offline-unreachable-bridge-flake](test-init-offline-unreachable-bridge-flake.md) | 未設定 | 2026-09-27 | `tests/test_init_and_guard.py::test_init_completes_offline_… |
| [trade-timeout-on-startup](trade-timeout-on-startup.md) | 未設定 | 2026-09-28 | 起動直後の cron trade mission が stop 時に timeout で終わる (m41/m43)。 … |
| [backend-descriptor](backend-descriptor.md) | 未設定 |  | backend 別 if-ladder ×5 (service/factory/worker_runner/missi… |
| [backlog-dup](backlog-dup.md) | 未設定 |  | /[backlog-dedup-miss] (統合) discoveries 由来の backlog #12/#13 … |
| [db-rebuild-helper](db-rebuild-helper.md) | 未設定 |  | store/db.py の table rebuild migration 7 本を parameterised he… |
| [improve-add-quotes](improve-add-quotes.md) | 未設定 |  | cosmetic: `improve add` がクォートを剥がさない (`commands.py:189`)。[sw… |
| [kind-read-duplication](kind-read-duplication.md) | 未設定 |  | `_read_candidate_kind` が `candidate_meta.kind` を使わず config.… |
| [legacy-d-5](legacy-d-5.md) | 未設定 |  | [D-5] afx plugin submit → submit_plugin → _validate_strateg… |
| [legacy-d-stage0-e](legacy-d-stage0-e.md) | 未設定 |  | [D-stage0→束E] switch.py resume 経路が .resolve() 無しで symlink 版… |
| [legacy-e2e-codex-sigtrap](legacy-e2e-codex-sigtrap.md) | 未設定 |  | [E2E-codex-sigtrap] codex+chatgpt の improve worker 環境下で cod… |
| [legacy-e2e-daemon-quiet](legacy-e2e-daemon-quiet.md) | 未設定 |  | [E2E-daemon-quiet] stdin 非 TTY だと `afx` が黙って daemon モードになる … |
| [legacy-e2e-econ-429](legacy-e2e-econ-429.md) | 未設定 |  | [E2E-econ-429] サービス起動毎に econ fetch が走り、再起動連打で HTTP 429 (実測)… |
| [legacy-e2e-model](legacy-e2e-model.md) | 未設定 |  | [E2E-model] `runner.<lane>.model` が backend 非分離 — backend 切… |
| [legacy-e2e-ttl](legacy-e2e-ttl.md) | 未設定 |  | [E2E-ttl] llama-swap TTL=120s と improve mission の相性: アイドル 2… |
| [legacy-l72](legacy-l72.md) | 未設定 |  | [L72] holdout の空期間分岐 (束 C 由来でない既存コードの穴)。tmp/review-bundleC/… |
| [legacy-oc-example-leak](legacy-oc-example-leak.md) | 未設定 |  | [OC-example-leak] プロンプト実例の idea「RSI の期間を 14 から 21 に…」が disc… |
| [legacy-oc-qwen3-8-toolcall](legacy-oc-qwen3-8-toolcall.md) | 未設定 |  | [OC-qwen3.8-toolcall] m20 primary 960 秒の中身 (transcript 初観測)… |
| [legacy-oc-resume-discard](legacy-oc-resume-discard.md) | 未設定 |  | [OC-resume-discard 残] M4 の report 降格経路は未発火 (m28 はモデルが自発的に o… |
| [legacy-oc-tool-not-found](legacy-oc-tool-not-found.md) | 未設定 |  | [OC-tool-not-found] m24/m26 で `read_staging_file`/`read_plu… |
| [legacy-oc-websearch-ssl](legacy-oc-websearch-ssl.md) | 未設定 |  | [OC-websearch-ssl] m26 で `afx_web_search` 10 回中 5 回 `DDGSEx… |
| [legacy-t13-step7](legacy-t13-step7.md) | 未設定 |  | [T13-Step7] 実機 E2E 手順 3 以降 (承認申請生成・unshare・strace egress・継続… |
| [legacy-t9b-go-not-sent-parent-runaway](legacy-t9b-go-not-sent-parent-runaway.md) | 未設定 |  | [起票] t9b 変異「go を送らない」で親がスレッド暴走 (67 本・12 分超、変異下のみ) → 子が進まないと… |
| [mission-output-not-persisted](mission-output-not-persisted.md) | 未設定 |  | missions.output_json NULL / transcript_json `[]` (m34〜m42 全… |
| [mission46-selftest-loop](mission46-selftest-loop.md) | 未設定 |  | 観測: qwen3.8 が self-test 修正に 53 write / 51 run_plugin_tests … |
| [mt5-import-window-before-data-start](mt5-import-window-before-data-start.md) | 未設定 |  | `--from` がデータ開始より前だと bridge が最古バー 1 本を要求窓外で返し importer が fa… |
| [observation-carryover-mismatch](observation-carryover-mismatch.md) | 未設定 |  | observation の申し送り (「staging 書込済・再開可能」) と実態 (`afx_list_stagi… |
| [rpc-call-helper](rpc-call-helper.md) | 未設定 |  | mission_worker の `_RagRpcProxy._call` / `_make_rpc_client.c… |
| [scheduler-fires-on-startup](scheduler-fires-on-startup.md) | 未設定 |  | trade mission は毎時 hh:12 だけでなく afx 起動直後にも発火する — E2E の時間帯回避は「… |
| [seed-priority](seed-priority.md) | 未設定 |  | ユーザー種まき #26 (open) より過去 observation #18 が選ばれ #26 の指示 (5/20 … |
| [selection-rationale-unverified](selection-rationale-unverified.md) | 未設定 |  | selection_rationale が未検証で payload に載る (m40 は attempts を捏造)。… |
| [tier-a-directive-ignored](tier-a-directive-ignored.md) | 未設定 |  | Tier A の repeated_failure directive は 9 回連続で無視された。Tier A には… |

