# 1 分足の短い欠けと flat 後の自動復帰 設計 C0

1 分足が 1 本だけ届かないときは、建玉があっても新規・リスク増だけを止める。決済・取消・SL/TP の保護は続ける。約 2 分待って同じ価格源へ取り直し、前後の足が揃ってなおその分だけ無ければ「取引なし」とみなす。後着した足が保護価格に触れていなかった場合も、健全な観測が 3 tick 続けば戻す。触れていた場合は degraded とし、人の確認を残す。過去の約定・決済を再現する replay は作らない。

degraded の原因になった建玉がすべて決済され、指値・処理中・結果不明の注文も無くなった場合は、`auto_resume_when_flat=true` なら回復後の健全 3 tick で pending を解除する。他の停止条件は解除しない。

作成日: 2026-11-21 JST。承認済み方針は `docs/tickets/open/datafeed-rollover-band-expected-gap.md:24` と `docs/tickets/open/datafeed-pending-auto-release-when-flat.md:23`。以下の数値・保存形式は本下書きの設計判断。実装・テスト・配備は今回行わない。

## 確認した実体

以下の短縮名は `outage.py` = `src/agentic_fx/datafeed/outage.py`、`ingest.py` = `src/agentic_fx/datafeed/ingest.py`、`scheduler.py` = `src/agentic_fx/core/scheduler.py`、`paper_fills.py` = `src/agentic_fx/core/paper_fills.py`。worktree の根拠は main と混同しない。

| 事項 | 読み取り結果・根拠 |
|---|---|
| 停止の分布 | epoch 52–66 の入口 15 件中 14 件が 22:0x–22:5x UTC、すべて USDJPY 1m の stalled。例外は epoch 57 の 12:33 UTC。`logs/activity.log:12671`, `:12676`, `:12678`, `:12684`, `:13043`, `:14102`, `:14433`, `:14714`, `:14719`, `:14724`, `:14990`, `:14992`, `:14997`, `:15269`, `:15530`。ログの stalled は「欠け 1 本」の証明ではない |
| flat と建玉ありの差 | flat の restricted→自動復帰は `logs/activity.log:12671`, `:12674`。建玉ありの epoch 66 は即 degraded (`:15530`)。pending の条件は `outage.py:308-351`, `:375-418`。pending は flat 後も保持される |
| 欠けの現存 | 下記 readonly SQL で、11/19 の 22:39・41・43 は各 0 行、隣の 22:38・40・42・44 は各 1 行。source は mt5-live。epoch 55・56 は最初の欠けの次の分も無い。14 件を全部「孤立した 1 本」とは扱わない |
| MT5 取得の形 | `mt5_bridge/mt5_client.py:626-705` は UTC を変換して `copy_rates_range` を呼び、rates の各行をそのまま足へ変換。None は例外、0 行なら空リスト。tick_volume は volume へ写すだけで、分ごとのゼロ足の補間は無い。無 tick 分に足が無い運用と整合するが、このコード・cache だけで各欠けの無 tick を証明はできない |
| SL/TP と gap | 最新 1m だけを渡す `src/agentic_fx/service.py:1829-1832`、鮮度・同一足を検査する `scheduler.py:942-949`、OPEN を走査する `:1786-1797`、`check_exit` を呼ぶ `:1844-1852` に履歴 drain は無い。`paper_fills.py:45-57` の gap は足 open が保護価格を飛び越す価格 gap。時系列の欠けを補う機能ではない。shadow 用の連続性検査 (`:104-130`) も通常執行の replay ではない |
| 健全の意味 | `outage.py:302-329`, `:548-569` は取得成功と最新 watermark の停滞判定。内部の欠けを列挙しない。`ingest.py:211-225` の通常の要求下限は watermark の 1 本前で、最新足が進むと古い欠けは要求外になる |
| 未処理建玉 | `outage.py:669-718` は適用 cursor を持たず OPEN/PENDING_FILL と対象時刻から安全側に推定する。実際に適用済みか、全 exposure がゼロかは証明しない |

