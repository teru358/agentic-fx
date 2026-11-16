# kill switch ラッチ中の未約定指値 — 設計 C0.2

対象: [ticket](../../tickets/open/killswitch-latch-pending-limit-fills.md)。source 確認: branch `kslimit`、HEAD `f11f7358`（C0.1）。設計レビュー 1・2 周目の採用全件を反映した提案であり、実装済み動作とは区別する。

## 1. 起きる動作と決定

**案 A（level-triggered の全 pending 取消）を採用する。** 開場 tick でラッチ（reset marker の投影を含む）を確認したら、未約定指値を全取消し、その tick の新規約定を止める。PENDING_FILL に残った注文はラッチ中の次 tick で再試行する。既存建玉の SL/TP と day 決済は継続する。取消済みの計画は解除後も復活せず、新しい intent は従来 gate で再評価する。scheduler は latch・generation・latched_at を書かない。

ラッチ未成立でも、現在 DD が `drawdown_kill_pct` 以上なら約定だけ止める。DD を理由とする取消・ラッチ書込みはしない。state 読取不能・共有 lock 競合も約定停止だけとし、読取障害を理由に計画を廃棄しない。

| 採用案 | 理由 |
|---|---|
| A: ラッチを確認した開場 tick ごとに全取消 + 約定禁止 | 古い計画を廃棄し、取消確定分の予約枠を解放。再起動で発動の edge を見逃しても回復する |

| 不採用 | 理由 |
|---|---|
| B: ラッチ中も約定保留だけで注文維持 | 解除直後に古い計画が約定し、予約枠も占有し続ける |
| C: 発動時に一度だけ取消 | 再起動・PENDING_FILL に残る取消失敗を取りこぼす |
| 各約定の最終区間を shared_guard の flock で囲む | clear→latched writer は core_lock 下または replay の単一 thread。tick の一度の読取で足りる（§4） |
| 非終端 close_reason に取消要求を永続化し、解除後も回復 | 主な失敗は既に約定候補外へ移る。残余の DB 障害は要求保存も失敗しうるため効果が乏しい |
| state 読取不能・共有 lock 競合で全取消 | 一時的な競合で計画を失う害が大きい。account_unknown の総リスク再検証不能とは異なる |
| 共有 lock を期限付きで待つ | day 決済／SL/TP より前の待機は保護処理を遅らせる。許容する根拠がなく、約定だけを次 tick に回せるため、一度の非 blocking 試行を採用 |
| scheduler だけ実ファイル存在を厳格に要求 | risk gate と非対称。state 消失／再作成の共通問題として別 ticket へ |
| 閉場 tick で guard 取消を再試行 | 閉場は既存の早期 return で約定しない。開場 tick で新たに読む |
| 全保存 run の再評価・新しい来歴欄 | 対象は latch ≥ 1 または DD 閾値以上の区間がある run。非該当を判定できない境界 commit をまたぐ比較も両側再実行（§5）。core_commit は既存 |

## 2. 裏取りした現状

以下のパスは特記なき限り `src/agentic_fx/` 配下。

