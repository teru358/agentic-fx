# 1 分足の穴の受容・停滞時の保護と flat 後の自動復帰 設計 C1 (2026-11-21 利用者承認)

後続の 1 分足が届いている穴は、時間帯・連続本数・発生回数に依らず受け入れ、合成しない。停滞中は新規リスクを止め、回復時は接触検査を先に済ませてから復帰を判定する。接触検査は約定・決済を書き換えない。

利用者が確認する資金保護の変化は次のとおり。

| 動作 | 現行からの変化・確認点 |
|---|---|
| 建玉ありの停滞・取得失敗・空 | 即 degraded から、連続 10 分以内は restricted に変わる。新規・指値約定・リスク増は止め、決済・取消・既存 SL/TP の保護は続ける |
| flat の停滞・取得失敗・空からの回復 | 期限は既定 1800 秒のまま。期限内 restricted は auto=true・同 tick 健全・接触なしなら E で復帰し、健全 3 tick を追加要求しない（現行 `outage.py:362-396`） |
| 上位 hard の不調 | 同 tick に 1m も失敗・空・停滞なら、上位 hard の失敗・空・停滞を 1m の猶予に合流。それ以外は現行規則 |
| 後続到着と取り直し | 通常取得を水位の 10 分前まで広げ、通常取得の機会ごとに同じ要求で読み直す。穴の検出・登録はせず、後着だけを判定する。後着検出は固定窓内の best effort。足の不在を無 tick の証明とはせず、穴は受容する |
| restricted の上限 | 開場時間で連続 30 分超なら、足が届いていても degraded・停止通知 |
| 自動解除前の検査 | restricted・degraded を通した non-ready epoch の間に届いた足のうち、当該注文について通常保護が評価しないものを検査。接触・判定不明なら flat でも人の確認と ack が必要 |
| 初回配備の既存 epoch 66 | observe 済み水位の保存が無いため、人の確認と ack で閉じる。以後の起動時回収は保存水位より後に限る |
| 通常停止後の flat | 全 exposure ゼロ・健全 3 tick・auto=true・必要な接触検査完了なら、通常 pending があっても自動解除。接触待ちは解除しない |
| auto_resume_when_flat の意味 | false は flat の H に加え、建玉あり restricted の E も止める。取得・検査は続け、健全 3 tick 後は degraded の手動待ち |
| 手動再開の健全 | 現行の定義は不変。全 hard key について停滞なし、直近結果が True と確認できることが必要。未取得の上位 hard を自動復帰の健全で代用しない（`outage.py:327-329`, `:609-613`） |
| 範囲外の既存の穴 | ready 中だけで生じる同 tick の古い足・鮮度切れ・金曜末尾足を通常保護が見ない問題は `docs/tickets/open/normal-protection-evaluates-latest-bar-only.md` に委ねる。通常保護の時刻順評価は追加しない（`src/agentic_fx/service.py:1829-1832`, `scheduler.py:942-949`） |

作成・改訂日: 2026-11-21 JST。方針は `docs/tickets/open/datafeed-rollover-band-expected-gap.md:26-27` と `docs/tickets/open/datafeed-pending-auto-release-when-flat.md:23-24` の「処置案・裁定」に従う。旧裁定の本数制限・即停止・停滞 5 分は置き換える。今回は本書のみ改訂し、実装・テスト実行・配備は行わない。

## 確認した実体

短縮名: `outage.py` は `src/agentic_fx/datafeed/outage.py`、`ingest.py` は `src/agentic_fx/datafeed/ingest.py`、`scheduler.py` は `src/agentic_fx/core/scheduler.py`、`paper_fills.py` は `src/agentic_fx/core/paper_fills.py`。通知 worktree は outnotice（commit `2e6e3b48`、未 merge）。以下の worktree 行番号はこの commit に固定する。`store/db.py` は `src/agentic_fx/store/db.py`。行番号は今回の実ファイルを照合した値で、main と worktree を混同しない。

| 事項 | 実体・根拠 |
|---|---|
| 停止と穴は別の数 | epoch 52–66 の入口は 15 件、うち 14 件が 22 時台 UTC。例は `logs/activity.log:12671`, `:13043`, `:14102`, `:15530`。下記 SQL の穴は全期間で 15 夜・54 区間・80 分。restricted 中の後続の停滞は streak をリセットするだけで epoch を増やさない (`outage.py:407-408`) |
| 現行の停止・復帰 | exposure があれば即 degraded (`outage.py:373-388`)。flat は 1800 秒の restricted (`:370-384`)。degraded の自動解除は `not pending` が必須 (`:409-418`) |
| 健全の定義 | 取得結果と最新 watermark の停滞を見る (`outage.py:302-329`, `:548-569`)。上位 hard 足は当 tick の failed/empty/stalled が無ければ前回結果を据え置き、未知を false としない (`:317-326`) |
| 通常取得 | `ingest.py:211-225` の下限は watermark の 1 本前。古い穴は自動では取り直さない。閉場中は取得しない (`:159-162`)、上位足は次の確定まで取得待ち (`:248-252`) |
| MT5 の足 | `mt5_bridge/mt5_client.py:675-705` は rates をそのまま変換し tick_volume を volume に写す。None は例外、空は空。無 tick 分の補間は無い。cache の不在だけで無 tick を証明はできない |
| 保護が見る足 | 本番の bars_fn は DB の最新 1m だけ (`src/agentic_fx/service.py:1829-1832`, `:1878`)。重複防止は `scheduler.py:942-949`、通常保護は OPEN 走査 (`:1786-1797`)。鮮度 5 分超は最新でも除外 (`scheduler.py:44`, `:945`)。同 tick に複数入る過去足を drain しない |
| SL/TP と価格 gap | `paper_fills.py:32-58` は SL/TP 判定。gap 滑りは open と保護価格の価格差であり、時系列の穴の replay ではない。shadow の連続性検査 (`:104-130`) も別用途 |
| 注文の保護条件 | direction・requested_price・SL/TP は発注 insert で設定 (`src/agentic_fx/core/executor.py:679-687`)。Action は OPEN/CLOSE/CANCEL/HOLD (`src/agentic_fx/core/contracts.py:12-16`)。`src/agentic_fx/store/orders.py:38-47` は汎用更新だが、src の更新呼出しに保護価格を書き換える経路はない。毎 tick の注文 snapshot は不要 |
| exposure と近似件数 | 全集合は `src/agentic_fx/core/executor.py:29-31`。既存の未処理近似は OPEN/PENDING_FILL と時刻だけ (`outage.py:669-718`) で、全 exposure ゼロや適用済みを証明しない |
| cache の書き手 | 本番 scheduler は DB を読むだけ。ingest が取得結果を保存 (`ingest.py:316-320`)。provider の保存は readonly で抑止 (`src/agentic_fx/datafeed/price_provider.py:220-221`, `:425-426`)。healthcheck も readonly (`src/agentic_fx/service.py:1599-1610`)。この本番経路で scheduler が cache を温めるという `service.py:1602-1606` の注記は実体と異なる |

