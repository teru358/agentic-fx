# 運用異常を人へ届けるための設計 C4

- 対象: `auto-killswitch-latch-emits-no-event` / `bot-gateway-disconnect-is-silent`
- 基準リビジョン: `main` `570d7fe1`
- 作成日: 2026-11-14 JST
- 状態: C4 (2026-11-14 利用者承認、実装中)
- 前版: 設計レビューの記録 c3.md (非公開)（原本は保持）
- 本改訂の判定基準: 設計レビューの記録 c3-close-terra.md (非公開)（Critical 0 / Important 3 / Minor 2）。採否と理由は付録 D
- レビュー: 設計レビューの記録 r3-opus.md (非公開)（Critical 0 / Important 6 / Minor 14、食い違い 11）。同レビューのコード照合基準は `afa7819f`（対象コードは `570d7fe1` と同一）

## 1. 結論

資金保護と通知配送を分離したまま、次の二点を直す。

1. kill switch の現 state を通知 outbox として扱う。`guard_kind=kill_switch` の event は executor、reset、reconcile から直接出さず、ops lane 内の共通 sweep だけが既存の `ops_events` と照合して出す。receipt table、queue、専用 sink、`persistence_code`、新しい state metadata は追加しない。
2. bot は gateway、Discord REST の先頭 event、poll 周回、Python event loop、停止処理を別々に監視する。10 分連続で進捗が無い経路は一時 exit にし、決定的に再発する入力・state・配送拒否は恒久 exit にする。systemd は一時 exit を上限 10 分で再試行し、恒久 exit を止める。

kill switch event の code、ref、fields は変えない。

```json
{
  "code": "guard_state_changed",
  "ref": "instance",
  "fields": {
    "guard_kind": "kill_switch",
    "state": "latched",
    "generation": 1
  }
}
```

`state` は `latched` または `released`。追加 field、自由文、口座金額、Discord の ID、URL、token、例外文、traceback は event、health snapshot、journal に入れない。

利用者裁定を次の動作として確定する。

- R1: DD は現行どおり open intent の gate 評価時に判定し、その場でラッチして通知する。mark-to-market tick へ判定を移さない。
- R2: kill switch 通知に DD% と閾値を載せない。
- R3: bot の gateway / delivery / loop health を daemon の `/v1/status` 経由で web に出す。
- R4: HWM rebase は別チケット `killswitch-reset-hwm-rebase` とする。本件では reset 成功時に、条件つき固定文で再ラッチを予告する。表示面は shell と `afx ctl` の二つだけである。
- R5: 独立 notify は任意である。未設定でも live を妨げず、status に `alternate_push=none` の固定警告を出す。

## 2. 動作上の境界と成功条件

### 2.1 不変条件

- 発注、SL、close、DD 判定、kill switch のラッチ・解除を LLM に委ねない。
- event、bot、notify、web、journal の失敗を理由に、成立した kill switch state を戻さない。
- bot の障害を daemon の取引・資金保護へ伝播させない。
- bot / notify は at-least-once とする。投稿成功後、cursor 保存前の crash による表示上の重複は許容する。
- `guard_state_changed` の fields は `guard_kind`、`state`、`generation` の三つから増減させない。
- account balance、equity、HWM の金額、Discord の guild/channel/user ID、gateway URL、session ID、webhook URL、token、例外文、traceback を新しい出力面へ出さない。
- DD 判定時点、HWM 算出、live の解除条件、replay の自動解除を変えない。
- notify の有無は kill switch の成立条件にも live の起動条件にも使わない。

### 2.2 成功条件

- 現存する latched 世代の作動と、既存 latched event に対応する解除を daemon crash や DB 一時障害の後にも event 化できる。clear + event 無しの履歴は補完しない。これは元チケットの「遷移ごとに 1 回」からの**完了条件の縮小**であり、全遷移の通知を保証した達成とは扱わない。前後 sweep がともに失敗したまま clear へ進む窓などの履歴欠落を受容する。誤った過去の作動通知を避けるための境界であり、T4 で元チケットの成功条件・残余リスクをこの範囲へ明示的に更新する（チケット更新は本改訂では未実施）。
- 同じ state incarnation の同じ `(state, generation)` を、ops lane の直列化と `NOT EXISTS` 照合により一件にする。
- executor、StateStore、replay の code は本件で変更しない。activity 重複除去と replay byte 比較 fixture は別チケットとする。
- gateway、REST send、poller、event loop、停止処理のどれが止まっても、process が半日無言で生存する経路を残さない。
- 再発必至の不正 payload / event / HWM 後退 / Discord 配送拒否を無期限の再起動 loop にしない。
- bot が未設定なら `not_configured`、snapshot が読めなければ `unknown` とし、web / status 全体を block しない。
- notify 未設定の live は動作し、status は代替 push が無い残余リスクを固定語で警告する。

## 3. 確認した現状

### 3.1 kill switch

- drawdown 判定は `src/agentic_fx/core/risk_gate.py:193-198` にあり、open intent の gate 評価から到達する。毎分の mark-to-market tick は DD を判定しない。
- 2026-11-13 の事例では DD 超過の観測が 01:43 JST、実際のラッチが次の intent の 07:20 だった。R1 により、この遅延可能性は意図して残す。
- `StateStore.update` は `false → true` で generation と `kill_switch_latched_at` を進め、既に true の単独更新は no-op にする。executor は戻り値で遷移を判別しないため、現行は activity を繰り返す。
- ops API の reset は `released`、reconcile は `latched` を直接 emit する。shell の reset / reconcile は StateStore を直接変え、event を出さない。
- `StateStore.load()` は reset marker を latched として投影し、`file_value()` は投影しない。通知判断は利用者へ示す安全側 state と一致させるため `load()` を使う。
- `live` の reset は generation と `kill_switch_latched_at` を保持する。これらを現世代の照合起点にできる。
- `ops_events` の retention / DELETE は現行コードに無い。
- replay の公開 artifact は `BacktestResult.kill_switch_events` と `kill_switch_latches` である。latched reason は同時刻の `kill_switch_latched` activity 文面から復元されるため、その文面も互換契約に含まれる。

### 3.2 bot / Discord / systemd

- Discord 通知は REST 投稿であり、gateway が切れていても REST が生きていれば配送できる。現行は send の `HTTPException`、`OSError`、`TimeoutError` をすべて握り、cursor を進めず 2 秒ごとに再試行する。
- discord.py 2.7.1 の `on_connect` は READY parse 時、`on_resumed` は RESUMED 時に dispatch される。`on_disconnect` は再接続失敗のたびにも dispatch される。
- `Client.latency` は最後の HEARTBEAT_ACK の RTT であり、ACK からの経過時間ではない。
- 429 は discord.py 内で例外を出さず `retry_after` を待ち得る。send の await、gate 用 executor、poll 周回も戻らない可能性がある。
- 現行 bot reason は `token_invalid`、`bot_state_invalid`、`discord_login_failed`、`discord_unavailable`、`gateway_failed`、`events_invalid`、`high_watermark_regressed`、`payload_invalid`、`bot_internal_error` の九つである。
- bot 本体より前の共通 startup は rc 70 / 71、daemon 到達不能は rc 3、鍵系の共通値は rc 4 を持つ。config 不正と secret 欠落は bot 本体へ届かず rc 71 になる。
- `SnapshotError` は I/O と内容不正を一つに畳み、bot state 層も両方を `bot_state_invalid` にする。
- root logger に handler が無い場合、WARNING 以上は `logging.lastResort` から stderr に出る。asyncio の既定 exception handler も例外文と traceback を出す。web には固定語へ閉じる先例がある (`afx-ops/afx_ops/web/runtime.py:459-480`)。
- 現行 unit は `StartLimitIntervalSec=60s`、`StartLimitBurst=3`、`RestartSec=5s`。このホストの systemd は 259。他配備先の version と restart step の reset 条件は未確認である。

## 4. A: kill switch 遷移を event にする

### 4.1 state 遷移は現行維持

専用 latch API は追加せず、executor、StateStore、activity の記録契約を変えない。通知用 state metadata も追加しない。`DurabilityUncertain` を含め、state が latched になれば sweep が拾う。成立・拒否・失敗の既存判断には介入しない。

### 4.2 replay の境界

replay / backtest runner、`_RecordingStateStore`、executor、risk gate、StateStore を変更対象から外す。本件の replay 受入はこれらの code に差分が無いことの確認に縮める。新 API、recording override、基準 artifact の byte 比較 fixture は要求しない。activity 重複除去と、その際に必要になる replay 互換検証は付録 C の別チケットへ移す。

### 4.3 単一の guard sweep

`OpsService.emit_pending_guard_notices()` を追加する。公開 wrapper は deadline 付きで ops lane を取り、任意の `after` state を先に、その後に現 state を処理する。lane を既に持つ reset / reconcile / scheduler 用には private unlocked helper を使う。kill switch event を INSERT するのはこの helper だけとし、既存の reset / reconcile の直接 `_guard_event` を削除する。

