# Kill switch 評価

## 不一致裁定

## 凍結母数

## 三分類

## 初回結果

## 履歴再現 a

## 履歴再現 b

## 履歴再現 c

## 初回測定 (run2、正解表の凍結直後)

- 正解表: `oracle/kill-switch-a.json` (A、188 行) と `oracle/kill-switch-b.json` (B、138 行、repo 外の隔離 checkout で作成) を第三者の裁定案と指揮者の抜取り照合で確定し `oracle/kill-switch-frozen.json` (221 行、critical 161、G_lit 105 / G_reach 116) に凍結した。sha256: A `798c080f…`、B `49d1d2e5…`、frozen `ae6911f0…`。採用前に確定した裁定: guard_version は評価用履歴の guard の版で kill switch と無関係のため母数から外し頁の resource からも外した / 採用比較 metric の表示行は critical にしない / event の読み手は producer 判定に使うものだけ critical / daemon の Notifier は critical にしない。
- 機械セル (G_lit critical): 母数 78、TP 37、FN 41、U 0。FN の主因は抽出器の実装が設計と食い違っていたこと — module 定数を `<module>.NAME` で記録していない (§2.2)、SQL・JSON path の文字列中の table 名・key を照合していない (§2.3)、dataclass の field 宣言を拾っていない。
- 頁セル (G_lit ∪ G_reach critical): 母数 161、TP 64、FN 97、U 0。頁の被覆宣言 106 件に対し正解表 221 行。
- 判定: **初回は不合格** (§4.3 の critical FN=0 を満たさない)。抽出器を設計の形へ合わせ、頁を手動補完して再測定する。再測定の点はこの初回の点と並べて記録する。
- 実測: extract 367 file / 5.9 MB / 0.82 秒 (一回)。

## 正解表の訂正 (run4 の後)

- `src/agentic_fx/store/state.py::StateStore._marker` × `marker:reset_marker` の class を G_lit → G_reach に訂正。理由: この symbol は literal を持たず定数名 `_MARKER_SUFFIX` を参照するだけで、literal は `<module>._MARKER_SUFFIX` 側にある (G_lit の定義「宣言 literal を持つ site」に当たらない)。出力を見た後の訂正なので記録する。役割: 指揮者。frozen sha256: `ae6911f0f7b3…` → `b929322136ed…`。
- 訂正前の点 (run4): 機械セル critical 母数 78 / TP 77 / FN 1 / U 0、頁セル critical 母数 161 / TP 64 / FN 97 / U 0。

## 抽出器の是正の経過 (設計の形に合わせたもの、正解表は見せていない)

| run | 変更 | 機械 critical FN | 頁 critical FN |
|---|---|---|---|
| run2 | 初回 | 41 / 78 | 97 / 161 |
| run3 | module 定数 `<module>.NAME`、文字列中の識別子境界の一致、dataclass field、marker literal の明示 | 4 / 78 | 97 / 161 |
| run4 | class 定数 `Class.NAME`、正解 symbol の入れ子を同じ symbol として数える、marker literal を suffix 文字列に | 1 / 78 | 97 / 161 |

## 頁の手動補完後の測定 (final、凍結表 b9293221…)

| セル | critical 母数 | TP | FN | U | 全体 TP / FN / FP |
|---|---|---|---|---|---|
| 機械 (G_lit) | 77 | 77 | 0 | 0 | 104 / 0 / 56 |
| 頁 (G_lit ∪ G_reach) | 161 | 161 | 0 | 0 | 174 / 47 / 16 |

- §4.3 の初回要件のうち機械セル・頁セルの critical FN=0 / U=0 を満たす (`pass_candidate: true`)。誤った direct 断定: 初版は reader/writer を断定しないので該当なし。未裁定 U: 0。
- 頁の補完は初回測定の後に FN 一覧を見て行った手動補完であり、補完前の点 (頁 critical FN 97 / 161) と並べて記録する。補完した行は 1 行ずつ source で確かめた (正解表の rationale を写していない)。補完の途中で既存の頁の誤り 1 件 (F13・E13 の anchor が wrapper の `Commands.dispatch` で、本体は `Commands._dispatch`) を見つけて直した。
- 頁の意味の抜取り確認 (指揮者): F43・F55・F67・F78・E21・E37 の 6 件を source と照合し、6 件とも記述が実動作と一致。
- FP: 機械セル 56 件・頁セル 16 件 (正解表に無い候補・被覆)。主に表示・読取の literal 候補と、state field / activity kind の同名 literal の両出し。仕分け時間は個別に計測していない (未計測)。FP は合否条件にしない (§4.3)。
- 正解表について補完者が挙げた注記 (正解表は変えていない): `daily_start_snapshot` は日次損失の基準で DD kill の HWM 入力ではない / `PaperBroker.equity` の relation の向きが読みにくい。T2 の正解表見直しで扱う。
- 実測 (一回): extract 1.85 秒 (367 file)。初回 run1 は 0.82 秒。cold/warm の反復は T2。

### 非 critical FN と処置 (最終測定 final2 の 83 件)

処置: いずれも非 critical (表示・読取の consumer、API の中継、補助的な literal site)。初回の合格要件 (critical) の外で、頁への手動補完は T2 で行う。それまで頁はこの範囲を被覆しないと明示する。下表は最終測定の集合そのもの (以前の 47 件の表を置き換えた)。