DB に行番号は無いため、DB の事実にはファイル行番号を捏造せず、以下の再現可能な SELECT を根拠とする。出力は日時・存在数だけで、価格・金額・口座情報を含めない。

```sh
sqlite3 -readonly data/agentic.db "WITH t(ts) AS (VALUES
 ('2026-11-19T22:38:00+00:00'),('2026-11-19T22:39:00+00:00'),
 ('2026-11-19T22:40:00+00:00'),('2026-11-19T22:41:00+00:00'),
 ('2026-11-19T22:42:00+00:00'),('2026-11-19T22:43:00+00:00'),
 ('2026-11-19T22:44:00+00:00'))
 SELECT t.ts,count(c.bar_time) FROM t LEFT JOIN ohlcv_cache c
 ON c.symbol='USDJPY' AND c.interval='1m' AND c.source='mt5-live'
 AND julianday(c.bar_time)=julianday(t.ts) GROUP BY t.ts ORDER BY t.ts;"
```

連続欠けの照合には次を使う。gap_start は最後の観測水位であり、欠けそのものの時刻ではない (`outage.py:514-535`)。55/56 の first_present と next_present は共に 0、57 は共に 1、66 は 0/1 だった。

```sh
sqlite3 -readonly data/agentic.db "SELECT g.epoch,
 EXISTS(SELECT 1 FROM ohlcv_cache c WHERE c.symbol=g.pair
 AND c.interval='1m' AND c.source='mt5-live'
 AND julianday(c.bar_time)=julianday(g.gap_start,'+1 minute')) AS first_present,
 EXISTS(SELECT 1 FROM ohlcv_cache c WHERE c.symbol=g.pair
 AND c.interval='1m' AND c.source='mt5-live'
 AND julianday(c.bar_time)=julianday(g.gap_start,'+2 minutes')) AS next_present
 FROM datafeed_outage_gap g WHERE g.epoch BETWEEN 52 AND 66 AND g.interval='1m'
 ORDER BY g.epoch;"
```

## 欠けの数え方と期限

対象は registry の hard key `(pair, interval, storage_source)`。soft 系列の不調は昇格理由にしない。足時刻は UTC の開始時刻、区間は半開 `[start,end)`。既知の休場は `market_hours.next_expected_trading_time` (`src/agentic_fx/core/market_hours.py:77-88`) と同じ期待時刻列から除く。ロールオーバー時刻だけの例外は作らない。

- 最後に検査済みの確定足から今回の確定 cutoff まで期待時刻を列挙し、同じ source の実足との差集合を求める。最新 watermark の差分だけでは数えない。前回の検査端点を 1 本重ね、初回・起点不明・保持期間外・設定/source 変更で範囲を保証できない場合は救済不能として degraded。
- 未承認の欠けが全 hard key 合計で **1 個、かつ 1m** の場合だけ候補にする。複数 pair 各 1 本、同じ pair の非連続 2 本、1m と 5m の同時欠けも救済しない。5m 以上の欠けは現行の degraded 経路。異なる足を 1m に換算して数えない。
- 内部の欠けは直後の期待足がある候補。末尾の 1 本未到着も restricted で待てるが、まだ「孤立」とは認定しない。次の期待足まで期限が来て欠ければ即 2 本として degraded。未確定の末尾は欠けに含めない。
- 欠け m の本来の確定時刻を `C=m+60秒` とする。取得の既存 grace は欠けを検出する cutoff にだけ使い、C に足し直さない。取り直しの開始予定 `V=C+120秒`、完了期限 `D=V+60秒`（通常の scheduler 1 周分）。V 以上 D 以下で取り直し・保存・検証を完了する。D ちょうどは受理、D を過ぎた結果は採用せず degraded。観測 now に加え完了時刻も検査する。V より前の後着は保存しても、V 以降の取り直しを省略しない。
- 「猶予超過」は D までに検証済みにならないこと。検証後の健全 3 tick は D の制約外。ただし不健全化・新しい欠けで streak をゼロにし degraded。遅い初回検出・再起動でも C/V/D を now から再計算しない。
- 同じ episode で別の欠けが一つでも起きたら degraded に固定する。一度検証済みでも episode を閉じるまで救済枠を再利用しない。ready 後の新しい欠けは新 epoch。ただし過去に承認した不在分を再検出して episode を作らないよう、source・pair・時刻付き認定を保持する。

