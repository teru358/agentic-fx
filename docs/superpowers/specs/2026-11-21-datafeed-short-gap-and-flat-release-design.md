# 1 分足の穴の受容・停滞時の保護と flat 後の自動復帰 設計 C0.3

後続の 1 分足が届いている穴は、時間帯・連続本数・発生回数に依らず受け入れる。停滞として観測される間は新規の発注を 1〜2 分止めることがあるが、後続が届き現在の停滞が解けたら通常の ready 判定に戻す。足は合成せず、出来高・指標・backtest の系列を MT5 履歴と揃える。

穴の約 2 分後の取り直しは状態と独立した best effort の背景検証とし、復帰を待たせない。同 source の正常応答で前後と対象範囲を確認できた場合、残る穴は「取引なし」と認定する。失敗・空・予算切れは次の 3 tick まで再試行し、なお確認できなければ activity と status に注記を残し、停止理由にしない。後着足の SL/TP・指値への接触（または接触判定の不明）を検出した場合だけ、flat でも degraded・人の再開待ちにし、停止通知を出す。約定・決済を replay で変更しない。

新しい足が来ない連続時間が 5 分以内なら、建玉ありでも restricted とする。取得失敗・空応答も同じ扱い。新規・指値の新規約定・リスク増を止め、決済・取消・既存 SL/TP の保護は続ける。新しい足が届けば停滞の計時を終え、次の停滞は新しい起点から数える。5 分超は degraded・人の再開待ち。flat は既存の `flat_stall_max_sec=1800` 秒を維持する。通常の degraded は全 exposure ゼロ・健全 3 tick で自動解除するが、接触による停止は自動解除しない。自動 ready は `auto_resume_when_flat=true` の場合に限る。

作成・改訂日: 2026-11-21 JST。方針は `docs/tickets/open/datafeed-rollover-band-expected-gap.md:26` の改定裁定と `docs/tickets/open/datafeed-pending-auto-release-when-flat.md:23` に従う。旧裁定の「1 本だけ」「穴だけで restricted」「合計 2 本で degraded」は置き換える。以下の保存形式・観測境界は設計判断。今回は本書のみ改訂し、実装・テスト実行・配備は行わない。

## 確認した実体

短縮名: `outage.py` は `src/agentic_fx/datafeed/outage.py`、`ingest.py` は `src/agentic_fx/datafeed/ingest.py`、`scheduler.py` は `src/agentic_fx/core/scheduler.py`、`paper_fills.py` は `src/agentic_fx/core/paper_fills.py`。通知 worktree は `tmp/wt/outnotice`。行番号は今回の実ファイルを照合した値で、main と worktree を混同しない。

| 事項 | 実体・根拠 |
|---|---|
| 停止と穴は別の数 | epoch 52–66 の入口は 15 件、うち 14 件が 22 時台 UTC。例は `logs/activity.log:12671`, `:13043`, `:14102`, `:15530`。下記 SQL の穴は 11 夜・35 区間・42 分。restricted 中の後続の停滞は streak をリセットするだけで epoch を増やさない (`outage.py:407-408`) |
| 現行の停止・復帰 | exposure があれば即 degraded (`outage.py:373-388`)。flat は 1800 秒の restricted (`:370-384`)。degraded の自動解除は `not pending` が必須 (`:409-418`) |
| 健全の定義 | 取得結果と最新 watermark の停滞を見る (`outage.py:302-329`, `:548-569`)。上位 hard 足は当 tick の failed/empty/stalled が無ければ前回結果を据え置き、未知を false としない (`:317-326`) |
| 通常取得 | `ingest.py:211-225` の下限は watermark の 1 本前。古い穴は自動では取り直さない。閉場中は取得しない (`:159-162`)、上位足は次の確定まで取得待ち (`:248-252`) |
| MT5 の足 | `mt5_bridge/mt5_client.py:675-705` は rates をそのまま変換し tick_volume を volume に写す。None は例外、空は空。無 tick 分の補間は無い。cache の不在だけで無 tick を証明はできない |
| 保護が見る足 | 本番の bars_fn は DB の最新 1m だけ (`src/agentic_fx/service.py:1829-1832`, `:1878`)。重複防止は `scheduler.py:942-949`、通常保護は OPEN 走査 (`:1786-1797`)。鮮度 5 分超は最新でも除外 (`scheduler.py:44`, `:945`)。同 tick に複数入る過去足を drain しない |
| SL/TP と価格 gap | `paper_fills.py:32-58` は SL/TP 判定。gap 滑りは open と保護価格の価格差であり、時系列の穴の replay ではない。shadow の連続性検査 (`:104-130`) も別用途 |
| 注文の保護条件 | direction・requested_price・SL/TP は発注 insert で設定 (`src/agentic_fx/core/executor.py:679-687`)。Action は OPEN/CLOSE/CANCEL/HOLD (`src/agentic_fx/core/contracts.py:12-16`)。`src/agentic_fx/store/orders.py:38-47` は汎用更新だが、src の更新呼出しに保護価格を書き換える経路はない。毎 tick の注文 snapshot は不要 |
| exposure と近似件数 | 全集合は `src/agentic_fx/core/executor.py:29-31`。既存の未処理近似は OPEN/PENDING_FILL と時刻だけ (`outage.py:669-718`) で、全 exposure ゼロや適用済みを証明しない |
| cache の書き手 | 本番 scheduler は DB を読むだけ。ingest が取得結果を保存 (`ingest.py:316-320`)。provider の保存は readonly で抑止 (`src/agentic_fx/datafeed/price_provider.py:220-221`, `:425-426`)。healthcheck も readonly (`src/agentic_fx/service.py:1599-1610`)。この本番経路で scheduler が cache を温めるという `service.py:1602-1606` の注記は実体と異なる |

## 過去 11 夜への当てはめ

本番 DB は次の readonly SQL だけで調べた。出力は時刻と件数のみ。これは現在残る cache の穴の形であり、当時の取得応答・実際の到着時刻・後着足の接触を再現するものではない。休場をまたぐ差はここでは 600 分以上として除き、実装では後述の期待時刻列を使う。

