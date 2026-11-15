---
{"id": "kill-switch", "owner_role": "conductor", "literals": {"marker:reset_marker": [".reset-in-progress"]}, "resources": ["state:app_state.kill_switch_latched", "state:app_state.kill_switch_generation", "state:app_state.kill_switch_latched_at", "marker:reset_marker", "event:guard_state_changed", "event:ops_request_terminal", "event:event_stream_health", "event:guard_notice_deferred", "event:guard_notice_backfill_skipped", "activity:kill_switch_latched", "activity:kill_switch_latched_durability_uncertain", "activity:kill_switch_reset", "activity:kill_switch_reconcile_required", "activity:kill_switch_reconcile_confirmed", "activity:kill_switch_reconcile_failed", "activity:kill_switch_state_uncertain", "db:account_snapshots", "config:risk.drawdown_kill_pct", "metric:kill_switch_latches", "metric:kill_switch_events", "ops:guard_lane_reentry", "ops:guard_lane_not_held", "ops:guard_state_unavailable", "ops:guard_notice_pending", "ops:guard_notice_generation", "ops:guard_kind"], "anchors": ["src/agentic_fx/core/risk_gate.py::evaluate", "src/agentic_fx/core/executor.py::Executor._evaluate_and_execute_open", "src/agentic_fx/store/state.py::StateStore.load", "src/agentic_fx/store/state.py::StateStore.update", "src/agentic_fx/store/state.py::StateStore.reset_kill_switch", "src/agentic_fx/store/state.py::StateStore.confirm_latched", "src/agentic_fx/ops/service.py::OpsService.reset_kill_switch", "src/agentic_fx/ops/service.py::OpsService.reconcile_kill_switch", "src/agentic_fx/ops/service.py::plan_kill_switch_notice", "src/agentic_fx/ops/service.py::OpsService._sweep_kill_switch", "src/agentic_fx/ops/service.py::OpsService._guard_step", "src/agentic_fx/ops/service.py::OpsService.emit_pending_guard_notices", "src/agentic_fx/commands.py::Commands.dispatch", "src/agentic_fx/commands.py::Commands._killswitch_reconcile", "src/agentic_fx/commands.py::Commands._data_resume", "src/agentic_fx/ops/service.py::OpsService.resume_data", "src/agentic_fx/ops/events.py::EventStore.emit_best_effort", "src/agentic_fx/ops/events.py::EventStore.emit", "src/agentic_fx/ops/service.py::OpsService._terminal", "src/agentic_fx/service.py::build_app", "src/agentic_fx/ops/service.py::OpsService.emit_pending_notices", "src/agentic_fx/ops/service.py::OpsService._current_state", "src/agentic_fx/core/accounting.py::record_snapshot", "src/agentic_fx/core/accounting.py::current_account", "src/agentic_fx/backtest/runner.py::_auto_release_kill_switch", "src/agentic_fx/backtest/runner.py::run_replay", "src/agentic_fx/backtest/metrics.py::compute_metrics", "afx-ops/afx_ops/gate/render.py::_event_text_for_code", "afx-ops/afx_ops/bot/adapter.py::BotAdapter.deliver_events", "afx-ops/afx_ops/notify/payload.py::encode_event", "afx-ops/afx_ops/notify/runtime.py::_Loop._poll_once", "src/agentic_fx/status.py::read_state", "afx-ops/afx_ops/web/render.py::render_activity", "src/agentic_fx/ops/service.py::OpsService.status", "src/agentic_fx/ops/events.py::<module>.FIELDS", "src/agentic_fx/service.py::_build_ops_service"], "related": ["auto-killswitch-latch-emits-no-event", "killswitch-reset-hwm-rebase", "datafeed-outage-discord-notice", "killswitch-latched-activity-repeats", "bot-gateway-disconnect-is-silent"], "tests": ["tests/store/test_state.py", "tests/core/test_executor.py", "tests/ops/test_ac51_killswitch_api.py", "tests/test_commands.py"]}
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
- **F06** `src/agentic_fx/store/state.py::StateStore.confirm_latched` — marker 無しなら no-op、marker 中 raw false は次世代 true、raw true は世代維持。保存後 marker を除く。 Resource: `state:app_state.kill_switch_latched`, `state:app_state.kill_switch_generation`, `marker:reset_marker`.
- **F07** `src/agentic_fx/ops/service.py::OpsService.reset_kill_switch` — ops lane の mutation 内で操作前・結果・現 state の順に guard step を試す。通知失敗と mutation 結果は分離する。 Resource: `state:app_state.kill_switch_latched`, `event:guard_state_changed`.
- **F08** `src/agentic_fx/ops/service.py::OpsService.reconcile_kill_switch` — confirm の前・結果・現 state で guard step を試し、finally で通知失敗を処理する。 Resource: `marker:reset_marker`, `event:guard_state_changed`.
- **F09** `src/agentic_fx/ops/service.py::plan_kill_switch_notice` — 世代と発動時刻を使って該当 incarnation の event を照合。latched 不足を補い、clear は既存 latched がある場合だけ released を補う。released 済みは閉じ、履歴を捏造しない。 Resource: `state:app_state.kill_switch_latched`, `state:app_state.kill_switch_generation`, `state:app_state.kill_switch_latched_at`, `event:guard_state_changed`, `ops:guard_kind`.
- **F10** `src/agentic_fx/ops/service.py::OpsService._sweep_kill_switch` — lane 所有を要求し、plan に基づき最大一件を発行。発動時刻を not_before とし、閉じた世代の欠落を固定 code で記録する。 Resource: `event:guard_state_changed`, `event:guard_notice_backfill_skipped`, `ops:guard_lane_not_held`.
- **F11** `src/agentic_fx/ops/service.py::OpsService._guard_step` — sweep 例外を捕捉し rollback を試み失敗を記録する。_finish_notices が固定 code を記録し、元の操作に失敗を伝えない。 Resource: `event:guard_notice_deferred`, `event:guard_state_changed`.
- **F12** `src/agentic_fx/ops/service.py::OpsService.emit_pending_guard_notices` — 公開 wrapper は同一 thread の lane 再入を拒否し、lane 内で after と現在 state を照合する。 Resource: `ops:guard_lane_reentry`, `event:guard_state_changed`.
- **F13** `src/agentic_fx/commands.py::Commands.dispatch` — shell reset は store を直接操作し前後に wrapper を呼ぶ。ResetNotApplied も投影 state で通知を試みる。state 不確実は別 activity。 Resource: `activity:kill_switch_reset`, `activity:kill_switch_state_uncertain`, `event:guard_state_changed`.
- **F14** `src/agentic_fx/commands.py::Commands._killswitch_reconcile` — shell confirm は直接 store を呼び、操作前後に wrapper を呼ぶ。失敗を activity に記録する。 Resource: `marker:reset_marker`, `activity:kill_switch_reconcile_confirmed`, `activity:kill_switch_reconcile_failed`, `event:guard_state_changed`.
- **F15** `src/agentic_fx/commands.py::Commands._data_resume` — outage.request_resume を直接呼び、ops lane と guard event を経由しない。 Resource: `event:guard_state_changed`.
- **F16** `src/agentic_fx/ops/service.py::OpsService.resume_data` — 注入 callback の成功後、data_feed の resume_requested を _guard_event で発行する。 Resource: `event:guard_state_changed`, `ops:guard_kind`.
- **F17** `src/agentic_fx/ops/events.py::EventStore.emit_best_effort` — INSERT 失敗は rollback と失敗集計で隔離し、後の成功時に health event を試す。schema と時刻下限の検証は try の外にある。 Resource: `event:event_stream_health`, `event:guard_state_changed`.
- **F18** `src/agentic_fx/ops/events.py::EventStore.emit` — schema 検証と INSERT・commit を行い、失敗を呼出し元へ伝える。not_before は event 時刻の下限。 Resource: `event:guard_state_changed`.
- **F19** `src/agentic_fx/ops/service.py::OpsService._terminal` — mutation の監査終端に対応する terminal event を発行する。guard event とは別である。 Resource: `event:ops_request_terminal`.
- **F20** `src/agentic_fx/service.py::build_app` — 起動時 marker の確認要求 activity を記録し、ops service と起動時 sweep を呼ぶ。 Resource: `activity:kill_switch_reconcile_required`, `event:guard_state_changed`.
- **F21** `src/agentic_fx/ops/service.py::OpsService.emit_pending_notices` — 起動時と scheduler tick で lane 内の improve と guard の通知を別々に試す。 Resource: `event:guard_state_changed`, `event:guard_notice_deferred`.
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
- **F33** `afx-ops/afx_ops/web/render.py::render_activity` — web は activity を描画する。event 配送の成功を表すものではない。 Resource: `activity:kill_switch_reset`.
- **F34** `src/agentic_fx/ops/service.py::OpsService.status` — status は投影 state と marker の確認情報を返す。 Resource: `state:app_state.kill_switch_latched`, `marker:reset_marker`.
- **F35** `src/agentic_fx/ops/events.py::<module>.FIELDS` — guard event の固定 field は guard_kind、state、generation。 Resource: `event:guard_state_changed`, `ops:guard_kind`.
- **F36** `src/agentic_fx/service.py::_build_ops_service` — read-only pending 投影と data_resume callback を OpsService に配線する。 Resource: `ops:guard_notice_pending`, `ops:guard_notice_generation`, `event:guard_state_changed`.