| 根拠（file:line） | 確認した事実・意味 |
|---|---|
| `core/scheduler.py:244–334` | 閉場は早期 return。開場は MTM → unknown 解決 → expiry → account/予約再検証 → day 決済 → fills → exits。account 不明／equity 非正は全取消 + 約定停止。fills には ready も必要 |
| `core/scheduler.py:286`, `core/risk_gate.py:193–198`, `core/accounting.py:44` | tick は (equity,hwm) を持つ。gate はラッチと現在 DD を独立に判定するが、予約約定では gate を再実行しない。主設計 §5 の「HWM -2% で新規停止」に合わせ、同じ drawdown_pct と設定閾値で fills を止める |
| `core/executor.py:653`, `loops/trade_loop.py:307/379,579/658`, `backtest/runner.py:463` | DD による latch 更新は executor。通常／signal の open_from_snapshot は core_lock 下、replay の handle_intent は単一 thread。tick も `service.py:2500` の同じ core_lock 下 |
| `store/state.py:438,460–475`, `backtest/runner.py:273` | reset は latched→clear の marker を書く。confirm は marker 中のみで、既に latched として可視。replay の再ラッチ補償も単一 thread。afx-ops／mt5_bridge に app state の latch writer はない |
| `store/state.py:186–206,234–292,308–312` | load/shared_guard は marker を安全側投影。期限なしは blocking。deadline 指定時は先に LOCK_NB を試し、競合時だけ期限確認・再試行待ちへ進む。到達済み deadline なら一度の試行で競合を返せる。shared_guard は破損等で state=None。ファイル不存在は既定 AppState（変更しない） |
| `core/accounting.py:85–92`, `store/snapshots.py:24–27`, `core/scheduler.py:979–1005`, `ops/rebase_accounting.py:198–202` | current_account は最新 snapshot を ts DESC,id DESC で取り、10 分ちょうどまで有効。MTM 見送り時も既存 snapshot を使う。rebase は同時刻に hwm=equity の新 snapshot を追加し、次の MTM もその HWM を継承 |
| `backtest/runner.py:21–31,490–508`, `backtest/metrics.py:34–49,110–148`, `backtest/cli.py:637–647,762–772`, `backtest/holdout.py:155–177`, `store/backtest_runs.py:114–125` | max_drawdown は実現損益ベースの equity_curve の比率。含み損益込みの snapshots は replay の戻り値にはあるが、通常の保存経路は metrics_json を保存し snapshot 時系列を保存しない。旧 metric だけでは DD 停止の非該当を判定できない |
| `core/executor.py:1155–1179`, `core/transitions.py:108–115` | CANCELLING への DB 遷移失敗なら PENDING_FILL のまま。broker 例外／unknown は CANCEL_UNKNOWN、ok 後の CANCELLED 遷移失敗なら CANCELLING のまま。CANCELLING／CANCEL_UNKNOWN → PENDING_FILL はない |
| `core/scheduler.py:1047–1090`, `core/executor.py:29–36` | CANCEL_UNKNOWN は ok かつ not_found の照会だけで終端化。CANCELLING は再走査されず予約を占有する。中間状態は exposure に算入し、取消要求だけで予約を解放しない |
| `core/executor.py:1172–1176`, `core/scheduler.py:1047,1550` 付近, `core/paper_broker.py:60–61` | rejected は PROTECTION_PENDING に移るが、それを進める保護走査はない。現行 PaperBroker.cancel は常に ok。既存保護経路へ回復するとは扱わない |
| `core/transitions.py:134–139`, `ops/trade_events.py:226,269`, `afx-ops/afx_ops/gate/trade_render.py:24–35` | 取消終端の observer は trade event を出し reason_code=close_reason。_REASONS に kill_switch がなく、現在は「理由は表示できません」になる |
| `backtest/runner.py:437–463,508`, `store/backtest_runs.py:118–123`, `backtest/cli.py:644,769`, `backtest/holdout.py:171` | replay は tick → 自動解除 → 前 tick の提案執行。latch 数の metric と保存 run の core_commit は既存。解除 commit 失敗は rollback／再ラッチ補償 |

現行は PaperBroker のローカル約定であり、実 broker の指値取消を保証する設計ではない。「SL/TP 継続」は新しい gate／取消／観測の例外で exits を妨げない意味。既存の閉場・MTM 失敗の早期 return は維持する。

## 3. tick の処理と失敗

開場 tick の MTM → unknown 解決 → expiry の直後、account/予約再検証より前に、共有 lock を待たずに一度だけ試して state を読む（非 blocking）。既存 `shared_guard` で projected を取り出してすぐ解放し、broker／DB 遷移中は保持しない。

既存の `shared_guard`／`load` は `deadline` を `_shared` → `_flock` に渡す。`_flock` は先に `LOCK_SH | LOCK_NB` を試し、競合時だけ `_wait_or_raise` に進むため、StateStore と同じ monotonic clock で取得開始前に既に到達した deadline（通常配線なら `deadline=time.monotonic()`）を渡せば、成功時は読取、競合時は sleep／再試行なしで `StateLockTimeout` となる。注入 clock 使用時も同じ clock の到達済み値を渡す。この既存 API で一度の非 blocking 試行が可能なので、新 API は追加しない。期限は待機予算として設けず、即時試行を指定するためだけに使う。同期ファイル I/O（lock ファイルの open、state／marker の読取等）自体の時間上限、tick 全体や保護処理の完了時間上限は保証しない。