```sh
sqlite3 -readonly data/agentic.db "WITH b AS (
 SELECT bar_time,julianday(bar_time) j,
 LAG(julianday(bar_time)) OVER (ORDER BY bar_time) pj,
 LAG(bar_time) OVER (ORDER BY bar_time) pt
 FROM ohlcv_cache WHERE symbol='USDJPY' AND interval='1m'
 AND source='mt5-live' AND bar_time>='2026-11-01')
 SELECT pt,bar_time,CAST(round((j-pj)*1440)-1 AS INT) missing
 FROM b WHERE (j-pj)*1440>1.5 AND (j-pj)*1440<600 ORDER BY bar_time;"

sqlite3 -readonly data/agentic.db "WITH b AS (
 SELECT bar_time,julianday(bar_time) j,
 LAG(julianday(bar_time)) OVER (ORDER BY bar_time) pj
 FROM ohlcv_cache WHERE symbol='USDJPY' AND interval='1m'
 AND source='mt5-live' AND bar_time>='2026-11-01'), g AS (
 SELECT date(bar_time) night,CAST(round((j-pj)*1440)-1 AS INT) n
 FROM b WHERE (j-pj)*1440>1.5 AND (j-pj)*1440<600)
 SELECT night,count(*),sum(n),max(n),sum(n>5) FROM g GROUP BY night;"
```

時系列の計算も readonly SQL で確認した。grace=30 秒を仮定し、直前足 `pt` から `S=pt+150秒`、後続足から回復可能時刻 `R=bar_time+90秒` を算出する（`market_hours.py:85-88`, `ingest.py:224-225`）。

```sh
sqlite3 -readonly data/agentic.db "WITH b AS (
 SELECT bar_time,julianday(bar_time) j,
 LAG(julianday(bar_time)) OVER (ORDER BY bar_time) pj,
 LAG(bar_time) OVER (ORDER BY bar_time) pt
 FROM ohlcv_cache WHERE symbol='USDJPY' AND interval='1m'
 AND source='mt5-live' AND bar_time>='2026-11-01')
 SELECT pt,bar_time,datetime(pt,'+150 seconds') S,
 datetime(bar_time,'+90 seconds') R,
 CAST(round((j-pj)*1440)-1 AS INT)*60 restricted_sec
 FROM b WHERE (j-pj)*1440>1.5 AND (j-pj)*1440<600 ORDER BY bar_time;"

sqlite3 -readonly data/agentic.db "SELECT bar_time FROM ohlcv_cache
 WHERE symbol='USDJPY' AND interval='1m' AND source='mt5-live'
 AND bar_time BETWEEN '2026-11-13T12:29:00' AND '2026-11-13T12:36:00'
 ORDER BY bar_time;
 SELECT pair,interval,epoch,gap_start FROM datafeed_outage_gap WHERE epoch=57;"
```

以下は UTC。穴欄は 22 時台の足の開始分、両端を含む。`S→R` は原則 22 時台の分:秒。通常取得が確定直後に追随し、auto=true・接触なし・他の hard 不調なしとした設計の当てはめであり、実測の restricted 長ではない。実際は `S` を過ぎた最初の tick から `R` 以後の最初の健全 tick までとなる（約 60.3 秒周期）。

| 夜 (2026 年) | 穴の開始分 | 区間数 / 欠け分数 / 最大連続 | restricted の S→R、各回の長さ | 回復 |
|---|---|---|---|---|
| 11-01 | 07、12、34、37–40、53、57–58 | 6 / 10 / 4 | 08:30→09:30、13:30→14:30、35:30→36:30、38:30→42:30、54:30→55:30、58:30→23:00:30（1/1/1/4/1/2 分） | 各回 E、間は ready |
| 11-02 | 13 | 1 / 1 / 1 | 14:30→15:30（1 分） | E |
| 11-03 | 08、11、43 | 3 / 3 / 1 | 09:30→10:30、12:30→13:30、44:30→45:30（各 1 分） | 各回 E |
| 11-05 | 07、09、21、27、34 | 5 / 5 / 1 | 08:30→09:30、10:30→11:30、22:30→23:30、28:30→29:30、35:30→36:30（各 1 分） | 各回 E、07 と 09 の間も ready |
| 11-08 | 09、16、25、29、48–50 | 5 / 7 / 3 | 10:30→11:30、17:30→18:30、26:30→27:30、30:30→31:30、49:30→52:30（1/1/1/1/3 分） | 各回 E |
| 11-09 | 02–03 | 1 / 2 / 2 | 03:30→05:30（2 分） | E |
| 11-15 | 01 | 1 / 1 / 1 | 02:30→03:30（1 分） | E |
| 11-16 | 02、05、13、16、29 | 5 / 5 / 1 | 03:30→04:30、06:30→07:30、14:30→15:30、17:30→18:30、30:30→31:30（各 1 分） | 各回 E |
| 11-17 | 03、09、23、25 | 4 / 4 / 1 | 04:30→05:30、10:30→11:30、24:30→25:30、26:30→27:30（各 1 分） | 各回 E、23 と 25 の間も ready |
| 11-18 | 17 | 1 / 1 / 1 | 18:30→19:30（1 分） | E |
| 11-19 | 39、41、43 | 3 / 3 / 1 | 40:30→41:30、42:30→43:30、44:30→45:30（各 1 分） | 各回 E、取り直し待ちで連結しない |
| 合計 | 11 夜 | 35 / 42 / 4 | 各停滞は 1〜4 分 | 穴を理由とする F は 0 |
| 11-13（穴なし、epoch 57） | 12:29〜12:35 の実足は全て存在 | 0 / 0 / 0 | S=12:33:30、restricted 実測入口 12:33:53。境界 12:38:30、12:38:54 は未試行、回復は 12:40 台（約 7 分） | 建玉ありは F、flat は 1800 秒内で復帰可能 |

