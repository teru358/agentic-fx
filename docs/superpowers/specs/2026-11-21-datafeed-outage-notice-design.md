# データ停止・再開待ちの Discord 通知 設計 C1 (2026-11-21 利用者承認)

作成日: 2026-11-21 JST。対象: `docs/tickets/open/datafeed-outage-discord-notice.md`。
基準: C0.1（commit `c71ed55e`）。静的確認、本番 DB の readonly SELECT、既存ログの読取りに基づく改訂。実装・test・配備はしていない。

## 利用者に届くもの

監視対象の価格系列でデータの欠落を検知して停止が続くとき、取得が戻って人の再開操作を待つとき、再開要求を記録したとき、その要求の判定が終わったとき、実際に ready に戻ったときに、既存 bot が Discord の通知先へ知らせる。新規判断・新規リスクの停止判定は変更しない。通知の失敗で停止を長引かせない。

一つの episode（永続 epoch）につき停止・待ち・復帰は各一回、受付は新規記録ごと、終端結果（受理・拒否・既に解除済み）は消費した要求ごとに送る。照会や ack 変更では受付を増やさない。同じ epoch で待ちを記録した後は停止通知を作らない。短い restricted だけの自動復帰と未処理足の更新は送らないが、要求への終端結果は返す。通知は発生時の記録なので、遅れて届いた場合は SSH の端末で `afx ctl status` を確認する。

| 契機 | 固定本文 |
|---|---|
| 停止確認 | 監視対象の価格系列でデータの欠落を検知しました。SSH の端末から `afx ctl status` で取得状態を確認してください。復旧後も同画面で再開操作の要否を確認してください。 |
| 再開待ち | 監視対象の価格系列でデータ取得の回復を確認しました。人の再開操作を待っています。SSH の端末から `afx ctl status` を確認してください。案内が通常の再開なら `afx ctl --as approver data resume` を実行してください。未処理の足がある建玉の確認が必要と出た場合は、建玉を確認し、再開を承認するときだけ `afx ctl --as approver data resume --acknowledge` を実行してください。 |
| 要求受付（epoch 付き） | 再開要求を記録しました。次の開場中の判定で結果を通知します。 |
| 要求受付（generation=None の互換本文） | データ取得の再開を要求しました。 |
| 要求受理 | 再開要求を受理しました。SSH の端末から `afx ctl status` を確認してください。 |
| 既に解除済み | 既に停止は解除されています。SSH の端末から `afx ctl status` を確認してください。 |
| 要求拒否 | 再開要求は受理されませんでした。SSH の端末から `afx ctl status` で理由を確認し、必要なら再要求してください。 |
| ready 復帰 | 監視対象の価格系列のデータ停止から復帰し、データによる停止を解除しました。SSH の端末から `afx ctl status` を確認してください。他の停止条件は同画面で確認できます。 |

金額・DD%・件数・経過時間・内部識別子・建玉内訳・生のエラー・URL は本文に載せない。原因や接続障害を断定せず、系列は「監視対象の価格系列」の固定語にする。既存 ops 通知の設定済み mention を利用し、追加 mention や承認ボタンは作らない。

`resume` は要求の記録であり、成功応答だけでは再開していない。次の開場中の観測 tick が全 hard key の取得確認と watermark を再検査し、未処理足がある OPEN/PENDING_FILL がなければ受理する。ある場合は `--acknowledge` による明示承認が必要で、データ不健全は迂回できない。未処理足の判定は保守的な近似であり、SL/TP replay の完了を意味しない。拒否された要求は消費されるため、状態を直して再要求する。休場中の要求は開場まで残る（`src/agentic_fx/datafeed/outage.py:236-254`, `:280-285`, `:597-648`, `:669-718`。以下、`outage.py` は同ファイル）。

長時間の再開待ちへの時間駆動の再通知は初版では行わない（裁定済み）。受付と終端結果で操作への返事を残し、配送 retry と status の案内を使う。停止確認の時間条件は未通知の停止を一度知らせるための条件であり、再通知ではない。

## 現行の根拠

以下は改修前の実体。短縮名 `events.py` は `src/agentic_fx/ops/events.py`、`market_hours.py` は `src/agentic_fx/core/market_hours.py` を指す。

