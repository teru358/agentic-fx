# kill switch ラッチ中の未約定指値 — 設計 C0

対象: [ticket](../../tickets/open/killswitch-latch-pending-limit-fills.md)。source 確認: branch `kslimit`、HEAD `78dd2535`。以下の変更は提案であり、現状の実装済み動作とは区別する。

## 1. 起きる動作と決定

**案 A（level-triggered の全 pending 取消）を推奨する。** Scheduler はラッチを観測した tick で未約定指値を全取消し、その tick の新規約定を止める。取消できなかった注文は次 tick に再処理する。既存建玉の SL/TP と day 決済は継続する。解除しても取消済みの計画は復活せず、判断 loop が最新条件で新しい注文を出す。Scheduler 自身は latch・generation・latched_at を変更しない。

取消は予約リスクを即座に帳消しにする操作ではない。broker の取消確定／不存在確認により終端化した分だけ解放する。ラッチ中の建玉全決済は行わない。

| 案 | 利点 | 不採用／採用の理由 |
|---|---|---|
| A: 毎 tick 全取消 + 約定禁止 | 古い計画を廃棄し、確定分の予約枠も解放 | 採用。市場閉鎖・予約維持不能・口座不明と同じ削減方向。再起動時に発動の edge を見逃しても回復する |
| B: 約定だけ保留、注文維持 | 一時停止後に同じ注文を使える | 解除直後に発動前の計画が約定しうる。予約枠も占有し続ける。将来 broker 側指値では scheduler の抑止だけでは止まらない |
| C: 発動時に一度だけ取消 | 取消走査を減らせる | 発動後の再起動、取消失敗、後から pending に戻る reconcile を取りこぼす。A の代わりにはしない |

取消失敗で PENDING_FILL に残った注文も、解除後に古い計画として約定させない。そのため取消要求を永続化して再試行する（§3）。利用者の裁定を必要とする未決事項はない。A は本 C0 の推奨判断であり、利用者が過去に採用済みと表現しない。

## 2. 裏取りした現状

| 根拠（source の symbol） | 確認できたこと・設計への意味 |
|---|---|
| `core/scheduler.py::Scheduler._tick_body` | MTM → unknown 解決 → expiry → account/予約再検証 → day 決済 → fills → exits。account 不明／equity 非正は `_cancel_all_pending` + `fills_allowed=False`。予約維持の false／例外も約定を止める。fills はさらに `state_fn()=="ready"` が必要 |
| `Scheduler._process_limit_fills` / `_suppress_fill` | PENDING_FILL の到達判定後、指標回避帯だけが抑止し、PROTECTION_PENDING → OPEN。scheduler 内の `kill_switch_latched` 参照は 0 件。SL/TP は別の `_process_exits` |
| `core/executor.py::Executor._evaluate_and_execute_open` | risk gate の理由に `kill switch` があり `latched` がない場合に state を true にする。予約約定で gate は再実行されない |
| `store/state.py::StateStore.load` / `shared_guard` / `update` | load は marker 中を latched に投影。shared_guard は共有 flock を保持したまま `GuardedState.projected` を読める。破損等では state=None または例外。update/reset/confirm は排他 flock。同世代 true の単独 update は no-op |
| `service.py::build_app` / `tools/mission_registry.py` / `backtest/runner.py::run_replay` | 実際の broker 構築は PaperBroker。`src/` に Mt5Broker や MT5 発注実装はない。MT5 は datafeed/history 取得。PaperBroker.submit は ID を返すだけで、指値約定は scheduler のローカル状態遷移。従って現行 B は技術的には可能だが、A の採用理由は「既に実 broker 指値があるから」ではない |
| `service.py::_scheduler_tick_body` / `loops/trade_loop.py` | tick と通常／signal の commit-core は同じ core_lock。取引判断は別 thread でも、DD による latch 更新と約定は通常経路では同時実行されない。shell の state mutation まで core_lock で覆うわけではない |
| `Executor.cancel_order` / `Scheduler._resolve_unknowns` | ok は CANCELLED + close_reason + `limit_cancelled`。unknown／broker 例外は CANCEL_UNKNOWN、rejected は PROTECTION_PENDING。unknown の終端化は ok かつ not_found のときだけ。DB 例外では CANCELLING 等に残る場合がある |
| `core/executor.py::_EXPOSURE` / `_UNKNOWN` | pending・cancelling・unknown・protection_pending を予約リスク／件数に算入。cancel_unknown は新規 open を止める。cancelled/expired は解放。CANCELLING は exposure だが既存 unknown 解決の走査対象ではない |
| `config/settings.yaml.example` / 主設計 §5 | 公開例の risk_per_trade_pct=0.5、max_total_risk_pct=1.5、max_positions=2。未約定も予約に算入。主設計の kill switch は「新規停止、既存建玉は決済せず保護継続」。予約枠内でも停止後の新規建玉は契約違反 |
| `backtest/runner.py::run_replay` / `_auto_release_kill_switch` | 同じ Scheduler/Executor/PaperBroker。各ループは scheduler.tick → 自動解除判定 → 前 tick の提案執行。解除は新 HWM snapshot と state を変更し、commit 失敗で rollback／再ラッチ補償 |