11-05 の 07/09、11-17 の 23/25、11-19 の 39/41/43 の背景 due はそれぞれ 10:30/12:30、26:30/28:30、42:30/44:30/46:30。後の due を待って restricted を延長しないため、2 分間隔の反復でも停止が連続せず、建玉ありでも穴だけで degraded にならない。exposure が穴と重ならなければ取り直し自体を省く。

epoch 57 は DB の gap_start=12:31 と期待確定式から S を照合した。入口・旧仕様の健全 3 tick 後の復帰 12:42:54 は `logs/activity.log:14102-14103`、12:37〜12:39 台の時刻規則未検証は `logs/agentic.log:65553-65557`。cache に穴が無いことは停滞が無かった証明ではない。

この 11 夜について示したのは穴の形に対する復帰であり、全停滞の無停止を主張しない。cache の始まりは 11-01 22:01 で、22:00 不在が保持境界か日曜最初の穴かは不明。過去の到着時刻・取得失敗・接触はこの SQL では復元できない。

## 穴と停滞の検出・時刻

対象は registry の hard 1m key `(pair, interval, storage_source)`。soft 系列の不調は昇格理由にしない。上位 hard 足の失敗・空・停滞は現行規則を維持する。上位足の内部欠けを新たに全列挙したり、次の 15m 成功を待ったりしない。

足時刻は UTC 開始時刻、区間は半開 `[start,end)`。期待時刻列は `src/agentic_fx/core/market_hours.py:77-88` と同じとし、週末・祝日・DST は `:30-43` の開閉場を使う。「直前」「後続」はこの列上の前後であり、単純な ±60 秒ではない。

- key ごとの検査端点を state と同じ transaction で保存する。端点から今回の最新確定足までの期待時刻列と同 source の実足を比較し、後続のある内部穴を登録する。末尾未到着は停滞であり、不在認定しない。複数 pair・連続・反復の穴を個別に扱う。
- 一括到着では検査端点から進んだ過去足を接触検査する。最新足は通常保護で評価できる場合だけ引き渡し、鮮度切れなどで評価できない後着足は最新でも本設計で検査する。金曜末尾足の日曜到着も対象。取り直しでは元の穴の時刻集合に現れた足と、要求範囲内で保存済み指紋から内容が変わった足を検査する。前後の実足は範囲確認に使い、内容不変で、接触検査または通常保護の評価完了を保存済みの足だけ再検査を省く。検査端点の通過だけでは評価済みとしない。
- 停滞の起点 `S` は最後の watermark の次の期待足の確定予定時刻（grace 込み、`outage.py:557-569`）。取得失敗・空応答も「新しい足が来ない」連続時間として同じ期限で扱い、水位がない初回の失敗だけはその観測時刻を S とする。失敗・空の繰返しで S を延ばさない。
- 新しい確定足が届けば、旧 S の期限内回復かを先に判定して、それまでの連続停滞の計時を終える。なお最新期待足が未到着なら、その最新水位から次の S を求める。古い穴の due は新しい S と無関係であり、穴が反復しても足が届くたびに計時を切る。現在も停滞していれば restricted、現在の停滞が解け同 tick の健全条件が揃えば E。
- S 以後の開場秒数で、exposure ありは 300 秒、flat は `flat_stall_max_sec` 秒を上限とする。休場は除外し、0 設定は flat の猶予なし。flat↔exposure の変化だけでは S を動かさず期限を切り替える。
- 5 分ちょうどは猶予内。境界以降最初の開場 tick で通常取得結果を先に照合し、回復足の本来の確定時刻が境界以内なら期限内回復とする。境界外の足しかない、依然停滞、backoff・時刻規則未検証などで 1m が未試行なら回復なしとして F。成功でも古い水位のままなら回復ではない。背景検証の成否はこの判定に含めない。
- tick 冒頭の now を prepare/observe に共用する（`src/agentic_fx/service.py:2480-2485`, `:2938-2962`）。再起動・tick の長時間停止で境界を逃しても期限超過を保持し、不明な到着時刻を推測して期限内回復としない。初観測が ready のまま期限超過なら直接 F とする（現行も `outage.py:362-375` で直接 degraded）。

## 背景の取り直しと保存

穴の初回発見を `detected_at`、最初の欠けた足の確定予定時刻を `missing_since`、`probe_due_at=missing_since+120秒` として保存する。生存期間が穴と重なる exposure が 0 件なら候補を省き、取り直さない。現在 terminal でも穴の当時に生存していれば対象とする。重複の有無を確定できない場合は省略しない。

取り直しは state・auto 設定に依らず、due 以降最初の開場 tick から行う。通常取得（hard/soft と既存の backoff を含む）を先に済ませ、既存 ingest budget の残りがある場合だけ実行する。残りが無ければ次の tick へ回す。通常取得を probe のために遅らせない。成功した範囲検証は 1 回で終了し、完了した候補を無期限に取り直さない。

`prepare(now, gap_probe=...)` は保存済み due 候補を読んで要求するだけとし、書込みなしの契約（`ingest.py:138-142`）を維持する。穴の発見・候補作成・結果適用・端点更新は後述の `transition()` だけで計算し、observe の短い core_lock 内 transaction で保存する。発見時すでに due 済みなら、その tick の通常応答が必要範囲を覆う場合はそれを検証結果に使う。覆わなければ保存後の次 tick の候補となる。prepare に穴判定や開始印の保存を重複実装しない。

同じ primary/source に穴の直前実足から直後の最初の実足までを要求する。半開 range の終端は直後足の開始+1 足幅とし、その足を含める。now まで広げず、fallback・tick 取得・補間をしない。同 key の重なる範囲は統合してよいが、間の内容不変・評価完了保存済みの足は接触検査しない。report は候補 ID・source/key 世代・要求範囲・cutoff・応答時刻・全確定足と指紋・結果を持つ。

不在認定と実足の接触検査は独立させる。同 source・現 key 世代で、時刻・内容が有効と確認できた対象足は、切詰め・前後不足などで範囲を網羅できなくても直ちに検査する。接触・接触不明は A を優先し、残る未確認範囲だけを再試行・注記にする。source 不一致・時刻や内容が無効な足を有効な接触証拠として扱わない。