| 読取結果 | 追加する処理 |
|---|---|
| projected.latched=true（marker を含む） | 先に fills_allowed=False。`_cancel_all_pending(now, reason="kill_switch", event="kill_switch_pending_cancel", why="kill switch ラッチ中")` 相当の注文単位取消 |
| 共有 lock 競合／state=None／load・flock・parse 等の例外 | fills_allowed=False のみ。state を補修せず、障害を理由とする取消はしない |
| projected.latched=false | state 側は許可。ただし下記 snapshot の DD と既存条件を全て満たす必要がある |

「現在 DD」は、**既存の鮮度規約（`current_account`）で取った最新 snapshot の DD** を指す。含み損益込みの equity と連鎖 HWM を使うが、当該 tick の MTM 成功を保証する意味ではない。MTM が stale／例外で見送られた場合も最新 snapshot は 10 分ちょうどまで有効で、10 分超なら account_unknown となる。この間、別ペアの指値が古い DD で許可されうるという既存 gate と共通の制約を維持し、鮮度規約は変更しない。rebase 後は同時刻なら id が新しい hwm=equity の snapshot を使い、次の MTM はその HWM から連鎖する。

account が有効なら `drawdown_pct(equity,hwm) >= settings.risk.drawdown_kill_pct` でも fills_allowed=False とする。state が clear でも止め、scheduler はラッチを書かず DD だけでは取り消さない。account_unknown／equity_nonpositive による既存全取消、予約再検証の失敗による約定停止はそのまま。各条件は AND で結合し、後段で False を True に戻さない。実装時は `core/scheduler.py:287` 付近の `fills_allowed = account is not None and account[0] > 0` の初期代入を新 guard より前に移すか、既存値との AND に変更し、state／DD 停止の False を上書きしないこと。degraded でもラッチ取消は行い、ready は約定だけの条件とする。

取消失敗の扱いは既存状態遷移に従う。PENDING_FILL に残ればラッチ中だけ次の開場 tick で再試行。CANCEL_UNKNOWN は既存照会、CANCELLING の滞留は全取消理由に共通する別 ticket とする。rejected の PROTECTION_PENDING 滞留も既存の欠陥として別途扱う。根拠なしの終端化／再 submit／新しい保護走査は追加しない。

pending 列挙・注文単位取消・activity の例外を隔離し、先に決めた約定停止を維持して他注文と day 決済／SL/TP へ進む。観測は best effort、例外文は safe_error_text。取消成功／不存在確認で終端化した分だけ予約を解放する。

expiry は先なので同 tick の期限切れは既存 EXPIRED／limit_expired を優先（expires_at < now）。ラッチ確認による取消は account/予約再検証より先。既存理由 `market_close`・`reservation`・`rate_unavailable`・`account_unknown`・`equity_nonpositive` は維持し、終端済み注文は再取消ししない。閉場処理を増やさず、開場 tick で fresh な state を読み、約定停止中も exits を gate で止めない。

## 4. 同 tick の順序と保証の境界

**clear→latched の writer は core_lock 下（または replay の単一 thread）に限る。** §2 の全 writer と呼出し元を test で pin する。live の通常／signal commit-core と tick は同じ core_lock で直列化されるため、一度の読取と fills の間に DD latch writer は割り込まない。confirm は既存 marker の latched 投影を確定するだけであり、reset は解除方向。新 writer／呼出し元を追加するときもこの契約を守る。

writer が先なら tick は取消／fill=0。tick が先なら、その tick の建玉はラッチ成立前として保護を継続する。ただし現在 DD が閾値以上なら §3 で既に約定を止める。state の取得中以外に共有 flock を保持する必要はない。scheduler から state mutation／ops lane 呼出し／latch event INSERT を追加しない。

