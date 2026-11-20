# データ停止・再開待ちの Discord 通知 設計 C0

作成日: 2026-11-21 JST。対象: `docs/tickets/open/datafeed-outage-discord-notice.md`。
基準: main `20d699da`。未承認の提案。今回は静的確認と本番 DB の readonly SELECT のみ。実装・test・配備はしていない。

## 利用者に届くもの

開場中のデータ障害が confirmed になったとき、取得が戻って人の再開操作を待つとき、実際に ready に戻ったときに、既存 bot が Discord の通知先へ知らせる。新規判断・新規リスクの停止判定は変更しない。通知を待って停止する仕組みにはしない。

一つの episode（永続 epoch）につき各種類一回、最大三件。短い restricted だけで自動復帰した episode は通知しない。confirmed 前に人の操作待ちになった場合は待ち通知を出す。未処理足の更新は送らない。通知は発生時の記録なので、遅れて届いた場合は SSH の端末で `afx ctl status` を確認する。

本文例（対象は例示。数字を含む足名は識別子として許す）:

| 契機 | 固定本文 |
|---|---|
| confirmed | データ停止を確認しました。監視対象: USDJPY / 1m。SSH の端末から `afx ctl status` で確認し、価格源の接続を復旧してください。復旧後の再開操作は次の通知で案内します。 |
| 再開待ち | データ取得の回復を確認しました。人の再開操作を待っています。監視対象: USDJPY / 1m。SSH の端末から `afx ctl status` を確認し、`afx ctl --as approver data resume [--acknowledge]` を実行してください。未処理の足がある建玉を確認して再開を承認する場合は `--acknowledge` が必要です。角括弧は入力しません。 |
| ready 復帰 | データ停止から復帰し、データによる停止を解除しました。監視対象: USDJPY / 1m。SSH の端末から `afx ctl status` を確認してください。他の停止条件は同画面で確認できます。 |

金額・DD%・件数・経過時間・epoch・event ID・建玉内訳・生のエラー・URL は本文に載せない。対象は障害原因を断定するものではなく、その episode の hard 監視対象である。soft 参照系列は含めない。既存 ops 通知の設定済み mention を利用し、追加 mention や承認ボタンは作らない。

`resume` は要求の記録であり、成功応答だけでは再開していない。次の開場中の観測 tick が全 hard key の取得確認と watermark を再検査し、未処理足がある OPEN/PENDING_FILL がなければ受理する。ある場合は `--acknowledge` による明示承認が必要で、これはデータ不健全を迂回する指定ではない。未処理足の判定は保守的な近似であり、SL/TP replay の完了を意味しない。拒否された要求は消費されるため、状態を直して再要求する。休場中の要求は開場まで残る（`outage.py:597-648`, `:669-718`, `:236-254`, `:280-285`。以下、同ファイルは `src/agentic_fx/datafeed/outage.py`）。

長時間の再開待ちへの定期再通知は、初版では行わないことを推奨する。取り逃した一通を補う価値はあるが、時間ごとの通知は根治せず、既存の配送 retry と status 強調でまず補う。episode の寿命によらず最大三件にでき、休場時間の累積も不要になる。将来採る場合の候補は「開場時間だけを累積して四時間ごと、追加二件まで」。休場中は送らず数えず、再起動でも累積と上限を保持する。この候補は今回の実装範囲に含めない。

## 現行の根拠