| 結果 | 扱い |
|---|---|
| 同 source の正常非空応答、前後の既知実足と対象範囲を確認でき、穴の足は不在 | `absent_validated`。取引なしと認定して候補を完了 |
| 一部または全部が後着 | 有効な対象足を直ちに検査し、接触/不明なら A。残る穴の不在認定は範囲確認の成立時だけ行い、非接触だけでは state を変えない |
| 空・例外・時刻規則未検証・切詰め・前後不足・source 不一致・予算切れ | 有効な実足の検査後、未確認範囲を初回機会の後、次の 3 開場 tick で再試行。予算切れや未試行も各 1 回と数える |
| 再試行しても検証不能 | `unverified` と理由・回数を activity と status 注記へ保存し、候補を完了。未確認範囲を停止・復帰・手動再開の gate にしない。不在認定もせず、実足で検出した接触の停止は保持 |
| 閉場・source/key 世代が変わった結果 | 閉場は試行に数えず due を保持。古い世代の応答は破棄 |
| 応答後 crash・結果未保存 | 読取りは冪等なので再起動後に再送可。開始印を保存せず、保存済み試行回数と due から続行 |

独立候補 ID は epoch に寿命を縛らない。保存項目は key/source/穴と要求範囲、detected_at/missing_since/due、試行回数・最終結果・理由・完了時刻、spread、対象足の指紋・接触種別・注文参照。過去の不在の恒久索引や毎 tick の注文 snapshot は作らない。

cache 保存後、core_lock 内の明示 transaction で端点・通常保護への引渡し待ち・候補・state・接触の確認待ち・streak と status facts（直近 observe の健全真偽と時刻）を保存する。保存に失敗したときは成功・解除を記録せず gate を閉じるが、probe の取得失敗を保存障害と混同しない。observe 失敗でも保護 tick は続ける（`src/agentic_fx/service.py:2525-2536`）。現行の自動復帰には古い穴の再検証自体が無いため（`ingest.py:211-225`, `outage.py:389-418`）、既存の自動復帰に背景の安全確認を追加する扱いとする。

通常保護へ渡す足は observe の transaction で key/source 世代・足時刻・指紋・足の内容・spread を引渡し待ちとして保存する。現行は commit→observe→scheduler の順（`src/agentic_fx/service.py:2502-2536`）で、端点保存は保護完了を意味しない。通常保護から対象足・関連注文の評価完了を明示的に受け、完了報告を `transition()` に渡し、core_lock 内の短い transaction で待ちの解消と評価完了を保存する。鮮度除外・注文単位の例外・指値判定の gate による省略は完了に数えない。`scheduler.py:1794-1813` は例外を隔離し、`:1827-1835` は足時刻を記録するため、tick の正常終了や `_processed_bar_ts` だけを完了証拠にしない。未評価分は次の observe で後着検査へ回し、その結果と待ちの解消を state と同時保存する。

### 起動・停止中の区間

開場 observe が 1 回 commit されるまで起動 gate を閉じる。閉場中は完了に数えない（`outage.py:280-284`）。端点が無ければ現在の水位から始め、activity に記録し、過去全履歴を遡らない。既存 non-ready/pending は消さない。

再起動は端点・通常保護への引渡し待ち・候補・due・試行回数・S・接触の確認待ちを復元する。完了保存前の crash では、待ちに保存した足・spread を後着検査へ回し、その結果と待ちの解消を最初の開場 observe で保存してから起動 gate を開く。通常保護の実行後でも完了保存前なら同じ扱いとする。cache が進んだ場合や対象足が保持範囲外でも保存した足を使う。足や必須情報を読めなければ B とし、単なる probe 検証不能に落とさない。停止中の内部穴も受容し、対象 exposure があれば背景検証する。後続もない停滞は旧 S で期限判定する。保持期間外などで検証できない候補は上の再試行・注記で終え、穴の古さだけで停止しない。実足を得たが保有・保護条件を判定できない場合は次節の接触不明とする。streak は再起動で 0、due と連続停滞の期限は延ばさない。

## 後着足の接触検査

純関数 `protection_contact(orders, bars, spread)` で評価し、注文・約定・損益を書き換えない。対象は穴や一括到着した足、通常保護が評価できない後着足（最新・金曜末尾を含む）、引渡し待ちから回収した足の区間と生存期間が重なる exposure 全体。現在 closed/cancelled でも、その区間に生存した可能性があれば除外しない。

毎 tick の履歴 snapshot は保存しない。direction / requested_price / stop_loss / take_profit は insert 後不変とし、`orders.update_fields` の実行時ガードで更新を拒否する（`src/agentic_fx/core/transitions.py:132` は keyword 引数をそのまま渡す）。生存期間は `created_at` から `closed_at`、他の終端なら `updated_at` までを保守的に長く取る。非終端は上限なし。`filled_at` が確実な場合だけ保有開始を絞る。時刻不整合・broker 結果不明は安全と断定しない。spread は候補作成時の `assumed_spread_pips × pip_size` (`scheduler.py:1837-1842`) を固定し、再起動後の設定で置換しない。一括到着にはその tick の `_spread_for` の値を使い、接触記録にも残す。

| 区間と重なる状態・証拠 | 判定 |
|---|---|
| OPEN、または確実に保有した履歴、方向・有限 SL・任意の有限 TP が有効 | `paper_fills.py:34-43` と同じ half spread で SL/TP の接触真偽を評価。long は low/open−half ≤ SL、high−half ≥ TP、short は high/open＋half ≥ SL、low＋half ≤ TP。両方も記録。決済価格の計算は不要 |
| 未約定が確実な PENDING_FILL、または約定せず終端 | 指値到達（long: open/low＋half ≤ 指値、short: open/high−half ≥ 指値、`paper_fills.py:14-29`）または SL/TP 到達なら接触。到達の真偽だけを評価し、仮約定・同足 replay はしない |
| protection_pending / submitting / submitted / closing / cancelling / submit_unknown / cancel_unknown / close_unknown | 保有の有無・保護を確定できなければ `late_bar_contact_unknown`。単に OPEN でない理由で除外しない |
| 方向・SL・spread・生存時刻の欠損や非有限値、状態不明 | `late_bar_contact_unknown` |
| 正常な再取得で実足が無い | 実足の接触検査はなし。「取引なし」の裁定を適用 |