提示事実と矛盾はなかった。ただし「SL/TP は常に走る」は約定 gate で止めない意味である。現行には閉場・MTM 失敗の早期 return があり、あらゆる tick で exits が呼ばれるわけではない。本変更はその範囲を拡大せず、新しい guard／取消／観測の例外による exits のスキップを禁止する。

## 3. 取消・読めない state・期限

開場 tick で既存の MTM → unknown 解決 → expiry を維持し、その直後、account/予約再検証より前に guard を判定する。閉場は既存 market_close の理由を維持し、残存 pending に対する guard 取消の再試行も行う（初回閉場処理だけに依存しない）。degraded 中でも取消は実行し、ready だけを取消の条件にしない。

| guard の結果 | 処理 |
|---|---|
| projected.latched=true（marker を含む） | まず `fills_allowed=False`。`_cancel_all_pending(now, reason="kill_switch", event="kill_switch_pending_cancel", why="kill switch ラッチ中")` と同じ注文単位の取消経路 |
| state=None／load・flock・parse 等の例外 | まず `fills_allowed=False`。安全側に同じ全取消を試み、reason=`kill_switch_state_unavailable`、activity=`kill_switch_pending_cancel` の本文で「state 読み不能」と区別。state を false に補修しない |
| projected.latched=false | 従来の account／予約維持／ready 条件を全て満たした場合だけ約定候補へ進む。既に取消要求のある注文は再試行し、約定候補から除く |

guard の取得は monotonic deadline と stop_event で打ち切る。C0 の提案値は取得待ち **100ms/tick の共通予算**（注文ごとに予算を更新しない）。予算切れは state 読み不能と同じ。replay の取引時刻・artifact に wall clock を混ぜない。

取消要求は `orders.close_reason` に上記 reason を保存してから broker.cancel を呼ぶ。非終端のこの値は「取消要求理由」であり、取消成功を意味しない、と明示する。既存 `orders.update_fields` で保存でき、新しい schema は不要。PENDING_FILL の該当 reason は解除後も約定せず取消を再試行する。CANCELLING に残ったものは該当 reason に限定して reconcile に接続し、再 submit や根拠なしの CANCELLED 化をしない。既存 CANCEL_UNKNOWN → not_found の終端化でも reason を保持する。broker が rejected（既に約定済み等）なら PROTECTION_PENDING の既存保護経路へ渡し、ラッチを理由に建玉を消したり全決済したりしない。

取消処理の外側にも例外境界を置く。pending 列挙、要求保存、注文単位の取消、復旧、activity の失敗は fills を再許可せず、後続の day 決済／SL/TP を妨げない。要求保存できなかった注文 ID はプロセス内でも取消待ちとして保持し、解除後も保存／取消を再試行する。**DB に要求を保存できず、そのままプロセス停止し、次の正常起動前に解除された場合まで取消意思を復元する保証はない**。永続媒体不達の限界として残す。通常の取消失敗・再起動は保存済み reason で回復できる。

期限は延長しない。expiry が先なので同 tick の期限切れは既存 EXPIRED／`limit_expired` を優先する（既存比較は expires_at < now）。取消要求済みの再試行では元の guard reason を失わない。market_close・reservation・account_unknown・equity_nonpositive は既存値を維持し、先に終端化した注文を再取消ししない。複数原因がある開場 tick は guard を先に判定し、guard 原因を優先する。

補足: 現行 `_load_unlocked` はファイル不存在で既定 AppState を返すため、例外捕捉だけでは「稼働中の state 消失」を検出できない。guard に opt-in の厳格読取（shared_guard の既定挙動は維持し、同じ共有 lock 内で実ファイル存在を要求）を追加し scheduler はこれを使う。replay は最初の tick 前に明示的に初期 state を作る。世代 floor の復旧設計は別 ticket のままとする。