live は毎分、replay は基底足ごとの観測であり、停止・過負荷時の取消遅延上限は保証しない。発動から解除まで tick が一度もない短い latch は取消の観測外。CANCELLING 遷移前の DB 失敗後、次 tick 前（通常約 1 分以内）に reset され価格が到達すれば、残った PENDING_FILL は clear かつ DD 未満で約定しうる。この残余を許容する。世代追跡は追加しない。将来の外部 broker 指値には broker 取消／照会／約定競合の別契約が要る。

## 5. replay と比較

tick → 自動解除 → intent の順を維持し、解除日の最初の tick もラッチ中の古い pending を先に取り消す。ラッチ／DD 中の到達を止めるので約定数・PnL・PF・DD・後続 latch 数は変わりうる。全 metric の改善は主張しない。

replay は専用の一時 state と単一 thread を使う。§3 の一度の LOCK_NB 試行は競合せず即成功し、期限判定も競合時だけなので、到達済み deadline による即時試行指定は結果に壁時計依存を持ち込まない。この前提を固定し、時刻／artifact に壁時計を混ぜず同入力で決定的にする。

K-REPLAY の理由文字列、kill_switch_events の形・snapshot 参照、kill_switch_latches の数え方、自動解除理由・snapshot・commit 失敗補償を維持する。取消 activity は latch 遷移でも event 加算でもない。手動 rebase module／ledger／source を参照しない。

**再評価対象は `kill_switch_latches ≥ 1`、または DD 閾値以上の区間がある run。** latch 数 0 でも、新規 OPEN intent がなく含み損による DD 閾値到達中に指値が到達すれば、新しい DD 停止により結果が変わる。未約定指値の存在を絞込み条件にはせず、DD 閾値到達自体を対象とする。

旧保存物を source で確認した結果、`metrics_json.max_drawdown` は実現損益ベースの `equity_curve` のピーク比（0〜1）であり、今回の snapshot の含み損益込み DD（%）とは異なる。単に 100 倍して閾値と比較しても DD 停止の非該当は証明できない。replay の戻り値には `(ts,id,equity,hwm)` を含む snapshots があるが、通常の CLI／holdout 保存経路は metrics だけを保存し、snapshot 時系列を永続化していない。従って通常の旧保存 run は latch 数 0 だけでは除外できず、**境界 commit をまたぐ比較は baseline／candidate の両側を再実行する**。別途完全な snapshot 時系列と設定閾値が保存されている run に限り、§3 の最新選択・鮮度・rebase 順序に従う DD で閾値以上の区間の有無を判定できる。latch 数 0 かつ全対象区間で DD 閾値未満と確認できれば再評価不要とし、保存物の欠落・不完全さ等で判定できなければ同じく両側を再実行する。実在の旧 run／追加保存物の棚卸しは実装時に行う。

旧 run は履歴として残し、該当する baseline／candidate 比較は修正後の同一 revision・dataset/hash・config・戦略・期間・基底足・seed 等で両方を再実行する。来歴には既存 `backtest_runs.core_commit` を使い、新 schema／旧規約互換スイッチは作らない。run をまたぐ `loops/baseline_monitor.py:89–94` と承認候補の比較表示（`commands.py:812`、`ops/change.py:407–416` 等）には、実装が着地する境界 commit で約定規約に段差があることを注記する。境界 commit は実装時に確定する。fixture はラッチ／DD 停止の非該当で不変、該当で注文単位の差を説明する。

## 6. 観測

close_reason は **kill_switch** だけを追加する。activity は `kill_switch_pending_cancel`（SYSTEM、pending がある取消試行時の件数。完了とは書かない）と `kill_switch_fill_guard_unavailable`（SYSTEM、読取不能による約定停止への遷移時に一度。正常読取後の再障害は再記録）とする。既存 `cancel_all_pending_error`・`limit_cancelled`・`unknown_resolved` を使い、取消結果不明を成功件数に含めない。K-REPEAT は別 ticket のまま、scheduler は kill_switch_latched を再発行しない。