接触は `late_bar_protection_contact`、不明は `late_bar_contact_unknown` として degraded・pending・接触の確認待ちを永続化する。不明も自動解除しない。SL/TP/指値/両方/不明、足時刻、注文参照、判定関数版を内部記録に残す。activity と ctl status に注文 ID・足時刻・接触種別を出す。Discord は固定本文とし、価格・金額・口座情報を出さない。後から非接触の足を見てもこの確認待ちを消さない。

## 復帰と状態機械

flat は全 pair を通じて `orders.list_by_status(conn,*_EXPOSURE)` が 0 行。集合は open / pending_fill / protection_pending / submitting / submitted / closing / cancelling / submit_unknown / cancel_unknown / close_unknown (`src/agentic_fx/core/executor.py:29-31`)。quantity=0 でも非終端なら flat ではない。未知 status・読取り失敗は flat 不明。approval_pending は含めないが、承認後 submit には既存 gate を適用する。

健全は現行 `outage.py:317-326` を基礎に、1m hard 全 key の非空成功と水位回復を使う。上位 hard 足に episode 内の初回成功を追加要求しない。受容した内部穴に連続性を要求しない。degraded の flat 自動解除だけは、flat 成立後に水位が進む健全 3 tick を要求する。同じ now・同じ足の再取得は加算せず、失敗・空・deferred・停滞・exposure 再発で 0。閉場・再起動でも 0 から確認する。scheduler の決済は observe の後なので、全決済の次の observe から数える。

通常 pending は表示と履歴の印で、flat 自動解除の gate ではない。接触の確認待ちは自動解除を禁止する。穴の取り直しは auto の値に依らず行う。`auto_resume_when_flat=false` が変えるのは自動 ready だけで、停滞回復時は健全 3 tick で degraded の手動待ちへ進む。手動再開に背景検証の完了は要求しない。

**`transition(row, facts) -> (row_next, effects)`** 一つに、穴の発見・候補作成・試行結果・検査端点・引渡し待ちと評価完了・状態の計算を集める。facts は保存済み端点・通常保護への引渡し待ちと完了報告・候補、通常応答・probe 結果、注文履歴・spread、健全、時刻・設定・手動要求。`_observe_open` と `_consume_resume_request` はその結果だけを保存する。A/B は安全優先、C は状態判定と独立、G の上位 hard 不調は D/E より優先する。D〜I が自動判定、J/K がその後の手動要求消費、L/M が共通の順序・閉場規則。行 ID を test に対応させる。

| ID | 入力・現状態 | 次状態・効果 |
|---|---|---|
| A | 対象足に接触または接触不明、任意 state・flat の別なし | degraded、pending=1、接触の確認待ち、streak=0、停止通知。ready なら停止 epoch を開く |
| B | 保存障害・必須状態の読取り不能 | gate を閉じ解除しない。保存可能なら degraded、streak=0。ready からなら停止 epoch を開く。背景 probe の失敗・unverified は含めない |
| C | 内部穴の発見・背景検証だけ | 候補・結果・注記を保存し、state・epoch を変えない。D〜K は通常 facts から別に判定 |
| D | 1m 停滞・失敗・空、連続未到着が猶予内、ready/restricted | restricted。exposure ありでも即昇格しない。後続で水位が進めば古い計時を終える |
| E | 期限内 restricted、同 tick に gate key 全て succeeded・非空・現在非停滞、上位 hard も健全、auto=true | ready、pending=0。取り直しの due・成否・未完了を待たず、追加の健全 3 tick も要求しない |
| F | 連続未到着が期限超過、または期限外の足だけで回復、ready/restricted | degraded。初観測ですでに期限超過の ready は restricted を経由せず停止 epoch を開く。exposure ありなら pending=1。flat は同じ S の 1800 秒（設定値）を適用 |
| G | 上位 hard 足の停滞・失敗・空 | 現行規則：flat は 1800 秒の restricted（失敗・空は現行どおり即 degraded）、exposure ありは即 degraded。内部穴の数と混ぜない |
| H | degraded、接触待ちなし、flat、auto=true、健全 | 健全 1〜2 tick は維持、3 で ready と pending=0 を一括保存。背景検証待ちは条件にしない |
| I | degraded、接触待ちあり / exposure あり / auto=false | 自動解除なし。健全なら人の再開待ち。auto=false の restricted 回復も健全 3 tick でこの待ちへ |
| J | 手動要求、不健全 | `rejected_watermark`。背景候補の未完了・unverified を理由に拒否しない |
| K | 手動要求、健全 | 未処理近似と接触/不明の注文参照の和集合を ack 対象とする。未 ack は `rejected_unprocessed`。接触は flat でも ack 必須。受理時に接触待ちも解除 |
| L | 自動判定と手動要求の競合 | 観測→自動判定→要求消費。先に ready なら `already_ready`。要求 key の比較更新で置換済み要求を消さない |
| M | 閉場 | 要求・probe は保留、streak を増やさず、due/S を動かさない。開場後最初の観測へ |

不変条件:

1. non-ready でも決済・取消・close retry・既存 SL/TP の保護を続ける（`scheduler.py:342-348`）。
2. 後続ありの穴は数・時刻で止めず補間しない。失敗・空を不在認定せず、背景検証不能は注記で終える。
3. 一括到着の過去足、穴への後着・内容変更足、通常保護が評価できない後着足、引渡し未完了の回収足を検査する。内容不変・評価完了保存済みの足の接触を新しい停止にしない。
4. 5 分は連続未到着ごとに測る。後続到着で計時を終え、背景 due は restricted を延長しない。再起動・deferred・ack だけで期限を延ばさない。
5. degraded の自動解除は全 exposure ゼロを要求し、接触待ちは flat でも解除しない。期限内 restricted の E は exposure ありでも許す。
6. データ停止の解除で他の kill switch・リスク・承認 gate を解除しない。