## 4. 同 tick の順序と競合

tick 冒頭の一度の load だけでは、load 後に外部 writer がラッチを保存してから OPEN 遷移する TOCTOU が残るため採用しない。**各注文の最終約定区間を `StateStore.shared_guard` で囲み、projected が clear であることの確認から PROTECTION_PENDING → OPEN と filled_ids への追加まで共有 flock を保持する。** バー取得・到達判定・指標帯判定は先に済ませ、activity と同一バー exit は guard の外に置く。guard 内で state.load/update/reset/confirm を呼ばない。

- latch writer が先なら、その注文は約定しない。取消は共有 flock を解放してから行い、残りの pending も取り消し、この tick の残りの fills を止める。
- fill の共有 flock が先なら、その約定完了後に writer が latch を成立させる。この一件は発動前の建玉であり保護を継続する。注文間に成立した latch は次の注文の guard で捕捉する。
- 通常の判断 loop が core_lock を待っている場合、tick が先に約定を完了してから DD 判定・ラッチ成立となることはある。これは成立済み latch の読取が一 tick 遅れる問題とは異なる。DD の検知自体を scheduler に移す変更はしない。

これにより「ラッチ保存済みなのに次 tick まで約定を許す窓」は塞ぐ。発動と取消の反映は次の scheduler tick まで遅れうるが、約定直前 guard で先に停止できる。live は毎分監視、replay は基底足ごとの観測であり、停止・過負荷時に壁時計の遅延上限を保証しない。発動後一度も tick が走らず解除まで済んだ短い latch は level-triggered 取消の観測外である。その間の約定は最終 guard で禁止するが、未観測の計画を事後に全廃する世代追跡は追加しない。

lock 順は core_lock → state 共有 flock とし、guard 内は短いローカル DB 遷移だけにする。broker、通知、ops lane は入れない。新たな同期通知も追加しない。外部 broker 指値導入時はこのローカル線形化だけでは約定を止められないので、broker 取消／照会／約定競合の契約を別途満たす必要がある。

## 5. replay と比較

ラッチ中に到達していた指値が消えるので、約定数・PnL・PF・DD、後続の latch 数も変わりうる。「保護を強めるので全 metric が改善する」とはしない。scheduler → 自動解除 → 提案執行の順を維持するため、解除日の最初の tick でも古い pending を先に処分し、解除後の新しい intent は従来 gate で評価する。

K-REPLAY の理由文字列、kill_switch_events artifact の形と snapshot 参照、kill_switch_latches の数え方、自動解除理由／snapshot／commit 失敗補償は維持する。取消 activity は latch 遷移ではなく、latch 数や event を増やさない。手動 rebase module／ledger／source を replay から参照しない。

保存済み評価は履歴として残すが、修正前後の数値を戦略改善の A/B に混ぜない。比較は同じ code revision（修正後）、入力 dataset／hash、config、戦略、期間、基底足、seed 等で baseline と candidate を両方再実行する。評価 artifact の来歴に revision と本 ticket の約定規約変更を残し、既存の保存済み成績・承認候補を利用する側でも再評価を要件にする。旧規約互換スイッチは作らない。ラッチ中 pending がない fixture は従来結果不変、ある fixture は差の理由を注文単位で説明する。

## 6. 観測

新しい **activity の kind** は `kill_switch_pending_cancel`（SYSTEM、件数と要求理由、取消完了とは書かない）と `kill_switch_fill_guard_unavailable`（SYSTEM、読取不能で新規約定停止）の二つ。前者は pending がある取消試行時だけ、後者は障害への遷移時に一度記録し、正常読取後に再度の障害を記録可能にする。取消 retry の詳細は既存 `cancel_all_pending_error`、成功は注文 ref_id 付き `limit_cancelled`、不明解消は `unknown_resolved` を使う。例外文は safe_error_text、観測失敗は best effort とする。

orders.close_reason は `kill_switch` / `kill_switch_state_unavailable`。取消結果不明を成功件数に含めない。K-REPEAT の latch activity の反復問題には手を付けず、scheduler から `kill_switch_latched` を再発行しない。

