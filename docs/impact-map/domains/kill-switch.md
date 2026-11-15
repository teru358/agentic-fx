---
{"id": "kill-switch", "owner_role": "conductor", "literals": {"marker:reset_marker": [".reset-in-progress"]}, "resources": ["state:app_state.kill_switch_latched", "state:app_state.kill_switch_generation", "state:app_state.kill_switch_latched_at", "marker:reset_marker", "event:guard_state_changed", "event:ops_request_terminal", "event:event_stream_health", "log:guard_notice_deferred", "log:guard_notice_backfill_skipped", "activity:kill_switch_latched", "activity:kill_switch_latched_durability_uncertain", "activity:kill_switch_reset", "activity:kill_switch_reconcile_required", "activity:kill_switch_reconcile_confirmed", "activity:kill_switch_reconcile_failed", "activity:kill_switch_state_uncertain", "db:account_snapshots", "config:risk.drawdown_kill_pct", "metric:kill_switch_latches", "metric:kill_switch_events", "ops:guard_lane_reentry", "ops:guard_lane_not_held", "ops:guard_state_unavailable", "ops:guard_notice_pending", "ops:guard_notice_generation", "ops:guard_kind"], "anchors": ["src/agentic_fx/core/risk_gate.py::evaluate", "src/agentic_fx/core/executor.py::Executor._evaluate_and_execute_open", "src/agentic_fx/store/state.py::StateStore.load", "src/agentic_fx/store/state.py::StateStore.update", "src/agentic_fx/store/state.py::StateStore.reset_kill_switch", "src/agentic_fx/store/state.py::StateStore.confirm_latched", "src/agentic_fx/ops/service.py::OpsService.reset_kill_switch", "src/agentic_fx/ops/service.py::OpsService.reconcile_kill_switch", "src/agentic_fx/ops/service.py::plan_kill_switch_notice", "src/agentic_fx/ops/service.py::OpsService._sweep_kill_switch", "src/agentic_fx/ops/service.py::OpsService._guard_step", "src/agentic_fx/ops/service.py::OpsService.emit_pending_guard_notices", "src/agentic_fx/commands.py::Commands.dispatch", "src/agentic_fx/commands.py::Commands._dispatch#reset_kill_switch", "src/agentic_fx/commands.py::Commands._dispatch#write@kill_switch_state_uncertain", "src/agentic_fx/commands.py::Commands._killswitch_reconcile", "src/agentic_fx/commands.py::Commands._data_resume", "src/agentic_fx/ops/service.py::OpsService.resume_data", "src/agentic_fx/ops/events.py::EventStore.emit_best_effort", "src/agentic_fx/ops/events.py::EventStore.emit", "src/agentic_fx/ops/service.py::OpsService._terminal", "src/agentic_fx/service.py::build_app#_build_ops_service", "src/agentic_fx/service.py::build_app#emit_pending_notices", "src/agentic_fx/service.py::build_app#_recover_ops", "src/agentic_fx/ops/service.py::OpsService.emit_pending_notices", "src/agentic_fx/ops/service.py::OpsService._current_state", "src/agentic_fx/core/accounting.py::record_snapshot", "src/agentic_fx/core/accounting.py::current_account", "src/agentic_fx/backtest/runner.py::_auto_release_kill_switch", "src/agentic_fx/backtest/runner.py::run_replay", "src/agentic_fx/backtest/metrics.py::compute_metrics", "afx-ops/afx_ops/gate/render.py::_event_text_for_code", "afx-ops/afx_ops/bot/adapter.py::BotAdapter.deliver_events", "afx-ops/afx_ops/notify/payload.py::encode_event", "afx-ops/afx_ops/notify/runtime.py::_Loop._poll_once", "src/agentic_fx/status.py::read_state", "afx-ops/afx_ops/web/render.py::render_activity", "src/agentic_fx/ops/service.py::OpsService.status", "src/agentic_fx/ops/events.py::<module>.FIELDS", "src/agentic_fx/service.py::_build_ops_service", "src/agentic_fx/service.py::_build_ops_service.status_provider", "src/agentic_fx/backtest/runner.py::_kill_switch_event", "src/agentic_fx/store/state.py::<module>._BOOL_FIELDS", "src/agentic_fx/store/state.py::<module>._REQUIRED_KEYS"], "related": ["auto-killswitch-latch-emits-no-event", "killswitch-reset-hwm-rebase", "datafeed-outage-discord-notice", "killswitch-latched-activity-repeats", "bot-gateway-disconnect-is-silent"], "tests": ["tests/store/test_state.py", "tests/core/test_executor.py", "tests/ops/test_ac51_killswitch_api.py", "tests/test_commands.py"]}
---
# Kill switch / guard state

観測 revision: `0dbcb27f`。
この頁は資金保護の latch、解除と通知の到達先を読む。
入口は live の二経路、replay、ops API と shell である。
DD gate は open intent の評価時に動く。
state の latch・generation・latched_at と reset marker を区別する。
marker 中の表示は安全側であり raw state と同一とは限らない。
account snapshot は DD 入力、replay metric は比較への出力である。
guard event、terminal event、activity は別の投影である。
C4 sweep は event と現 state を照合する。
通知失敗は成立済み操作を戻さない。
shell reset/reconcile は lane 外の直接 mutation を維持する。
shell data resume は guard event を通らない。
ops data resume は callback 後に guard event を発行する。
明示的 HWM rebase は承認済みだが、この revision では未実装。
通知 sweep は失われた全履歴の復元を保証しない。
status の marker 投影と StateStore の全分岐一致は未確認。
実通知、本番データと運用設定は調査していない。
下の F は source 観測、K は契約、D は差または残余リスク。
手動辺は literal 索引では結べない入口・callback を補う。
検証記録は実行の有無を明示し、未測定を合格としない。

## F — 観測事実

- **F01** `src/agentic_fx/core/risk_gate.py::evaluate` — open 判定で既存 latch と drawdown 条件を別に拒否する。risk の設定は引数で伝わる。 Resource: `state:app_state.kill_switch_latched`, `config:risk.drawdown_kill_pct`.
- **F02** `src/agentic_fx/core/executor.py::Executor._evaluate_and_execute_open` — gate の理由文字列の部分一致で DD を認識し state を true にする。通常 latch は activity を記録し、durability 不確実時は load 確認と別 activity・通知を試みる。通常 latch から event の直送はしない。 Resource: `state:app_state.kill_switch_latched`, `activity:kill_switch_latched`, `activity:kill_switch_latched_durability_uncertain`.
- **F03** `src/agentic_fx/store/state.py::StateStore.load` — marker があると raw を安全側の latched に投影する。file_value は raw を返す。 Resource: `state:app_state.kill_switch_latched`, `marker:reset_marker`.
- **F04** `src/agentic_fx/store/state.py::StateStore.update` — raw を読み、単独 true の既存 latch は no-op。false→true は世代と発動時刻を更新し、false は時刻を消す。 Resource: `state:app_state.kill_switch_latched`, `state:app_state.kill_switch_generation`, `state:app_state.kill_switch_latched_at`.
- **F05** `src/agentic_fx/store/state.py::StateStore.reset_kill_switch` — 世代一致・latched・marker 不在を検査し、marker 永続化→state 保存→marker 削除。世代と発動時刻は保持する。 Resource: `state:app_state.kill_switch_latched`, `state:app_state.kill_switch_generation`, `state:app_state.kill_switch_latched_at`, `marker:reset_marker`.
- **F06** `src/agentic_fx/store/state.py::StateStore.confirm_latched` — marker 無しなら no-op、marker 中 raw false は次世代 true と現在時刻の発動時刻、raw true は世代と時刻を維持。保存後 marker を除く。 Resource: `state:app_state.kill_switch_latched`, `state:app_state.kill_switch_generation`, `state:app_state.kill_switch_latched_at`, `marker:reset_marker`.
- **F07** `src/agentic_fx/ops/service.py::OpsService.reset_kill_switch` — ops lane の mutation 内で操作前・結果・現 state の順に guard step を試す。通知失敗と mutation 結果は分離する。 Resource: `state:app_state.kill_switch_latched`, `event:guard_state_changed`.
- **F08** `src/agentic_fx/ops/service.py::OpsService.reconcile_kill_switch` — confirm の前・結果・現 state で guard step を試し、finally で通知失敗を処理する。 Resource: `marker:reset_marker`, `event:guard_state_changed`.
- **F09** `src/agentic_fx/ops/service.py::plan_kill_switch_notice` — 世代と発動時刻を使って該当 incarnation の event を照合。latched 不足を補い、clear は既存 latched がある場合だけ released を補う。released 済みは閉じ、履歴を捏造しない。 Resource: `state:app_state.kill_switch_latched`, `state:app_state.kill_switch_generation`, `state:app_state.kill_switch_latched_at`, `event:guard_state_changed`, `ops:guard_kind`.
- **F10** `src/agentic_fx/ops/service.py::OpsService._sweep_kill_switch` — lane 所有を要求し、plan に基づき最大一件を guard_kind=kill_switch で発行する (kill switch の guard event を INSERT する唯一の箇所)。発動時刻を not_before とし、閉じた世代の欠落は warning log の固定 code (event ではない) で一度だけ記録する。発動時刻 (latched_at) は event の not_before にだけ渡す。 Resource: `event:guard_state_changed`, `log:guard_notice_backfill_skipped`, `state:app_state.kill_switch_latched_at`, `ops:guard_lane_not_held`, `ops:guard_kind`.
- **F11** `src/agentic_fx/ops/service.py::OpsService._guard_step` — sweep 例外を捕捉し rollback を試み、失敗は run の旗に立てるだけで、元の操作に失敗を伝えない。固定 code の warning log は後段の _finish_notices が出し、この関数自身は log も event も出さない。 Resource: `event:guard_state_changed`.
- **F12** `src/agentic_fx/ops/service.py::OpsService.emit_pending_guard_notices` — 公開 wrapper は同一 thread の lane 再入を拒否し、lane 内で after と現在 state を照合する。 Resource: `ops:guard_lane_reentry`, `event:guard_state_changed`.
- **F13** `src/agentic_fx/commands.py::Commands._dispatch` — (`dispatch` は通知失敗を集める wrapper で、reset の本体はここ) shell reset は store を直接操作し前後に wrapper を呼ぶ。ResetNotApplied も投影 state で通知を試みる。state 不確実は別 activity。 reset の activity 本文には操作前の発動時刻を渡す。 Resource: `activity:kill_switch_reset`, `activity:kill_switch_state_uncertain`, `event:guard_state_changed`, `state:app_state.kill_switch_latched_at`.
- **F14** `src/agentic_fx/commands.py::Commands._killswitch_reconcile` — shell confirm は直接 store を呼び、操作前後に wrapper を呼ぶ。失敗を activity に記録する。 Resource: `marker:reset_marker`, `activity:kill_switch_reconcile_confirmed`, `activity:kill_switch_reconcile_failed`, `event:guard_state_changed`.
- **F15** `src/agentic_fx/commands.py::Commands._data_resume` — outage.request_resume を直接呼び、ops lane と OpsService.resume_data を通らないため guard event を生成しない (D04)。
- **F16** `src/agentic_fx/ops/service.py::OpsService.resume_data` — 注入 callback の成功後、data_feed の resume_requested を _guard_event で発行する。 Resource: `event:guard_state_changed`, `ops:guard_kind`.
- **F17** `src/agentic_fx/ops/events.py::EventStore.emit_best_effort` — INSERT 失敗は rollback と失敗集計で隔離し、後の成功時に health event を試す。schema と時刻下限の検証は try の外にある。 Resource: `event:event_stream_health`, `event:guard_state_changed`.
- **F18** `src/agentic_fx/ops/events.py::EventStore.emit` — schema 検証と INSERT・commit を行い、失敗を呼出し元へ伝える。not_before は event 時刻の下限。 terminal と stream health も同じ emit で書く。 Resource: `event:guard_state_changed`, `event:ops_request_terminal`, `event:event_stream_health`.
- **F19** `src/agentic_fx/ops/service.py::OpsService._terminal` — mutation の監査終端に対応する terminal event を発行する。guard event とは別である。 Resource: `event:ops_request_terminal`.
- **F20** `src/agentic_fx/service.py::build_app` — 起動時に reconcile_marker を読み、marker があれば確認要求 activity を記録し、ops service と起動時 sweep を呼ぶ。 Resource: `activity:kill_switch_reconcile_required`, `marker:reset_marker`, `event:guard_state_changed`.
- **F21** `src/agentic_fx/ops/service.py::OpsService.emit_pending_notices` — 起動時と scheduler tick で lane 内の improve と guard の通知を別々に試す。guard 側の失敗や lane 不獲得は warning log の固定 code (event ではない) で記録し、次の周期に回す。 Resource: `event:guard_state_changed`, `log:guard_notice_deferred`.
- **F22** `src/agentic_fx/ops/service.py::OpsService._current_state` — state store が無い場合に固定 code を送出し、通常は marker 投影済み state を読む。 Resource: `ops:guard_state_unavailable`, `state:app_state.kill_switch_latched`.
- **F23** `src/agentic_fx/core/accounting.py::record_snapshot` — live の account snapshot を INSERT する。HWM と equity は DD 入力に関係する。 Resource: `db:account_snapshots`.
- **F24** `src/agentic_fx/core/accounting.py::current_account` — 最新 snapshot を口座入力に利用する。 Resource: `db:account_snapshots`.
- **F25** `src/agentic_fx/backtest/runner.py::_auto_release_kill_switch` — replay の解除は snapshot 書込みと state 変更を行い、commit 失敗時には補償を試みる。 Resource: `db:account_snapshots`, `state:app_state.kill_switch_latched`.
- **F26** `src/agentic_fx/backtest/runner.py::run_replay` — replay は遷移と理由から event artifact と latch 数を構成し、snapshot を結果に含める。 Resource: `metric:kill_switch_events`, `metric:kill_switch_latches`, `db:account_snapshots`.
- **F27** `src/agentic_fx/backtest/metrics.py::compute_metrics` — replay 結果の latch 数を比較 metric に出す。 Resource: `metric:kill_switch_latches`.
- **F28** `afx-ops/afx_ops/gate/render.py::_event_text_for_code` — guard と terminal の表示を分岐する。terminal は既定で投稿対象外。 Resource: `event:guard_state_changed`, `event:ops_request_terminal`.
- **F29** `afx-ops/afx_ops/bot/adapter.py::BotAdapter.deliver_events` — poll・送信・cursor を扱う。送信失敗時は cursor を進めず再試行する。 Resource: `event:guard_state_changed`, `event:ops_request_terminal`, `event:event_stream_health`.
- **F30** `afx-ops/afx_ops/notify/payload.py::encode_event` — event schema を検査し配送用 payload に展開する。 Resource: `event:guard_state_changed`, `event:ops_request_terminal`, `event:event_stream_health`.
- **F31** `afx-ops/afx_ops/notify/runtime.py::_Loop._poll_once` — notify は bot と独立した cursor を使用し、送信後に保存する。初回は high まで skip する。 Resource: `event:guard_state_changed`, `event:ops_request_terminal`, `event:event_stream_health`.
- **F32** `src/agentic_fx/status.py::read_state` — StateStore と別に raw と marker を読み、安全側表示する。marker の lstat 失敗も存在扱い。 Resource: `marker:reset_marker`, `state:app_state.kill_switch_latched`.
- **F33** `afx-ops/afx_ops/web/render.py::render_activity` — web は activity を描画する。event 配送の成功を表すものではない。 kind による選別はせず、lines の末尾を換算・escape して描く。 Resource: `activity:kill_switch_reset`, `activity:kill_switch_latched`, `activity:kill_switch_latched_durability_uncertain`, `activity:kill_switch_reconcile_required`, `activity:kill_switch_reconcile_confirmed`, `activity:kill_switch_reconcile_failed`, `activity:kill_switch_state_uncertain`.
- **F34** `src/agentic_fx/ops/service.py::OpsService.status` — status は投影 state と marker の確認情報を返す。 kill_switch に世代と発動時刻も載せる。 Resource: `state:app_state.kill_switch_latched`, `marker:reset_marker`, `state:app_state.kill_switch_generation`, `state:app_state.kill_switch_latched_at`.
- **F35** `src/agentic_fx/ops/events.py::<module>.FIELDS` — event 種別ごとの固定 field 表。guard は guard_kind・state・generation、terminal は endpoint_code・result_code・authenticated_principal、stream health は failure_count・first_failed_at・last_failed_at。 Resource: `event:guard_state_changed`, `event:ops_request_terminal`, `event:event_stream_health`, `ops:guard_kind`.
- **F36** `src/agentic_fx/service.py::_build_ops_service` — OpsService に read-only pending 投影と data_resume callback を配線する。event の生成は OpsService 側で、この関数は配線だけを行う。status への pending 投影そのものは入れ子の status_provider (F92) にある。