| 確認事項 | 現行コードの根拠 |
|---|---|
| 永続 state は ready / restricted / degraded。再開待ちは state 名ではない。confirmed は停止 gate と独立した二連続の問題観測 | `outage.py:23-24`, `:117-120`, `:425-431` |
| flat の停滞は restricted を経る。失敗・空応答・exposure・期限超過などで degraded。pending は exposure または自動復帰無効時に立ち、保持される | `outage.py:308-351`, `:362-408` |
| 自動復帰は healthy・flat・auto_resume_when_flat・pending なしが必要。健全 streak の既定は三回 | `outage.py:103-105`, `:409-423` |
| confirmed、pending、epoch、再開待ちの印、resume 要求は永続。直近の拒否結果を残す列はない | `src/agentic_fx/store/db.py:814-835`; `outage.py:633-655` |
| 未処理内訳の前回値はプロセスメモリ。変化時に activity へ再記録するので、足の近似値が増える健全時は実質毎分となり、再起動でも出得る | `outage.py:135-139`, `:245-251`, `:679-706`; `logs/activity.log:15631-15669` |
| activity は state commit 後に書き、書込みは best effort | `outage.py:230-254`, `:659-661`; `src/agentic_fx/activity.py:70-87` |
| 休場中は観測・resume 消費へ進まない。停滞式は休場を飛ばす次の足確定時刻を使う | `outage.py:236-237`, `:280-285`, `:557-569`; `market_hours.py:30-43`, `:77-88` |
| ingest commit → observe → scheduler の順。observe 失敗でも保護処理へ進む | `src/agentic_fx/service.py:2512-2536` |
| 改善 outbox は終端 snapshot と event を照合し、期間下限を設け、不正な一件を skip する。遷移記録の前例はこちらを使う | `events.py:199-211`; `src/agentic_fx/ops/service.py:1489-1505` |
| kill switch は ops lane 内で event を照合。共通回収は起動時・tick 後に呼び、improve の設定と失敗から隔離する | `src/agentic_fx/ops/service.py:2031-2104`; `src/agentic_fx/service.py:2554-2563` |
| EventStore.emit は自分で commit する。best effort は失敗時 rollback し失敗数を保持する | `events.py:150-197` |
| 未消費要求は単一行の時刻・ack を上書きする。ready 消費は要求を消すだけで結果を残さない | `outage.py:460-467`, `:599-608` |
| 観測で epoch が増えてから要求を消費する。消費時の全 hard key と現 epoch の未処理建玉を再検査する | `outage.py:254`, `:327-329`, `:362-365`, `:609-627` |
| guard_state_changed は三 field と ref=instance を使用。data_feed の既存 event は resume_requested だけ | `events.py:27`, `:45`, `:101-107`; `src/agentic_fx/ops/service.py:1754-1768`; `afx-ops/afx_ops/gate/render.py:453-460` |
| API data は state のみ、ctl は一語、web は警告カード。shell に詳細はあるが daemon 運用では shell を起動しない | `src/agentic_fx/service.py:2221-2225`, `:3050-3052`; `src/agentic_fx/ops/client.py:779-781`; `afx-ops/afx_ops/web/render.py:240-278`; `src/agentic_fx/commands.py:715-760`; `deploy/afx.service.direct:12` |

### 実測と今回の停止

`logs/activity.log:12671-15530` の epoch 52〜66 は15 episode。そのうち14件が22時台 UTC の1m停滞で、残る1件は epoch 57 の12時台だった。ロールオーバー前後の反復と整合するが、無 tick の原因はこのログだけでは断定しない。flat なら自動復帰し得る一方、建玉を持ち越す場合は即 degraded・pending となるため、人手の再開が繰り返し必要になり得る（`outage.py:351`, `:373-398`, `:410`）。資金保護の規則自体は本件で変更しない。

| UTC | epoch 66 の実測 | 根拠 |
|---|---|---|
| 11-19 22:40 | 建玉保有中に1m停滞、即 degraded | `logs/activity.log:15524`, `:15530`; `outage.py:351`, `:373-375` |
| 11-19 22:41 | 健全な観測で待ちの印が立つ | `logs/activity.log:15531`; `outage.py:421-423` |
| 11-20 00:56 | 建玉を決済。その後も pending が自動復帰を防ぐ | `logs/activity.log:15670`; `outage.py:410` |
| 11-20 04:19 | 再起動後の二連続の問題観測で confirmed。待ちの印は同 epoch に残る | `logs/activity.log:15687`; `logs/agentic.log:72829-72839`; `outage.py:120`, `:421-431` |
| 11-20 04:23 | 再起動後、時刻規則未検証で取得が deferred | `logs/agentic.log:72844-72846` |
| 11-20 23:24 | acknowledge 付き要求を記録。休場中なので未消費 | `logs/activity.log:15771`; DB の要求時刻と acknowledge |

2026-11-21 に `sqlite3 -readonly data/agentic.db` で確認した行は `state=degraded, epoch=66, confirmed=1, pending_human_confirmation=1, recovered_notified_epoch=66, resume_requested_at=2026-11-20T23:24:09.461457+00:00, resume_acknowledge=1`。要求は記録済みで、開場最初の tick で判定予定。ready 復帰は未観測である。過去の `data_resume_accepted` は epoch 1・2 の開場中の2件だけ（`logs/activity.log:1420`, `:1620`）。

既存の裁定は [運用異常通知設計](2026-11-14-ops-alert-delivery-design.md) §1・§2・§4〜6 を継承する。作動時に知らせ、DD% を出さず、独立 notify は任意のまま。[停止・backfill 設計](2026-09-24-outage-stop-and-backfill-design.md) の将来 replay 構想は現行機能と扱わない。

## 通知の記録と経路

### 方式の選択

小さな永続遷移記録を ops lane の sweep で event 化する。activity の tail や自由文解析は使わない。ready で confirmed が消え、次 epoch で待ちの印が置き換わるので、現 state だけの sweep では足りない（`outage.py:363-365`, `:413-418`, `:619-626`）。ネットワーク処理は既存 bot に任せ、追加 process は不要。

state/gap の確定と通知記録は別 transaction にする。通知記録を失敗しても成功済みの状態遷移を巻き戻さず、observe_failed を立てない。この分離では state commit 後・記録前の crash に欠落の窓がある。現在状態と直近の判定結果から補える分だけ再試行し、完全な履歴保証はしない。

### 発生側

新表 `datafeed_outage_notices` は主キー `(epoch, notice_state, request_key)`、列は `occurred_at`（UTC）と nullable な `event_id`。通知状態は `stopped / awaiting_resume / resume_accepted / resume_rejected / resume_already_ready / resumed`。`request_key` は終端結果の三種類では消費対象の `resume_requested_at`、他は空文字とする。要求ごとのキーは要求記録時に衝突しない UTC 時刻にする。受付は既存の操作経路の event を使い、この表へ二重に記録しない。