| resource | symbol | relation |
|---|---|---|
| `activity:kill_switch_latched` | `afx-ops/afx_ops/web/render.py::render_activity` | consumes |
| `activity:kill_switch_latched` | `src/agentic_fx/ops/client.py::Runner._op_activity` | consumes |
| `activity:kill_switch_latched` | `src/agentic_fx/ops/service.py::OpsService.activity` | exposes |
| `activity:kill_switch_latched` | `src/agentic_fx/status.py::_activity` | consumes |
| `activity:kill_switch_latched_durability_uncertain` | `afx-ops/afx_ops/web/render.py::render_activity` | consumes |
| `activity:kill_switch_latched_durability_uncertain` | `src/agentic_fx/core/executor.py::Executor._notify` | invokes |
| `activity:kill_switch_latched_durability_uncertain` | `src/agentic_fx/core/executor.py::Executor.defer_notifications` | consumes |
| `activity:kill_switch_latched_durability_uncertain` | `src/agentic_fx/core/notifier.py::Notifier.send` | consumes |
| `activity:kill_switch_latched_durability_uncertain` | `src/agentic_fx/loops/trade_loop.py::TradeLoop._run_once_impl` | exposes |
| `activity:kill_switch_latched_durability_uncertain` | `src/agentic_fx/loops/trade_loop.py::TradeLoop._run_signal_mission` | exposes |
| `activity:kill_switch_latched_durability_uncertain` | `src/agentic_fx/ops/client.py::Runner._op_activity` | consumes |
| `activity:kill_switch_latched_durability_uncertain` | `src/agentic_fx/ops/service.py::OpsService.activity` | exposes |
| `activity:kill_switch_latched_durability_uncertain` | `src/agentic_fx/status.py::_activity` | consumes |
| `activity:kill_switch_reconcile_confirmed` | `afx-ops/afx_ops/web/render.py::render_activity` | consumes |
| `activity:kill_switch_reconcile_confirmed` | `src/agentic_fx/ops/client.py::Runner._op_activity` | consumes |
| `activity:kill_switch_reconcile_confirmed` | `src/agentic_fx/ops/service.py::OpsService.activity` | exposes |
| `activity:kill_switch_reconcile_confirmed` | `src/agentic_fx/status.py::_activity` | consumes |
| `activity:kill_switch_reconcile_failed` | `afx-ops/afx_ops/web/render.py::render_activity` | consumes |
| `activity:kill_switch_reconcile_failed` | `src/agentic_fx/ops/client.py::Runner._op_activity` | consumes |
| `activity:kill_switch_reconcile_failed` | `src/agentic_fx/ops/service.py::OpsService.activity` | exposes |
| `activity:kill_switch_reconcile_failed` | `src/agentic_fx/status.py::_activity` | consumes |
| `activity:kill_switch_reconcile_required` | `afx-ops/afx_ops/web/render.py::render_activity` | consumes |
| `activity:kill_switch_reconcile_required` | `src/agentic_fx/ops/client.py::Runner._op_activity` | consumes |
| `activity:kill_switch_reconcile_required` | `src/agentic_fx/ops/service.py::OpsService.activity` | exposes |
| `activity:kill_switch_reconcile_required` | `src/agentic_fx/status.py::_activity` | consumes |
| `activity:kill_switch_reset` | `afx-ops/afx_ops/display_time.py::kill_switch_reset` | duplicates |
| `activity:kill_switch_reset` | `src/agentic_fx/core/display_time.py::kill_switch_reset` | produces |
| `activity:kill_switch_reset` | `src/agentic_fx/ops/client.py::Runner._op_activity` | consumes |
| `activity:kill_switch_reset` | `src/agentic_fx/ops/service.py::OpsService.activity` | exposes |
| `activity:kill_switch_reset` | `src/agentic_fx/status.py::_activity` | consumes |
| `activity:kill_switch_state_uncertain` | `afx-ops/afx_ops/web/render.py::render_activity` | consumes |
| `activity:kill_switch_state_uncertain` | `src/agentic_fx/ops/client.py::Runner._op_activity` | consumes |
| `activity:kill_switch_state_uncertain` | `src/agentic_fx/ops/service.py::OpsService.activity` | exposes |
| `activity:kill_switch_state_uncertain` | `src/agentic_fx/status.py::_activity` | consumes |
| `config:risk.drawdown_kill_pct` | `src/agentic_fx/config.py::RiskSettings` | schema |
| `config:risk.drawdown_kill_pct` | `src/agentic_fx/config.py::Settings._warn_high_drawdown_kill_pct` | reads |
| `config:risk.drawdown_kill_pct` | `src/agentic_fx/service.py::build_splash` | exposes |
| `event:event_stream_health` | `src/agentic_fx/ops/api_server.py::_call` | invokes |
| `event:event_stream_health` | `src/agentic_fx/ops/events.py::EventStore.emit` | writes |
| `event:event_stream_health` | `src/agentic_fx/ops/events.py::EventStore.poll` | reads |
| `event:guard_state_changed` | `src/agentic_fx/ops/api_server.py::_call` | invokes |
| `event:guard_state_changed` | `src/agentic_fx/ops/events.py::EventStore.poll` | reads |
| `event:guard_state_changed` | `src/agentic_fx/ops/service.py::OpsService.events_after` | invokes |
| `event:guard_state_changed` | `src/agentic_fx/service.py::_GuardNoticeProbe.__call__` | invokes |
| `event:ops_request_terminal` | `src/agentic_fx/ops/api_server.py::_call` | invokes |
| `event:ops_request_terminal` | `src/agentic_fx/ops/events.py::EventStore.emit` | writes |
| `event:ops_request_terminal` | `src/agentic_fx/ops/events.py::EventStore.poll` | reads |
| `event:ops_request_terminal` | `src/agentic_fx/ops/service.py::OpsService._mutate` | invokes |
| `event:ops_request_terminal` | `src/agentic_fx/ops/service.py::OpsService._project` | produces |
| `event:ops_request_terminal` | `src/agentic_fx/ops/service.py::OpsService.recover_after_journal` | produces |
| `event:ops_request_terminal` | `src/agentic_fx/service.py::_recover_ops` | invokes |
| `event:ops_request_terminal` | `src/agentic_fx/service.py::build_app` | invokes |
| `marker:reset_marker` | `src/agentic_fx/commands.py::Commands._status` | reads |
| `marker:reset_marker` | `src/agentic_fx/ops/client.py::format_status` | exposes |
| `metric:kill_switch_latches` | `afx-ops/afx_ops/metric_words.py::metric_items` | duplicates |
| `metric:kill_switch_latches` | `afx-ops/afx_ops/web/render.py::_baseline_section` | reads |
| `metric:kill_switch_latches` | `src/agentic_fx/commands.py::Commands._baseline_comparison_lines` | invokes |
| `metric:kill_switch_latches` | `src/agentic_fx/commands.py::Commands._comparison_metrics_line` | reads |
| `metric:kill_switch_latches` | `src/agentic_fx/loops/improve_loop.py::ImproveLoop._FLOOR_REPORT_METRIC_KEYS` | schema |
| `metric:kill_switch_latches` | `src/agentic_fx/loops/improve_loop.py::ImproveLoop._render_comparison_report_detail` | reads |
| `metric:kill_switch_latches` | `src/agentic_fx/loops/improve_loop.py::ImproveLoop._render_floor_report_detail` | reads |
| `metric:kill_switch_latches` | `src/agentic_fx/metric_words.py::metric_items` | reads |
| `metric:kill_switch_latches` | `src/agentic_fx/ops/change.py::_metric_row` | reads |
| `ops:guard_kind` | `afx-ops/afx_ops/web/render.py::<module>.ACTIVITY_FIELDS` | schema |
| `ops:guard_lane_not_held` | `src/agentic_fx/ops/service.py::GuardLaneNotHeld` | schema |
| `ops:guard_lane_not_held` | `src/agentic_fx/ops/service.py::OpsService._guard_notices_in_lane` | exposes |
| `ops:guard_lane_reentry` | `src/agentic_fx/ops/service.py::GuardLaneReentry` | schema |
| `ops:guard_lane_reentry` | `src/agentic_fx/ops/service.py::OpsService._reject_lane_reentry` | exposes |
| `state:app_state.kill_switch_generation` | `src/agentic_fx/ops/client.py::Runner._op_killswitch_show` | reads |
| `state:app_state.kill_switch_generation` | `src/agentic_fx/ops/service.py::OpsService.status` | exposes |
| `state:app_state.kill_switch_latched` | `afx-ops/afx_ops/web/render.py::<module>.STATUS_FIELDS` | schema |
| `state:app_state.kill_switch_latched` | `afx-ops/afx_ops/web/render.py::<module>._EMPHASIS` | schema |
| `state:app_state.kill_switch_latched` | `afx-ops/afx_ops/web/render.py::render_status` | exposes |
| `state:app_state.kill_switch_latched` | `src/agentic_fx/commands.py::Commands._projected_state` | reads |
| `state:app_state.kill_switch_latched` | `src/agentic_fx/commands.py::Commands._status` | exposes |
| `state:app_state.kill_switch_latched` | `src/agentic_fx/ops/client.py::Runner._op_killswitch_show` | reads |
| `state:app_state.kill_switch_latched` | `src/agentic_fx/ops/client.py::format_status` | exposes |
| `state:app_state.kill_switch_latched` | `src/agentic_fx/service.py::build_splash` | exposes |
| `state:app_state.kill_switch_latched` | `src/agentic_fx/status.py::collect_status` | invokes |
| `state:app_state.kill_switch_latched` | `src/agentic_fx/status.py::render_status` | exposes |
| `state:app_state.kill_switch_latched` | `src/agentic_fx/store/state.py::StateStore.file_value` | reads |
| `state:app_state.kill_switch_latched_at` | `src/agentic_fx/commands.py::Commands._dispatch` | reads |
| `state:app_state.kill_switch_latched_at` | `src/agentic_fx/ops/service.py::OpsService.status` | exposes |