- **F37** `afx-ops/afx_ops/gate/render.py::<module>.OPS_EVENT_SPECS` — guard の既定通知を有効、terminal と stream health を無効として同じ文面関数に登録する。 Resource: `event:guard_state_changed`, `event:ops_request_terminal`, `event:event_stream_health`.
- **F38** `afx-ops/afx_ops/gate/render.py::_event_text_for_code` — guard_kind と state により kill switch と data feed の文面を分ける。 Resource: `ops:guard_kind`.

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
- **E07** `src/agentic_fx/commands.py::Commands._data_resume` → `src/agentic_fx/datafeed/outage.py::OutageStateMachine.request_resume`; resource `event:guard_state_changed`。shell の直接呼出し、event は出さない。F15、D04。
- **E08** `src/agentic_fx/service.py::_build_ops_service` → `src/agentic_fx/ops/service.py::OpsService.resume_data`; resource `event:guard_state_changed`。data_resume callback を注入。F16、F36、D04。
- **E17** `src/agentic_fx/service.py::build_app` → `src/agentic_fx/service.py::_build_ops_service`; resource `event:guard_state_changed`。ops service と callback の構築を委譲。F20、F36、D04。
- **E09** `src/agentic_fx/ops/service.py::OpsService.resume_data` → `src/agentic_fx/ops/service.py::OpsService._guard_event`; resource `event:guard_state_changed`。callback 成功後に data_feed を通知。F16、D04。
- **E10** `src/agentic_fx/ops/service.py::OpsService.reset_kill_switch` → `src/agentic_fx/ops/service.py::OpsService._guard_step`; resource `event:guard_state_changed`。前後と現 state を照合。F07、F11、K-NOTICE。
- **E11** `src/agentic_fx/ops/service.py::OpsService.reconcile_kill_switch` → `src/agentic_fx/ops/service.py::OpsService._guard_step`; resource `event:guard_state_changed`。前後と現 state を照合。F08、F11、K-RECONCILE。
- **E12** `src/agentic_fx/ops/service.py::OpsService._guard_step` → `src/agentic_fx/ops/service.py::OpsService._sweep_kill_switch`; resource `event:guard_state_changed`。lane 内で通知を照合。F10–F11、K-NOTICE。
- **E13** `src/agentic_fx/commands.py::Commands.dispatch` → `src/agentic_fx/commands.py::Commands._emit_guard_notices`; resource `event:guard_state_changed`。shell reset の前後 wrapper。F12–F13、D02。
- **E14** `src/agentic_fx/commands.py::Commands._killswitch_reconcile` → `src/agentic_fx/commands.py::Commands._emit_guard_notices`; resource `event:guard_state_changed`。shell confirm の前後 wrapper。F12、F14、D02。
- **E15** `src/agentic_fx/commands.py::Commands._emit_guard_notices` → `src/agentic_fx/ops/service.py::OpsService.emit_pending_guard_notices`; resource `event:guard_state_changed`。lane を取得する公開 wrapper。F12–F14、D02。
- **E16** `src/agentic_fx/service.py::build_app` → `src/agentic_fx/ops/service.py::OpsService.emit_pending_notices`; resource `event:guard_state_changed`。起動時 sweep。F20–F21、K-START。