**再入の強制検査:** OpsService instance ごとの `threading.local()` に ops lane 所有フラグを置く。`_ops_lane` は `_acquire` より前に同一 thread の所有を検査し、所有済みなら固定識別子 `guard_lane_reentry` の専用 `RuntimeError` 派生例外を即時に送出する。lock の取得に成功した直後に所有を立て、context の `finally` で例外時も所有を消して lock を解放する。公開 wrapper `emit_pending_guard_notices` も入口で同じ検査を通知専用 try より前に行う。再入を成功扱いにしたり、deadline まで待ったりしない。lane を取る他の公開経路からの再入も共通 `_ops_lane` の検査で拒否する。private unlocked helper は逆に同一 instance / thread の所有を要求する。別 thread の正当な競合は従来の deadline 付き取得を使う。この設計の対象は同期 action / thread の ops lane であり、別 thread へ再入処理を委譲して待つ構造は導入しない。通知専用境界はこの違反も固定 code で隔離して元の mutation 結果を保つが、直接の wrapper 再入呼出しには即時例外が返る。例外文や thread 識別子は出力しない。

sweep は `StateStore.load()` で marker 投影後の state を読み、`generation > 0` かつ `kill_switch_latched_at` がある現世代を扱う。code / ref / `guard_kind` / `state` / `generation` / `event.ts >= latched_at` を UTC aware datetime で照合し、次の順で動く。

1. 現世代の `released` が既にあれば、その世代は閉じたものとして何も出さない。`latched` が無い場合も後追いで補わず、固定 code `guard_notice_backfill_skipped` を process 内で一度だけ journal に残す。
2. `released` が無く、state が latched なら、欠けている `latched` を一件出す。
3. state が clear なら、既存 `latched` があり `released` が無い場合だけ `released` を一件出す。event が両方無ければ閉じた過去世代として skip し、`latched` を補わない。

照合・INSERT が失敗したらその sweep を打ち切り、次の起動 / tick で再試行する。clear + event 無しは skip のため、常に全遷移を復元できる保証ではない。

概念上の存在条件は次である。

```text
EXISTS ops_events
  WHERE code = guard_state_changed
    AND ref = instance
    AND fields.guard_kind = kill_switch
    AND fields.state = <latched|released>
    AND fields.generation = current_generation
    AND parsed_utc(event.ts) >= parsed_utc(current_generation_latched_at)
```

`EventStore.emit` に `not_before` 相当の時刻下限を足す。event の `ts` は `max(UTC wall clock, kill_switch_latched_at)` とし、wall clock 後退中も照合対象外の event を毎 tick 増やさない。受理する値は UTC aware datetime だけとする。

receipt / unique constraint は置かない。同じ SQLite connection、単一 ops lane、kill-switch emitter の一元化が重複防止の前提である。`guard_kind=kill_switch` の INSERT が helper 外に無いことを静的 test で固定する。`_guard_event` 全般は禁止しない（data_feed の `resume_requested` は対象外）。別接続の `_decide_events` と improve supervisor の EventStore からも kill switch を出さないことを scan に含める。

### 4.4 呼出し時点、reset / reconcile、shell

sweep は次で呼ぶ。

- daemon 起動時: event store 構築後、listener と supervisor の開始前。失敗は固定 code で保留し、取引起動を妨げない。
- scheduler tick 後: 一回の ops lane 取得内で improve と guard の helper を別々の try に入れる。guard は `settings.notify.improve_events` の値にかかわらず実行し、improve 側の例外でも飛ばさない。lane 取得失敗は次の tick へ回す。
- ops reset / reconcile: action 内で mutation 前の現 state を sweep する。mutation 後は戻り値 state を明示的に sweep し、次に現 state を sweep する。reset の clear 戻り値で旧世代の release を扱ってから次世代を見る。
- shell reset / reconcile confirm: 現行の直接 StateStore mutation を残し、直接 mutation の前と後に公開 wrapper を一回ずつ呼ぶ。前の呼出し `emit_pending_guard_notices()` (after なし) は、ラッチ後最初の tick より前の解除で latched を失わないためである。後の呼出しは成功 / `ResetNotApplied` / reconcile confirm 後に `emit_pending_guard_notices(after=state)` とする。成功時は mutation の戻り値を捕捉する。`ResetNotApplied` のように戻り値が無い場合は `load()` の marker 投影済み state を best-effort で取得する。wrapper 内では after、現 state の順に処理する。

**例外境界:** pre-sweep、戻り値の post-sweep、現 state の post-sweep をそれぞれ独立した通知専用 try で囲み、SELECT の busy、INSERT 失敗、state 読取失敗を操作結果へ伝播させない。pre-sweep が失敗しても mutation は続ける。通知失敗は一操作につき固定 `guard_notice_deferred` を一行だけ残し、元の成功 / 例外 / error 写像 / 監査終端結果を保つ。lane を保持した action から公開 wrapper を再入呼出ししない。前後の sweep が共に失敗すると、clear + event 無しは §4.3 により skip される。この履歴欠落は受容する。

shell 側も wrapper の lane timeout と通知例外を握り、次の tick へ任せる。`ops_service=None` の test 構成では通知呼出しを skip し、従来どおり直接操作できる。production は `ops_service` 注入が shell 起動より前であり、未注入期間を理由に拒否しない。shell の文面、activity、error 写像、既存 test 契約を維持し、OpsService mutation へ移さないため `ops_request_terminal` の新規 Discord 通知も増やさない。確定済み R4 の §4.7 固定警告追加だけは文面維持の例外とする。

`killswitch_reset` endpoint の `idempotent=False` と expected generation による stale retry 拒否は維持する。shell の mutation 自体が ops lane を通らないことは既知の逸脱である。wrapper 内の照合 / INSERT は直列化するが、shell mutation と wrapper 取得の間に別 sweep が次世代を先に出す競合まで、世代間の全順序は保証しない。これを閉じるための shell 移管は本件で行わない。

### 4.5 導入時 backfill

導入時も §4.3 の三規則を使う。既に released があれば閉じる。未解除の latched state だけに欠けた latched event を補う。clear なら既存 latched に対応する released だけを補い、event 無しの clear から古い「作動しました」を生成しない。

r3 の本番 read-only 調査により、kill switch event は **released・世代 1 の一行だけ**、latched event は無いと確認済みである。解除は直後の `ops_request_terminal` が示す **ops API の approver 経由**であり、shell ではない。state は clear・世代 1、latched_at は存在し marker は無い。released の時刻は latched_at 以後である。従って閉じた世代として追加通知無し、`guard_notice_pending=false` になる。ここで本番 DB を再調査したとは主張しない。

`guard_notice_backfill_skipped` は process 内で一度なので、同じ閉じた世代のまま daemon を再起動するたびに出得る。runbook では正常な導入互換動作と説明する。

generation floor は別チケットへ移す。init / wizard の既存動作を変えず、DB 読取失敗による新しい起動拒否を加えない。時刻照合で旧 incarnation を分離するが、state file 消失と wall clock の大幅な後退が重なる場合の再利用リスクは残る。手編集による generation 後退は自動修復しない。

### 4.6 status、保証、残余リスク

`guard_notice_pending` と対象 generation は、status provider が使うものと同じ短命の read-only DB connection で §4.3 の照合を行って算出する。lane 専用 connection を status thread から使わない。status の途中で state が変わる可能性は観測値として許容し、取引判断には使わない。

- ラッチは supervisor の trade-loop thread で起きる。通常成功時の event 化は、その後最初に終わる scheduler tick の末尾であり、通常の遅れは最大でおよそ 60 秒 + tick の処理時間。これは配送完了時間の保証ではない。R1 の判定時点は変えない。
- lane timeout / DB 一時障害では次の起動時または tick で再試行する。
- state は現世代しか持たない。event 保存に失敗したまま release と次世代の relatch まで進むと、前世代は復元できない。完全保証には durable transition log が必要であり、本設計はこの窓を受容する。
- ops reset の明示戻り値により、DB 正常時の reset 直後 relatch による旧世代 release の見失いを防ぐ。shell の lane 外 mutation の残余競合は §4.4 のとおり。
- event retention を将来導入する場合は receipt 又は durable transition id を改めて設計する。

### 4.7 reset 後の固定警告

HWM は本件で rebase しない。reset が成功したとき、次の固定文を必ず追加表示する。

```text
DD が閾値以上のままなら、次の新規判断で kill switch が再びラッチします。
```

表示面は shell の `killswitch reset` と `afx ctl --as approver killswitch reset` の二つである。数値は出さない。API response field と event field は増やさない。HWM を付け直す操作は `killswitch-reset-hwm-rebase` だけで設計する。

## 5. B: bot の自己監視と回復

### 5.1 gateway state machine

health 判定は monotonic clock、表示時刻は UTC wall clock を使う。状態は `starting / healthy / unhealthy` とする。

- process 開始時から `starting`、`gateway_unhealthy_since=開始時刻` とする。login 中も監視窓に含める。login が済んで起動 reconcile に入る間は gateway の窓を止め（起動 reconcile は §5.2 の最初の poll の窓が監視する）、`connect()` を始めた時点から gateway の 10 分窓を新たに数える。login 9.5 分 + 起動 reconcile 30 秒で誤って exit しない。
- `on_connect`（READY parse 時）と `on_resumed` だけが `healthy` にし、連続不健全 timer を消す。
- `on_disconnect` は `unhealthy` にする。既に不健全なら起点を動かさない。
- `Client.latency` は最後の ACK RTT の表示にだけ使い、health の根拠にしない。
- login / DNS / TLS の retryable failure は process 内で上限つき backoff により再試行し、`starting` が 10 分続いた時点で一時 exit にする。即時 exit と systemd READY の組合せによる 30 秒 login storm を作らない。

連続 10 分 `starting / unhealthy` なら `gateway_unhealthy` として orderly shutdown を開始する。通常の RECONNECT → RESUMED は timer を消す。