## 履歴再現 (C4 前の 558cb9fd、§4.3)

558cb9fd の source を比較用に読み、頁 + 索引 (同じ extract を旧 source の二 root に適用、355 file、diagnostics なし) から a/b/c を表に出せるかを確かめた。正式 oracle の基準は 0dbcb27f のまま。本番の時刻・event 行・金額は使っていない。

| 事象 | 558cb9fd の経路 (入力 → mutation → event / consumer) | 頁・索引からの到達 | C4 後 (0dbcb27f) の解消と残余 | 判定 |
|---|---|---|---|---|
| (a) 自動ラッチの event 欠落 | `risk_gate.py::evaluate` (latch と DD を独立判定、理由文字列) → `executor.py::Executor._evaluate_and_execute_open` (理由の部分一致 → `StateStore.update(kill_switch_latched=True)`、latch activity) → guard event を出さない。guard 発行は `ops/service.py::OpsService._guard_event` にあり自動ラッチ経路から接続されない。bot / notify の consumer は存在するが配送する event が無い | F01・F02・F04、E01〜E06、D01、F28〜F31。索引は mutation と別の event 発行箇所を示し、頁が欠落・回収経路・限界を結ぶ | C4 の sweep (`plan_kill_switch_notice`・`OpsService._sweep_kill_switch`、起動と tick から) が不足 event を回収。自動ラッチからの直送ではない。sweep 前に clear になった場合の履歴、retention 外の復元、activity の反復抑制は保証しない | 出せた |
| (b) shell の lane 迂回 | reset: `commands.py::Commands.dispatch` → `StateStore.reset_kill_switch` (activity あり、guard event・ops lane なし)。reconcile: `Commands._killswitch_reconcile` → `StateStore.confirm_latched` (同)。data resume: `Commands._data_resume` → `OutageStateMachine.request_resume` (受付時に guard event なし。ops 側 `OpsService.resume_data` は lane 内で guard を出す) | F05・F06・F13〜F16、D02・D04、E07〜E09・E13〜E15・E17・E26。data resume は索引に event の record が無く、手動辺 E07 が不在経路を補う | C4 は reset / reconcile の前後に通知 wrapper を追加 (mutation は lane 外のまま、世代間の全順序は保証しない)。data resume の迂回は**未解消** (D04、是正 ticket 未起票) | 出せた |
| (c) docstring と実動作の範囲差 | `ops/events.py` の module docstring は「生成は best-effort で、INSERT に失敗しても元の操作を失敗させない」と広く読める。実際は `EventStore.emit` が検証・INSERT・commit の例外を捕捉せず伝播、`emit_best_effort` も事前検証は try 外で伝播し、INSERT 障害だけを隔離する | F17・F18、D05。docstring 自体に対象 resource の literal は無く、主張は頁 (D05) と anchor から辿る。health event の record から実装に到達できる | C4 後も `emit` は例外を捕捉せず、`emit_best_effort` の検証も try 外。module の広い説明は残る。文書修正の所有 ticket は未確定 | 出せた |