- **F37** `afx-ops/afx_ops/gate/render.py::<module>.OPS_EVENT_SPECS` — guard の既定通知を有効、terminal と stream health を無効として同じ文面関数に登録する。 Resource: `event:guard_state_changed`, `event:ops_request_terminal`, `event:event_stream_health`.
- **F38** `afx-ops/afx_ops/gate/render.py::_event_text_for_code` — guard_kind と state により kill switch と data feed の文面を分ける。 Resource: `ops:guard_kind`.

- **F39** `src/agentic_fx/store/snapshots.py::add` — account_snapshots へ ts(UTC 化)・balance・equity・hwm・cashflow・source を INSERT して commit する。source の既定は paper。 Resource: `db:account_snapshots`.
- **F40** `src/agentic_fx/store/snapshots.py::latest` — `ORDER BY ts DESC, id DESC LIMIT 1` の最新行を返す。current_account と replay の解除判定が読む。 Resource: `db:account_snapshots`.
- **F41** `src/agentic_fx/store/snapshots.py::first_since` — 指定 ts 以降で最古の行を返す。日次基準の算出が使う。 Resource: `db:account_snapshots`.
- **F42** `src/agentic_fx/store/snapshots.py::last_before` — 指定 ts より前で最新の行を返す。日初以降に行が無いときの日次基準が使う。 Resource: `db:account_snapshots`.
- **F43** `src/agentic_fx/store/db.py::<module>._SCHEMA` — account_snapshots の列 (id, ts, balance, equity, hwm, cashflow, source) と (ts, id) 索引を定義する。 Resource: `db:account_snapshots`.
- **F44** `src/agentic_fx/core/accounting.py::daily_start_snapshot` — 日初以降の最古行、無ければ直前行を基準とし、直前行が上限時間を超えて古ければ None (fail closed)。基準以降の cashflow を加算する。daily_start_equity 経由の日次損失の基準で、DD kill の hwm 入力ではない。 Resource: `db:account_snapshots`.
- **F45** `src/agentic_fx/backtest/runner.py::_kill_switch_event` — entry の snapshot_id の行 (id 無しの旧呼出しは ts 以下の最新行) の equity と hwm から、hwm が正のとき drawdown_pct を計算する。同 ts の kill_switch_latched activity があれば reason をその本文に置き換え、event 一件を返す。 Resource: `db:account_snapshots`, `metric:kill_switch_events`.
- **F46** `src/agentic_fx/backtest/runner.py::_release_is_due` — replay の解除は、呼出し側が読んだ最新 snapshot が今 tick の新規行 (id 増加、ts が now、source が paper) で、開場中かつ発動後の次の rollover を過ぎたときだけ成立する。 Resource: `db:account_snapshots`.
- **F47** `src/agentic_fx/core/executor.py::Executor._open` — current_account が None (snapshot 無し・陳腐化) なら gate_rejected を記録して拒否する。得た equity と hwm、現 state の latched を GateContext に渡す。 Resource: `db:account_snapshots`.
- **F48** `src/agentic_fx/core/executor.py::Executor.open_from_snapshot` — 実行 snapshot の鮮度検査の後、_open と同様に current_account の equity と hwm を使い、無ければ fail closed で拒否する。latched は現 state から読む。 Resource: `db:account_snapshots`.
- **F49** `src/agentic_fx/core/executor.py::Executor._best_effort_record` — activity 書込みと通知を、それぞれ例外を握りつぶして試す。DD latch の耐久性不確実時に kind を kill_switch_latched_durability_uncertain として呼ばれ、拒否の完結を妨げない。 Resource: `activity:kill_switch_latched_durability_uncertain`.
- **F50** `src/agentic_fx/store/state.py::AppState` — latched (既定 False)・generation (既定 0)・latched_at (既定 None) を持つ state の型。 Resource: `state:app_state.kill_switch_latched`, `state:app_state.kill_switch_generation`, `state:app_state.kill_switch_latched_at`.
- **F51** `src/agentic_fx/store/state.py::parse_state` — latched は必須の bool、generation は無ければ 0 で int 限定、latched_at は None か str。違反は StateError。I/O を持たず、read-only の観測側とも共有される。 Resource: `state:app_state.kill_switch_latched`, `state:app_state.kill_switch_generation`, `state:app_state.kill_switch_latched_at`.
- **F52** `src/agentic_fx/store/state.py::StateStore.save` — AppState 全体を直列化し、一時 file の fsync、replace、親 dir の fsync の順で書く。dir fsync の失敗は DurabilityUncertain とする。latched・generation・latched_at を一括で書く唯一の経路。 Resource: `state:app_state.kill_switch_latched`, `state:app_state.kill_switch_generation`, `state:app_state.kill_switch_latched_at`.
- **F53** `src/agentic_fx/store/state.py::StateStore._load_unlocked` — file が無ければ既定の AppState、あれば parse_state で検証する。marker の投影は load 側 (F03) が行い、ここは raw を返す。 Resource: `state:app_state.kill_switch_latched`.
- **F54** `src/agentic_fx/store/state.py::<module>._MARKER_SUFFIX` — reset marker の名前を state file の path に付ける固定 suffix。 Resource: `marker:reset_marker`.
- **F55** `src/agentic_fx/store/state.py::StateStore._marker` — state file の path に suffix を付けた marker の path を返す。 Resource: `marker:reset_marker`.
- **F56** `src/agentic_fx/store/state.py::StateStore._write_marker` — requested_generation と started_at を O_EXCL で作成し、fsync と親 dir の fsync まで行う。作れなければ ResetNotApplied、作りかけの除去に失敗すれば StateUncertain。 Resource: `marker:reset_marker`.
- **F57** `src/agentic_fx/store/state.py::StateStore._remove_marker` — marker を消し、後続の親 dir fsync の失敗は伝えない。戻った marker はラッチ側に倒れるだけとする。 Resource: `marker:reset_marker`.
- **F58** `src/agentic_fx/store/state.py::StateStore.reconcile_marker` — marker が無ければ None。読めない・壊れている場合も「有る」として理由付きの dict を返す。 Resource: `marker:reset_marker`.
- **F59** `src/agentic_fx/backtest/runner.py::_RecordingStateStore.update` — 親の update の前後で latched が変わったとき、publish が真なら latched か released の遷移を ts・reason・snapshot_id 付きで記録する。遷移時に snapshot_id が無ければ注入関数から取る。 Resource: `state:app_state.kill_switch_latched`.
- **F60** `src/agentic_fx/backtest/runner.py::_RecordingStateStore.publish_released` — 解除の遷移を released として ts・reason・snapshot_id 付きで追記する。replay の自動解除が使い、kill_switch_events の元になる。 Resource: `metric:kill_switch_events`.
- **F61** `src/agentic_fx/backtest/runner.py::BacktestResult` — replay の結果の型。snapshots、kill_switch_events (既定は空)、kill_switch_latches (既定 0) を持つ。 Resource: `metric:kill_switch_events`, `metric:kill_switch_latches`.
- **F62** `src/agentic_fx/core/risk_gate.py::GateContext` — gate 入力の型。equity・hwm・kill_switch_latched を含み、呼出し側が組み立てて渡す。 Resource: `state:app_state.kill_switch_latched`.
- **F63** `src/agentic_fx/backtest/metrics.py::<module>.METRIC_KEYS` — 保存・比較に使える指標名の許可集合。kill_switch_latches を含む。 Resource: `metric:kill_switch_latches`.
- **F64** `src/agentic_fx/store/backtest_runs.py::_filter_metrics` — 指標を METRIC_KEYS の白リストとスカラー型で濾過する。件数系は非負 int だけを通す。 Resource: `metric:kill_switch_latches`.
- **F65** `src/agentic_fx/store/backtest_runs.py::_insert` — 濾過後の指標を metrics_json にして backtest_runs へ INSERT する。 Resource: `metric:kill_switch_latches`.
- **F66** `src/agentic_fx/plugin/strategy_gate.py::<module>._COMPARE_METRIC_KEYS` — 候補と配備済みの比較に出す指標名。kill_switch_latches を含む。 Resource: `metric:kill_switch_latches`.
- **F67** `src/agentic_fx/plugin/strategy_gate.py::_project` — 指標 dict から比較対象の key だけを抜き出す。無い key は None。 Resource: `metric:kill_switch_latches`.
- **F68** `src/agentic_fx/ops/events.py::<module>._REF` — event 種別ごとの ref の形。guard と stream health は固定の instance、terminal は audit:数値。 Resource: `event:guard_state_changed`, `event:ops_request_terminal`, `event:event_stream_health`.
- **F69** `src/agentic_fx/ops/events.py::validate_fields` — 種別ごとの field 集合 (必須と任意) の一致、非取引 event の ref の形、値が None・整数・安全な文字列であることを検査し、違反は EventSchemaError。 Resource: `event:guard_state_changed`, `event:ops_request_terminal`, `event:event_stream_health`.
- **F70** `src/agentic_fx/ops/events.py::kill_switch_event_states` — code が guard_state_changed、ref が instance、guard_kind が kill_switch、generation 一致の event の (ts, state) を読む。ops_events の読取りはこの module に限る。 Resource: `event:guard_state_changed`, `ops:guard_kind`.
- **F71** `afx-ops/afx_ops/notify/payload.py::<module>.ALLOWED_FIELDS` — 配送側の field 許可集合。ops 側 FIELDS と手で二重化しており、全 code の一致を機械的に守る検査は未確認。 Resource: `event:guard_state_changed`, `event:ops_request_terminal`, `event:event_stream_health`, `ops:guard_kind`.
- **F72** `afx-ops/afx_ops/notify/payload.py::<module>._REF` — 配送側の ref の形。ops 側 _REF と二重化し、終端を \Z で固定する。 Resource: `event:guard_state_changed`, `event:ops_request_terminal`, `event:event_stream_health`.
- **F73** `src/agentic_fx/ops/service.py::OpsService._guard_event` — guard_kind・state・generation を固定 field として guard_state_changed を instance 宛てに best-effort で発行する。data_feed などの kill switch 以外の guard が使う。 Resource: `ops:guard_kind`.
- **F74** `afx-ops/afx_ops/gate/service.py::GateService.plan_event` — 完全検証 (decide) の後に cursor 以下を除き、設定で出さない event は SkipKnown、出す event は EventPlan にする。guard は ops_notices の設定に従う。 Resource: `event:guard_state_changed`.
- **F75** `afx-ops/afx_ops/event_outcome.py::decide` — encode_event による完全検証 (PayloadError) が先で、その後に設定の filter を当てて Post か SkipKnown を返す。 Resource: `event:guard_state_changed`.
- **F76** `afx-ops/afx_ops/gate/render.py::wants_ops_event` — OPS_EVENT_SPECS の既定 (guard は有効) か ops_notices が all のとき真。 Resource: `event:guard_state_changed`.
- **F77** `afx-ops/afx_ops/gate/render.py::render_event` — 検証済み payload を固定 template の一行にし、長すぎれば汎用文へ落とす。mention は設定値から付ける。 Resource: `event:guard_state_changed`.
- **F78** `afx-ops/afx_ops/notify/runtime.py::_Loop._wants` — improve と取引以外の event (guard・terminal・stream health) は設定を見ず常に配送する。bot の ops_notices による絞込みとは別。 Resource: `event:guard_state_changed`.
- **F79** `afx-ops/afx_ops/api_client.py::BotClient.events` — bot の event 取得入口。`/v1/events` を after・limit だけで GET し、code を選ばず返す。guard・terminal・stream health は同じ page に混ざって bot へ渡る。 Resource: `event:guard_state_changed`, `event:ops_request_terminal`, `event:event_stream_health`.
- **F80** `afx-ops/afx_ops/api_client.py::NotifyClient.events` — notify の event 取得入口。bot と同じ GET を principal だけ変えて呼ぶ。読取り専用で actor を付けない。code は選ばない。 Resource: `event:guard_state_changed`, `event:ops_request_terminal`, `event:event_stream_health`.
- **F81** `afx-ops/afx_ops/events_page.py::parse_page` — bot と notify が共有する page parser。応答形を検査して失敗は注入された failure("events_invalid") で返し、id が読める event を id 順に並べ、high watermark は応答値と最大 id の大きい方、読めない id に当たったら打ち切って件数を返す。code には依存しない。 Resource: `event:guard_state_changed`, `event:ops_request_terminal`, `event:event_stream_health`.
- **F82** `afx-ops/afx_ops/bot/adapter.py::_event_content` — 通知本文の最終射影。plan の mention と本文を連結し、2000 字を超えたら mention だけを外す。code に依らず全 event の本文がここを通る。 Resource: `event:guard_state_changed`, `event:ops_request_terminal`, `event:event_stream_health`.
- **F83** `afx-ops/afx_ops/notify/runtime.py::_Loop._deliver` — encode 済み body を retry 経由で注入された送信へ渡す。RetryError は _Failure に写し、event ごとの cursor 保存は呼出し側で行う。 Resource: `event:guard_state_changed`, `event:ops_request_terminal`, `event:event_stream_health`.
- **F84** `afx-ops/afx_ops/notify/runtime.py::_Loop._deliver.attempt` — retry に注入される callback。self._send(target, body) を実行し、DeliveryError を再試行可否付きの RetryError に写す。 Resource: `event:guard_state_changed`, `event:ops_request_terminal`, `event:event_stream_health`.
- **F85** `src/agentic_fx/service.py::build_splash` — 起動 splash で state.reconcile_marker() の有無に応じ、確認要求の行を加える。marker を直接読む。 latched と drawdown_kill_pct も同じ行に出す。 Resource: `marker:reset_marker`, `state:app_state.kill_switch_latched`, `config:risk.drawdown_kill_pct`.
- **F86** `src/agentic_fx/loops/improve_loop.py::ImproveLoop._persist_gate_rows` — gate_rows を mission_outcome 付きで save_harness_run へ渡し、harness の metrics (latch 数を含む) を保存する。 Resource: `metric:kill_switch_latches`.
- **F87** `src/agentic_fx/loops/improve_loop.py::ImproveLoop._persist_ledger_rows` — 受理された run_backtest の result_summary から行引数を射影し、harness metrics を save_harness_run で保存する。 Resource: `metric:kill_switch_latches`.
- **F88** `src/agentic_fx/plugin/approval.py::run_kind_gate._sink` — harness の行を rows に積み、注入された record_fn があれば同じ行を渡す callback。metrics を含む行がそのまま通る。 Resource: `metric:kill_switch_latches`.
- **F89** `src/agentic_fx/backtest/runner.py::_kill_switch_event` — replay の latched 遷移の reason を、同時刻・同 kind の activity 行の text で置き換える (無ければ既定文)。activity は replay 内の入力として読むだけ。 Resource: `activity:kill_switch_latched`.
- **F90** `src/agentic_fx/store/state.py::<module>._BOOL_FIELDS` — kill_switch_latched を bool 型検証の対象にし、parse_state が bool 以外の状態読込を拒否する。 Resource: `state:app_state.kill_switch_latched`.
- **F91** `src/agentic_fx/store/state.py::<module>._REQUIRED_KEYS` — kill_switch_latched を必須 key にし、欠落した state 文書の読込を parse_state が拒否する。 Resource: `state:app_state.kill_switch_latched`.
- **F92** `src/agentic_fx/service.py::_build_ops_service.status_provider` — 入れ子の status 生成で guard_notice の (pending, generation) を status の guard_notice_pending / guard_notice_generation に投影する。観測であり判断や event 生成には使わない。 Resource: `ops:guard_notice_pending`, `ops:guard_notice_generation`.
- **F93** `src/agentic_fx/ops/client.py::Runner._op_activity` — shell client は `/v1/activity` の lines を kind で選ばず convert_activity_line で換算して表示する。 Resource: `activity:kill_switch_latched`, `activity:kill_switch_latched_durability_uncertain`, `activity:kill_switch_reset`, `activity:kill_switch_reconcile_required`, `activity:kill_switch_reconcile_confirmed`, `activity:kill_switch_reconcile_failed`, `activity:kill_switch_state_uncertain`.
- **F94** `src/agentic_fx/ops/service.py::OpsService.activity` — 認可と limit を検査し、category の filter と tail で activity.log の行を返す。kind 専用の選別はしない。 Resource: `activity:kill_switch_latched`, `activity:kill_switch_latched_durability_uncertain`, `activity:kill_switch_reset`, `activity:kill_switch_reconcile_required`, `activity:kill_switch_reconcile_confirmed`, `activity:kill_switch_reconcile_failed`, `activity:kill_switch_state_uncertain`.
- **F95** `src/agentic_fx/status.py::_activity` — offline status が activity.log の末尾 1 行の先頭 3 欄 (時刻・category・kind) を redact して表示する。 Resource: `activity:kill_switch_latched`, `activity:kill_switch_latched_durability_uncertain`, `activity:kill_switch_reset`, `activity:kill_switch_reconcile_required`, `activity:kill_switch_reconcile_confirmed`, `activity:kill_switch_reconcile_failed`, `activity:kill_switch_state_uncertain`.
- **F96** `src/agentic_fx/core/executor.py::Executor.defer_notifications` — 遅延窓の間の _notify を list に溜め、窓を抜けた後に呼出し元が送る。DD latch の durability 不確実の通知文もこの list に入る。 Resource: `activity:kill_switch_latched_durability_uncertain`.
- **F97** `src/agentic_fx/core/notifier.py::Notifier.send` — 有効な webhook へ本文を 1900 字までに切って POST し、失敗は warning log に留める。 Resource: `activity:kill_switch_latched_durability_uncertain`.
- **F98** `src/agentic_fx/loops/trade_loop.py::TradeLoop._run_once_impl` — commit-core を defer_notifications の中で実行し、抜けた後に溜まった通知文を notifier へ送る。失敗経路も通る。 Resource: `activity:kill_switch_latched_durability_uncertain`.
- **F99** `src/agentic_fx/loops/trade_loop.py::TradeLoop._run_signal_mission` — signal 経路も defer_notifications の list を保持し、外側 finally で通知文を送る。途中 return でも排出される。 Resource: `activity:kill_switch_latched_durability_uncertain`.
- **F100** `afx-ops/afx_ops/display_time.py::kill_switch_reset` — core 側の同名 builder の byte 一致の写し。web と bot 側で reset の activity 本文を換算する。 Resource: `activity:kill_switch_reset`.
- **F101** `src/agentic_fx/core/display_time.py::kill_switch_reset` — shell reset の activity (SYSTEM, kill_switch_reset) の本文を世代と発動時刻から組む。正本はこの file。 Resource: `activity:kill_switch_reset`.
- **F102** `src/agentic_fx/config.py::RiskSettings` — risk 設定の型。drawdown_kill_pct は 0 より大きく 100 以下の float で、省略できない。 Resource: `config:risk.drawdown_kill_pct`.
- **F103** `src/agentic_fx/config.py::Settings._warn_high_drawdown_kill_pct` — drawdown_kill_pct が妥当域の閾値を超えると warning log を出すだけで、値は拒否も変更もしない。 Resource: `config:risk.drawdown_kill_pct`.
- **F104** `src/agentic_fx/ops/events.py::EventStore.poll` — after と limit を検査し、1 つの読取り tx で high watermark と id が after より大きい行を id 順に返す。code は選ばない。 Resource: `event:event_stream_health`, `event:guard_state_changed`, `event:ops_request_terminal`.
- **F105** `src/agentic_fx/ops/service.py::OpsService._project` — mutation 終端の audit を ops_request_terminal へ best-effort で投影する。shell 以外は activity 行も重ねる。 Resource: `event:ops_request_terminal`.
- **F106** `src/agentic_fx/ops/service.py::OpsService.recover_after_journal` — 起動時回復で終端の無い audit を閉じ、各件の ops_request_terminal を best-effort で発行する。 Resource: `event:ops_request_terminal`.
- **F107** `src/agentic_fx/commands.py::Commands._status` — shell の status が latched・世代・発動時刻と、marker があるときの確認要求の行を出す。 Resource: `marker:reset_marker`, `state:app_state.kill_switch_latched`.
- **F108** `src/agentic_fx/ops/client.py::format_status` — `/v1/status` の kill_switch を LATCHED と世代・発動時刻の行に整え、reconcile_required のとき確認要求の行を足す。 Resource: `marker:reset_marker`, `state:app_state.kill_switch_latched`.
- **F109** `afx-ops/afx_ops/metric_words.py::metric_items` — src 側の同名関数と同じ語と桁の写し。kill_switch_latches が 0 以上の int のときだけ回数の項目を足す。 Resource: `metric:kill_switch_latches`.
- **F110** `afx-ops/afx_ops/web/render.py::_baseline_section` — 承認画面の比較表の列に kill_switch_latches を含め、値を escape して表示する。 Resource: `metric:kill_switch_latches`.
- **F111** `src/agentic_fx/commands.py::Commands._comparison_metrics_line` — 比較表の 1 行。8 指標を固定順で出し、kill_switch_latches は値が無ければ `-`、数値でなければ不正形式にする。 Resource: `metric:kill_switch_latches`.
- **F113** `src/agentic_fx/loops/improve_loop.py::ImproveLoop._render_comparison_report_detail` — 比較節の表を、候補の指標の key 順で段・pair ごとに描く。key は固定せず、kill_switch_latches は比較 snapshot に入っていれば表に載る。 Resource: `metric:kill_switch_latches`.
- **F114** `src/agentic_fx/loops/improve_loop.py::ImproveLoop._render_floor_report_detail` — 床の報告の表を、8 指標の列の組 (_FLOOR_REPORT_METRIC_KEYS、kill_switch_latches を含む class 属性) の列で描き、各 pair の kill_switch_latches を並べる。 Resource: `metric:kill_switch_latches`.
- **F115** `src/agentic_fx/metric_words.py::metric_items` — 勝率・max DD・kill switch 回数のうち読める項目だけを返す。kill_switch_latches が 0 以上の int のときだけ回数を足す。 Resource: `metric:kill_switch_latches`.
- **F116** `src/agentic_fx/ops/change.py::_metric_row` — 承認の変更内容の行に、数値と確かめた指標を写す。kill_switch_latches もその 1 つ。 Resource: `metric:kill_switch_latches`.
- **F117** `afx-ops/afx_ops/web/render.py::<module>.ACTIVITY_FIELDS` — web が受け取る activity DTO の field の allowlist。guard_kind を含む。 Resource: `ops:guard_kind`.
- **F118** `src/agentic_fx/ops/service.py::GuardLaneNotHeld` — lane を持たない呼出しの sweep を拒否する例外型。 Resource: `ops:guard_lane_not_held`.
- **F119** `src/agentic_fx/ops/service.py::OpsService._guard_notices_in_lane` — lane の所有を確かめ、持たなければ GuardLaneNotHeld を送出する。 Resource: `ops:guard_lane_not_held`.
- **F120** `src/agentic_fx/ops/service.py::GuardLaneReentry` — lane を持つ thread からの公開入口の呼出しを拒否する例外型。 Resource: `ops:guard_lane_reentry`.
- **F121** `src/agentic_fx/ops/service.py::OpsService._reject_lane_reentry` — 同一 thread が lane を持つなら待たずに GuardLaneReentry を送出する。 Resource: `ops:guard_lane_reentry`.
- **F122** `src/agentic_fx/ops/client.py::Runner._op_killswitch_show` — status の kill_switch から latched・世代・発動時刻を読み、解除の手順に世代を埋めて案内する。 Resource: `state:app_state.kill_switch_generation`, `state:app_state.kill_switch_latched`.
- **F123** `afx-ops/afx_ops/web/render.py::<module>.STATUS_FIELDS` — web が表示する status の top-level field の allowlist。kill_switch を含む。 Resource: `state:app_state.kill_switch_latched`.
- **F124** `afx-ops/afx_ops/web/render.py::<module>._EMPHASIS` — status の field のうち真のとき強調するものの表。kill_switch は danger。 Resource: `state:app_state.kill_switch_latched`.
- **F125** `afx-ops/afx_ops/web/render.py::render_status` — STATUS_FIELDS と _EMPHASIS で status の kill_switch を描き、入れ子の latched・世代・発動時刻は NESTED_FIELDS で通す。 Resource: `state:app_state.kill_switch_latched`.
- **F126** `src/agentic_fx/commands.py::Commands._projected_state` — store.load の marker 投影後の state を返す。 Resource: `state:app_state.kill_switch_latched`.
- **F127** `src/agentic_fx/status.py::render_status` — offline status が `state:` 行に read_state の kill_switch の値を載せる。 Resource: `state:app_state.kill_switch_latched`.
- **F128** `src/agentic_fx/store/state.py::StateStore.file_value` — marker を無視した file のままの state を共有 lock 下で返す。reconcile の表示用。 Resource: `state:app_state.kill_switch_latched`.

