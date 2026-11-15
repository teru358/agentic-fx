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

### 非 critical FN (47 件) と処置

処置: いずれも表示・読取側の consumer や補助的な literal site で、初回の合格要件 (critical) の外。頁への手動補完は T2 で行う (それまで頁はこの範囲を被覆しないと明示する)。

| resource | symbol |
|---|---|
| `activity:kill_switch_latched_durability_uncertain` | `src/agentic_fx/core/executor.py::Executor._notify` |
| `activity:kill_switch_latched_durability_uncertain` | `src/agentic_fx/core/notifier.py::Notifier.send` |
| `activity:kill_switch_reset` | `afx-ops/afx_ops/display_time.py::kill_switch_reset` |
| `activity:kill_switch_reset` | `src/agentic_fx/core/display_time.py::kill_switch_reset` |
| `activity:kill_switch_reset` | `src/agentic_fx/ops/service.py::OpsService.activity` |
| `config:risk.drawdown_kill_pct` | `src/agentic_fx/config.py::RiskSettings` |
| `config:risk.drawdown_kill_pct` | `src/agentic_fx/config.py::Settings._warn_high_drawdown_kill_pct` |
| `event:event_stream_health` | `src/agentic_fx/ops/events.py::EventStore.emit` |
| `event:event_stream_health` | `src/agentic_fx/ops/events.py::EventStore.poll` |
| `event:guard_state_changed` | `src/agentic_fx/ops/events.py::EventStore.poll` |
| `event:guard_state_changed` | `src/agentic_fx/ops/service.py::OpsService.events_after` |
| `event:guard_state_changed` | `src/agentic_fx/service.py::_GuardNoticeProbe.__call__` |
| `event:ops_request_terminal` | `src/agentic_fx/ops/events.py::EventStore.emit` |
| `event:ops_request_terminal` | `src/agentic_fx/ops/events.py::EventStore.poll` |
| `event:ops_request_terminal` | `src/agentic_fx/ops/service.py::OpsService._mutate` |
| `event:ops_request_terminal` | `src/agentic_fx/ops/service.py::OpsService._project` |
| `event:ops_request_terminal` | `src/agentic_fx/ops/service.py::OpsService.recover_after_journal` |
| `event:ops_request_terminal` | `src/agentic_fx/service.py::_recover_ops` |
| `event:ops_request_terminal` | `src/agentic_fx/service.py::build_app` |
| `marker:reset_marker` | `src/agentic_fx/commands.py::Commands._status` |
| `marker:reset_marker` | `src/agentic_fx/ops/client.py::format_status` |
| `metric:kill_switch_latches` | `afx-ops/afx_ops/metric_words.py::metric_items` |
| `metric:kill_switch_latches` | `afx-ops/afx_ops/web/render.py::_baseline_section` |
| `metric:kill_switch_latches` | `src/agentic_fx/commands.py::Commands._baseline_comparison_lines` |
| `metric:kill_switch_latches` | `src/agentic_fx/commands.py::Commands._comparison_metrics_line` |
| `metric:kill_switch_latches` | `src/agentic_fx/loops/improve_loop.py::ImproveLoop._FLOOR_REPORT_METRIC_KEYS` |
| `metric:kill_switch_latches` | `src/agentic_fx/loops/improve_loop.py::ImproveLoop._render_comparison_report_detail` |
| `metric:kill_switch_latches` | `src/agentic_fx/loops/improve_loop.py::ImproveLoop._render_floor_report_detail` |
| `metric:kill_switch_latches` | `src/agentic_fx/metric_words.py::metric_items` |
| `metric:kill_switch_latches` | `src/agentic_fx/ops/change.py::_metric_row` |
| `ops:guard_kind` | `afx-ops/afx_ops/web/render.py::<module>.ACTIVITY_FIELDS` |
| `ops:guard_lane_not_held` | `src/agentic_fx/ops/service.py::GuardLaneNotHeld` |
| `ops:guard_lane_not_held` | `src/agentic_fx/ops/service.py::OpsService._guard_notices_in_lane` |
| `ops:guard_lane_reentry` | `src/agentic_fx/ops/service.py::GuardLaneReentry` |
| `ops:guard_lane_reentry` | `src/agentic_fx/ops/service.py::OpsService._reject_lane_reentry` |
| `ops:guard_notice_generation` | `src/agentic_fx/service.py::_build_ops_service.status_provider` |
| `ops:guard_notice_pending` | `src/agentic_fx/service.py::_build_ops_service.status_provider` |
| `state:app_state.kill_switch_generation` | `src/agentic_fx/ops/client.py::Runner._op_killswitch_show` |
| `state:app_state.kill_switch_generation` | `src/agentic_fx/ops/service.py::OpsService.status` |
| `state:app_state.kill_switch_latched` | `src/agentic_fx/commands.py::Commands._projected_state` |
| `state:app_state.kill_switch_latched` | `src/agentic_fx/commands.py::Commands._status` |
| `state:app_state.kill_switch_latched` | `src/agentic_fx/ops/client.py::Runner._op_killswitch_show` |
| `state:app_state.kill_switch_latched` | `src/agentic_fx/ops/client.py::format_status` |
| `state:app_state.kill_switch_latched` | `src/agentic_fx/status.py::collect_status` |
| `state:app_state.kill_switch_latched` | `src/agentic_fx/status.py::render_status` |
| `state:app_state.kill_switch_latched` | `src/agentic_fx/store/state.py::StateStore.file_value` |
| `state:app_state.kill_switch_latched_at` | `src/agentic_fx/ops/service.py::OpsService.status` |

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