`datafeed_outage_state` に `resume_last_result`（`accepted / rejected_unprocessed / rejected_watermark / already_ready` または NULL）、判定時刻 `resume_last_result_at`、対象要求時刻 `resume_last_request_at` を加える。判定結果は要求消費・状態更新と同じ transaction に残す。両方の拒否条件に該当した場合は rejected_watermark を優先する。新要求は過去の結果を上書きせず、要求ありの表示を優先し、新 epoch では結果をリセットする。既に ready の要求消費も `already_ready` と判定時刻・対象要求時刻を確定して消去する。解除遷移は作らず、要求への終端結果を記録する。

要求記録は read→判定→更新を同じ書込み transaction 内で行い、tick の消費との競合でも一つの未消費要求だけを扱う。ctl と shell は共通の結果区分を使う（現状の上書きは `outage.py:460-467`）。

| 未消費要求 | 操作 | 応答と記録 |
|---|---|---|
| なし | resume（ack の有無を問わない） | 新しい要求時刻を記録し、受付を一件作る |
| あり | 同じ ack、または ack ありに対する ack なし | 「既に記録済み・開場判定待ち」。時刻・ack を保ち、新しい受付を作らない。ack を取り消さない |
| ack なしであり | ack あり | 新しい要求時刻・ack ありへ置換し、「先の要求を ack 付き要求に置き換えました。開場判定待ち」と応答する。追加の受付は作らない。先の要求への別の結果通知は作らず、消費された一件だけに終端結果を出す |

新規記録・照会・置換の結果と記録時の epoch を呼出し元へ返し、受付の有無と generation を確定する。事前の gap_summary だけから新規受付と判断しない（現状は `src/agentic_fx/service.py:2252-2258`; `src/agentic_fx/ops/service.py:1763-1764`）。

要求の記録時と消費時で epoch が変わっても、要求は消さず現行と同じく消費時の epoch に対して再検査する。受付の generation は記録時、終端結果と停止解除の generation は消費時の epoch。対応は要求時刻で保持し、世代番号の一致から受理を推定しない。ready 中の要求の後に新しい停止が始まった場合も、非 ready なら現 epoch の取得確認・watermark・未処理建玉を再検査し、ack があっても不健全なら拒否する。消費時も ready の要求と、観測が先に自動 ready に戻した要求は `already_ready` とする（`outage.py:254`, `:327-329`, `:362-365`, `:604-627`）。ack の効力と資金保護の判定条件は変更しない。

記録の条件は次のとおり。

- 停止: degraded で confirmed が立ったとき、または `entered_degraded_at` から開場時間で N 分（初版の既定10分）が過ぎても待ち・ready にならないとき。一度だけ記録する。confirmed 列の意味は変えない。待ちの記録または同 epoch の待ちの印があれば、後から停止を記録しない。再開の要求が未消費の間は、人が操作中なので時間条件の停止を記録しない。要求への結果が出たら、その epoch でまだ停止を知らせていないときだけ、その判定時刻から開場時間 N 分を数え直し、confirmed に届かず回復もしなければ停止を一度記録する (既に停止を知らせた epoch には時間による再通知をせず、結果の通知で足りる)。
- 時間条件: `market_hours.is_market_open` と日次境界を用いて UTC の区間内の開場時間だけを積算する（`market_hours.py:30-61`）。永続の開始時刻から再計算し、休場・祝日を除き、再起動で時計を戻さない。判定は開場中の既存 tick だけで行う。
- 待ち: healthy かつ degraded で、自動 ready の条件が成立せず、要求も未記録のときに一度記録する。pending が残って flat になったケースも含む。`recovered_notified_epoch` と activity の条件もこの定義に揃える。要求中は待ちを促さず、既存の待ちの印を消さない。停止 gate・自動復帰・pending 解除条件は変えない。
- 終端結果: 消費した要求ごとに受理・拒否・既に解除済みのいずれか一件を記録する。要求時刻と固定結果を確定済みの判定結果から渡す。拒否後の再要求は別のキーになり、再度通知できる。ready 中の要求と、消費前に自動 ready へ戻った要求にも「既に停止は解除されています」と返す。
- 復帰: degraded→ready の自動復帰は同 epoch に停止または待ち記録がある場合。手動の非 ready→ready も実際の停止解除として一度記録する。要求受理とは別の記録・通知であり、同時なら受理→復帰の順とする。restricted だけの自動復帰は記録しないが、要求があれば既に解除済みの返事は出す。

通知記録の書き手は commit 後の専用 helper 一つとし、起動補完も同じ helper を呼ぶ。state/gap と要求消費の transaction を抜けた後に、固定語・整数・要求時刻だけを `INSERT ... ON CONFLICT DO NOTHING` で保存する。通知用接続で短い transaction を所有し、例外は rollback して固定語 `datafeed_notice_deferred` をログへ残す。通知の例外を observe の失敗へ伝播させない。外向き payload の検査と描画は sweep/consumer 側で行う。

記録失敗後は次 tick の現在状態・直近判定から再試行する。状態が先へ進んだ場合や、失敗中に複数の判定結果が上書きされた場合は、過去の全遷移までは復元できない。通知表には自動削除を設けない。

### ops_events の契約

既存の `guard_state_changed`、ref=`instance`、三 field を使う。`generation` は data_feed のとき epoch を表す。

```json
{
  "code": "guard_state_changed",
  "ref": "instance",
  "fields": {"guard_kind": "data_feed", "state": "awaiting_resume", "generation": 66}
}
```

state は `stopped / awaiting_resume / resume_requested / resume_accepted / resume_rejected / resume_already_ready / resumed`。追加の field は設けない。既存の検査をそのまま通る（`events.py:27`, `:45`, `:101-107`; `afx-ops/afx_ops/notify/payload.py:22`, `:36`, `:54-85`）。有効化後の生成側はこの固定語と非負整数だけを渡す。互換受付の generation=None も既存の検査を通る。旧 consumer も既知 code として汎用文で扱える（`afx-ops/afx_ops/gate/render.py:453-460`）。data_feed の描画に上の固定本文を追加し、内部整数は本文に描画しない。

