# 発注・決済の通知 設計書

**版:** v1.1 (v1.0 はユーザー承認済み。v1.1 は sink の busy 再試行の技術改訂、§12)
**対象:** チケット `trade-order-and-close-notices` (ユーザー要望 2026-10-20「発注内容と決済情報を通知してほしい」)。
**材料:** 現状調査 `tmp/design-trade-notices/survey.md` (file:line つき)。以下の「調査 §n」はその節。
**関連チケット:** `trade-order-and-close-notices` (本設計で実装)、`ops-bot-initial-start-replays-all-events` (T0 で閉じる)。

## 0. 目的、範囲、決定

利用者は、取引が起きたことも結果も Discord で知れない。web の活動ページと `afx ctl` で後から見るだけである。
いまの操作 API の event は 6 種で、取引の event は無い (調査 §3.1)。daemon 直送の Discord 通知も異常系 4 種だけで、正常な発注と決済は送らない (調査 §3.3)。

この設計は次の 3 つを足す。

1. 発注 (指値を入れた / 約定した / 拒否された / 取消・失効した) の event。
2. 決済の event。約定価格、損益、保有時間、理由を持つ。
3. bot がそれを日本語の短い文にして投稿する。

加えて、取引 event の履歴が増える前に直す必要がある既知の欠陥 (初回起動で全 event を流す。チケット `ops-bot-initial-start-replays-all-events`) を、本設計の配備前提 T0 として含める。bot と notify の runtime まで含め、snapshot に「event の cursor が初期化済みか」の明示 flag を持たせて直す (§8.3)。

| 項目 | 決定 |
|---|---|
| event 種別 | 2 種を足す。`order_state_changed` (placed / filled / rejected / cancelled / expired、12 field) と `position_closed` (13 field)。値は既存契約どおり code・識別子・数だけで、自由文 (rationale、reasoning、reject reason) は入れない (§3)。 |
| 数値の持ち方 | **float は既存の検証を通らない** (調査 §3.1)。価格・数量・損益は field ごとに grammar を決めた固定書式の文字列で持つ。producer と afx-ops の両方が grammar を検査し、通らない event は作らない・投稿しない (§3.3)。 |
| emit 位置 | `transitions.transition` の 1 か所 (§4)。CLOSED への確定点は 2 か所、EXPIRED / CANCELLED / REJECTED も複数か所にあり、呼び出し側に足すと取りこぼす (調査 §2.3)。 |
| emit の安全 | 保存は既存の `ops_events` で、DB の schema は変えない (§2)。transition は commit の後に observer を呼び、observer の例外と遅延は注文処理に影響させない。observer は flock もファイル I/O もしない (§4.2)。ただし同一 DB の書き込み競合は完全には消せず、sink の接続は待たずに捨てる (§4.2、残余リスク §4.5)。 |
| 欠落の観測 | 捨てた event は理由別の counter、warning ログ、プロセス内の health 状態に数える。counter の件数は `afx ctl status` にも載る。`service_health_changed` は保存できたときの補助通知で、失敗時に必ず出るとは約束しない (§4.2)。 |
| bot の既定 | 全種類を出す (ユーザー要望、`trade_notices` の既定 `all`)。数量と損益の絶対額も既定で出す (`trade_amounts` の既定 `absolute`)。文面は固定 template。出す種類 (`trade_notices`) と絶対額の有無 (`trade_amounts`) を `ops.toml` の optional key で切り替える (§5)。取引通知には設定済みの mention を付けない。 |
| notify の既定 | 取引 event は既定で配送しない (opt-in)。notify 用 `ops.toml` の optional key `deliver_trade_events` を true にしたときだけ配送する (§5.3)。 |
| web / CLI | 新しい画面は作らない。`afx ctl events` は新 code がそのまま載る。活動ページは既存の activity 行のまま (§6)。 |
| 遮断 | event の値は agent の読み取り面に無い。新しい経路を作らない。既存の LLM 入力はバイト一致で pin する (§7)。 |
| 配備の順序 | afx-ops (allowlist + renderer + filter + 設定 reader) を先に配って再起動と cursor 前進を確認し、その後に daemon の emitter を有効にする。emitter は settings の `notify.trade_events` (既定 true) で止められ、rollback はこれを止めてから afx-ops を戻す (§10.3)。 |
| 初回起動 | 過去の取引 event は流さない。bot / notify の初回は最初の正常な poll の high watermark から始める (§8.3)。 |
| 実装 | 3 束。T0 = 初回 cursor (配備前提。bot / notify の state と runtime、snapshot schema の版上げ)、T1 = afx-ops 側、T2 = daemon 側 (settings の schema 版上げを含む。§10)。 |

## 1. 用語と非目標

| 用語 | 意味 |
|---|---|
| 起点 code | 注文のもとになった判断 Mission の起動元。`cron` か `signal:<plugin>`。戦略 plugin の帰属ではない (§2.2)。 |
| mode | `paper` か `live`。broker 種別で、現状は `paper` だけ (調査 §1.7)。 |
| state_mode | daemon の運転状態 `learning` / `trading` (`core/contracts.py:48` の `Mode`)。broker 種別の `mode` とは別物 (§2.5)。service が起動時に 1 回読んだ値をメモリで持つ (§4.2)。 |
| R | 価格ベースの損益倍率。(決済価格 − 約定価格) × 方向 ÷ (約定価格と SL の差の絶対値)。commission とスプレッドは含めない (§3.3)。 |
| observer / sink | observer は `transitions.transition` が commit の後に呼ぶ読み取り専用の関数。sink は event を受けて `ops_events` に書く部品で、専用の接続と書き込み thread を持つ。 |

非目標:

- 日次サマリ、損益グラフ、通知のまとめ送信、DM、戦略別の成績 (帰属は別チケット)。
- gate が拒否した intent の通知 (orders 行が無く、1 時間ごとに起きうる。activity と web で見られる)。
- 送信結果不明などの異常系の event 化と kill switch 自動ラッチの event 化、実発注 (live) の broker (`mode` の欄だけ確保する)。
- notify (webhook) の schema 変更。allowlist に code を足すだけで、形は固定のまま。

## 2. データと材料

### 2.1 既にあるもの

- orders の全列 (`store/db.py:282-298`: id、intent_id、approval_id、client_order_id、pair、direction、entry_type、horizon、status、quantity、filled_quantity、remaining_quantity、avg_fill_price、requested_price、close_price、stop_loss、take_profit、fees_swap、realized_pnl、close_reason、broker_order_id、broker_position_id、broker_synced_at、expires_at、created_at、updated_at、filled_at、closed_at)。event の値は遷移後の行から作り、呼び出し側の局所変数は使わない。
- 起点は `orders.intent_id` → `trade_intents.mission_id` → `missions.trigger`。
- 状態遷移の関数 `core/transitions.py:38-47` (observer の差し込み点)。保存先 `ops_events` (`store/db.py:479`)。口座通貨は `settings.account_currency`。

### 2.2 起点 code は戦略の帰属ではない

orders に戦略名の列は無い (調査 §1.11)。引けるのは「どの signal plugin が Mission を起こしたか」までで、その Mission の中で LLM が別の根拠を使った可能性は消せない。
そのため event の field は `strategy_code` でなく `origin_code` とし、意味を「起点」に限る。戦略別の成績は別チケットの帰属が入ってから足す (§11)。

`origin_code` は次の規則で作る。

- `missions.trigger` を読む境界で、`cron` の完全一致、または `signal:<plugin>` の完全一致を再検査する。`<plugin>` は plugin loader の正規名 `^[a-z][a-z0-9_]{0,63}$` (`plugin/loader.py:143`)。合わなければ None にする。長さだけの検査にしない。
- `signal:` を付けると最大 71 字になり、event の値の上限 64 字を超え得る。収まらないときは None にする (切らない)。
- 表示側 (bot) は Markdown を escape し、この値から mention を発生させない (§5.2)。

### 2.3 新しい表や列を作らない理由

event は通知用の投影で、正は orders 表である (ops-api spec §4.3 と同じ)。生成の失敗で注文処理は失敗させず、取りこぼした event は orders との突き合わせでしか復元できない。
DB の schema は変わらないので `schema_version` と migration は不要。契約が変わるのは `FIELDS` の定数と afx-ops の allowlist だけである。

### 2.4 読み取りの規則

event を作る関数は、遷移後の orders 行の `id`、`pair`、`direction`、`entry_type`、`status`、`quantity`、`requested_price`、`avg_fill_price`、`close_price`、`stop_loss`、`take_profit`、`realized_pnl`、`close_reason`、`filled_at`、`closed_at`、`intent_id` だけを読む。
`trade_intents.payload_json` の `reasoning` や `confidence` は読まない。`missions.trigger` を 1 回引く。`state_mode` は service が起動時に 1 回読んでメモリに持つ値を使い、event ごとにファイルを読まない (§4.2)。

### 2.5 learning モードでも paper の発注は起きる

現物では、取引 Mission の起動は `Mode` で止められていない (`core/scheduler.py:285-293`)。executor にも learning を理由にした拒否は無い。`Mode` を見るのは market close 時の指値取消だけ (`scheduler.py:947-952`)。
つまり learning 中にも paper の発注・約定・決済が起きる。`mode=paper` は broker 種別であって「学習中か本番運転中か」を表さない。
そこで両 event に `state_mode` (`learning` / `trading`) を足す。起動時に読めなかったときは None にする。bot は 1 語で表示する (§5.1)。値の持ち方は §4.2。