時刻例: m=22:39、C=22:40、V=22:42、D=22:43 UTC。22:40・41 が取得でき、22:42 以降の再取得でも m だけ無ければ不在認定できる。22:41 も無ければ、期限を待たず 2 本として degraded。実例の 22:39・41・43 の反復を一つの孤立 gap とまとめない。

## 取り直しと永続化

outage が保存した候補を、次 tick の lock 外 prepare に要求として渡す。`prepare(now, gap_probe=...)` の追加引数は省略可能。予定 V までは通常取得、V 以降は budget・backoff よりこの hard probe を優先する。下限を `min(通常下限,m-60秒)`、終端を now として、欠けの直前から今回の全確定足まで同じ primary に一回の range 要求を出す。fallback、tick 取得、ゼロ足生成は使わない。要求範囲の正規化は現行と同じだが、欠けを黙って補間しない。

report に候補 ID/epoch、source、要求範囲、cutoff、取得完了時刻、応答した確定足の時刻集合、候補足（あれば OHLCV）、結果を追加する。通常 succeeded だけで probe 完了とはしない。

| 結果 | 判定 |
|---|---|
| 通信・時刻規則・正規化の例外 | failed。即 degraded、原因は内部の固定コードで保存 |
| 成功、正規化後の確定足が 0 本 | empty。即 degraded。「m が無い」とは区別 |
| 非空だが直前足・後続の期待足が不足 | incomplete。即 degraded。切詰め・要求範囲不一致も同じ |
| 非空、前後が揃い m だけ無い | absent_validated。「無 tick 証明」でなく、承認方針に基づく「取引なし」認定 |
| m を含め期待足が揃う | late_bar。下記の接触検査へ |
| probe を行えない、D 超過、commit 失敗 | unverifiable。degraded。成功記録や streak を進めない |

追加保存は既存 gap の watermark 起点と別に、候補 `(epoch,pair,interval,source,m)`、C/V/D、検査端点、検証結果・理由・完了時刻・後着足の指紋、接触有無・注文参照、判定用 snapshot を持つ。既存 `restricted_deadline_at` は D の表示へ合わせる。認定済み不在の索引は epoch が閉じても残し、cache の保持期限までは削除しない。認定 m に実足が後で届けば ready 中でも再検査し、接触・不明なら新 episode で degraded。要求外の過去を無期限に再取得する仕組みは作らない。

cache commit の成功後、同一 core_lock 内の明示 transaction で候補・認定・state・pending・streak を更新する。cache が先に保存されて落ちても、検証未完了は復帰根拠にならず、再起動で同じ候補を再検証する。source/epoch が prepare 中に変わった結果は捨てる。state 保存失敗は既存 `_observe_failed` (`outage.py:140-146`, `:229-235`) により新規を止め、保護 tick は続行する (`src/agentic_fx/service.py:2527-2536`)。起動時は候補を読み終え初回 observe が済むまで gate を開けない。旧 restricted の候補時刻が不明なら期限を作り直さず degraded。

## 後着足の SL/TP 接触

評価は純関数で行い、注文・約定・決済の履歴を書き換えない。欠けの区間と生存期間が重なる OPEN と未約定指値を対象に、閉じた注文も除外しない。検出後に flat になっただけで、当時の建玉を接触判定から消さない。

当時の保護価格を現在の orders から推測しない。通常の各 tick で、直近検査端点以後に exposure を持つ注文の状態・生存時刻・direction・SL/TP・指値・spread と変更前後を永続 snapshot に残す。候補確定時に該当 snapshot を固定する。orders の保護条件を更新する経路も同じ記録を残し、snapshot 保存に失敗した場合は自動復帰を禁止する。後付けの transition observer は commit 後で例外を握り潰す (`src/agentic_fx/core/transitions.py:47-49`) ため、この安全記録の唯一の保存口にはしない。既存 episode・再起動で履歴が不足する場合は接触不明として degraded、flat 後の解除だけを許す。足途中の生成・取消・SL/TP 変更は、重なり得る全版を評価する。順序を絞れなければ不明に倒す。