受付 event は既存 `resume_data` の操作経路で、新規記録の場合だけ残す。照会・ack 置換は応答と既存操作監査だけに残す。有効化前は既存の generation=None と互換本文を保ち、結果通知を約束しない（`src/agentic_fx/ops/service.py:1760-1765`; `afx-ops/afx_ops/gate/render.py:459`）。consumer は generation=None と非負整数を描き分ける。通知記録と status の先行配備では受付の generation を変更しない。producer の有効化と同時に受付を要求時の epoch へ切り替え、この受付だけ「結果を通知します」と案内する。新たな field は足さない。受付・終端結果・実際の停止解除を区別する。bot の通常通知・all と notify で同じ固定本文を使う。操作監査の terminal event は既存設定どおりとする。

### sweep・重複抑止

`OpsService.emit_pending_notices` に datafeed helper を別 try で加える。起動時と tick 末尾の回収点を使い、improve 設定・kill switch 成否から独立させる。observe の lock 区間から ops lane を取得しない。

重複判定は単一 daemon の ops lane 内で行う。通常遷移は code・ref・guard_kind・generation・state で照合する。終端結果は同 epoch に複数存在するため、state 一致だけで既出としない。記録の要求キーと event_id の対応で照合する。event_id は配送済みの印ではなく、生成済み event との対応である。

終端結果を含む sweep の event INSERT と記録への event_id 保存は同じ通知 transaction で確定し、途中 crash なら両方 rollback する。このため EventStore に commit しない INSERT primitive を分け、既存の時刻・schema 検査を再利用する。既存 emit の自動 commit をこの transaction に入れない（`events.py:150-160`）。表間の対応を含む重複抑止は ops lane 内の照合と対応保存だけで行う。kill switch の照合は guard_kind で絞る既存契約を維持する（`events.py:263-277`）。

通知生成を有効にする daemon を初めて起動した時刻を `producer_enabled_at` として通知用の管理行に永続化する。sweep は `occurred_at >= producer_enabled_at` の記録だけを扱う。再起動・rollback 後の再導入でこの下限を更新しない。通知記録だけを先に配備した間の古い停止・復帰は送らず、現在の active episode は下記の補完で扱う。

回収は発生時刻、epoch、同時刻なら停止→待ち→終端結果→復帰の順。既存 poll 上限以下の有限 batch とし、残りは次 tick へ送る。下限以後に永続化できた記録は次 epoch に進んでも回収する。schema 違反の一件は固定語で skip し後続を止めず、保存失敗は rollback して再試行する。event.ts は発生時刻を下回らない。DB の巻戻し・消失と将来の retention は別問題とする。

### bot・notify・導入順

既存の通知対象判定を維持し、七種類とも通常の ops 通知で配送する。新規要求の受付を配送し、照会・ack 置換では受付を生成しない。bot は event_id cursor、投稿後の保存、一時失敗 retry、恒久失敗時の停止、health 表示を再利用する（`afx-ops/afx_ops/bot/adapter.py:809-819`, `:875-898`, `:971-989`）。notify は任意とし、未設定でも live 起動を妨げない。

導入は consumer → 永続記録 → status 案内 → producer 有効化の順。consumer の固定文と status の操作案内を先に出す。初回 bot/notify は過去全件を送らず high watermark へ cursor を置くため、初回 cursor 初期化を確認してから producer を有効にする（`afx-ops/afx_ops/bot/adapter.py:814-819`; `afx-ops/afx_ops/notify/runtime.py:144-148`）。既存 cursor は消さない。旧 consumer が新 state を未知 code として拒否する危険はないが、固定文による操作案内には対応 consumer が必要である。

起動補完は専用 helper だけが担当する。現在の epoch が degraded の場合、次の優先順で0〜1件を、発生時刻を導入時刻として通常キーに補う (初回の導入時だけ。後の再起動で補うときは実際に補った時刻)。本文は通常の固定文とする。

1. 要求あり: 補完しない。status で「次の開場中の判定待ち」を示す。
2. 要求なしで同 epoch の待ちの印あり: 待ちだけを補う。過去の回復観測なので現在の健全性を断定しない。
3. それ以外で confirmed または開場時間の停止条件を満たす: 停止だけを補う。
4. ready・restricted、または停止条件未到達: 補完しない。過去の closed episode は補わない。

今回の snapshot は要求ありなので補完ゼロ。有効化前に同じキーの未 event 化記録があれば、補完対象の一件だけ発生時刻を導入時刻へ更新して回収対象にする。有効化後の記録と生成済みの対応は変更しない。補完処理を再実行しても同じキーは増えず、発生時刻も動かさない。休場中でも既存記録の配送・補完は行えるが、新たな観測・要求消費は行わない。

producer の有効化は通知生成に対応した daemon の版で切り替える。設定 key は増やさない。rollback は通知生成が未有効の直前の daemon へ戻し、consumer は固定文対応の版に残す。加算した表・列・管理行・未配送 event は削除しない。旧 daemon が追加情報を無視して動くことを受入で確認する。

## status で次の操作を見せる

API の `data` に既存 state を保ち、`phase`、`next_action`、`unprocessed_positions`（現在の未処理建玉数、非負整数）、直近の判定結果と判定時刻を加える。既存 `gap_summary` の読取りを使い、認証付き web と UDS の status で案内する。建玉数は Discord へ渡さない（`outage.py:184-214`; `src/agentic_fx/service.py:2180-2188`, `:2247-2258`）。読取り失敗時は件数をゼロとみなさず unknown にする。