## 全期間への当てはめ

本番 DB は readonly で照合した。対象 cache の保持範囲は 2026-10-21 13:26〜11-20 21:58 UTC。以下は日付制限なしの全期間 SQL であり、出力は時刻・件数のみ。600 分以上の差は休場をまたぐため除外する簡便集計で、実装では期待時刻列で休場を除く。現在残る穴の形であり、過去の到着時刻や接触の再現ではない。

```sh
sqlite3 -readonly data/agentic.db "WITH b AS (
 SELECT bar_time,julianday(bar_time) j,
 LAG(julianday(bar_time)) OVER (ORDER BY bar_time) pj,
 LAG(bar_time) OVER (ORDER BY bar_time) pt
 FROM ohlcv_cache WHERE symbol='USDJPY' AND interval='1m'
 AND source='mt5-live'), g AS (
 SELECT pt,bar_time,CAST(round((j-pj)*1440)-1 AS INT) n
 FROM b WHERE (j-pj)*1440>1.5 AND (j-pj)*1440<600)
 SELECT pt,bar_time,n FROM g ORDER BY bar_time;"

sqlite3 -readonly data/agentic.db "WITH b AS (
 SELECT bar_time,julianday(bar_time) j,
 LAG(julianday(bar_time)) OVER (ORDER BY bar_time) pj
 FROM ohlcv_cache WHERE symbol='USDJPY' AND interval='1m'
 AND source='mt5-live'), g AS (
 SELECT date(bar_time) night,CAST(round((j-pj)*1440)-1 AS INT) n
 FROM b WHERE (j-pj)*1440>1.5 AND (j-pj)*1440<600)
 SELECT night,count(*),sum(n),max(n),sum(n>10) FROM g GROUP BY night;
 SELECT min(bar_time),max(bar_time),count(*) FROM ohlcv_cache
 WHERE symbol='USDJPY' AND interval='1m' AND source='mt5-live';"
```

grace=30 秒、直前足 pt から S=pt+150秒、後続足から回復可能時刻 R=bar_time+90秒（`src/agentic_fx/core/market_hours.py:85-88`, `ingest.py:224-225`）。穴欄は UTC の時:分で両端を含む。通常取得が確定直後に追随し、auto=true・接触なし・他の hard 不調なし・observe が継続する仮定では S→R の長さは欠け本数と同じ分数となり、各区間 D→E、間は ready。約 60.3 秒周期の実 tick の restricted 長を実測した表ではない。

| 夜 (2026 年) | 穴の開始時刻・範囲 | 区間数 / 欠け分数 / 最大連続 | S→R の長さ (分) |
|---|---|---|---|
| 10-22 | 21:02 | 1 / 1 / 1 | 1 |
| 10-25 | 21:02、04、06、09、13–17、20–28、35、37、39–40、43、54 | 11 / 24 / 9 | 1/1/1/1/5/9/1/1/2/1/1 |
| 10-27 | 21:04–08、31、34–35、38、41–42 | 5 / 11 / 5 | 5/1/2/1/2 |
| 10-29 | 21:04、06 | 2 / 2 / 1 | 1/1 |
| 11-01 | 22:07、12、34、37–40、53、57–58 | 6 / 10 / 4 | 1/1/1/4/1/2 |
| 11-02 | 22:13 | 1 / 1 / 1 | 1 |
| 11-03 | 22:08、11、43 | 3 / 3 / 1 | 1/1/1 |
| 11-05 | 22:07、09、21、27、34 | 5 / 5 / 1 | 1/1/1/1/1 |
| 11-08 | 22:09、16、25、29、48–50 | 5 / 7 / 3 | 1/1/1/1/3 |
| 11-09 | 22:02–03 | 1 / 2 / 2 | 2 |
| 11-15 | 22:01 | 1 / 1 / 1 | 1 |
| 11-16 | 22:02、05、13、16、29 | 5 / 5 / 1 | 1/1/1/1/1 |
| 11-17 | 22:03、09、23、25 | 4 / 4 / 1 | 1/1/1/1 |
| 11-18 | 22:17 | 1 / 1 / 1 | 1 |
| 11-19 | 22:39、41、43 | 3 / 3 / 1 | 1/1/1 |
| 合計 | 15 夜 | 54 / 80 / 9 | 1 本44、2 本5、3 本1、4 本1、5 本2、9 本1 区間 |

10-25 の 5 本は S=21:14:30→R=21:19:30、9 本は 21:21:30→21:30:30。10-27 の 5 本は 21:05:30→21:10:30。いずれも 600 秒以内で、上記仮定で穴を理由とする F は 0。反復穴も後続到着ごとに停滞計時を切り、取り直しのために restricted を連結しない。

穴が無い epoch 57 も別に確認した。gap_start=11-13 12:31、S=12:33:30、実入口 12:33:53、回復は 12:40 台（約 7 分）。旧仕様の健全 3 tick 後の復帰は `logs/activity.log:14102-14103`、時刻規則未検証は `logs/agentic.log:65553-65557`。継続観測の下では新しい 10 分猶予内だが、当時の全 report を SQL だけでは再現できない。

```sh
sqlite3 -readonly data/agentic.db "SELECT g.epoch,g.gap_start,
 datetime(g.gap_start,'+1 minute') missing_at,count(b.bar_time) present
 FROM datafeed_outage_gap g LEFT JOIN ohlcv_cache b
 ON b.symbol=g.pair AND b.interval=g.interval AND b.source='mt5-live'
 AND datetime(b.bar_time)=datetime(g.gap_start,'+1 minute')
 WHERE g.interval='1m'
 AND (g.epoch BETWEEN 52 AND 56 OR g.epoch BETWEEN 58 AND 66)
 GROUP BY g.epoch,g.pair,g.interval,g.gap_start ORDER BY g.epoch;"
```

