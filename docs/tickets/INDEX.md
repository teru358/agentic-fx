# チケット一覧

自動生成: `ticket.py index` (tickets skill)。手で編集しない。
未完了: 151 件。完了分は closed/。

## 実装中 (3)

| id | 優先 | 起票 | 概要 |
|---|---|---|---|
| [sandbox-escape-via-user-daemons-unmeasured](open/sandbox-escape-via-user-daemons-unmeasured.md) | 高 | 2026-10-03 | 改善 agent の檻からの脱出 (D-Bus / screen 経由) を測らずに塞ぐ |
| [improve-objective-filtered-signal-quality](open/improve-objective-filtered-signal-quality.md) | 中 | 2026-10-07 | 改善ループの目標を「選別可能な勝ち筋シグナル」に寄せる (PF ≥ 1.0 を床に) |
| [improve-backlog-self-feedback-duplicates-work](open/improve-backlog-self-feedback-duplicates-work.md) | 中 | 2026-10-10 | 改善 mission が自分の成果報告を backlog に書き戻し、次回に同じ戦略を再実装する |

## 実装待ち (13)

| id | 優先 | 起票 | 概要 |
|---|---|---|---|
| [legacy-e2e-diag](open/legacy-e2e-diag.md) | 中 | 2026-08-30 | codex 失敗の理由が stderr 最終行だけで、本当のエラーが読めない |
| [scheduler-bars-per-tick-memo](open/scheduler-bars-per-tick-memo.md) | 中 | 2026-09-06 | 複数ペア化すると tick ごとの価格取得が注文数に比例して増える |
| [bot-event-notices-are-raw-log-lines](open/bot-event-notices-are-raw-log-lines.md) | 中 | 2026-10-17 | bot の通知が event の k=v 行そのままで人が読めない |
| [legacy-2026-08-22-a-4](open/legacy-2026-08-22-a-4.md) | 低 | 2026-08-22 | codex の MCP protocolVersion 実測値が allowlist に未登録 |
| [legacy-2026-08-23-c-0](open/legacy-2026-08-23-c-0.md) | 低 | 2026-08-23 | datafeed テスト 11 件が実 TCP 接続を試みる疑い |
| [backtest-available-lists-over-max-bars](open/backtest-available-lists-over-max-bars.md) | 低 | 2026-09-18 | bars 上限で拒否した指標が代替候補として返る |
| [indicator-result-wire-validation-unify](open/indicator-result-wire-validation-unify.md) | 低 | 2026-09-18 | 指標結果の検査が 2 箇所に重複している |
| [subprocess-allowlist-by-line-number](open/subprocess-allowlist-by-line-number.md) | 低 | 2026-09-18 | subprocess の stdin 方針テストが行番号固定で壊れやすい |
| [switch-ops-r2b-low-unpinned](open/switch-ops-r2b-low-unpinned.md) | 低 | 2026-09-20 | 版ディレクトリ検証の変異 3 本が未 pin、テスト名に旧版の名残 |
| [improve-add-fullwidth-placeholder](open/improve-add-fullwidth-placeholder.md) | 低 | 2026-09-27 | improve add で全角プレースホルダが警告されない |
| [flaky-tool-schemas](open/flaky-tool-schemas.md) | 低 |  | 全ツールのスキーマテストがフル suite でだけ ERROR になる |
| [improve-add-quotes](open/improve-add-quotes.md) | 低 |  | improve add がクォートを剥がさず表示が崩れる |
| [legacy-oc-tool-not-found](open/legacy-oc-tool-not-found.md) | 低 |  | 存在しない候補名を読むときの誘導が足りない |

## 裁定待ち (8)

| id | 優先 | 起票 | 概要 |
|---|---|---|---|
| [escape-probe-manual-measurement](open/escape-probe-manual-measurement.md) | 中 | 2026-10-04 | 改善 agent の檻からの脱出可否を人手で実測する |
| [legacy-2026-08-23-e-11-m3-m7](open/legacy-2026-08-23-e-11-m3-m7.md) | 低 | 2026-08-23 | apply_decision のキー方向と crash point A の縮退が未裁定 |
| [improve-catchup-runs-at-startup](open/improve-catchup-runs-at-startup.md) | 低 | 2026-09-20 | 起動直後に改善 mission が自動起動する (catch-up と推定) |
| [gate-noop-followup](open/gate-noop-followup.md) | 低 |  | fact 行が選択され得る件は kind 必須化の追随が未観測 |
| [legacy-e2e-ttl](open/legacy-e2e-ttl.md) | 低 |  | llama-swap の TTL 120 秒で mission 中にモデルが unload される |
| [mt5-import-window-before-data-start](open/mt5-import-window-before-data-start.md) | 低 |  | --from がデータ開始より前だと importer が停止する |
| [seed-priority](open/seed-priority.md) | 低 |  | ユーザーの種まきより古い observation が選ばれ指示が無視された |
| [archive-index-naming](open/archive-index-naming.md) | 未設定 | 2026-09-12 | `plugins/_archive/INDEX.md` は approval 終端を載せず GC も更新しないので目録… |