## 3. event 種別と値

### 3.1 `order_state_changed`

`ref` は `order:<orders.id>`。fields は次の **12 個**で、**全て必須** (既存の検証は field 集合の完全一致を求める)。値が無いものは None。

| field | 値 | 備考 |
|---|---|---|
| `state` | `placed` / `filled` / `rejected` / `cancelled` / `expired` | 遷移先から決まる (§4.1)。 |
| `pair` | `USDJPY` など | |
| `side` | `long` / `short` | `orders.direction`。 |
| `entry_type` | `market` / `limit` | |
| `qty` | lot の文字列 (§3.3) | `orders.quantity`。 |
| `price` | 価格の文字列 (§3.3) | placed / rejected / cancelled / expired は `requested_price`、filled は `avg_fill_price`。 |
| `sl` | 価格の文字列 | `stop_loss`。 |
| `tp` | 価格の文字列 or None | `take_profit`。 |
| `origin_code` | `cron` / `signal:<plugin>` or None | §2.2。 |
| `mode` | `paper` / `live` | 現状は `paper` 固定。 |
| `state_mode` | `learning` / `trading` or None | §2.5。 |
| `reason_code` | `close_reason` or None | cancelled だけに入る (llm_cancel / market_close / reservation / rate_unavailable / account_unknown / equity_nonpositive)。 |

成行は placed を出さず、filled の 1 件だけにする (発注と約定が同じ tick で起きるため)。指値は placed → filled / cancelled / expired の順に出る。

### 3.2 `position_closed`

`ref` は `order:<orders.id>` (発注 event と同じ)。fields は次の **13 個**で、全て必須。

| field | 値 | 備考 |
|---|---|---|
| `pair` | | |
| `side` | `long` / `short` | |
| `qty` | lot の文字列 | |
| `entry_price` | 価格の文字列 | `avg_fill_price`。 |
| `close_price` | 価格の文字列 | |
| `pnl` | 口座通貨の文字列 or None | `realized_pnl`。換算レートが一度も取れていないときは None (調査 §2.4)。 |
| `ccy` | 3 文字の通貨 code | `settings.account_currency`。 |
| `pnl_r` | R の文字列 or None | §3.3。 |
| `held_sec` | int or None | `closed_at − filled_at` を秒で。負なら 0。 |
| `reason_code` | `sl` / `tp` / `day_rollover` / `llm_close` | `close_reason`。 |
| `origin_code` | §3.1 と同じ | |
| `mode` | §3.1 と同じ | |
| `state_mode` | §3.1 と同じ | |

brief の推奨にあった `manual` と `killswitch` は入れない。手動クローズの経路も、kill switch による強制クローズも存在しない (調査 §2.7)。`expired` は建玉でなく予約の終端なので発注 event 側にある。

### 3.3 数値の書式と検査

書式は producer が作り、同じ grammar を afx-ops が受け側で再検査する。両方で通ったものだけが event になり、投稿される。

| field | 書式 (producer) | grammar (両側で完全一致) | 範囲 |
|---|---|---|---|
| `qty`、`price`、`sl`、`tp`、`entry_price`、`close_price` | `f"{x:.8f}"` の末尾の 0 と末尾の `.` を落とす (例 `150.123`、`0.1`、`150`)。指数表記は使わない | `[0-9]{1,12}(\.[0-9]{1,8})?` に合い、かつ canonical (下記) | 有限、書式化後の値が 0 より大きい、10^12 未満 |
| `pnl` | `f"{x:.2f}"`。`-0.00` は `0.00` に正規化 | `-?[0-9]{1,15}\.[0-9]{2}` | 有限、絶対値 10^15 未満 |
| `pnl_r` | `f"{x:.2f}"`。`-0.00` は `0.00` に正規化 | `-?[0-9]{1,6}\.[0-9]{2}` | 有限、絶対値 10^6 未満 |
| `held_sec` | int | 0 以上 10^10 未満 | |
| `ccy` | 大文字 3 字 | `[A-Z]{3}` | |

- producer は、書式化の後に (1) `isfinite`、(2) 書式化後の長さが 64 字以内、(3) 上の grammar への完全一致、を検査する。1 つでも通らなければ **event を作らない** (drop して counter に数える。§4.2)。値を None に置き換えない。None にするのは「元データが元々無い」場合 (`realized_pnl` が NULL、`take_profit` が無い、R の分母が 0) だけである。
- **canonical の要求。** afx-ops は grammar に合うだけで受理しない。値を parse し、producer と同じ formatter で再生成して、元の文字列と**完全一致**することを要求する (`0001.20`、`0.10000000`、`+1.5`、`1.50` のような producer が作らない書式は拒否する)。正値の判定は元の float でなく**書式化後の値**にも適用する (`0 < x < 0.5×10^-8` は `.8f` で `"0"` になり得るので、producer も afx-ops も書式化後が 0 なら通さない)。
- **8 小数の精度。** 8 小数への丸めが FX の必要精度 (JPY 絡みの 3 桁、その他の 5 桁、pipette 0.1 pip、lot 0.01) を覆うことを fixture で固定する (AC-2)。
- afx-ops は field ごとに上の grammar と範囲を検査する。通らない event は、`payload_invalid` の既存の扱いに合わせて **全体を投稿せず**、cursor を進めず止まる (既存の fail-closed。§5.2)。
- 表示の桁は 1 規則: event の桁をそのまま保持する (価格・数量は桁を足さない)。ただし `ccy` の minor unit が 0 の通貨 (JPY) の `pnl` だけは整数に四捨五入して表示する。それ以外の通貨は event の 2 桁を保持する。R は 2 桁を保持する。
- R の分母 (|約定価格 − SL|) が 0 のときは None。R は価格だけから決まり、spec も換算レートも要らない。transition の中で行だけから計算できる。

### 3.4 ref と allowlist

`_REF` に `order:[0-9]{1,19}` を足し、regex を通った後に**整数化して `1 <= id <= 2^63-1` (9223372036854775807) を検査する** (regex だけでは `order:0` と上限超過の 19 桁を通すため)。`FIELDS` に 2 code を足し、同じ内容を afx-ops の `notify/payload.py` (別 package の写し、調査 §3.1) にも足す。値の grammar (§3.3) も両側に持つ。食い違わないことを test で pin する (AC-3)。

## 4. emit 位置

### 4.1 遷移と event の対応

`transitions.transition` が commit した後、遷移先で次のように決める。

| 遷移先 (to) | event | 備考 |
|---|---|---|
| `PENDING_FILL` | `order_state_changed` state=placed | 指値を入れた。 |
| `OPEN` | `order_state_changed` state=filled | 成行の約定も指値の約定も、取消との競合で `PROTECTION_PENDING` を経た約定も同じ。 |
| `REJECTED` | `order_state_changed` state=rejected | broker 拒否、データ状態不備 (`executor.py:522`)、未解決 unknown の解決 (`scheduler.py` の reconcile)。 |
| `CANCELLED` | `order_state_changed` state=cancelled | |
| `EXPIRED` | `order_state_changed` state=expired | |
| `CLOSED` | `position_closed` | `_finish_close` と `_retry_close` の両方を 1 か所で捕まえる。 |
| その他 | 出さない | submitting / submitted / protection_pending / closing / 各 unknown は途中状態。 |

orders 行が無い gate 拒否は event にならない (非目標)。

### 4.2 observer と sink

1. `transitions.transition` は `update_fields` の commit の後で、登録された observer を呼ぶ。signature は変えない。登録の無い接続 (バックテスト、test) は何も起きない。
2. observer の登録簿 (registry):
   - `transitions.observe(conn, fn)` は token を返す。`transitions.unobserve(token)` で解除する。同じ接続への重複登録は拒否する (例外)。登録簿は接続を強参照するので、解除を必ず行う。
   - 登録は scheduler、supervisor、ops listener の起動より前に行う。登録に失敗したら daemon を起動しない (fail-closed。observer が無いまま取引を始めると、欠落が黙って起きる)。
   - 停止の順序は、scheduler と supervisor を止めた後に unobserve、その後に sink を止める。
   - 登録を呼ぶのは `service.py` の 1 か所だけにする (AC-10 で pin する)。バックテストは別の `:memory:` 接続に自前の Executor を作るので、その登録簿に入らない。
   - daemon の emitter には settings の enable flag `notify.trade_events` (既定 true) を置く。false のとき `service.py` は observer も sink も起こさず、取引 event は一切出ない。これを止めることが「emitter を止める」と同値で、rollback の手段になる (§10.3)。`improve` 節と同じ流儀の optional key で、`config/settings.yaml.example` と同期する。