この突合は 14 件すべて present=0。現在の穴の残存だけを示し、全期間の後着ゼロや将来の無接触を証明しない。

## 穴の受容と停滞の検出・時刻

対象は registry の hard 1m key `(pair, interval, storage_source)`。soft 不調は昇格理由にしない。足時刻は UTC 開始時刻、区間は半開 `[start,end)`。期待時刻列は `src/agentic_fx/core/market_hours.py:77-88`、開閉場・祝日・DST は `:30-43` を使う。

- S は key ごとの連続未到着の起点。最後の watermark の次の期待足の確定予定時刻（grace 込み、`outage.py:557-569`）とし、水位なしの初回失敗だけ観測時刻を使う。失敗・空の繰返しでは延ばさない。
- 新しい確定足で旧計時を終える。回復足は旧水位の次の期待足以降で最初に存在する足。最新期待足が依然未到着なら最新水位から次の S を求める。後続ありの内部穴は停滞と混ぜない。
- 上限は S 以後の開場秒数で exposure あり 600 秒、flat は `flat_stall_max_sec`（既定1800秒、0は猶予なし）。flat↔exposure だけで S を動かさない。複数 key は各 S と期限を評価し、最も厳しい結果を採る。
- 継続観測中は観測で期限判定する。現在も停滞・失敗・空なら上限超で F、ちょうどは D。停滞が解けていれば旧 S の境界との足時刻比較を追加せず、健全なら E。境界後最初の tick で回復を観測すれば E、11 本の穴の途中で 600 秒超を観測すれば F。
- 再起動後および前回 observe の `updated_at` からの開場時間が規定 2 tick 分を超えた場合は継続回復と区別する。2 tick は120秒（`scheduler.py:47` の `TICK_INTERVAL_SEC=60`、発火は `src/agentic_fx/service.py:2941`）とする。commit 前水位・保存 S から旧停滞の期限超過を先に判定し、超過なら一括到着でも F、同 tick に E/H で解除しない。到着時刻を足の確定時刻から推測しない。ready の初観測でも同様とする。
- 同 tick の 1m 失敗・空・停滞には、上位 hard の失敗・空・停滞も合流させる。それ以外の上位不調は G。上位の内部欠け列挙は追加しない。
- restricted の連続開始は既存 `restricted_since` を保存し、S の更新で動かさない。連続開場 1800 秒超は Q。restricted を抜けたときだけこの計時を切る。

## 通常取得での取り直しと保存

hard 1m の通常取得下限を watermark−10 分とし、上限 now、同じ primary/source・要求・try・budget・保存単位で取り直す。水位なしは現行 planner の範囲。prepare の書込みなし契約（`ingest.py:138-142`）と正規化（`src/agentic_fx/datafeed/closed_bars.py:16-34`）を維持し、補間・fallback・tick 取得はしない。hard 優先・soft は残 budget、backoff・時刻規則未検証も現行の通常取得規則（`ingest.py:146-148`, `:182-193`, `:258-275`）。失敗は通常 report に反映する。

prepare は取得した有効足のうち「取得前水位以前で同 source の cache に無い時刻」を読取りだけで後着と判定し、取得前水位・新規時刻・後着足を report に載せる。穴の検出・登録はしない。通常取得の機会ごとに固定窓を読み直し、取得結果の後着だけを判定する。後着検出の範囲は固定 10 分の遡り内（通常約10分、未試行で機会は減る）。窓を外れた古い穴は追跡しない。non-ready の一括到着検査はこの窓に制限されない。

cache 保存（現行 `ingest.py:316-320`）は、内部 commit のある `src/agentic_fx/store/ohlcv.py:144-152` を呼ぶため、ingest 用に commit を呼出し側へ委ねる経路へ改める。その上で同じ transaction で「未検査の後着」を 1 表に保存する。主キーは key/source/足時刻、値は足の OHLCV・検出時刻。observe は表を読んで接触検査し、非接触なら行を削除、接触/不明なら確認待ちと参照へ移して行を削除する。削除と state 保存は同一 transaction。再取得や再起動で未検査を失わず、判定失敗は削除しない。金額は記録しない。

### 保存・復元の対応

| 保存先 | 内容・寿命 |
|---|---|
| 既存 `datafeed_outage_state` | state/epoch/pending/confirmed/streak・要求・通知印。`restricted_since` は連続 restricted の起点、`updated_at` は前回 observe。`restricted_deadline_at` は表示用で期限計算を二重化しない（`outage.py:435-441`, `:496-512`） |
| 既存 `datafeed_outage_gap.gap_start` | non-ready epoch の key ごとの固定起点。ready→non-ready で開き、restricted→degraded では開き直さず、ready への復帰で epoch 終了。上位のみの停止でも全 hard key を保存（`outage.py:514-536`） |
| state と同じ transaction の追加 facts | key/source ごとの S（単一最小値だけにはしない）、上位 hard の直近取得結果、観測した自動/手動の健全真偽・時刻、接触待ち・注文/足参照、初回配備の移行確認待ち（ack まで E/H を禁止）。key/source ごとの「observe が最後に見た水位」も 1 値保存する。ready 中も毎回、必要な検査の消費と state 保存と同じ transaction で更新し、cache 保存だけでは進めない。上位結果は `_last_attempt_ok` の process 内限定（`outage.py:134`）を改め、epoch 開始のクリア（`:310-316`）と復元を保存へ揃える |
| 新しい未検査の後着表 | cache と原子的に保存、observe の判定後に消費。epoch 外の ready 中の後着も対象 |

`gap_start` は epoch の固定起点として既存列を使い、起動時回収の下限にはしない。水位なしを now に置換する現行 `outage.py:531` は改め、NULL を明示できる migration にする（現行 `src/agentic_fx/store/db.py:829-836`）。別に保存する observe 済み水位は、現行の cache 水位読取り（`outage.py:302`, `:548-555`）と state 保存（`:435-441`）を一つの observe transaction（`:272-285`）に揃えて記録する設計とする。これは通常保護の完了時刻ではない。明示 NULL は「まだ足を見ていない」であり、その key の初回からを対象にする。値の読取り障害は B、保存を開始する前の既存 epoch は初回配備の手動確認として区別し、ack まで自動解除しない確認待ちを保存する。現在の cache 水位で検査済みを捏造しない。source は gap 行と observe 済み水位にも保存し、設定変更時に混同しない。