status の既存 kill_switch／pending 投影と web の activity 描画を使う。読取不能を clear 表示にしない。取消成功は transitions observer → trade_events → bot／notify の既存 trade event としても届くため、ラッチの K-NOTICE sweep 通知と注文ごとの取消通知が重なる。新しい取消通知経路や event schema は作らない。`afx-ops/afx_ops/gate/trade_render.py::_REASONS` に `kill_switch: "kill switch による取消"` を追加し、同 package の `test_ac20_close_reason_table` と取消理由表示の test を更新する。未知理由への fallback は維持する。

## 7. 影響地図へ渡す変更案

正本: [kill-switch 頁](../../impact-map/domains/kill-switch.md)、工程: [影響地図設計 §6.1–6.2](2026-11-14-code-impact-map-design.md)。scheduler 経路の追記が必要。以下は仮 ID の予定台帳で、頁・metadata・verification 自体は本作業で変更しない。

| 区分 | ID | 内容／根拠 |
|---|---|---|
| 守る K | K-LATCH / K-REPEAT | open gate の DD 理由 producer/consumer、通知失敗で state を戻さないこと、世代 no-op を維持。予約約定契約は新 K に分離し K-LATCH を二重に拡張しない |
| 守る K | K-RECONCILE / K-RESET / K-START | marker 投影・世代・発動時刻・reset/confirm の規則と既存 load の初期化規約を維持 |
| 守る K | K-REBASE / K-RESET-UI / K-NOTICE / K-LANE / K-VIEW / K-CONSUME | rebase 前提・表示・sweep 一元化・lane 再入規律・観測専用 status・配送 cursor を維持 |
| 守る／追記 K | K-REPLAY | 理由・artifact・metric 定義・解除補償を維持。約定規約の段差と §5 の再評価（latch ≥ 1 または DD 閾値以上の区間。旧保存物で非該当を判定できない境界比較は両側再実行）を追記 |
| 新 K | K-LATCH-WRITERS | clear→latched writer は core_lock 下または replay 単一 thread のみ。executor の全呼出し元、marker 中 confirm、reset marker、replay 補償を pin |
| 新 K | K-PENDING-STOP | ラッチ中・DD 閾値以上は約定禁止。ラッチ確認 tick は全 pending 取消、確定まで予約維持。共有 lock は待たず一度だけ試し、競合・読取不能は約定だけ停止。同 tick の保護処理へ進み、取消／観測失敗で exits を止めない |
| 新 D | D-PENDING-GAP | 現行 scheduler の latch/DD gate 欠落。実装後は解消記録にする（欠陥を F としない） |
| 新 D | D-CANCEL-STALL | 全理由共通の CANCELLING 滞留と PROTECTION_PENDING の走査欠如。別 ticket。DB 遷移前失敗と即 reset の残余も §4 に残す |
| 新 F | F-PENDING-TICK | 実装後の一度の非 blocking state 読取・競合時の即時停止・snapshot DD 判定・取消・例外隔離。§3 の動作 |
| 新 F | F-LATCH-WRITERS | §2/4 の全 writer と core_lock 配線（確認済み事実） |
| 新 F | F-PENDING-CANCEL | 共通 cancel_order の結果／予約解放と他の呼出し元: cancel_intent (executor:1195)、market_close (scheduler:1182)、全取消 (:1306)、reservation (:1370)、rate_unavailable (:1442)。expiry の別 broker 取消経路も関連付ける |
| 新 F | F-PENDING-REPLAY / F-PENDING-VIEW | tick→解除→intent、core_commit と限定再評価、activity と既存 trade event／理由表示を記録 |
| 新手動辺 | E-PENDING-STATE | Scheduler._tick_body → StateStore.shared_guard → GuardedState.projected。resource=state:app_state.kill_switch_latched / marker:reset_marker。K-LATCH-WRITERS / K-PENDING-STOP |
| 新手動辺 | E-PENDING-DD | Scheduler._tick_body → current_account / drawdown_pct / risk.drawdown_kill_pct → fills_allowed。state writer への辺は作らない |
| 新手動辺 | E-PENDING-CANCEL | tick → _cancel_all_pending → Executor.cancel_order → PaperBroker.cancel / transitions。resource=db:orders.close_reason / activity:kill_switch_pending_cancel / activity:limit_cancelled。上記の他呼出し元も併記 |
| 新手動辺 | E-PENDING-TRADE | transitions observer → ops.trade_events (reason_code=close_reason) → afx-ops trade_render._REASONS → bot/notify。resource=trade event / db:orders.close_reason。K-CONSUME |
| 新手動辺 | E-PENDING-REPLAY | run_replay → Scheduler.tick → 同じ停止／取消 → metric／注文 artifact／backtest_runs.core_commit。K-REPLAY |
| 新手動辺 | E-PENDING-METRICS | 保存 run → baseline_monitor／承認候補の比較表示。resource=metric:kill_switch_latches / db:backtest_runs.metrics_json / db:backtest_runs.core_commit。latch ≥ 1 または DD 閾値以上の区間を対象とし、max_drawdown は実現損益ベースなので非該当証明に使わない。保存物で判定不能な境界比較は両側再実行（§5） |