status は既存の kill_switch 投影、注文／pending 投影、直近 activity から確認する。web は kind を選別せず activity を描画するため新しい本文も表示される。guard 読取障害は latch の新規成立と区別し、既存 status の state 読取不能表示を false にしない。bot／notify 向け event schema・event 種別は増やさず、ラッチ通知は既存 K-NOTICE の sweep に任せる。取消結果不明の既存通知は維持する。全注文の取消通知追加は、発動通知と重複するため不要。

## 7. 影響地図へ渡す変更案

正本: [kill-switch 頁](../../impact-map/domains/kill-switch.md)、工程: [影響地図設計 §6.1–6.2](2026-11-14-code-impact-map-design.md)。同頁は scheduler 約定経路を欠き、E18–E23 は時価 snapshot の辺であって代用にならない。**頁改訂が必要**。下表は予定台帳であり新 F/E は仮 ID（実装後に指揮者が空き番号へ採番）。本作業では頁本文・metadata・verification を変更しない。

| 区分 | ID | 守る内容／変更案・根拠 |
|---|---|---|
| 守る K | K-LATCH / K-REPEAT | DD 理由 producer/consumer、通知失敗でも state を戻さない、世代 no-op を維持。scheduler は latch writer にならない |
| 守る K | K-RECONCILE / K-RESET / K-START | marker の安全側投影、世代・発動時刻・reset/confirm の規則を維持。厳格読取は opt-in、既存 load の初期化規約を変えない |
| 守る K | K-REBASE / K-RESET-UI | 手動 HWM 操作の認可・全注文終端等の前提、通常 reset と表示の区別を維持。取消完了は rebase の他の前提を代替しない |
| 守る K | K-NOTICE / K-LANE / K-VIEW / K-CONSUME | sweep 一元化、lane 再入規律、status は観測専用、配送 cursor を維持。activity は配送成功の証拠にしない |
| 守る／変わる K | K-REPLAY | 理由・artifact schema・比較 metric の意味・解除補償は維持。約定規約と数値 baseline は変わるので同 revision 再評価を追加 |
| 拡張 K | K-LATCH | 「open gate で停止」に加え、予約済み指値の新規約定も latch／marker 中は禁止と明文化 |
| 新 K | K-PENDING-CANCEL | 毎 tick 全取消、確定まで予約維持。取消要求は解除／再起動後も再試行、cancel_unknown の根拠なし終端化禁止 |
| 新 K | K-FILL-GUARD | state 不明は fail-closed、各 fill の共有 guard と writer の排他で順序確定。取消／観測失敗で SL/TP を止めない |
| 新 F | F-PENDING-GAP | 現行 `_tick_body` / `_process_limit_fills` の latch 読み欠落を記録。実装後に guard と取消の実動作へ置換し、欠陥解消を明記 |
| 新 F | F-PENDING-CANCEL | `_cancel_all_pending` → `Executor.cancel_order`、close_reason、cancel_unknown／CANCELLING の回復、予約枠解放時点 |
| 新 F | F-FILL-ORDER | service／TradeLoop の core_lock と `StateStore.shared_guard` の最終 fill 区間、厳格読取と deadline |
| 新 F | F-PENDING-REPLAY | run_replay の tick→解除→intent の順、旧評価との比較条件、初期 state 明示作成 |
| 新 F | F-PENDING-VIEW | 新 activity と既存 status/web への投影、bot event を増やさないこと |
| 新手動辺 | E-PENDING-STATE | `Scheduler._tick_body` / `_process_limit_fills` → `StateStore.shared_guard` → `GuardedState.projected`。resource=`state:app_state.kill_switch_latched`, `marker:reset_marker`。K-FILL-GUARD／K-RECONCILE |
| 新手動辺 | E-PENDING-CANCEL | `Scheduler._tick_body` → `_cancel_all_pending` → `Executor.cancel_order` → `PaperBroker.cancel` / orders 状態遷移。resource=`db:orders.close_reason`, `activity:kill_switch_pending_cancel`, `activity:limit_cancelled`。K-PENDING-CANCEL |
| 新手動辺 | E-PENDING-RETRY | 取消要求保存 → scheduler の pending/CANCELLING 再走査 → reconcile。resource=`db:orders.close_reason`。解除後も禁止する fill 経路を示す |
| 新手動辺 | E-PENDING-REPLAY | `run_replay` → `Scheduler.tick` → 同じ guard/取消。resource=比較 metric と注文 artifact。K-REPLAY |

頁の anchors/resources/tests/related と鮮度記録も実装差分に合わせて指揮者が裁定し、code と同 commit で更新する。既存 F25–F27（replay）・F32–F34（表示）との関係を追記する。