- a/b/c とも頁 + 索引から出せた。ただし event の不在や例外の伝播は literal 索引だけでは証明できず、頁の手動辺と D 欄が要る (§2.3 の限界のとおり)。
- 設計書 §3.4 で (c) に当てていた D18 は、本頁では D05 が担う。

## 初回 T1 の結論 (1 周目のレビューで限定した版)

§4.3 の初回要件のうち、機械セル G_lit critical FN=0 / U=0、頁セル G_lit ∪ G_reach critical FN=0 / U=0、誤った direct 断定 0、非 critical FN の全件列挙と処置、FP の件数記録、履歴再現 a/b/c は満たした。ただし次の 2 点が未完のため、**初回 T1 はまだ完了としない**:
- 凍結表の `residual_unread` (両表とも辿り切れていない範囲 6 件) が U に符号化されていない。上の「U 0」は符号化された unresolved が 0 であることしか示さない。6 件を source で裁き (行の追加 / 範囲外 / unresolved)、記録してから U を数え直す。
- FP の仕分け時間 (§4.3 の記録要件) を計測していない。

また頁セルは初回測定の後の手動補完で達したもの (補完前 97 / 161 の FN)、機械セルは抽出器を設計の形に合わせる是正 (run2 → run5) と正解表の訂正 1 件を経たもの。速度の反復は T2。

## 正解表の追加 (1 周目のレビューの後、residual_unread の裁定)

- 凍結表の `residual_unread` 6 件を、A・B と別の裁定者 (codex sol) が source で裁き、6 件とも `add` (unresolved 0)。指揮者が 3 件を抜取り照合 (BotClient.events・build_splash の marker 参照・MissionSupervisor._dispatch) して採用。追加 73 行 (critical 36)、凍結表は 294 行 (critical 197)。各件の裁定理由は凍結表の `residual_unread_resolved` に残した。
- 出力を見た後の正解表の変更なので記録する (§4.1 (4))。役割: 裁定者 codex sol、確定は指揮者。frozen sha256: `b929322136ed…` → `cde1d229a985…`。追加前の点は上の final 測定の表。

## FP の仕分け (1 回の実測) と正解表の追加