### 起動・停止中の区間

ingest/outage が両方ある構成だけ、最初の開場 observe commit まで新規発注・指値約定・自動 ready・手動要求消費を待たせる。どちらかが無い構成には起動待ちを掛けない（`src/agentic_fx/service.py:2483-2485`, `:2507`, `:2526-2527`）。取消・決済・既存保護・close retry は続ける。閉場 observe では待ちを解かない（`outage.py:280-284`）。

再起動は既存 gap_start・restricted_since・S・updated_at・上位結果・接触待ちと、key/source ごとの observe 済み水位を復元し、streak は0にする。未検査の後着を独立に回収し、保存済み non-ready epoch では「保存した observe 済み水位より後、今回読む cache 水位以下」の新規足だけを N の対象として検査してから初回待ちを解く。cache 保存と observe は別 transaction（`ingest.py:316-320`, `src/agentic_fx/store/ohlcv.py:144-152`, `outage.py:272-285`）なので、この差分で cache commit 後・observe 前に落ちた tick の足を回収する。保存水位以下の通常決済済みの足を gap_start から読み直さず、進んだ cache 水位で下限を置換もしない。episode なしの健全な ready 起動は N を適用しない。必須状態が読めなければ B、保護条件不明の実足なら A。通常保護の完了報告・時刻順評価は追加しない。

## 後着足の接触検査

純関数 `protection_contact(orders, bars, spread)` で評価し、注文・約定・損益を書き換えない。対象は、穴への後着足と、non-ready epoch 内に新規保存された足のうち当該注文について通常保護が評価しない足の 2 種類であり、生存期間が重なる exposure 全体との組を評価する。現在 closed/cancelled でも、その区間に生存した可能性があれば除外しない。

毎 tick の履歴 snapshot は保存しない。direction / requested_price / stop_loss / take_profit は insert 後不変とする。`src/agentic_fx/store/orders.py:7-14`, `:20`, `:40-42` の insert と共用の集合から update 用の許可集合を分け、requested_price / stop_loss / take_profit を update 用から除く。direction は既に拒否される。`src/agentic_fx/core/transitions.py:132` の keyword 引数経由も同じガードを通す。

生存期間は新しい列を足さず、開始を `created_at`、終了を `closed_at` とする。足の `[ts,ts+1分)` とこの窓が重なる注文を対象とし、非終端は上限なし、closed_at のない他の終端は `updated_at` を保守的に使う。作成時刻の保存は `src/agentic_fx/store/orders.py:23-26`、決済時刻は tick の now（`src/agentic_fx/core/executor.py:959-961`）。`filled_at` で開始を狭めず、指値時代を含めて広く取る。時刻の欠損・不整合や broker 結果不明で安全とは断定しない。

この広い窓には直前に通常保護で決済した注文も入り、実際には処置済みでも接触と判定し得る。偽の接触は人の再開待ちに倒して許容する。穴への後着では、穴が後から埋まることと直前の決済が重なった場合に起きる。readonly で `datafeed_outage_gap` の epoch 52–56・58–66 の 14 件と同 source の cache を突合したところ、全件で `gap_start` の次の分の穴が現在も残っていた。停止記録の水位保存は `outage.py:515-536`、cache の時刻・source は `src/agentic_fx/store/ohlcv.py:144-152` に対応する。これは後着が少ないことの傍証に限り、54 区間全体の過去の埋まりや将来の非発生を証明しない。

spread は評価時の `_spread_for`（`scheduler.py:1837-1842`）を使い、足には固定保存しない。pair 規則が無い場合は通常保護と同じ 0.0、読取り失敗・非有限値は接触不明とする。対象足の指値到達も接触として扱う。

| 区間と重なる状態・証拠 | 判定 |
|---|---|
| OPEN、または確実に保有した履歴、方向・有限 SL・任意の有限 TP が有効 | `paper_fills.py:34-43` と同じ half spread で SL/TP の接触真偽を評価。long は low/open−half ≤ SL、high−half ≥ TP、short は high/open＋half ≥ SL、low＋half ≤ TP。両方も記録。決済価格の計算は不要 |
| 未約定が確実な PENDING_FILL、または約定せず終端 | 指値到達（long: open/low＋half ≤ 指値、short: open/high−half ≥ 指値、`paper_fills.py:14-29`）または SL/TP 到達なら接触。到達の真偽だけを評価し、仮約定・同足 replay はしない |
| protection_pending / submitting / submitted / closing / cancelling / submit_unknown / cancel_unknown / close_unknown | 保有の有無・保護を確定できなければ `late_bar_contact_unknown`。単に OPEN でない理由で除外しない |
| 方向・SL・spread・生存時刻の欠損や非有限値、状態不明 | `late_bar_contact_unknown` |
| 正常な再取得で実足が無い | 実足の接触検査はなし。不在は受容するが、無 tick・取引なし・非接触の証明にはしない |

接触は `late_bar_protection_contact`、不明は `late_bar_contact_unknown` として degraded・pending・接触の確認待ちを永続化する。不明も自動解除しない。SL/TP/指値/両方/不明、足時刻、注文参照を内部記録に残す。activity と ctl status に注文 ID・足時刻・接触種別を出す。Discord は固定本文とし、価格・金額・口座情報を出さない。後から非接触の足を見てもこの確認待ちを消さない。

## 復帰と状態機械

flat は全 pair を通じ `orders.list_by_status(conn,*_EXPOSURE)` が0行。集合は open / pending_fill / protection_pending / submitting / submitted / closing / cancelling / submit_unknown / cancel_unknown / close_unknown（`src/agentic_fx/core/executor.py:29-31`）。quantity=0 でも非終端なら flat ではない。未知 status・読取り失敗は不明。approval_pending は含めず、承認後 submit に既存 gate を掛ける。

