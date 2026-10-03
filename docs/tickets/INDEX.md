# チケット一覧

自動生成: `ticket.py index` (tickets skill)。手で編集しない。
未完了: 128 件。完了分は CLOSED.md。

## 実装待ち (16)

| id | 優先 | 起票 | 概要 |
|---|---|---|---|
| [legacy-e2e-diag](legacy-e2e-diag.md) | 中 | 2026-08-30 | codex 失敗の理由が stderr 最終行だけで、本当のエラーが読めない |
| [scheduler-bars-per-tick-memo](scheduler-bars-per-tick-memo.md) | 中 | 2026-09-06 | 複数ペア化すると tick ごとの価格取得が注文数に比例して増える |
| [legacy-2026-08-22-a-4](legacy-2026-08-22-a-4.md) | 低 | 2026-08-22 | codex の MCP protocolVersion 実測値が allowlist に未登録 |
| [legacy-2026-08-23-c-0](legacy-2026-08-23-c-0.md) | 低 | 2026-08-23 | datafeed テスト 11 件が実 TCP 接続を試みる疑い |
| [backtest-available-lists-over-max-bars](backtest-available-lists-over-max-bars.md) | 低 | 2026-09-18 | bars 上限で拒否した指標が代替候補として返る |
| [indicator-result-wire-validation-unify](indicator-result-wire-validation-unify.md) | 低 | 2026-09-18 | 指標結果の検査が 2 箇所に重複している |
| [subprocess-allowlist-by-line-number](subprocess-allowlist-by-line-number.md) | 低 | 2026-09-18 | subprocess の stdin 方針テストが行番号固定で壊れやすい |
| [flake-trade-claude-real-process](flake-trade-claude-real-process.md) | 低 | 2026-09-20 | 全スイート負荷時に claude 実プロセスのテストが稀に落ちる |
| [switch-ops-r2b-low-unpinned](switch-ops-r2b-low-unpinned.md) | 低 | 2026-09-20 | 版ディレクトリ検証の変異 3 本が未 pin、テスト名に旧版の名残 |
| [improve-add-fullwidth-placeholder](improve-add-fullwidth-placeholder.md) | 低 | 2026-09-27 | improve add で全角プレースホルダが警告されない |
| [flake](flake.md) | 低 |  | 全スイート負荷時に対話停止テストが落ちる (タイミング依存) |
| [flake-rpc-timeout-by-kind](flake-rpc-timeout-by-kind.md) | 低 |  | 種別 timeout テストが実時間依存で負荷下に落ち得る |
| [flake-shell-interrupt-timing](flake-shell-interrupt-timing.md) | 低 |  | 別 pytest 並走で shell 割り込みテストが落ちる |
| [flaky-tool-schemas](flaky-tool-schemas.md) | 低 |  | 全ツールのスキーマテストがフル suite でだけ ERROR になる |
| [improve-add-quotes](improve-add-quotes.md) | 低 |  | improve add がクォートを剥がさず表示が崩れる |
| [legacy-oc-tool-not-found](legacy-oc-tool-not-found.md) | 低 |  | 存在しない候補名を読むときの誘導が足りない |

## 裁定待ち (7)

| id | 優先 | 起票 | 概要 |
|---|---|---|---|
| [candidate-strategy-backtest-runs-without-landlock](candidate-strategy-backtest-runs-without-landlock.md) | 高 | 2026-10-03 | 候補 strategy の backtest が Landlock の無い plugin worker で動く |
| [legacy-2026-08-23-e-11-m3-m7](legacy-2026-08-23-e-11-m3-m7.md) | 低 | 2026-08-23 | apply_decision のキー方向と crash point A の縮退が未裁定 |
| [improve-catchup-runs-at-startup](improve-catchup-runs-at-startup.md) | 低 | 2026-09-20 | 起動直後に改善 mission が自動起動する (catch-up と推定) |
| [gate-noop-followup](gate-noop-followup.md) | 低 |  | fact 行が選択され得る件は kind 必須化の追随が未観測 |
| [legacy-e2e-ttl](legacy-e2e-ttl.md) | 低 |  | llama-swap の TTL 120 秒で mission 中にモデルが unload される |
| [mt5-import-window-before-data-start](mt5-import-window-before-data-start.md) | 低 |  | --from がデータ開始より前だと importer が停止する |
| [seed-priority](seed-priority.md) | 低 |  | ユーザーの種まきより古い observation が選ばれ指示が無視された |