## 設計待ち (127)

| id | 優先 | 起票 | 概要 |
|---|---|---|---|
| [backtest-worker-cpu-budget-shrinks-with-timeframe](open/backtest-worker-cpu-budget-shrinks-with-timeframe.md) | 高 | 2026-09-20 | strategy の backtest は plugin worker 1 プロセスを再生全体で使い回すので、`plu… |
| [gate-pytest-dev-writable-and-ldso-exec](open/gate-pytest-dev-writable-and-ldso-exec.md) | 高 | 2026-10-03 | gate の pytest は /dev 全体が書き込み可で ld.so 経由で任意の ELF を起動できる |
| [backtest-dedup-cache](open/backtest-dedup-cache.md) | 中 | 2026-09-08 | 同一 (候補, config) の backtest 再実行で予算を消費しない (run6: 6 枠中 3 枠がビット… |
| [baseline-replay-unimplemented](open/baseline-replay-unimplemented.md) | 中 | 2026-09-12 | 設計書 2026-08-16 §4.2-4「(pair, timeframe) ごとに現在 live の D4-app… |
| [candidates-converge-to-example-sma](open/candidates-converge-to-example-sma.md) | 中 | 2026-09-12 | codex #74 / ornith #75 / muse #76 の提出候補は content_hash が異なるだ… |
| [human-corridor-gate-rows-null-outcome](open/human-corridor-gate-rows-null-outcome.md) | 中 | 2026-09-12 | 人間回廊 (`submit_candidate` / `bless_candidate` → `_run_full_g… |
| [improve-targeted-run](open/improve-targeted-run.md) | 中 | 2026-09-12 | 現状は `improve add <text>` + 引数なし `improve` で LLM が open から自分… |
| [backtest-rpc-timeout-does-not-stop-parent-work](open/backtest-rpc-timeout-does-not-stop-parent-work.md) | 中 | 2026-09-21 | backtest の待ち時間超過後も親の処理と worker が走り続ける |
| [db-healthcheck-continuous-session-freshness](open/db-healthcheck-continuous-session-freshness.md) | 中 | 2026-09-28 | `service.py:1243` `db_healthcheck` の鮮度式 `bar start + 2×幅 + … |
| [live-storage-source-mapping-scattered](open/live-storage-source-mapping-scattered.md) | 中 | 2026-09-28 | primary → ライブ保存名 (`mt5` → `mt5-live`) の写像が 8 箇所に散在 (service… |
| [signal-producer-catchup-requires-latest-tail](open/signal-producer-catchup-requires-latest-tail.md) | 中 | 2026-09-28 | producer は過去 bucket を順に評価 (`signal_producer.py:195`) しながら、毎… |
| [trade-cron-hybrid-mode](open/trade-cron-hybrid-mode.md) | 中 | 2026-09-28 | 取引判断 LLM の起動をハイブリッドにする — 建玉・未約定指値が**ある**間だけ判断足ごとの cron miss… |
| [trade-timeout-on-startup](open/trade-timeout-on-startup.md) | 中 | 2026-09-28 | 停止による打ち切りの trade mission が timeout と記録される |
| [full-suite-fd-over-1024-breaks-shell-tests](open/full-suite-fd-over-1024-breaks-shell-tests.md) | 中 | 2026-10-02 | 全体テストで fd が 1024 を超え shell 系 10 件と init 1 件が落ちる |
| [gate-pytest-tail-stored-in-last-result](open/gate-pytest-tail-stored-in-last-result.md) | 中 | 2026-10-02 | gate の pytest 出力末尾が次回の改善 mission の材料になる |
| [improve-handler-unexpected-exception-text-reaches-agent](open/improve-handler-unexpected-exception-text-reaches-agent.md) | 中 | 2026-10-02 | 改善 mission の handler で想定外例外の文面が agent に届く |
| [research-user-agent-claims-unverified-url](open/research-user-agent-claims-unverified-url.md) | 中 | 2026-10-02 | 研究用取得の User-Agent が実在未確認の URL を名乗る |
| [settings-yaml-duplicate-key-silently-last-wins](open/settings-yaml-duplicate-key-silently-last-wins.md) | 中 | 2026-10-02 | settings.yaml の重複キーは黙って後の値が効く |
| [backlog-status-update-is-two-step-and-races-selection](open/backlog-status-update-is-two-step-and-races-selection.md) | 中 | 2026-10-03 | backlog reject/reopen/note が SELECT 後の更新で選択と競合する |
| [improve-daily-params-weekly-algorithm](open/improve-daily-params-weekly-algorithm.md) | 中 | 2026-10-03 | 改善を日次のパラメータ調整と週次のアルゴリズム改善に分ける |
| [manual-improve-runs-synchronously-on-caller-thread](open/manual-improve-runs-synchronously-on-caller-thread.md) | 中 | 2026-10-03 | 手動 improve が呼び出し元のスレッドで mission 全体を同期実行する |
| [reference-symbols-as-indicator-inputs](open/reference-symbols-as-indicator-inputs.md) | 中 | 2026-10-08 | 戦略 plugin に株価指数など参照 symbol の値を指標として渡す |
| [signal-producer-evaluates-before-bar-arrives](open/signal-producer-evaluates-before-bar-arrives.md) | 中 | 2026-10-10 | signal_producer が 1h 足の確定前に評価して毎時 WARNING を出す |
| [full-suite-from-repo-root-hangs-with-live-data](open/full-suite-from-repo-root-hangs-with-live-data.md) | 中 | 2026-10-11 | 全 suite (約 8.8k 本) を 1 プロセスで回すと約 6,800 本目で固まる |
| [trade-backend-opencode-single-local-path](open/trade-backend-opencode-single-local-path.md) | 中 | 2026-10-14 | 取引判断にも opencode backend を許し、完全ローカル運用を opencode 1 本に |
| [wizard-stage4-menu-per-role](open/wizard-stage4-menu-per-role.md) | 中 | 2026-10-14 | 段 4 の LLM 構成メニューを役割ごとの 2 問に揃える |
| [bot-readonly-slash-commands](open/bot-readonly-slash-commands.md) | 中 | 2026-10-15 | Discord bot に読み取り専用の slash command を足す |
| [cli-surface-principle-and-reorg](open/cli-surface-principle-and-reorg.md) | 中 | 2026-10-15 | afx / afx ctl の置き場の原則を定義し、コマンド面を整理する |
| [news-source-cli-and-429-backoff](open/news-source-cli-and-429-backoff.md) | 中 | 2026-10-15 | afx news (list/add/enable/disable/test) と 429 連続時のバックオフ |
| [strategy-attribution-and-shadow-evaluation](open/strategy-attribution-and-shadow-evaluation.md) | 中 | 2026-10-16 | 戦略ごとの帰属と影の評価 (共有建玉のまま戦略別の成績を出す) |
| [first-start-check-times-out-on-model-download](open/first-start-check-times-out-on-model-download.md) | 中 | 2026-10-17 | 初回起動の確認が埋め込みモデル取得中に timeout して失敗表示になる |
| [deploy-cli-config-error-traceback](open/deploy-cli-config-error-traceback.md) | 中 | 2026-10-18 | afx の subcommand は設定不在・不正 YAML で traceback を出す |
| [improve-mission-output-not-persisted](open/improve-mission-output-not-persisted.md) | 中 | 2026-10-18 | 改善 mission の出力 (output_json / transcript) が DB に残らない |
| [ops-bot-exits-not-lock-holder](open/ops-bot-exits-not-lock-holder.md) | 中 | 2026-10-22 | afx-ops bot が稼働中に not_lock_holder で終了し systemd が再起動する |
| [harness-failure-becomes-fact](open/harness-failure-becomes-fact.md) | 中 |  | harness 由来の失敗 (timeout 等) をモデルが「環境制約 fact」として note 化し続ける (#… |
| [refused-errors-double-count](open/refused-errors-double-count.md) | 中 |  | 予算拒否は terminal streak と tool_error streak の両方に入り `refused=1… |
| [tier-a-directive-ignored](open/tier-a-directive-ignored.md) | 中 |  | 自己テストの連続失敗の警告が無視され続け、止める仕組みが無い |
| [fail-observability](open/fail-observability.md) | 低 | 2026-08-30 | 改善 mission 失敗の死因が記録されない問題は是正済 |
| [legacy-e2e-egress](open/legacy-e2e-egress.md) | 低 | 2026-08-30 | codex 子プロセスが apps 無効でも chatgpt.com に接続する |
| [legacy-t13-f9](open/legacy-t13-f9.md) | 低 | 2026-08-30 | codex プローブ失敗の残件 (chatgpt 経路の確認と bin 設定) |
| [pair-rules-vs-data-availability](open/pair-rules-vs-data-availability.md) | 低 | 2026-09-03 | データの無い通貨ペアを agent に見せてしまう余地がある |
| [gate-admission-helper](open/gate-admission-helper.md) | 低 | 2026-09-06 | gate 検査が 3 経路で重複し drift する |
| [jinja-templating-deferred](open/jinja-templating-deferred.md) | 低 | 2026-09-07 | 改善プロンプトの {{ }} 脱出が読みにくい (jinja 化は保留) |
| [selftest-loop-no-cutoff](open/selftest-loop-no-cutoff.md) | 低 | 2026-09-07 | self-test の無限修正ループに打ち切りが無い問題は是正済 |
| [pine-script-to-plugin](open/pine-script-to-plugin.md) | 低 | 2026-09-12 | Pine script を plugin に変換する手段が無い |
| [indicator-reference-oracle-gate](open/indicator-reference-oracle-gate.md) | 低 | 2026-09-15 | indicator の正しさを確かめる門が無い |
| [multi-timeframe-indicator-deps](open/multi-timeframe-indicator-deps.md) | 低 | 2026-09-15 | 1 つの strategy が複数時間足の指標を同時に使えない |
| [fake-run-context-consolidation](open/fake-run-context-consolidation.md) | 低 | 2026-09-17 | テストのフェイク RunContext が散在し追随漏れで壊れやすい |
| [iw-r3-unverified-residuals](open/iw-r3-unverified-residuals.md) | 低 | 2026-09-19 | iw 3 周目で未検証のまま残った 3 点 |
| [ops-ui](open/ops-ui.md) | 低 | 2026-09-19 | 承認 bot と読み取り専用 web ダッシュボードが無い |
| [reject-revert-without-live-classification](open/reject-revert-without-live-classification.md) | 低 | 2026-09-20 | `reject_candidate` (`switch.py:1866-1875`) は自分の未完 journal が… |
| [switch-ops-code-review-r2-altitude](open/switch-ops-code-review-r2-altitude.md) | 低 | 2026-09-20 | switch ops の重複コードや型の緩さの整理 |
| [improve-add-tokenizer-collapses-whitespace](open/improve-add-tokenizer-collapses-whitespace.md) | 低 | 2026-09-21 | シェルの tokenizer (`line.strip().split()` → `" ".join`) が全角空白・… |
| [live-confirmed-bars-into-history](open/live-confirmed-bars-into-history.md) | 低 | 2026-09-21 | ライブで集めた確定足を履歴に積めず二重保存になる |
| [multi-decision-timeframes](open/multi-decision-timeframes.md) | 低 | 2026-09-21 | 判断足を複数持つ構想 (将来拡張) |
| [abandoned-signal-observability](open/abandoned-signal-observability.md) | 低 | 2026-09-27 | 建玉ゼロ中の signal が abandoned で終わる件の観測 |
| [intent-evidence-timeframe-gate](open/intent-evidence-timeframe-gate.md) | 低 | 2026-09-27 | 判断足と無関係な足を根拠にした OPEN を gate が弾かない |
| [market-tool-budget-persist-via-alert-state](open/market-tool-budget-persist-via-alert-state.md) | 低 | 2026-09-27 | 市場ツール予算超過の警告が再起動で重複する |
| [realbackend-tests-fail-under-claude-code](open/realbackend-tests-fail-under-claude-code.md) | 低 | 2026-09-27 | Claude Code の Bash から実 backend テスト 2 本が handshake 失敗 |
| [test-init-offline-unreachable-bridge-flake](open/test-init-offline-unreachable-bridge-flake.md) | 低 | 2026-09-27 | init の offline テストがフル suite の負荷下で落ちる |
| [trade-prompt-says-hourly](open/trade-prompt-says-hourly.md) | 低 | 2026-09-28 | 取引判断 prompt の冒頭「1 時間毎に呼び出され」が固定文言のまま (判断足 15m では 15 分毎)。`de… |
| [outage-observe-closed-guard-unpinned](open/outage-observe-closed-guard-unpinned.md) | 低 | 2026-09-29 | `OutageStateMachine.observe` の「閉場中は観測しない」early return を落として… |
| [signal-producer-hourly-warning-before-confirmation](open/signal-producer-hourly-warning-before-confirmation.md) | 低 | 2026-09-29 | producer が毎時 xx:00:07 の tick で「1h bucket not yet present」を … |
| [backtest-aggregates-partial-minute-bars](open/backtest-aggregates-partial-minute-bars.md) | 低 | 2026-10-01 | backtest と resample が欠けた 1m 足をある分だけ集約し、部分的な足を作る |
| [plugin-sees-live-spread](open/plugin-sees-live-spread.md) | 低 | 2026-10-01 | plugin と判断 mission が実 spread を見られるようにする |
| [risk-gate-rr-uses-configured-spread](open/risk-gate-rr-uses-configured-spread.md) | 低 | 2026-10-01 | risk gate の RR 計算が設定値の spread を使い、実 spread と乖離する |
| [first-run-empty-1m-starts-degraded](open/first-run-empty-1m-starts-degraded.md) | 低 | 2026-10-02 | 初期化直後に 1m 足が空だと degraded から始まる (実機で確認) |
| [outage-leftover-transaction-partial-commit](open/outage-leftover-transaction-partial-commit.md) | 低 | 2026-10-02 | 停止判定の rollback 失敗で残った中途の書き込みが、次の足の保存で確定し得る |
| [restricted-state-followups](open/restricted-state-followups.md) | 低 | 2026-10-02 | restricted 状態の残り: 欠落の記録、入力契約のテスト、結合検証 |
| [runner-trade-backend-error-wording-mismatch](open/runner-trade-backend-error-wording-mismatch.md) | 低 | 2026-10-02 | runner.trade の backend の検証文言が受け付け方と食い違う |
| [tracked-files-contain-author-home-path](open/tracked-files-contain-author-home-path.md) | 低 | 2026-10-02 | 追跡ファイルに作者のホームの絶対パスが残る |
| [rlimit-nproc-per-uid-does-not-bound-worker](open/rlimit-nproc-per-uid-does-not-bound-worker.md) | 低 | 2026-10-03 | RLIMIT_NPROC は uid 全体の値で worker の子プロセス数を縛れない |
| [supervisor-cleanup-test-leaks-36-fds](open/supervisor-cleanup-test-leaks-36-fds.md) | 低 | 2026-10-03 | worker supervisor の cleanup テストが 1 回で fd を 36 本増やす |
| [activity-log-grows-without-rotation](open/activity-log-grows-without-rotation.md) | 低 | 2026-10-04 | activity ログが無期限に伸びる |
| [gate-pytest-worker-dev-dir-rw](open/gate-pytest-worker-dev-dir-rw.md) | 低 | 2026-10-09 | gate pytest worker が /dev をディレクトリ単位 read_write で許している |
| [ops-keys-orphan-secret-temp-after-hard-crash](open/ops-keys-orphan-secret-temp-after-hard-crash.md) | 低 | 2026-10-09 | 鍵 rotate が key:written 直後に強制停止されると平文の一時 file が鍵 dir に残る |
| [cli-launch-malformed-frames-order-dependent](open/cli-launch-malformed-frames-order-dependent.md) | 低 | 2026-10-10 | cli_launch の malformed frame 試験が実行順に依存 |
| [ops-ac25-decision-lane-test-race](open/ops-ac25-decision-lane-test-race.md) | 低 | 2026-10-10 | ops ac25 決定レーン試験が順序窓で不安定 |
| [canary-reject-flaky-port-reuse-forward](open/canary-reject-flaky-port-reuse-forward.md) | 低 | 2026-10-11 | canary の reject 試験が稀に port_reuse_forward も失敗項目に含めて落ちる |
| [improve-admission-hashes-cli-every-mission](open/improve-admission-hashes-cli-every-mission.md) | 低 | 2026-10-11 | 改善 mission の admission が毎回 CLI binary を hash し fingerprint … |
| [ops-bot-display-residuals](open/ops-bot-display-residuals.md) | 低 | 2026-10-11 | ops bot の表示まわりの残余 (CAS 不一致後の終端 edit が 1 回のみ、承認単位 Lock の表が増え… |
| [rpc-dispatcher-join-budget-test-flaky-under-load](open/rpc-dispatcher-join-budget-test-flaky-under-load.md) | 低 | 2026-10-11 | rpc dispatcher の join budget 試験が高負荷時に揺れる |
| [seccomp-chmod-eperm-breaks-codex-rollout](open/seccomp-chmod-eperm-breaks-codex-rollout.md) | 低 | 2026-10-11 | seccomp の chmod/fchmod EPERM で codex の rollout 書込みが失敗する |
| [verify-backend-isolated-via-service](open/verify-backend-isolated-via-service.md) | 低 | 2026-10-11 | 隔離付きの backend 疎通確認を service 経由 (ops API / afx ctl) で提供する |
| [web-ui-charts-equity-indicators](open/web-ui-charts-equity-indicators.md) | 低 | 2026-10-11 | 同梱 web に資金遷移グラフとチャート重ね表示 |
| [afx-init-reset-builtin](open/afx-init-reset-builtin.md) | 低 | 2026-10-12 | afx init --reset (内蔵のリセット) を追加する |
| [legacy-discord-webhook-notify-removal](open/legacy-discord-webhook-notify-removal.md) | 低 | 2026-10-12 | 旧 discord.enabled + .env webhook の簡易通知を廃止する |
| [ops-decision-lane-ac25-timing-flake](open/ops-decision-lane-ac25-timing-flake.md) | 低 | 2026-10-13 | tests/ops の ac25 が負荷下で timing flake |
| [bridge-expose-margin-mode](open/bridge-expose-margin-mode.md) | 低 | 2026-10-16 | bridge の /account に margin_mode (netting / hedging) を出す |
| [per-strategy-virtual-books](open/per-strategy-virtual-books.md) | 低 | 2026-10-16 | 戦略ごとの実帳簿 (同一口座内の仮想分割、両建ては戦略間のみ) |
| [signal-maintenance-offload-worker](open/signal-maintenance-offload-worker.md) | 低 | 2026-10-16 | signal maintenance を tick から別 worker へ切り出す |
| [wizard-script-conventions-and-selection-ui](open/wizard-script-conventions-and-selection-ui.md) | 低 | 2026-10-16 | ウィザード台本の表記規約と選択 UI (矢印選択・色) の整備 |
| [tests-leak-tmp-dirs-outside-basetemp](open/tests-leak-tmp-dirs-outside-basetemp.md) | 低 | 2026-10-17 | 改善隔離 test helper が /tmp に afx-root-* を残す (basetemp 外) |
| [disable-dependent-change-error-notice](open/disable-dependent-change-error-notice.md) | 低 | 2026-10-18 | disable 後に依存 strategy が解決不能になる旨の表示が無い |
| [plugin-lock-wait-notice-on-wait-argument](open/plugin-lock-wait-notice-on-wait-argument.md) | 低 | 2026-10-18 | plugin lock の待機通知を on_wait 引数に置き換える |
| [rollback-foreign-temp-link-sweep](open/rollback-foreign-temp-link-sweep.md) | 低 | 2026-10-18 | foreign で終端した rollback の temp symlink を sweep の対象にする |
| [isolated-worker-probe-lacks-bless](open/isolated-worker-probe-lacks-bless.md) | 低 | 2026-10-19 | 実隔離 worker の deploy 境界 probe に plugin bless が無い |
| [improve-new-candidate-metric-reference-set-freeze](open/improve-new-candidate-metric-reference-set-freeze.md) | 低 | 2026-10-22 | 方向性指標の「新規候補」の参照集合を観測開始時点で凍結する |
| [approval-no-history-passthrough](open/approval-no-history-passthrough.md) | 低 |  | `plugin/approval.py` の `_validate_kind`/`_validate_strategy… |
| [backend-descriptor](open/backend-descriptor.md) | 低 |  | backend 別の if 分岐が 5 ファイルに散在 |
| [backlog-dup](open/backlog-dup.md) | 低 |  | backlog に実質同じ案が重複して溜まる |
| [backtest-scheduler-log-leak](open/backtest-scheduler-log-leak.md) | 低 |  | backtest 内部 Scheduler の `maintain_reservations failed: no c… |
| [credentials-file-copied-unused](open/credentials-file-copied-unused.md) | 低 |  | `ClaudeRunner.__init__(credentials_file_copied)` が未使用 |
| [db-rebuild-helper](open/db-rebuild-helper.md) | 低 |  | DB の table rebuild migration が 7 本重複している |
| [graceful-stop-waits-mission](open/graceful-stop-waits-mission.md) | 低 |  | graceful stop が走行中 improve mission の timeout (最大 60 分) を待つ … |
| [improvement-runs-backlog-id-on-failure](open/improvement-runs-backlog-id-on-failure.md) | 低 |  | 失敗 mission では `improvement_runs.backlog_id` が NULL のままで選択 b… |
| [latest-in-sample-metrics-requires-row-factory](open/latest-in-sample-metrics-requires-row-factory.md) | 低 |  | `latest_in_sample_metrics` は `row_factory=sqlite3.Row` を暗黙に… |
| [legacy-d-stage0-e](open/legacy-d-stage0-e.md) | 低 |  | switch の resume 経路が symlink 版ディレクトリを resolve せず受理 |
| [legacy-e2e-model](open/legacy-e2e-model.md) | 低 |  | backend を切り替えるたびにモデル名の手編集が要る |
| [legacy-l72](open/legacy-l72.md) | 低 |  | holdout の空期間分岐に穴があるとの記録 |
| [legacy-oc-qwen3-8-toolcall](open/legacy-oc-qwen3-8-toolcall.md) | 低 |  | モデルが壊れた引数でツールを連打し予算を焼く |
| [legacy-oc-resume-discard](open/legacy-oc-resume-discard.md) | 低 |  | resume 時の report 降格経路が未発火で未検証 |
| [legacy-oc-websearch-ssl](open/legacy-oc-websearch-ssl.md) | 低 |  | web 検索が間欠的に SSL 検証失敗する |
| [legacy-t13-step7](open/legacy-t13-step7.md) | 低 |  | Task 13 の実機 E2E 手順 3 以降が未実施 |
| [legacy-t9b-go-not-sent-parent-runaway](open/legacy-t9b-go-not-sent-parent-runaway.md) | 低 |  | go が送られない変異で親がスレッド暴走する |
| [loader-double-read-hash](open/loader-double-read-hash.md) | 低 |  | `_discover_one` が bytes を持ちながら path 版 `content_hash(entry)`… |
| [mission-output-not-persisted](open/mission-output-not-persisted.md) | 低 |  | mission の出力と transcript が DB に残らない疑い |
| [missions-finished-at-is-logical](open/missions-finished-at-is-logical.md) | 低 |  | `missions.finished_at` は終端メソッドに渡す `now` で、親ゲート 3 本 (≒70 秒) … |
| [normalize-reason-duplicated](open/normalize-reason-duplicated.md) | 低 |  | `cli_runner._normalize_reason` が local_runner.py:39-56 の逐語複… |
| [observation-carryover-mismatch](open/observation-carryover-mismatch.md) | 低 |  | observation の申し送りと staging の実態が食い違う |
| [opencode-as-mb-hardcode](open/opencode-as-mb-hardcode.md) | 低 |  | mission_worker.py:703 の `max(as_mb, 262144)` を settings 化 (… |
| [opencode-node-modules-copytree](open/opencode-node-modules-copytree.md) | 低 |  | mission 毎に node_modules 63MB/3648 file を copytree。symlink +… |
| [primary-transcript-lost-on-timeout](open/primary-transcript-lost-on-timeout.md) | 低 |  | timeout→段B 追撃経路で primary の transcript (tool 64 件) が保存されず消える |
| [rpc-call-helper](open/rpc-call-helper.md) | 低 |  | RPC の framing が 2 箇所で重複実装されている |
| [settings-hash-excludes-improve-gate](open/settings-hash-excludes-improve-gate.md) | 低 |  | `settings_snapshot_hash` は `risk` + `backtest` のみ → `improv… |
| [test-isolation](open/test-isolation.md) | 低 |  | 特定 4 ファイルの部分実行で loops の fixture が見えなくなる |

### 優先未設定 (1)

| id | 優先 | 起票 | 概要 |
|---|---|---|---|
| [archive-artifact-hash-vs-submitted](open/archive-artifact-hash-vs-submitted.md) | 未設定 |  | `candidate_archives.artifact_hash` (backtest 時点) と approval… |