3. observer は `(row, from_status, to_status)` から §3 の event を作り、sink の queue に非同期で渡す。作る関数は純粋で、DB を読むのは `missions.trigger` の 1 回だけ。**observer は flock もファイル I/O も一切しない。** transition は core_lock を保持したまま observer を呼ぶので、state ファイルの共有 flock を取る `StateStore.load()` を呼ぶと、state の writer が lock を持つ間は注文遷移全体が無期限に待つ。`state_mode` は service が**起動時に 1 回** state から読んだ値をメモリに持ち、それを observer に渡す (読めなかったときは None)。現物では稼働中に mode を変える経路は無い。将来 mode を変える経路ができたら、永続化が成功した後に同じメモリの値を更新する。`transition` の引数は増やさない (呼び出しが 25 か所以上あり、バックテストにも波及するため)。
4. observer の中で起きた例外は握りつぶして warning を 1 行出し、`observer_error` に数える。transition の戻り値と DB の状態は変わらない。
5. sink は専用の接続と書き込み thread を 1 本持つ。接続は `connect(db)` の後に **`PRAGMA busy_timeout=0`** を設定する。1 件ごとに、INSERT とその commit だけを行う短い tx にする (他の文を挟まない)。INSERT が `SQLITE_BUSY` / `SQLITE_LOCKED` の場合に限り、sink thread 内で短い backoff (5 ms 開始、指数で 50 ms 上限、±20% jitter) を挟み、単調時計で最大 1 秒まで再試行する。停止要求の後も join の期限 (項 8) までは再試行を続け、期限で放棄した 1 件は `shutdown_drop` に数える。期限を超えたら event を捨てて `db_busy_drop` に数える。observer と queue 投入は待たない。busy は INSERT より前に起きるので、再試行で重複は生じない。その他の DB の失敗は **再試行せず捨てる** (`db_error_drop`)。
6. queue は有限 (1000)。溢れたら捨てて `queue_drop` に数える (注文処理を待たせない)。
7. 欠落の記録:
   - 理由別の counter: `queue_drop`、`observer_error`、`db_busy_drop`、`db_error_drop`、`invalid_value_drop` (§3.3 で作らなかった event)、`worker_death`、`shutdown_drop`。
   - **一次の観測手段**は、(1) 上の counter、(2) drop のたびの warning ログ (理由別、スロットル付き)、(3) プロセス内の health 状態 (`trade_event_sink` 用の component 状態) の 3 つである。sink 自身が壊れている (worker 死亡、DB の失敗や busy) ときに、同じ経路で health event を確実に保存することはできないので、event に頼らない。
   - `afx ctl status` に counter の件数だけを載せる (7 種の理由別の件数。理由の文言や event の中身は載せない)。このため status の応答と formatter (`ops/client.py`) の変更を §10.1 と AC-15b に加える。新しい画面は作らない。
   - `service_health_changed` (`component_code=trade_event_sink`、`health_code=degraded` / `ok`) は**補助通知**である。保存できる状態になったときに、状態が変わった分だけ 1 回出す。失敗時に必ず出るとは約束しない (出なくても欠落は counter とログで見える)。既存の `event_stream_health` の field 集合は変えない。
   - **回復の規則。** 直近 60 秒 (単調時計) に drop が 1 件も無ければ `ok` とみなす。再 degraded と ok の通知は、状態が変わったときだけ 1 回出し、drop が続いている間に繰り返さない。
   - この component 状態は、既存の `HealthLatch` (process の生存中は回復しない) とは**別物**として持つ。`HealthLatch` を流用せず、既存の latch の意味は変えない。
   - 既存の `service_health_changed` は schema にあるが emit 箇所が無かった (§11)。本件はこの code を使う最初の emit 箇所になる。
8. worker は daemon thread 任せにせず、service が所有する資源とする。停止時は queue を排出し、最大 5 秒で join する。排出が終わらなければ残りを `shutdown_drop` に数えて停止を続ける。期限の時点で worker が書込み中の 1 件は保存を確かめられないので `shutdown_drop` に数え、worker 側と二重には数えない。直後に commit が終わっていることがあるため、`shutdown_drop` は「保存を確認できなかった件数」の上限であり、過少には数えない。worker が死んだら `worker_death` を立て、以降の event は `queue_drop` に数える (自動再起動はしない)。

core_lock を保持したまま別接続に INSERT すると待ち得る。sink を別 thread にするのは、`defer_notifications` が lock の外で送る理由 (`executor.py:240-283`) と同じ考えである。

### 4.3 呼び出し側に足す案を採らない理由

`_retry_close` は `_finish_close` を呼ばず自前で CLOSED にする (調査 §2.3)。EXPIRED は scheduler、CANCELLED は executor と reconcile、REJECTED は 3 か所にある。
遷移の呼び出しは 25 か所以上あり、将来 1 か所足されるたびに取りこぼす。status を書く関数が 1 つだけなので、そこを唯一の観測点にする。

### 4.4 版ずれ

未知の code は notify と bot の両方で `payload_invalid` になり、同じ event で止まり続ける (調査 §3.1、fail-closed の設計)。
- 古い daemon + 新しい afx-ops: 新 event がまだ無いので止まらない。
- 新しい daemon + 古い afx-ops: 最初の新 event で notify と bot が止まる。
対策は配備の順序と実プロセスの版確認で行う (§10.3)。daemon 側の版検知は作らない。

### 4.5 残余リスク (受容するもの)

- **同一 DB の完全非干渉は保証できない。** `orders` と `ops_events` は同じ SQLite (WAL) で、書き込み tx は 1 本しか持てない。sink は `busy_timeout=0` なので SQLite の中で lock を待たず、INSERT が busy なら rollback して lock を放してから、sink thread 内で backoff (最大 1 秒) して再試行する。再試行中の sink は write lock を保持しないが、lock 解放後に sink が獲得する頻度が増えるため、core commit の待機頻度と tail latency は増え得る。一方 core の書き込みは `busy_timeout=5000` のままなので、sink が INSERT 後・commit 前の短い間に lock を持っている間だけ、core がその時間だけ待ち得る。待ちの上限は sink の「INSERT + commit」1 件の所要時間で、5 秒に達するのは sink の commit が 5 秒止まったときだけである。再試行あり (1 秒) / なし (0 秒) を同じ人工負荷 (遷移 150 回、各遷移の直後に core が 2 ms の小 commit を 6 回続ける、遷移の間隔 2 ms / 20 ms。および queue に 150 件を先に積み core が間を置かず 900 回 commit する「sink が次の event でも再獲得する」固定 fixture) で比べた A/B では、core の commit 所要時間は p50 約 2.07 ms、p99.9 は最大 3.2 ms、最大 3.2 ms で再試行の有無による tail の悪化は見られず (固定 2 ms の保持時間を含む)、event の保存率は再試行なしで 0〜2% に対し、あり (1 秒) で 99.3〜100% だった。実運用相当の tick 密度、1 秒を超える writer、queue の長時間滞留は未検証の残余リスクである。queue 容量 1000 は「ピーク取引 event / 秒 × 1 秒 + 通常 backlog」を満たす (遷移 150 件の burst で最大深度 150、再試行 1 秒の head-of-line でも超過分は `queue_drop` に数える)。**「sink が先に write lock を持つと core が待つ」ことは受容する残余リスクであり、受入条件にはしない** (同一 SQLite の writer lock 下では満たせないため。AC-14 は sink が待たないことと、observer / queue 投入が sink を待たないことに限る)。完全非干渉が必要になったら event 専用 DB に分離する (§11)。
- **欠落は回復できない。** process crash、queue の中身、observer の登録前の遷移、sink の drop は、orders の最終状態からは分かるが、placed → filled などの遷移履歴は復元できない。既存の同期的な `emit_best_effort` より欠落の窓は広い。counter と warning ログと health 状態 (§4.2) で気づけるようにするが、crash 時の queue 内の event は数えられない。正は orders 表である。
- **配送は at-least-once。** 投稿の成功後・cursor 保存前に bot / notify が落ちると、同じ event を再投稿する。webhook は `event_id` で受信側が重複を排除できる。Discord の文面には安定した識別子が無く、重複して見える (許容する)。

## 5. bot の文面と既定

### 5.1 文面

固定 template に、検査済みの値だけを埋める (承認 spec §6.4)。event から自由文は復元しない。先頭に `[ペーパー]` (mode が live のときは `[実発注]`) を付け、`state_mode` が `learning` のときは続けて `学習中` の 1 語を付ける (None のときは付けない)。
side は long → 買い、short → 売り。数値は §3.3 の表示規則に従う (桁区切りと符号は付けるが、桁は足さない)。`origin_code` は「起点」と明示して 1 行で表示し、None のときはその行を省く (戦略の帰属ではなく Mission の起動元であることを字面で示す。event と webhook が既に持つ値なので、Discord だけ隠しても公開範囲は変わらない)。

発注 (state=placed):

```
[ペーパー] 学習中 指値を入れました #12 USDJPY 買い
数量 0.1 lot / 価格 150.123
SL 149.8 / TP 150.9
起点 signal:rsi_div
```

約定 (state=filled) は「指値を入れました」を「約定しました」に、末尾の行は同じ形にする。

以下の例は `trade_amounts=absolute` (既定。数量と損益の絶対額を出す) の形。`relative` のときの形はこの節の末尾に置く。

決済 (position_closed):

```
[ペーパー] 決済しました #12 USDJPY 買い (利確到達)
決済価格 150.9 (約定 150.123) / 数量 0.1 lot
損益 +7,770 JPY (+2.40R) / 保有 3 時間 12 分
起点 signal:rsi_div
```

拒否・取消・失効は 1 行 (`[ペーパー] 指値を取り消しました #12 USDJPY 買い 0.1 lot @150.123 (市場クローズ前の取消)`)。

理由の表示: sl = 損切り到達、tp = 利確到達、day_rollover = 日次の強制決済、llm_close = 判断による決済、llm_cancel = 判断による取消、market_close = 市場クローズ前の取消、reservation = 予約を維持できない、rate_unavailable = 換算レート不明、account_unknown = 口座情報なし、equity_nonpositive = 残高が正でない。