| 確認事項 | 現行コードの根拠 |
|---|---|
| 永続 state は ready / restricted / degraded。再開待ちは state 名ではない。confirmed は停止 gate と独立した通知用の二連続観測判定 | `outage.py:23-24`, `:117-120`, `:362-429` |
| flat の停滞は restricted を経る。失敗・空応答・exposure・期限超過などで degraded。pending は exposure または自動復帰無効時に立ち、保持される | `outage.py:308-351`, `:362-408` |
| 自動復帰は healthy・flat・auto_resume_when_flat・pending なしが必要。健全 streak の既定は三回 | `outage.py:103-105`, `:409-423` |
| 再開待ち activity の印は recovered_notified_epoch。confirmed、pending、epoch、resume 要求等は DB 永続。未処理内訳の前回値だけはプロセスメモリ | `outage.py:135-139`, `:421-441`; `src/agentic_fx/store/db.py:814-835` |
| activity は commit 後に SYSTEM として出す。未処理内訳が変わると再記録し、再起動でも再記録し得る。activity 書込み自体は best effort | `outage.py:238-254`, `:659-661`; `src/agentic_fx/activity.py:70-87` |
| 休場中は観測・resume 消費へ進まない。停滞式は market_hours の次の足確定時刻を使う | `outage.py:236-237`, `:280-285`, `:557-569`; `src/agentic_fx/core/market_hours.py:77-88` |
| ingest commit → observe → scheduler の順で、失敗は保護処理から隔離する | `src/agentic_fx/service.py:2512-2536` |
| kill switch は ops lane 内で既存 event と照合する sweep。起動時・tick 後に共通回収し、improve 無効でも動く | `src/agentic_fx/ops/service.py:2016-2104`; `src/agentic_fx/service.py:2554-2563` |
| EventStore.emit は自分で commit する。best effort の失敗は rollback して失敗数を保持する | `src/agentic_fx/ops/events.py:150-197` |
| data resume API の既存 event は要求受付だけ。ready 受理とは別 | `src/agentic_fx/ops/service.py:1754-1767`; `src/agentic_fx/service.py:2247-2258`; `afx-ops/afx_ops/gate/render.py:453-459` |
| API の data は state のみ。ctl はその一語、web は既存の警告カードを出す。対話 shell は gap・未処理内訳・要求ありを既に表示 | `src/agentic_fx/service.py:2180-2225`; `src/agentic_fx/ops/client.py:779-781`; `afx-ops/afx_ops/web/render.py:240-278`; `afx-ops/afx_ops/web/marks.py:48-49`; `src/agentic_fx/commands.py:715-760` |

既存の裁定は [運用異常通知設計](2026-11-14-ops-alert-delivery-design.md) §1・§2・§4〜6 を継承する。作動時に知らせ、DD% を出さず、独立 notify は任意のまま。元の [停止・backfill 設計](2026-09-24-outage-stop-and-backfill-design.md) の将来 replay 構想を、現在すでに動く機能とは扱わない。

2026-11-21 の readonly SELECT では `state=degraded, epoch=66, confirmed=1, pending_human_confirmation=1, recovered_notified_epoch=66`、resume 要求ありだった。チケットの「11-21 08:24 JST の要求後、開場最初の tick で ready」と整合する待機状態であり、今回 ready への遷移を観測したわけではない。

## 通知の記録と経路

### 方式の選択

activity の tail は自由文の解析と再起動重複を招くため採らない。kill switch と同じ「現 state だけを outbox とする sweep」は最小だが、ready で confirmed が消え、次 epoch で待ちの印が置き換わるため全三種類の回収には不足する。**小さな遷移記録を state と同時に保存し、ops lane の sweep が event 化する**案を推奨する。ネットワーク処理は既存 bot のみ。追加の通知 process は不要。

### 発生側

新表 `datafeed_outage_notices` を追加する。主キー `(epoch, notice_state)`、列は `occurred_at`（UTC）、`targets`、`target_scope`、`action`。state は `confirmed / recovered_awaiting_resume / resumed` の三語に限定する。送信済み列は持たず、対応する ops_events の存在を照合する。自動削除は導入しない。

- confirmed の 0→1 を、その `_observe_open` の state/gap transaction 内で記録する。
- 再開待ちは「healthy かつ degraded で、自動 ready の条件が成立しない」ときに一度記録する。既存の activity 条件だけでなく、sticky な pending が残ったまま flat になったケースも含む。停止 gate・auto_resume_when_flat・pending の解除条件は変えない。既存の `recovered_notified_epoch` と activity 条件もこの定義に揃える。
- resumed は degraded→ready の自動復帰、および `_consume_resume_request` による非 ready→ready の受理を、その更新 transaction 内で記録する。ただし同 epoch に confirmed または待ち記録がある場合だけ。restricted→ready だけでは作らない。
- 確認なしで既に ready の要求消費、resume 拒否、unprocessed_bars、restricted の開始は通知記録にしない。
- 対象集合は episode 開始時の hard_keys を gap と共に固定し、その後設定が変わっても同じ集合を使う。旧 episode は gap の pair/interval を使う。対象不明なら固定語 `unknown`。健全であっても設定内の別 pair が原因とは断定しない。