自動復帰の健全は現行 `outage.py:317-326` を基礎に、1m hard 全 key の当 tick 非空成功・現在非停滞と、上位 hard の直近結果が False でないこと。上位結果は保存して再起動で失敗の記憶を失わない。受容穴の連続性は条件にしない。手動再開 J/K の健全は別で、現行 `outage.py:327-329`, `:609` のまま全 hard の `attempt_ok is not True` を未確認として拒否する。上位 hard 未取得は自動健全になり得ても手動健全にはならない。

degraded の flat 自動解除は、flat 成立後に水位が進む健全3 tick。同じ now・同じ足は加算せず、失敗・空・未試行・停滞・exposure 再発・閉場・再起動で0。決済は observe の後（`src/agentic_fx/service.py:2502-2506`, `:2527-2536`）なので次の observe から数える。通常 pending は履歴の印で H の gate にしない。接触待ちは flat でも自動解除を禁止する。auto=false は E/H を止めるが取得・検査は続け、restricted の健全3 tick 後は degraded の手動待ちにする。

N の単位は「足と注文の組」。通常 tick では、その commit で新たに入った足だけを対象にする。まず最新・鮮度内の足を除いた組で N を検査し、最新以外・鮮度切れの新規足の接触/不明は A とする。その結果と他の facts から遷移先を仮計算し、ready（E/H/R）になるなら最新・鮮度内の足は通常保護に任せる。non-ready のままなら、その最新足と PENDING_FILL の組を追加検査し、接触/不明なら A とする。OPEN の最新・鮮度内で通常保護の対象になる足は除外する。仮計算では保存・解除せず、必要な検査完了後にだけ最終結果を保存する。これにより回復 tick の指値到達を偽の A にしない。observe は scheduler より先（`src/agentic_fx/service.py:2527-2536`）で、ready なら同 tick の指値約定が実行され、non-ready でも既存 OPEN の保護は続く（`scheduler.py:342-348`）。最新足の鮮度判定は `scheduler.py:942-949`。前 tick に最新として通常保護が SL 決済した足を、次 tick に古くなっただけで再検査しない（OPEN は `scheduler.py:1786-1797`、処理済み時刻は `:1827-1835`）。未検査の後着は ready を含め独立に先に検査し、最新・鮮度内を除く扱いでは省略しない。

non-ready epoch は ready→restricted/degraded で開始、ready に戻るまで継続する。restricted→degraded・flat 化・到着ごとには起点を動かさない。N は degraded の回復一括到着にも掛け、flat の H より先に完了させる。起動時は保存した observe 済み水位と cache 水位の差分だけを回収し、通常 tick も過去全区間へ広げない。ready 中だけで生じる複数到着・鮮度切れ等は範囲外のまま。

**`outage_transition(row, facts) -> (row_next, effects)`** 一つで接触・停滞・復帰・手動要求を計算し、observe と要求消費は結果だけ保存する。facts は前回 updated_at・key ごとの S・既存 gap_start・observe 済み水位・cache 水位・通常 report（取得前水位と今回新規足）・未検査の後着・注文履歴・spread・自動/手動の健全・時刻・設定・要求。A/B と N/O の最新・鮮度内を除く検査を先行し、Q、独立した G、D〜I（異常なしは R）で遷移先を仮計算する。non-ready 維持なら PENDING_FILL の最新足を追加検査して A を優先し、最終状態を決めてから J/K を適用する。L/M は共通規則。行 ID を実装時の test に対応させる。

| ID | 入力・現状態 | 1m 未試行の扱い | 次状態・効果 |
|---|---|---|---|
| A | 対象組に接触/不明、任意 state・flat | 検査可能な保存足にも適用 | degraded、pending=1、接触待ち・参照、streak=0。ready なら epoch を開き、接触停止通知 |
| B | 保存障害・必須状態の読取り不能 | 同じ | gate を閉じ解除しない。保存可能なら degraded、pending=建玉あり or 既存 pending、streak=0。ready なら epoch を開く。通常取得失敗は D/F |
| C | 後着なしの通常取り直し | 未試行なら T | 取り直しだけでは state/epoch を変えず、他の facts で状態判定。穴の検出・登録はしない |
| D | 1m 停滞・失敗・空、猶予内、ready/restricted | 停滞なら D、非停滞なら T | restricted。ready からは epoch を開き全 hard の gap 行を取得前水位で保存、attempt_ok をクリアして当 tick 結果だけ反映。S を保存し、継続中は gap_start/restricted_since を動かさない |
| E | restricted、継続観測で回復、当 tick の自動健全、auto=true | E 不可 | N/O 完了・接触なしなら ready、pending=0。追加3 tick や取り直し完了を待たない |
| F | 停滞の開場秒数が期限超、または再起動/観測中断で旧期限超 | 停滞継続なら F | degraded、streak=0。ready なら停止 epoch を開く。建玉ありなら pending=1、flat は設定期限。回復一括到着でもこの tick は解除しない |
| G | 1m と同時でない上位 hard の停滞・失敗・空 | 上位 facts で判定 | 現行規則：exposure ありは即 degraded、flat の失敗・空も即 degraded、flat の停滞は設定期限の restricted。停滞 key の S の最小値で判定（`outage.py:367-387`） |
| H | degraded、接触待ちなし、flat、auto=true、自動健全 | 加算せず streak=0 | epoch 内の必要な N/O 完了後、健全1〜2 tick は維持、3で ready と pending=0 を一括保存 |
| I | degraded、接触待ちあり / exposure あり / auto=false | 健全確認に加算しない | 自動解除なし。健全なら人の待ち。auto=false の restricted も健全3 tick 後この待ちへ |
| J | 手動要求、手動健全でない | 直近結果が未確認なら拒否 | rejected_watermark。取り直しの成否を独立の拒否理由にしない |
| K | 手動要求、手動健全 | 全 hard の直近 True・停滞なしなら判定可 | 未処理近似と接触/不明の注文参照の和集合を ack 対象とする。未 ack は rejected_unprocessed。flat でも接触 ack 必須。受理で接触待ち解除 |
| L | 自動判定と手動要求の競合 | 同じ | 観測→自動判定→要求消費。先に ready なら already_ready。要求 key 比較更新で置換済み要求を消さない |
| M | 閉場 | 取得・要求消費を保留 | streak=0、S/gap_start/restricted_since を保持。壁時計で期限超過にしない |
| N | non-ready epoch 内の新規足、または未検査の後着 | 保存足は検査する | 最新・鮮度内を除く組を先に検査して遷移先を仮計算。ready なら最新足は通常保護へ、non-ready 維持なら PENDING_FILL の最新足も検査。後着は独立に検査。degraded・flat も省略せず、接触/不明は A |
| O | 最初の開場 observe | 必要な検査は省略しない | 未検査表を回収し、保存済み epoch は observe 済み水位より後〜cache 水位以下を N/A の対象とする。episode なしは N を適用しない。必須状態の読取り障害は B。検査・state・observe 済み水位を同時保存して起動待ち解除 |
| P | degraded、停滞・失敗・空・期限超過 | 維持、streak=0 | degraded と既存 pending・接触待ちを維持 |
| Q | restricted の連続開場時間1800秒超 | 同じ | 足到着に依らず degraded、既存 pending を保持し建玉ありなら1、streak=0、confirmed=1・停止通知。E より優先 |
| R | ready、当 tick 自動健全、異常なし | T | ready 維持 |
| T | 他のどの行にも当たらない | 非停滞の未試行を含む | 現 state を維持し、新 epoch/gap を作らない。attempt_ok は据置き（失敗/空なら False、成功なら True）、健全 streak は加算せず0 |