pnl が None のときは「損益 未確定」と書く。R が None のときは R を省く。

`trade_amounts=relative` のときの表示形 (bot 用 `ops.toml` の `trade_amounts` で切り替える。既定は `absolute`。§5.3)。数量と損益の絶対額 (`qty`、`pnl`、`ccy`) は Discord の文面に出さない。発注・約定・取消などの行から数量の語を省く (価格、SL / TP は出す)。決済は次の形にする。損益は R 倍数と価格の変化率で示し、変化率は表示側が `entry_price` と `close_price` と `side` から Decimal で計算した (決済価格 − 約定価格) × 方向 ÷ 約定価格 の百分率を、符号つき 2 桁で出す (例 `+0.52%`)。R が None のときは R を省いて変化率だけを出す。変化率は価格から必ず計算できるので「未確定」にはならない。

```
[ペーパー] 決済しました #12 USDJPY 買い (利確到達)
決済価格 150.9 (約定 150.123)
損益 +2.40R (価格 +0.52%) / 保有 3 時間 12 分
起点 signal:rsi_div
```

`relative` でも event と webhook には `qty` と `pnl` が残る (event の形は変えない)。Discord の文面だけを絞る設定で、webhook は notify 用 `ops.toml` の `deliver_trade_events` (§5.3) で別に決める。未知の `reason_code` は固定の汎用文にする (生の値を写さない)。未知の **event code** は文面を作らず、`payload_invalid` として止める (§5.2)。

### 5.2 無害化と処理の 2 値化

承認 spec §6.4 と同じ部品を使う。bot と notify の event 処理は、次の 2 段にする。

1. **完全検証。** event code が既知か、field 集合の完全一致、各値の grammar と範囲と canonical (§3.3)、`ref` の形と範囲 (§3.4)、を全て検査する。1 つでも通らなければ **event 全体を投稿せず**、cursor を進めず止まる (従来どおりの `payload_invalid`。項目だけを省く扱いは採らない)。
2. **結果の 2 値の型。** 完全検証を通った event に対して、bot と notify に共通の判別型 `POST(payload)` / `SKIP_KNOWN(event_id)` を返す。`POST` (投稿する) は成功の後に cursor と high watermark を snapshot の 1 回の置換で進める。`SKIP_KNOWN` (既知だが設定で出さない) は配送せず、同じ snapshot の更新で cursor と high watermark を atomic に進める。bot には `commit_skip`、notify には配送なしの `advance_delivery` 相当を設ける。
   - 現物の bot の `plan_event()` は `EventPlan | None` で、`None` は「cursor 以下」と「設定で出さない」を区別できず、`plan is None` は `continue` になる (cursor が進まない)。**`plan is None` を設定 filter に再利用しない**。filter は完全検証の後段に置き、上の型で表す (AC-23)。

Discord の本文に入る非信頼の値 (`origin_code`、`pair`、reason の表示など) は、次の 2 点を満たす。(a) Markdown を escape し、値そのものから mention を発生させない (`@everyone`、`<@id>` 等が mention にならない。`AllowedMentions` で防ぐ)。(b) 利用者が設定済みの `mention_role_id` / `mention_user_ids` は、取引通知には**付けない** (§8.1 問 5 の決定)。取引通知は mention の対象外にし、専用の key は足さない。既存の承認通知の mention の扱いは変えない。最大でも 8 行程度で 2000 字を超えない。

### 5.3 既定と設定

既定は**全部出す**。ユーザーが要望した通知であり、1 pair・1 時間足では件数が少ない (1 日に数件)。T0 (初回 cursor、§8.3) は配備前提なので、T0 の前に取引 event を出荷することはない (暫定既定は置かない)。

出す種類を絞る設定は、bot 用 `ops.toml` の **optional な平坦 key `trade_notices`** とする。既定は `all` (§8.1 問 4 の決定)。(現物の `ops.toml` は feature ごとの平坦な exact schema で、「通知節」は存在しない。`config.py:23-29`、`225-263`)。

- `all` (既定): 発注 event の全 state と決済。
- `results`: filled、rejected、決済だけ (placed / cancelled / expired を `SKIP_KNOWN` にする)。拒否の理由が読めることを保つ絞り方。
- `fills_closes`: filled と決済だけ (placed / rejected / cancelled / expired を `SKIP_KNOWN` にする)。
- `off`: 取引 event を全て `SKIP_KNOWN` にする。

Discord の文面の数値表示は、bot 用 `ops.toml` の **optional な平坦 key `trade_amounts`** で切り替える。値は `absolute` (既定。数量と損益の絶対額を出す。§5.1 の template) と `relative` (R 倍数と価格の変化率だけを出す。§5.1 末尾の形) の 2 つで、`trade_notices` とは独立に働く。event と webhook の中身は変わらない。

notify (webhook) の取引 event は、notify 用 `ops.toml` の **optional な平坦 key `deliver_trade_events`** (真偽、**既定 false**) で opt-in にする。false のとき、完全検証を通った取引 event 2 種は `SKIP_KNOWN` として配送せずに cursor と high watermark を進める (§5.2)。true のときは、既存の承認・改善・health の event と同じ形で配送する。理由は、取引 event には数量・価格・損益が入り、webhook の受け手 (自前の受信口か外部サービスか) が利用者にしか分からないため。daemon の settings の `notify.trade_events` (emitter の enable flag、既定 true、§4.2、§10.3) とは名前を変えて区別する。

key が無い旧 `ops.toml` では既定値で起動できる (bot は `trade_notices=all`、`trade_amounts=absolute`、notify は `trade_events=false`)。不正な値は起動時に拒否する。`OpsConfig`、writer、probe、隔離 plan、example、`trade_notices` を同時に更新する (3 つの key は同じ箇所に足す)。設定は隔離の前に検査済みの fd から読み、値だけを隔離後の runtime に渡す (隔離後に再読しない)。

承認通知 (承認の中身 T3) の設定との合成規則:

| trade_notices | 承認通知の設定 | 取引 event | 承認の通知 |
|---|---|---|---|
| 無し (既定 = `all`) | 既定 | 全部投稿 | 承認側の既定どおり |
| `results` | 任意 | filled / rejected / 決済だけ投稿 | 承認側の設定だけで決まる (影響しない) |
| `fills_closes` | 任意 | filled / 決済だけ投稿 | 承認側の設定だけで決まる (影響しない) |
| `off` | 任意 | 全て `SKIP_KNOWN` | 承認側の設定だけで決まる (影響しない) |
| 不正値 | 任意 | 起動を拒否 | 起動を拒否 |

`trade_amounts` は文面の形だけを変え、投稿される種類と cursor には影響しない。notify の `deliver_trade_events` は notify の配送だけを決め、bot の設定とは独立である。承認通知の設定と `trade_notices` は独立で、一方が他方の cursor の進め方を変えない (どちらも §5.2 の 2 値で進める)。承認通知側の key 名と既定は承認 spec の T3 に従う。

## 6. web・CLI

- web: 新しい画面も route も作らない。活動ページは `activity.log` の末尾を出すだけで、既存の `order_opened` / `order_closed` 行が載る (調査 §3.3)。activity の文面は変えない。
- `afx ctl events` は新 code をそのまま表示する (`ops/client.py:806-811`)。
- `afx ctl status` には sink の counter の件数 (7 種の理由別) だけを足す (§4.2、AC-15b、§10.1)。それ以外の status の表示は変えない。

## 7. 遮断と秘密

### 7.1 絶対制約

- 発注・SL 変更・クローズは LLM に委ねない。observer と sink は読み取りだけで、event から操作は発生しない。
- event に鍵・口座 ID・ホスト名・URL・自由文 (`reasoning`、`reject_reason`、plugin のコード・説明、例外文) を入れない。値は §3 の列挙と数値書式だけで、検査の grammar が自由文を通さない。
- 改善 agent に戻る経路を新設しない。

### 7.2 agent への経路

- `ops_events` を読む src は `ops/events.py` と `store/db.py` の DDL だけで、取引判断・改善・reflection の tool に event 読み取りは無い (調査 §4)。この設計はそれを変えない。
- sink の書き込み先は `ops_events` だけで、`improvement_backlog`、`last_result`、`policy`、prompt の材料には書かない。
- **既存の事実:** 実取引の損益は orders 表から既に LLM に渡っている (調査 §4)。経路は 3 本ある。
  - 改善 Mission: 90 日の勝率・PF・pair 別・時間帯別 (`loops/improve_context.py:27-70`)。
  - 取引判断 Mission: 直近 10 件の損益と close_reason (`loops/summary.py:82-93`)。
  - reflection: closed order の価格・PnL・close reason と元 intent の reasoning (`loops/reflection_cycle.py:148-157`)。改善 agent そのものではないが、LLM へ戻る既存経路である。
- 本 C0 はこれらを変えない。実取引の損益を改善 agent に見せるか否かは別チケット (`strategy-attribution-and-shadow-evaluation`、`improve-objective-filtered-signal-quality`) の論点である。
- pin の形 (AC-17、AC-18):
  - 固定 fixture・固定時刻・固定 settings で、`_performance_report` の canonical JSON、`build_state_summary`、reflection prompt を UTF-8 byte 完全一致で golden と比較する。
  - registry の exact-set: improve の registry の名前集合と `_TRADE_TOOLS` の集合が既存の exact-set test (`tests/test_service_wiring_pins.py:123-131`、`tests/tools/test_mission_registry.py:224-247`) と一致し、`events.read` や `ops_events` の reader が加わっていない。
  - grep test は補助にとどめる。

