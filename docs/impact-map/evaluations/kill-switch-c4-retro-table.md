# kill-switch 頁 C4 の遡及運用確認: 185 項目の個別裁定表

`affected --page kill-switch --base d7025768^1 --head <0dbcb27f に今の頁と道具を足した一時 commit>` の出力 185 項目を、source を読んで一件ずつ分類した。分類は「無関係」(kill switch の guard event・state・reset・配送の挙動に関わらない)、「関連・既存の頁で足りる」(根拠の F・E・K・D の ID を示す)、「頁の更新が要る (更新済み)」(更新した F の ID を示す)、道具の情報項目の四つ。変更行の特定語の有無は根拠にしていない。

件数: 無関係 27、関連・既存の頁で足りる 143、頁の更新が要る 14、道具の情報項目 1 (計 185)。

| anchor | 種別 | 分類 | 根拠 |
|---|---|---|---|
| `src/agentic_fx/commands.py::Commands._dispatch#_emit_guard_notices` | ambiguous_sub_anchor | 関連・既存の頁で足りる | ambiguous_sub_anchor: E13 を T2 の裁定のとおり再確認 |
| `(なし)` | base_declarations_partial | 道具の情報項目 | base の頁が雛形 (covers_absent)。想定どおり |
| `src/agentic_fx/service.py::build_app` | coarse_parent_changed | 関連・既存の頁で足りる | coarse_parent_changed: 起動 sweep の入口の差し替え (F20・F21・E16) |
| `src/agentic_fx/commands.py::Commands._dispatch [added activity:kill_switch_latched]` | literal_candidates_changed | 関連・既存の頁で足りる | 本体が dispatch から _dispatch へ移った (F13) |
| `src/agentic_fx/commands.py::Commands._dispatch [added activity:kill_switch_reset]` | literal_candidates_changed | 関連・既存の頁で足りる | 本体が dispatch から _dispatch へ移った (F13) |
| `src/agentic_fx/commands.py::Commands._dispatch [added activity:kill_switch_state_uncertain]` | literal_candidates_changed | 関連・既存の頁で足りる | 本体が dispatch から _dispatch へ移った (F13) |
| `src/agentic_fx/commands.py::Commands._dispatch [added state:app_state.kill_switch_generation]` | literal_candidates_changed | 関連・既存の頁で足りる | 本体が dispatch から _dispatch へ移った (F13) |
| `src/agentic_fx/commands.py::Commands._dispatch [added state:app_state.kill_switch_latched]` | literal_candidates_changed | 関連・既存の頁で足りる | 本体が dispatch から _dispatch へ移った (F13) |
| `src/agentic_fx/commands.py::Commands._dispatch [added state:app_state.kill_switch_latched_at]` | literal_candidates_changed | 関連・既存の頁で足りる | 本体が dispatch から _dispatch へ移った (F13) |
| `src/agentic_fx/commands.py::Commands._emit_guard_notices [added log:guard_notice_deferred]` | literal_candidates_changed | 関連・既存の頁で足りる | 宣言済み symbol 内の literal の増減。F の本文に記述あり: F12・F13・E13–E15 |
| `src/agentic_fx/commands.py::Commands.dispatch [removed activity:kill_switch_latched]` | literal_candidates_changed | 関連・既存の頁で足りる | 本体が dispatch から _dispatch へ移った (F13) |
| `src/agentic_fx/commands.py::Commands.dispatch [removed activity:kill_switch_reset]` | literal_candidates_changed | 関連・既存の頁で足りる | 本体が dispatch から _dispatch へ移った (F13) |
| `src/agentic_fx/commands.py::Commands.dispatch [removed activity:kill_switch_state_uncertain]` | literal_candidates_changed | 関連・既存の頁で足りる | 本体が dispatch から _dispatch へ移った (F13) |
| `src/agentic_fx/commands.py::Commands.dispatch [added log:guard_notice_deferred]` | literal_candidates_changed | 関連・既存の頁で足りる | 本体が dispatch から _dispatch へ移った (F13) |
| `src/agentic_fx/commands.py::Commands.dispatch [removed state:app_state.kill_switch_generation]` | literal_candidates_changed | 関連・既存の頁で足りる | 本体が dispatch から _dispatch へ移った (F13) |
| `src/agentic_fx/commands.py::Commands.dispatch [removed state:app_state.kill_switch_latched]` | literal_candidates_changed | 関連・既存の頁で足りる | 本体が dispatch から _dispatch へ移った (F13) |
| `src/agentic_fx/commands.py::Commands.dispatch [removed state:app_state.kill_switch_latched_at]` | literal_candidates_changed | 関連・既存の頁で足りる | 本体が dispatch から _dispatch へ移った (F13) |
| `src/agentic_fx/ops/events.py::kill_switch_event_states [added event:guard_state_changed]` | literal_candidates_changed | 関連・既存の頁で足りる | 宣言済み symbol 内の literal の増減。F の本文に記述あり: F70 |
| `src/agentic_fx/ops/events.py::kill_switch_event_states [added ops:guard_kind]` | literal_candidates_changed | 関連・既存の頁で足りる | 宣言済み symbol 内の literal の増減。F の本文に記述あり: F70 |
| `src/agentic_fx/ops/service.py::GuardLaneNotHeld.code [added ops:guard_lane_not_held]` | literal_candidates_changed | 関連・既存の頁で足りる | 宣言済み symbol 内の literal の増減。F の本文に記述あり: F09–F12・F22・F70・F92 |
| `src/agentic_fx/ops/service.py::GuardLaneReentry.code [added ops:guard_lane_reentry]` | literal_candidates_changed | 関連・既存の頁で足りる | 宣言済み symbol 内の literal の増減。F の本文に記述あり: F09–F12・F22・F70・F92 |
| `src/agentic_fx/ops/service.py::OpsService._current_state [added ops:guard_state_unavailable]` | literal_candidates_changed | 関連・既存の頁で足りる | 宣言済み symbol 内の literal の増減。F の本文に記述あり: F22 |
| `src/agentic_fx/ops/service.py::OpsService._finish_notices [added log:guard_notice_deferred]` | literal_candidates_changed | 関連・既存の頁で足りる | 宣言済み symbol 内の literal の増減。F の本文に記述あり: F09–F12・F22・F70・F92 |
| `src/agentic_fx/ops/service.py::OpsService._sweep_kill_switch [added event:guard_state_changed]` | literal_candidates_changed | 関連・既存の頁で足りる | 宣言済み symbol 内の literal の増減。F の本文に記述あり: F10 |
| `src/agentic_fx/ops/service.py::OpsService._sweep_kill_switch [added log:guard_notice_backfill_skipped]` | literal_candidates_changed | 関連・既存の頁で足りる | 宣言済み symbol 内の literal の増減。F の本文に記述あり: F10 |
| `src/agentic_fx/ops/service.py::OpsService._sweep_kill_switch [added ops:guard_kind]` | literal_candidates_changed | 関連・既存の頁で足りる | 宣言済み symbol 内の literal の増減。F の本文に記述あり: F10 |
| `src/agentic_fx/ops/service.py::OpsService._sweep_kill_switch [added state:app_state.kill_switch_latched_at]` | literal_candidates_changed | 関連・既存の頁で足りる | 宣言済み symbol 内の literal の増減。F の本文に記述あり: F10 |
| `src/agentic_fx/ops/service.py::OpsService.emit_pending_notices [added log:guard_notice_deferred]` | literal_candidates_changed | 関連・既存の頁で足りる | 宣言済み symbol 内の literal の増減。F の本文に記述あり: F21 |
| `src/agentic_fx/ops/service.py::plan_kill_switch_notice [added activity:kill_switch_latched]` | literal_candidates_changed | 関連・既存の頁で足りる | 宣言済み symbol 内の literal の増減。F の本文に記述あり: F09 |
| `src/agentic_fx/ops/service.py::plan_kill_switch_notice [added ops:guard_kind]` | literal_candidates_changed | 関連・既存の頁で足りる | 宣言済み symbol 内の literal の増減。F の本文に記述あり: F09 |
| `src/agentic_fx/ops/service.py::plan_kill_switch_notice [added state:app_state.kill_switch_generation]` | literal_candidates_changed | 関連・既存の頁で足りる | 宣言済み symbol 内の literal の増減。F の本文に記述あり: F09 |
| `src/agentic_fx/ops/service.py::plan_kill_switch_notice [added state:app_state.kill_switch_latched]` | literal_candidates_changed | 関連・既存の頁で足りる | 宣言済み symbol 内の literal の増減。F の本文に記述あり: F09 |
| `src/agentic_fx/ops/service.py::plan_kill_switch_notice [added state:app_state.kill_switch_latched_at]` | literal_candidates_changed | 関連・既存の頁で足りる | 宣言済み symbol 内の literal の増減。F の本文に記述あり: F09 |
| `src/agentic_fx/service.py::_build_ops_service.status_provider [added ops:guard_notice_generation]` | literal_candidates_changed | 関連・既存の頁で足りる | 宣言済み symbol 内の literal の増減。F の本文に記述あり: guard_notice の投影は F92。bot_health の追加は表示 (D09) |
| `src/agentic_fx/service.py::_build_ops_service.status_provider [added ops:guard_notice_pending]` | literal_candidates_changed | 関連・既存の頁で足りる | 宣言済み symbol 内の literal の増減。F の本文に記述あり: guard_notice の投影は F92。bot_health の追加は表示 (D09) |
| `afx-ops/afx_ops/bot/adapter.py::BotAdapter.deliver_events` | symbol_changed | 頁の更新が要る (更新済み) | 送信失敗の三経路 (F29 を書き直した) |
| `afx-ops/afx_ops/bot/runtime.py::_poll` | symbol_changed | 関連・既存の頁で足りる | poll_begin / poll_end で包む (E34・F166) |
| `afx-ops/afx_ops/web/render.py::render_status` | symbol_changed | 無関係 | bot の section を足すだけで kill_switch の描画 (F125) は不変。入力不正は空にして握る |
| `src/agentic_fx/commands.py::Commands._dispatch#reset_kill_switch` | symbol_changed | 関連・既存の頁で足りる | 分割後の reset 本体 (E26・F13) |
| `src/agentic_fx/commands.py::Commands._dispatch#write@kill_switch_state_uncertain` | symbol_changed | 関連・既存の頁で足りる | dispatch→_dispatch の改名で副 anchor が base に無い (F13) |
| `src/agentic_fx/commands.py::Commands._emit_guard_notices` | symbol_changed | 関連・既存の頁で足りる | F12・F13・E13–E15 |
| `src/agentic_fx/commands.py::Commands._killswitch_reconcile` | symbol_changed | 関連・既存の頁で足りる | 前後 wrapper (F14・E14) |
| `src/agentic_fx/commands.py::Commands._projected_state` | symbol_changed | 関連・既存の頁で足りる | F126 |
| `src/agentic_fx/commands.py::Commands.dispatch` | symbol_changed | 関連・既存の頁で足りる | 通知失敗を集める wrapper (F13) |
| `src/agentic_fx/ops/client.py::Runner._op_killswitch_reset` | symbol_changed | 頁の更新が要る (更新済み) | 成功後の固定警告 (F130 を足した) |
| `src/agentic_fx/ops/client.py::format_status` | symbol_changed | 無関係 | bot_health の行を足すだけ。bot_health.lines は不正な入力で空を返す |
| `src/agentic_fx/ops/events.py::EventStore.emit` | symbol_changed | 関連・既存の頁で足りる | not_before (F18) |
| `src/agentic_fx/ops/events.py::EventStore.emit_best_effort` | symbol_changed | 関連・既存の頁で足りる | F17 |
| `src/agentic_fx/ops/events.py::kill_switch_event_states` | symbol_changed | 関連・既存の頁で足りる | F70 |
| `src/agentic_fx/ops/service.py::GuardLaneNotHeld` | symbol_changed | 関連・既存の頁で足りる | F10・K-LANE |
| `src/agentic_fx/ops/service.py::GuardLaneReentry` | symbol_changed | 関連・既存の頁で足りる | F12・F121・K-LANE |
| `src/agentic_fx/ops/service.py::OpsService.__init__` | symbol_changed | 関連・既存の頁で足りる | thread-local の lane 所有 flag を持つ (F129・F12)。HWM 側の rebase 引数は別 (F147) |
| `src/agentic_fx/ops/service.py::OpsService._current_state` | symbol_changed | 関連・既存の頁で足りる | F22 |
| `src/agentic_fx/ops/service.py::OpsService._guard_notices_in_lane` | symbol_changed | 関連・既存の頁で足りる | F12・E31 |
| `src/agentic_fx/ops/service.py::OpsService._guard_step` | symbol_changed | 関連・既存の頁で足りる | F11・E12 |
| `src/agentic_fx/ops/service.py::OpsService._ops_lane` | symbol_changed | 頁の更新が要る (更新済み) | lane 取得口の再入拒否と所有 flag (F129 を足した) |
| `src/agentic_fx/ops/service.py::OpsService._reject_lane_reentry` | symbol_changed | 関連・既存の頁で足りる | F121 |
| `src/agentic_fx/ops/service.py::OpsService._sweep_kill_switch` | symbol_changed | 関連・既存の頁で足りる | F10 |
| `src/agentic_fx/ops/service.py::OpsService.emit_pending_guard_notices` | symbol_changed | 関連・既存の頁で足りる | F12 |
| `src/agentic_fx/ops/service.py::OpsService.emit_pending_notices` | symbol_changed | 関連・既存の頁で足りる | F21 |
| `src/agentic_fx/ops/service.py::OpsService.reconcile_kill_switch` | symbol_changed | 関連・既存の頁で足りる | F08 |
| `src/agentic_fx/ops/service.py::OpsService.reset_kill_switch` | symbol_changed | 関連・既存の頁で足りる | F07 |
| `src/agentic_fx/ops/service.py::plan_kill_switch_notice` | symbol_changed | 関連・既存の頁で足りる | F09 |
| `src/agentic_fx/service.py::_GuardNoticeProbe.__call__` | symbol_changed | 関連・既存の頁で足りる | F92・E60 |
| `src/agentic_fx/service.py::_build_ops_service` | symbol_changed | 関連・既存の頁で足りる | F36 |
| `src/agentic_fx/service.py::_build_ops_service.status_provider` | symbol_changed | 関連・既存の頁で足りる | guard_notice の投影は F92。bot_health の追加は表示 (D09) |
| `src/agentic_fx/service.py::_sweep_improve_notices` | symbol_changed | 関連・既存の頁で足りる | E32・E33 |
| `src/agentic_fx/service.py::build_app#emit_pending_notices` | symbol_changed | 関連・既存の頁で足りる | E16・F21 |
| `src/agentic_fx/status.py::collect_status` | symbol_changed | 無関係 | bot_health の取得を try で包み、失敗は空にする。kill_switch の取得 (E65) は不変 |
| `src/agentic_fx/status.py::render_status` | symbol_changed | 無関係 | bot_health の行を足すだけ。state の行 (F127) は不変 |
| `afx-ops/afx_ops/bot/adapter.py::<module>.RESPONSE_DEADLINE_SECONDS` | unmapped_changed_unmapped_symbol | 関連・既存の頁で足りる | 期限は interaction の応答と、poll 周回内の後始末 edit (_edit の deadline) の待ちに使う。周回を占める時間の上限 (F173・F166) |
| `afx-ops/afx_ops/bot/adapter.py::<module>.REST_RETRY_MAX_SECONDS` | unmapped_changed_unmapped_symbol | 関連・既存の頁で足りる | 承認の fetch・履歴照合の再試行の待ちの上限。起動 reconcile と poll 周回を占める (F172・F173) |
| `afx-ops/afx_ops/bot/adapter.py::<module>.REST_RETRY_SECONDS` | unmapped_changed_unmapped_symbol | 関連・既存の頁で足りる | 承認の fetch・履歴照合の再試行の待ち。起動 reconcile と poll 周回を占め、配送の開始を待たせる (F172・F173) |
| `afx-ops/afx_ops/bot/adapter.py::BotAdapter.__init__` | unmapped_changed_unmapped_symbol | 関連・既存の頁で足りる | ProgressTracker を受ける。配送の無進捗の判定の入口で F166 が記述 |
| `afx-ops/afx_ops/bot/adapter.py::BotAdapter._commit_post` | unmapped_changed_unmapped_symbol | 関連・既存の頁で足りる | cursor の確定。state の I/O 失敗は一時、内容の不正は恒久の理由で BotFailure (F168)。K-CONSUME の cursor 保存 |
| `afx-ops/afx_ops/bot/adapter.py::BotAdapter._commit_skip` | unmapped_changed_unmapped_symbol | 関連・既存の頁で足りる | 設定で出さない event の cursor 確定。同上 (F29・F168・K-CONSUME) |
| `afx-ops/afx_ops/bot/adapter.py::BotAdapter._edit` | unmapped_changed_unmapped_symbol | 関連・既存の頁で足りる | 承認表示の edit。恒久の拒否は BotFailure で bot 全体を止める (F165)。poll 周回の _sync_views から呼ばれる (F173・F168) |
| `afx-ops/afx_ops/bot/adapter.py::BotAdapter._fetch` | unmapped_changed_unmapped_symbol | 関連・既存の頁で足りる | 起動 reconcile の fetch。一時失敗の再試行中は poll・配送が始まらず、恒久拒否は bot を止める (F172・F165・F170) |
| `afx-ops/afx_ops/bot/adapter.py::BotAdapter._find_posted` | unmapped_changed_unmapped_symbol | 関連・既存の頁で足りる | 承認投稿の履歴照合。一時失敗は再試行 (配送の開始を待たせる)、恒久拒否は BotFailure (F172・F173・F165) |
| `afx-ops/afx_ops/bot/adapter.py::BotAdapter._initialize_cursor` | unmapped_changed_unmapped_symbol | 関連・既存の頁で足りる | 初回の cursor 初期化の失敗理由が I/O (一時) と内容 (恒久) に分かれる (F168、K-CONSUME) |
| `afx-ops/afx_ops/bot/adapter.py::BotAdapter._load_and_invalidate` | unmapped_changed_unmapped_symbol | 関連・既存の頁で足りる | prepare から login の前に呼ぶ。state の I/O 失敗は一時、内容不正は恒久で bot 全体が止まる (F172・F168・F170) |
| `afx-ops/afx_ops/bot/adapter.py::BotAdapter._observe_high` | unmapped_changed_unmapped_symbol | 関連・既存の頁で足りる | high watermark の確定。後退は恒久 (F168)。K-CONSUME |
| `afx-ops/afx_ops/bot/adapter.py::BotAdapter._repost_locked` | unmapped_changed_unmapped_symbol | 関連・既存の頁で足りる | 承認投稿の出し直し。sync_pending と起動 reconcile から呼ばれ、恒久拒否は BotFailure (F172・F173・F165) |
| `afx-ops/afx_ops/bot/adapter.py::BotAdapter._respond` | unmapped_changed_unmapped_symbol | 関連・既存の頁で足りる | 確認 message の束縛で gate の state を単一 worker で書く。cursor の保存と同じ worker・file を共有する (F168・K-CONSUME) |
| `afx-ops/afx_ops/bot/adapter.py::BotAdapter._respond_within` | unmapped_changed_unmapped_symbol | 無関係 | interaction の応答一回を wait_for で待ち、失敗は (False, None) で握る。BotFailure を出さず再試行も state の書込みも無く、poll 周回とは別の callback で動く (source で確認) |
| `afx-ops/afx_ops/bot/adapter.py::BotAdapter._save` | unmapped_changed_unmapped_symbol | 関連・既存の頁で足りる | bot state の保存失敗の理由を型の reason にする (F168) |
| `afx-ops/afx_ops/bot/adapter.py::BotAdapter._send_approval` | unmapped_changed_unmapped_symbol | 関連・既存の頁で足りる | 承認投稿の送信。恒久拒否は BotFailure で bot を止める (F165・F173・F172) |
| `afx-ops/afx_ops/bot/adapter.py::BotAdapter._send_substitute` | unmapped_changed_unmapped_symbol | 頁の更新が要る (更新済み) | 不正本文の固定代替文 (F29 に記述を足した) |
| `afx-ops/afx_ops/bot/adapter.py::BotAdapter._update_display_locked` | unmapped_changed_unmapped_symbol | 関連・既存の頁で足りる | 承認表示の更新。poll 周回の _sync_views から呼ばれ、恒久拒否は bot を止める (F173・F165) |
| `afx-ops/afx_ops/bot/adapter.py::BotAdapter.on_component` | unmapped_changed_unmapped_symbol | 関連・既存の頁で足りる | 応答後の _sync_views の BotFailure を _fail で記録し、bot 全体を止める (F170・F173) |
| `afx-ops/afx_ops/bot/adapter.py::open_state_dir` | unmapped_changed_unmapped_symbol | 関連・既存の頁で足りる | state dir を開く失敗の分類 (symlink・非 dir・無しは恒久、他の OS 失敗は一時)。F168 |
| `afx-ops/afx_ops/bot/runtime.py::<module>.FATAL_CLOSE_CODES` | unmapped_changed_unmapped_symbol | 関連・既存の頁で足りる | 再接続しても直らない gateway close は恒久の終了 (F168・K-CONSUME) |
| `afx-ops/afx_ops/bot/runtime.py::<module>.LOGIN_BACKOFF_MAX_SECONDS` | unmapped_changed_unmapped_symbol | 関連・既存の頁で足りる | 同上の上限 (F170) |
| `afx-ops/afx_ops/bot/runtime.py::<module>.LOGIN_BACKOFF_SECONDS` | unmapped_changed_unmapped_symbol | 関連・既存の頁で足りる | login の一時失敗の再試行の待ち。起動の回復 (F170) |
| `afx-ops/afx_ops/bot/runtime.py::<module>.MONITOR_INTERVAL` | unmapped_changed_unmapped_symbol | 関連・既存の頁で足りる | 無進捗 monitor の確認の間隔 (F167) |
| `afx-ops/afx_ops/bot/runtime.py::<module>.RC_BOT_FAILED` | unmapped_changed_unmapped_symbol | 関連・既存の頁で足りる | 旧い失敗コードの削除。終了コードは exits に移った (F168) |
| `afx-ops/afx_ops/bot/runtime.py::<module>.RC_OK` | unmapped_changed_unmapped_symbol | 関連・既存の頁で足りる | 終了コード定数の exits への移動 (F168) |
| `afx-ops/afx_ops/bot/runtime.py::<module>.REASONS` | unmapped_changed_unmapped_symbol | 関連・既存の頁で足りる | 失敗理由の表の exits への移動 (F168) |
| `afx-ops/afx_ops/bot/runtime.py::_Client` | unmapped_changed_unmapped_symbol | 関連・既存の頁で足りる | gateway の状態を progress へ渡す Client。gateway の健全性の根拠 (F166) |
| `afx-ops/afx_ops/bot/runtime.py::_Client.on_connect` | unmapped_changed_unmapped_symbol | 関連・既存の頁で足りる | READY で gateway を健全に戻す (F166) |
| `afx-ops/afx_ops/bot/runtime.py::_Client.on_disconnect` | unmapped_changed_unmapped_symbol | 関連・既存の頁で足りる | 切断で不健全の起点を立てる (F166) |
| `afx-ops/afx_ops/bot/runtime.py::_Client.on_resumed` | unmapped_changed_unmapped_symbol | 関連・既存の頁で足りる | RESUMED で gateway を健全に戻す (F166) |
| `afx-ops/afx_ops/bot/runtime.py::_gateway` | unmapped_changed_unmapped_symbol | 関連・既存の頁で足りる | gateway の再接続と恒久拒否の終了。10 分の窓は F166・F167 |
| `afx-ops/afx_ops/bot/runtime.py::_is_permanent` | unmapped_changed_unmapped_symbol | 関連・既存の頁で足りる | 失敗が恒久かの判定で終了コードを決める (F168) |
| `afx-ops/afx_ops/bot/runtime.py::_login` | unmapped_changed_unmapped_symbol | 関連・既存の頁で足りる | login の一時失敗は backoff 再試行、恒久拒否は止まる (F165・F170) |
| `afx-ops/afx_ops/bot/runtime.py::_main` | unmapped_changed_unmapped_symbol | 関連・既存の頁で足りる | 初期化中の中断でも shutdown を記録する。終了の枠 (F170) |
| `afx-ops/afx_ops/bot/runtime.py::_main_body` | unmapped_changed_unmapped_symbol | 頁の更新が要る (更新済み) | 起動と常駐の本体 (F170 を足した) |
| `afx-ops/afx_ops/bot/runtime.py::_monitor` | unmapped_changed_unmapped_symbol | 頁の更新が要る (更新済み) | 無進捗の monitor (F167 を足した) |
| `afx-ops/afx_ops/bot/runtime.py::_quiet_discord_logging` | unmapped_changed_unmapped_symbol | 無関係 | 削除。discord の log の扱いは logsafe (record の中身を出さない固定語化) に移り、配送・state・終了の判断を持たない |
| `afx-ops/afx_ops/bot/runtime.py::_reset_http` | unmapped_changed_unmapped_symbol | 関連・既存の頁で足りる | 失敗した login の HTTP session を閉じる後始末。login の回復 (F170) |
| `afx-ops/afx_ops/bot/runtime.py::_retrieve` | unmapped_changed_unmapped_symbol | 無関係 | done callback で完了済み task の exception() を読むだけ (取消し以外)。task の結果の扱いは _main_body が .result() で行う (F170) ので判断は変わらない |
| `afx-ops/afx_ops/bot/runtime.py::_spawn` | unmapped_changed_unmapped_symbol | 無関係 | asyncio.create_task に _retrieve の callback を付ける補助。task の起動順・待ち・終了の判断は変えない |
| `afx-ops/afx_ops/bot/runtime.py::_start_health` | unmapped_changed_unmapped_symbol | 無関係 | health writer を別 daemon thread で起こす。作成の失敗は None にして握り、health の task は終了判断の集合に入れない (_main_body)。loop は fsync しない |
| `afx-ops/afx_ops/bot/runtime.py::_startup` | unmapped_changed_unmapped_symbol | 関連・既存の頁で足りる | 起動の順序 (state の無効化→login→起動 reconcile)。F170 |
| `afx-ops/afx_ops/bot/runtime.py::new_watchdog` | unmapped_changed_unmapped_symbol | 関連・既存の頁で足りる | loop 停止の watchdog の生成。停止時に process を終え再起動へ回す (F168) |
| `afx-ops/afx_ops/bot/runtime.py::run` | unmapped_changed_unmapped_symbol | 頁の更新が要る (更新済み) | bot の入口 (F171 を足した) |
| `afx-ops/afx_ops/cli.py::_start` | unmapped_changed_unmapped_symbol | 無関係 | bot の起動前に log の handler を差し替える。handler は固定語を stderr に出すだけで、例外を握り、配送・state・終了の判断を持たない |
| `afx-ops/afx_ops/cli.py::main` | unmapped_changed_unmapped_symbol | 無関係 | 同上 (finally で handler を元に戻す) |
| `afx-ops/afx_ops/gate/state.py::StateError` | unmapped_changed_unmapped_symbol | 関連・既存の頁で足りる | 内容の不正は恒久の理由 reason を持つ (F168) |
| `afx-ops/afx_ops/gate/state.py::StateIOError` | unmapped_changed_unmapped_symbol | 関連・既存の頁で足りる | I/O 失敗は一時の理由 bot_state_unavailable (F168) |
| `afx-ops/afx_ops/gate/state.py::load` | unmapped_changed_unmapped_symbol | 関連・既存の頁で足りる | snapshot の I/O 失敗を StateIOError に分ける (F168) |
| `afx-ops/afx_ops/gate/state.py::save` | unmapped_changed_unmapped_symbol | 関連・既存の頁で足りる | 同上 (cursor 保存の失敗分類。K-CONSUME) |
| `afx-ops/afx_ops/isolation/startup.py::run` | unmapped_changed_unmapped_symbol | 無関係 | docstring のみの変更 (diff で確認)。挙動の変更なし |
| `afx-ops/afx_ops/snapshot.py::SnapshotContentError` | unmapped_changed_unmapped_symbol | 関連・既存の頁で足りる | snapshot の I/O 失敗と内容の不正を型で分ける。cursor 保存の失敗分類 (F168) |
| `afx-ops/afx_ops/snapshot.py::SnapshotError` | unmapped_changed_unmapped_symbol | 関連・既存の頁で足りる | snapshot の I/O 失敗と内容の不正を型で分ける。cursor 保存の失敗分類 (F168) |
| `afx-ops/afx_ops/snapshot.py::SnapshotIOError` | unmapped_changed_unmapped_symbol | 関連・既存の頁で足りる | snapshot の I/O 失敗と内容の不正を型で分ける。cursor 保存の失敗分類 (F168) |
| `afx-ops/afx_ops/snapshot.py::_remove_stale_temps` | unmapped_changed_unmapped_symbol | 関連・既存の頁で足りる | write_snapshot (cursor の保存を含む) の前処理。失敗は SnapshotIOError (一時) になり、以前の恒久から変わった (F168・K-CONSUME) |
| `afx-ops/afx_ops/snapshot.py::_remove_stale_temps_once` | unmapped_changed_unmapped_symbol | 関連・既存の頁で足りる | 同上。初回の掃除の失敗が cursor の保存の失敗として現れる (F168) |
| `afx-ops/afx_ops/snapshot.py::read_snapshot` | unmapped_changed_unmapped_symbol | 関連・既存の頁で足りる | 読込失敗を型で分ける (F168) |
| `afx-ops/afx_ops/snapshot.py::write_snapshot` | unmapped_changed_unmapped_symbol | 関連・既存の頁で足りる | 書込失敗を型で分ける (F168) |
| `afx-ops/afx_ops/web/render.py::<module>.ALTERNATE_NONE_TEXT` | unmapped_changed_unmapped_symbol | 無関係 | daemon の status の bot_health を描く固定文。web は daemon API だけを読む純粋な描画で、配送を操作しない |
| `afx-ops/afx_ops/web/render.py::<module>.BOT_ALTERNATE_PUSH` | unmapped_changed_unmapped_symbol | 無関係 | 同上 (許可語の集合) |
| `afx-ops/afx_ops/web/render.py::<module>.BOT_HEALTH_STATES` | unmapped_changed_unmapped_symbol | 無関係 | 同上 (許可語の集合) |
| `afx-ops/afx_ops/web/render.py::<module>.INTERACTION_UNAVAILABLE_TEXT` | unmapped_changed_unmapped_symbol | 無関係 | 同上 (固定文) |
| `afx-ops/afx_ops/web/render.py::<module>._BOT_CODE` | unmapped_changed_unmapped_symbol | 無関係 | 同上 (表示する code の形の検査) |
| `afx-ops/afx_ops/web/render.py::_bot_section` | unmapped_changed_unmapped_symbol | 無関係 | 同上。入力が不正なら空の section にするだけで配送に触れない |
| `src/agentic_fx/commands.py::<module>._log` | unmapped_changed_unmapped_symbol | 関連・既存の頁で足りる | shell の通知失敗の warning 用 logger (F13) |
| `src/agentic_fx/commands.py::<module>._notice_failures` | unmapped_changed_unmapped_symbol | 関連・既存の頁で足りる | shell の通知失敗の収集 (F13) |
| `src/agentic_fx/ops/events.py::EventStore._check_floor` | unmapped_changed_unmapped_symbol | 関連・既存の頁で足りる | not_before の UTC aware 検査 (F17・F18) |
| `src/agentic_fx/ops/events.py::EventStore._stamp` | unmapped_changed_unmapped_symbol | 関連・既存の頁で足りる | event 時刻を max(now, not_before) にする (F18) |
| `src/agentic_fx/ops/service.py::<module>._KILL_SWITCH_STATES` | unmapped_changed_unmapped_symbol | 関連・既存の頁で足りる | 照合する state の集合 (F09) |
| `src/agentic_fx/ops/service.py::<module>._backfill_skip_lock` | unmapped_changed_unmapped_symbol | 関連・既存の頁で足りる | backfill skip の log を一度だけにする lock (F10) |
| `src/agentic_fx/ops/service.py::<module>._backfill_skip_logged` | unmapped_changed_unmapped_symbol | 関連・既存の頁で足りる | 同上 (F10) |
| `src/agentic_fx/ops/service.py::GuardLaneNotHeld.__init__` | unmapped_changed_unmapped_symbol | 関連・既存の頁で足りる | lane 非所有の固定 code (F10・K-LANE) |
| `src/agentic_fx/ops/service.py::GuardLaneReentry.__init__` | unmapped_changed_unmapped_symbol | 関連・既存の頁で足りる | lane 再入の固定 code (F12・F121・K-LANE) |
| `src/agentic_fx/ops/service.py::OpsService._finish_notices` | unmapped_changed_unmapped_symbol | 関連・既存の頁で足りる | 通知失敗の固定 code の warning (F11) |
| `src/agentic_fx/ops/service.py::OpsService._lane_owned` | unmapped_changed_unmapped_symbol | 頁の更新が要る (更新済み) | lane 所有 flag (F129 を足した) |
| `src/agentic_fx/ops/service.py::OpsService.emit_pending_improve_notices` | unmapped_changed_unmapped_symbol | 頁の更新が要る (更新済み) | GuardLaneReentry を伝える (F12 に足した) |
| `src/agentic_fx/ops/service.py::_GuardNoticeFailed` | unmapped_changed_unmapped_symbol | 関連・既存の頁で足りる | event 保存失敗の固定例外 (F10・F11) |
| `src/agentic_fx/ops/service.py::_NoticeRun` | unmapped_changed_unmapped_symbol | 関連・既存の頁で足りる | 通知専用の失敗記録 (F11) |
| `src/agentic_fx/ops/service.py::_parse_utc` | unmapped_changed_unmapped_symbol | 関連・既存の頁で足りる | event 時刻と発動時刻の UTC 比較 (F09) |
| `src/agentic_fx/ops_setup/interactive.py::<module>.REASON_CODES` | unmapped_changed_unmapped_symbol | 無関係 | systemd_too_old を報告に使える語へ足すだけ。拒否の判断は _check_bot_systemd (F169) にあり、ここは理由の語の正規化 |
| `src/agentic_fx/ops_setup/stages.py::<module>._BOT_DROPIN_WARN` | unmapped_changed_unmapped_symbol | 無関係 | drop-in が exact でないときの警告 code の文字列。判断は _check_bot_systemd (F169) |
| `src/agentic_fx/ops_setup/stages.py::<module>._BOT_DROPIN_WARN_TEXT` | unmapped_changed_unmapped_symbol | 無関係 | 同上 (警告の文言) |
| `src/agentic_fx/ops_setup/stages.py::OpsSetup._check_bot_systemd` | unmapped_changed_unmapped_symbol | 関連・既存の頁で足りる | bot の recovery drop-in を書く前に systemd の version を確かめ、満たさなければ enable を拒否する (F169) |
| `src/agentic_fx/ops_setup/stages.py::OpsSetup._disable` | unmapped_changed_unmapped_symbol | 関連・既存の頁で足りる | bot を外すとき drop-in も整合させる設置手順 (F169) |
| `src/agentic_fx/ops_setup/stages.py::OpsSetup._enable` | unmapped_changed_unmapped_symbol | 関連・既存の頁で足りる | bot の enable で drop-in を設置する手順 (F169) |
| `src/agentic_fx/ops_setup/stages.py::OpsSetup._rollback_install` | unmapped_changed_unmapped_symbol | 関連・既存の頁で足りる | 設置失敗時の drop-in の巻戻し (F169) |
| `src/agentic_fx/ops_setup/stages.py::OpsSetup._uninstall_gap` | unmapped_changed_unmapped_symbol | 関連・既存の頁で足りる | uninstall で drop-in が残らないようにする確認 (F169) |
| `src/agentic_fx/ops_setup/stages.py::_bot_health_lines` | unmapped_changed_unmapped_symbol | 無関係 | ops status に bot health の行を足す表示。失敗は表示から除く |
| `src/agentic_fx/ops_setup/stages.py::run_ops_command` | unmapped_changed_unmapped_symbol | 無関係 | command の入口に bot health の表示行を足すだけ |
| `src/agentic_fx/ops_setup/systemd.py::Systemd.version` | unmapped_changed_unmapped_symbol | 関連・既存の頁で足りる | drop-in の前提 (systemd の version) を答える (F169) |
| `src/agentic_fx/ops_setup/units.py::<module>.BOT_DROPIN` | unmapped_changed_unmapped_symbol | 関連・既存の頁で足りる | bot unit の再起動の方針を書く drop-in の本文 (F169) |
| `src/agentic_fx/ops_setup/units.py::<module>.BOT_DROPIN_NAME` | unmapped_changed_unmapped_symbol | 関連・既存の頁で足りる | drop-in の file 名 (F169) |
| `src/agentic_fx/ops_setup/units.py::<module>.BOT_MIN_SYSTEMD_VERSION` | unmapped_changed_unmapped_symbol | 関連・既存の頁で足りる | RestartSteps が使える最小の systemd version (F169) |
| `src/agentic_fx/ops_setup/units.py::<module>.BOT_PERMANENT_EXIT_STATUS` | unmapped_changed_unmapped_symbol | 頁の更新が要る (更新済み) | 再起動を止める終了コード (F169 を足した) |
| `src/agentic_fx/ops_setup/units.py::UnitPaths.artifact_paths` | unmapped_changed_unmapped_symbol | 関連・既存の頁で足りる | 設置物の一覧に drop-in を含める (F169) |
| `src/agentic_fx/ops_setup/units.py::UnitPaths.bot_dropin_path` | unmapped_changed_unmapped_symbol | 関連・既存の頁で足りる | drop-in の path (F169) |
| `src/agentic_fx/ops_setup/units.py::_remove_bot_dropin` | unmapped_changed_unmapped_symbol | 関連・既存の頁で足りる | drop-in の除去 (F169) |
| `src/agentic_fx/ops_setup/units.py::bot_dropin_exact` | unmapped_changed_unmapped_symbol | 関連・既存の頁で足りる | drop-in の内容の一致検査 (F169) |
| `src/agentic_fx/ops_setup/units.py::bot_systemd_problem` | unmapped_changed_unmapped_symbol | 関連・既存の頁で足りる | systemd の version が足りないときの拒否理由 (F169) |
| `src/agentic_fx/ops_setup/units.py::install_units` | unmapped_changed_unmapped_symbol | 関連・既存の頁で足りる | drop-in を含めた設置 (F169) |
| `src/agentic_fx/ops_setup/units.py::uninstall_feature` | unmapped_changed_unmapped_symbol | 関連・既存の頁で足りる | drop-in を含めた除去 (F169) |
| `src/agentic_fx/ops_setup/units.py::units_match` | unmapped_changed_unmapped_symbol | 関連・既存の頁で足りる | drop-in を含めた設置物の一致検査 (F169) |
| `src/agentic_fx/service.py::_GuardNoticeProbe` | unmapped_changed_unmapped_symbol | 関連・既存の頁で足りる | status 用の読取り照合 (F92・E60) |
| `src/agentic_fx/service.py::_GuardNoticeProbe.__init__` | unmapped_changed_unmapped_symbol | 関連・既存の頁で足りる | 失敗の遷移の 1 行 log のための flag (F92) |
| `src/agentic_fx/status.py::StatusData` | unmapped_changed_unmapped_symbol | 無関係 | bot_health の表示 field を足す dataclass。値は表示用 |
| `afx-ops/afx_ops/bot/exits.py` | unmapped_new_file | 頁の更新が要る (更新済み) | 終了コードと失敗理由の表 (F168 を足した) |
| `afx-ops/afx_ops/bot/logsafe.py` | unmapped_new_file | 無関係 | discord・aiohttp・asyncio の log の record を固定語と件数に畳む。handler の失敗で既定の出力に落とさない。配送・state・終了の判断を持たない |
| `afx-ops/afx_ops/bot/progress.py` | unmapped_new_file | 頁の更新が要る (更新済み) | 配送・gateway・poll の無進捗判定 (F166 を足した) |
| `afx-ops/afx_ops/bot/rest.py` | unmapped_new_file | 頁の更新が要る (更新済み) | REST 失敗の恒久・一時の分類 (F165 を足した) |
| `afx-ops/afx_ops/bot/watchdog.py` | unmapped_new_file | 関連・既存の頁で足りる | loop 停止 (10 分) と停止処理の超過で process を直接終える。終了コード 80 / 79 で再起動対象 (F168) |
| `afx-ops/afx_ops/health.py` | unmapped_new_file | 無関係 | health snapshot の writer。別 daemon thread で書き、失敗は握る。終了時の finalize は最大 1 秒待って戻るだけ。終了判断の集合に入らない (F170) |
| `src/agentic_fx/core/kill_switch_words.py` | unmapped_new_file | 頁の更新が要る (更新済み) | reset 後の固定警告 (F130 を足した) |
| `src/agentic_fx/ops/bot_health.py` | unmapped_new_file | 無関係 | daemon の status 用に snapshot を読んで整形する。どの失敗でも unknown にして例外を出さず、配送・state に触れない |