指揮者が空き番号へ採番し、F25–F27（replay）・F32–F34（表示）等との関連、anchors/resources/tests/related と鮮度を実装と同 commit で更新する。

実装 brief:

```text
守る契約: 既存 K 群を維持し、K-LATCH-WRITERS と K-PENDING-STOP を追加。writer 全数 pin と §8 の挙動 test を満たす。
実装上の注意: scheduler.py:287 付近の fills_allowed 初期代入を新 guard より前へ移すか AND に変え、新しい停止判定の False を上書きしない。
禁止経路: scheduler の latch mutation/event INSERT、停止判定後の fills 再許可、取消／観測例外による exits スキップ、根拠なし終端化、replay→手動 rebase 参照。
```

## 8. 実装 task と test 要件

| ID | 成果物 | 完了条件 |
|---|---|---|
| T1 | tick の state／DD 停止とラッチ取消 | 一度の非 blocking 読取（既存 API の到達済み deadline、sleep／再試行なし）、初期代入による停止上書きの防止、既存取消経路、例外隔離が §3–4 を満たす。writer 全数と呼出し元の直列化を pin |
| T2 | 観測・理由表示・replay 比較注記 | §5–6 の activity、afx-ops _REASONS と test_ac20_close_reason_table 更新、core_commit 境界と §5 の再評価範囲・判定不能時の両側再実行を反映 |
| T3 | 指揮者の影響地図差分／閉鎖記録 | §7 の F/K/D/辺・metadata・鮮度を実装と同 commit へ。CANCELLING／保護滞留と state 消失を別 ticket に記録 |

実装時に既存の scheduler／executor／state／kill_switch_replay と afx-ops の所有 test へ追加する。本改訂では実行しない。barrier 競合 test は要求せず、writer の全数と呼出し元の core_lock／replay 単一 thread を pin し、次の挙動と併せて検証する。