### 7.3 公開される情報

event には pair、数量、価格、SL / TP、損益が入り、口座の規模と成績が bot の channel の閲覧者と notify の受け手に見える。
channel と受け手が誰かはリポジトリから確認できず、利用者しか決められない。そのため bot の絶対額の表示は `trade_amounts` (既定 `absolute`、`relative` にすると Discord の文面から数量と絶対額が消える)、notify の配送は `deliver_trade_events` (既定 false の opt-in) を利用者の設定にした (§5.3、§8.1 問 2、問 3)。`relative` でも event には `qty` と `pnl` が残り、`deliver_trade_events=true` の webhook にもそのまま流れる。

## 8. 裁定の記録

### 8.1 ユーザー裁定の記録

5 つの問いは 2026-10-20 にユーザーが裁定した (問 3・4 は第二意見 (codex sol) を踏まえた)。以下は決定の記録で、本文はこの決定に従う。

| # | 問い | 決定 | 日付 | 根拠 |
|---|---|---|---|---|
| 1 | bot / notify を初めて起動したとき、過去の取引 event をどこから流すか | **(a) 今から。** 初回 (snapshot が無い、または `event_cursor_initialized` が false) は、最初の正常な `/events` 応答の high watermark を cursor にして、その page は配送しない。過去は流さない。bot の v4 `{0,0}` snapshot は false として migrate する。T0 として本 C0 に含める (§8.3) | 2026-10-20 | 流した履歴は取り消せず、channel の閲覧者に口座の成績が見える。現物は snapshot 無しで cursor=0 から始めて全履歴を投稿する (`gate/state.py:100-102`、`notify/state.py:33-35`)。sol も同意 |
| 2 | 数量と損益の絶対額を Discord に出すか | **設定で切り替える。** bot 用 `ops.toml` の optional key `trade_amounts` (`absolute` / `relative`)、**既定 `absolute`** (数量と損益の絶対額を出す)。`relative` は R 倍数と価格の変化率 % だけ (§5.1 末尾の形、pips は使わない)。event と webhook の形は変えない | 2026-10-20 | ユーザーが「決済情報」を要望しており損益額が主眼なので、既定は絶対額を出す。一方 sol は、絶対額は口座規模を推測させるので `relative` を既定にする案と、共有 channel の可能性が不明なら安全側に倒す案を挙げた。分岐を設定にして、利用者が共有 channel のときに絞れるようにした |
| 3 | notify (webhook) に取引 event を流すか | **既定 OFF の opt-in。** notify 用 `ops.toml` の optional key `deliver_trade_events` (真偽、既定 false)。OFF のときは `SKIP_KNOWN` で cursor を進める (配送しない) | 2026-10-20 | 当初案は「流す」だったが、sol が、金額を含む新 event の追加は既存 webhook の情報公開範囲を黙って拡大する、と指摘した。受け手が自前か外部サービスかは利用者しか知らないので、安全側の既定 OFF にして opt-in にした。問 2 と対で、bot で絶対額を隠しても webhook に流せば公開は防げない |
| 4 | bot の既定でどこまで出すか | **(a) 全部が既定。** `trade_notices` の既定は `all`。選択肢は `all` / `results` (filled / rejected / 決済) / `fills_closes` / `off` | 2026-10-20 | 1 pair・1 時間足で件数が少ない見込み (1 日に数件、保証値ではない)。sol は「拒否だけは知らせる」既定として結果系だけにする案を挙げたため、その絞り方を選択肢 `results` として足した。ユーザーは `all` を既定に選んだ |
| 5 | 設定済みの mention を取引通知にも付けるか | **(b) 付けない。** 取引通知には `mention_role_id` / `mention_user_ids` を付けず、専用の key も足さない。承認通知の mention の扱いは変えない | 2026-10-20 | 承認通知は人の行動が要るので呼び出しが妥当だが、取引通知は定常的に流れ、毎回 ping するのは安全な既定ではない。sol も同意。非信頼値から mention を出さないこと (§5.2 (a)) は技術判断で、この問いとは別 |

### 8.2 既に決めた確認事項 (問いにしない)

次は設計で決めた事項であり、覆したいときだけ指摘する。

- **event 名と field:** brief の推奨から変えた点がある。
  - `strategy_code` は `origin_code` にした (戦略の帰属が orders に無いため、§2.2)。
  - `manual` と `killswitch` の決済 reason は入れない (経路が存在しない、調査 §2.7)。
  - `ccy` を足した (bot が口座通貨を知らないため)。
  - `state_mode` を足した (learning 中にも paper 発注が起きるため、§2.5)。
  - 発注 event の `reason_code` は取消の理由を持つ。
- **mode の欄:** 現状 `paper` 固定。実発注の broker が入るときに broker の型から決める。`state_mode` (learning / trading) とは別物。
- **float を使わない:** 既存の検証を変えず、数値は field ごとの grammar の文字列にする。pnl を int にしない (afx-ops が 0 以上だけを通すため)。afx-ops は parse → 再生成 → 完全一致の canonical を要求する (§3.3)。
- **`origin_code` の表示:** 「起点」と明示して表示し、None なら行を省く。event と webhook が既に持つ値なので、Discord だけ隠しても公開範囲は変わらない (技術・表示の判断)。
- **`state_mode` の読み方:** service が起動時に 1 回読んだ値をメモリで持つ。observer は flock もファイル I/O もしない (§4.2)。
- **sink の観測:** 一次は counter、warning ログ、プロセス内の health 状態。`service_health_changed` は補助 (§4.2)。
- **rollback:** daemon の emitter (`notify.trade_events`) を止めてから afx-ops を戻す。`trade_notices` は表示の絞り込みで、rollback の手段ではない (§10.3)。
- **R の定義:** 価格ベースで commission とスプレッドを含めない。行だけから計算でき、換算レート待ちにならない。
- **唯一の観測点:** `transitions.transition`。呼び出し側に足す案は取りこぼす (§4.3)。登録は接続ごとで、登録の呼び出しは service の 1 か所だけ。
- **sink を別 thread にし、待たずに捨てる:** core_lock の中で別接続へ INSERT しない。sink は `busy_timeout=0`、queue が溢れたら捨てる。busy だけは sink thread 内で最大 1 秒再試行し、その他の DB の失敗は再試行しない。
- **クラッシュ時の取りこぼし:** queue の中身は永続化しない。生成は best-effort という既存の契約の範囲 (ops-api spec §4.3)。ただし drop は理由別に数え、health で見える (§4.2)。正は orders 表。
- **不正な event は全体を投稿しない:** 既存の fail-closed に合わせる (§5.2)。
- **版ずれの対策:** afx-ops を先に配って実プロセスの版を確認する運用で足りる (§10.3)。daemon 側の版検知は作らない。
- **発注 event と既存の daemon 直送:** 直送は異常系 4 種だけで、重複しない。正常系を直送に足す案は、bot の cursor・設定・日本語 template を使えず、2 つの経路で文面が食い違うので採らない。

### 8.3 配備前提 T0 (初回 cursor)

チケット `ops-bot-initial-start-replays-all-events` を本 C0 の T0 に昇格し、設計を含める。

現物では、bot も notify も snapshot が無いと `cursor=0, high_watermark=0` の空状態に潰され、「snapshot が無かった」事実が残らない。さらに bot は最初の event poll より前に snapshot を保存する経路を 2 つ持つ。起動順は prepare → login → `reconcile_startup()` → poller で、`_load_and_invalidate` の保存と、`reconcile_startup()` の `commit_plan()` → `_save()` (空状態でも無条件に保存する) の両方が初回 poll より前に走る。「保存を 1 か所やめる」設計では、初回 poll が失敗したあとの再起動で snapshot が既にあり、fresh が失われて全履歴を再生する。

そこで **snapshot の存在と event cursor の初期化を分離する。**