- 仕分け: FP 70 件 (機械 54・頁 16) を A・B と別の仕分け者 (codex sol) が source で仕分けた。所要 **180 秒** (起動から完了まで、1 回)。内訳: literal_collision 31 (state field と activity kind などの同名 literal)、display_or_reader 16、**oracle_miss 15**、**page_overclaim 8**。
- oracle_miss のうち重複を除く 13 行 (critical 11) を凍結表に追加 (state の検証定数 `_BOOL_FIELDS` / `_REQUIRED_KEYS`、`_sweep_kill_switch` の発動時刻の読取、replay の `_kill_switch_event` ほか)。指揮者が 2 件を抜取り照合。凍結表は 307 行 (critical 208)。出力を見た後の変更 (§4.1 (4))。frozen sha256: `cde1d229a985…` → `d745092e2753…`。
- page_overclaim 8 件は頁の誤り: `Commands._data_resume` と `OutageStateMachine.request_resume` が guard event を出すかのような被覆宣言 (実際は出さない — D04 と矛盾)、`_build_ops_service` への帰属の過大 (正しくは入れ子の `status_provider`)、そして `guard_notice_deferred` / `guard_notice_backfill_skipped` を ops event として宣言したこと。後者 2 つは warning log の固定 code であり、resource の一覧で `event:` に入れたのは指揮者の誤り。namespace を `log:` に直す。

## 最終測定 (fill4、凍結表 d745092e…)

| セル | critical 母数 | TP | FN | U | 全体の FP |
|---|---|---|---|---|---|
| 機械 (G_lit) | 81 | 81 | 0 | 0 | 54 (仕分け済み、上記) |
| 頁 (G_lit ∪ G_reach) | 208 | 208 | 0 | 0 | 3 |

- 頁の是正: page_overclaim 8 件を source で確かめて全件是正 (被覆を外す / 入れ子の symbol に直す / 本文の誤読を直す)、`guard_notice_*` を `log:` に直した。補完者の報告で抜取りではなく全件確認。
- 残る頁の FP 3 件は正解表の漏れ候補 (`log:guard_notice_backfill_skipped` × `OpsService._sweep_kill_switch`、`log:guard_notice_deferred` × `OpsService.emit_pending_notices`、`state:app_state.kill_switch_latched_at` × `StateStore.reset_kill_switch`)。FP は合否条件ではないので、正解表の再変更は重ねず T2 の正解表見直しで扱う。
- 非 critical FN 83 件は上の「非 critical FN と処置」の表に全件 (T2 で頁を補完)。

## 初回 T1 の結論 (最終)

§4.3 の初回要件 — 機械セル G_lit critical FN=0 / U=0、頁セル G_lit ∪ G_reach critical FN=0 / U=0、誤った direct 断定 0 (初版は reader/writer を断定しない)、未裁定 U 0 (residual_unread 6 件を裁定済み、unresolved 0)、非 critical FN の全件列挙と処置、FP の件数と仕分け時間 (70 件・180 秒、1 回)、履歴再現 a/b/c — を満たした。**初回 T1 は合格**とする。
ただしこの合格は次の経過を経たもので、索引と頁が初めから足りていたことを示さない: 抽出器を設計の形に合わせる是正 (run2 の機械 critical FN 41 → 0)、頁の手動補完 (初回の頁 critical FN 97 → 0)、正解表の追加・訂正 3 回 (§4.1 (4) の記録どおり)。速度の反復と FP の仕分け時間の反復は T2。

## /code-review high の是正後の再測定 (final2)

- 採用 6 件 (代入先の添字・属性の照合、symlink 検査を repo 内に限定、致命的な診断で index.json を残さない、literal の正規表現の前計算、TP・FN・U の排他集計、構文解析の再帰・メモリ超過の診断) を入れて再測定。不採用 1 件 (頁の FP を合否に含める) は §4.3 が FP をゼロ条件にしないため。
- 機械セル critical 81 / TP 81 / FN 0 / U 0 (全体 FP 54 → 49)、頁セル critical 208 / TP 208 / FN 0 / U 0 (全体 FP 3)。合否は変わらない。extract 0.97 秒 (一回)。

## T2 (verify / check / affected / 固有例外台帳 / 非 critical の頁補完)