不変条件は、(1) non-ready でも決済・取消・既存保護を続ける、(2) 後続ありの穴は数・時刻で止めず補間しない、(3) 接触検査は穴への後着と non-ready epoch の通常保護対象外に限り約定・決済を書き換えない、(4) 10 分は連続未到着ごとで、再起動・未試行・ack で延ばさない、(5) H は全 exposure ゼロと必要検査の完了を要求し接触待ちは解除しない、(6) 他の kill switch・リスク・承認 gate は解除しない、である。

## 通知と status

通知の基盤は outnotice（commit `2e6e3b48`）を先に merge する。以下の行番号はこの commit に固定する。`resume_guidance` は `outage.py:48-81`、要求比較更新は `:856-869`、通知の一意キーは `(epoch,notice_state,request_key)`（`:934-944`）。

| 事実 | 通知・記録 |
|---|---|
| 取り直し・後着検査の非接触・不在、期限内 restricted と E | Discord 無通知。内部 activity のみ。穴の検出・登録・穴単位の activity は追加せず、穴だけで epoch/problem_streak を進めない |
| 接触/不明 | confirmed=1、同 epoch の固定 request_key `contact` で stopped を1回。通常停止の空キーと分け、既存停止/待ち印・要求中・flat でも初回接触を抑止しない。追加接触は activity/status の参照追加だけ |
| 通常 degraded | 連続問題2 tick / 開場10分の既存停止条件。restricted 中も problem_streak は進め、通知は degraded で判定 |
| restricted 連続30分超 | confirmed=1、通常停止を同 epoch 1回 |
| 回復後の人の待ち | awaiting_resume を同 epoch 1回。初回接触 stopped を先に保存 |
| H | 停止/待ち記録のある epoch だけ resumed を1回。自動確認中に新たな awaiting_resume を出さない |
| 手動要求 | accepted/rejected/already_ready を必ず終端記録 |

配送 retry は基盤を継承する。安全状態 commit 後に別 transaction で通知を保存する（固定 commit の `outage.py:881-888`, `:934-944`）。接触待ちから未記録の contact 通知を起動時に再照合し、再送でも同キーで重複させない。通知失敗で安全状態を戻さず、保護 tick を止めない。Discord は固定本文とし、価格・金額・口座情報を含めない。

status は保存した自動/手動健全・時刻・streak・state・接触待ち/参照から一つの resume_guidance で決め、API で健全を再計算しない。unknown→要求あり→ready を最優先とし、以後は以下を古い拒否・待ち印より先に判定する。

| 条件 | phase | next_action |
|---|---|---|
| restricted | restricted | wait_tick |
| degraded、接触待ちあり、手動健全 | awaiting_resume | resume_with_ack |
| degraded、接触待ちなし・flat・auto=true、自動健全確認中 | recovering | wait_tick |
| degraded、データ不健全 | stopped | restore_source |
| degraded、手動待ち、手動健全 | awaiting_resume | ack 対象により resume_with_ack / resume |
| 必須 facts の読取り不能 | unknown | check_status |

ack 件数には接触/不明参照も重複除去して含め、近似0でも resume に戻さない。上位未確認で自動健全だけ成立する場合は自動確認と手動不可を区別する。古い拒否は別欄。auto_resume_possible は全 exposure・接触待ち・健全条件に揃える。内部状態4項目の公開は追加せず、phase・既存案内・接触参照を使う。guidance_text.py・ctl・web を同期し、recovering は「健全な足の確認を続けています。条件が揃えば自動解除します」。旧クライアントの未知 phase は unknown。

## 実装と適用順

main への merge が本番ペーパー運用への配備になる。outnotice 後の schema_version に migration を追加する。接触純関数と価格更新ガードを先に適用できるが、停滞猶予・通常取り直し・接触停止・flat 解除・表示は一つの branch、1回の merge で同時有効化する。初回配備の既存 epoch 66 は observe 済み水位の保存が無いため、人の確認と ack で閉じる。以後の epoch は保存水位から差分回収し、通常決済足の再検査を避ける。今回は実装しない。

- 接触判定と価格更新ガード：生存窓・SL/TP・指値到達を純関数化し、insert と update の価格許可集合を分ける。
- 保存と取得：通常取得の固定窓・読取りだけの後着判別・report を追加。未検査の後着と cache を同じ transaction、判定消費・state・key/source ごとの observe 済み水位を同じ transaction にする。既存列/表の再利用・S/上位結果・健全/接触 facts・NULL 起点の移行と冪等性を確認する。
- 状態：outage_transition に10分・上位不調の合流・30分上限・注文ごとの対象選択・N/O・起動待ち・未試行の据置きを集める。
- 復帰と表示：手動健全を保ち、H の検査優先と ack、epoch ごとの接触通知を接続。ctl/web・afx-ops・設定 example・docstring・通知設計・運用手順を同期する。

## test と受入

以下は実装時の検証であり、今回は test を回さない。状態表全行を入口×state で確認し、observe と手動要求は同じ facts から計算する。