- **明示状態 `event_cursor_initialized`。** bot の snapshot に `event_cursor_initialized` (真偽) を持たせる。`false` の間は cursor と high watermark の値に意味が無く、event を一切配送しない。snapshot の他の保存 (`reconcile_startup()` の `_save()`、`_load_and_invalidate` の保存、承認の更新) はこの flag を変えない。flag を `true` にするのは次の保存だけである。
- **初回の扱い。** `false` の状態で最初の `/events` 成功応答を受けたとき、`cursor = high_watermark` と `event_cursor_initialized = true` を**同じ snapshot の 1 回の置換 (atomic)**で保存し、**その page は配送しない**。以降は通常どおり、新しい event だけを読む。
- **snapshot が無いとき。** 新規作成の snapshot は `event_cursor_initialized = false` で始める。保存済みの `cursor=0` と「不在」は flag で区別でき、同一視しない。
- **schema の版上げ。** bot の snapshot は schema v4 → v5 (`event_cursor_initialized` を加える)。既存の規律どおり、schema_version と migration、旧版 fixture、byte 一致 test を持つ。v4 の snapshot の migration 方針: `cursor > 0` または `high_watermark > 0` の snapshot は配送実績があるので `true` として migrate する (続きから、履歴を再生しない)。**`cursor=0` かつ `high_watermark=0` の v4 snapshot は `false` として migrate する** (現物の v4 は初回 poll 前にも保存するので、この形は「まだ一度も正常な page を受けていない」と区別できず、`true` にすると初回 v5 poll で全履歴が流れる。`false` にすると停止中に届いた event は配送されないが、その bot はまだ何も配送していないので利用者が見た通知との連続性は失われない)。AC-25 に v4 `{0,0}` → v5 の upgrade fixture (全履歴が流れないこと) を含める。snapshot の loader は版不一致を state 層の前で拒否するので、旧版の明示的な読み取り経路 (v4 / v2 を読んで migrate する関数) を持つ。
- **notify も同じ flag。** notify の snapshot (`notify/state.py`) も同じ名前の flag を持つ。schema v2 → v3、v2 は `true` として migrate、旧 fixture と byte 一致 test は bot と同じ。notify は現物では最初の `client.events()` の前に保存しないが、同じ表現にそろえて、将来の保存追加で fresh が失われないようにする。
- **「開始時点」の定義** = 最初の正常な poll の時点。daemon に届かない間は `false` のまま、event を配送せず、届くまで待つ。
- 変更範囲は state (`gate/state.py`、`notify/state.py`) に加えて `bot/adapter.py` と `notify/runtime.py`。bot の `bot/runtime.py` は起動順を変えない (flag で守るので、順序の入れ替えは不要)。
- 初回は「今から」とする (§8.1 問 1 の決定)。過去の event を流す経路は持たない。
- 検証: bot と notify のそれぞれで、(a) **混在履歴** (approve / improve / health / 取引 event を含む既存の履歴) がある状態で snapshot 無しで起動 → 何も投稿せず、cursor が high watermark に置かれ、flag が `true` になる、(b) 起動後の新 event は投稿する、(c) 旧版 (bot v4、notify v2) の snapshot からの再起動は、配送実績のあるものは `true` として migrate されて続きから、bot v4 の `cursor=0` かつ `high_watermark=0` は `false` として migrate され履歴が流れない、(d) 最初の poll が失敗しても `false` のまま残る、(e) bot で **`reconcile_startup()` が snapshot を保存した後、最初の `/events` が失敗し、プロセスを再起動し、次の成功で末尾から始まり、履歴は流れない**、の fixture を持つ。取引 event だけの fixture では既存チケットの全体を覆えない。
- これで `ops-bot-initial-start-replays-all-events` を閉じられる内容にする (AC-25)。
- T0 の完了前に取引 event を有効にしない (daemon の emitter を有効化する手順は T0 と T1 の配備確認の後)。

## 9. 受入条件

### 9.1 event の形

- AC-1: `FIELDS` に 2 code が入り、§3.1 (12 field) / §3.2 (13 field) の field 集合と完全一致する。足りない field、余分な field、自由文を含む値、float は `EventSchemaError` で拒否される。`ref` は `order:<数字 1〜19 桁>` の regex を通り、整数化して `1 <= id <= 2^63-1` のものだけを通す (`order:0`、`order:9223372036854775807` (上限、通る)、`order:9223372036854775808` (上限+1、拒否) の fixture を含む)。
- AC-2: 数値書式 (§3.3) の境界 fixture: 価格 5 桁・3 桁、数量 0.01、pnl の負値、`-0.00` の正規化、書式化後 64 字超になる巨大な有限値、nan、inf、分母 0 の R、grammar を外れる値。canonical の fixture: 8 小数の丸めで FX の必要精度 (JPY 絡みの 3 桁、その他の 5 桁、pipette、lot 0.01) が保たれること、`0 < x < 0.5×10^-8` が書式化後に `"0"` になって拒否されること。producer は、nan・inf・長さ超過・grammar 外では **event を作らず** `invalid_value_drop` に数える。元データが NULL の項目だけが None になる。
- AC-3: `ops/events.py` の `FIELDS` / `_REF` / 数値 grammar と afx-ops の `ALLOWED_FIELDS` / `_REF` / 数値 grammar が、2 つの package にまたがる test で完全一致する。片方だけ足すと赤になる。afx-ops の `encode_event` は実 daemon が出す形の匿名化 fixture (pnl が負の文字列、None の field を含む) を通す。afx-ops は `pnl="abc"`、`pnl="1e999"`、範囲外、`-0.00` の event を全体として拒否する。さらに非 canonical (`0001.20`、`0.10000000`、`+1.5`、`1.50` の価格など、grammar には合うが parse → 再生成で元と一致しない値) も全体として拒否する (producer と同じ formatter での再生成一致)。

### 9.2 emit

- AC-4: 成行の発注 → 約定 → SL 到達の決済で、`order_state_changed` (filled) が 1 件、`position_closed` (reason=sl) が 1 件出て、id が単調に増える。placed は出ない。
- AC-5: 指値の発注で placed、約定で filled、決済で `position_closed`。指値の取消で cancelled (reason_code つき)、期限切れで expired が出る。
- AC-6: CLOSED の 2 つの確定点 (`_finish_close` 経由、`_retry_close` 経由) の両方で `position_closed` が 1 件ずつ出る。`_retry_close` の fixture を実際に CLOSING で滞留させて作る。
- AC-7: 取消と約定の競合 (`CANCELLING` → `PROTECTION_PENDING` → `OPEN`) で filled が 1 件だけ。reconcile の解決 (`SUBMIT_UNKNOWN` → `REJECTED`、`CANCEL_UNKNOWN` → `CANCELLED`) でも出る。
- AC-8: `realized_pnl` が NULL の決済でも `position_closed` は出て、`pnl` は None。
- AC-9: gate 拒否と HOLD は event を出さない。不正な遷移 (2 回目) は event を増やさない。
- AC-10: バックテスト runner の Executor (observer 未登録) は `ops_events` に行を増やさない。`transitions.observe` を呼ぶ箇所が `service.py` の 1 か所だけであることを静的に pin する。重複登録は拒否される。unobserve 後は event が出ない。登録に失敗したときは daemon が起動しない。
- AC-10b: `learning` と `trading` のそれぞれで paper 発注を作り、両 event の `state_mode` がそれぞれの値になる。起動時に state が読めなかったときは None。**observer は flock もファイル I/O もしない**: observer の実行中に `open` / `flock` / `StateStore.load` が呼ばれると赤になる pin を持ち (state の writer が lock を保持している間も transition が待たない fixture を含む)、`state_mode` が service の起動時にメモリへ読んだ値だけで決まることを確かめる。observer の中に `state.load()` を足す変異で赤になる。

### 9.3 安全

- AC-11: observer が例外を投げても、transition の戻り値、orders の行、続く SL/TP 監視の処理が変わらず、`observer_error` が増える (observer を例外を投げるものに差し替える変異で確認)。
- AC-12: sink の INSERT が失敗しても (`ops_events` を壊した接続で)、注文処理は失敗せず、`db_error_drop` が増え、event は再試行されず、プロセス内の health 状態が degraded になる。
- AC-13: sink の queue が溢れても transition は待たされず、`queue_drop` が増える (queue を 1 にして 2 件目を投入し、時間で pin せず挙動で確かめる)。
- AC-14: 別接続が write lock を保持している状態で、(a) 別接続が write lock を保持していても observer / queue 投入は直ちに戻り、sink は `busy_timeout=0` のまま worker 内だけで最大 1 秒再試行する。期限内に解放されれば event が正確に 1 件保存され、期限超過なら `db_busy_drop` が 1 増え、重複しないことを確認する。(b) observer と queue への投入は sink の完了を待たない (sink を止めても transition が戻る)。sink の接続が `busy_timeout=0` であること自体を pin し、5000 に戻す変異で赤になる。INSERT と commit の間に他の文を挟む変異でも赤になる。**「sink が先に write lock を持つと core が待つ」は AC にしない**: §4.5 の残余リスクとして受容し、T2 で INSERT + commit の所要時間を実測して記録する。
- AC-15: 停止時に queue の残りが `ops_events` に書かれる。排出が 5 秒で終わらないときは残りを `shutdown_drop` に数えて停止が完了する。worker を殺した場合は `worker_death` が立つ。
- AC-15b: 各 drop の理由が counter (7 種) と warning ログに出て、プロセス内の health 状態が degraded になる。直近 60 秒 (単調時計。fake clock で pin) に drop が無ければ ok に戻り、再 degraded / ok の通知は状態が変わったときだけ 1 回出る (drop が続く間に繰り返さない)。`service_health_changed` (`component_code=trade_event_sink`) は保存できたときの補助通知で、保存できない状況で出ることを要求しない。`afx ctl status` に counter の件数が載る (status の応答と formatter の fixture)。この component 状態は既存の `HealthLatch` と別で、`HealthLatch` の挙動は変わらない。event stream の既存の `event_stream_health` の field は変わらない。

### 9.4 遮断と秘密

- AC-16: canary: intent の `reasoning` / 理由欄に鍵風の文字列・URL・ホスト名・口座風の数字列を入れても、生成された event のどの値にも現れない。
- AC-17: 取引判断・改善・reflection の tool registry に event 読み取りが無い。improve の registry の名前集合と `_TRADE_TOOLS` が exact-set で一致し、`events.read` と `ops_events` の reader が加わると赤になる。`ops_events` を読む src が `ops/events.py`、`ops/service.py`、`store/db.py` に限られる (grep ベースの test は補助)。
- AC-18: 固定 fixture・時刻・settings で、`_performance_report` の canonical JSON、`build_state_summary`、reflection prompt が、本変更の前後で UTF-8 byte 完全一致する (golden)。
- AC-19: sink と observer が書く表は `ops_events` だけで、登録あり / なしで orders の全列が同一。

### 9.5 bot と notify