実装 brief の二行案:

```text
守る契約: K-LATCH/K-REPEAT/K-RECONCILE/K-REPLAY/K-NOTICE と追加 K-PENDING-CANCEL/K-FILL-GUARD。本 C0 §8 の合成 test で同 tick・marker・読取不能・取消失敗・解除後・replay を検証する。
禁止経路: guard clear の確認と OPEN 遷移の間で共有 flock を解放しない。guard 内の state 再入・broker/通知/ops lane 呼出し、scheduler からの latch mutation/event INSERT、取消要求済み注文の再約定、観測例外による exits スキップ、replay→手動 rebase 参照を禁止する。
```

## 8. 実装 task と test 要件

| ID | 成果物 | 完了条件 |
|---|---|---|
| T1 | state guard と tick の取消 gate | 厳格読取・共通期限・marker 投影、level-triggered 取消、注文直前の排他と例外隔離が §3–4 を満たす |
| T2 | 取消要求の保存／回復 | close_reason の非終端意味を明記。pending/CANCELLING/unknown の解除・再起動後の回復と予約維持を確認 |
| T3 | activity／replay と合成 test | 下記の挙動要件が通り、比較規約変更の来歴と初期 state 作成を記録。通知 schema は維持 |
| T4 | 指揮者の頁差分／閉鎖記録 | 新 F/K/手動辺と metadata・鮮度候補を裁定し、実装と同 commit に含める。実行結果と未検証を区別 |

test は既存 `tests/core/test_scheduler.py`、`tests/core/test_executor.py`、`tests/store/test_state.py`、`tests/backtest/test_kill_switch_replay.py` 等の所有領域で追加する。必要な既存 ops 表示／解除回帰も実装時に選ぶ。source の文字列 pin だけで合格にしない。

| 合成条件 | 必須の観測 |
|---|---|
| ラッチ成立と価格到達が同 tick、複数 pending | latch 先なら fill=0・取消。fill 先ならその建玉だけ先行成立、後続は停止。実 thread/process の barrier で順序を固定し、sleep に依存しない。通常／signal と tick の core_lock 配線も確認 |
| marker 中（raw false/true） | 両方で停止／取消、raw/generation/latched_at を変更しない |
| state parse/load/flock 例外、state=None、稼働中ファイル消失、取得 deadline | fill=0、取消試行、同 tick の既存 SL/TP は実行。state 初期化と replay 初期 state は正常動作 |
| cancel ok/unknown/例外/rejected、DB 遷移／要求保存／activity 失敗 | ok だけ解放、unknown は予約維持・新規停止、not_found のみ終端化。rejected は保護経路。PENDING/CANCELLING の残存を回復し、他注文と SL/TP を妨げない |
| 解除後／再起動後 | cancelled は復活しない。保存済み取消要求は clear でも約定せず回復。新しい intent は gate 再評価。発動から解除まで tick がない条件の観測限界も固定 |
| expiry／degraded／閉場と既存取消 | 期限を延長せず理由の優先が §3 と一致。ready でなくても取消。指標帯抑止・同一バー TP 抑止・SL 優先は維持 |
| replay のラッチ中到達と解除日 | tick→解除→intent を維持し古い注文は消える。非該当 fixture 不変、該当 fixture の差を説明、同入力再実行で決定的。artifact の理由／snapshot 参照／latch 数と解除 commit 失敗補償を維持 |

## 9. 範囲外

- DD がさらに深い場合の既存建玉全決済は、利用者の判断を要する別設計。
- 解除時の世代指定省略と DD 事前表示は別 ticket `killswitch-reset-implicit-generation-and-dd-preview`。

## 変更履歴

| 日付 | 版 | 変更 | 理由 | commit |
|---|---|---|---|---|
| 2026-11-16 | C0 | A 推奨、最終 fill guard、取消要求回復、replay 比較・影響地図・test 要件を設計 | ラッチ中の予約指値による新規建玉を停止する | 未 commit |

## 逸脱・未完

設計書のみ新規作成。コード・test・ticket・影響地図の頁は変更していない。test は依頼どおり未実行であり、既存失敗との判定はしていない。単一スレッドで source と設計を自己点検した。実装、挙動検証、保存評価の再計算、指揮者の影響地図更新は未着手。永続媒体不達と短時間の未観測 latch の保証限界は §3–4 に明記した。