spread `s` は scheduler と同じ `assumed_spread_pips × pip_size` (`scheduler.py:1837-1842`) を当時の設定で固定する。half=`s/2`。履歴 spread の新推定や MT5 rates の spread への切替はしない。非有限値・欠損・方向不明は検査失敗。

| 対象 | 一意な判定 |
|---|---|
| OPEN long | `check_exit(snapshot,bar,s,entry_same_bar=False)`。SL は low−half または open−half ≤ SL、TP は high−half ≥ TP |
| OPEN short | 同関数。SL は high＋half または open＋half ≥ SL、TP は low＋half ≤ TP |
| SL と TP の両方 | 同関数の SL 優先。open が SL を超える場合の gap 滑りも `paper_fills.py:45-51` と同じ。価格を発注に使わない |
| 未約定 long limit | `check_limit_fill(snapshot,bar,s)`。open＋half ≤ 指値なら open＋half、そうでなく low＋half ≤ 指値なら指値 |
| 未約定 short limit | 同関数。open−half ≥ 指値なら open−half、そうでなく high−half ≥ 指値なら指値 |
| 指値が仮に約定する足 | 仮注文に対し `check_exit(...,entry_same_bar=True)` で SL を検査。さらに `entry_same_bar=False` で TP 接触も検査する。同一足 TP は通常の約定確定では抑止される (`paper_fills.py:53-57`) が、ここでは接触の可能性として degraded。指値に届かない場合はその指値の SL/TP 検査不要 |

指値約定だけで SL/TP 非接触なら承認方針どおり自動復帰対象。仮約定を実注文へ反映しない。新規約定を止めていた区間の取り逃しを replay で埋めない。SL/TP 接触または時系列不明なら `late_bar_protection_contact` / `late_bar_contact_unknown` として degraded + pending。内部の安全記録に epoch、足時刻、source、注文参照、SL/TP/両方/不明、使用した snapshot と関数版を残す。activity/Discord に価格・金額・口座を出さない。後着足の訂正で指紋が変わった場合も再検査し、非接触への訂正だけで既存 pending を消さない。

## 健全 3 tick と flat の解除

tick は scheduler の観測一回で、足の本数とは違う。同じ now の再実行は重複計上しない。1m hard key がある場合はその全 key が当 tick に成功・非空で、最新の確定 watermark が前の計上 tick より進み、回復起点以後の期待足が揃うことを必要とする。同じ最新足を 3 回取得しても 3 tick にはしない。既に認定した不在 m だけは連続性検査から除外する。上位 hard 足は当 episode/再起動後に非空成功を一度確認し、以後 failed/empty/stalled でないこと。1m が無ければ全 hard key で同様に取得成功・watermark 前進を要求する。

短い欠けは検証完了 tick を 1 として数えられる。degraded は、取得が戻って最初に成功した確定足を回復起点にし、それ以降の連続性を検査する。長い障害中の全欠けを埋めることは flat 解除の条件にしない。取得失敗・空・deferred・必要な probe 未試行・水位停滞・新しい欠けは streak=0。閉場中は増やさず、開場後は 0 から確認する。再起動で streak=0、期限と認定結果は保持する。

flat は `orders.list_by_status(conn,*_EXPOSURE)` が **全 pair を通じて 0 行** とする。状態集合は `src/agentic_fx/core/executor.py:28-36` の open / pending_fill / protection_pending / submitting / submitted / closing / cancelling / submit_unknown / cancel_unknown / close_unknown。quantity が 0 でも非終端状態なら止める。未知の status・読取り失敗は flat 不明。approval_pending はまだ broker exposure ではないので含めないが、承認後の submit は既存 outage gate を必ず通す。

既存の未処理近似が 0 でも closing や unknown を見落とすため、解除には使わない。terminal の closed/cancelled/rejected/expired/invalidated は処理完了記録として扱う。遅い broker 結果で終端と整合しないものは既存 reconcile 経路で非終端・unknown として残ることが前提。不整合を検知したら解除しない。