- AC-20: 5 つの発注 state と決済のそれぞれが、§5.1 の template どおりの文面になる (golden 比較)。`[ペーパー]` / `[実発注]` の接頭辞は `mode` で、`学習中` は `state_mode` で切り替わる。`origin_code` は「起点」と明示して表示され、None のときは行が省かれる。設定済みの mention は取引通知に付かず (§8.1 問 5)、本文・投稿の `allowed_mentions` が一致する。`trade_amounts` が `absolute` (既定) のときは §5.1 の絶対額の template になり、`relative` のときは `qty` と `pnl` が文面のどこにも現れず、変化率が Decimal で計算される (符号、2 桁、R None で R を省く、`pnl` None でも欠けない fixture)。key が無い旧 `ops.toml` では `absolute` になる。pnl None、R None、origin None、state_mode None、未知の reason_code、JPY の整数表示と他通貨の 2 桁表示の各 fixture。
- AC-21: 値の検査に通らない event、未知の event code は投稿されず、cursor を進めず止まる (既存の `payload_invalid` の扱い)。項目だけを省いた投稿は無い。
- AC-22: 敵対 fixture: `origin_code` などの非信頼値に Markdown・mention・改行風の文字を入れても (grammar を通る範囲と、通らず拒否される範囲の両方)、escape されて値そのものから mention が発生しない (`AllowedMentions` で pin)。DB の `missions.trigger` が grammar 外 (`signal:` 以外の 64 字以内の文字列など) のとき、producer は `origin_code` を None にする。
- AC-23: 設定 `all` / `results` / `fills_closes` / `off` のそれぞれで投稿される種類が §5.3 の一覧どおり。`trade_amounts` の不正な値も起動時に拒否する。notify の `deliver_trade_events` は key が無い、または false のとき取引 event 2 種が配送されず `SKIP_KNOWN` で cursor と high watermark が進み (既存の承認・改善・health の event の配送は変わらない)、true のとき配送される。不正な値は起動時に拒否する。key が無い旧 `ops.toml` で起動できる (既定は `trade_notices=all`、`trade_amounts=absolute`、`deliver_trade_events=false`)。`SKIP_KNOWN` の event も cursor と high watermark が atomic に進み (bot は `commit_skip`、notify は配送なしの `advance_delivery`)、後続の event を塞がず、`afx ctl events` では読める。設定 filter が `plan is None` を再利用していない (「cursor 以下」と「設定で出さない」が区別され、設定で出さない event で cursor が進む) ことを、filter を `continue` だけにする変異で赤になる形で pin する。未知 code は従来どおり進まず止まる。承認通知の設定との合成が §5.3 の表どおり。
- AC-24: 隔離の前に検査済みの fd から設定 (`trade_notices`、`trade_amounts`、notify の `deliver_trade_events`) を読み、値だけが隔離後の runtime に渡る (隔離後に再読しない)。既存の隔離検査を通す。
- AC-25 (T0): §8.3 の 5 つの fixture が bot と notify の両方で通る (e は bot)。混在履歴 (approve / improve / health / 取引 event) の fixture を含む。`reconcile_startup()` が保存した後に最初の `/events` が失敗して再起動し、次の成功で末尾から始まって履歴が流れない fixture を持つ。旧版 snapshot (bot v4、notify v2) が migrate される fixture (配送実績のあるものは `true`、bot v4 の `{0,0}` は `false` で履歴が流れない) と byte 一致 test を持つ。変異で、それぞれ赤になること: (1) `reconcile_startup()` の `_save()` が `event_cursor_initialized` を `true` にする (または snapshot の存在で fresh を判定する)、(2) bot が初回 poll の前に空 snapshot を保存して flag を落とす、(3) snapshot の不在と保存済み `cursor=0` を同一視する、(4) 初回の page を配送する、(5) flag を `false` のまま配送する、(6) 初回成功時に cursor と flag を別の保存に分ける。

- AC-26 (T2): settings の `schema_version` が 4 になり、v3 → v4 の migration が通る。節の無い旧 YAML (v1〜v3) が既定 `notify.trade_events: true` で読め、migrate 後の出力が期待 fixture と byte 一致する。`config/settings.yaml.example` が v4 で `Settings` の検証を通り、example との byte 一致 test が通る。wizard / init の関連 test が新しい版で通る。`notify.trade_events: false` のとき `service.py` が observer も sink も起こさず、取引 event が一切出ない。

## 10. 変更ファイルと task 表

### 10.1 変更ファイル (予定)

| 領域 | ファイル | 変更 |
|---|---|---|
| 初回 cursor (T0) | `afx-ops/afx_ops/gate/state.py`、`afx-ops/afx_ops/notify/state.py`、`afx-ops/afx_ops/bot/adapter.py`、`afx-ops/afx_ops/notify/runtime.py` | snapshot に `event_cursor_initialized` を加える (bot v4 → v5、notify v2 → v3。配送実績のある旧版は `true` として migrate、bot v4 の `{0,0}` は `false`。旧 fixture と byte 一致 test)。`false` の間は配送せず、`reconcile_startup()` などの保存は flag を変えない。最初の正常な `/events` 応答で cursor=high_watermark と `true` を atomic に保存してその page を配送しない |
| event 契約 | `src/agentic_fx/ops/events.py` | `FIELDS` と `_REF` に 2 code、数値 grammar |
| 純粋関数 | `src/agentic_fx/core/trade_events.py` (新) | 遷移後の行 → (code, ref, fields)。書式と検査。`origin_code` の引き当てと再検査 |
| 観測点 | `src/agentic_fx/core/transitions.py` | `observe` / `unobserve` と commit 後の呼び出し (例外は握る) |
| sink | `src/agentic_fx/ops/` の新モジュール | queue、書き込み thread、`busy_timeout=0`、counter とプロセス内 health 状態 (回復 60 秒、`HealthLatch` とは別)、停止時の排出と bounded join |
| status | `src/agentic_fx/ops/client.py` と status の応答 | `afx ctl status` に counter の件数だけを載せる |
| 配線 | `src/agentic_fx/service.py`、`src/agentic_fx/config.py`、`config/settings.yaml.example` | `conn_core` に observer を登録 (1 か所)、`state_mode` を起動時に 1 回読んでメモリに持つ、sink の起動と停止、登録失敗で起動しない、emitter の enable flag `notify.trade_events` (既定 true。`Settings` は `extra="forbid"` なので新しい節として足す) |
| settings の版上げ | `src/agentic_fx/setup/settings_migrate.py`、`src/agentic_fx/config.py`、`config/settings.yaml.example`、wizard / init の関連 test | settings の `schema_version` を 3 → 4。v3 → v4 の migration (節は挿入せず marker だけを更新してよい。節の無い旧 YAML は既定 true で読める)、v4 fixture と旧版 (v1〜v3) の migration fixture、example との byte 一致 test |
| allowlist | `afx-ops/afx_ops/notify/payload.py` | `ALLOWED_FIELDS` と `_REF` と数値 grammar |
| bot / notify | `afx-ops/afx_ops/gate/render.py`、`bot/adapter.py`、`notify/runtime.py`、`gate/state.py`、`notify/state.py` | 完全検証 → 共通の判別型 `POST(payload)` / `SKIP_KNOWN(event_id)`、bot の `commit_skip`、notify の配送なしの `advance_delivery`、取引 event の template、設定による絞り込み (`plan is None` を再利用しない)、非信頼値の escape と `AllowedMentions` |
| 設定 | `afx-ops/afx_ops/config.py`、writer、probe、隔離 plan、example | bot 用 `ops.toml` の optional key `trade_notices` (`all` / `results` / `fills_closes` / `off`、既定 `all`) と `trade_amounts` (`absolute` / `relative`、既定 `absolute`)、notify 用 `ops.toml` の optional key `deliver_trade_events` (真偽、既定 false)。mention は取引通知に付けないので専用の key は足さない |
| 文書 | ops-api spec §4.3、afx-ops spec、`docs/operations/` | event 表に 2 行、文面の追記、配備と rollback の手順 |
| test | 各領域 | 実 payload 形の匿名化 fixture、2 package にまたがる一致 test、変異、canary、golden bytes |

`core/risk_gate.py` は変更しない。orders の書き込み内容と SL/TP 監視の判定は変えない。

### 10.2 task 表 (3 束)

| task | 範囲 | test 完成形の要点 | 規模 |
|---|---|---|---|
| T0 初回 cursor | bot と notify の state と runtime (`adapter.py`、`runtime.py`)。snapshot の `event_cursor_initialized` (bot v5、notify v3、旧版は配送実績があれば `true`、bot v4 の `{0,0}` は `false` として migrate、§8.3)、`false` の間は配送しない、`reconcile_startup()` の保存は flag を変えない、最初の成功 poll で cursor=high_watermark と `true` を atomic に保存して page を配送しない | AC-25。混在履歴 (approve / improve / health / 取引) の fixture、`reconcile_startup()` 保存後の poll 失敗 + 再起動の fixture、migration の旧 fixture と byte 一致、変異 6 種 | M |
| T1 afx-ops | allowlist と数値 grammar (canonical の再生成一致、`ref` の整数範囲)、完全検証 → 共通の `POST` / `SKIP_KNOWN` 型 (`commit_skip`、`advance_delivery`)、取引 event の template、`trade_notices` / `trade_amounts` / notify の `trade_events` (config、writer、probe、隔離 plan、example)、運用文書 | AC-3 (afx-ops 側)、AC-20〜24。golden と敵対 fixture。承認 spec の T3 と同じ部品なので T3 の後か同時 | M |
| T2 daemon | `trade_events`、`FIELDS` / `_REF`、`transitions` の observer registry、sink、配線 (`state_mode` の起動時読み、`notify.trade_events`)、**settings の schema 3 → 4 (migration、v4 fixture、旧版 fixture、example の byte 一致、wizard / init の関連 test)**、counter と health 状態、`afx ctl status` への counter 件数、ops-api spec | AC-1〜19、AC-10b、AC-15b、settings migration の test (AC-26)。transition の呼び出し箇所を全数 grep し、遷移先ごとの event 有無を表で pin (§4.1)。observer と sink の変異 (例外・遅延・溢れ・commit 前停止)。INSERT + commit の実測 | M〜L |