| 条件 | 必須の観測 |
|---|---|
| ラッチ先／tick 先、複数 pending、marker raw false/true | ラッチ先は fill=0・全取消。tick 先は成立前として扱う。marker も停止／取消し、raw/generation/latched_at を変えない |
| clear の DD 閾値未満／一致／超過 | 一致・超過は fill=0、DD だけでは取消も latch 書込みもしない。既存 gate 条件も維持 |
| 共有 lock 競合／state=None／parse/load/flock 例外 | fill=0、障害による取消なし。LOCK_NB は一度だけ試し、競合時は待たずに同 tick の保護処理へ進む。sleep／再試行がなく day 決済／SL/TP 継続、障害 activity は遷移時のみ。正常読取で回復。同期ファイル I/O の時間上限を test の保証にしない |
| MTM 見送り（stale／評価例外）、snapshot の年齢が 10 分未満／ちょうど／超過 | 未満・ちょうどは最新 snapshot の DD で判定し、同 tick の MTM 成功を要求しない。超過は account_unknown の既存全取消と fill=0。別ペアの到達も含め day 決済／SL/TP は継続 |
| rebase 後（同時刻に旧 snapshot と新 snapshot）、次の MTM | id の新しい hwm=equity を採り DD=0。state clear かつ他条件を満たせば約定許可、latched なら DD=0 でも停止／取消。次の MTM は rebase 後の HWM を継承して DD 判定 |
| state／DD guard で False の後に account 有効／予約再検証成功／ready | scheduler.py:287 付近の初期代入を含め False→True に戻らず fill=0 |
| cancel ok／unknown／broker 例外／DB 遷移失敗／activity 失敗 | 確定分だけ解放。PENDING_FILL はラッチ中再試行、CANCEL_UNKNOWN は not_found のみ終端化。CANCELLING／rejected の PROTECTION_PENDING は滞留する現状を正しく記録。他注文と exits を妨げない |
| 解除／再起動、expiry／degraded／閉場 | cancelled は復活せず、新 intent は gate 評価。ラッチ中の残存 pending は再走査。§4 の観測限界を固定。期限・既存理由・指標帯・同一バー TP 抑止・SL 優先を維持 |
| replay のラッチ／DD 到達と解除日、理由表示（latch 数 0・DD 停止該当も含む） | tick→解除→intent、非該当 fixture 不変、該当の注文差と同入力決定性、既存 artifact／latch 数／解除失敗補償を確認。kill_switch の取消 event が既知理由で表示される |
| 保存 run の再評価判定と境界比較 | latch ≥ 1、または latch 数 0 でも DD 閾値以上の区間があれば対象。完全な snapshot 保存物で latch 数 0・全区間 DD 未満と確認できる run は除外。metrics_json の max_drawdown だけ／保存物不完全なら非該当とせず、境界 commit をまたぐ baseline／candidate の両側を修正後の同条件で再実行する |

## 9. 範囲外

- 既存建玉の全決済、外部 broker 指値の競合制御。
- CANCELLING の再走査・PROTECTION_PENDING の保護走査は全理由共通の別 ticket。
- ファイル消失の厳格読取は別 ticket（または `killswitch-generation-floor-on-state-recreate` に追記）。
- 解除時の世代指定省略と DD 事前表示は `killswitch-reset-implicit-generation-and-dd-preview`。

## 変更履歴

| 日付 | 版 | 変更 | 理由 | commit |
|---|---|---|---|---|
| 2026-11-16 | C0 | A 推奨、最終 fill guard、取消要求回復、replay 比較・影響地図・test 要件を設計 | ラッチ中の予約指値による新規建玉を停止する | 41915a59 |
| 2026-11-16 | C0.1 | 1 周目の採用全件を反映。writer 契約、一度の読取、DD 停止、取消と読取障害の分離、限定再評価、理由表示と台帳を整理 | source 全数と裁定に合わせて過剰な同期／永続化を除く | f11f7358 |
| 2026-11-16 | C0.2 | 2 周目の採用全件を反映。DD 停止の再評価範囲、旧保存物の判定限界、一度の非 blocking 読取、snapshot 鮮度／rebase 契約と test 要件、fills_allowed 初期化の注意を追加 | latch 数 0 の漏れと lock 待ちによる保護処理遅延を除き、既存 source の契約に揃える | fed370dc |
| 2026-11-16 | C0.2 | 閉鎖確認 (指摘なし) の後、利用者が承認 | 実装へ進む | (実装と同じ commit) |

## 逸脱・未完

2 周目の採用事項は全件反映した。source 照合により旧 metric の max_drawdown では含み損益込み DD の非該当を判定できないこと、既存 shared_guard／load の到達済み deadline で一度の非 blocking 試行が可能なことを確認し、§3/5/7/8 に反映した。設計上の逸脱はない。指定設計書だけを改訂し、コード・test・ticket・影響地図は未変更。単一スレッドで照合・自己点検し、test は依頼どおり未実行。実装・挙動検証・別 ticket 起票／追記・該当保存 run の再評価・境界 commit 確定と消費者への注記・指揮者の地図更新は未着手。CANCELLING／PROTECTION_PENDING の滞留、state 消失、短い未観測 latch と DB 失敗後の即 reset の限界は §3–4/9 に残した。