- 道具: `verify` (正規化 AST hash と方式 version、期待 hash が一致したときだけ照合記録)、`check` (stale・根拠消失・必須違反は 1、解析失敗・宣言不正・方式不整合・履歴取得不能は 2)、`affected` (revision 間または作業 tree との差を種類別に)、固有例外台帳 `deviations.json` と Commands の直呼び検査 (D02 の shell reset / confirm、D04 の data resume)。
- 頁の補完: 非 critical 83 行のうち 82 行を頁に足した。残る 1 行 (`ImproveLoop._FLOOR_REPORT_METRIC_KEYS`) は、class 直下の定数を extract は `Class.NAME` で記録するのに verify / check の anchor 解決が解けない食い違いのため、頁に被覆を書くと check が落ちる。解決側を extract に揃える是正を入れてから被覆する。
- 正解表の追加 (§4.1 (4)): 頁の FP 3 件を source で確かめた結果、頁の宣言が正しかった (正解表の漏れ) ので非 critical として凍結表に足した — `OpsService._sweep_kill_switch` × `log:guard_notice_backfill_skipped`、`OpsService.emit_pending_notices` × `log:guard_notice_deferred`、`StateStore.reset_kill_switch` × `state:app_state.kill_switch_latched_at` (reads)。凍結表 310 行。frozen sha256: `d745092e…` → `65ee08c7…`。
- 0dbcb27f..HEAD (HWM の merge を含む) の affected: dependency_changed 1 (`store/db.py::<module>._SCHEMA`)、symbol_changed 4 (`ops/api_server.py::_call`・`ops/client.py::Runner._op_killswitch_reset`・`service.py::_build_ops_service`・`service.py::build_app`)、literal_candidates_changed 13、unmapped_new_file 3 (`ops/rebase_accounting.py`・`ops/rebase_service.py`・`ops/rebase_view.py`)。HWM の実運用確認 (§6.4 の二件目、T3) の材料。
- 最終 (T2): anchor 解決を揃えた後に残り 1 行を被覆。頁セル 全体 310 / TP 310 / FN 0 / FP 0 (critical 208 / 208)、機械セル 全体 114 / TP 114 / FN 0 / FP 59 (critical 81 / 81)。`check --page kill-switch` は終了コード 0。
- 人手の裁定 (`ambiguous_sub_anchor`、E13): `src/agentic_fx/commands.py::Commands._dispatch#_emit_guard_notices` は、shell の `killswitch reset <世代>` の分岐にある `_emit_guard_notices` の 3 文 (store の reset 前の `None`、`ResetNotApplied` のときの `self._projected_state`、成功時の `lambda: after`) に該当し、callee と literal では 1 文に絞れない。3 文とも同じ分岐の前処理・保存失敗時・成功時の wrapper で、E13 の記述 (shell reset の前後 wrapper) と一致することを 94ad84e3 の source で確かめた。照合記録はこの裁定を前提に人が読む項目として残し、exit 0 を照合済みとは扱わない。
- 人手の裁定 (`base_declarations_partial`、0dbcb27f): base の頁は a58dc4bb で置いた雛形 (front matter の resources・anchors が空、covers marker 無し) で、破損ではないことを履歴で確かめた。base 側の宣言は空として扱うので、0dbcb27f..HEAD で依存の削除は検出できない (この区間では頁が初めて書かれたので、削除される宣言も無い)。
- 新 resource の保守的検出の実測 (0dbcb27f..HEAD、HWM の差分): HWM が足した resource は `db:kill_switch_hwm_rebases` (`store/db.py` の `_SCHEMA`・`TABLE_NAMES`)、ops の endpoint `killswitch_rebase`・`killswitch_rebase_preview` (`ops/service.py` の `ENDPOINTS`・`ops/api_server.py::_call`・`ops/client.py`・`cli_surface.py`)、ops の error code (`ops/contracts.py` の `preview_changed`・`market_closed`、新しい 3 file の拒否 code)、`log:rebase_release_failed` (`OpsService`)。既知 literal の無い file の未宣言 symbol を候補に出さない規則では `cli_surface.py`・`ops/contracts.py`・`ops/client.py` (宣言済みの 1 symbol を除く)・`afx-ops/afx_ops/web/write.py` が一件も出なかった。規則を外すと `unmapped_changed_unmapped_symbol` は 15 → 38 件で、これらがすべて出る。
- T2 の閉鎖: 道具に入った追加 — 副 anchor (`path::symbol#callee[@literal]`、最小 statement の hash)、`ambiguous_sub_anchor` / `coarse_symbol` / `coarse_parent_changed` / `base_declarations_partial` の情報項目 (閉鎖時に必ず読む、exit 0 は照合済みではない)、既存の照合記録の reason・evidence の再検査、根拠 test 名の AST 照合、既知 literal の無い file も候補走査、base と head の resource の和集合、照合記録の原子的・排他の書込み、直呼び検査の scope 単位の may-alias。tests/scripts/test_impact_map*.py 95 本。実頁: `check` 終了コード 0 (人が読む項目は E13 の 1 件)、0dbcb27f..HEAD の `affected` 58 項目。所要: `check` 約 0.9 秒、`affected` 約 4.7 秒。

## T3 一件目: C4 の遡及運用確認 (§6.4)

**これは遡及運用確認であり、「関係を変える code と頁が同 commit で更新された」ことの実地試験ではない。** C4 の merge 時点 (0dbcb27f) の頁は雛形で、今の頁は C4 の後に書かれたものである。

- 手順: 0dbcb27f の使い捨て worktree (repo 外) に今の `docs/impact-map/` と `scripts/impact_map.py` を写して一時 commit し、`affected --page kill-switch --base d7025768^1 --head <一時 commit>` を回した (差分は C4 の merge の親 → merge 結果、first-parent の 5 merge)。一時 commit が足す docs と scripts は裁定から除いた。終了後に worktree を remove した。
- 出力: 185 項目 (終了コード 0)。内訳は `unmapped_changed_unmapped_symbol` 109、`symbol_changed` 33、`literal_candidates_changed` 32、`unmapped_new_file` 8、`ambiguous_sub_anchor` 1、`coarse_parent_changed` 1、`base_declarations_partial` 1。
- 裁定 (全件、source で。差分 40 file のうち src と afx-ops を読んだ。この差分は kill switch の通知 (guard event の sweep) と bot の health・終了分類の二つの機能を含む):