pending が立っていても、flat=true・auto=true・回復後健全が 3 回揃えば pending=0 と ready を同じ transaction で保存する。flat が初めて成立した tick より前の健全回数は流用しない。scheduler の決済は observe の後なので、その tick に全決済されても次の observe から数える。flat が崩れればゼロから。auto=false は建玉ありの短い欠けを含め自動復帰を無効とし、健全時は degraded の手動待ちへ進める。

## 状態機械と資金保護

以下は上から優先。「安全な短欠け」は取り直し成功かつ不在認定または非接触。「自動条件」は健全・設定有効・必要なら flat が成立した状態。pending は解除理由の履歴であり state 名ではない。confirmed は停止通知用の補助印。

| 入口 | 現 state | 次 state / 処理 |
|---|---|---|
| 保存・取得失敗、empty、2 個以上、上位足欠け、期限超過、証拠不足 | 任意 | degraded、streak=0。exposure または auto=false なら pending=1。接触/不明も pending=1 |
| 1m の未承認欠けが 1 個、期限内 | ready | epoch を開き restricted。exposure の有無で昇格しない。C/V/D と snapshot を保存 |
| V 未満 / probe 待ち、別の欠けなし | restricted | restricted。streak=0。D 超過なら先頭行 |
| 安全な短欠け、自動条件、streak 1–2 | restricted | restricted。検証済みとして待つ |
| 安全な短欠け、自動条件、streak 3 | restricted | ready。pending=0、復帰理由を保存 |
| 短欠けの検証完了、auto=false | restricted | degraded、pending=1、健全なら手動待ち |
| 接触あり・接触不明 | restricted / ready | degraded、pending=1。ready なら新 epoch |
| 回復したが exposure あり、または auto=false | degraded | degraded。健全なら手動待ちを一回記録 |
| flat + auto=true、回復後健全 1–2 | degraded | degraded。pending の有無に依らず解除を確認中 |
| flat + auto=true、回復後健全 3 | degraded | ready と pending=0 を一括保存 |
| 手動要求 | non-ready | 同じ tick の検証後に水位/ack 条件を再検査。接触 pending + exposure ありは近似件数 0 でも ack 必須。未検証の短欠け・不健全は ack でも拒否 |
| 自動解除と手動要求が競合 | 任意 | 観測→自動判定→要求消費の順。先に ready なら already_ready、まだ non-ready なら通常判定。要求 key の比較更新で置換済み要求を消さない |
| 閉場 | 任意 | 新規判断は既存制限。要求を保留、streak を増やさない。期限は延長せず次の開場観測で D を再検査 |

不変条件:

1. non-ready では mission・新規 open・指値の新規 fill・リスク増を止める。決済、取消、close retry、既存 SL/TP を止めない (`scheduler.py:329-348`)。
2. 欠けを価格で埋めない。取得失敗と「非空の区間内に m が無い」を混同しない。最新水位だけで検証完了にしない。
3. 復帰には保存済み証拠を使う。再起動、手動 ack、通知配送の成功で期限・検証条件を迂回しない。
4. exposure が残る接触ありの pending は自動解除しない。flat になっても他の kill switch・リスク・承認 gate は維持する。

## 通知と status の整合

関連設計 `docs/superpowers/specs/2026-11-21-datafeed-outage-notice-design.md:10-26`, `:96-100`, `:153-178` の語彙を使う。永続 state は ready/restricted/degraded のまま。追加は表示 phase だけ。restricted が建玉ありでも増えるが、restricted の入口・取り直し・健全回数の通知は増やさない。短い restricted だけの自動解除は従来どおり Discord 無通知、内部 activity `datafeed_recovered_auto` に理由を足す。

degraded の停止・手動待ちを同 epoch で記録済みなら、flat 自動解除にも既存の **resumed** を一回記録する。「監視対象の価格系列のデータ停止から復帰し、データによる停止を解除しました。SSH の端末から `afx ctl status` を確認してください。他の停止条件は同画面で確認できます。」という既存固定本文を使う。新しい通知種別は不要。手動要求の accepted/rejected/already_ready は別に必ず終端記録を返す。待ちを記録した後の stopped 抑止、記録の一意キー、配送 retry、記録前 crash の通知欠落限界は outnotice のまま。