## K — 守る契約

契約の正本は [通知設計 C4](../../superpowers/specs/2026-11-14-ops-alert-delivery-design.md) §2、§4、§6、§10 と [HWM rebase 設計](../../superpowers/specs/2026-11-14-killswitch-hwm-rebase-design.md) §1–2、§4–5。以下の F は現状の根拠であり、test 名は検証候補であって今回の実行結果ではない。

- **K-LATCH**: live / replay の open gate で DD を判定し、理由文字列 producer と部分一致 consumer の互換を守る。通知失敗で成立済み state を戻さない（F01–F02、`tests/core/test_executor.py`）。
- **K-REPEAT**: 同世代の単独 true は世代を増やさない。拒否と activity の反復は別であり C4 は一件化しない（F02、F04）。
- **K-NOTICE**: 単一 lane の sweep で現世代を照合し、発動時刻を下限とする。clear と event 無しから履歴を作らず、通知失敗は操作・監査を変えない。全履歴復元や shell mutation の世代間全順序は保証しない（F07–F12、F17–F18、F21）。
- **K-RESET**: ops reset は認可、世代一致、marker 不在、latched を前提とし、API は autopilot 中を拒否する。marker→保存→削除の順と世代・発動時刻を保持する。shell は直接 mutation のまま wrapper を使い、通知失敗を隔離する（F05、F07、F13、`tests/ops/test_ac51_killswitch_api.py`）。
- **K-RECONCILE**: marker がある場合は安全側へ確定し、raw false なら次世代、raw true なら同世代とする。marker 中の DD update 後も raw true なら世代を増やさない（F04、F06、F08、F14、`tests/store/test_state.py`）。
- **K-START**: 起動時 marker を安全側に投影し確認を求める。監査回復の terminal は guard 回復の代用ではない。起動と tick の sweep を行う（F20–F21）。
- **K-LANE**: lane の同一 thread 再入は待たず拒否し、所有なしの sweep も拒否する（F10、F12）。
- **K-VIEW**: pending と対象世代は短命 read-only 接続の観測で、取引判断に使わない。web は status/activity を表示し、event 配送成功と混同しない（F20、F32–F34、F36）。
- **K-CONSUME**: bot と notify は各 cursor を持ち送信後に保存する。送信後保存前の crash による重複は許容する。C4 の無進捗・恒久 4xx の終了と回復例外は同設計 §5.2 に従う（F28–F31）。
- **K-REPLAY**: replay の理由、artifact、比較 metric と解除時補償を保つ。backtest から手動 rebase module・ledger・source を参照しない（F25–F27、HWM §2.4–2.5）。
- **K-RESET-UI**: shell と ctl は reset 成功時に固定警告を表示する。ctl の rebase 案内は HWM 実装の契約であり、この revision では未実装（F13、K-REBASE）。
- **K-REBASE**: 承認済み・未実装（branch で実装中）。独立した approver の明示操作で、PaperBroker、注文状態、新鮮 snapshot、DD、世代、autopilot、閉場を検査する。HWM のみ変更し latch・世代・時刻を保持、値 CAS と ledger/snapshot の原子性を守る。通常 reset や shell/bot/web に入口を加えない。詳細と認可境界は HWM 設計 §1–2、§4–5 を正本とする（F23–F24 は既存入力の根拠）。

## D — 既知の逸脱と残余リスク

- **D01**（F02、F09–F12、K-NOTICE）自動 latch の直送はない。C4 の起動・tick sweep が回収するが、両側の sweep 失敗後に clear になった履歴や retention 外の完全復元は保証しない。[通知 ticket](../../tickets/closed/auto-killswitch-latch-emits-no-event.md)。
- **D02**（F13–F14、K-RESET）shell mutation は lane 外であり、wrapper は通知を補うが mutation を lane に移さない。世代間の全順序は保証しない。
- **D03**（F02、F04、K-REPEAT）state は no-op でも latch activity は繰り返し得る。[反復 ticket](../../tickets/open/killswitch-latched-activity-repeats.md) の解消まで区別する。
- **D04**（F15–F16）shell data resume は lane と guard event を迂回する。是正 ticket は未起票。停止自体の guard event 不足は別の [data feed ticket](../../tickets/open/datafeed-outage-discord-notice.md) で扱う。
- **D05**（F17–F18）events module の広い best-effort 説明に対し、emit は例外を伝え、emit_best_effort も事前検証例外を伝える。文書修正の所有 ticket は未確定。
- **D06**（F32、K-VIEW）独立 status と StateStore の marker path 解決は異なる。親 directory・lock 不在・例外・既定値・symlink の組合せを合成 fixture で比較するまで同一と断定しない。
- **D07**（F01–F02、K-LATCH）DD 理由文字列は暗黙 protocol。producer/consumer の挙動 test が必要で、型付き理由への変更は別途扱う。
- **D08**（F23–F24、K-REBASE）live rebase の入口・ledger・排他・CAS は未着地。[HWM ticket](../../tickets/open/killswitch-reset-hwm-rebase.md) の実装と受入で更新する。
- **D09**（F28–F31、K-CONSUME）配送と status は独立し、実通知の成功は未確認。bot health の運用確認は [関連 ticket](../../tickets/closed/bot-gateway-disconnect-is-silent.md) を参照。

## 手で保守する辺

- **E01** `src/agentic_fx/loops/trade_loop.py::TradeLoop._run_once_impl` → `src/agentic_fx/core/executor.py::Executor.open_from_snapshot`; resource `state:app_state.kill_switch_latched`。live 定期入口。F01–F02、K-LATCH。
- **E02** `src/agentic_fx/loops/trade_loop.py::TradeLoop._run_signal_mission` → `src/agentic_fx/core/executor.py::Executor.open_from_snapshot`; resource `state:app_state.kill_switch_latched`。live signal 入口。F01–F02、K-LATCH。
- **E03** `src/agentic_fx/backtest/runner.py::run_replay` → `src/agentic_fx/core/executor.py::Executor.handle_intent`; resource `state:app_state.kill_switch_latched`。replay intent 入口。F02、F26、K-REPLAY。
- **E04** `src/agentic_fx/core/executor.py::Executor.handle_intent` → `src/agentic_fx/core/executor.py::Executor._open`; resource `state:app_state.kill_switch_latched`。replay open へ分岐。F02、K-LATCH。
- **E05** `src/agentic_fx/core/executor.py::Executor._open` → `src/agentic_fx/core/executor.py::Executor._evaluate_and_execute_open`; resource `config:risk.drawdown_kill_pct`。共有 gate へ合流。F01–F02、K-LATCH。
- **E06** `src/agentic_fx/core/executor.py::Executor.open_from_snapshot` → `src/agentic_fx/core/executor.py::Executor._evaluate_and_execute_open`; resource `config:risk.drawdown_kill_pct`。live snapshot から共有 gate へ合流。F01–F02、K-LATCH。
- **E07** `src/agentic_fx/commands.py::Commands._data_resume` → `src/agentic_fx/datafeed/outage.py::OutageStateMachine.request_resume`; resource なし。shell の直接呼出し。復旧要求を永続化するだけで guard event は出さず、event の生成は API 側 resume_data にだけある (resource なし)。F15、D04。
- **E08** `src/agentic_fx/service.py::_build_ops_service` → `src/agentic_fx/ops/service.py::OpsService.resume_data`; resource `event:guard_state_changed`。data_resume callback を注入。F16、F36、D04。
- **E17** `src/agentic_fx/service.py::build_app#_build_ops_service` → `src/agentic_fx/service.py::_build_ops_service`; resource `event:guard_state_changed`。ops service と callback の構築を委譲。F20、F36、D04。
- **E09** `src/agentic_fx/ops/service.py::OpsService.resume_data` → `src/agentic_fx/ops/service.py::OpsService._guard_event`; resource `event:guard_state_changed`。callback 成功後に data_feed を通知。F16、D04。
- **E10** `src/agentic_fx/ops/service.py::OpsService.reset_kill_switch` → `src/agentic_fx/ops/service.py::OpsService._guard_step`; resource `event:guard_state_changed`。前後と現 state を照合。F07、F11、K-NOTICE。
- **E11** `src/agentic_fx/ops/service.py::OpsService.reconcile_kill_switch` → `src/agentic_fx/ops/service.py::OpsService._guard_step`; resource `event:guard_state_changed`。前後と現 state を照合。F08、F11、K-RECONCILE。
- **E12** `src/agentic_fx/ops/service.py::OpsService._guard_step` → `src/agentic_fx/ops/service.py::OpsService._sweep_kill_switch`; resource `event:guard_state_changed`。lane 内で通知を照合。F10–F11、K-NOTICE。
- **E13** `src/agentic_fx/commands.py::Commands._dispatch#_emit_guard_notices` → `src/agentic_fx/commands.py::Commands._emit_guard_notices`; resource `event:guard_state_changed`。shell reset の前後 wrapper。F12–F13、D02。
- **E14** `src/agentic_fx/commands.py::Commands._killswitch_reconcile` → `src/agentic_fx/commands.py::Commands._emit_guard_notices`; resource `event:guard_state_changed`。shell confirm の前後 wrapper。F12、F14、D02。
- **E15** `src/agentic_fx/commands.py::Commands._emit_guard_notices` → `src/agentic_fx/ops/service.py::OpsService.emit_pending_guard_notices`; resource `event:guard_state_changed`。lane を取得する公開 wrapper。F12–F14、D02。
- **E16** `src/agentic_fx/service.py::build_app#emit_pending_notices` → `src/agentic_fx/ops/service.py::OpsService.emit_pending_notices`; resource `event:guard_state_changed`。起動時 sweep。F20–F21、K-START。