### 5.2 REST 配送と poller の進捗

先頭の配送対象 event について、最初の send を開始する直前に `delivery_stalled_since` を monotonic で設定する。例外の捕捉を起点にしない。loop 上の独立した周期 monitor が await の完了を待たずに期限を見る。

- 同じ event の send 中、429 の内部 sleep 中、retryable 失敗後の再試行中は起点を動かさない。
- send 成功と cursor snapshot の保存が両方完了した時だけ timer を clear する。
- 別の REST 呼出しの成功、gateway の READY、health snapshot の成功では clear しない。
- 10 分で `delivery_stalled` の一時 exit にする。cursor は進めず、再起動後に同じ event を再送する。
- REST は下表の呼出し文脈と HTTP status / Discord JSON error code (`HTTPException.code`) で分類する。例外クラス `NotFound` 又は HTTP 404 だけでは恒久終了にも投稿消失にも決めない。分類を既存の広い catch / return より先に適用し、恒久 `BotFailure` を握りつぶさない。
- 「恒久終了」は `discord_rejected` rc 78（login の認証拒否は `discord_login_failed`）、「一時」は 429 / 5xx / network / timeout を既存の再試行と §5.1〜5.3 の進捗上限に従わせる。「有限の応答失敗として除外」は bot 全体の恒久終了・配送 timer の対象から外し、当該操作だけで終える。除外経路も await の無期限停止を許さず、defer / followup / 単発 cleanup は各 REST await を 10 秒の deadline で打ち切る。poll 内の有限 edit retry は回数上限と poller 監視を維持する。

以下は現行 `afx-ops/afx_ops/bot/adapter.py` の Discord REST 呼出し**全 8 箇所**の棚卸し（行番号は C4 作成時）。login など adapter 外の lifecycle も末尾で区別する。`self.client` の approval / events / improve_run は daemon API であり Discord REST ではない。`get_partial_messageable`、`get_partial_message`、`add_view`、`view.stop` はローカル操作で REST を発行しない。

| 呼出し / 現行位置 | 分類 | 理由・処置 |
|---|---|---|
| event の channel send、`deliver_events`:817 | 代替文 / 恒久終了 / 一時 | 固定通知先で繰り返す配送。**400 + code 50035**（その event の body だけの不正）は、本文・例外文を含まない固定の代替文（「この通知は表示できませんでした (event <id>)」）を 1 回だけ送り、送れたら cursor を進める。代替文も拒否されたら恒久。Discord の JSON code が無い 403（Cloudflare 等の前段）と 408 は一時。それ以外の 429 以外の 4xx は恒久拒否、特に 401、403（code 付き）、404 / 10003 は設定修復が必要。失敗 event 以後へ cursor を進めない。一時失敗は先頭 event の 10 分窓を維持 |
| 承認 send、`_send_approval`:447（新規 / 再投稿で共有） | 恒久終了 / 一時 | 同じ bot token / 通知先への承認投稿。恒久 / 一時の規則は event send と同じ（JSON code 無しの 403 と 408 は一時）。ただし 400 + 50035 に代替文は使わず恒久とする: 承認は View が本体で代わりの文では押せず、固定の組立てが拒否されるのは設定か実装の欠陥であり、隠さず停止して知らせる。失敗時 View を stop し、record を投稿成功として確定しない |
| `fetch_message`、`_fetch`:406（現行 :407 catch） | 有限の応答失敗として除外 / 恒久終了 / 一時 | **404 + code 10008** のみ既知 message の消失として `None` を返し、既存の起動時 reconcile / 再投稿へ渡す。channel 消失・権限拒否は下記共通規則で恒久。一時障害は投稿消失に変換せず、対象操作を保留して backoff で再試行する。上限は起動 reconcile か poll 周回の監視窓 (10 分) |
| bot-token edit、`_edit`:434（現行 :435 catch） | 有限の応答失敗として除外 / 恒久終了 / 一時 | **404 + code 10008** は `gone`。pending 表示は再投稿、terminal 表示は既に無いとして完了。通常 pending edit のその他 4xx は恒久。終端 View 除去 (:669)、不採用再投稿の cleanup (:380) は message 局所の 400 等を有限失敗として除外し、前者は既存 `EDIT_RETRY_LIMIT=8` / abandon、後者は一回で終了。ただし下記共有 channel / 認証 / 権限拒否は cleanup でも恒久に優先する。一時障害は `failed` 等の既存経路へ |
| `history(limit=100)`、`_find_posted`:996 | 恒久終了 / 一時 | 投稿後・保存前 crash の回収。429 以外の 4xx は channel / 権限等の恒久拒否。10008 を履歴「空」に読み替えない。一時失敗でも履歴無しと決めて重複投稿せず、履歴の読み直しを保留して backoff で再試行する。上限は起動 reconcile か poll 周回の監視窓 (10 分) |
| `interaction.response.defer`、`on_component`:509 | 有限の応答失敗として除外 | 利用者操作一回への ACK。期限切れ / 二重 ACK 等の 4xx、一時 HTTP 障害、network / timeout はその操作で終了。gate / daemon mutation は defer 成功後だけ行い、失敗時は行わない。bot を止めず、同じ interaction を自動再試行しない |
| `interaction.followup.send`（View 無し）、`_respond`:610 | 有限の応答失敗として除外 | 期限切れ interaction / webhook 等の 4xx も当該利用者応答だけの失敗。gate の決定済み結果を戻さず bot は継続。429 / 5xx / network / timeout も当該応答で終了し再送 loop を作らない |
| `interaction.followup.send`（確認 View 有り、wait=True）、`_respond`:612 | 有限の応答失敗として除外 | 同上。失敗時 View を stop、`bind_confirm` と route 追加を行わない。成功した場合だけ既存の bind を行う |
| adapter 外: `bot.login`、`runtime.py`:151（identity / application 等の内部 REST を含む） | 恒久終了 / 一時 | `LoginFailure` / 401 は認証の恒久拒否、その他 429 以外の 4xx も恒久。一時障害は starting の同じ 10 分窓で backoff し、失敗のたびに試行が作った HTTP session を閉じて（connector は作り直して）から再試行する。内部 endpoint の version 別全数は未確認であり、この login 境界で捕捉する |
| adapter 外: `bot.connect(reconnect=True)`、`runtime.py`:183 | 恒久終了 / 一時（REST 部分） | gateway 接続処理で内部 REST が失敗した場合も status / code が判別できる 4xx は上記恒久規則を優先。`ConnectionClosed` の致命的な close code（4004 認証失敗、4010〜4014 shard・API version・intents の拒否）は再接続しても直らないので `discord_login_failed` の恒久。それ以外の close と一時 REST / 接続失敗は gateway の同じ 10 分窓。WebSocket 自体は REST ではなく §5.1 の対象 |
| adapter 外: `bot.close`、`runtime.py`:177 | 有限の応答失敗として除外（終了処理） | 新たな配送はしない。内部 REST の有無は未確認。close の失敗で既定の終了理由を上書きせず、停止が戻らなければ §5.3 の shutdown 60 秒上限で終了 |

**404 / 権限の優先規則（bot-token の永続 channel 操作）:** `status=404, code=10008` は上表の fetch / edit に限り message 消失。code を HTTP status より先に見る: `10003` (Unknown channel)、`50001` (Missing access)、`50013` (Missing permissions)、HTTP 401 / 403 は共有配送先 / 認証の恒久拒否であり、status が message 局所の 400 であっても、cleanup 除外より優先する。有限の失敗にするのは、これらに当たらない message 局所の 400 等だけである。fetch / 通常 edit の未知・欠落 code の 404 は消失と推測せず恒久拒否とする。cleanup でも 10008 以外の 404 は恒久拒否。interaction token に依存する defer / followup にはこの channel 規則を適用しない。例えば `10062` (Unknown interaction)、`10015` (Unknown webhook)、`50027` (Invalid webhook token)、`40060` (Interaction has already been acknowledged) は一操作の失効として bot を止めない。code / body / ID / 例外文は出力せず、分類結果の固定 code だけを使う。