| 境界・入力 | 期待 |
|---|---|
| 全期間54区間・任意時刻・複数 pair・反復穴 | 穴の検出・登録はせず、後着なしの通常取り直しは C。表の仮定では D→E を区間ごとに切り、穴だけで F にしない |
| 5/6/9/10/11 本、建玉あり/なし | 継続観測では10分ちょうどまで D、回復なら E。建玉あり11本は停滞中の600秒超観測で F。flat は設定期限。初観測が回復なら観測規則、観測中断なら F の別条件を確認 |
| 取得失敗/空、600秒直前/ちょうど/超過、水位不変成功 | 同じ S で D/F、成功でも停滞なら E 不可。回復足で旧計時を切り、新たな停滞は次の S |
| epoch 57 の穴なし約7分停滞、境界 tick 未試行 | 継続観測なら10分猶予。未試行は E 不可、停滞期限超なら F |
| ready/restricted × 1m未試行・非停滞（backoff/時刻規則未検証/取得時刻前） | T で現 state、epoch/gap/attempt_ok 据置き、streak=0 |
| 1m と上位 hard が同 tick に失敗/空/停滞、上位単独不調、soft 不調 | 同時は D/F、単独は G、soft は昇格理由にしない |
| 上位 hard の失敗を保存→再起動→1m回復・上位未取得 | 記憶を失わず E/H 不可。上位未知でも J/K は手動健全で拒否し、未取得を True にしない |
| 通常取得の各機会、固定窓内/外の後着 | 穴の発見を待たず固定窓を同じ通常要求で再取得。窓内の新規時刻を後着表へ保存し検査、窓外を無期限追跡しない |
| 通常要求の空/例外/時刻規則未検証、budget なし | 通常 report から D/F/T。新しい後着なしを非接触・無 tick の証明にしない |
| 部分応答に有効な後着足、他の足は不在 | 有効足を即検査、接触は A。不在部分だけで gate を足さず、非接触で既存接触待ちを消さない |
| restricted 維持、鮮度内の最新足で PENDING_FILL の指値到達 | 最新以外の N と状態の仮計算後も non-ready なら、最新足を追加検査して A。仮約定しない |
| 回復 tick の鮮度内の最新足で PENDING_FILL の指値到達、他の接触・異常なし | E/H/R で ready になるなら A にせず、同 tick の通常指値約定へ。最新以外や後着に接触があれば A を優先 |
| tick t の最新足 X で通常保護が SL 決済、t+1 で X が古い足になる | 新規 commit 足でない X を再検査せず、通常の複数 tick で偽の A を作らない |
| 建玉あり40分不通→F→40本一括到着、途中足で接触→flat | degraded も同じ epoch。N→A、H を止め、人の ack を要求。非接触なら通常の H 条件 |
| non-ready で鮮度切れ最新・週末をまたぐ一括到着 | 通常保護対象外の新規組は N、休場は計時除外。ready 中だけの金曜末尾足は範囲外 |
| restricted 中に停止→再起動の一括到着、ready 初観測で旧期限超 | updated_at と旧 S で中断判定し F。同 tick の回復を E/H にしない。N/O の接触は A 優先 |
| 未検査後着の cache commit 後・observe 前 crash | 表から必ず再検査。検査と state 保存前 crash は未消費、保存後 crash は重複停止通知なし |
| non-ready の一括到着を cache 保存後・observe 前 crash | 保存した observe 済み水位より後〜cache 水位以下だけを回収。進んだ cache 水位で下限を置換せず、接触を見ずに H に進まない |
| degraded 中に observe 済み水位保存→同 tick の通常 SL/TP 決済→再起動 | observe→通常決済の順（`src/agentic_fx/service.py:2527-2536`）、closed_at は決済 tick の now（`src/agentic_fx/core/executor.py:959-961`）。保存水位以下の決済足を再検査せず、偽の A を作らない。新規差分と未検査後着は検査する |
| 検査・state・observe 済み水位の保存前/後 crash | 保存前は差分を再検査、保存後は同じ足を回収しない。ready 中も水位を保存する |
| 初回配備の既存 epoch 66 | observe 済み水位を現在 cache から検査済み扱いで補わず、移行確認待ちで E/H を止め、人の確認と ack で閉じる。再起動でも ack 前の待ちを保持 |
| episode なしの健全 ready 起動、observe 済み水位の明示 NULL、必須状態読取り不能 | 前者は N なし。明示 NULL は初回足から差分を検査、読取り障害は B。ingest/outage なし構成は起動待ちなし |
| long/short、SL/TP/両方/指値、open gap、保護条件不明 | 接触真偽だけ評価、不明も A。通常保護の約定/決済/損益を変更しない |
| 全 exposure・terminal、生存途中の生成/決済、作成前/終了後の足 | 作成時刻側〜closed_at の広い窓。後着と直前決済が重なる偽の接触は人の確認待ちとして許容 |
| direction/requested_price/SL/TP の update、transition の keyword 経由 | 実行時ガードで拒否 |
| flat の1800秒/設定0、flat↔exposure、restricted 30分ちょうど/超過 | 状態変化で S を延ばさず、Q は足到着に依らず超過時停止 |
| pending 後flat、quantity=0、別pair、未知status、flat2 tick 後exposure再発 | H は全 exposure ゼロから健全3回。同足/nowは非加算、再発・不明では解除しない |
| 接触時すでにflat、接触後flat、auto=false | 接触は常に ack 必須。auto=false は建玉あり E と H を止め、取得/検査は続ける |
| degraded・1m回復・上位 hard未取得で手動要求 | 現行の手動健全で J。自動健全を J/K に流用しない |
| 手動競合・ack置換・閉場・新epoch | J/K/L/M、一要求一終端結果。検査未完了は解除を先にしない |
| 既存停止/待ち印・要求中で初回接触、追加接触・再送・通知保存前crash | contact キーで同 epoch 1回、追加は activity/status のみ、起動時再照合。現在 facts を古い拒否より優先 |
| 状態保存・通知・任意注文検査の失敗 | 保存障害では解除せず、他の注文の通常保護・取消・決済を続ける。通知失敗で安全状態を戻さない |