以下は上から優先する真理値表。`*` は不問、「待ち印」は同 epoch、「拒否」は現在 epoch の直近結果が拒否、「件数」は現在の snapshot。pending と confirmed は停止理由の補足であり、単独では操作の要否を決めない。

| observe_failed / 読取り失敗 | state | 要求 | 拒否 | 待ち印 | pending / confirmed | 件数 | phase | next_action |
|---|---|---|---|---|---|---|---|---|
| あり | * | * | * | * | * | * | unknown | check_status |
| なし | * | あり | * | * | * | * | resume_requested | wait_tick |
| なし | ready | なし | * | * | * | * | ready | none |
| なし | restricted / degraded | なし | rejected_watermark | * | * | * | resume_rejected | restore_source |
| なし | restricted / degraded | なし | rejected_unprocessed | * | * | あり | resume_rejected | resume_with_ack |
| なし | restricted / degraded | なし | rejected_unprocessed | * | * | なし | resume_rejected | resume |
| なし | degraded | なし | なし | あり | * | あり | awaiting_resume | resume_with_ack |
| なし | degraded | なし | なし | あり | * | なし | awaiting_resume | resume |
| なし | degraded | なし | なし | なし | * | * | stopped | restore_source |
| なし | restricted | なし | なし | なし | * | * | restricted | wait_tick |
| なし | その他 / 情報不足 | * | * | * | * | * | unknown | check_status |

開場・休場の両方で同じ表を使う。wait_tick は必ず「次の開場中の tick の判定待ち」と表示する。resume / resume_with_ack は休場中でも要求を記録できるが、判定は開場後である。待ち印は現在の健全性を保証しないため「取得状態を再確認してください」を添える。restore_source は接続原因を断定せず「取得状態を確認し、回復後に再要求」と案内する。拒否理由は固定語から説明し、件数がゼロになっていれば ack 不要の案内へ変える。最新の watermark 判定は resume 消費時に行い、status に別の health 判定器は作らない。

ctl は停止理由と次の操作の二行、web は data カード直下に同じ固定案内を常時表示する。resume_with_ack は建玉確認後の明示承認だけを促し、自動付与しない。実行は SSH 端末からとし、web/Discord の実行ボタンは足さない。旧 API では既存 state 表示に戻し、不明を ready と扱わない。ctl と対話 shell の resume 応答は新規記録・照会・ack 置換を区別し、いずれも次の開場中の判定待ちと案内する（現状は `src/agentic_fx/ops/client.py:1903-1907`; `src/agentic_fx/commands.py:757-760`）。

## 停滞の猶予と接触検査による変更 (1 分足の穴の受容)

後続の足が届いている穴を受け入れ、停滞を猶予付きの restricted で受ける設計 (`2026-11-21-datafeed-short-gap-and-flat-release-design.md`) に合わせ、通知・案内を次のとおり変える。

- 猶予内の restricted と、その自動復帰 (健全な当 tick) は通知しない。連続 30 分 (開場秒) を超えた restricted の停止は、通常の停止通知を同じ epoch に 1 回出す。
- 停止中に届いた足、または穴への後着足が、建玉・未約定指値の保護価格に触れた (または判定できなかった) ときは、通常の停止とは別の通知記録 `(epoch, stopped, contact)` を 1 回作る。通常の停止・待ちの印・要求中・flat のいずれでも抑止しない。追加の接触は参照が増えるだけで、通知は増えない。起動時に、接触待ちがあるのに記録が無い epoch は同じ鍵で作り直す (重複しない)。event では `guard_kind=data_feed` の state を `contact_stopped` とし、通常の停止と重複排除が混ざらないようにする。本文は固定語で、価格・金額・件数・口座情報を含まない。
- 復帰後の人の待ち (`awaiting_resume`) は、初回の接触通知の後に同じ epoch で 1 回。flat で自動解除できる epoch は、自動確認中に待ちを出さない。
- 通常の停止後に flat で健全な tick が続いたときの自動解除 (`resumed`) は、停止または待ちの記録がある epoch だけ 1 回。通常の pending は自動解除を妨げない。接触待ち・移行確認待ちは妨げる。

status の案内は observe が保存した現在の facts (自動・手動の健全、接触待ち、exposure) から 1 つの関数で決め、古い拒否や待ちの印より先に判定する。拒否の結果は別欄 (`resume_last_result`) に残る。

| state | 条件 | phase | next_action |
|---|---|---|---|
| restricted | | restricted | wait_tick |
| degraded | 全 exposure ゼロ・自動復帰が有効・接触待ちなし・移行確認待ちなし・自動の健全 | recovering | wait_tick |
| degraded | 上の条件で、自動の健全でない | stopped | restore_source |
| degraded | 上の条件に当たらず (建玉あり・接触待ち・移行確認待ち・自動復帰が無効)、手動の健全 | awaiting_resume | resume_with_ack (確認対象の件数が 1 以上) / resume |
| degraded | 上の条件に当たらず、手動の健全でない (上位 hard の未取得を含む) | stopped | restore_source |

確認対象の件数は、未処理の足がある建玉と接触・不明の注文参照の和集合 (同じ注文は 1 件) に、移行確認待ちを 1 件足した値で、近似がゼロでも接触があれば resume に戻さない。`recovering` の固定文は「健全な足の確認を続けています。条件が揃えば自動解除します」。API の `data` には接触・不明の参照 (注文 ID・足時刻・種別) の一覧 `contacts` が加わり、ctl と web が表示する。旧 client の未知の phase は unknown として扱う。