error code の意味は [Discord 公式 HTTP / JSON error codes](https://docs.discord.com/developers/topics/opcodes-and-status-codes#json-json-error-codes) と照合した。実環境で各 status / code の組合せが返ること、discord.py 2.7.1 内部の全 endpoint、各 deadline の cancellation 完了は未確認であり、§9.2 の fixture / 実装時確認対象である。

poller には `poll_started_at` と `last_poll_completed_at` を持たせる。最初の窓は login 後の起動 reconcile の開始時から数え (login 前は §5.1 の gateway の窓が担当する)、通常の poll が始まってからは各周回の開始から数える。起動 reconcile も一周も、始めてから 10 分完了しなければ `poller_stalled` の一時 exit にする。これにより、event page / gate executor / disk I/O / send 以外の await が戻らない経路も拾う。

初回・snapshot 欠損・v4 cursor 0 の backlog skip と、破損 snapshot で停止する規則は変えない。

### 5.3 event-loop / shutdown watchdog

隔離後に一つの daemon thread を起動する。startup の単一 thread 検査は thread 起動前に完了している。thread は一定周期で `loop.call_soon_threadsafe()` に固定 callback を積み、callback が更新する monotonic acknowledgement を監視する。

- 通常運転で acknowledgement が 10 分更新されなければ event-loop stall とする。
- stderr は書込み直前に `flags = fcntl.fcntl(2, fcntl.F_GETFL)` で既存フラグを読み、`fcntl.fcntl(2, fcntl.F_SETFL, flags | os.O_NONBLOCK)` で他のフラグを保存したまま非 blocking にする。`poll(fd=2, POLLOUT, 0)` は補助とし、PIPE_BUF より短い固定 ASCII 一行を `os.write` する。EAGAIN、fd 操作失敗、書込み不能では出力を捨て、必ず `os._exit(RC_BOT_LOOP_STALLED)` へ進む。poll 後の競合でも block させない。
- `RC_BOT_LOOP_STALLED` は専用の一時 exit code とし、systemd の `status=N` だけで loop stall を区別できるようにする。
- shutdown flag が立っても watchdog を止めない。shutdown 開始から 60 秒で runtime が完了していなければ、同じ非 blocking 出力規則の後 `os._exit(RC_BOT_TRANSIENT)` する。
- `asyncio.run` 完了や `call_soon_threadsafe` の `RuntimeError` では watchdog を止めず、loop 終了後の段へ移る。shutdown 開始時刻を保持し、戻った後の reason 出力、worker 終了、interpreter 終了待ちも同じ 60 秒上限で監視する。通常 exit は daemon thread のため妨げない。意図した rc 78 でも終了が詰まれば上限で rc 79 になる。

`os._exit` は SIGKILL と同じ断面である。cursor と health snapshot は一意 temp への fsync と atomic replace で守られ、孤児 temp は新 process の (dir, name) ごとの最初の書込みで回収される。投稿成功後・cursor 保存前なら既存の at-least-once 規約で再送する。起動時に未終端の確認を無効化する既存回復も維持する。seccomp の `fcntl(F_SETFL)`、`poll`、`exit`、`exit_group`、thread 用 legacy `clone` は現行 allowlist にあるが、実隔離 process の canary を release 条件にする。

### 5.4 health snapshot と安全な reader

cursor を持つ `bot.snapshot` は流用せず、bot state dir に `gateway.snapshot` version 1 を atomic replace で置く。field は次に固定する。

```text
version
observed_at
gateway_state: starting | healthy | unhealthy
gateway_unhealthy_since
last_ready_at
last_resumed_at
last_disconnect_at
last_ack_latency_ms
delivery_state: idle | healthy | stalled
delivery_stalled_since
last_delivery_success_at
poll_started_at
last_poll_completed_at
loop_acknowledged_at
terminal: permanent | transient | null
terminal_code: 固定 allowlist の code | null
```

`last_ack_latency_ms` の NaN / ±inf は `null` にする。unknown key、型違い、非 canonical timestamp、上限超過、秘密 canary を拒否する。ID、URL、session、event 本文、例外文を入れない。

書込みは一つの専用 worker に直列化し、loop thread から fsync を直接行わない。遷移時と 60 秒ごとの最新一件だけを書き、同時 writer を作らない。表示用 snapshot の書込み失敗は bot を止めず、固定 code を遷移時に一度だけ記録する。

追加 worker は daemon thread とし、`ThreadPoolExecutor` の終了時無期限 join に依存しない。追加 thread を非 daemon にする場合は finally で上限つき join を行い、なお残る thread も §5.3 の watchdog が強制終了する。

bot 本体が恒久 / 一時終了を決定したら、単一 writer に最終 snapshot（`terminal` と固定 `terminal_code`）を要求し、最大 1 秒だけ best-effort で待つ。書けなくても exit する。code は §5.6 の bot reason と固定 shutdown code の exact allowlist、通常運転は両 field null とし、任意文字列は受けない。起動時は terminal を null にした snapshot を優先要求し、前 process の終端値を引き継がない。snapshot 自体が書けない恒久異常や、本体より前の rc 71 / import failure は最終値を残せないため web は unknown / stale、又は古い terminal を示し得る。web の permanent-failure は最後に記録できた終了判断であり systemd の現在状態の証明ではない。

state dir の解決は bot と同じく、絶対 path の `$XDG_STATE_HOME` だけを採用し、空 / 相対なら既定の `~/.local/state`、配下は `agentic-fx/ops/bot/` とする。daemon と CLI もこの契約を共有 golden で検査する。両 unit の XDG 環境がずれても別 path を推測探索せず、daemon が解決した path だけを読む。そこに file が無ければ `unknown` だが、古い有効 snapshot が残っていればその値（時刻に応じた stale、terminal が permanent なら permanent-failure）が表示され得るため、環境不一致時の `unknown` は保証しない。ずれの自動判別は追加しない。導入時は bot / daemon 両 unit の実効 XDG_STATE_HOME と HOME からの解決先を local CLI も含め照合し、同じ snapshot を読み書きしていることを observed_at の更新で確認する。環境全体や秘密は出力せず、この一致確認を T2d / T4 の導入チェックとする。

daemon と local CLI の reader は次の順を守る。

1. `O_RDONLY|O_NOFOLLOW|O_NONBLOCK|O_CLOEXEC` で開く。
2. `fstat` で regular file かつ `st_size <= 4096` を確かめる。
3. `os.read(fd, 4097)` を一回だけ行い、4096 byte 超なら拒否する。二回目の read と EOF 待ちはしない。
4. exact schema を検査する。違反、I/O failure、symlink、FIFO、過大 file は `unknown` に閉じる。

bot 未設定は `not_configured`、file 無し又は読取不能は `unknown`、検査済み `terminal=permanent` は時刻が古くても `permanent-failure` を stale より優先する。それ以外は `observed_at` が 2 分超なら `stale` とする。daemon は `afx_ops` を import できないため parser は二重実装になるが、両 package の test が同じ version-controlled golden JSON と hostile fixture を読む contract test を置く。

`/v1/status` は検査済みの非機密値だけを `bot_health` に入れる。web は daemon API だけを読み、bot state dir の Landlock 許可や bot → daemon の mutation endpoint を追加しない。読取り失敗は status 全体を失敗させず `unknown` にする。

### 5.5 logger と asyncio の例外

`discord` logger は閉じたままにし、生 record を journal に流さない。gateway の状態機械が状態遷移ごとに allowlist の固定 code を出すことで「切断を journal に残す」を満たす。discord.py 2.7.1 の template 棚卸し・変換 allowlist は別チケットに分ける。同じ状態の disconnect 連打は timer を延長せず、journal も繰り返さない。

さらに web の先例に合わせて次を行う。

- bot loop に `set_exception_handler` を設定し、allowlist 済み固定 code だけを出す。
- root logger の既存 handler を安全な固定 code handler に置き換え、`asyncio` / `aiohttp` を含む伝播先を閉じる。
- `logging.lastResort = None`、`logging.raiseExceptions = False` とし、安全 handler の失敗で生の record を fallback 出力しない。
- monitor task の例外は必ず回収し、unretrieved task exception にしない。

依存 version を更新するときは asyncio / aiohttp と秘密 canary を再実行する。template inventory は別チケットの保守範囲とする。

### 5.6 exit code の全分類

新しい code は afx-ops の既存 0 / 1 / 2 / 3 / 4 / 69〜74 と衝突させない。C4 では `RC_BOT_PERMANENT=78`、`RC_BOT_TRANSIENT=79`、`RC_BOT_LOOP_STALLED=80` を契約値とする。

| 起点 / reason | exit | 分類 | systemd / 回復 |
|---|---:|---|---|
| 正常 stop | 0 | 正常 | 再起動しない |
| 共通 body failure / startup 前の import 等の失敗 | 1 | 未分類・再起動対象 | 固定 `body_failed` を出せる範囲で出す。配備破損は再起動で直らない場合もある |
| unit の feature / argv 不正 | 2 | 恒久 | 再起動しない。unit / installer を直す |
| daemon 到達不能 (`RC_UNREACHABLE`) | 3 | 暫定の一時分類 | backoff 再起動。server 認証の決定的拒否も 3 へ来るかは未確認。実装時に確認し、確認できた決定的拒否は 78 へ分類 |
| key 不正 (`RC_KEY`) | 4 | 恒久 | 再起動しない。現 bot entry から通常は返らないが共通値として pin |
| feature 未実装 | 69 | 恒久 | 再起動しない。配備物を直す |
| runtime import failure | 70 | 恒久 | 再起動しない。package を直す |
| 共通 startup / config / secret / isolation failure | 71 | 恒久 | bot drop-in の `RestartPreventExitStatus` に含める |
| accept 系 72 / 74、probe 73 | — | bot unit の通常経路外 | 到達しないことを contract test で pin |
| `token_invalid` | 78 | 恒久 | secret を直し明示 restart |
| `discord_login_failed`（401 / login rejection） | 78 | 恒久 | token / Discord 側を直し明示 restart |
| `bot_state_invalid`（内容 / schema 不正） | 78 | 恒久 | snapshot を調査・回復し明示 restart |
| `payload_invalid` | 78 | 恒久 | producer / consumer 契約を直し明示 restart |
| `events_invalid` | 78 | 恒久 | event page 契約を直し明示 restart |
| `high_watermark_regressed` | 78 | 恒久 | daemon / DB を調査し明示 restart |
| `discord_rejected`（§5.2 表の恒久対象 4xx。投稿消失・有限応答の除外を先に判定） | 78 | 恒久 | cursor を保ち、権限 / channel / payload を直し明示 restart |
| bot cursor state / `open_state_dir` の OSError | 79 | 一時 | backoff 再起動。内容不正とは例外型を分ける。health 表示専用 snapshot の I/O は非致命。ただし state file が symlink のとき (`O_NOFOLLOW` の ELOOP) は、人が直すまで再試行しても直らないので内容不正と同じ `bot_state_invalid` の恒久 78 にする（明示的な例外） |
| `discord_unavailable`、`gateway_failed`、`gateway_unhealthy` | 79 | 一時 | process 内の 10 分監視後に backoff 再起動 |
| `delivery_stalled`、`poller_stalled` | 79 | 一時 | cursor を保ち backoff 再起動 |
| `bot_internal_error` | 79 | 一時 | 固定語だけ出し backoff 再起動 |
| shutdown 60 秒超過 | 79 | 一時 | watchdog が `os._exit`、backoff 再起動 |
| event loop 10 分 stall | 80 | 一時・専用 | watchdog が `os._exit`、backoff 再起動 |
| SIGHUP / SIGINT / SIGTERM / SIGPIPE | signal | clean exit | `Restart=on-failure` では再起動しない |
| SIGSYS / SIGKILL / SIGSEGV 等 | signal | 異常終了 | systemd が再起動。status で signal を区別 |

同じ数値でも他 process では別の意味を持つ（server_auth_probe の 3 は REJECTED、4 は NO_ANSWER、probe の 71 は INPUT_FAILED）。上表は bot unit の入口に限る。表に無い非零 rc（Python 終了時 flush failure の 120 を含む）は restart prevent に無いため再起動される。

rc 71 は config 不正だけでなく `secrets_failed` の一時 I/O や isolation の環境失敗も含む。それでも bot unit では恒久扱いにする。共通 startup の reason taxonomy をこの ticket で大きく分割するより、無期限 loop を確実に止める方を優先するためである。人が原因を直した後は明示 restart が必要である。この副作用を runbook と status に書く。

snapshot 層には I/O failure と decoded content / schema failure を区別する例外型を追加し、前者だけを 79、後者を 78 にする。

bot instance 専用 drop-in は次の意味を持つ。

```ini
[Unit]
StartLimitIntervalSec=0

[Service]
Restart=on-failure
RestartSec=30s
RestartSteps=5
RestartMaxDelaySec=10m
RestartPreventExitStatus=2 4 69 70 71 78
```

login 前を gateway の `starting` 10 分窓に含め、retryable login failure は process 内で backoff する。このため systemd が READY のたび restart step を初期化する実装でも、Discord outage 中に 30 秒ごとの login / IDENTIFY は起きない。restart step が初期化されない実装では、最悪の自動回復時間は検知 10 分 + restart 待ち 10 分であり、これを受容上限とする。

実 manager smoke で restart step の reset 条件（READY、自動 restart、手動 start、reset-failed）を測る。`Requisite=afx.service` があるため、daemon 停止中に再起動時刻が来て依存で失敗した場合、その後自動再試行するか daemon の `Wants=` による start を待つかも確認する。後者なら runbook に daemon 回復時の bot 起動確認を明記する。daemon が稼働し依存を満たす間の回復上限として、どちらの挙動でも、長期 outage 時に process の即時 exit を繰り返す 30 秒 login storm を作らず、10 分監視後の systemd 再試行待ちが 10 分を超えないことを release 条件にする。process 内の上限つき login backoff と process 間の再起動待ちは区別する。断定は実測後に運用文書へ記録する。

installer は bot instance 専用 drop-in を install / exact verify / remove / stage artifact / contract test の対象にする。systemd 254 未満は暗黙 fallback を作らず、bot enable を固定理由で拒否する。

## 6. 任意の notify と表示面

`afx-ops@notify` は bot と別 cursor を持つ既存の任意経路である。live の配備条件にはしない。設定されていなくても daemon と bot を起動し、本 ticket の完了条件を満たせる。

status の値は次の二つだけにする。

- `alternate_push=none`: notify が enable 完了状態に無い。`afx status` と `afx ops bot status` は固定 warning `alternate_push=none` を出す。
- `alternate_push=configured`: notify が enable 完了状態にある。endpoint の値や種別は出さない。

この値は「別 cursor の notify が設定された」ことだけを表し、Discord からの障害領域の独立性、現在の到達性、配送成功を保証しない。独立性は runbook の確認項目に留め、`independent` という status 値は作らない。stage/config を読めない場合は安全側に `none` とし、秘密 file や webhook URL は読まない。

R3 の `bot_health` にもこの二値を含め、web は警告を表示できるようにする。notify 未設定を `bot_health=unhealthy` とはしない。新しい email / SMS / pager provider、host 外 dead-man monitoringは対象外である。

## 7. 失敗時の動き

| 失敗 | state / 取引 | event / 配送 | 見え方と回復 |
|---|---|---|---|
| state 保存前 crash | 遷移不成立 | event なし | 次の open intent で再評価 |
| state replace 後、sweep 前 crash | latched 維持 | 起動時 sweep が補完 | pending → event |
| `DurabilityUncertain`、reload latched | latched 維持 | sweep が補完 | 固定 durability warning |
| `DurabilityUncertain`、reload clear | 現行どおり上位失敗 | event なし | daemon failure |
| SQLite busy / lane timeout | state 不変 | 次の起動 / tick で再試行 | `guard_notice_pending` |
| 旧 DB に `released N` だけ存在 | state は clear | `latched N` を後追いしない | `guard_notice_backfill_skipped` |
| ops reset 後すぐ N+1 へ再ラッチ | N+1 latched | reset 戻り値から `released N`、現 state から `latched N+1` | 順序を保つ |
| event DB 障害中に release → relatch | 現世代を優先 | 前世代を復元不能な窓あり | §4.6 の残余リスク |
| gateway 不健全 <10 分 | daemon 無影響 | REST が生きれば配送継続 | unhealthy、interaction 不可 |
| gateway / login 不健全 10 分 | daemon 無影響 | rc 79 | systemd backoff restart |
| send / 429 wait が 10 分 | cursor 不変 | rc 79、再起動後再送 | delivery stalled |
| send / fetch / history / 対象 edit / login が 429 以外の 4xx | cursor 不変 | rc 78 | permanent failure、人が修復 |
| poll 周回が 10 分未完了 | daemon 無影響 | rc 79 | poller stalled、再起動 |
| event loop が 10 分停止 | daemon 無影響 | rc 80 | 非 blocking stderr 後 `os._exit` |
| orderly shutdown が 60 秒停止 | daemon 無影響 | rc 79 | watchdog が `os._exit` |
| health file が symlink / FIFO / 巨大 / 破損 | daemon 無影響 | 配送継続 | bot health `unknown`、status は応答 |
| payload / events / HWM が決定的不正 | daemon 無影響 | rc 78、cursor 不変 | permanent failure |
| rc 71 startup failure | daemon 無影響 | bot 未起動 | unit は恒久停止、明示 restart。web の terminal 表示は保証しない |
| notify 未設定 | daemon / bot 無影響 | bot の一経路だけ | `alternate_push=none` 警告 |
| bot と notify の双方停止 | daemon 無影響 | push なし | status / web / journal の pull のみ |

## 8. 互換性、導入、rollback

### 8.1 互換性

- event code / ref / fields は不変で、旧 afx-ops の strict validator が受理できる。
- AppState schema は増やさない。既存の latch / generation / latched_at だけを使う。
- `gateway.snapshot` は新規 file で、既存 `bot.snapshot` の version / cursor 規約を変えない。
- `/v1/status` の `bot_health` は additive である。古い daemon では web に項目が無く、新 daemon + 旧 bot は `unknown` になる。
- bot 専用 drop-in だけが start-limit / restart prevent を上書きし、web / notify の policy は変えない。

### 8.2 導入順

1. afx-ops に exit 分類、snapshot 例外分離、gateway / delivery / poller tracker、loop / shutdown watchdog、安全 logger、health snapshot を配る。
2. bot 専用 drop-in を installer で配り、実 manager と実 seccomp の smoke を行う。
3. daemon に EventStore の時刻下限、guard sweep、shell 直接操作後の公開 wrapper、安全な health reader、status / web field を配る。latch / replay / init は変更しない。
4. 最初の sweep で現 state と event の組合せを確認する。`released` だけの世代へ `latched` を後追いしていないことを確認する。
5. notify 未設定の live が起動し、`afx status` と `afx ops bot status` に `alternate_push=none` が出ることを確認する。notify を使う環境だけ、別 cursor の E2E を追加確認する。

daemon が新 event を出す前に afx-ops を配る。event schema 自体は不変だが、恒久 exit 分類と監視を先に有効にするためである。

### 8.3 rollback

- daemon を戻すと自動ラッチ event の補完が止まる。既存 event と state は削除しない。
- AppState schema は増えないため、metadata migration / rollback は不要である。
- afx-ops を戻す前に bot 専用 drop-in を戻す。旧 bot は新 exit code の意味を持たず、`StartLimitIntervalSec=0` だけを残すと決定的不正を無期限再起動し得る。
- `gateway.snapshot` は未参照 file として残せる。削除を rollback 条件にしない。
- event row と bot / notify cursor を削除・巻き戻ししない。

## 9. test と受入条件

### 9.1 state / sweep / replay

- StateStore / executor / risk gate / replay / backtest の code に本件の差分が無い。AppState schema と既存 activity を変更しない。
- marker を投影する `load()` を正本とし、marker 中の clear file から released を出さない。
- 起動 / combined tick / ops reset 前後 / reconcile 前後で同じ incarnation の event を重複させない。trade-loop のラッチ後に最初に終わる tick で event 化する。
- ops reset 直後の N+1 ラッチでも戻り値から released N、現 state から latched N+1 を出す。
- fixture は clear + `released N` のみ → 追加無し、clear + `latched N` のみ → released のみ、clear + event 無し → 追加無し。latched + event 無し → latched 一件も検査する。
- pre-sweep の SELECT busy 後も mutation を続ける。mutation 成功後の照合 SELECT busy / INSERT failure でも元の成功と監査終端結果を維持する。戻り値 sweep の失敗で現 state sweep を飛ばさず、通知例外文を出さない。
- improve 無効時も guard が動き、improve 例外でも guard を実行する。lane は一回だけ取得する。
- lane を持つ reset / reconcile / scheduler の action から公開 wrapper を意図的に再入させ、`guard_lane_reentry` が即時に返ることを確認する。再入時に `_acquire` / sleep / deadline 待ちを呼ばず、二重 INSERT も無いことを spy で検証する。通知境界で捕捉されても mutation 結果・監査終端を保つ。例外後の所有フラグ解除、次の通常 wrapper 成功、所有無し private helper の拒否、別 thread の通常競合と timeout を別々に確認し、単なる全呼出し拒否で test が通らないようにする。
- wall clock 後退は `not_before` で clamp し、timezone 表記違いも datetime 比較する。
- ラッチ直後 (tick 前、event 無し) の shell reset で latched と released がこの順に一件ずつ出ること、reconcile confirm で前の世代の未送出 latched が先に出ること、前の wrapper が失敗しても reset が成功し文面と activity が変わらないことを確認する。前の呼出しを外す変異で赤になること。
- shell の直接 mutation、既存 test、error 写像、activity、Discord の ops_request_terminal が増えないことを確認する。成功 / ResetNotApplied / reconcile confirm 後の wrapper は通知失敗を握る。未注入 test も従来どおり通る。R4 の警告追加だけを別途検査する。
- source scan は kill-switch INSERT の helper 一元化だけを固定し、data_feed の `_guard_event` を誤検知しない。`_decide_events` と improve supervisor の別接続も対象に含める。
- pending status は短命 read-only connection を使い、閉じた世代は false。本番と同じ released 世代 1 のみ + clear fixture は追加通知無しとなる。
- producer / bot / notify の exact-set test で event 三 field を維持する。

### 9.2 bot / systemd / 秘密

- fake monotonic clock で starting / READY / RESUMED / disconnect / disconnect 連打 / login retry / RECONNECT を試す。9分59秒は生存、10分で rc 79。disconnect 連打で起点を動かさない。
- 初回 poll の窓は login 後の起動 reconcile の開始から数える。login に 9 分半かかった後でも最初の poll は開始から 10 分生存し、10 分で `poller_stalled` になる。通常 poll の開始後は各周回の開始から 10 分。起動 reconcile の hang も rc 79 で止める。fetch / history の 429・一時失敗は保留して backoff で再試行し、消失・履歴空に読み替えず、窓の上限で止める。`connect()` の一時的な REST / 接続例外は backoff で再接続して既存の不健全の起点を動かさず、恒久 4xx は先に判定して rc 78 にする。test 自身も実時間の期限を持ち、超過を明示的な失敗として報告する。
- finite / NaN / inf latency を試し、health 判定には使わず、非有限値は snapshot の null になる。
- send が永遠に返らない、429 の内部 sleep、retryable 例外、別 REST 成功、send 成功後 cursor 保存停止を試す。期限は先頭 event の最初の試行からで、send + cursor 保存時だけ回復する。
- gate executor の関数を block し、poll 周回が 10 分で rc 79 になる。gateway と loop ack が正常でも検知する。
- §5.2 表の adapter 内 REST 全 8 箇所と adapter 外 login / connect / close を fixture に対応させる。event send と承認 send を別々に 400 / 401 / 403（code 付き）/ 404（10003）→ rc 78、429 / 5xx / network / timeout と JSON code 無しの 403 / 408 → 一時として試す。event send の 400 + 50035 は代替文 1 回で cursor が進み、代替文の拒否は rc 78、一時失敗は同じ event から再送。承認 send の 400 + 50035 は rc 78。login が 9.5 分 + 起動 reconcile 30 秒以上でも gateway の窓で落ちない（connect の stub を即時にしない）。致命的な close code（4004・4010〜4014）→ rc 78。login の失敗が HTTP session を溜めない。state dir の symlink / dir でない / 無いは rc 78、その他の OS の失敗は rc 79。history の権限拒否も rc 78 とし、失敗を履歴空にしない。login 認証拒否と一時 failure の 10 分窓も確認する。cursor は拒否 event より先へ進めない。
- fetch の 404 / 10008 は `None` を返し起動時 reconcile が既存の再投稿 / adopt に進む。pending edit の 404 / 10008 は `gone` から repost intent 保存 → 再投稿 → record 付替えへ進む。terminal edit の同じ消失は完了扱いで再投稿しない。いずれも rc 78 にせず、既存の世代 / footer / cursor 契約を保つ回帰 test を置く。
- 同じ fetch / edit に 404 / 10003、403 / 50001、403 / 50013、401 と、status 400 の 10003 / 50001 / 50013 を注入し rc 78 とする。`None` / `gone` / 再投稿へ進めず、cleanup でも恒久拒否を握らない。code 欠落 / 未知の 404 も恒久、429 / 5xx / network は消失にしない。有限の message 局所 cleanup 失敗は既存 8 回上限又は単発で終わり、無限 retry に変えない。
- defer と followup 二経路に 10062 / 10015 / 50027 / 40060 等の 4xx、429 / 5xx / network / timeout を注入し bot が生存して次の poll / interaction を処理できる。defer 失敗時は gate / daemon 操作無し、followup 失敗時は成立済み操作を戻さず再送しない。View 有り失敗では stop、bind_confirm / route 追加無しを確認する。返らない await は各 10 秒で打切り、成功した REST / poll の進捗を損なわない。
- `payload_invalid`、`events_invalid`、`high_watermark_regressed`、state 内容不正は rc 78。cursor state / open_state_dir の一時 I/O は rc 79、health 表示専用 I/O は非致命。
- loop を停止し、watchdog が F_GETFL → 既存 flags と O_NONBLOCK の OR → F_SETFL の順で他の flags を保持し、`poll(POLLOUT,0)` 後に rc 80 で `os._exit` する。F_GETFL / F_SETFL 失敗、fd 2 書込み不能でも出力を捨てて exit へ進む。
- shutdown 中に worker / `bot.close` / default executor を block し、60 秒で rc 79 になる。`asyncio.run` 完了後も watchdog が生存し、恒久 rc 78 を返す直前の fd 2 停滞 / worker fsync 停滞でも shutdown 開始から 60 秒以内に rc 79 へ進む。正常 exit は妨げない。
- 実 seccomp profile で fcntl の O_NONBLOCK 設定、poll、legacy clone、exit / exit_group を通し、systemd の再起動まで確認する。
- health writer は単一で、NaN / inf を書かず、書込み失敗で bot を止めない。最終 permanent snapshot は最大 1 秒待ち、成功なら古くても web は permanent-failure、失敗しても exit する。再起動時に null terminal へ置き換わること、startup 前 failure では terminal を保証できないことも確認する。
- reader は symlink、FIFO、directory、4096 / 4097 byte、size TOCTOU、破損 JSON、unknown key を block せず `unknown` にする。bot 未設定は `not_configured`。
- daemon package と afx-ops package が同じ golden JSON を読み、同じ正規化結果を返す。絶対 / 空 / 相対 XDG の path 解決も golden に含め、unit 間の設定不一致時は daemon の解決先が欠損なら unknown、古い有効 file がある場合は stale / permanent-failure 等その file に従うことを確認する。実効環境から同じ path を解決し observed_at 更新を共有する導入確認も行う。
- discord / root / asyncio / aiohttp の record に token、Authorization、URL、snowflake、body、改行、traceback を置き、出力 byte に一つも現れない。gateway の同状態反復で journal が増えず、discord logger の生 record が閉じている。
- exit 表の全行を unit test し、`RestartPreventExitStatus=2 4 69 70 71 78` を exact verify する。
- disposable user manager で 30 秒 → 最大 10 分、start-limit 無効、恒久 stop、正常 stop を確認する。restart step の reset 条件を記録し、長期 retryable outage に対して process 内 10 分監視 + 最大 10 分待ちを確認する。daemon 稼働・依存充足中の再試行を 20 分以内にし、daemon 停止中の Requisite 失敗と復帰時の起動経路も測定する。
- installer の install / exact verify / remove / stage、systemd 254 未満拒否、`tests/ops_setup/test_systemd_contract_ac12.py` を更新する。

### 9.3 end-to-end / 表示

- generation 1 の latched event は既存三 field と完全一致し、bot と設定済み notify が別 cursor で配送できる。
- notify 未設定でも live / bot が動き、`afx status`、`afx ops bot status`、web の bot health に `alternate_push=none` の警告が出る。
- notify 設定済みなら `alternate_push=configured`。Discord から独立しているとは表示しない。
- gateway health は web に `healthy / unhealthy / stalled / stale / unknown / not_configured / permanent-failure` として秘密なしで出る。
- gateway 不健全中は承認 interaction が使えないことを UI / runbook に出す。
- reset 成功時、shell と `afx ctl` の両方が §4.7 の固定文を一回出す。API field、event、Discord 通知に DD 数値を足さない。
- bot と notify を止めても kill switch state と generation は変わらず、status / web / journal から確認できる。

## 10. 利用者裁定の記録

| ID | 裁定 | 本設計への反映 | 理由 |
|---|---|---|---|
| R1 | open intent の gate 評価時にラッチして通知 | §1、§2、§3、§4 | 資金保護と replay の意味を変えず、成立済み遷移の通知欠落だけを直す |
| R2 | DD% を通知に載せない | event 三 fieldを維持し、state metadata も追加しない | 過去世代の数値履歴を新設せず互換性を保つ。閾値は status、ラッチ時 DD は activity にある |
| R3 | web に bot gateway 状態を出す | 安全な 4 KiB reader → `/v1/status` → web | bot から daemon への mutation や web の追加 filesystem 権限なしで観測できる |
| R4 | HWM rebase は別 ticket | §4.7 の固定警告だけを shell / `afx ctl` に出す | reset と損失予算の再設定を混ぜない |
| R5 | notify は任意、未設定なら status 警告 | §6、§8.2、§9.3 | 導入を止めず、代替 push が無い残余リスクを隠さない |

## 11. 未確認と対象外

### 11.1 未確認

- 2026-11-13 の bot 不通の直接原因と正確な gateway 切断時刻。snapshot 停止だけでは gateway / poller / loop のどれかを断定できない。
- 当時 user manager に load されていた unit properties、restart count、DNS inode 置換との因果。
- 他配備先の systemd version。確認済みはこのホストの 259だけである。
- systemd restart step の正確な reset 条件と、daemon 停止中の Requisite start job 失敗後の再試行。実 manager smoke で確かめる。
- discord.py 2.7.1 の失敗した login を同じ Client で再試行できるか。実装時に session 後始末を確認し、成立しなければ一試行ごとに Client を作り直す（同じ 10 分窓を維持）。
- daemon が bot 鍵を恒久拒否した際に ApiError が rc 3 へ来るか。実装時に経路を確認し、決定的拒否を 78 に写像する。
- notify endpoint が Discord から独立しているか。status は独立を主張しない。

### 11.2 対象外

- 専用 latch API、activity 重複除去、replay fixture、generation floor、discord logger template 棚卸し（付録 C）。

- DD 判定を mark-to-market tick へ移すこと。
- HWM rebase、自動解除、解除猶予を変えること。
- event retention、完全な transition history、exactly-once 配送。
- bot slash status の新設。
- 新しい pager / email / SMS provider、host 外 dead-man monitoring。
- 過去事例の原因を DNS、gateway、REST、poller、loop stall のいずれかと断定すること。
- afx-ops 全 feature の startup rc 71 taxonomy を分割すること。本件は bot drop-in で 71 を恒久停止にする。

## 12. 変更履歴

| 版 | 日付 | 変更 | 理由 | commit |
|---|---|---|---|---|
| C0 | 2026-11-13 | 専用 latch API + receipt / queue / sink、gateway watchdog、3 starts / 1h、追加 event fields を提案 | 初案 | 未確認（tmp 文書） |
| C1 | 2026-11-13 | state + `NOT EXISTS` sweep、既存三 field、REST / loop 監視、backoff と恒久 exit を提案 | opus 1 周目の指摘を反映 | 未確認（tmp 文書） |
| C2 | 2026-11-13 | R1〜R5 を確定。R5 任意化、closed generation backfill、全 exit 分類、send / poller / shutdown 監視、安全 reader / logger、metadata 削除、replay fixture、task 分割を確定 | opus 2 周目 Important 8 / Minor 14 と利用者裁定を絶対基準で再判定 | 未確認（C2 の commit は本改訂で確認していない） |
| C3 | 2026-11-14 | sweep 例外境界、全 REST 分類、終了後 watchdog、terminal snapshot、shell 直接経路維持、周辺作業分割、実データ反映 | opus 3 周目 I1〜I6 / m1〜m14 と今回の指示に基づき、通知欠落と無言停止に範囲を絞る | —（c3.md、未 commit） |
| C4 | 2026-11-14 | REST 呼出し全数表、message 消失回復と恒久拒否の分離、lane 再入即時拒否、XDG の古い表示と導入確認、fcntl flags 保持、完了条件縮小を明示 | terra C3 閉鎖確認 I1〜I3 / Minor 2 件とチケット成功条件の指摘を採用。コード・C3 は未変更、実装検証は未実施 | —（c4.md、未 commit） |
| C4.1 | 2026-11-14 | §4.4 shell は直接 mutation の前にも公開 wrapper を一回呼ぶ。§9.1 に対応 test を追加 | 実装で見つかった shell の前 sweep の欠落 (ラッチ後最初の tick より前の shell 解除で latched も released も出ない) | — |
| C4.2 | 2026-11-14 | 初回 poll の窓を login 後の起動 reconcile の開始から数える、fetch / history / connect の一時失敗を保留 + backoff にする、cleanup の恒久判定で code を先に見る、symlink の state file を恒久 78 の明示例外にする | 実装レビューで見つかった初回 poll の窓の起点と一時失敗の再試行の欠落 | — |
| C4.3 | 2026-11-14 | gateway の窓は login を含み、起動 reconcile の間は止めて connect の開始から数え直す。送信の 400 + 50035 は固定の代替文 1 回、JSON code 無しの 403 / 408 は一時。state dir の symlink / 無い / dir でないは恒久。致命的な gateway close code は恒久。login の再試行ごとに HTTP session を閉じる | /code-review high の指摘（窓の起点、1 通知の拒否での全停止、再試行しても直らない失敗の再起動ループ、session の滞留） | — |

## 13. task 表

| task | 実装範囲 | 完了条件 |
|---|---|---|
| T1b guard sweep | 時刻下限、combined sweep、ops 前後の例外境界、shell 後 wrapper、lane 所有検査、backfill / pending | §9.1 fault / race / fixture / source scan、再入即時拒否と非待機、replay code 差分無し |
| T2a bot progress / exit | gateway / delivery / poller、§5.2 全 REST 表と有限応答 deadline、state I/O 分離 | fake clock / hang / 各 REST 分類、10008 の既存消失回復、channel・権限拒否、followup / defer 非致命、exit 表 |
| T2b loop / shutdown | watchdog、F_GETFL + O_NONBLOCK の OR + F_SETFL、loop 終了後も停止上限 | 既存 flags 保持、fcntl 失敗時 exit、fd 2 / worker 停滞、実 seccomp |
| T2c logging | discord 遮断、root / lastResort / asyncio handler、task 回収 | 秘密 canary、固定 gateway 遷移 code |
| T2d health surface | 単一 writer + terminal、安全 reader、path golden、CLI / API / web | hostile file、恒久表示、再起動後クリア、XDG 不一致で古い有効 file を表示する fixture と導入時 path 一致確認 |
| T3 recovery unit | bot drop-in、installer / verify / remove / stage、254 未満拒否 | 実 manager の backoff / prevent / reset / Requisite |
| T4 operations | R4 固定文、任意 notify 警告、runbook / ticket / 既存 spec 整合、XDG 導入確認 | §9.3、r3 食い違い文書の更新。元チケットの「遷移ごとに 1 回」から §2.2 の完了条件へ縮小したことと clear + event 無しの履歴欠落受容を成功条件・残余リスクへ明記し、全遷移保証の達成とは報告しない。bot / daemon / CLI の path 一致と snapshot 更新共有を確認 |

T1a / T1c は欠番として別チケットへ移す。T2c の template 棚卸しも本件の完了条件に含めない。

## 付録 A. opus 3 周目の採否と理由

r3 を判定基準とし、r2 の採否表は C2 に履歴として残す。r3 の推奨を本版の範囲に合わせて具体化した差分は以下に明記する。

| ID | 採否 | C3 の裁定と理由（C4 の訂正を併記） |
|---|---|---|
| I1 | 採用（補完規則は m6 優先） | §4.4。前後の通知例外を操作から隔離し、前 sweep 失敗でも続行。m6 採用により clear + event 無しを latched → released で補う案は採らず、欠落窓を明記する |
| I2 | 採用 | §5.3〜5.4。asyncio.run 完了後も daemon watchdog が 60 秒を監視する。新 thread の終了方針を固定し、fd 2 / fsync 停滞を受入に追加 |
| I3 | 採用（C4 で訂正） | §5.2、§5.6。C3 の一律 4xx→78 は terra I1 / I2 により訂正。永続 channel の恒久拒否と、一投稿の 10008 消失・有限 cleanup・一操作の interaction 応答失敗を区別する |
| I4 | 採用 | §5.4。terminal / terminal_code を単一 writer が最大 1 秒で best-effort 保存。permanent は stale より優先する。保存失敗と startup 前終了は表示保証外と明記 |
| I5 | 採用・別チケット | §4.1〜4.2、付録 C。changed API は sweep に不要で、activity 修正だけのために replay へ変更を広げない |
| I6 | 採用 | §4.4。shell は直接 mutation の後に公開 wrapper。既存 error / activity / test と ops_request_terminal を維持。R4 の警告だけは確定済み例外。lane 外 mutation と世代間順序の残余競合を明記 |
| m1 | 採用 | §4.6、§9.1。trade-loop でラッチし、その後最初に終わる tick で event 化。通常およそ 60 秒 + tick 処理時間 |
| m2 | 採用 | §4.4、§9.1。guard は improve 設定から独立し、単一 lane 内の別 try で失敗を隔離 |
| m3 | 採用 | §4.4。production は起動前注入。未注入の test は直接経路を保ち、固定拒否を削除 |
| m4 | 採用 | §5.6。clean signal / 異常 signal、startup 前 rc 1、未掲載 rc 120、open_state_dir OSError を明記。server auth 拒否の rc 経路は未確認として実装時確認へ |
| m5 | 採用 | §5.3、§9.2。poll は補助、fcntl O_NONBLOCK で write の競合を閉じる |
| m6 | 採用 | §4.3〜4.5。clear + event 無しから古い作動通知を生成しない。既存 latched がある clear にだけ released を補う |
| m7 | 採用 | §4.5。backfill_skipped は再起動ごとに出得る正常な互換動作と runbook へ記す |
| m8 | 採用 | §5.4、§9.2。XDG 絶対 / 空 / 相対の解決と golden を追加。unit 環境不一致では推測探索しない。C4 で「必ず unknown」を訂正し、古い有効 snapshot 表示の可能性と導入時の一致確認を明記 |
| m9 | 採用 | §4.3、§9.1。kill-switch INSERT に限定して scan。data_feed を除外し別接続も含める |
| m10 | 採用・別チケット | 付録 C。latched_at 以後の照合で通常は足り、floor は今回の通知不具合に不要。state 消失 + 時刻後退は残余リスクとして明示 |
| m11 | 採用・一部分割 | §5.5、付録 C。template 棚卸しは分離。新しい監視 task の秘密漏洩防止と gateway 固定 code は必要なので残す |
| m12 | 採用 | §4.5、§11.1。r3 が確認した released 世代 1 の一行と ops API approver 経由で二つの未確認事項を置換 |
| m13 | 採用（指摘の前提を除去） | I6 により shell 移管を行わず、新たな ops_request_terminal と Discord 操作終了通知を増やさない |
| m14 | 採用 | §5.6、§9.2、§11.1。restart step reset と Requisite 失敗後の挙動は未確認のまま実 manager smoke に含める。READY で消えるとは断定しない |

全面却下の指摘は無い。I1 の「clear 時も二遷移補完」は m6 の誤通知防止を優先して却下した。I6 の wrapper だけで世代間の順序競合をすべて閉じるという説明は、shell が lane 外で mutation する以上保証できないため採らず、保証を wrapper 内の照合 / INSERT の直列化に限定した。いずれも資金保護・既存 shell 契約を変えずに成立する通知を補うという本件の境界に従う。

## 付録 B. r3 の食い違い 11 件の処置

| # | 食い違い | C3 の処置 |
|---:|---|---|
| 1 | reset 経路 / released のみが未確認 | §4.5 へ r3 実データの確認済み事実を記載。§11.1 から未確認を削除 |
| 2 | ラッチした scheduler tick とした | §4.6、§9.1 を trade-loop → 次の tick 末尾へ訂正 |
| 3 | permanent-failure の出所が無い | §5.4 の terminal snapshot と reader 優先順位で定義。保存できない場合の限界も明記 |
| 4 | 全 signal を再起動とした | §5.6 で clean / 異常 signal を分割 |
| 5 | send 以外の決定的 4xx が transient | §5.2、§5.6 の共通 REST 分類と §9.2 の fetch / history fixture へ統一 |
| 6 | production に shell 未注入期間がある | §4.4 を起動前注入の事実に訂正。test の None は従来経路を維持 |
| 7 | 既存 trade-notices spec は guard_state_changed が reset だけと説明 | T4 で `docs/superpowers/specs/2026-10-20-trade-notices-design.md` の該当説明を更新する。現行 reconcile と data_feed、導入後の kill-switch sweep を区別する |
| 8 | チケットの producer best-effort / discord WARNING 案と設計が異なる | T4 で対象二チケットの処置案を単一 sweep / gateway 固定遷移 code + 生 logger 遮断へ更新する |
| 9 | runtime docstring と send 無言 retry が異なる | T2a で runtime docstring と実装を進捗監視・恒久拒否・一時再試行の契約へ合わせる |
| 10 | 孤児 temp 回収を毎書込みと説明 | §5.3 を新 process の (dir, name) ごとの最初の書込みと訂正 |
| 11 | 同じ rc の意味を全 process で共通視 | §5.6 の注で bot unit 入口に限定し、probe 等では別の意味であることを明記 |

#7〜#9 の既存文書・コードは本改訂では変更しない。上表は実装時の更新範囲であり、C3 を書いたことで既存リポジトリとの食い違いまで解消済みとは扱わない。

## 付録 C. 別チケット案

### C-1. `killswitch-latched-activity-repeats`

- 題名: 同一 kill switch 世代で繰り返される latch activity を一件にする
- 優先度: P2（本件の前提・release blocker ではない）

open intent を同一 latched 世代で評価するたびに記録される `kill_switch_latched` activity を実遷移一回に絞る。StateStore に原子的な latch API と changed 結果を設け、executor は changed 時だけ現行文面を記録し、intent ごとの gate_rejected は残す。replay の RecordingStateStore override と二重記録防止を同時に設計し、基準リビジョンの「ラッチ → 拒否反復 → 自動解除 → 再ラッチ」fixture を保存する。BacktestResult の kill_switch_events / kill_switch_latches、orders / fills、equity / account、PF / drawdown / 件数 / 改善 gate、解除時刻 / replay_ks_rebase を canonical byte 比較して互換性を確認する。通知 event の sweep とは独立に完了判定する。

### C-2. `killswitch-generation-floor-on-state-recreate`

- 題名: state 再作成時の kill switch 世代再利用を防ぐ
- 優先度: P3（state file 消失・時刻後退への追加耐性）

init / wizard が既存 state を引き継ぐ契約を維持し、state file が無い新規作成時だけ既存 ops_events の最大 kill switch generation を floor として用いる案を検討する。DB / table 無しは 0 とし、存在する table の読取失敗を新たな停止理由にする必要性と運用復旧手順を評価する。現行の latched_at 時刻照合で通常の通知重複は防げるため、本件の event 化には導入しない。時刻が古い event より前へ戻る状況と state 消失が重なる fixture で残余リスクと効果を確認する。

### C-3. `bot-discord-log-template-inventory`

- 題名: discord.py 内部ログを安全な固定 code に写像する保守契約を作る
- 優先度: P3（gateway 遷移通知に対する追加診断）

pinned discord.py 2.7.1 の logger name / record.msg template を棚卸しし、必要な診断だけを固定 code に写像する allowlist を作る。args、整形済み message、body、URL、exc_info、traceback、heartbeat blocked の動的 suffix は出さず、反復は bounded counter に集約する。依存更新ごとの inventory と秘密 canary を保守条件にする。本件では discord logger を閉じたまま gateway 状態機械の固定 code、root / lastResort / asyncio handler、task 例外回収で通知と秘密保護を満たすため、この棚卸しは前提としない。

R4 の既存別チケット `killswitch-reset-hwm-rebase` は引き続き対象外。本付録は起票案であり、外部チケットの作成・更新は実施していない。


## 付録 D. terra の C3 閉鎖確認の採否と理由

判定基準は `c3-close-terra.md`（Critical 0 / Important 3 / Minor 2）。指摘はすべて採用し、覆したものは無い。C4 は設計文書のみの改訂であり、下表の閉鎖は設計上の対処定義を意味する。実装・test 実行・実 manager 検証・チケット更新は未実施である。

| ID | 採否 | 反映先・理由 |
|---|---|---|
| I1 REST 網羅 | 採用 | §5.2 に adapter 内全 8 箇所の表と adapter 外 lifecycle を列挙。利用者一操作の defer / followup は有限応答として除外し、期限切れで bot を止めない。§9.2 / T2a に各分類と非致命・deadline test を追加。daemon API とローカル操作を混同しない |
| I2 404 回復 | 採用 | §5.2 / §5.6 の一律 404 恒久化を訂正。既知 message fetch / edit の 404 + 10008 のみ None / gone、10003・認証・権限拒否と未知 404 は恒久。§9.2 に起動時回復、pending 再投稿、terminal 完了と拒否の対照 test。既存消失回復を壊さず共有配送不能を停止できる |
| I3 lane 再入 | 採用 | §4.3〜4.4 の規約を instance / thread 所有検査で強制。wrapper 入口と共通 lane 取得前に即時拒否、private helper は所有を要求。§9.1 / T1b に取得・sleep を呼ばない test と例外後 / 別 thread の正常経路を追加 |
| Minor 1 XDG | 採用 | §5.4 / §9.2 / T2d / T4。「不一致なら unknown」は自動判別無しには保証不能。別 path の古い有効 snapshot を表示し得ることと、実効環境・解決 path・observed_at 更新の導入確認を明記 |
| Minor 2 fcntl | 採用 | §5.3 / §9.2 / T2b。F_GETFL で取得した flags に O_NONBLOCK を OR して F_SETFL。既存 flags を保持し、読取・設定失敗時も出力を捨てて exit へ進む |
| 完了条件縮小 | 採用 | §2.2 / T4。元チケットの「遷移ごとに 1 回」を満たす完全履歴保証ではないと明記。clear + event 無しを補わない境界を成功条件と残余リスクへ更新する作業を必須化。誤通知を避ける選択を完了条件の据置きとして扱わない |

terra が閉鎖済みとした他項目は再設計しない。server 認証拒否の rc 経路、systemd restart step / Requisite の実挙動、既存文書・コードとの食い違い #7〜#9 は C3 と同じく未確認又は実装時更新対象のまま残す。実機 Discord 応答と依存内部の経路については §5.2 の未確認を維持し、秘密情報を調査・転記していない。