fixture は全期間 SQL の時刻と合成価格を使う。fake bridge・仮時計・一時 SQLite・通知 double で確認し、本番 DB/cache の変更、bridge 停止、Discord 試験送信は行わない。

## 利用者の確認項目

冒頭の資金保護の変化の表を確認項目とする。追加の問いはない。約定時刻・上位足出来高による不在の反証や tick 数の強化は加えない。

## 付録: 文書・前提とコードの食い違い

参照元とコードは今回は変更せず、実装時の同期に含める。

| 文書・前提 | 実体・本案での扱い |
|---|---|
| epoch 数と穴数を同一視、11-01がcacheの始まり | 全期間は10-21から、54区間。epoch は別の数 |
| cache 不在で無 tick を証明 | rates を写すだけ（`mt5_bridge/mt5_client.py:675-705`）。不在は穴として受容し、後着は固定窓で検出 |
| backfill/replay が既にある | 現行対象外（`outage.py:3-9`）。通常保護の時刻順評価を追加しない |
| restricted はflatだけ、建玉あり即停止 | 現行 `outage.py:373-388` を建玉あり停滞10分へ変更 |
| 停止後は常に手動、pending があればflatでも解除不可 | 現行 `outage.py:409-418` の not pending を通常停止について外し、N完了・接触待ちなしで H |
| 自動健全と手動健全は同じ | 自動は `outage.py:317-326`、手動は `:327-329`, `:609`。手動は緩めない |
| 通常取得で古い穴も検証済み | 現行 `ingest.py:211-225` は水位−1本。固定10分へ広げ、窓外を保証しない |
| 近似0はflat、TPもentry_same_barで確認 | 近似は OPEN/PENDING_FILL 限定（`outage.py:699-716`）、flat は全 exposure。`paper_fills.py:52-57` は TP 抑止。接触は真偽を直接評価 |
| 保護価格は変わる、毎tick snapshotが必要 | insert 設定（`src/agentic_fx/core/executor.py:679-687`）。update 用許可集合を分け不変にする |
| 通知と安全状態は同じtransaction | 通知基盤の固定 commit `outage.py:881-888` は別保存。後着/cache と判定/state の原子性とは区別 |
| scheduler がcacheを温める | `src/agentic_fx/service.py:1602-1606` の注記は実体と不一致。本番は `:1829-1832`, `:1878` のDB読取り、保存は `ingest.py:319` |
| example/docstring はflatの短停滞だけ | `config/settings.yaml.example:63-65`, `outage.py:3-7` を10分・穴受容・検査優先へ同期。健全3 tick は H と auto=false の手動待ち、Eは同tick。autoは建玉ありEも制御 |
| 長時間停止も回復足の確定時刻で期限内復帰できる | 現行 `outage.py:370-373` の壁時計判定を開場秒数・exposure別期限へ変更し、updated_at の観測中断を区別 |
| 回復 tick の N は `IngestTickReport.new_times` から数える | `new_times` は載せない。N は保存した observe 済み水位から数える |

## 変更履歴

| 日付 | 版 | 変更 | 理由 | commit |
|---|---|---|---|---|
| 2026-11-21 | C0 | 短い欠けと flat 自動解除を統合。期限、接触、全 exposure、通知・merge 順、受入境界を定義 | 2 チケットの承認済み方針を具体化し、実欠けの形と現行実装を区別する | 3eaa4936 |
| 2026-11-21 | C0.1 | 穴の無停止受容と 1 回の背景確認、停滞 5 分・flat 1800 秒、接触時の人手優先へ改訂。11 夜の SQL、永続化・状態表・通知・test・task を更新し snapshot 等を削減 | 設計レビュー 1 周目の指摘と利用者の方針の改めを反映 | 7248b3dc |
| 2026-11-21 | C0.2 | 停滞と背景検証を分離し、best effort・指値接触・保存責務・通知キー・有効化順を整理。11 夜の時系列と穴なし停滞を追記 | 設計レビュー 2 周目の指摘を反映 | 8349d2f0 |
| 2026-11-21 | C0.3 | 金曜末尾を含む後着検査、部分応答の接触優先、通常保護への引渡し未完了の永続化・回収、ready からの期限超過停止と受入を明記 | 設計レビュー 3 周目の指摘を反映 | 311d0605 |
| 2026-11-21 | C0.4 | 通常保護の時刻順評価と評価済み時刻への集約、完了・決済足の定義、probe の分離、停滞・起動・通知・適用順と受入を改訂 | 設計レビュー 4 周目の指摘を反映 | 44bbea64 |
| 2026-11-21 | C0.5 | 接触検査を穴への後着と停滞後の一括到着に限定し、生存窓・状態表・適用範囲・受入を整理。通常保護の時刻順評価・引渡しの永続化・境界足の指紋比較・金曜末尾足の特別扱いを削除 | 設計レビュー 5 周目の指摘を受け、範囲を承認済みの方針に削る | 99100c2f |
| 2026-11-21 | C0.6 | restricted 中の鮮度切れの最新足を接触検査へ含め、episode 起点の水位を保存・復元。資金保護の変化・偽の接触の根拠・通知の参照・履歴・受入を修正 | 設計レビュー 6 周目の指摘を反映 | 6f002a52 |
| 2026-11-21 | C0.7 | 起動時に起点の復元を求めるのは保存済みの episode があるときだけに限り、episode なしの再起動の受入を足す | 設計レビュー 7 周目の指摘を反映 | fb6606e9 |
| 2026-11-21 | C0.8 | 停滞 600 秒、epoch 全体の接触検査、通常取得による取り直しと保存・状態・通知・受入を整理 | 独立チェックの指摘と利用者裁定 (停滞 10 分・自動解除前の検査) を反映し、取り直しを通常取得に寄せて単純化 | 952b3f31 |
| 2026-11-21 | C0.9 | 回復 tick の接触検査を遷移先に応じて分け、observe 済み水位による起動時差分回収と既存 epoch の手動確認を定義。穴の発見を前提とした記述と受入を整理 | 独立チェックの閉鎖確認の指摘を反映 | bac23aac |
| 2026-11-21 | C1 | 利用者が承認 | 利用者承認 | |
| 2026-11-28 | C1.1 | 付録に `IngestTickReport.new_times` を載せない旨を追記 | 実装 (gapfix) の数え方に文書を合わせる | |