自動 flat 確認中に awaiting_resume を新規発生させない。過去の待ち印は履歴として保持し、status は現在の自動確認を優先する。auto=false や exposure 再発で自動確認を失った場合は元の手動案内に戻す。既存の未処理近似は手動 ack 案内に残すが、自動可否には用いない。

真理値表は既存の **unknown → 要求あり → ready** の 3 行を最優先として残し、その後、旧「拒否」「待ち印」の行より前に次を挿入する。入力は observe が確定した snapshot とし、status が独自に健全判定をしない。

| state | 追加条件（要求なし、読取り正常） | phase | next_action |
|---|---|---|---|
| restricted | 短欠けの候補、V 待ち / 再取得待ち | short_gap_verifying | wait_tick |
| restricted | 安全な短欠け検証済み、auto=true、健全 0–2 tick | recovering | wait_tick |
| degraded | flat 確定、auto=true、回復証拠あり、健全 0–2 tick | recovering | wait_tick |
| degraded | flat でも回復未確認、現在不健全 | stopped | restore_source |
| restricted / degraded | 候補・flat・保存結果の必須情報が不明 | unknown | check_status |

recovering は「健全な足の確認を続けています。条件が揃えば自動解除します」、short_gap_verifying は「短い欠けの取り直しを待っています」。wait_tick は既存どおり次の開場 tick 待ち。手動拒否の結果と時刻は別欄に残すが、現在自動確認が成立すれば古い拒否だけを理由に resume を促さない。自動確認しない場合の rejected_watermark / rejected_unprocessed / awaiting_resume の既存行は維持する。接触理由は内部記録から手動 ack 対象へ含め、未処理近似が 0 でも exposure が残る接触案件は resume_with_ack とする。

API に `short_gap_result`、`recovery_check_active`、`flat_confirmed`、`protection_contact_pending` を追加する。`auto_resume_possible` は pending だけで false にせず、安全な短欠けまたは flat 解除条件に従う。`guidance_text.py` の許可組・ctl・web を同時に更新し、旧クライアントは未知 phase を unknown と表示する。

## 実装 task と merge 順

各行は、その前提が main に入っていれば単独で merge・レビューできる変更単位。今回これらは実施しない。

| task | 内容・単独 merge の条件 | 前提・通知との順序 |
|---|---|---|
| T1 | 候補・認定・snapshot の追加 schema と保存口、期待足列の純粋な検査。動作切替はまだ無効。既存 DB から冪等に移行 | main のみ。outnotice の schema を上書きせず追加 migration |
| T2 | gap_probe 引数・範囲拡張・明示結果 report と cache 保存の検査。呼出しなしでは従来動作 | T1。通知の有無に非依存 |
| T3 | 後着足の接触検査、short-gap 状態遷移、期限・起動 gate。保護の継続、手動要求の検証条件も一緒に有効化 | T1–T2。通知接続口は下記契約に従う |
| T4 | 全 exposure の flat 判定、pending 自動解除、3 tick と手動競合の処理 | T1。T3 より先でも可。未検証短欠けは自動解除しない |
| T5 | 通知への接続、API/ctl/web 真理値表・固定案内、関連設計の食い違いを更新 | T3–T4 と outnotice の両方。二つの merge 順を検証 |
| T6 | 実欠け形の fixture、fake/Dunia の統合受入・運用手順と境界検証を確定 | T5。実装変更ごとの単体検証は T1–T5 に同梱 |

outnotice は `tmp/wt/outnotice`（branch outnotice、未 merge）で同じ outage.py を変更中。通知が先なら、本件の T3/T4 は既存 `_Outcome.notices` / `_record_notices` に遷移後の事実を渡し、resumed の条件を新しい解除にも適用する。本件が先なら T3/T4 は通知に依存しない復帰理由・同 epoch の観測記録を残し、後から outnotice を merge する際に同じ接続を行う。既存 outnotice の自動遷移ブロックを丸ごと採用して pending 固定・exposure 即 degraded を復活させない。