| 区分 | 件数 | 裁定 |
|---|---|---|
| 頁の記述と一致 (更新不要) | 79 | `symbol_changed` 28 と `literal_candidates_changed` 32 (入口 `dispatch` → `_dispatch` の分割に伴う literal の移動を含む)、`coarse_parent_changed` 1 (`build_app` の sweep 入口の差し替え、F20・F21・E16 と一致)、`ambiguous_sub_anchor` 1 (E13、T2 の裁定のとおり再確認)、通知機構の `unmapped_changed_unmapped_symbol` 17 (F09〜F12・F17〜F18・F21・F70・K-LANE・K-NOTICE の本文で説明済みの補助 symbol) |
| kill switch と無関係 (頁に載せない) | 101 | bot の REST 分類・進捗・watchdog・health snapshot・logsafe・終了コード、`ops_setup` の systemd drop-in と units、snapshot の I/O 失敗の型 (`unmapped_changed_unmapped_symbol` 90、`unmapped_new_file` 7)、`status`・`ctl` の status 整形・web の `render_status` の bot health 表示 (`symbol_changed` 4)。これらの file の変更行に `kill` / `guard` / `latch` / `reset_marker` を含むものは無いことを grep で確認した。D09 の関連 ticket の範囲 |
| 頁の更新が要った | 4 | 下記 |
| 道具の情報項目 | 1 | `base_declarations_partial` (下記) |

(計 185。F29 の `deliver_events` は `symbol_changed` 1、`_ops_lane` と `emit_pending_improve_notices` は `unmapped_changed_unmapped_symbol` 2、`kill_switch_words.py` は `unmapped_new_file` 1。)

- `base_declarations_partial` 1 (`covers_absent`): base の頁が雛形のため想定どおり。この区間の削除は検出できないが、頁が初めて書かれた区間なので削除される宣言も無い。
- 頁の更新が要った 4 点 (頁が拾えなかった変更。いずれも source で確かめて直した):
  1. F29 (`BotAdapter.deliver_events`): 「送信失敗時は cursor を進めず再試行」だけでは不足だった。C4 で、恒久の拒否は BotFailure で止まり、event 固有の不正な本文は固定の代替文を送って確定する経路が加わった。F29 を三経路に書き直した。
  2. `OpsService._ops_lane` (新 F129): 同一 thread の lane 再入の拒否と thread-local の所有 flag は guard 専用でなく ops lane を使う全 mutation の共通入口にある。F121 (`_reject_lane_reentry`) だけでは入口が読めなかった。被覆を `ops:guard_lane_reentry` に宣言した。
  3. `core/kill_switch_words.py` の `KILL_SWITCH_RESET_WARNING` と ctl の reset 成功時の出力 (新 F130、`unmapped_new_file`): K-RESET-UI は shell 側 (F13) しか指していなかった。
  4. `OpsService.emit_pending_improve_notices` が `GuardLaneReentry` を握らず伝える点 (`unmapped_changed_unmapped_symbol`): F12 に一文を足した。
- 変更前の F06・F07・F09・D との照合: F06 (`confirm_latched`) の store は差分に無く不変。F07 (`reset_kill_switch`) は操作前・結果・現 state の guard step に変わり、頁の記述と一致。F09 (`plan_kill_switch_notice`) は新規で、released 済みを閉じ履歴を捏造しない規則と一致。D01・D04・D05 は C4 後の source と一致 (D04 の `_data_resume` と `resume_data` の `_guard_event` は差分に無い)。
- 見つけた欠陥・見落とし: 上の 4 点 (すべて F の本文・入口の抜けで、被覆や辺の誤りではない)。道具の欠陥は無かった。ただし `unmapped_changed_unmapped_symbol` 109 件の大半は別機能 (bot の health) の変更で、頁の領域外か否かの切り分けは人手の grep が要った (resource の literal を含む変更行の有無を領域外の根拠にできるが、道具は出さない)。
- 所要時間 (おおよそ): 道具の実行は数秒。裁定 (source の読取り) と頁の更新を含め、開始から頁の commit まで約 3 分 (`date` による wall clock の実測。source の読取りは diff の通読が中心)。
- 更新後の `check --page kill-switch` は終了コード 0 (人が読む項目は E13 の 1 件のみ)。
- 更新後の頁セル: 凍結表 310 行は TP 310 / FN 0 / U 0 (critical 208 / 208) のまま、頁セルの FP は 0 → 1。増えた 1 件は新しい F129 (`OpsService._ops_lane` × `ops:guard_lane_reentry`) で、凍結後の追加である (凍結表は変えない)。

## T3 二件目: HWM の運用確認 (§6.4)

**この確認は、HWM が頁の無い branch で実装されたため、「関係を変える code と頁が同 commit で更新された」ことの実地試験にはならない。** 頁は HWM の merge の後に合わせた。同 commit の更新の実地試験は、次の通常の変更 (頁のある領域に触れるチケット) で行う。

- 手順: `affected --page kill-switch --base 0dbcb27f --head HEAD` (終了コード 0)。58 項目: `unmapped_changed_unmapped_symbol` 33、`literal_candidates_changed` 13、`symbol_changed` 5、`unmapped_new_file` 3、`ambiguous_sub_anchor` 1、`coarse_parent_changed` 1、`dependency_changed` 1、`base_declarations_partial` 1。
- HWM 以外の混入: 無い。`src/` と `afx-ops/` の差分は HWM の 12 file だけ (`ops/rebase_service.py`・`ops/rebase_accounting.py`・`ops/rebase_view.py` の新規 3 file と、`ops/service.py`・`ops/client.py`・`ops/api_server.py`・`ops/contracts.py`・`cli_surface.py`・`store/state.py`・`store/db.py`・`service.py`・`afx-ops/afx_ops/web/write.py` の変更)。影響地図の道具と記録は `scripts/`・`docs/`・`tests/` にあり候補走査から除かれる。
- 裁定 (58 項目を全件 source で。HWM の設計の §1–2・§4–5 と突き合わせた):