通知記録の INSERT は commit しない primitive にする。`EventStore.emit` を observe の transaction から呼ばない。DB の state/記録 transaction が失敗した場合は既存の observe 失敗時と同じく新規リスクを停止する。資金保護は継続する。成功済みの state を、後段 event 化・Discord の失敗で巻き戻さない。

### ops_events の契約

新 code `datafeed_outage_changed`、ref `outage:<epoch>`（正整数、最大18桁）を使う。guard_state_changed の既存三 field は増やさない。

```json
{
  "code": "datafeed_outage_changed",
  "ref": "outage:66",
  "fields": {
    "epoch": 66,
    "state": "recovered_awaiting_resume",
    "targets": "USDJPY:1m",
    "target_scope": "listed",
    "action": "resume_check_ack"
  }
}
```

fields は上記の五つのみ。epoch は ref と一致する整数（bool 不可）、state は三語、action は `restore_source / resume_check_ack / check_status` を state と一対一にする。targets は canonical な pair と有効な interval の組をソート・重複除去してカンマ結合する。最大16組かつ512 ASCII文字までの先頭集合に制限し、残りがあれば `target_scope=additional`、なければ `listed`。本文では additional を「ほかの監視対象あり」と描画し、分割投稿しない。unknown は単独のみ許可する。自由文・エラー・秘密を渡せない専用 validator と daemon/afx-ops 共通 golden を用意する。

現行の汎用値検査は64文字まででカンマも通さないため、この code 専用の検査を両 package に追加する（`src/agentic_fx/ops/events.py:22-107`; `afx-ops/afx_ops/notify/payload.py:14-85`）。event の整数は内部照合に使い、Discord 本文には表示しない。ack の要否を過去の建玉状態から断定する field は作らず、常に status で現在を確認させる。

### sweep・重複抑止

`OpsService.emit_pending_notices` に datafeed 専用 helper を別 try で加える。起動時と tick 末尾の既存回収点を使い、improve 設定・kill switch 成否から独立させる。observe の lock 区間から ops lane を取得しない。

helper は ops lane 内で遷移記録を読み、`(code, ref, fields.state)` が同じ event がなければ INSERT する。複数接続でも重複を防ぐため、新 code に限定した `(ref, json_extract(fields_json,'$.state'))` の UNIQUE index を migration で作る。旧 code は影響させない。失敗後は rollback して次 tick/再起動に再試行し、成功を記憶するプロセスメモリは使わない。event.ts は発生時刻を下回らない。

回収順は発生時刻、epoch、同時刻なら confirmed→待ち→resumed。一回の sweep の回収件数は既存 poll 上限以下の有限 batch とし、残りは次 tick へ送る。遷移記録が残るので、event INSERT 前に ready や次 epoch へ進んでも欠落しない。DB 自体の巻戻し・消失、将来の retention は別問題であり、復旧時に重複/欠落し得る。

### bot・notify・導入順

gate の検証・描画・通知対象判定に新 code を足す。通常の ops 通知で三種類とも配送する。既存 `data_feed/resume_requested` の Discord 表示は要求受付であるため、二重の「再開」通知にしないよう投稿対象から外す（event と API 応答は維持）。`ops_notices=all` でもこの受付 event は投稿しない。操作監査の terminal event は既存設定どおりで、この三件上限とは別の利用者操作通知である。

bot は既存 event_id cursor、投稿後の保存、一時失敗 retry、恒久失敗時の停止、health 表示を再利用する（`afx-ops/afx_ops/gate/service.py:112-167`; `afx-ops/afx_ops/bot/adapter.py:809-819`, `:841-898`, `:971-989`）。notify の payload 検証にも新 code を追加するが、独立 process の設定は任意とし、未設定でも live 起動を妨げない。代替 push が無い旨の既存表示を維持する。