両方が入るまで T5 の新 phase を公開しない。片側だけでも既存 state による停止・保護・旧表示は成立する。通知が後の場合は本件だけで新たな Discord 配送保証を作らず、過去 ready episode を遡って通知しない。outnotice 側の resumed 記録は state 保存後の独立 transaction のまま (`tmp/wt/outnotice/src/agentic_fx/datafeed/outage.py:869-900`)。安全証拠の保存と通知の保存を混ぜない。

## test と受入

以下は実装時の検証項目。今回テストは実行していない。

| 境界 | 期待 |
|---|---|
| 欠け 0 / 1 / 2、内部 / 末尾、複数 pair、1m+5m、soft のみ | 0 は ready、1m 合計 1 個だけ restricted、2 個以上と上位 hard 不調は degraded。soft は無関係 |
| V 直前 / ちょうど、D ちょうど / 直後、途中で完了期限超過 | V で初めて probe、D までは完了受理、直後は degraded |
| 成功非空で m 不在 / 空 / 例外 / 前後欠損 / source 違い / 保存失敗 | 不在だけ認定。残りは認定も streak 前進もしない |
| 健全 2 / 3 tick、同じ足の再取得、同じ now 再実行、途中失敗 | 2 では止め、異なる水位の 3 で解除。重複を数えず不健全でリセット |
| 再起動、時計後退、期限超過で起動、旧 schema・snapshot 不足 | C/V/D 不変、streak=0。時計後退で期限を延ばさず、自動検証不能なら degraded |
| 後着足が long/short の SL / TP / 両方に接触、open の gap、非接触 | 接触は degraded + pending、非接触だけ自動候補。SL 優先を確認 |
| 指値不成立 / 成立、同足 SL / TP、途中取消・保護変更、既に closed | snapshot の純関数評価。TP 順序不明も止める。注文・損益は不変 |
| 全 exposure 各状態を 1 件ずつ、quantity=0、別 pair、status 不明 | いずれも flat と誤認しない。terminal のみなら flat |
| pending=true→flat、auto=false、flat 2 tick 後に exposure 再発 | pending は健全 3 回でだけ解除。auto=false は解除せず、再発でゼロ |
| 自動 ready と要求新規/ack 置換/休場/新 epoch が競合 | 一件に一終端結果。水位・候補検証を ack で迂回しない |
| 通知が先 / 本件が先、待ち印や古い拒否がある recovering | stopped/awaiting_resume/resumed の一回性、current phase の優先、ctl/web の同じ案内 |
| 任意失敗・通知例外中の tick | 新規・リスク増は止まり、取消・決済・全 OPEN の通常保護走査は続く |

fixture は本番 cache の**時刻の有無**から採り、価格は合成値へ置換する。孤立形（epoch 52 等）、連続欠け（55/56）、22:39・41・43 の反復形（66）、後着する形（57）を分ける。readonly の追加抽出は受入準備で行い、金額・口座は含めない。Dunia または fake bridge、仮時計、一時 SQLite、通知 double だけで入口から解除まで再現する。本番 bridge を止めたり、本番 DB/cache に欠けを作ったり、Discord に試験通知を流さない。

受入は、建玉あり孤立 1 本→不在認定→3 tick 復帰、後着非接触→復帰、接触→手動待ち→全 exposure 終端→3 tick 自動解除をそれぞれ示す。連続欠け・反復欠けは degraded を維持し、保護走査の呼出しが停止前後で続くことを確認する。期待状態・期限・復帰理由・通知記録を照合し、実行順を変えても同じ結論になることを確認する。

## 利用者に問う事項

追加の問いは 0 件。時間帯例外を採らないこと、足不在による初版認定、接触時の人手確認、flat 後の自動解除は裁定済み。120 秒待ち＋完了に 1 周分、合計 1 個の救済枠、同 episode の再発昇格、重複水位を数えないことは安全側の技術判断として提案する。bridge の tick 数による強化は別件とする。

## 付録: 文書・前提とコードの食い違い