## 影響先と検証記録

変更時は gate の理由文字列、store の marker/raw、ops と shell の入口、event schema、bot/notify の cursor、status/activity、replay artifact と比較 metric を別々に照合する。literal 索引は callback や引数伝播を証明しない。`ops:guard_version` は宣言された literal だが、guard event schema の version と結び付くことは未確認である。

基準 `0dbcb27f` と作業 tree の `src/`・`afx-ops/` 差分が空であることを確認した。source の読取による下書きであり、test、独立 oracle、頁セルの採点、実通知は未実施。status と StateStore の全分岐比較、reset crash 統合 test の網羅は未確認。

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
      "symbol": "src/agentic_fx/commands.py::Commands._data_resume",
      "resource": "event:guard_state_changed",
      "ids": [
        "E07"
      ],
      "kind": "manual_edge"
    },
    {
      "symbol": "src/agentic_fx/commands.py::Commands._data_resume",
      "resource": "event:guard_state_changed",
      "ids": [
        "F15"
      ],
      "kind": "page"
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
      "symbol": "src/agentic_fx/commands.py::Commands.dispatch",
      "resource": "activity:kill_switch_reset",
      "ids": [
        "F13"
      ],
      "kind": "page"
    },
    {
      "symbol": "src/agentic_fx/commands.py::Commands.dispatch",
      "resource": "activity:kill_switch_state_uncertain",
      "ids": [
        "F13"
      ],
      "kind": "page"
    },
    {
      "symbol": "src/agentic_fx/commands.py::Commands.dispatch",
      "resource": "event:guard_state_changed",
      "ids": [
        "E13"
      ],
      "kind": "manual_edge"
    },
    {
      "symbol": "src/agentic_fx/commands.py::Commands.dispatch",
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
      "symbol": "src/agentic_fx/datafeed/outage.py::OutageStateMachine.request_resume",
      "resource": "event:guard_state_changed",
      "ids": [
        "E07"
      ],
      "kind": "manual_edge"
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
      "resource": "event:guard_notice_deferred",
      "ids": [
        "F11"
      ],
      "kind": "page"
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
      "resource": "event:guard_notice_backfill_skipped",
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
      "resource": "event:guard_notice_deferred",
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
      "symbol": "src/agentic_fx/service.py::_build_ops_service",
      "resource": "event:guard_state_changed",
      "ids": [
        "E08",
        "E17"
      ],
      "kind": "manual_edge"
    },
    {
      "symbol": "src/agentic_fx/service.py::_build_ops_service",
      "resource": "event:guard_state_changed",
      "ids": [
        "F36"
      ],
      "kind": "page"
    },
    {
      "symbol": "src/agentic_fx/service.py::_build_ops_service",
      "resource": "ops:guard_notice_generation",
      "ids": [
        "F36"
      ],
      "kind": "page"
    },
    {
      "symbol": "src/agentic_fx/service.py::_build_ops_service",
      "resource": "ops:guard_notice_pending",
      "ids": [
        "F36"
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
    }
  ]
}
```
<!-- /impact-map:covers -->