初回 bot/notify は過去全件を送らず high watermark へ cursor を置く。したがって導入は「consumer の新 code 対応と起動 → cursor 初期化の確認 → producer の有効化と backfill」の順にする。既存 cursor は消さない。新 code を未知として拒否する旧 consumer が残った状態では producer を有効化しない。初回 cursor 初期化より前に event を作った場合は自動配送を保証できないため、導入受入でこの順序を必須にする。専用の外向き backfill API は新設しない。

配備直後、現在の epoch が degraded かつ confirmed なら停止記録を一回補い、recovered_notified_epoch が現 epoch なら待ち記録も一回補う。現状の例では最大二件になる。一通だけへ潰すより、通常運転と同じ重複キーを使えることを優先する。待ちの印は過去の回復観測なので、現在も健全であるとは断定しない。confirmed 前の degraded は confirmed または待ちの実観測まで通知せず、ready の過去 episode は補完しない。backfill は新表と既存 event の同じキーで再実行しても増えない。休場中でも既存の停止/待ち記録は配送する。新たな観測・再開判定は行わない。

rollback は producer を先に無効化し consumer は新 code を読める版に残す。加算した表・index・未配送 event を削除しない。

## status で次の操作を見せる

API の `data` に、既存 state を保ったまま `phase`（`ready / restricted / stopped / awaiting_resume / resume_requested / unknown`）、`next_action`（`none / restore_source / resume_check_ack / wait_tick / check_status`）、上記と同じ targets/target_scope を追加する。読み取り専用 snapshot で生成し、配送済みかどうかを phase 判定に使わない。

再開待ちの印は一度回復した履歴で、現在の健全性とは限らない。表示は「再開操作待ち（取得状態を再確認してください）」とする。要求が残れば「再開要求を記録済み・次の開場中 tick の判定待ち」を優先する。observe_failed は unknown と新規リスク停止を強調し、ready と誤認させない。再開要求/待ちの区別には既存 DB 情報を用い、新しい health 判定器は作らない。

`afx ctl status` では data 行を停止理由と次の操作の二行にし、web Status 概要でも data カードの直下に同じ固定案内を常時表示する。コマンドは SSH 端末から実行する表示だけにし、web/Discord の実行ボタンは足さない。建玉詳細は外部通知に出さず、ローカルの既存確認手段を使う。新 field を持たない旧 API では既存 state 表示に戻し、不明を ready 扱いしない。

## 失敗時と通信量

| 失敗 | 動き |
|---|---|
| activity の失敗 | 通知の発生記録・event 化に使わないため配送判断に影響しない |
| 通知用遷移の DB commit 失敗 | state 更新も未確定。既存 observe の fail closed と次 tick 再判定。通知用に取得・発注処理を追加しない |
| ops lane timeout / event INSERT 失敗 | 遷移記録を保持し再試行。固定 code `datafeed_notice_deferred` のみ。state と保護処理は成功済みのまま |
| Discord 一時不通 / bot 停止 | cursor を先へ進めず、既存 retry / watchdog / restart を利用。遅延配送になる |
| 投稿成功後 cursor 保存前の crash | 同一 event の再投稿があり得る。event 生成は一件でも表示の exactly-once は保証しない |
| 恒久拒否 / 未設定 / 初回 cursor の誤った導入順 | status で判別。無通知の可能性を隠さず、取得の再開条件とは切り離す |

追加する論理 event は最大三件/epoch、既存 bot の poll 間隔・接続数・bridge 呼出しを増やさない。pair ごとに分割せず、定期再通知も外部状態照会も追加しない。notify を設定した場合はその配送先にも最大三件。これは成功する通常配送の件数上限であり、一時失敗 retry と投稿後 crash を含む物理 HTTP 試行回数の厳密な上限ではない。厳密な有限試行上限は通知を捨てることになるため採らない。

## test と受入

この文書作成では test を実行しない。実装時に以下を fixture（fake clock、tmp SQLite、偽 ingest/Discord transport）で検証する。