## 失敗時と通信量

| 失敗 | 動き |
|---|---|
| activity 書込み失敗 | 発生記録・event 化に使わないため配送判断に影響しない |
| state/gap または要求判定の保存失敗 | 既存の fail closed。通知記録の失敗とは区別する |
| 通知記録の INSERT / commit 失敗・重複 | 重複は成功扱い。その他は通知 transaction だけ rollback。状態・保護処理を維持し、次 tick に補える分を再試行する |
| state commit 後・通知記録前の crash | 現在状態・直近結果だけ補完可能。消えた過去遷移の通知欠落はあり得る |
| ops lane timeout / event INSERT 失敗 | 記録を保持し再試行。固定ログ `datafeed_notice_deferred`。状態・保護処理は巻き戻さない |
| observe_failed の継続 | 本設計の Discord 通知の対象外。status は unknown・新規リスク停止を表示する。継続失敗の通知は別チケット候補 |
| Discord 一時不通 / bot 停止 | cursor を進めず既存 retry / watchdog / restart で遅延配送する |
| 投稿成功後 cursor 保存前の crash | 同一 event の再投稿があり得る。表示の exactly-once は保証しない |
| 恒久拒否 / 未設定 / 初回 cursor 初期化前の event | 配送 health で判別。無通知の可能性は取得・再開条件から切り離す |

論理 event は「時間で増えない（人の操作か状態の一回の遷移でだけ増える）」を上限とする。停止は時間条件でも最初の一件だけで、待ち・復帰も epoch ごと一件。受付は新規記録、終端結果は消費した要求の数に応じて増える。照会・ack 置換では Discord 通知を増やさない。既存の操作監査通知は別枠である。pair ごとの分割、時間駆動の再通知、外部状態照会は追加せず、既存 poll 間隔・接続数・bridge 呼出しを増やさない。任意 notify の各配送先にも同じ論理 event を送る。

一時失敗 retry と投稿後 crash を含む物理 HTTP 試行回数に厳密な有限上限は置かない。通知を捨てることになるためである。

## test と受入

この改訂では test を実行しない。実装時に fake clock・tmp SQLite・偽 ingest/Discord transport で以下を確認する。

1. failure→degraded→二連続の問題観測→待ち→受付→拒否→再要求→受理。固定本文と順序を比較し、金額・数値・生エラーを本文に出さない。要求ごとの終端結果が消されず、受理と停止解除が別通知になること。
2. epoch 66 の実 transcript を fixture 化する。待ち→再起動→二 tick 失敗→deferred→休場中の要求→開場判定を再生し、待ち後の停止を出さず、開場時の受理・拒否の両分岐を通知する。
3. 連続 confirmed に届かない degraded を、開場時間の10分で一度通知する。休場・祝日・再起動をまたいで時間を計り、待ち・ready が先なら停止を出さない。
4. pending を立てた後の flat・auto=true の待ち、自動復帰、restricted のみの復帰、confirmed 前の待ちを網羅する。停止 gate と pending 解除条件は変わらないこと。
5. ack なしの未処理足、ack ありでも不健全、両方の拒否理由、拒否後の再要求、ready 中の要求、restricted の要求が消費前に自動 ready になる場合、新 epoch をまたぐ要求を網羅し、消費した一件に終端結果と時刻を永続化・通知する。同一 ack の再要求と ack 取消しは元の時刻・ack を保持し、ack なし→ありだけ置換する。消費と競合しても新規・照会・置換の応答と受付件数が一致し、置換前の別結果は出さない。受付を実再開と誤認させないこと。
6. 通知記録の重複 INSERT・migration 未適用・保存失敗が observe_failed を立てず、状態更新と資金保護を妨げないこと。state commit と記録の間の crash における補完範囲と欠落の限界も確認する。
7. sweep の繰返し・再起動で各キー一件。event INSERT と event_id 対応保存の間の fault は両方 rollback し、同 epoch の終端結果が複数でも要求ごとに正しく対応する。event 未生成のまま次 epoch へ進んでも有効化後の記録を回収する。kill switch と干渉せず、不正な一件で後続を止めないこと。
8. 通知記録の先行配備で溜まった closed episode は有効化時に送らない。起動補完は snapshot の要求ありならゼロ、待ちなら待ち一件、停止なら停止一件。再実行して増えず、ready の過去履歴は補わない。
9. 真理値表を ctl/web の入力にし、pending・confirmed の真偽、現在件数の変化、開場/休場、observe_failed、旧 API、読取り失敗を比較する。ack の要否が通知有効化前に使え、新規・照会・ack 置換の応答も開場中の判定待ちと案内すること。
10. consumer 単独、永続記録・status 案内までと旧 daemon では generation=None の互換本文で結果通知を約束しない。producer 配備で初めて epoch 付き受付と結果通知を有効にし、rollback 後は互換本文へ戻ること。既存 guard schema と旧 consumer の汎用描画で新 state を扱えること。旧 daemon が追加の表・列・管理行を無視して起動・観測できること。初回 cursor→producer の順では届き、逆順では skip する既存境界を確認する。
11. 休場中の要求を消費せず、記録済み通知は配送する。bot/notify の一時不通・恒久停止・未設定が取得を止めず、投稿後 cursor 保存前の fault では表示重複だけが起こり得ること。

可能なら Dunia の隔離 clone・専用 tmp DB/設定・fake 価格源・専用 Discord 試験 channel で通常遷移と拒否・再要求・受理、status 案内、再起動、導入順を確認する。多数の fault/retry は偽 transport で行う。利用できなければ未実施理由を残す。本番 process 停止、MT5 切断、本番 DB/ログの書換えは受入に使わない。