- **E18** `src/agentic_fx/core/scheduler.py::Scheduler._mark_to_market` → `src/agentic_fx/core/scheduler.py::Scheduler._evaluate_positions`; resource `db:account_snapshots`。時価評価を委譲。評価不能な tick は snapshot を記録しない。F23。
- **E19** `src/agentic_fx/core/scheduler.py::Scheduler._evaluate_positions` → `src/agentic_fx/core/paper_broker.py::PaperBroker.equity`; resource `db:account_snapshots`。executor.broker.equity() の動的呼出し。live 配線では PaperBroker で、orders の実現損益と swap 費用から balance を得る。snapshot を直接読まず、後段で書く balance の源。F23。
- **E20** `src/agentic_fx/core/scheduler.py::Scheduler._mark_to_market` → `src/agentic_fx/core/accounting.py::record_snapshot`; resource `db:account_snapshots`。stale・例外のとき以外に balance と含み損益込みの equity を記録する。時系列逆行 (ValueError) は tick を中断する。F23。
- **E21** `src/agentic_fx/core/scheduler.py::Scheduler._tick_body` → `src/agentic_fx/core/scheduler.py::Scheduler._mark_to_market`; resource `db:account_snapshots`。開場中の tick で時価 snapshot を先に記録する。F23。
- **E22** `src/agentic_fx/core/scheduler.py::Scheduler.tick` → `src/agentic_fx/core/scheduler.py::Scheduler._tick_body`; resource `db:account_snapshots`。tick の本体へ委譲する。F23。
- **E23** `src/agentic_fx/service.py::_scheduler_tick_body` → `src/agentic_fx/core/scheduler.py::Scheduler.tick`; resource `db:account_snapshots`。scheduler_thread の一 tick。資金保護のため例外でも tick を走らせる。F23。
- **E24** `src/agentic_fx/loops/trade_loop.py::TradeLoop.run_once` → `src/agentic_fx/loops/trade_loop.py::TradeLoop._run_once_impl`; resource `state:app_state.kill_switch_latched`。周期境界で例外を握り、本体へ委譲する。E01 へ続く。F01、K-LATCH。
- **E25** `src/agentic_fx/service.py::build_app._trade_fn` → `src/agentic_fx/loops/trade_loop.py::TradeLoop.run_once`; resource `state:app_state.kill_switch_latched`。scheduler の trade callback。lock は TradeLoop 側で扱う。E24 へ続く。F01、K-LATCH。
- **E26** `src/agentic_fx/commands.py::Commands._dispatch#reset_kill_switch` → `src/agentic_fx/store/state.py::StateStore.reset_kill_switch`; resource `state:app_state.kill_switch_latched`, `state:app_state.kill_switch_generation`。shell の killswitch reset が世代を引数に直接呼ぶ。世代一致を store が検査する。F05、F13、K-RESET。
- **E27** `src/agentic_fx/ops/api_server.py::_call` → `src/agentic_fx/ops/service.py::OpsService.reset_kill_switch`; resource `state:app_state.kill_switch_latched`, `state:app_state.kill_switch_generation`。killswitch_reset route を expected_generation 付きで呼ぶ。F05、F07、K-RESET。
- **E28** `src/agentic_fx/ops/api_server.py::_call` → `src/agentic_fx/ops/service.py::OpsService.reconcile_kill_switch`; resource `marker:reset_marker`。killswitch_reconcile route から呼ぶ。F06、F08、K-RECONCILE。
- **E29** `src/agentic_fx/ops/client.py::Runner._op_killswitch_reset` → `src/agentic_fx/ops/api_server.py::_call`; resource `state:app_state.kill_switch_latched`, `state:app_state.kill_switch_generation`。afx ctl が status で確認後、POST /v1/killswitch/reset で到達する。別 process で HTTP を介し、直接の呼出しではない。F07、K-RESET。
- **E30** `src/agentic_fx/ops/client.py::Runner._op_killswitch_reconcile` → `src/agentic_fx/ops/api_server.py::_call`; resource `marker:reset_marker`。afx ctl が確認後、POST /v1/killswitch/reconcile で到達する。別 process で HTTP を介す。F08、K-RECONCILE。
- **E31** `src/agentic_fx/ops/service.py::OpsService._guard_notices_in_lane` → `src/agentic_fx/ops/service.py::OpsService._guard_step`; resource `event:guard_state_changed`。lane 所有を確認し、操作前の after と現 state を別々の step で照合する。F11、F12、K-LANE。
- **E32** `src/agentic_fx/service.py::run_service.scheduler_thread` → `src/agentic_fx/service.py::_sweep_improve_notices`; resource `event:guard_state_changed`。tick の後に毎回呼ぶ。F21、K-START。
- **E33** `src/agentic_fx/service.py::_sweep_improve_notices` → `src/agentic_fx/ops/service.py::OpsService.emit_pending_notices`; resource `event:guard_state_changed`。改善通知の設定にかかわらず guard 側も回し、例外は警告だけで tick を止めない。F21、K-START。
- **E34** `afx-ops/afx_ops/bot/runtime.py::_poll` → `afx-ops/afx_ops/bot/adapter.py::BotAdapter.poll_once`; resource `event:guard_state_changed`。bot の周回。poll_begin と poll_end で包み、interval 待つ。F29。
- **E35** `afx-ops/afx_ops/bot/adapter.py::BotAdapter.poll_once` → `afx-ops/afx_ops/bot/adapter.py::BotAdapter.deliver_events`; resource `event:guard_state_changed`。event 配送を最初に行い、その後に承認・view の同期を行う。F29。
- **E36** `afx-ops/afx_ops/bot/adapter.py::BotAdapter._plan` → `afx-ops/afx_ops/gate/service.py::GateService.plan_event`; resource `event:guard_state_changed`。bot の設定 (mention、取引・ops・improve の通知設定) を kwargs で注入し、設定の分岐はここだけにある。F74。
- **E37** `afx-ops/afx_ops/notify/runtime.py::_Loop.run` → `afx-ops/afx_ops/notify/runtime.py::_Loop._poll_once`; resource `event:guard_state_changed`。webhook 検証後に state を読み、満杯 page の進捗があるときだけ待たず続ける。F31。
- **E38** `src/agentic_fx/backtest/cli.py::_backtest_run_plugin` → `src/agentic_fx/store/backtest_runs.py::save_human_run`; resource `metric:kill_switch_latches`。手元の plugin 評価。run_replay の結果を compute_metrics で集計し、人間の run として保存する。F27、F64、F65、K-REPLAY。
- **E39** `src/agentic_fx/backtest/cli.py::_backtest_run_proposal` → `src/agentic_fx/store/backtest_runs.py::save_human_run`; resource `metric:kill_switch_latches`。提案 file の評価。同じく compute_metrics の結果を人間の run として保存する。F27、F64、F65、K-REPLAY。
- **E40** `src/agentic_fx/backtest/holdout.py::_run_scope` → `src/agentic_fx/backtest/runner.py::run_replay`; resource `metric:kill_switch_latches`。期間 scope の replay を走らせ、compute_metrics の結果を harness の run として保存する (record_fn があれば差し替え)。F26、F27、K-REPLAY。
- **E41** `src/agentic_fx/loops/improve_loop.py::ImproveLoop._build_rpc_handlers.run_backtest_handler` → `src/agentic_fx/backtest/holdout.py::run_in_sample`; resource `metric:kill_switch_latches`。改善 loop の run_backtest RPC が in-sample 評価を呼ぶ。E40 へ続く。F27、K-REPLAY。
- **E42** `src/agentic_fx/plugin/strategy_gate.py::evaluate_strategy_adoption_gate#run_holdout` → `src/agentic_fx/backtest/holdout.py::run_holdout_gate`; resource `metric:kill_switch_latches`。採用 gate が holdout 評価を呼ぶ (in-sample も同様)。run_holdout_gate_fn で差し替え可能。E40 へ続く。F27、K-REPLAY。
- **E43** `src/agentic_fx/plugin/strategy_gate.py::_build_comparison` → `src/agentic_fx/plugin/strategy_gate.py::_project`; resource `metric:kill_switch_latches`。候補と配備済みの指標を比較表示用の key へ射影する。F66、F67。
- **E44** `afx-ops/afx_ops/bot/adapter.py::_page` → `afx-ops/afx_ops/events_page.py::parse_page`; resource `event:guard_state_changed`, `event:ops_request_terminal`, `event:event_stream_health`。bot の page wrapper。BotFailure を注入して共通 parser に渡す。F81。
- **E45** `afx-ops/afx_ops/notify/runtime.py::_page` → `afx-ops/afx_ops/events_page.py::parse_page`; resource `event:guard_state_changed`, `event:ops_request_terminal`, `event:event_stream_health`。notify の page wrapper。_Failure を注入して共通 parser に渡す。F81。
- **E46** `src/agentic_fx/loops/improve_loop.py::ImproveLoop._persist_gate_rows` → `src/agentic_fx/store/backtest_runs.py::save_harness_run`; resource `metric:kill_switch_latches`。gate の行を harness run として保存する。F86、K-REPLAY。
- **E47** `src/agentic_fx/loops/improve_loop.py::ImproveLoop._persist_ledger_rows` → `src/agentic_fx/store/backtest_runs.py::save_harness_run`; resource `metric:kill_switch_latches`。台帳の run_backtest 行を harness run として保存する。F87、K-REPLAY。
- **E48** `src/agentic_fx/loops/improve_loop.py::ImproveLoop._run_strategy_gate` → `src/agentic_fx/plugin/strategy_gate.py::evaluate_strategy_adoption_gate`; resource `metric:kill_switch_latches`。改善候補の採用 gate を呼び、record_fn・evaluation_sink・compare_mode を中継する。E42 へ続く。K-REPLAY。
- **E49** `src/agentic_fx/plugin/approval.py::run_kind_gate` → `src/agentic_fx/plugin/strategy_gate.py::evaluate_strategy_adoption_gate`; resource `metric:kill_switch_latches`。strategy 分岐で採用 gate を呼び、結果行を _sink 経由で rows と record_fn に流す。F88、E42 へ続く。K-REPLAY。
- **E50** `src/agentic_fx/plugin/switch.py::_evaluate_deployed` → `src/agentic_fx/plugin/strategy_gate.py::evaluate_strategy_adoption_gate`; resource `metric:kill_switch_latches`。配備済み strategy の再評価入口。record_fn に rows.append を渡して結果行を集める。E42 へ続く。K-REPLAY。
- **E51** `src/agentic_fx/core/scheduler.py::Scheduler._tick_body` → `src/agentic_fx/service.py::build_app.on_trade_mission`; resource `state:app_state.kill_switch_latched`。trade mission の起動理由と decision_bars を注入 callback に渡す。callback は composition root が配線する。K-LATCH。
- **E52** `src/agentic_fx/service.py::build_app.on_trade_mission` → `src/agentic_fx/core/supervisor.py::MissionSupervisor.try_submit`; resource `state:app_state.kill_switch_latched`。signal_max_id を固定し、kind=trade と trigger を supervisor に投入する。E51 から続く。K-LATCH。
- **E53** `src/agentic_fx/core/supervisor.py::MissionSupervisor.try_submit` → `src/agentic_fx/core/supervisor.py::MissionSupervisor._run`; resource `state:app_state.kill_switch_latched`。受理した job を queue に入れ、別 thread の _run が取り出す。直接の呼出しではない。K-LATCH。
- **E54** `src/agentic_fx/core/supervisor.py::MissionSupervisor._run` → `src/agentic_fx/core/supervisor.py::MissionSupervisor._dispatch`; resource `state:app_state.kill_switch_latched`。queue から取り出した kind と kwargs で _dispatch を実行する。K-LATCH。
- **E55** `src/agentic_fx/core/supervisor.py::MissionSupervisor._dispatch` → `src/agentic_fx/service.py::build_app._trade_fn`; resource `state:app_state.kill_switch_latched`。kind=trade で trigger と残りの引数を注入された trade_fn に渡す。E25 へ続く。K-LATCH。
- **E56** `src/agentic_fx/backtest/runner.py::run_replay` → `src/agentic_fx/backtest/runner.py::_kill_switch_event`; resource `activity:kill_switch_latched`。遷移ごとに activity.entries を渡し、latched の理由を activity の text から取り込む。F26、F89、K-REPLAY。
- **E57** `src/agentic_fx/core/executor.py::Executor._notify` → `src/agentic_fx/core/notifier.py::Notifier.send`; resource `activity:kill_switch_latched_durability_uncertain`。遅延中でなければ通知を即時送信する。F49、F96、F97。
- **E58** `src/agentic_fx/ops/api_server.py::_call` → `src/agentic_fx/ops/service.py::OpsService.events_after`; resource `event:guard_state_changed`, `event:ops_request_terminal`, `event:event_stream_health`。route events を after と limit だけで呼び、code は選ばない。F79、F80。
- **E59** `src/agentic_fx/ops/service.py::OpsService.events_after` → `src/agentic_fx/ops/events.py::EventStore.poll`; resource `event:guard_state_changed`。lane 内で poll を呼び page を返す。F104。
- **E60** `src/agentic_fx/service.py::_GuardNoticeProbe.__call__` → `src/agentic_fx/ops/service.py::plan_kill_switch_notice`; resource `event:guard_state_changed`。status 用に現世代の欠けを読取り接続で照合する。補助表示で失敗は status を止めない。F09、F92、K-NOTICE。
- **E61** `src/agentic_fx/ops/service.py::OpsService._mutate` → `src/agentic_fx/ops/service.py::OpsService._terminal`; resource `event:ops_request_terminal`。mutation の終端で terminal を書く。F19、F105。
- **E62** `src/agentic_fx/service.py::_recover_ops` → `src/agentic_fx/ops/service.py::OpsService.recover_after_journal`; resource `event:ops_request_terminal`。起動時回復を呼び、失敗なら listener を開かない。F106。
- **E63** `src/agentic_fx/service.py::build_app#_recover_ops` → `src/agentic_fx/service.py::_recover_ops`; resource `event:ops_request_terminal`。plugin 切替 journal の reconcile の後に呼ぶ。F20。
- **E64** `src/agentic_fx/commands.py::Commands._baseline_comparison_lines` → `src/agentic_fx/commands.py::Commands._comparison_metrics_line`; resource `metric:kill_switch_latches`。保存済み snapshot の候補と配備中の版の指標を 1 行ずつ描く。F111。
- **E65** `src/agentic_fx/status.py::collect_status` → `src/agentic_fx/status.py::read_state`; resource `state:app_state.kill_switch_latched`。offline status が kill_switch を read_state から得る。読めなければ unavailable。F32。

## 影響先と検証記録

変更時は gate の理由文字列、store の marker/raw、ops と shell の入口、event schema、bot/notify の cursor、status/activity、replay artifact と比較 metric を別々に照合する。literal 索引は callback や引数伝播を証明しない。`ops:guard_version` は宣言された literal だが、guard event schema の version と結び付くことは未確認である。

基準 `0dbcb27f` と作業 tree の `src/`・`afx-ops/` 差分が空であることを確認した。source の読取による下書きであり、test、独立 oracle、実通知は未実施。頁セルは凍結表の critical 行を被覆宣言で全件対応づけたが、宣言は存在の照合で意味の証明ではなく、F39 以降は source 読取りのみで test を実行していない。status と StateStore の全分岐比較、reset crash 統合 test の網羅は未確認。