## 設計待ち (105)

| id | 優先 | 起票 | 概要 |
|---|---|---|---|
| [backtest-worker-cpu-budget-shrinks-with-timeframe](backtest-worker-cpu-budget-shrinks-with-timeframe.md) | 高 | 2026-09-20 | strategy の backtest は plugin worker 1 プロセスを再生全体で使い回すので、`plu… |
| [candidate-pyc-overrides-inspected-source](candidate-pyc-overrides-inspected-source.md) | 高 | 2026-10-03 | 候補の偽 pyc が検査済み plugin.py の代わりに実行される |
| [gate-pytest-dev-writable-and-ldso-exec](gate-pytest-dev-writable-and-ldso-exec.md) | 高 | 2026-10-03 | gate の pytest は /dev 全体が書き込み可で ld.so 経由で任意の ELF を起動できる |
| [plugin-worker-can-prlimit-other-processes](plugin-worker-can-prlimit-other-processes.md) | 高 | 2026-10-03 | plugin worker から同 uid の他プロセスの rlimit を書き換えられる |
| [sandbox-escape-via-user-daemons-unmeasured](sandbox-escape-via-user-daemons-unmeasured.md) | 高 | 2026-10-03 | sandbox から他デーモン (D-Bus / screen) 経由で抜けられるかが未実測 |
| [backtest-dedup-cache](backtest-dedup-cache.md) | 中 | 2026-09-08 | 同一 (候補, config) の backtest 再実行で予算を消費しない (run6: 6 枠中 3 枠がビット… |
| [baseline-replay-unimplemented](baseline-replay-unimplemented.md) | 中 | 2026-09-12 | 設計書 2026-08-16 §4.2-4「(pair, timeframe) ごとに現在 live の D4-app… |
| [candidates-converge-to-example-sma](candidates-converge-to-example-sma.md) | 中 | 2026-09-12 | codex #74 / ornith #75 / muse #76 の提出候補は content_hash が異なるだ… |
| [human-corridor-gate-rows-null-outcome](human-corridor-gate-rows-null-outcome.md) | 中 | 2026-09-12 | 人間回廊 (`submit_candidate` / `bless_candidate` → `_run_full_g… |
| [improve-targeted-run](improve-targeted-run.md) | 中 | 2026-09-12 | 現状は `improve add <text>` + 引数なし `improve` で LLM が open から自分… |
| [legacy-submit-corridor-bypasses-gate](legacy-submit-corridor-bypasses-gate.md) | 中 | 2026-09-12 | `afx plugin submit <name>` (live plugins/ の既存 plugin) は `ap… |
| [first-run-setup](first-run-setup.md) | 中 | 2026-09-17 | 初回起動の対話設定と service 設置が無い |
| [retire-symlink-deployed-plugin](retire-symlink-deployed-plugin.md) | 中 | 2026-09-19 | afx plugin retire が symlink 配備の指標を退役できない |
| [policy-add-unwired-in-service](policy-add-unwired-in-service.md) | 中 | 2026-09-20 | `service.py:1054` の `Commands(...)` に `policy_path` を渡していない… |
| [secret-env-guard-false-positive](secret-env-guard-false-positive.md) | 中 | 2026-09-20 | 改善 backend を CLI 系 (claude / codex) にすると、起動時の検査⑤ (`service.… |
| [backtest-rpc-timeout-does-not-stop-parent-work](backtest-rpc-timeout-does-not-stop-parent-work.md) | 中 | 2026-09-21 | backtest の待ち時間超過後も親の処理と worker が走り続ける |
| [db-healthcheck-continuous-session-freshness](db-healthcheck-continuous-session-freshness.md) | 中 | 2026-09-28 | `service.py:1243` `db_healthcheck` の鮮度式 `bar start + 2×幅 + … |
| [live-storage-source-mapping-scattered](live-storage-source-mapping-scattered.md) | 中 | 2026-09-28 | primary → ライブ保存名 (`mt5` → `mt5-live`) の写像が 8 箇所に散在 (service… |
| [signal-producer-catchup-requires-latest-tail](signal-producer-catchup-requires-latest-tail.md) | 中 | 2026-09-28 | producer は過去 bucket を順に評価 (`signal_producer.py:195`) しながら、毎… |
| [trade-cron-hybrid-mode](trade-cron-hybrid-mode.md) | 中 | 2026-09-28 | 取引判断 LLM の起動をハイブリッドにする — 建玉・未約定指値が**ある**間だけ判断足ごとの cron miss… |
| [trade-timeout-on-startup](trade-timeout-on-startup.md) | 中 | 2026-09-28 | 停止による打ち切りの trade mission が timeout と記録される |
| [sandbox-worker-response-unvalidated](sandbox-worker-response-unvalidated.md) | 中 | 2026-09-30 | worker 応答の pid と error 文字列が無検証で session とログに入る |
| [example-eval-source-defaults-to-dukascopy](example-eval-source-defaults-to-dukascopy.md) | 中 | 2026-10-02 | 設定例の履歴源の既定が dukascopy で大量取得を促す |
| [full-suite-fd-over-1024-breaks-shell-tests](full-suite-fd-over-1024-breaks-shell-tests.md) | 中 | 2026-10-02 | 全体テストで fd が 1024 を超え shell 系 10 件と init 1 件が落ちる |
| [gate-pytest-tail-stored-in-last-result](gate-pytest-tail-stored-in-last-result.md) | 中 | 2026-10-02 | gate の pytest 出力末尾が次回の改善 mission の材料になる |
| [improve-handler-unexpected-exception-text-reaches-agent](improve-handler-unexpected-exception-text-reaches-agent.md) | 中 | 2026-10-02 | 改善 mission の handler で想定外例外の文面が agent に届く |
| [load-settings-loads-dotenv-as-side-effect](load-settings-loads-dotenv-as-side-effect.md) | 中 | 2026-10-02 | 設定の検証が .env を環境に読み込む副作用を持つ |
| [news-fetch-no-user-agent-yahoo-429](news-fetch-no-user-agent-yahoo-429.md) | 中 | 2026-10-02 | ニュース取得が User-Agent を付けず Yahoo が毎回 429 を返す |
| [plugin-ast-check-bypass-via-library-attributes](plugin-ast-check-bypass-via-library-attributes.md) | 中 | 2026-10-02 | plugin がライブラリの属性経由で os に届き、コード検査を回避できる |
| [repo-readme-empty](repo-readme-empty.md) | 中 | 2026-10-02 | README.md が 0 バイト |
| [research-user-agent-claims-unverified-url](research-user-agent-claims-unverified-url.md) | 中 | 2026-10-02 | 研究用取得の User-Agent が実在未確認の URL を名乗る |
| [settings-yaml-duplicate-key-silently-last-wins](settings-yaml-duplicate-key-silently-last-wins.md) | 中 | 2026-10-02 | settings.yaml の重複キーは黙って後の値が効く |
| [backlog-status-update-is-two-step-and-races-selection](backlog-status-update-is-two-step-and-races-selection.md) | 中 | 2026-10-03 | backlog reject/reopen/note が SELECT 後の更新で選択と競合する |
| [manual-improve-runs-synchronously-on-caller-thread](manual-improve-runs-synchronously-on-caller-thread.md) | 中 | 2026-10-03 | 手動 improve が呼び出し元のスレッドで mission 全体を同期実行する |
| [plugin-worker-startup-failure-unreadable](plugin-worker-startup-failure-unreadable.md) | 中 | 2026-10-03 | plugin worker の起動失敗の理由が読めない |
| [harness-failure-becomes-fact](harness-failure-becomes-fact.md) | 中 |  | harness 由来の失敗 (timeout 等) をモデルが「環境制約 fact」として note 化し続ける (#… |
| [live-signal-eval-blocks-protection-tick](live-signal-eval-blocks-protection-tick.md) | 中 |  | 失敗する plugin の signal 計算が毎 tick 10〜30 秒 lock を保持する |
| [refused-errors-double-count](refused-errors-double-count.md) | 中 |  | 予算拒否は terminal streak と tool_error streak の両方に入り `refused=1… |
| [tier-a-directive-ignored](tier-a-directive-ignored.md) | 中 |  | 自己テストの連続失敗の警告が無視され続け、止める仕組みが無い |
| [fail-observability](fail-observability.md) | 低 | 2026-08-30 | 改善 mission 失敗の死因が記録されない問題は是正済 |
| [legacy-e2e-egress](legacy-e2e-egress.md) | 低 | 2026-08-30 | codex 子プロセスが apps 無効でも chatgpt.com に接続する |
| [legacy-t13-f9](legacy-t13-f9.md) | 低 | 2026-08-30 | codex プローブ失敗の残件 (chatgpt 経路の確認と bin 設定) |
| [pair-rules-vs-data-availability](pair-rules-vs-data-availability.md) | 低 | 2026-09-03 | データの無い通貨ペアを agent に見せてしまう余地がある |
| [gate-admission-helper](gate-admission-helper.md) | 低 | 2026-09-06 | gate 検査が 3 経路で重複し drift する |
| [jinja-templating-deferred](jinja-templating-deferred.md) | 低 | 2026-09-07 | 改善プロンプトの {{ }} 脱出が読みにくい (jinja 化は保留) |
| [selftest-loop-no-cutoff](selftest-loop-no-cutoff.md) | 低 | 2026-09-07 | self-test の無限修正ループに打ち切りが無い問題は是正済 |
| [pine-script-to-plugin](pine-script-to-plugin.md) | 低 | 2026-09-12 | Pine script を plugin に変換する手段が無い |
| [indicator-reference-oracle-gate](indicator-reference-oracle-gate.md) | 低 | 2026-09-15 | indicator の正しさを確かめる門が無い |
| [multi-timeframe-indicator-deps](multi-timeframe-indicator-deps.md) | 低 | 2026-09-15 | 1 つの strategy が複数時間足の指標を同時に使えない |
| [fake-run-context-consolidation](fake-run-context-consolidation.md) | 低 | 2026-09-17 | テストのフェイク RunContext が散在し追随漏れで壊れやすい |
| [iw-r3-unverified-residuals](iw-r3-unverified-residuals.md) | 低 | 2026-09-19 | iw 3 周目で未検証のまま残った 3 点 |
| [ops-ui](ops-ui.md) | 低 | 2026-09-19 | 承認 bot と読み取り専用 web ダッシュボードが無い |
| [reject-revert-without-live-classification](reject-revert-without-live-classification.md) | 低 | 2026-09-20 | `reject_candidate` (`switch.py:1866-1875`) は自分の未完 journal が… |
| [switch-ops-code-review-r2-altitude](switch-ops-code-review-r2-altitude.md) | 低 | 2026-09-20 | switch ops の重複コードや型の緩さの整理 |
| [improve-add-tokenizer-collapses-whitespace](improve-add-tokenizer-collapses-whitespace.md) | 低 | 2026-09-21 | シェルの tokenizer (`line.strip().split()` → `" ".join`) が全角空白・… |
| [live-confirmed-bars-into-history](live-confirmed-bars-into-history.md) | 低 | 2026-09-21 | ライブで集めた確定足を履歴に積めず二重保存になる |
| [multi-decision-timeframes](multi-decision-timeframes.md) | 低 | 2026-09-21 | 判断足を複数持つ構想 (将来拡張) |
| [abandoned-signal-observability](abandoned-signal-observability.md) | 低 | 2026-09-27 | 建玉ゼロ中の signal が abandoned で終わる件の観測 |
| [intent-evidence-timeframe-gate](intent-evidence-timeframe-gate.md) | 低 | 2026-09-27 | 判断足と無関係な足を根拠にした OPEN を gate が弾かない |
| [market-tool-budget-persist-via-alert-state](market-tool-budget-persist-via-alert-state.md) | 低 | 2026-09-27 | 市場ツール予算超過の警告が再起動で重複する |
| [realbackend-tests-fail-under-claude-code](realbackend-tests-fail-under-claude-code.md) | 低 | 2026-09-27 | Claude Code の Bash から実 backend テスト 2 本が handshake 失敗 |
| [test-init-offline-unreachable-bridge-flake](test-init-offline-unreachable-bridge-flake.md) | 低 | 2026-09-27 | init の offline テストがフル suite の負荷下で落ちる |
| [trade-prompt-says-hourly](trade-prompt-says-hourly.md) | 低 | 2026-09-28 | 取引判断 prompt の冒頭「1 時間毎に呼び出され」が固定文言のまま (判断足 15m では 15 分毎)。`de… |
| [outage-observe-closed-guard-unpinned](outage-observe-closed-guard-unpinned.md) | 低 | 2026-09-29 | `OutageStateMachine.observe` の「閉場中は観測しない」early return を落として… |
| [signal-producer-hourly-warning-before-confirmation](signal-producer-hourly-warning-before-confirmation.md) | 低 | 2026-09-29 | producer が毎時 xx:00:07 の tick で「1h bucket not yet present」を … |
| [sandbox-lifecycle-minor-followups](sandbox-lifecycle-minor-followups.md) | 低 | 2026-09-30 | worker 観測境界の残り: ready 直後の死亡で code が割れる、細部の未 pin |
| [backtest-aggregates-partial-minute-bars](backtest-aggregates-partial-minute-bars.md) | 低 | 2026-10-01 | backtest と resample が欠けた 1m 足をある分だけ集約し、部分的な足を作る |
| [plugin-sees-live-spread](plugin-sees-live-spread.md) | 低 | 2026-10-01 | plugin と判断 mission が実 spread を見られるようにする |
| [risk-gate-rr-uses-configured-spread](risk-gate-rr-uses-configured-spread.md) | 低 | 2026-10-01 | risk gate の RR 計算が設定値の spread を使い、実 spread と乖離する |
| [first-run-empty-1m-starts-degraded](first-run-empty-1m-starts-degraded.md) | 低 | 2026-10-02 | 初期化直後に 1m 足が空だと degraded から始まる (実機で確認) |
| [outage-leftover-transaction-partial-commit](outage-leftover-transaction-partial-commit.md) | 低 | 2026-10-02 | 停止判定の rollback 失敗で残った中途の書き込みが、次の足の保存で確定し得る |
| [restricted-state-followups](restricted-state-followups.md) | 低 | 2026-10-02 | restricted 状態の残り: 欠落の記録、入力契約のテスト、結合検証 |
| [runner-trade-backend-error-wording-mismatch](runner-trade-backend-error-wording-mismatch.md) | 低 | 2026-10-02 | runner.trade の backend の検証文言が受け付け方と食い違う |
| [tracked-files-contain-author-home-path](tracked-files-contain-author-home-path.md) | 低 | 2026-10-02 | 追跡ファイルに作者のホームの絶対パスが残る |
| [rlimit-nproc-per-uid-does-not-bound-worker](rlimit-nproc-per-uid-does-not-bound-worker.md) | 低 | 2026-10-03 | RLIMIT_NPROC は uid 全体の値で worker の子プロセス数を縛れない |
| [supervisor-cleanup-test-leaks-36-fds](supervisor-cleanup-test-leaks-36-fds.md) | 低 | 2026-10-03 | worker supervisor の cleanup テストが 1 回で fd を 36 本増やす |
| [approval-no-history-passthrough](approval-no-history-passthrough.md) | 低 |  | `plugin/approval.py` の `_validate_kind`/`_validate_strategy… |
| [backend-descriptor](backend-descriptor.md) | 低 |  | backend 別の if 分岐が 5 ファイルに散在 |
| [backlog-dup](backlog-dup.md) | 低 |  | backlog に実質同じ案が重複して溜まる |
| [backtest-scheduler-log-leak](backtest-scheduler-log-leak.md) | 低 |  | backtest 内部 Scheduler の `maintain_reservations failed: no c… |
| [credentials-file-copied-unused](credentials-file-copied-unused.md) | 低 |  | `ClaudeRunner.__init__(credentials_file_copied)` が未使用 |
| [db-rebuild-helper](db-rebuild-helper.md) | 低 |  | DB の table rebuild migration が 7 本重複している |
| [graceful-stop-waits-mission](graceful-stop-waits-mission.md) | 低 |  | graceful stop が走行中 improve mission の timeout (最大 60 分) を待つ … |
| [improvement-runs-backlog-id-on-failure](improvement-runs-backlog-id-on-failure.md) | 低 |  | 失敗 mission では `improvement_runs.backlog_id` が NULL のままで選択 b… |
| [latest-in-sample-metrics-requires-row-factory](latest-in-sample-metrics-requires-row-factory.md) | 低 |  | `latest_in_sample_metrics` は `row_factory=sqlite3.Row` を暗黙に… |
| [legacy-d-stage0-e](legacy-d-stage0-e.md) | 低 |  | switch の resume 経路が symlink 版ディレクトリを resolve せず受理 |
| [legacy-e2e-model](legacy-e2e-model.md) | 低 |  | backend を切り替えるたびにモデル名の手編集が要る |
| [legacy-l72](legacy-l72.md) | 低 |  | holdout の空期間分岐に穴があるとの記録 |
| [legacy-oc-qwen3-8-toolcall](legacy-oc-qwen3-8-toolcall.md) | 低 |  | モデルが壊れた引数でツールを連打し予算を焼く |
| [legacy-oc-resume-discard](legacy-oc-resume-discard.md) | 低 |  | resume 時の report 降格経路が未発火で未検証 |
| [legacy-oc-websearch-ssl](legacy-oc-websearch-ssl.md) | 低 |  | web 検索が間欠的に SSL 検証失敗する |
| [legacy-t13-step7](legacy-t13-step7.md) | 低 |  | Task 13 の実機 E2E 手順 3 以降が未実施 |
| [legacy-t9b-go-not-sent-parent-runaway](legacy-t9b-go-not-sent-parent-runaway.md) | 低 |  | go が送られない変異で親がスレッド暴走する |
| [loader-double-read-hash](loader-double-read-hash.md) | 低 |  | `_discover_one` が bytes を持ちながら path 版 `content_hash(entry)`… |
| [mission-output-not-persisted](mission-output-not-persisted.md) | 低 |  | mission の出力と transcript が DB に残らない疑い |
| [missions-finished-at-is-logical](missions-finished-at-is-logical.md) | 低 |  | `missions.finished_at` は終端メソッドに渡す `now` で、親ゲート 3 本 (≒70 秒) … |
| [normalize-reason-duplicated](normalize-reason-duplicated.md) | 低 |  | `cli_runner._normalize_reason` が local_runner.py:39-56 の逐語複… |
| [observation-carryover-mismatch](observation-carryover-mismatch.md) | 低 |  | observation の申し送りと staging の実態が食い違う |
| [opencode-as-mb-hardcode](opencode-as-mb-hardcode.md) | 低 |  | mission_worker.py:703 の `max(as_mb, 262144)` を settings 化 (… |
| [opencode-node-modules-copytree](opencode-node-modules-copytree.md) | 低 |  | mission 毎に node_modules 63MB/3648 file を copytree。symlink +… |
| [primary-transcript-lost-on-timeout](primary-transcript-lost-on-timeout.md) | 低 |  | timeout→段B 追撃経路で primary の transcript (tool 64 件) が保存されず消える |
| [rpc-call-helper](rpc-call-helper.md) | 低 |  | RPC の framing が 2 箇所で重複実装されている |
| [sandbox-pycache-prefix](sandbox-pycache-prefix.md) | 低 |  | `sandbox._build_env` が PYTHONPYCACHEPREFIX 未設定 → 候補 dir に _… |
| [settings-hash-excludes-improve-gate](settings-hash-excludes-improve-gate.md) | 低 |  | `settings_snapshot_hash` は `risk` + `backtest` のみ → `improv… |
| [test-isolation](test-isolation.md) | 低 |  | 特定 4 ファイルの部分実行で loops の fixture が見えなくなる |