| 区分 | 件数 | 裁定 |
|---|---|---|
| 頁の更新が要り、更新した | 52 | 付け直しの新 file・symbol・literal・辺。入口 (ctl の parse・`_op_killswitch_rebase`・`_rebase_done`・固定文・端末、cli_surface、api_server の route と件数固定、endpoint 表、error code、web の既知 code)、検査 (evaluate_rebase・RebaseWiring・error_code_name)、原子的な書込み (apply_rebase・台帳の読取・図形の導出・broker 評価)、排他と state の読取り (rebase_under_core・rebase_commit・shared_guard・GuardedState)、台帳表と許可集合。F131–F164 と E66–E78、F36・F40・F43 の本文の更新で対応づけた |
| literal の衝突で頁に載せない | 3 | `evaluate_rebase`・`_rebase_commit`・`GuardedState.projected` の `activity:kill_switch_latched` (state の属性名と同じ literal。activity の読み書きは無い) |
| 道具の情報項目で追従済み | 3 | `coarse_parent_changed` の `build_app` (core_lock と broker を ops service に渡す、F20・F36)、`ambiguous_sub_anchor` の E13 (T2 の裁定のとおり再確認)、`base_declarations_partial` (0dbcb27f の頁が雛形のため想定どおり) |

(計 58。`dependency_changed` の `_SCHEMA` は F43 の更新に、`symbol_changed` の `_mutate`・`_build_ops_service`・`_op_killswitch_reset`・`_call` は F146・F36・F130・F149 に含めた。)
- 頁の更新: K-REBASE を「実装済み」に (approver の明示操作と service 先頭の直接検査、PaperBroker・注文状態・新鮮 snapshot・DD・世代・autopilot・閉場の検査、HWM のみ変更で latch・世代・発動時刻を保持し state を書かない、値 CAS と ledger / snapshot の一つの transaction、通常 reset・shell・bot・web・notify に入口を足さない — それぞれ source で確かめた。入口が無いことは `Commands`・`afx-ops` の全 file の grep と cli_surface の record (shell の欄が無い) と api_server の `_match` (local_guard は UDS のみ) で確認)。D08 を解消済みにし、D10 (累積の硬い上限が無い、paper 限定) と D11 (COMMIT 結果不明は自動で再送しない) を残余として追加。K-RESET-UI を ctl の案内の追加に合わせた。頁の冒頭と影響先の記述 (観測 revision `160f1e47`、「未実装」の文) を直した。
- 更新量: F 34 行 (F131–F164) と E 13 行 (E66–E78) と D 2 行を新規、K 2 行・D 1 行・F 5 行 (F20・F36・F40・F43・F130) と前文 3 箇所を更新。front matter に resource 6 (`db:kill_switch_hwm_rebases`・`ops:killswitch_rebase`・`ops:killswitch_rebase_preview`・`ops:preview_changed`・`ops:market_closed`・`log:rebase_release_failed`)・anchor 8・test 2 を追加。被覆宣言は 41 行追加 (すべて凍結表に無い行で、下の FP の 41 件と一致する)。頁の差分は 389 行追加・12 行削除 (被覆宣言の JSON が大半)。
- 見落とし・所見: (a) `affected` は base の頁が雛形だと宣言済みか否かを区別できず、頁を直した後も 58 項目が同じ形で出る (比較は base 側の宣言に依るため。想定どおりで欠陥ではない)。(b) `GuardedState.projected` は現在の `src/` に呼出し元が無い (F162 に書いた)。(c) ledger を直接読む symbol は `read_rebase_totals` と `apply_rebase` だけで、`evaluate_rebase` は間接 (F131 に書き、被覆は宣言せず辺 E76 で結んだ)。(d) 抽出器は `OpsService._mutate` の `RebaseOutcomeUnknown` への分岐を資源の変更として出さず、`symbol_changed` だけで出る。
- 検査: `check --page kill-switch` は終了コード 0 (人が読む項目は E13 の 1 件のみ)。`tests/scripts/test_impact_map*.py` 95 本 passed。
- evaluate (凍結表は変えない): 頁セル 凍結表 310 行は TP 310 / FN 0 / U 0 (critical 208 / 208)、機械セル critical 81 / 81、FN 0、U 0。**FP は頁セル 1 → 42、機械セル 59 → 86。増えた分はすべて HWM の追加による凍結後の追加**で、凍結表の symbol への FP の増加は無い。頁セルの内訳 (41 件): 新 resource (台帳表・endpoint 二つ・error code 二つ・log code) の被覆 25、既存 resource の HWM symbol への被覆 16 (`evaluate_rebase`・`_rebase_commit`・`rebase_preview`・`shared_guard`・`GuardedState.projected`・`apply_rebase` の state・config・snapshot の読み書き)。機械セルの新しい FP 27 は同じ新 resource の literal の索引の命中と、`activity:kill_switch_latched` の衝突。凍結表の更新は次の正解表見直しで扱う。
- 所要時間 (おおよそ): `affected` 約 5 秒、`check` 約 1 秒、`extract` + `evaluate` 約 2 秒、頁の更新 (約 50 行の新規と書き直し) と照合を含めて開始から commit まで約 4〜5 分 (`date` による wall clock の実測)。