開場最初の tick の実結果は、開場後に DB の readonly SELECT と activity の読取りだけで受理/拒否を観測し、チケットへ追記する。現時点では未来の観測であり完了扱いにしない。

## task

順序は T1 → T2 → T4 → T3 の4件。各行を単独 merge できる境界とし、参照元文書の訂正は実装時に各 task へ含める。

| task | 範囲 | 単独 merge の境界 |
|---|---|---|
| T1 | 既存 guard event の data_feed 固定描画、generation=None の互換受付と epoch 付き受付の描き分け、bot/notify の配送確認 | producer 未有効でも既存 event を扱い、互換受付で結果通知を約束しない consumer を先に出せる |
| T2 | 通知表・判定結果列の migration、新規/照会/ack 置換の契約と、その契約に要る ctl/shell の応答文 (新規・照会・ack 置換を区別し、照会では既存の要求と有効な ack を保ったことを示す)、終端結果永続化、commit 後の記録 helper、pending の待ち条件補完 | event 化は未有効。将来の sweep は有効化前の記録を除外する。記録失敗と状態更新の分離を検証できる。契約と応答文が同じ版で入るので、途中配備でも再要求の結果を読み違えない |
| T4 | API data の phase・現在件数・判定結果・next_action、ctl/web の案内 | T2 の後。真理値表・ack 要否・旧 API fallback が使用可能。T3 の必須前提 |
| T3 | ops lane sweep、event と対応の同時保存、時間条件、起動補完、有効化時刻、結果通知と同時の受付 generation 切替、導入/rollback | T1/T2/T4 の後に daemon の版で有効化。fault/cursor/通信量と隔離受入を確認する |

## 利用者に問う事項

### 裁定済み

| 問い | 裁定と理由 |
|---|---|
| Q1: ready 復帰も通知するか | **裁定済み: 送る。** 要求受付と実際の停止解除を区別できる。終端結果も消費した要求ごとに通知し、通信量は前述の「時間で増えない」で扱う |
| Q2: 長時間の再開待ちを時間駆動で再通知するか | **裁定済み: 初版はなし。** 受付・終端結果で操作への返事を残し、配送 retry と status の案内を使う |

新規要求の受付投稿の維持、終端結果の通知、起動補完、SSH 操作、任意 notify、金額非表示は本案で確定し、再び問いにしない。

### 別チケットの問い

次の二件は指揮者が別チケットとして起票し、利用者の裁定を得る。いずれも資金保護の規則に関わるため技術判断では変更せず、本設計の通知範囲にも含めない。

| 別チケットの問い | 推奨と理由 |
|---|---|
| I1(a): 日次ロールオーバー帯の無 tick 分を欠落として停止するか。期待足の例外を認めるか | 価格源の取引時間と取得記録から通常の無 tick 帯を確認したうえで、その帯だけ期待足の例外とする案を推奨する。15件中14件の反復を減らせる可能性がある。ただし時間帯だけで実障害を見逃す危険があるため、例外の範囲と建玉保有時の扱いを利用者が裁定する。裁定までは現行停止条件を維持する |
| I1(b): 全建玉が決済され、gap 以降の足が健全に揃った後も pending の解除に人手を要するか | 未処理対象がなく連続健全を確認できる場合に限り、flat の自動解除を認める案を推奨する。epoch 66 のように決済後も停止だけが残る事態を減らせる。ただし健全な足の取得は replay 完了の証明ではなく、未反映処理がないと判断する条件と手動解除を残す条件は利用者が裁定する。裁定までは pending を維持する |

## 付録: 文書・前提と現行コードの食い違い

旧設計の当時の分析と現行機能を区別する。今回、参照元は変更しない。

| # | 文書・前提 | 現行の実体と本案での扱い |
|---|---|---|
| 1 | チケット末尾は停止確認 + 待ちの二回 | Q1 は裁定済み。復帰と新規要求の受付・消費した要求の終端結果を含める。現行の復帰は activity のみ（`outage.py:358-360`, `:628-632`） |
| 2 | 未処理足は毎分記録 | 時間駆動ではないが、足の近似値が毎分変わる健全時は実質毎分。再起動でも出得る（`outage.py:135-139`, `:245-251`, `:679-706`）。通知しない |
| 3 | recovered_awaiting_resume は永続 state | 永続 state ではなく degraded の activity と印（`outage.py:23-24`, `:421-423`）。表示 phase と区別する |
| 4 | 9/24 設計は失敗/停滞で即 degraded | 現在は flat の停滞に restricted がある（`outage.py:362-408`）。これだけの自動復帰は通知しない |
| 5 | 9/24 設計は手動復帰、将来 backfilling/replay | 現在は flat の条件付き自動 ready があり、replay は未実装（`outage.py:3-9`, `:409-418`）。完了を通知で約束しない |
| 6 | 停滞は watermark と足幅の単純な壁時計計算 | 現在は休場を飛ばす next_bar_confirmation（`outage.py:557-569`; `market_hours.py:77-88`）。停止通知の時間条件も開場時間だけを使う |
| 7 | watermark がない gap 起点は必要本数から計算 | 現行は now を仮置き（`outage.py:514-536`）。本件では変えない |
| 8 | pending があれば健康回復後に必ず待ちを知らせる | 現行は flat・auto=true の待ち条件が漏れる（`outage.py:351`, `:410-423`）。待ち条件だけ補完し、pending 解除は別チケットの問いとする |
| 9 | 待ちの印があれば現在も健全 | 同 epoch の再悪化でも印は消えない（`outage.py:365`, `:421-441`）。待ち後の停止を抑え、status と拒否で再確認を促す |
| 10 | 本番でも shell の詳細で ack 要否を確認できる | daemon 運用では shell を起動しない（`src/agentic_fx/service.py:3050-3052`）。status 案内を通知有効化の前提にする |
| 11 | guard event を使えば停止/復帰も既に届く | 現行 data_feed は受付だけ（`src/agentic_fx/ops/service.py:1764`; `afx-ops/afx_ops/gate/render.py:459`）。既存契約に状態と固定描画を追加する |
| 12 | 現 state の sweep だけで全履歴を保証できる | ready・次 epoch で印が消える（`outage.py:363-365`, `:413-418`, `:619-626`）。別 transaction の遷移記録を使い、記録前 crash の限界を明示する |
| 13 | event を補えば初回 consumer にも必ず届く | 初回 poll は末尾へ cursor を初期化して配送しない（`afx-ops/afx_ops/bot/adapter.py:814-819`; `afx-ops/afx_ops/notify/runtime.py:144-148`）。初回だけ導入順を守る |
| 14 | 再起動をまたいで Discord 表示も厳密に一回 | 投稿後に cursor 保存（`afx-ops/afx_ops/bot/adapter.py:875-898`, `:971-977`）。生成の重複抑止と表示重複の窓を区別する |
| 15 | 要求後、開場最初の tick で ready になった | 今回は要求記録までの観測。開場後は拒否もあり得る（`outage.py:609-648`）。チケットは判定予定へ訂正し、実結果は後日 readonly で追記する |
| 16 | resume 応答は次の tick で判定 | 休場中は消費されない（`outage.py:236-237`, `:280-285`）。ctl/shell とも新規・照会・ack 置換を区別し、次の開場中の判定待ちと案内する |