## 通知と status

outnotice を先に merge する。その実体の語彙は `tmp/wt/outnotice/src/agentic_fx/datafeed/outage.py:30-34`、表示表は `:48-81`、要求比較更新は `:854-869`。C0 の引用から行番号が動いているため、今回読み直した位置を使う。

| 事実 | 通知の記録・confirmed |
|---|---|
| ready 中の穴の発見・取り直し・非接触・不在 | Discord 無通知。停止 epoch も problem_streak も進めない。内部記録のみ |
| 期限内 restricted・期限内自動復帰 | 従来の短い restricted と同じく無通知。activity に復帰理由を記録 |
| 接触/不明による degraded | 同 tick に confirmed=1、候補 ID（一括到着は key・足指紋から作る永続検出 ID）を request_key として既存固定本文の stopped を検出単位で 1 回記録。flat・既存の stopped/待ち印・要求中でも抑止しない |
| 通常の失敗・停滞による degraded | outnotice の連続問題 2 tick / 開場時間 10 分の停止条件を維持。restricted 中の停滞も problem_streak は進めるが通知は degraded でのみ判定 |
| probe の失敗・再試行終了 | Discord 無通知。activity と status 注記だけ。state・confirmed・problem_streak を変えない |
| 回復後の人の待ち | awaiting_resume を同 epoch 1 回。接触の stopped を先に保存し、両方が初回なら順序も固定 |
| 通常 degraded の flat 自動解除 | 停止/待ち記録のある epoch だけ既存 resumed を 1 回。自動確認中は新しい awaiting_resume を出さない |
| 手動要求 | accepted/rejected/already_ready を必ず終端記録。接触 episode の受理は既存の手動再開通知を使う |

現行の一意キーは `(epoch,notice_state,request_key)`（worktree `outage.py:931-935`）。接触は通常停止の空 request_key と分け、`outage.py:575-576`, `:908-915` の抑止を通さず記録する。配送 retry は outnotice を継承する。安全状態 commit 後の別 transaction で保存する (`tmp/wt/outnotice/src/agentic_fx/datafeed/outage.py:879-888`, `:925-944`)。記録前 crash の欠落限界は同じだが、接触の確認待ちから起動時に stopped の未記録を再照合する。通知失敗で保護 tick や安全状態を戻さない。価格・金額・口座情報を本文に含めない。

status は observe が保存した健全真偽・時刻、ready_streak、state、接触待ち・参照、未完了候補と最終結果から一つの `resume_guidance` で決める。API 側で健全を再計算しない（worktree `outage.py:48-56`）。既存の unknown → 要求あり → ready を最優先にし、以後は下表を古い拒否・待ち印より先に判定する。

| 条件 | phase | next_action |
|---|---|---|
| restricted、現在不健全 / auto=false の健全確認中 | restricted | wait_tick |
| degraded、接触/不明の確認待ち、健全 | awaiting_resume | resume_with_ack |
| degraded、接触待ちなし・flat・auto=true・健全確認中 | recovering | wait_tick |
| degraded、現在不健全 | stopped | restore_source |
| degraded、手動待ち、ack 対象あり / なし | awaiting_resume | resume_with_ack / resume |
| 必須事実の読取り不能 | unknown | check_status |

要求の終端結果は既存のまま（worktree `outage.py:34`）。未完了・検証不能候補は ready を含む各 phase の独立した注記として理由・回数・区間を出し、再開を拒まない。ack 件数は既存近似だけでなく接触/不明の参照も重複除去して含めるため、近似 0 でも案内が resume に戻らない。過去の拒否は別欄に保持し、現在の recovering より優先しない。`auto_resume_possible` も pending 一つで決めず、この表の条件に揃える。

API の内部状態 4 項目の追加は取りやめ、phase・既存案内と今回必要な背景検証の注記・接触参照を公開する。`guidance_text.py` の許可組・ctl・web を同時に更新する。recovering は「健全な足の確認を続けています。条件が揃えば自動解除します」。旧クライアントは未知 phase を unknown と表示する。

## 実装と適用順

先に outnotice を main に入れ、その後の schema_version で migration を追加して旧 fixture を更新する。flat 自動解除は接触検査・例外と同じ有効化単位にし、単独で先に動かさない。既存 epoch 66 への適用もこの条件を守る。各変更の検証は実装時に行う。

| 変更 | 内容 | 前提 |
|---|---|---|
| 保存形式 | 検査端点、通常保護への引渡し待ちと評価完了、独立候補・due/試行回数/結果/理由/spread/指紋、接触待ち・注文参照、直近 observe 健全真偽と時刻・ready_streak の保存、期待時刻列、冪等 migration | outnotice |
| 取得 | 保存済み候補を読むだけの prepare、通常取得後の残 budget による限定範囲要求、report・cache 保存。発見時 due 済みは通常応答を利用、開始印なし・crash 再送可 | 保存形式 |
| 接触判定 | exposure 全体の生存重複・接触純関数、指値到達、対象足と前後確認足の区別、通常保護で評価不能な最新足の検査、一括到着の spread、update_fields の保護条件更新ガード | 保存形式 |
| 状態遷移 | transition に発見・候補作成・結果・端点・引渡し待ちの回収・表 A〜M を集約。部分応答の有効実足を先に検査し、残範囲だけ再試行。通常保護の完了報告を保存。連続未到着 300/1800 秒、回復即 ready、best effort と次の 3 tick の再試行・注記、接触 stopped の別キーと status facts を接続 | 取得、接触判定。有効化は flat 解除・起動回収・表示と運用手順と揃える |
| flat 解除 | 通常 pending の flat・健全 3 tick 解除、接触待ちの除外、recovering 表示・通知。epoch 66 にも適用 | 保存形式、状態遷移。接触の例外と同時か後に有効化 |
| 起動回収 | 起動 gate、停止中区間・引渡し未完了の crash 回収、手動 ack 集合・要求競合。未完了/unverified による拒否なし、auto=false でも背景検証 | 状態遷移 |
| 表示と運用手順 | ctl/web・afx-ops の固定語と許可組、設定 example の ready_confirm_ticks/auto の範囲、outage docstring、cache 注記、通知設計・運用手順を同期 | flat 解除、起動回収 |
| 統合受入 | 11 夜の時系列、穴なし停滞、境界・接触・通知を fake/Dunia で統合受入 | 表示と運用手順 |