1. failure→degraded→二連続で confirmed→healthy で待ち→resume 受理で ready。三 event の固定本文・対象・順序を golden 比較し、数値/金額/生エラーの canary が出ないこと。
2. unprocessed_bars が足ごとに変化しても event は増えない。再起動、sweep 重複、二接続の競合で各キー一件。event INSERT 前に次 epoch へ進めても旧三件を回収できる。
3. state/記録 commit の前後、event commit の前後、Discord 成功と cursor 保存の間に fault を入れる。rollback と再試行、表示重複を許す窓を区別する。EventStore の内部 commit が observe の transaction に入らないこと。
4. pending を立てた後に flat になり auto_resume_when_flat=true でも、healthy なら待ちを記録する。自動復帰する flat は confirmed→resumed の二件、restricted のみの自動復帰はゼロ、confirmed 前の手動待ちは待ち→resumed の二件。
5. acknowledge なしの未処理足あり、ack ありでも watermark 不健全、受理、拒否後の再要求、ready 中の要求を網羅する。受付通知を ready 通知と誤認しないこと。
6. 休場中に観測・要求消費・streak が進まない。要求は開場後に判定される。導入時の既存通知は休場中も一回配送。再通知時計や外部リクエストは増えない。
7. 今回の readonly snapshot 相当から migration/backfill を繰り返し、停止と待ちが各一件。ready の過去履歴は補わない。cursor 初期化→backfill で届き、逆順では skip する既存境界も確認する。
8. schema の未知 field、巨大/不正 targets、epoch/ref 不一致、bool、不正 state/action を両 package で拒否。対象が多くても追加本文一通に収まり、soft 系列を混ぜない。
9. ctl/web で ready、stopped、待ち、要求あり、observe_failed、旧 API を比較する。bot/notify 未設定・一時不通・恒久停止でも status は使え、通知設定を起動 gate にしない。

可能なら Dunia の隔離 clone・専用 tmp DB/設定・fake 価格源・専用 Discord 試験 channel で、三通と次操作表示、再起動後の重複抑止、consumer 先行導入を確認する。実 Discord は通常三通に限り、fault/retry の多数試行は偽 transport で行う。Dunia を利用できなければ fixture の結果と未実施理由を残す。本番 process を止める、MT5 を切断する、本番 DB/ログを書き換える受入は行わない。

## task

順に積み上げられ、各行を単独で merge できる。実装時の既存文書訂正も各 task に含める。

| task | 範囲 | 単独 merge の境界 |
|---|---|---|
| T1 | daemon/afx-ops の event schema、専用 validator、固定描画、通知選別、golden | producer 未有効でも旧 event を扱える consumer を先に出せる |
| T2 | 遷移記録表・migration・transaction 内の記録、sticky pending の待ち記録補完 | ops event の producer はまだ無効。状態・記録 fixture だけで検証できる |
| T3 | ops lane sweep、限定 UNIQUE index、起動 backfill、既存 resume 受付投稿の抑止、導入/rollback 手順 | T1/T2 の後で通知を有効化。fault/cursor/上限 fixture と隔離受入で完了 |
| T4 | API の data 追加情報、ctl/web の待ち・要求・次操作表示 | T2 の後に独立 merge。通知有効化前でも旧 API fallback を含め使用可能 |

## 利用者に問う事項

技術条件は上記で決め、利用体験の変更だけを確認事項とする。今回は回答を待たず推奨を書いた。

| 問い | 推奨と理由 |
|---|---|
| Q1: チケットの最大二通に ready 復帰の一通を加えてよいか | 最大三通を推奨。要求受付ではなく実際の解除を確認でき、停止が終わったかを見落とさない |
| Q2: 長期の再開待ちを定期再通知するか | 初版はなしを推奨。既存 retry と status 強調を先に使い、episode の通信量を固定する。必要なら前述の開場四時間・追加二件を別改訂で採る |

導入時の active episode 補完、SSH 操作、任意 notify、金額非表示は依頼と既存裁定で決まり、改めて問いにしない。

## 付録: 文書・前提と現行コードの食い違い