今回確認した本件に関わる食い違いを全件列挙する。参照元とコードは今回は変更しない。

| # | 文書・前提 | 実体・本案での扱い |
|---|---|---|
| 1 | 停止 14 件がすべて孤立した 1 本 | ログは stalled だけ。readonly の gap_start+1/+2 分照合では epoch 55/56 は両方不在、57 は最初の足が現存。分布 14/15 と形の認定を分ける |
| 2 | cache の足不在で無 tick と証明できる | bridge は返された rates だけを写す (`mt5_bridge/mt5_client.py:675-705`)。初版は不在に基づく運用上の認定で、tick 不在の証明ではない |
| 3 | 9/24 設計の backfilling・連続性・SL/TP replay がある | 現行は対象外 (`outage.py:3-9`)。`docs/superpowers/specs/2026-09-24-outage-stop-and-backfill-design.md:85-96` の将来範囲を本件で実装済みとしない |
| 4 | 9/24 設計は停止後に手動復帰だけ | 現行は flat 条件付き自動復帰あり (`outage.py:409-418`)。今回 pending が立った後の flat にも拡張 |
| 5 | restricted は flat の 1m 停滞だけ | 現行は not flat で即 degraded (`outage.py:375-392`)。今回は厳密な単一欠けなら exposure に依らず restricted |
| 6 | 単一欠けの猶予が約 2 分 | 現行は flat_stall_max_sec 既定 1800 (`outage.py:105`, `:384-387`)。新しい C/V/D を独立に定義し既存値の名前だけ流用しない |
| 7 | 健全は連続性の証明 | 最新水位・取得結果のみ (`outage.py:302-329`)。新しい範囲検証と回復後の期待足検査を加える |
| 8 | 通常 ingest で古い欠けも再取得する | watermark−1 本 (`ingest.py:211-225`)。候補に基づく要求下限を加える |
| 9 | unprocessed_positions=0 なら処理中なし | OPEN/PENDING_FILL の時刻近似のみ (`outage.py:699-716`)。全 `_EXPOSURE` (`src/agentic_fx/core/executor.py:29-31`) のゼロを使う |
| 10 | pending 後の flat は自動解除する | 現行は not pending が必須 (`outage.py:410`)。outnotice も同条件 (`tmp/wt/outnotice/src/agentic_fx/datafeed/outage.py:537-550`)。本件で変更 |
| 11 | 通知設計のロールオーバー帯例外・flat 解除は未裁定 | 通知設計 `docs/superpowers/specs/2026-11-21-datafeed-outage-notice-design.md:241-242` は旧提案。本件の 2 チケットの承認済み裁定を優先 |
| 12 | 通知設計の待ち印・拒否で次の手動操作が決まる | 同設計 `:157-169` と worktree `outage.py:47-80` は新しい自動確認を知らない。本件の recovering 等を先に評価 |
| 13 | auto_resume_possible は pending があると常に false | worktree `tmp/wt/outnotice/src/agentic_fx/datafeed/outage.py:320-326`。flat の現在証拠に従う判定へ変更 |
| 14 | check_exit(entry_same_bar=True) で全接触を拾える | TP は None に抑止 (`paper_fills.py:53-57`)。接触可能性の検査には False の結果も使う |
| 15 | 現在の orders の保護条件だけで後着判定できる | orders は上書き保存 (`src/agentic_fx/store/orders.py:38-47`)。当時の版が不明なら非接触と証明できない。永続 snapshot を追加 |
| 16 | 通知の通知表は安全状態と同じ transaction | outnotice は後段の独立保存 (`tmp/wt/outnotice/src/agentic_fx/datafeed/outage.py:869-900`)。その失敗は安全状態の commit を取り消さない |

## 変更履歴

| 日付 | 版 | 変更 | 理由 | commit |
|---|---|---|---|---|
| 2026-11-21 | C0 | 短い欠けと flat 自動解除を統合。期限、接触、全 exposure、通知・merge 順、受入境界を定義 | 2 チケットの承認済み方針を具体化し、実欠けの形と現行実装を区別する | —（未 commit） |