実装の順序は T0 → T1 → T2 (T1 が出荷されるまで daemon の emitter を有効にしない)。AC-3 の 2 package 一致 test は T2 で全体が揃う。

### 10.3 配備手順と rollback

配備:

1. T0 と T1 を含む afx-ops を配る (allowlist、数値 grammar、renderer、filter、設定 reader)。この時点では daemon は旧版で、新 event は出ない。
2. bot と notify を再起動し、実プロセスの版を確認する (`afx-ops --version` または起動ログの版。ファイルを置いただけでは足りない)。health が正常で、cursor が前進することを確認する。
3. daemon の emitter を有効にする (T2 の配備。`notify.trade_events` が true であること)。
4. 最初の取引 event が bot に届いて投稿されることを確認する。notify は `trade_events` が既定 false なので配送されない (cursor が進むことを確認する)。受け手が webhook に流すことを選んだ場合だけ `trade_events=true` にして、配送されることを確認する。

rollback は逆順で、**必ず daemon の emitter を止めてから afx-ops を戻す**。daemon の emitter を止めるには、settings の `notify.trade_events: false` にして daemon を再起動する (または T2 を戻す)。すでに `ops_events` に保存された取引 event は残り、旧版の afx-ops はそれを読むと止まるので、emitter を止めた後、bot と notify の cursor が high watermark に追いついた (保存済みの取引 event を通過した) ことを確認してから afx-ops を戻す。`trade_notices=off` は bot の表示の絞り込みにすぎず、daemon は新 code を生成し続け、notify の配送方針も止まらないので、rollback の手段にしない。daemon を止めずに afx-ops だけ戻すと、新 code で bot と notify が止まる。

`trade_notices`、`trade_amounts`、notify の `trade_events` は optional key + 既定値なので、旧 `ops.toml` のままで新 afx-ops が起動できる。運用文書 (`docs/operations/`) にこの手順を書く。

## 11. 後続と非目標

- 戦略別の成績: 帰属 (チケット `strategy-attribution-and-shadow-evaluation`) の後に `strategy_code` を足す。
- 日次・週次の損益サマリ、損益グラフ、通知のまとめ送信。
- 異常系 (送信結果不明・クローズ結果不明・取消結果不明・換算 degraded) の event 化。現状は daemon 直送で、2 経路の統合は別設計。executor の自動 kill switch ラッチも event を出さない (`guard_state_changed` は ops の reset 操作だけ)。運用上の重要度は高めで、別チケットにする。
- 実発注 (live) の broker が入るときの `mode` の導出と `[実発注]` 文面の見直し。通知の設定を `afx init` の対象にするか。
- event 専用 DB への分離 (同一 DB の書き込み競合を完全に消したくなったとき。§4.5)。
- `service_health_changed` の既存の emit 欠落 (sink 以外の component)。本件は `trade_event_sink` だけを使い、他は別。
- gate 拒否 intent を「拒否通知」に含めるか。利用者の期待とずれ得るので、ユーザー裁定の候補 (現案の rejected は orders 行ができた後だけ)。

## 12. 変更履歴

| 日付 | 版 | 変更 | 理由 | commit |
|---|---|---|---|---|
| 2026-10-20 | v0.1 | C0 下書きを新規作成 | チケット `trade-order-and-close-notices` の設計 | |
| 2026-10-20 | v0.2 | sink を `busy_timeout=0` + 即 drop、`position_closed` を 13 field・両 event に `state_mode`、数値 grammar と producer / afx-ops の二重検査、欠落の理由別 counter と health、初回 cursor を T0 に昇格、配備と rollback の手順、`POST` / `SKIP_KNOWN` の 2 値と `trade_notices` の optional key、`origin_code` の再検査、observer registry、learning 中の paper 発注の明記、遮断 pin を golden bytes + exact-set へ、reflection 経路の追記、orders 全列、`order:` を 19 桁、§8.1 を 4 列・4 問へ | codex sol 1 周目 13 件 (High 6、Medium 5、Low 2) の全採用 | |
| 2026-10-20 | v0.3 | T0 の範囲に `bot/adapter.py` と `notify/runtime.py` を含め、snapshot の不在と保存済みの 0 の区別・初回 poll 前に空 snapshot を保存しない・最初の成功 poll で cursor=high_watermark を保存して配送しない・混在履歴 fixture へ。observer は flock もファイル I/O もせず `state_mode` は起動時に読んだメモリ値。AC-14 を sink の即 drop と observer / queue の非待機に限り、core の待ちは残余リスク + 実測へ。sink の health を counter + warning ログ + プロセス内状態 (回復 60 秒) とし `service_health_changed` は補助、`afx ctl status` に counter 件数。rollback は emitter (`notify.trade_events`) を止めてから afx-ops を戻す。数値は parse → 再生成の canonical 一致。`POST` / `SKIP_KNOWN` の共通型 (`commit_skip`、`advance_delivery`)。mention を非信頼値の防止と設定済み mention (§8.1 問 5) に分け、`origin_code` を「起点」と明示して表示に確定。`order:` の整数範囲 | codex sol 2 周目 9 件 (High 5、Medium 3、Low 1) の全採用 | |
| 2026-10-20 | v0.4 | T0 を、bot の `reconcile_startup()` の保存も含めて閉じる設計へ (snapshot に `event_cursor_initialized` を明示、bot schema v4 → v5、notify v2 → v3、旧版は `true` として migrate、`false` の間は配送せず `reconcile_startup()` の保存は flag を変えない、初回成功で cursor と flag を atomic に保存)。AC-25 に再起動 fixture と変異 6 種。daemon の settings に `notify.trade_events` の新節を足す schema 3 → 4 の migration、fixture、example の byte 一致、wizard / init test を T2 の範囲に (AC-26)。§6 の `afx ctl status` 不変更の文言を、counter 件数の表示と一致させて直した。§8.1 問 2 を (a) 絶対額を出す / (b) R 倍数と価格の変化率だけ、問 5 を (a) 既存の mention を取引通知にも付ける / (b) 付けない、の二択に直し、(b) の表示形と AC を追加 | codex sol 3 周目 3 件 (High 1、Medium 1、Low 1) の全採用 + 問 2・問 5 の実装可能な二択化 | |
| 2026-10-20 | v0.5 | bot v4 `{0,0}` snapshot の migration 方針 (false、問 1 が全履歴なら true) と旧版の明示的な読み取り経路、AC-25 に upgrade fixture。問 1 を二択 (今から / 全履歴) に | codex sol 4 周目 (閉鎖確認) の Medium 1 + Low 1 | |
| 2026-10-20 | v1.0 | ユーザー裁定 5 問を反映して清書。§8.1 を決定の記録に (問 1 今から、問 2 設定で切り替え・既定 `absolute`、問 3 notify は既定 OFF の opt-in、問 4 全部が既定、問 5 mention なし)。bot 用 `ops.toml` に `trade_amounts`、notify 用 `ops.toml` に `trade_events` を足し、`trade_notices` に `results` を足して `fills_closes` を filled と決済だけに改めた。条件分岐の記述を決定後の文に書き換え、v4 `{0,0}` の migration を旧版の記述 (§8.3 (c)、AC-25、§10.1) と整合させた。関連チケットを §0 に追記。AC-20 / 23 / 24 の範囲で追記 | ユーザー裁定 (2026-10-20) | |
| 2026-10-20 | v1.0a | notify 用 key を `deliver_trade_events` に改名 (settings の `notify.trade_events` との衝突回避)、T0 が配備前提なので暫定既定を削除、`fills_closes` = filled + 決済 (rejected は `results` 側) | 清書時の指揮者判断 | |
| 2026-10-21 | v1.1 | sink の busy 再試行を最大 1 秒 (backoff 5〜50 ms、±20% jitter、停止要求で中断) に改訂し、既定で有効にした (§4.2、§4.5、AC-14(a))。core 側の待ちの増加は §4.5 の残余リスクとして A/B 実測を付けて記録 | T2 の実測で、再試行なしは lock 競合で 26〜50% の event を既知のまま捨てるため。外部契約 (best-effort、発注は sink に同期しない) は不変の技術改訂 | |
| 2026-10-21 | v1.1a | 停止要求の後も join の期限までは busy 再試行を続ける (v1.1 の「停止要求で中断」を改める)。期限で放棄した書込み中の 1 件は `shutdown_drop` に 1 回だけ数え、この counter は「保存を確認できなかった件数」の上限と定義 (§4.2 項 5・8) | 停止直前の event を不要に捨てないため。commit の完了と放棄の判定は原子的にできないので、過少でなく過大に数える側に倒す | |