## test と受入

以下は実装時の検証であり、今回は test を回さない。表 A〜M を入口 × state で検証し、observe と手動要求が同じ facts で同じ結論になることを確認する。

| 境界・入力 | 期待 |
|---|---|
| 内部穴 0/1/連続 2〜4/多数・任意時刻・複数 pair | C。後続ありなら穴の数で停止しない。補間なし |
| 11 夜、特に 11-05 の 07/09、11-17 の 23/25、11-19 の 39/41/43 | 各停滞の S→R で D→E、間は ready。古い due・失敗が次の restricted を延ばさない |
| due 直前/ちょうど、連続 4 分後の初回発見 | due 以降最初の機会に検証。発見時 due 済みは通常応答を使い、不足なら次 tick。state は待たない |
| 停滞・通常取得の失敗/空、300 秒直前/ちょうど/超過、回復足の確定が期限内/外 | D/E/F。失敗/空だけで即 degraded にしない。新しい足で連続計時を切り、現在非停滞・同 tick 成功の E は 3 tick を待たない |
| ready で停止・建玉あり、開場時間で 5 分超の再起動/長時間 tick 停止、後続なし | 最初の observe で直接 F、停止 epoch を開き pending=1。restricted を経由しない。flat は同じ S から設定期限を判定 |
| 境界 tick の backoff 待ち・時刻規則未検証・未試行、epoch 57 の穴なし約 7 分停滞 | 回復なしで F。epoch 57 は建玉ありなら 5 分超で degraded |
| 通常取得成功後に budget なし、probe が空/例外/不整合/source 違い | 通常取得を優先。初回機会と次の 3 tick（予算切れも数える）で打切り、注記だけ。停止・手動拒否なし |
| 前後・範囲確認の成立した正常非空で全不在/一部後着/全部後着 | 不在を認定し、後着対象足を検査。非接触は無処置 |
| 前後不足・切詰めの部分応答に有効な接触足/非接触足あり | 接触なら即 A、停止通知・接触待ちを保存。残範囲だけ再試行し、打切り注記でも接触停止は消さない。非接触でも範囲確認なしに不在認定しない |
| 穴と重なる exposure なし、現在 terminal だが当時は生存 | 前者は probe を省略、後者は検査。flat の取得失敗で復帰を退行させない |
| 穴の直後の足で通常 SL 決済、その後 probe。内容不変/変更の前後実足 | 内容不変・通常保護の評価完了保存済みの足は無処置。後着・変更足は対象として評価 |
| 同 tick の複数新着足・deferred 後の一括到着 | 過去足を検査し手前の接触も A。spread はその tick の値。最新も通常保護で評価不能なら本設計で検査し、引渡し時は未完了を保存 |
| long/short、SL/TP/両方/指値到達、open gap、非接触 | 真偽だけ評価。指値が未約定でも到達は A、仮約定・決済価格・損益変更なし |
| 全 exposure・terminal、生存途中の生成/決済、保護条件・時刻不明 | 生存重複を保守的に評価。実足の接触判定不明は A、取得不能とは区別 |
| update_fields 経由の direction/requested_price/SL/TP 更新 | 実行時ガードで拒否。transitions.transition の keyword 引数経由でも拒否 |
| flat の 1800 秒/設定 0、flat↔exposure、上位 hard 不調、soft 不調 | S は状態変化だけで延ばさない。上位 hard は G の現行規則、soft は昇格理由にしない |
| pending 後 flat、quantity=0、別 pair、未知 status、flat 2 tick 後 exposure 再発 | H は全 exposure ゼロから健全 3 回。同じ足/now は加算せず、不明・再発は解除しない |
| 接触時すでに flat/接触後 flat、auto=false、未完了 probe | 接触は I、flat でも ack 必須。auto=false は自動 ready だけ止め、背景検証は続ける。未完了は手動再開を妨げない |
| due 超過、応答後/commit 前 crash、再起動・保持期間外・端点なし | due・回数・S・接触待ちを復元、冪等再送可。端点なしは現水位から開始して記録。開場 observe commit まで起動 gate 閉 |
| 引渡し待ちと端点の保存後、通常保護前/評価途中/評価後の完了保存前に crash、再起動時に次足あり | 保存した足を後着検査へ回し、接触なら最初の開場 observe で A。回収結果保存まで起動 gate を閉じ、flat 解除不可。完了保存済み・内容不変なら再検査を省く |
| 金曜末尾足が日曜に到着する swing 保有、日曜足あり/なし、接触/非接触 | 日曜足ありの過去足も、なしの最新足も後着検査へ。接触なら A、非接触は停止理由にしない。通常保護の鮮度除外で検査を落とさない |
| 金曜末尾・日曜最初の穴、祝日・DST | 期待時刻列で休場を除く。週末後も due を作り直さない |
| 手動競合・ack 置換・閉場・新 epoch | J/K/L/M、一要求一終端結果。unverified を理由に拒否しない |
| 既存 stopped/待ち印・要求中に接触、新検出の再送 | 別 request_key で接触 stopped を 1 回。注文 ID・足時刻を activity/ctl で確認可。現在 facts を古い拒否より優先 |
| 保存・通知・任意注文の検査失敗 | 保存障害では解除せず、残る注文の通常保護・取消・決済は続く。通知失敗で安全状態を戻さない |

fixture は readonly SQL の 11 夜の時刻を使い、価格は合成する。fake bridge・仮時計・一時 SQLite・通知 double で背景検証と復帰が独立することを示す。本番 DB/cache の変更、bridge の停止、Discord の試験送信は行わない。