今回参照した資料のうち、この通知と停止/再開の判断に関係する相違を列挙する。旧設計の当時の「現物」分析は当時の記録であり、現行機能とは区別する。今回、参照元は変更しない。

| # | 文書・前提 | 現行の実体と本案での扱い |
|---|---|---|
| 1 | チケット末尾は confirmed + 待ちの二回 | ready 通知は未実装。本案は最大三回への変更提案（Q1）。現行は `outage.py:358-360`, `:628-632` の activity のみ |
| 2 | 未処理足は「毎分の記録」 | 時間駆動ではなく内訳が変化した時だけ。再起動でも出得る（`outage.py:135-139`, `:245-251`）。どちらも通知しない |
| 3 | recovered_awaiting_resume を state と読む前提 | DATA_STATES に存在せず degraded の activity/epoch 印（`outage.py:23-24`, `:421-423`）。通知 state と停止 state を区別 |
| 4 | 9/24 設計 §3.1 は失敗/停滞で即 degraded | 現在は flat の停滞に restricted がある（`outage.py:362-408`）。これだけでは通知しない |
| 5 | 9/24 設計 §3.1 は手動復帰、将来 backfilling/replay | 現在は flat の条件付き自動 ready があり、backfilling は未実装（`outage.py:3-9`, `:409-418`）。replay 完了を本文で約束しない |
| 6 | 9/24 設計 §3.1 の停滞式は watermark + 二足幅 + grace | 現在は休場を飛ばす next_bar_confirmation（`outage.py:557-569`; `market_hours.py:77-88`）。旧設計の単純な壁時計時間を再通知等へ流用しない |
| 7 | 9/24 設計 §3.1 は watermark がない gap 起点を必要本数から計算 | 現行は now を仮置き（`outage.py:514-535`）。本件では起点を直さず監視対象だけ使う |
| 8 | pending が立っていれば健康回復後に必ず人へ待ちを知らせる想定 | pending 自体が自動復帰を防ぐのに、flat かつ auto=true では既存の待ち activity 条件に入らない（`outage.py:351`, `:410-423`）。本案は待ちの記録条件だけ補完 |
| 9 | 待ちの印があれば現在もデータ健全と読む前提 | recovered_notified_epoch は再悪化しても同 epoch 中に消えない（`outage.py:365`, `:421-441`）。遅延通知/導入通知と status に再確認を明記 |
| 10 | ctl/web で再開待ちを認識できる想定 | API data は state だけ、ctl は一語、web は state の警告カード。shell のみ詳細あり（現行の根拠表参照）。T4 で案内追加 |
| 11 | 既存 ops event を使えば停止/復帰も既に配送できる想定 | code は未定義、data_feed は resume_requested のみ（`events.py:22-51`; `ops/service.py:1754-1767`; `gate/render.py:459`）。T1/T3 で追加し受付と解除を分離 |
| 12 | kill switch sweep の現 state だけで三遷移を再起動越しに保証できる想定 | ready で confirmed を消し、次 epoch で待ち印を消す（`outage.py:363-365`, `:413-418`, `:619-626`）。11/14 設計 §4.6 も完全履歴を保証しない。本案は永続遷移記録を追加 |
| 13 | event を補えば初回 consumer にも必ず届く想定 | 初回正常 poll は末尾へ cursor 初期化して配送しない（`bot/adapter.py:814-819`; `notify/runtime.py:144-148`、いずれも `afx-ops/afx_ops/` 配下）。導入順で対処 |
| 14 | 「再起動をまたいでも一回」を Discord 表示の厳密保証と読む前提 | 既存配送は投稿後 cursor 保存（`bot/adapter.py:875-898`, `:971-977`）。11/14 設計 §2.1 と同じ at-least-once を継承し、生成一件と表示重複の窓を区別 |

## 変更履歴

| 日付 | 版 | 変更 | 理由 | commit |
|---|---|---|---|---|
| 2026-11-21 | C0 | 停止・待ち・実再開の固定通知、永続遷移と sweep、導入補完、status 案内、上限と受入を提案 | 再発時の長時間停止と再開操作の見落としを防ぐ | —（未 commit） |