## 付録: 導入と rollback の手順

導入は consumer → 永続記録・status 案内 → producer の順で、各段を確認してから次へ進む。producer を有効にするのは通知生成を含む daemon の版であり、設定 key は無い。

1. consumer (bot・任意の notify・afx-ops) を固定文対応の版へ更新して再起動する。bot が既に cursor を持っていれば消さない。初めて起動する bot・notify は、初回 poll で cursor を最新の event に置くので、その初期化が済んだこと (配送 health) を確認してから次へ進む。初期化前に出した event は届かない。
2. 永続記録と status 案内を含む版を配備し、`afx ctl status` の data 行に停止理由と次の操作が出ることを確認する。この間は受付が互換本文 (generation なし) のままで、結果通知を約束しない。
3. 通知生成を含む daemon を再起動する。起動時に有効化の時刻を `alert_state` の `datafeed_notice.producer_enabled_at` へ一度だけ置き、続いて停止中の episode があれば補完の 0〜1 件を作る (要求あり = 0、待ちの印あり = 待ち、confirmed または開場時間の停止条件 = 停止)。以後の受付は要求時の epoch を generation に持ち、結果通知を案内する。
4. 配備前から開いていた `afx ctl` の対話 shell は閉じて開き直す。古い shell は新しい再開要求の応答文を知らない。
5. 再起動後は active と warning だけで済まさず、status の data 行・直近の取引判断の mission・`ops_events` の data_feed の件数 (導入直後は補完の 0〜1 件) まで読取りで確認する。DB は readonly の SELECT だけで見る。

rollback は通知生成が未有効の直前の daemon へ戻す。consumer は固定文対応の版に残す。加算した表・列・`alert_state` の行・未配送の event は削除しない。戻した間は受付が互換本文に戻り、通知記録の生成も止まる。再導入しても `producer_enabled_at` は更新しないので、戻した間に記録だけが作られた場合は、下限以後のものが再導入後の最初の回収でまとめて届く (発生時刻より遅れて届く。本文に時刻は無いので、遅れて届いたときは `afx ctl status` を確認する)。下限を新しくしたいときは、その行を人が明示して消す運用判断として別に扱う。

## 変更履歴

| 日付 | 版 | 変更 | 理由 | commit |
|---|---|---|---|---|
| 2026-11-21 | C0 | 停止・待ち・実再開の固定通知、永続遷移と sweep、導入補完、status 案内、上限と受入を提案 | 再発時の長時間停止と再開操作の見落としを防ぐ | —（未 commit） |
| 2026-11-21 | C0.1 | 受付・拒否通知、停止の時間条件と順序、記録失敗の隔離、既存 guard 契約、status 先行導入、別チケットの問いへ改訂 | 設計レビュー 1 周目と利用者の裁定を反映 | |
| 2026-11-21 | C0.2 | 再要求と ack 置換、要求の終端結果と停止解除、受付の互換本文と有効化境界を明確化 | 設計レビュー 2 周目の指摘を反映 | |
| 2026-11-21 | C0.3 | 再要求の応答文を契約と同じ T2 に移す | 設計レビュー 3 周目の指摘 (途中配備で応答が契約に追随しない) | |
| 2026-11-21 | C1 | 利用者承認 | 3 周 (opus → sol → sol) で収束 | |
| 2026-11-21 | C1.1 | 導入と rollback の手順を付録に追加。要求が未消費の間は時間条件の停止を記録せず、停止をまだ知らせていない epoch では結果の判定時刻から数え直す。有効化の時刻は daemon の構築時に置く。再起動時の補完は実際に補った時刻を発生時刻にする | 実装時の判断 (人が操作中は知らせない。拒否後も続く停止は知らせる) | |
| 2026-11-22 | C1.2 | 猶予付きの restricted・接触検査に合わせ、接触の停止通知 (`contact_stopped`)・recovering の案内・確認対象の件数・接触参照の表示を追加 | 1 分足の穴の受容の設計 (C1) の実装に伴う同期 | |