## 利用者に問う事項

追加の問いは 0 件。裁定に従い、後続到着で restricted を解き背景検証は待たず、検証失敗は再試行後の注記で終える。auto=false も検証は継続し、自動 ready のみ禁止する。指値への到達は接触として扱い、flat 自動解除より人の確認を優先する。

約定時刻・上位足出来高による不在の反証は採らない。これらは欠けた足の保護接触を証明せず、新しい停止条件を足すことになる。tick 数の強化は対象外。金曜末尾を含め、通常保護が評価できない後着足の接触検査は本設計に含める。

## 付録: 文書・前提とコードの食い違い

参照元とコードは今回は変更しない。実装時の同期に含める。

| # | 文書・前提 | 実体・本案での扱い |
|---|---|---|
| 1 | 15 epoch のうち 14 件を孤立 1 本と扱う | epoch と穴は別。SQL は 11 夜 35 区間。連続 4 本もあるが、新方針では数で停止しない |
| 2 | cache 不在で無 tick を証明 | rates を写すだけ (`mt5_bridge/mt5_client.py:675-705`)。取引なしは裁定に基づく運用認定 |
| 3 | 9/24 設計の backfilling・SL/TP replay が存在 | 現行の対象外 (`outage.py:3-9`)。`docs/superpowers/specs/2026-09-24-outage-stop-and-backfill-design.md:85-96` を実装済みとしない |
| 4 | 停止後は常に手動復帰 | 現行も flat 自動解除あり (`outage.py:409-418`)。通常 pending 後の flat へ拡張 |
| 5 | restricted は flat だけ | 現行は exposure ありで即 degraded (`outage.py:373-388`)。新方針は建玉ありの停滞にも 5 分猶予 |
| 6 | 2 分が停滞の猶予 | 2 分は穴の背景再取得の待ち。停滞は 300 秒 / flat 1800 秒 (`outage.py:370-384` を変更) |
| 7 | 健全は全足連続の証明 | 最新水位・取得結果のみ (`outage.py:302-329`)。受容穴の連続性は復帰条件にしない |
| 8 | 通常 ingest が古い穴を再取得 | watermark−1 本 (`ingest.py:211-225`)。保存候補の範囲を通常取得後に best effort で検証 |
| 9 | 未処理近似 0 なら exposure なし | `outage.py:699-716` は OPEN/PENDING_FILL だけ。全 `_EXPOSURE` ゼロで flat 判定 |
| 10 | pending 後の flat は自動解除 | main `outage.py:410` と通知 worktree `outage.py:548-559` は not pending 必須。通常 pending は除き、接触の確認待ちは残す |
| 11 | 通知設計の帯例外・flat 解除は未裁定 | `docs/superpowers/specs/2026-11-21-datafeed-outage-notice-design.md:241-242` より改定チケット裁定を優先 |
| 12 | 待ち印・拒否だけで次の手動操作が決まる | worktree `outage.py:48-81` に現在の接触・recovering の優先行と背景検証の注記を追加 |
| 13 | auto_resume_possible は pending で false | worktree `outage.py:321-335` を全 exposure と接触の確認待ちに揃える |
| 14 | entry_same_bar=True で TP も検査 | `paper_fills.py:52-57` は TP を抑止。接触は SL/TP の真偽を直接評価、仮約定不要 |
| 15 | SL/TP が変わるので毎 tick snapshot 必須 | C0 の誤り。insert 設定 (`src/agentic_fx/core/executor.py:679-687`) 後の変更経路はない。update_fields の実行時ガードで不変を固定し、生存期間は orders の時刻から保守的に取る |
| 16 | 通知と安全状態は同一 transaction | worktree `outage.py:879-888`, `:925-944` は後段の別保存。この分離を維持 |
| 17 | scheduler が provider 経由で 1m cache を温める | `src/agentic_fx/service.py:1602-1606` の注記と実体が不一致。本番は `:1829-1832`, `:1878` の DB 読取り、保存は `ingest.py:316-320` |
| 18 | example・docstring は flat の短停滞だけを説明 | `config/settings.yaml.example:63-65`, `outage.py:3-7` を停滞 5 分・穴受容・接触の人手優先へ同期。`config/settings.yaml.example:64` の健全 3 tick は degraded の flat 解除と auto=false の手動待ちに適用し、期限内 restricted の E は同 tick の健全で復帰することを明記 |
| 19 | 金曜最後の 1m 足も通常保護が評価する | 閉場中は取得しない（`ingest.py:159-162`）。最新のみ・鮮度 5 分の通常保護（`src/agentic_fx/service.py:1829-1832`, `scheduler.py:44`, `:945`）では日曜到着の金曜末尾足を見落とす。本設計の後着検査で、日曜足の有無に依らず対象にする |

## 変更履歴

| 日付 | 版 | 変更 | 理由 | commit |
|---|---|---|---|---|
| 2026-11-21 | C0 | 短い欠けと flat 自動解除を統合。期限、接触、全 exposure、通知・merge 順、受入境界を定義 | 2 チケットの承認済み方針を具体化し、実欠けの形と現行実装を区別する | 3eaa4936 |
| 2026-11-21 | C0.1 | 穴の無停止受容と 1 回の背景確認、停滞 5 分・flat 1800 秒、接触時の人手優先へ改訂。11 夜の SQL、永続化・状態表・通知・test・task を更新し snapshot 等を削減 | 設計レビュー 1 周目の指摘と利用者の方針の改めを反映 | 7248b3dc |
| 2026-11-21 | C0.2 | 停滞と背景検証を分離し、best effort・指値接触・保存責務・通知キー・有効化順を整理。11 夜の時系列と穴なし停滞を追記 | 設計レビュー 2 周目の指摘を反映 | 8349d2f0 |
| 2026-11-21 | C0.3 | 金曜末尾を含む後着検査、部分応答の接触優先、通常保護への引渡し未完了の永続化・回収、ready からの期限超過停止と受入を明記 | 設計レビュー 3 周目の指摘を反映 | |