<!-- impact-map:covers -->
```json
{
 "covers": [
  {
   "symbol": "afx-ops/afx_ops/bot/adapter.py::BotAdapter.deliver_events",
   "resource": "event:event_stream_health",
   "ids": [
    "F29"
   ],
   "kind": "page"
  },
  {
   "symbol": "afx-ops/afx_ops/bot/adapter.py::BotAdapter.deliver_events",
   "resource": "event:guard_state_changed",
   "ids": [
    "F29"
   ],
   "kind": "page"
  },
  {
   "symbol": "afx-ops/afx_ops/bot/adapter.py::BotAdapter.deliver_events",
   "resource": "event:ops_request_terminal",
   "ids": [
    "F29"
   ],
   "kind": "page"
  },
  {
   "symbol": "afx-ops/afx_ops/gate/render.py::_event_text_for_code",
   "resource": "event:guard_state_changed",
   "ids": [
    "F28"
   ],
   "kind": "page"
  },
  {
   "symbol": "afx-ops/afx_ops/gate/render.py::_event_text_for_code",
   "resource": "event:ops_request_terminal",
   "ids": [
    "F28"
   ],
   "kind": "page"
  },
  {
   "symbol": "afx-ops/afx_ops/notify/payload.py::encode_event",
   "resource": "event:event_stream_health",
   "ids": [
    "F30"
   ],
   "kind": "page"
  },
  {
   "symbol": "afx-ops/afx_ops/notify/payload.py::encode_event",
   "resource": "event:guard_state_changed",
   "ids": [
    "F30"
   ],
   "kind": "page"
  },
  {
   "symbol": "afx-ops/afx_ops/notify/payload.py::encode_event",
   "resource": "event:ops_request_terminal",
   "ids": [
    "F30"
   ],
   "kind": "page"
  },
  {
   "symbol": "afx-ops/afx_ops/notify/runtime.py::_Loop._poll_once",
   "resource": "event:event_stream_health",
   "ids": [
    "F31"
   ],
   "kind": "page"
  },
  {
   "symbol": "afx-ops/afx_ops/notify/runtime.py::_Loop._poll_once",
   "resource": "event:guard_state_changed",
   "ids": [
    "F31"
   ],
   "kind": "page"
  },
  {
   "symbol": "afx-ops/afx_ops/notify/runtime.py::_Loop._poll_once",
   "resource": "event:ops_request_terminal",
   "ids": [
    "F31"
   ],
   "kind": "page"
  },
  {
   "symbol": "afx-ops/afx_ops/web/render.py::render_activity",
   "resource": "activity:kill_switch_reset",
   "ids": [
    "F33"
   ],
   "kind": "page"
  },
  {
   "symbol": "src/agentic_fx/backtest/metrics.py::compute_metrics",
   "resource": "metric:kill_switch_latches",
   "ids": [
    "F27"
   ],
   "kind": "page"
  },
  {
   "symbol": "src/agentic_fx/backtest/runner.py::_auto_release_kill_switch",
   "resource": "db:account_snapshots",
   "ids": [
    "F25"
   ],
   "kind": "page"
  },
  {
   "symbol": "src/agentic_fx/backtest/runner.py::_auto_release_kill_switch",
   "resource": "state:app_state.kill_switch_latched",
   "ids": [
    "F25"
   ],
   "kind": "page"
  },
  {
   "symbol": "src/agentic_fx/backtest/runner.py::run_replay",
   "resource": "db:account_snapshots",
   "ids": [
    "F26"
   ],
   "kind": "page"
  },
  {
   "symbol": "src/agentic_fx/backtest/runner.py::run_replay",
   "resource": "metric:kill_switch_events",
   "ids": [
    "F26"
   ],
   "kind": "page"
  },
  {
   "symbol": "src/agentic_fx/backtest/runner.py::run_replay",
   "resource": "metric:kill_switch_latches",
   "ids": [
    "F26"
   ],
   "kind": "page"
  },
  {
   "symbol": "src/agentic_fx/backtest/runner.py::run_replay",
   "resource": "state:app_state.kill_switch_latched",
   "ids": [
    "E03"
   ],
   "kind": "manual_edge"
  },
  {
   "symbol": "src/agentic_fx/commands.py::Commands._emit_guard_notices",
   "resource": "event:guard_state_changed",
   "ids": [
    "E13",
    "E14",
    "E15"
   ],
   "kind": "manual_edge"
  },
  {
   "symbol": "src/agentic_fx/commands.py::Commands._killswitch_reconcile",
   "resource": "activity:kill_switch_reconcile_confirmed",
   "ids": [
    "F14"
   ],
   "kind": "page"
  },
  {
   "symbol": "src/agentic_fx/commands.py::Commands._killswitch_reconcile",
   "resource": "activity:kill_switch_reconcile_failed",
   "ids": [
    "F14"
   ],
   "kind": "page"
  },
  {
   "symbol": "src/agentic_fx/commands.py::Commands._killswitch_reconcile",
   "resource": "event:guard_state_changed",
   "ids": [
    "E14"
   ],
   "kind": "manual_edge"
  },
  {
   "symbol": "src/agentic_fx/commands.py::Commands._killswitch_reconcile",
   "resource": "event:guard_state_changed",
   "ids": [
    "F14"
   ],
   "kind": "page"
  },
  {
   "symbol": "src/agentic_fx/commands.py::Commands._killswitch_reconcile",
   "resource": "marker:reset_marker",
   "ids": [
    "F14"
   ],
   "kind": "page"
  },
  {
   "symbol": "src/agentic_fx/commands.py::Commands._dispatch",
   "resource": "activity:kill_switch_reset",
   "ids": [
    "F13"
   ],
   "kind": "page"
  },
  {
   "symbol": "src/agentic_fx/commands.py::Commands._dispatch",
   "resource": "activity:kill_switch_state_uncertain",
   "ids": [
    "F13"
   ],
   "kind": "page"
  },
  {
   "symbol": "src/agentic_fx/commands.py::Commands._dispatch",
   "resource": "event:guard_state_changed",
   "ids": [
    "E13"
   ],
   "kind": "manual_edge"
  },
  {
   "symbol": "src/agentic_fx/commands.py::Commands._dispatch",
   "resource": "event:guard_state_changed",
   "ids": [
    "F13"
   ],
   "kind": "page"
  },
  {
   "symbol": "src/agentic_fx/core/accounting.py::current_account",
   "resource": "db:account_snapshots",
   "ids": [
    "F24"
   ],
   "kind": "page"
  },
  {
   "symbol": "src/agentic_fx/core/accounting.py::record_snapshot",
   "resource": "db:account_snapshots",
   "ids": [
    "F23"
   ],
   "kind": "page"
  },
  {
   "symbol": "src/agentic_fx/core/executor.py::Executor._evaluate_and_execute_open",
   "resource": "activity:kill_switch_latched",
   "ids": [
    "F02"
   ],
   "kind": "page"
  },
  {
   "symbol": "src/agentic_fx/core/executor.py::Executor._evaluate_and_execute_open",
   "resource": "activity:kill_switch_latched_durability_uncertain",
   "ids": [
    "F02"
   ],
   "kind": "page"
  },
  {
   "symbol": "src/agentic_fx/core/executor.py::Executor._evaluate_and_execute_open",
   "resource": "config:risk.drawdown_kill_pct",
   "ids": [
    "E05",
    "E06"
   ],
   "kind": "manual_edge"
  },
  {
   "symbol": "src/agentic_fx/core/executor.py::Executor._evaluate_and_execute_open",
   "resource": "state:app_state.kill_switch_latched",
   "ids": [
    "F02"
   ],
   "kind": "page"
  },
  {
   "symbol": "src/agentic_fx/core/executor.py::Executor._open",
   "resource": "config:risk.drawdown_kill_pct",
   "ids": [
    "E05"
   ],
   "kind": "manual_edge"
  },
  {
   "symbol": "src/agentic_fx/core/executor.py::Executor._open",
   "resource": "state:app_state.kill_switch_latched",
   "ids": [
    "E04"
   ],
   "kind": "manual_edge"
  },
  {
   "symbol": "src/agentic_fx/core/executor.py::Executor.handle_intent",
   "resource": "state:app_state.kill_switch_latched",
   "ids": [
    "E03",
    "E04"
   ],
   "kind": "manual_edge"
  },
  {
   "symbol": "src/agentic_fx/core/executor.py::Executor.open_from_snapshot",
   "resource": "config:risk.drawdown_kill_pct",
   "ids": [
    "E06"
   ],
   "kind": "manual_edge"
  },
  {
   "symbol": "src/agentic_fx/core/executor.py::Executor.open_from_snapshot",
   "resource": "state:app_state.kill_switch_latched",
   "ids": [
    "E01",
    "E02"
   ],
   "kind": "manual_edge"
  },
  {
   "symbol": "src/agentic_fx/core/risk_gate.py::evaluate",
   "resource": "config:risk.drawdown_kill_pct",
   "ids": [
    "F01"
   ],
   "kind": "page"
  },
  {
   "symbol": "src/agentic_fx/core/risk_gate.py::evaluate",
   "resource": "state:app_state.kill_switch_latched",
   "ids": [
    "F01"
   ],
   "kind": "page"
  },
  {
   "symbol": "src/agentic_fx/loops/trade_loop.py::TradeLoop._run_once_impl",
   "resource": "state:app_state.kill_switch_latched",
   "ids": [
    "E01"
   ],
   "kind": "manual_edge"
  },
  {
   "symbol": "src/agentic_fx/loops/trade_loop.py::TradeLoop._run_signal_mission",
   "resource": "state:app_state.kill_switch_latched",
   "ids": [
    "E02"
   ],
   "kind": "manual_edge"
  },
  {
   "symbol": "src/agentic_fx/ops/events.py::<module>.FIELDS",
   "resource": "event:guard_state_changed",
   "ids": [
    "F35"
   ],
   "kind": "page"
  },
  {
   "symbol": "src/agentic_fx/ops/events.py::<module>.FIELDS",
   "resource": "ops:guard_kind",
   "ids": [
    "F35"
   ],
   "kind": "page"
  },
  {
   "symbol": "src/agentic_fx/ops/events.py::EventStore.emit",
   "resource": "event:guard_state_changed",
   "ids": [
    "F18"
   ],
   "kind": "page"
  },
  {
   "symbol": "src/agentic_fx/ops/events.py::EventStore.emit_best_effort",
   "resource": "event:event_stream_health",
   "ids": [
    "F17"
   ],
   "kind": "page"
  },
  {
   "symbol": "src/agentic_fx/ops/events.py::EventStore.emit_best_effort",
   "resource": "event:guard_state_changed",
   "ids": [
    "F17"
   ],
   "kind": "page"
  },
  {
   "symbol": "src/agentic_fx/ops/service.py::OpsService._current_state",
   "resource": "ops:guard_state_unavailable",
   "ids": [
    "F22"
   ],
   "kind": "page"
  },
  {
   "symbol": "src/agentic_fx/ops/service.py::OpsService._current_state",
   "resource": "state:app_state.kill_switch_latched",
   "ids": [
    "F22"
   ],
   "kind": "page"
  },
  {
   "symbol": "src/agentic_fx/ops/service.py::OpsService._guard_event",
   "resource": "event:guard_state_changed",
   "ids": [
    "E09"
   ],
   "kind": "manual_edge"
  },
  {
   "symbol": "src/agentic_fx/ops/service.py::OpsService._guard_step",
   "resource": "event:guard_state_changed",
   "ids": [
    "E10",
    "E11",
    "E12"
   ],
   "kind": "manual_edge"
  },
  {
   "symbol": "src/agentic_fx/ops/service.py::OpsService._guard_step",
   "resource": "event:guard_state_changed",
   "ids": [
    "F11"
   ],
   "kind": "page"
  },
  {
   "symbol": "src/agentic_fx/ops/service.py::OpsService._sweep_kill_switch",
   "resource": "log:guard_notice_backfill_skipped",
   "ids": [
    "F10"
   ],
   "kind": "page"
  },
  {
   "symbol": "src/agentic_fx/ops/service.py::OpsService._sweep_kill_switch",
   "resource": "event:guard_state_changed",
   "ids": [
    "E12"
   ],
   "kind": "manual_edge"
  },
  {
   "symbol": "src/agentic_fx/ops/service.py::OpsService._sweep_kill_switch",
   "resource": "event:guard_state_changed",
   "ids": [
    "F10"
   ],
   "kind": "page"
  },
  {
   "symbol": "src/agentic_fx/ops/service.py::OpsService._sweep_kill_switch",
   "resource": "ops:guard_lane_not_held",
   "ids": [
    "F10"
   ],
   "kind": "page"
  },
  {
   "symbol": "src/agentic_fx/ops/service.py::OpsService._terminal",
   "resource": "event:ops_request_terminal",
   "ids": [
    "F19"
   ],
   "kind": "page"
  },
  {
   "symbol": "src/agentic_fx/ops/service.py::OpsService.emit_pending_guard_notices",
   "resource": "event:guard_state_changed",
   "ids": [
    "E15"
   ],
   "kind": "manual_edge"
  },
  {
   "symbol": "src/agentic_fx/ops/service.py::OpsService.emit_pending_guard_notices",
   "resource": "event:guard_state_changed",
   "ids": [
    "F12"
   ],
   "kind": "page"
  },
  {
   "symbol": "src/agentic_fx/ops/service.py::OpsService.emit_pending_guard_notices",
   "resource": "ops:guard_lane_reentry",
   "ids": [
    "F12"
   ],
   "kind": "page"
  },
  {
   "symbol": "src/agentic_fx/ops/service.py::OpsService.emit_pending_notices",
   "resource": "log:guard_notice_deferred",
   "ids": [
    "F21"
   ],
   "kind": "page"
  },
  {
   "symbol": "src/agentic_fx/ops/service.py::OpsService.emit_pending_notices",
   "resource": "event:guard_state_changed",
   "ids": [
    "E16"
   ],
   "kind": "manual_edge"
  },
  {
   "symbol": "src/agentic_fx/ops/service.py::OpsService.emit_pending_notices",
   "resource": "event:guard_state_changed",
   "ids": [
    "F21"
   ],
   "kind": "page"
  },
  {
   "symbol": "src/agentic_fx/ops/service.py::OpsService.reconcile_kill_switch",
   "resource": "event:guard_state_changed",
   "ids": [
    "E11"
   ],
   "kind": "manual_edge"
  },
  {
   "symbol": "src/agentic_fx/ops/service.py::OpsService.reconcile_kill_switch",
   "resource": "event:guard_state_changed",
   "ids": [
    "F08"
   ],
   "kind": "page"
  },
  {
   "symbol": "src/agentic_fx/ops/service.py::OpsService.reconcile_kill_switch",
   "resource": "marker:reset_marker",
   "ids": [
    "F08"
   ],
   "kind": "page"
  },
  {
   "symbol": "src/agentic_fx/ops/service.py::OpsService.reset_kill_switch",
   "resource": "event:guard_state_changed",
   "ids": [
    "E10"
   ],
   "kind": "manual_edge"
  },
  {
   "symbol": "src/agentic_fx/ops/service.py::OpsService.reset_kill_switch",
   "resource": "event:guard_state_changed",
   "ids": [
    "F07"
   ],
   "kind": "page"
  },
  {
   "symbol": "src/agentic_fx/ops/service.py::OpsService.reset_kill_switch",
   "resource": "state:app_state.kill_switch_latched",
   "ids": [
    "F07"
   ],
   "kind": "page"
  },
  {
   "symbol": "src/agentic_fx/ops/service.py::OpsService.resume_data",
   "resource": "event:guard_state_changed",
   "ids": [
    "E08",
    "E09"
   ],
   "kind": "manual_edge"
  },
  {
   "symbol": "src/agentic_fx/ops/service.py::OpsService.resume_data",
   "resource": "event:guard_state_changed",
   "ids": [
    "F16"
   ],
   "kind": "page"
  },
  {
   "symbol": "src/agentic_fx/ops/service.py::OpsService.resume_data",
   "resource": "ops:guard_kind",
   "ids": [
    "F16"
   ],
   "kind": "page"
  },
  {
   "symbol": "src/agentic_fx/ops/service.py::OpsService.status",
   "resource": "marker:reset_marker",
   "ids": [
    "F34"
   ],
   "kind": "page"
  },
  {
   "symbol": "src/agentic_fx/ops/service.py::OpsService.status",
   "resource": "state:app_state.kill_switch_latched",
   "ids": [
    "F34"
   ],
   "kind": "page"
  },
  {
   "symbol": "src/agentic_fx/ops/service.py::plan_kill_switch_notice",
   "resource": "event:guard_state_changed",
   "ids": [
    "F09"
   ],
   "kind": "page"
  },
  {
   "symbol": "src/agentic_fx/ops/service.py::plan_kill_switch_notice",
   "resource": "ops:guard_kind",
   "ids": [
    "F09"
   ],
   "kind": "page"
  },
  {
   "symbol": "src/agentic_fx/ops/service.py::plan_kill_switch_notice",
   "resource": "state:app_state.kill_switch_generation",
   "ids": [
    "F09"
   ],
   "kind": "page"
  },
  {
   "symbol": "src/agentic_fx/ops/service.py::plan_kill_switch_notice",
   "resource": "state:app_state.kill_switch_latched",
   "ids": [
    "F09"
   ],
   "kind": "page"
  },
  {
   "symbol": "src/agentic_fx/ops/service.py::plan_kill_switch_notice",
   "resource": "state:app_state.kill_switch_latched_at",
   "ids": [
    "F09"
   ],
   "kind": "page"
  },
  {
   "symbol": "src/agentic_fx/service.py::build_app",
   "resource": "activity:kill_switch_reconcile_required",
   "ids": [
    "F20"
   ],
   "kind": "page"
  },
  {
   "symbol": "src/agentic_fx/service.py::build_app",
   "resource": "event:guard_state_changed",
   "ids": [
    "E16",
    "E17"
   ],
   "kind": "manual_edge"
  },
  {
   "symbol": "src/agentic_fx/service.py::build_app",
   "resource": "event:guard_state_changed",
   "ids": [
    "F20"
   ],
   "kind": "page"
  },
  {
   "symbol": "src/agentic_fx/status.py::read_state",
   "resource": "marker:reset_marker",
   "ids": [
    "F32"
   ],
   "kind": "page"
  },
  {
   "symbol": "src/agentic_fx/status.py::read_state",
   "resource": "state:app_state.kill_switch_latched",
   "ids": [
    "F32"
   ],
   "kind": "page"
  },
  {
   "symbol": "src/agentic_fx/store/state.py::StateStore.confirm_latched",
   "resource": "marker:reset_marker",
   "ids": [
    "F06"
   ],
   "kind": "page"
  },
  {
   "symbol": "src/agentic_fx/store/state.py::StateStore.confirm_latched",
   "resource": "state:app_state.kill_switch_generation",
   "ids": [
    "F06"
   ],
   "kind": "page"
  },
  {
   "symbol": "src/agentic_fx/store/state.py::StateStore.confirm_latched",
   "resource": "state:app_state.kill_switch_latched",
   "ids": [
    "F06"
   ],
   "kind": "page"
  },
  {
   "symbol": "src/agentic_fx/store/state.py::StateStore.load",
   "resource": "marker:reset_marker",
   "ids": [
    "F03"
   ],
   "kind": "page"
  },
  {
   "symbol": "src/agentic_fx/store/state.py::StateStore.load",
   "resource": "state:app_state.kill_switch_latched",
   "ids": [
    "F03"
   ],
   "kind": "page"
  },
  {
   "symbol": "src/agentic_fx/store/state.py::StateStore.reset_kill_switch",
   "resource": "marker:reset_marker",
   "ids": [
    "F05"
   ],
   "kind": "page"
  },
  {
   "symbol": "src/agentic_fx/store/state.py::StateStore.reset_kill_switch",
   "resource": "state:app_state.kill_switch_generation",
   "ids": [
    "F05"
   ],
   "kind": "page"
  },
  {
   "symbol": "src/agentic_fx/store/state.py::StateStore.reset_kill_switch",
   "resource": "state:app_state.kill_switch_latched",
   "ids": [
    "F05"
   ],
   "kind": "page"
  },
  {
   "symbol": "src/agentic_fx/store/state.py::StateStore.reset_kill_switch",
   "resource": "state:app_state.kill_switch_latched_at",
   "ids": [
    "F05"
   ],
   "kind": "page"
  },
  {
   "symbol": "src/agentic_fx/store/state.py::StateStore.update",
   "resource": "state:app_state.kill_switch_generation",
   "ids": [
    "F04"
   ],
   "kind": "page"
  },
  {
   "symbol": "src/agentic_fx/store/state.py::StateStore.update",
   "resource": "state:app_state.kill_switch_latched",
   "ids": [
    "F04"
   ],
   "kind": "page"
  },
  {
   "symbol": "src/agentic_fx/store/state.py::StateStore.update",
   "resource": "state:app_state.kill_switch_latched_at",
   "ids": [
    "F04"
   ],
   "kind": "page"
  },
  {
   "symbol": "afx-ops/afx_ops/gate/render.py::<module>.OPS_EVENT_SPECS",
   "resource": "event:event_stream_health",
   "ids": [
    "F37"
   ],
   "kind": "page"
  },
  {
   "symbol": "afx-ops/afx_ops/gate/render.py::<module>.OPS_EVENT_SPECS",
   "resource": "event:guard_state_changed",
   "ids": [
    "F37"
   ],
   "kind": "page"
  },
  {
   "symbol": "afx-ops/afx_ops/gate/render.py::<module>.OPS_EVENT_SPECS",
   "resource": "event:ops_request_terminal",
   "ids": [
    "F37"
   ],
   "kind": "page"
  },
  {
   "symbol": "afx-ops/afx_ops/gate/render.py::_event_text_for_code",
   "resource": "ops:guard_kind",
   "ids": [
    "F38"
   ],
   "kind": "page"
  },
  {
   "symbol": "src/agentic_fx/store/state.py::StateStore.confirm_latched",
   "resource": "state:app_state.kill_switch_latched_at",
   "ids": [
    "F06"
   ],
   "kind": "page"
  },
  {
   "symbol": "src/agentic_fx/ops/service.py::OpsService._sweep_kill_switch",
   "resource": "ops:guard_kind",
   "ids": [
    "F10"
   ],
   "kind": "page"
  },
  {
   "symbol": "src/agentic_fx/service.py::build_app",
   "resource": "marker:reset_marker",
   "ids": [
    "F20"
   ],
   "kind": "page"
  },
  {
   "symbol": "src/agentic_fx/ops/events.py::<module>.FIELDS",
   "resource": "event:ops_request_terminal",
   "ids": [
    "F35"
   ],
   "kind": "page"
  },
  {
   "symbol": "src/agentic_fx/ops/events.py::<module>.FIELDS",
   "resource": "event:event_stream_health",
   "ids": [
    "F35"
   ],
   "kind": "page"
  },
  {
   "symbol": "src/agentic_fx/store/snapshots.py::add",
   "resource": "db:account_snapshots",
   "ids": [
    "F39"
   ],
   "kind": "page"
  },
  {
   "symbol": "src/agentic_fx/store/snapshots.py::latest",
   "resource": "db:account_snapshots",
   "ids": [
    "F40"
   ],
   "kind": "page"
  },
  {
   "symbol": "src/agentic_fx/store/snapshots.py::first_since",
   "resource": "db:account_snapshots",
   "ids": [
    "F41"
   ],
   "kind": "page"
  },
  {
   "symbol": "src/agentic_fx/store/snapshots.py::last_before",
   "resource": "db:account_snapshots",
   "ids": [
    "F42"
   ],
   "kind": "page"
  },
  {
   "symbol": "src/agentic_fx/store/db.py::<module>._SCHEMA",
   "resource": "db:account_snapshots",
   "ids": [
    "F43"
   ],
   "kind": "page"
  },
  {
   "symbol": "src/agentic_fx/core/accounting.py::daily_start_snapshot",
   "resource": "db:account_snapshots",
   "ids": [
    "F44"
   ],
   "kind": "page"
  },
  {
   "symbol": "src/agentic_fx/backtest/runner.py::_kill_switch_event",
   "resource": "db:account_snapshots",
   "ids": [
    "F45"
   ],
   "kind": "page"
  },
  {
   "symbol": "src/agentic_fx/backtest/runner.py::_kill_switch_event",
   "resource": "metric:kill_switch_events",
   "ids": [
    "F45"
   ],
   "kind": "page"
  },
  {
   "symbol": "src/agentic_fx/backtest/runner.py::_release_is_due",
   "resource": "db:account_snapshots",
   "ids": [
    "F46"
   ],
   "kind": "page"
  },
  {
   "symbol": "src/agentic_fx/core/executor.py::Executor._open",
   "resource": "db:account_snapshots",
   "ids": [
    "F47"
   ],
   "kind": "page"
  },
  {
   "symbol": "src/agentic_fx/core/executor.py::Executor.open_from_snapshot",
   "resource": "db:account_snapshots",
   "ids": [
    "F48"
   ],
   "kind": "page"
  },
  {
   "symbol": "src/agentic_fx/core/executor.py::Executor._best_effort_record",
   "resource": "activity:kill_switch_latched_durability_uncertain",
   "ids": [
    "F49"
   ],
   "kind": "page"
  },
  {
   "symbol": "src/agentic_fx/store/state.py::AppState",
   "resource": "state:app_state.kill_switch_latched",
   "ids": [
    "F50"
   ],
   "kind": "page"
  },
  {
   "symbol": "src/agentic_fx/store/state.py::AppState",
   "resource": "state:app_state.kill_switch_generation",
   "ids": [
    "F50"
   ],
   "kind": "page"
  },
  {
   "symbol": "src/agentic_fx/store/state.py::AppState",
   "resource": "state:app_state.kill_switch_latched_at",
   "ids": [
    "F50"
   ],
   "kind": "page"
  },
  {
   "symbol": "src/agentic_fx/store/state.py::parse_state",
   "resource": "state:app_state.kill_switch_latched",
   "ids": [
    "F51"
   ],
   "kind": "page"
  },
  {
   "symbol": "src/agentic_fx/store/state.py::parse_state",
   "resource": "state:app_state.kill_switch_generation",
   "ids": [
    "F51"
   ],
   "kind": "page"
  },
  {
   "symbol": "src/agentic_fx/store/state.py::parse_state",
   "resource": "state:app_state.kill_switch_latched_at",
   "ids": [
    "F51"
   ],
   "kind": "page"
  },
  {
   "symbol": "src/agentic_fx/store/state.py::StateStore.save",
   "resource": "state:app_state.kill_switch_latched",
   "ids": [
    "F52"
   ],
   "kind": "page"
  },
  {
   "symbol": "src/agentic_fx/store/state.py::StateStore.save",
   "resource": "state:app_state.kill_switch_generation",
   "ids": [
    "F52"
   ],
   "kind": "page"
  },
  {
   "symbol": "src/agentic_fx/store/state.py::StateStore.save",
   "resource": "state:app_state.kill_switch_latched_at",
   "ids": [
    "F52"
   ],
   "kind": "page"
  },
  {
   "symbol": "src/agentic_fx/store/state.py::StateStore._load_unlocked",
   "resource": "state:app_state.kill_switch_latched",
   "ids": [
    "F53"
   ],
   "kind": "page"
  },
  {
   "symbol": "src/agentic_fx/store/state.py::<module>._MARKER_SUFFIX",
   "resource": "marker:reset_marker",
   "ids": [
    "F54"
   ],
   "kind": "page"
  },
  {
   "symbol": "src/agentic_fx/store/state.py::StateStore._marker",
   "resource": "marker:reset_marker",
   "ids": [
    "F55"
   ],
   "kind": "page"
  },
  {
   "symbol": "src/agentic_fx/store/state.py::StateStore._write_marker",
   "resource": "marker:reset_marker",
   "ids": [
    "F56"
   ],
   "kind": "page"
  },
  {
   "symbol": "src/agentic_fx/store/state.py::StateStore._remove_marker",
   "resource": "marker:reset_marker",
   "ids": [
    "F57"
   ],
   "kind": "page"
  },
  {
   "symbol": "src/agentic_fx/store/state.py::StateStore.reconcile_marker",
   "resource": "marker:reset_marker",
   "ids": [
    "F58"
   ],
   "kind": "page"
  },
  {
   "symbol": "src/agentic_fx/backtest/runner.py::_RecordingStateStore.update",
   "resource": "state:app_state.kill_switch_latched",
   "ids": [
    "F59"
   ],
   "kind": "page"
  },
  {
   "symbol": "src/agentic_fx/backtest/runner.py::_RecordingStateStore.publish_released",
   "resource": "metric:kill_switch_events",
   "ids": [
    "F60"
   ],
   "kind": "page"
  },
  {
   "symbol": "src/agentic_fx/backtest/runner.py::BacktestResult",
   "resource": "metric:kill_switch_events",
   "ids": [
    "F61"
   ],
   "kind": "page"
  },
  {
   "symbol": "src/agentic_fx/backtest/runner.py::BacktestResult",
   "resource": "metric:kill_switch_latches",
   "ids": [
    "F61"
   ],
   "kind": "page"
  },
  {
   "symbol": "src/agentic_fx/core/risk_gate.py::GateContext",
   "resource": "state:app_state.kill_switch_latched",
   "ids": [
    "F62"
   ],
   "kind": "page"
  },
  {
   "symbol": "src/agentic_fx/backtest/metrics.py::<module>.METRIC_KEYS",
   "resource": "metric:kill_switch_latches",
   "ids": [
    "F63"
   ],
   "kind": "page"
  },
  {
   "symbol": "src/agentic_fx/store/backtest_runs.py::_filter_metrics",
   "resource": "metric:kill_switch_latches",
   "ids": [
    "F64"
   ],
   "kind": "page"
  },
  {
   "symbol": "src/agentic_fx/store/backtest_runs.py::_insert",
   "resource": "metric:kill_switch_latches",
   "ids": [
    "F65"
   ],
   "kind": "page"
  },
  {
   "symbol": "src/agentic_fx/plugin/strategy_gate.py::<module>._COMPARE_METRIC_KEYS",
   "resource": "metric:kill_switch_latches",
   "ids": [
    "F66"
   ],
   "kind": "page"
  },
  {
   "symbol": "src/agentic_fx/plugin/strategy_gate.py::_project",
   "resource": "metric:kill_switch_latches",
   "ids": [
    "F67"
   ],
   "kind": "page"
  },
  {
   "symbol": "src/agentic_fx/ops/events.py::<module>._REF",
   "resource": "event:guard_state_changed",
   "ids": [
    "F68"
   ],
   "kind": "page"
  },
  {
   "symbol": "src/agentic_fx/ops/events.py::<module>._REF",
   "resource": "event:ops_request_terminal",
   "ids": [
    "F68"
   ],
   "kind": "page"
  },
  {
   "symbol": "src/agentic_fx/ops/events.py::<module>._REF",
   "resource": "event:event_stream_health",
   "ids": [
    "F68"
   ],
   "kind": "page"
  },
  {
   "symbol": "src/agentic_fx/ops/events.py::validate_fields",
   "resource": "event:guard_state_changed",
   "ids": [
    "F69"
   ],
   "kind": "page"
  },
  {
   "symbol": "src/agentic_fx/ops/events.py::validate_fields",
   "resource": "event:ops_request_terminal",
   "ids": [
    "F69"
   ],
   "kind": "page"
  },
  {
   "symbol": "src/agentic_fx/ops/events.py::validate_fields",
   "resource": "event:event_stream_health",
   "ids": [
    "F69"
   ],
   "kind": "page"
  },
  {
   "symbol": "src/agentic_fx/ops/events.py::kill_switch_event_states",
   "resource": "event:guard_state_changed",
   "ids": [
    "F70"
   ],
   "kind": "page"
  },
  {
   "symbol": "src/agentic_fx/ops/events.py::kill_switch_event_states",
   "resource": "ops:guard_kind",
   "ids": [
    "F70"
   ],
   "kind": "page"
  },
  {
   "symbol": "afx-ops/afx_ops/notify/payload.py::<module>.ALLOWED_FIELDS",
   "resource": "event:guard_state_changed",
   "ids": [
    "F71"
   ],
   "kind": "page"
  },
  {
   "symbol": "afx-ops/afx_ops/notify/payload.py::<module>.ALLOWED_FIELDS",
   "resource": "event:ops_request_terminal",
   "ids": [
    "F71"
   ],
   "kind": "page"
  },
  {
   "symbol": "afx-ops/afx_ops/notify/payload.py::<module>.ALLOWED_FIELDS",
   "resource": "event:event_stream_health",
   "ids": [
    "F71"
   ],
   "kind": "page"
  },
  {
   "symbol": "afx-ops/afx_ops/notify/payload.py::<module>.ALLOWED_FIELDS",
   "resource": "ops:guard_kind",
   "ids": [
    "F71"
   ],
   "kind": "page"
  },
  {
   "symbol": "afx-ops/afx_ops/notify/payload.py::<module>._REF",
   "resource": "event:guard_state_changed",
   "ids": [
    "F72"
   ],
   "kind": "page"
  },
  {
   "symbol": "afx-ops/afx_ops/notify/payload.py::<module>._REF",
   "resource": "event:ops_request_terminal",
   "ids": [
    "F72"
   ],
   "kind": "page"
  },
  {
   "symbol": "afx-ops/afx_ops/notify/payload.py::<module>._REF",
   "resource": "event:event_stream_health",
   "ids": [
    "F72"
   ],
   "kind": "page"
  },
  {
   "symbol": "src/agentic_fx/ops/service.py::OpsService._guard_event",
   "resource": "ops:guard_kind",
   "ids": [
    "F73"
   ],
   "kind": "page"
  },
  {
   "symbol": "afx-ops/afx_ops/gate/service.py::GateService.plan_event",
   "resource": "event:guard_state_changed",
   "ids": [
    "F74"
   ],
   "kind": "page"
  },
  {
   "symbol": "afx-ops/afx_ops/event_outcome.py::decide",
   "resource": "event:guard_state_changed",
   "ids": [
    "F75"
   ],
   "kind": "page"
  },
  {
   "symbol": "afx-ops/afx_ops/gate/render.py::wants_ops_event",
   "resource": "event:guard_state_changed",
   "ids": [
    "F76"
   ],
   "kind": "page"
  },
  {
   "symbol": "afx-ops/afx_ops/gate/render.py::render_event",
   "resource": "event:guard_state_changed",
   "ids": [
    "F77"
   ],
   "kind": "page"
  },
  {
   "symbol": "afx-ops/afx_ops/notify/runtime.py::_Loop._wants",
   "resource": "event:guard_state_changed",
   "ids": [
    "F78"
   ],
   "kind": "page"
  },
  {
   "symbol": "src/agentic_fx/core/scheduler.py::Scheduler._evaluate_positions",
   "resource": "db:account_snapshots",
   "ids": [
    "E18"
   ],
   "kind": "manual_edge"
  },
  {
   "symbol": "src/agentic_fx/core/paper_broker.py::PaperBroker.equity",
   "resource": "db:account_snapshots",
   "ids": [
    "E19"
   ],
   "kind": "manual_edge"
  },
  {
   "symbol": "src/agentic_fx/core/scheduler.py::Scheduler._mark_to_market",
   "resource": "db:account_snapshots",
   "ids": [
    "E20"
   ],
   "kind": "manual_edge"
  },
  {
   "symbol": "src/agentic_fx/core/scheduler.py::Scheduler._tick_body",
   "resource": "db:account_snapshots",
   "ids": [
    "E21"
   ],
   "kind": "manual_edge"
  },
  {
   "symbol": "src/agentic_fx/core/scheduler.py::Scheduler.tick",
   "resource": "db:account_snapshots",
   "ids": [
    "E22"
   ],
   "kind": "manual_edge"
  },
  {
   "symbol": "src/agentic_fx/service.py::_scheduler_tick_body",
   "resource": "db:account_snapshots",
   "ids": [
    "E23"
   ],
   "kind": "manual_edge"
  },
  {
   "symbol": "src/agentic_fx/loops/trade_loop.py::TradeLoop.run_once",
   "resource": "state:app_state.kill_switch_latched",
   "ids": [
    "E24"
   ],
   "kind": "manual_edge"
  },
  {
   "symbol": "src/agentic_fx/service.py::build_app._trade_fn",
   "resource": "state:app_state.kill_switch_latched",
   "ids": [
    "E25"
   ],
   "kind": "manual_edge"
  },
  {
   "symbol": "src/agentic_fx/commands.py::Commands._dispatch",
   "resource": "state:app_state.kill_switch_latched",
   "ids": [
    "E26"
   ],
   "kind": "manual_edge"
  },
  {
   "symbol": "src/agentic_fx/commands.py::Commands._dispatch",
   "resource": "state:app_state.kill_switch_generation",
   "ids": [
    "E26"
   ],
   "kind": "manual_edge"
  },
  {
   "symbol": "src/agentic_fx/ops/api_server.py::_call",
   "resource": "state:app_state.kill_switch_latched",
   "ids": [
    "E27"
   ],
   "kind": "manual_edge"
  },
  {
   "symbol": "src/agentic_fx/ops/api_server.py::_call",
   "resource": "state:app_state.kill_switch_generation",
   "ids": [
    "E27"
   ],
   "kind": "manual_edge"
  },
  {
   "symbol": "src/agentic_fx/ops/api_server.py::_call",
   "resource": "marker:reset_marker",
   "ids": [
    "E28"
   ],
   "kind": "manual_edge"
  },
  {
   "symbol": "src/agentic_fx/ops/client.py::Runner._op_killswitch_reset",
   "resource": "state:app_state.kill_switch_latched",
   "ids": [
    "E29"
   ],
   "kind": "manual_edge"
  },
  {
   "symbol": "src/agentic_fx/ops/client.py::Runner._op_killswitch_reset",
   "resource": "state:app_state.kill_switch_generation",
   "ids": [
    "E29"
   ],
   "kind": "manual_edge"
  },
  {
   "symbol": "src/agentic_fx/ops/client.py::Runner._op_killswitch_reconcile",
   "resource": "marker:reset_marker",
   "ids": [
    "E30"
   ],
   "kind": "manual_edge"
  },
  {
   "symbol": "src/agentic_fx/ops/service.py::OpsService._guard_notices_in_lane",
   "resource": "event:guard_state_changed",
   "ids": [
    "E31"
   ],
   "kind": "manual_edge"
  },
  {
   "symbol": "src/agentic_fx/service.py::run_service.scheduler_thread",
   "resource": "event:guard_state_changed",
   "ids": [
    "E32"
   ],
   "kind": "manual_edge"
  },
  {
   "symbol": "src/agentic_fx/service.py::_sweep_improve_notices",
   "resource": "event:guard_state_changed",
   "ids": [
    "E33"
   ],
   "kind": "manual_edge"
  },
  {
   "symbol": "afx-ops/afx_ops/bot/runtime.py::_poll",
   "resource": "event:guard_state_changed",
   "ids": [
    "E34"
   ],
   "kind": "manual_edge"
  },
  {
   "symbol": "afx-ops/afx_ops/bot/adapter.py::BotAdapter.poll_once",
   "resource": "event:guard_state_changed",
   "ids": [
    "E35"
   ],
   "kind": "manual_edge"
  },
  {
   "symbol": "afx-ops/afx_ops/bot/adapter.py::BotAdapter._plan",
   "resource": "event:guard_state_changed",
   "ids": [
    "E36"
   ],
   "kind": "manual_edge"
  },
  {
   "symbol": "afx-ops/afx_ops/notify/runtime.py::_Loop.run",
   "resource": "event:guard_state_changed",
   "ids": [
    "E37"
   ],
   "kind": "manual_edge"
  },
  {
   "symbol": "src/agentic_fx/backtest/cli.py::_backtest_run_plugin",
   "resource": "metric:kill_switch_latches",
   "ids": [
    "E38"
   ],
   "kind": "manual_edge"
  },
  {
   "symbol": "src/agentic_fx/backtest/cli.py::_backtest_run_proposal",
   "resource": "metric:kill_switch_latches",
   "ids": [
    "E39"
   ],
   "kind": "manual_edge"
  },
  {
   "symbol": "src/agentic_fx/backtest/holdout.py::_run_scope",
   "resource": "metric:kill_switch_latches",
   "ids": [
    "E40"
   ],
   "kind": "manual_edge"
  },
  {
   "symbol": "src/agentic_fx/loops/improve_loop.py::ImproveLoop._build_rpc_handlers.run_backtest_handler",
   "resource": "metric:kill_switch_latches",
   "ids": [
    "E41"
   ],
   "kind": "manual_edge"
  },
  {
   "symbol": "src/agentic_fx/plugin/strategy_gate.py::evaluate_strategy_adoption_gate",
   "resource": "metric:kill_switch_latches",
   "ids": [
    "E42"
   ],
   "kind": "manual_edge"
  },
  {
   "symbol": "src/agentic_fx/plugin/strategy_gate.py::_build_comparison",
   "resource": "metric:kill_switch_latches",
   "ids": [
    "E43"
   ],
   "kind": "manual_edge"
  },
  {
   "symbol": "afx-ops/afx_ops/api_client.py::BotClient.events",
   "resource": "event:guard_state_changed",
   "ids": [
    "F79"
   ],
   "kind": "page"
  },
  {
   "symbol": "afx-ops/afx_ops/api_client.py::BotClient.events",
   "resource": "event:ops_request_terminal",
   "ids": [
    "F79"
   ],
   "kind": "page"
  },
  {
   "symbol": "afx-ops/afx_ops/api_client.py::BotClient.events",
   "resource": "event:event_stream_health",
   "ids": [
    "F79"
   ],
   "kind": "page"
  },
  {
   "symbol": "afx-ops/afx_ops/api_client.py::NotifyClient.events",
   "resource": "event:guard_state_changed",
   "ids": [
    "F80"
   ],
   "kind": "page"
  },
  {
   "symbol": "afx-ops/afx_ops/api_client.py::NotifyClient.events",
   "resource": "event:ops_request_terminal",
   "ids": [
    "F80"
   ],
   "kind": "page"
  },
  {
   "symbol": "afx-ops/afx_ops/api_client.py::NotifyClient.events",
   "resource": "event:event_stream_health",
   "ids": [
    "F80"
   ],
   "kind": "page"
  },
  {
   "symbol": "afx-ops/afx_ops/events_page.py::parse_page",
   "resource": "event:guard_state_changed",
   "ids": [
    "F81"
   ],
   "kind": "page"
  },
  {
   "symbol": "afx-ops/afx_ops/events_page.py::parse_page",
   "resource": "event:ops_request_terminal",
   "ids": [
    "F81"
   ],
   "kind": "page"
  },
  {
   "symbol": "afx-ops/afx_ops/events_page.py::parse_page",
   "resource": "event:event_stream_health",
   "ids": [
    "F81"
   ],
   "kind": "page"
  },
  {
   "symbol": "afx-ops/afx_ops/bot/adapter.py::_event_content",
   "resource": "event:guard_state_changed",
   "ids": [
    "F82"
   ],
   "kind": "page"
  },
  {
   "symbol": "afx-ops/afx_ops/bot/adapter.py::_event_content",
   "resource": "event:ops_request_terminal",
   "ids": [
    "F82"
   ],
   "kind": "page"
  },
  {
   "symbol": "afx-ops/afx_ops/bot/adapter.py::_event_content",
   "resource": "event:event_stream_health",
   "ids": [
    "F82"
   ],
   "kind": "page"
  },
  {
   "symbol": "afx-ops/afx_ops/notify/runtime.py::_Loop._deliver",
   "resource": "event:guard_state_changed",
   "ids": [
    "F83"
   ],
   "kind": "page"
  },
  {
   "symbol": "afx-ops/afx_ops/notify/runtime.py::_Loop._deliver",
   "resource": "event:ops_request_terminal",
   "ids": [
    "F83"
   ],
   "kind": "page"
  },
  {
   "symbol": "afx-ops/afx_ops/notify/runtime.py::_Loop._deliver",
   "resource": "event:event_stream_health",
   "ids": [
    "F83"
   ],
   "kind": "page"
  },
  {
   "symbol": "afx-ops/afx_ops/notify/runtime.py::_Loop._deliver.attempt",
   "resource": "event:guard_state_changed",
   "ids": [
    "F84"
   ],
   "kind": "page"
  },
  {
   "symbol": "afx-ops/afx_ops/notify/runtime.py::_Loop._deliver.attempt",
   "resource": "event:ops_request_terminal",
   "ids": [
    "F84"
   ],
   "kind": "page"
  },
  {
   "symbol": "afx-ops/afx_ops/notify/runtime.py::_Loop._deliver.attempt",
   "resource": "event:event_stream_health",
   "ids": [
    "F84"
   ],
   "kind": "page"
  },
  {
   "symbol": "src/agentic_fx/service.py::build_splash",
   "resource": "marker:reset_marker",
   "ids": [
    "F85"
   ],
   "kind": "page"
  },
  {
   "symbol": "src/agentic_fx/loops/improve_loop.py::ImproveLoop._persist_gate_rows",
   "resource": "metric:kill_switch_latches",
   "ids": [
    "F86"
   ],
   "kind": "page"
  },
  {
   "symbol": "src/agentic_fx/loops/improve_loop.py::ImproveLoop._persist_ledger_rows",
   "resource": "metric:kill_switch_latches",
   "ids": [
    "F87"
   ],
   "kind": "page"
  },
  {
   "symbol": "src/agentic_fx/plugin/approval.py::run_kind_gate._sink",
   "resource": "metric:kill_switch_latches",
   "ids": [
    "F88"
   ],
   "kind": "page"
  },
  {
   "symbol": "afx-ops/afx_ops/bot/adapter.py::_page",
   "resource": "event:guard_state_changed",
   "ids": [
    "E44"
   ],
   "kind": "manual_edge"
  },
  {
   "symbol": "afx-ops/afx_ops/bot/adapter.py::_page",
   "resource": "event:ops_request_terminal",
   "ids": [
    "E44"
   ],
   "kind": "manual_edge"
  },
  {
   "symbol": "afx-ops/afx_ops/bot/adapter.py::_page",
   "resource": "event:event_stream_health",
   "ids": [
    "E44"
   ],
   "kind": "manual_edge"
  },
  {
   "symbol": "afx-ops/afx_ops/notify/runtime.py::_page",
   "resource": "event:guard_state_changed",
   "ids": [
    "E45"
   ],
   "kind": "manual_edge"
  },
  {
   "symbol": "afx-ops/afx_ops/notify/runtime.py::_page",
   "resource": "event:ops_request_terminal",
   "ids": [
    "E45"
   ],
   "kind": "manual_edge"
  },
  {
   "symbol": "afx-ops/afx_ops/notify/runtime.py::_page",
   "resource": "event:event_stream_health",
   "ids": [
    "E45"
   ],
   "kind": "manual_edge"
  },
  {
   "symbol": "src/agentic_fx/loops/improve_loop.py::ImproveLoop._persist_gate_rows",
   "resource": "metric:kill_switch_latches",
   "ids": [
    "E46"
   ],
   "kind": "manual_edge"
  },
  {
   "symbol": "src/agentic_fx/loops/improve_loop.py::ImproveLoop._persist_ledger_rows",
   "resource": "metric:kill_switch_latches",
   "ids": [
    "E47"
   ],
   "kind": "manual_edge"
  },
  {
   "symbol": "src/agentic_fx/loops/improve_loop.py::ImproveLoop._run_strategy_gate",
   "resource": "metric:kill_switch_latches",
   "ids": [
    "E48"
   ],
   "kind": "manual_edge"
  },
  {
   "symbol": "src/agentic_fx/plugin/approval.py::run_kind_gate",
   "resource": "metric:kill_switch_latches",
   "ids": [
    "E49"
   ],
   "kind": "manual_edge"
  },
  {
   "symbol": "src/agentic_fx/plugin/switch.py::_evaluate_deployed",
   "resource": "metric:kill_switch_latches",
   "ids": [
    "E50"
   ],
   "kind": "manual_edge"
  },
  {
   "symbol": "src/agentic_fx/core/scheduler.py::Scheduler._tick_body",
   "resource": "state:app_state.kill_switch_latched",
   "ids": [
    "E51"
   ],
   "kind": "manual_edge"
  },
  {
   "symbol": "src/agentic_fx/service.py::build_app.on_trade_mission",
   "resource": "state:app_state.kill_switch_latched",
   "ids": [
    "E52"
   ],
   "kind": "manual_edge"
  },
  {
   "symbol": "src/agentic_fx/core/supervisor.py::MissionSupervisor.try_submit",
   "resource": "state:app_state.kill_switch_latched",
   "ids": [
    "E53"
   ],
   "kind": "manual_edge"
  },
  {
   "symbol": "src/agentic_fx/core/supervisor.py::MissionSupervisor._run",
   "resource": "state:app_state.kill_switch_latched",
   "ids": [
    "E54"
   ],
   "kind": "manual_edge"
  },
  {
   "symbol": "src/agentic_fx/core/supervisor.py::MissionSupervisor._dispatch",
   "resource": "state:app_state.kill_switch_latched",
   "ids": [
    "E55"
   ],
   "kind": "manual_edge"
  },
  {
   "symbol": "src/agentic_fx/backtest/runner.py::_kill_switch_event",
   "resource": "activity:kill_switch_latched",
   "ids": [
    "F89"
   ],
   "kind": "page"
  },
  {
   "symbol": "src/agentic_fx/backtest/runner.py::run_replay",
   "resource": "activity:kill_switch_latched",
   "ids": [
    "E56"
   ],
   "kind": "manual_edge"
  },
  {
   "symbol": "src/agentic_fx/store/state.py::<module>._BOOL_FIELDS",
   "resource": "state:app_state.kill_switch_latched",
   "ids": [
    "F90"
   ],
   "kind": "page"
  },
  {
   "symbol": "src/agentic_fx/store/state.py::<module>._REQUIRED_KEYS",
   "resource": "state:app_state.kill_switch_latched",
   "ids": [
    "F91"
   ],
   "kind": "page"
  },
  {
   "symbol": "src/agentic_fx/ops/service.py::OpsService._sweep_kill_switch",
   "resource": "state:app_state.kill_switch_latched_at",
   "ids": [
    "F10"
   ],
   "kind": "page"
  },
  {
   "symbol": "src/agentic_fx/service.py::_build_ops_service.status_provider",
   "resource": "ops:guard_notice_pending",
   "ids": [
    "F92"
   ],
   "kind": "page"
  },
  {
   "symbol": "src/agentic_fx/service.py::_build_ops_service.status_provider",
   "resource": "ops:guard_notice_generation",
   "ids": [
    "F92"
   ],
   "kind": "page"
  },
  {
   "symbol": "src/agentic_fx/ops/client.py::Runner._op_activity",
   "resource": "activity:kill_switch_latched",
   "ids": [
    "F93"
   ],
   "kind": "page"
  },
  {
   "symbol": "src/agentic_fx/ops/client.py::Runner._op_activity",
   "resource": "activity:kill_switch_latched_durability_uncertain",
   "ids": [
    "F93"
   ],
   "kind": "page"
  },
  {
   "symbol": "src/agentic_fx/ops/client.py::Runner._op_activity",
   "resource": "activity:kill_switch_reset",
   "ids": [
    "F93"
   ],
   "kind": "page"
  },
  {
   "symbol": "src/agentic_fx/ops/client.py::Runner._op_activity",
   "resource": "activity:kill_switch_reconcile_required",
   "ids": [
    "F93"
   ],
   "kind": "page"
  },
  {
   "symbol": "src/agentic_fx/ops/client.py::Runner._op_activity",
   "resource": "activity:kill_switch_reconcile_confirmed",
   "ids": [
    "F93"
   ],
   "kind": "page"
  },
  {
   "symbol": "src/agentic_fx/ops/client.py::Runner._op_activity",
   "resource": "activity:kill_switch_reconcile_failed",
   "ids": [
    "F93"
   ],
   "kind": "page"
  },
  {
   "symbol": "src/agentic_fx/ops/client.py::Runner._op_activity",
   "resource": "activity:kill_switch_state_uncertain",
   "ids": [
    "F93"
   ],
   "kind": "page"
  },
  {
   "symbol": "src/agentic_fx/ops/service.py::OpsService.activity",
   "resource": "activity:kill_switch_latched",
   "ids": [
    "F94"
   ],
   "kind": "page"
  },
  {
   "symbol": "src/agentic_fx/ops/service.py::OpsService.activity",
   "resource": "activity:kill_switch_latched_durability_uncertain",
   "ids": [
    "F94"
   ],
   "kind": "page"
  },
  {
   "symbol": "src/agentic_fx/ops/service.py::OpsService.activity",
   "resource": "activity:kill_switch_reset",
   "ids": [
    "F94"
   ],
   "kind": "page"
  },
  {
   "symbol": "src/agentic_fx/ops/service.py::OpsService.activity",
   "resource": "activity:kill_switch_reconcile_required",
   "ids": [
    "F94"
   ],
   "kind": "page"
  },
  {
   "symbol": "src/agentic_fx/ops/service.py::OpsService.activity",
   "resource": "activity:kill_switch_reconcile_confirmed",
   "ids": [
    "F94"
   ],
   "kind": "page"
  },
  {
   "symbol": "src/agentic_fx/ops/service.py::OpsService.activity",
   "resource": "activity:kill_switch_reconcile_failed",
   "ids": [
    "F94"
   ],
   "kind": "page"
  },
  {
   "symbol": "src/agentic_fx/ops/service.py::OpsService.activity",
   "resource": "activity:kill_switch_state_uncertain",
   "ids": [
    "F94"
   ],
   "kind": "page"
  },
  {
   "symbol": "src/agentic_fx/status.py::_activity",
   "resource": "activity:kill_switch_latched",
   "ids": [
    "F95"
   ],
   "kind": "page"
  },
  {
   "symbol": "src/agentic_fx/status.py::_activity",
   "resource": "activity:kill_switch_latched_durability_uncertain",
   "ids": [
    "F95"
   ],
   "kind": "page"
  },
  {
   "symbol": "src/agentic_fx/status.py::_activity",
   "resource": "activity:kill_switch_reset",
   "ids": [
    "F95"
   ],
   "kind": "page"
  },
  {
   "symbol": "src/agentic_fx/status.py::_activity",
   "resource": "activity:kill_switch_reconcile_required",
   "ids": [
    "F95"
   ],
   "kind": "page"
  },
  {
   "symbol": "src/agentic_fx/status.py::_activity",
   "resource": "activity:kill_switch_reconcile_confirmed",
   "ids": [
    "F95"
   ],
   "kind": "page"
  },
  {
   "symbol": "src/agentic_fx/status.py::_activity",
   "resource": "activity:kill_switch_reconcile_failed",
   "ids": [
    "F95"
   ],
   "kind": "page"
  },
  {
   "symbol": "src/agentic_fx/status.py::_activity",
   "resource": "activity:kill_switch_state_uncertain",
   "ids": [
    "F95"
   ],
   "kind": "page"
  },
  {
   "symbol": "src/agentic_fx/core/executor.py::Executor.defer_notifications",
   "resource": "activity:kill_switch_latched_durability_uncertain",
   "ids": [
    "F96"
   ],
   "kind": "page"
  },
  {
   "symbol": "src/agentic_fx/core/notifier.py::Notifier.send",
   "resource": "activity:kill_switch_latched_durability_uncertain",
   "ids": [
    "F97"
   ],
   "kind": "page"
  },
  {
   "symbol": "src/agentic_fx/loops/trade_loop.py::TradeLoop._run_once_impl",
   "resource": "activity:kill_switch_latched_durability_uncertain",
   "ids": [
    "F98"
   ],
   "kind": "page"
  },
  {
   "symbol": "src/agentic_fx/loops/trade_loop.py::TradeLoop._run_signal_mission",
   "resource": "activity:kill_switch_latched_durability_uncertain",
   "ids": [
    "F99"
   ],
   "kind": "page"
  },
  {
   "symbol": "afx-ops/afx_ops/display_time.py::kill_switch_reset",
   "resource": "activity:kill_switch_reset",
   "ids": [
    "F100"
   ],
   "kind": "page"
  },
  {
   "symbol": "src/agentic_fx/core/display_time.py::kill_switch_reset",
   "resource": "activity:kill_switch_reset",
   "ids": [
    "F101"
   ],
   "kind": "page"
  },
  {
   "symbol": "src/agentic_fx/config.py::RiskSettings",
   "resource": "config:risk.drawdown_kill_pct",
   "ids": [
    "F102"
   ],
   "kind": "page"
  },
  {
   "symbol": "src/agentic_fx/config.py::Settings._warn_high_drawdown_kill_pct",
   "resource": "config:risk.drawdown_kill_pct",
   "ids": [
    "F103"
   ],
   "kind": "page"
  },
  {
   "symbol": "src/agentic_fx/ops/events.py::EventStore.poll",
   "resource": "event:event_stream_health",
   "ids": [
    "F104"
   ],
   "kind": "page"
  },
  {
   "symbol": "src/agentic_fx/ops/events.py::EventStore.poll",
   "resource": "event:guard_state_changed",
   "ids": [
    "F104"
   ],
   "kind": "page"
  },
  {
   "symbol": "src/agentic_fx/ops/events.py::EventStore.poll",
   "resource": "event:ops_request_terminal",
   "ids": [
    "F104"
   ],
   "kind": "page"
  },
  {
   "symbol": "src/agentic_fx/ops/service.py::OpsService._project",
   "resource": "event:ops_request_terminal",
   "ids": [
    "F105"
   ],
   "kind": "page"
  },
  {
   "symbol": "src/agentic_fx/ops/service.py::OpsService.recover_after_journal",
   "resource": "event:ops_request_terminal",
   "ids": [
    "F106"
   ],
   "kind": "page"
  },
  {
   "symbol": "src/agentic_fx/commands.py::Commands._status",
   "resource": "marker:reset_marker",
   "ids": [
    "F107"
   ],
   "kind": "page"
  },
  {
   "symbol": "src/agentic_fx/commands.py::Commands._status",
   "resource": "state:app_state.kill_switch_latched",
   "ids": [
    "F107"
   ],
   "kind": "page"
  },
  {
   "symbol": "src/agentic_fx/ops/client.py::format_status",
   "resource": "marker:reset_marker",
   "ids": [
    "F108"
   ],
   "kind": "page"
  },
  {
   "symbol": "src/agentic_fx/ops/client.py::format_status",
   "resource": "state:app_state.kill_switch_latched",
   "ids": [
    "F108"
   ],
   "kind": "page"
  },
  {
   "symbol": "afx-ops/afx_ops/metric_words.py::metric_items",
   "resource": "metric:kill_switch_latches",
   "ids": [
    "F109"
   ],
   "kind": "page"
  },
  {
   "symbol": "afx-ops/afx_ops/web/render.py::_baseline_section",
   "resource": "metric:kill_switch_latches",
   "ids": [
    "F110"
   ],
   "kind": "page"
  },
  {
   "symbol": "src/agentic_fx/commands.py::Commands._comparison_metrics_line",
   "resource": "metric:kill_switch_latches",
   "ids": [
    "F111"
   ],
   "kind": "page"
  },
  {
   "symbol": "src/agentic_fx/loops/improve_loop.py::ImproveLoop._render_comparison_report_detail",
   "resource": "metric:kill_switch_latches",
   "ids": [
    "F113"
   ],
   "kind": "page"
  },
  {
   "symbol": "src/agentic_fx/loops/improve_loop.py::ImproveLoop._render_floor_report_detail",
   "resource": "metric:kill_switch_latches",
   "ids": [
    "F114"
   ],
   "kind": "page"
  },
  {
   "symbol": "src/agentic_fx/metric_words.py::metric_items",
   "resource": "metric:kill_switch_latches",
   "ids": [
    "F115"
   ],
   "kind": "page"
  },
  {
   "symbol": "src/agentic_fx/ops/change.py::_metric_row",
   "resource": "metric:kill_switch_latches",
   "ids": [
    "F116"
   ],
   "kind": "page"
  },
  {
   "symbol": "afx-ops/afx_ops/web/render.py::<module>.ACTIVITY_FIELDS",
   "resource": "ops:guard_kind",
   "ids": [
    "F117"
   ],
   "kind": "page"
  },
  {
   "symbol": "src/agentic_fx/ops/service.py::GuardLaneNotHeld",
   "resource": "ops:guard_lane_not_held",
   "ids": [
    "F118"
   ],
   "kind": "page"
  },
  {
   "symbol": "src/agentic_fx/ops/service.py::OpsService._guard_notices_in_lane",
   "resource": "ops:guard_lane_not_held",
   "ids": [
    "F119"
   ],
   "kind": "page"
  },
  {
   "symbol": "src/agentic_fx/ops/service.py::GuardLaneReentry",
   "resource": "ops:guard_lane_reentry",
   "ids": [
    "F120"
   ],
   "kind": "page"
  },
  {
   "symbol": "src/agentic_fx/ops/service.py::OpsService._reject_lane_reentry",
   "resource": "ops:guard_lane_reentry",
   "ids": [
    "F121"
   ],
   "kind": "page"
  },
  {
   "symbol": "src/agentic_fx/ops/client.py::Runner._op_killswitch_show",
   "resource": "state:app_state.kill_switch_generation",
   "ids": [
    "F122"
   ],
   "kind": "page"
  },
  {
   "symbol": "src/agentic_fx/ops/client.py::Runner._op_killswitch_show",
   "resource": "state:app_state.kill_switch_latched",
   "ids": [
    "F122"
   ],
   "kind": "page"
  },
  {
   "symbol": "afx-ops/afx_ops/web/render.py::<module>.STATUS_FIELDS",
   "resource": "state:app_state.kill_switch_latched",
   "ids": [
    "F123"
   ],
   "kind": "page"
  },
  {
   "symbol": "afx-ops/afx_ops/web/render.py::<module>._EMPHASIS",
   "resource": "state:app_state.kill_switch_latched",
   "ids": [
    "F124"
   ],
   "kind": "page"
  },
  {
   "symbol": "afx-ops/afx_ops/web/render.py::render_status",
   "resource": "state:app_state.kill_switch_latched",
   "ids": [
    "F125"
   ],
   "kind": "page"
  },
  {
   "symbol": "src/agentic_fx/commands.py::Commands._projected_state",
   "resource": "state:app_state.kill_switch_latched",
   "ids": [
    "F126"
   ],
   "kind": "page"
  },
  {
   "symbol": "src/agentic_fx/status.py::render_status",
   "resource": "state:app_state.kill_switch_latched",
   "ids": [
    "F127"
   ],
   "kind": "page"
  },
  {
   "symbol": "src/agentic_fx/store/state.py::StateStore.file_value",
   "resource": "state:app_state.kill_switch_latched",
   "ids": [
    "F128"
   ],
   "kind": "page"
  },
  {
   "symbol": "src/agentic_fx/core/executor.py::Executor._notify",
   "resource": "activity:kill_switch_latched_durability_uncertain",
   "ids": [
    "E57"
   ],
   "kind": "manual_edge"
  },
  {
   "symbol": "src/agentic_fx/ops/api_server.py::_call",
   "resource": "event:guard_state_changed",
   "ids": [
    "E58"
   ],
   "kind": "manual_edge"
  },
  {
   "symbol": "src/agentic_fx/ops/api_server.py::_call",
   "resource": "event:ops_request_terminal",
   "ids": [
    "E58"
   ],
   "kind": "manual_edge"
  },
  {
   "symbol": "src/agentic_fx/ops/api_server.py::_call",
   "resource": "event:event_stream_health",
   "ids": [
    "E58"
   ],
   "kind": "manual_edge"
  },
  {
   "symbol": "src/agentic_fx/ops/service.py::OpsService.events_after",
   "resource": "event:guard_state_changed",
   "ids": [
    "E59"
   ],
   "kind": "manual_edge"
  },
  {
   "symbol": "src/agentic_fx/service.py::_GuardNoticeProbe.__call__",
   "resource": "event:guard_state_changed",
   "ids": [
    "E60"
   ],
   "kind": "manual_edge"
  },
  {
   "symbol": "src/agentic_fx/ops/service.py::OpsService._mutate",
   "resource": "event:ops_request_terminal",
   "ids": [
    "E61"
   ],
   "kind": "manual_edge"
  },
  {
   "symbol": "src/agentic_fx/service.py::_recover_ops",
   "resource": "event:ops_request_terminal",
   "ids": [
    "E62"
   ],
   "kind": "manual_edge"
  },
  {
   "symbol": "src/agentic_fx/service.py::build_app",
   "resource": "event:ops_request_terminal",
   "ids": [
    "E63"
   ],
   "kind": "manual_edge"
  },
  {
   "symbol": "src/agentic_fx/commands.py::Commands._baseline_comparison_lines",
   "resource": "metric:kill_switch_latches",
   "ids": [
    "E64"
   ],
   "kind": "manual_edge"
  },
  {
   "symbol": "src/agentic_fx/status.py::collect_status",
   "resource": "state:app_state.kill_switch_latched",
   "ids": [
    "E65"
   ],
   "kind": "manual_edge"
  },
  {
   "symbol": "src/agentic_fx/commands.py::Commands._dispatch",
   "resource": "state:app_state.kill_switch_latched_at",
   "ids": [
    "F13"
   ],
   "kind": "page"
  },
  {
   "symbol": "src/agentic_fx/ops/events.py::EventStore.emit",
   "resource": "event:ops_request_terminal",
   "ids": [
    "F18"
   ],
   "kind": "page"
  },
  {
   "symbol": "src/agentic_fx/ops/events.py::EventStore.emit",
   "resource": "event:event_stream_health",
   "ids": [
    "F18"
   ],
   "kind": "page"
  },
  {
   "symbol": "afx-ops/afx_ops/web/render.py::render_activity",
   "resource": "activity:kill_switch_latched",
   "ids": [
    "F33"
   ],
   "kind": "page"
  },
  {
   "symbol": "afx-ops/afx_ops/web/render.py::render_activity",
   "resource": "activity:kill_switch_latched_durability_uncertain",
   "ids": [
    "F33"
   ],
   "kind": "page"
  },
  {
   "symbol": "afx-ops/afx_ops/web/render.py::render_activity",
   "resource": "activity:kill_switch_reconcile_required",
   "ids": [
    "F33"
   ],
   "kind": "page"
  },
  {
   "symbol": "afx-ops/afx_ops/web/render.py::render_activity",
   "resource": "activity:kill_switch_reconcile_confirmed",
   "ids": [
    "F33"
   ],
   "kind": "page"
  },
  {
   "symbol": "afx-ops/afx_ops/web/render.py::render_activity",
   "resource": "activity:kill_switch_reconcile_failed",
   "ids": [
    "F33"
   ],
   "kind": "page"
  },
  {
   "symbol": "afx-ops/afx_ops/web/render.py::render_activity",
   "resource": "activity:kill_switch_state_uncertain",
   "ids": [
    "F33"
   ],
   "kind": "page"
  },
  {
   "symbol": "src/agentic_fx/ops/service.py::OpsService.status",
   "resource": "state:app_state.kill_switch_generation",
   "ids": [
    "F34"
   ],
   "kind": "page"
  },
  {
   "symbol": "src/agentic_fx/ops/service.py::OpsService.status",
   "resource": "state:app_state.kill_switch_latched_at",
   "ids": [
    "F34"
   ],
   "kind": "page"
  },
  {
   "symbol": "src/agentic_fx/service.py::build_splash",
   "resource": "state:app_state.kill_switch_latched",
   "ids": [
    "F85"
   ],
   "kind": "page"
  },
  {
   "symbol": "src/agentic_fx/service.py::build_splash",
   "resource": "config:risk.drawdown_kill_pct",
   "ids": [
    "F85"
   ],
   "kind": "page"
  },
  {
   "symbol": "src/agentic_fx/loops/improve_loop.py::ImproveLoop._FLOOR_REPORT_METRIC_KEYS",
   "resource": "metric:kill_switch_latches",
   "ids": [
    "F114"
   ],
   "kind": "page"
  }
 ]
}
```
<!-- /impact-map:covers -->
