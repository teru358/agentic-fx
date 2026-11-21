# 1 分足の穴の受容・停滞時の保護と flat 後の自動復帰 設計 C0.1

後続の 1 分足が届いている穴は、時間帯・連続本数・同じ夜の発生回数に依らず受け入れ、取引判断を止めない。足を合成せず、出来高・指標・backtest の系列を MT5 履歴と揃える。約 2 分後に穴の区間を同じ価格源から 1 回取り直す。届かなければ「取引なし」で確定し、後着足が建玉の SL/TP に触れていなければ何もしない。接触を検出したら、すでに全決済されていても degraded・人の再開待ちとし、停止通知を出す。過去の約定・決済を replay で変更しない。

新しい足が来ない停滞は、建玉があっても restricted とする。新規・指値の新規約定・リスク増を止め、決済・取消・既存 SL/TP の保護は続ける。5 分以内の回復と穴の確認が揃えば自動で ready に戻す。建玉ありの 5 分超は degraded・人の再開待ち。flat は既存の `flat_stall_max_sec=1800` 秒を維持する。通常の degraded は全 exposure がなくなり健全 3 tick が揃えば自動解除するが、接触を検出した停止は flat でも自動解除しない。

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
| 保護が見る足 | 本番の bars_fn は DB の最新 1m だけ (`src/agentic_fx/service.py:1829-1832`, `:1878`)。重複防止は `scheduler.py:942-949`、通常保護は OPEN 走査 (`:1786-1797`)。同 tick に複数入る過去足を drain しない |
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

以下の時刻はすべて UTC、穴の分の開始時刻。範囲は両端を含む。全区間で直後の実足が現存し、最大連続欠けは 4 分、5 分超の区間は 0 件だった。

| 夜 (2026 年) | 穴の時刻 (すべて 22 時台) | 区間数 / 欠け分数 / 最大連続 | 新方針の適用 |
|---|---|---|---|
| 11-01 | 07、12、34、37–40、53、57–58 | 6 / 10 / 4 | 後続ありの 6 区間を受容 |
| 11-02 | 13 | 1 / 1 / 1 | 同 1 区間 |
| 11-03 | 08、11、43 | 3 / 3 / 1 | 同 3 区間 |
| 11-05 | 07、09、21、27、34 | 5 / 5 / 1 | 同 5 区間 |
| 11-08 | 09、16、25、29、48–50 | 5 / 7 / 3 | 同 5 区間 |
| 11-09 | 02–03 | 1 / 2 / 2 | 同 1 区間 |
| 11-15 | 01 | 1 / 1 / 1 | 同 1 区間 |
| 11-16 | 02、05、13、16、29 | 5 / 5 / 1 | 同 5 区間 |
| 11-17 | 03、09、23、25 | 4 / 4 / 1 | 同 4 区間 |
| 11-18 | 17 | 1 / 1 / 1 | 同 1 区間 |
| 11-19 | 39、41、43 | 3 / 3 / 1 | 同 3 区間 |
| 合計 | 11 夜 | 35 / 42 / 4 | 穴の本数による degraded は 0 |

11 夜すべてで、後続足がある穴自体は停止理由にならない。逐次観測中は後続が判明するまで restricted になり得る。取得が通常どおり追随し、再取得が成功し接触が無ければ全夜で自動復帰できる形である。ただし cache だけで「当時も 5 分以内に応答した」「停止が必ず 0 回」とは断定しない。実際の到着遅延・通信失敗・接触は独立の停止理由として残す。

## 穴と停滞の検出・時刻

対象は registry の hard 1m key `(pair, interval, storage_source)`。soft 系列の不調は昇格理由にしない。上位 hard 足の失敗・空・停滞は現行規則を維持する。上位足の内部欠けを新たに全列挙したり、次の 15m 成功を待ったりしない。

足時刻は UTC 開始時刻、区間は半開 `[start,end)`。期待時刻列は `src/agentic_fx/core/market_hours.py:77-88` と同じとし、週末・祝日・DST は `:30-43` の開閉場を使う。「直前」「後続」はこの列上の前後であり、単純な ±60 秒ではない。

- ready 中も key ごとの検査端点を state と同じ transaction で保存する。停滞を検出した末尾の起点も保存し、後続到着時の missing_since として引き継ぐ。端点から今回の最新確定足までを列挙し、同 source の実足との差集合を穴として登録する。内部穴には必ず後続の実足がある。複数 pair・連続・反復を個別に扱い、数の上限による停止は作らない。
- deferred・backoff・未試行の後に複数の確定足が入る場合も、通常保護が見なかった過去足を確認対象にする。保存前の水位から新しい最新足までにある過去足をすべて接触検査し、最新足は通常保護へ渡す。再取得応答の全確定足も検査する。最新 1 本だけの検査で済ませない。
- 停滞の起点 `S` は、最後の watermark の次の期待足が確定する予定時刻 (既存 grace 込み)。`outage.py:557-569` の式を共用する。検出 tick の now を起点にしない。末尾未到着はまだ内部穴ではなく停滞である。
- 停滞時間は S 以後の開場秒数で数え、既知の休場秒は除く。exposure がある停滞の境界は開場 300 秒後、flat は開場 `flat_stall_max_sec` 秒後。休場を挟まなければそれぞれ `S+300秒` / `S+flat_stall_max_sec`。0 の設定は flat の猶予なしを維持する。flat から exposure ありになったら同じ S の 300 秒で再判定し、逆なら flat の期限を適用する。S はどちらでも動かさない。
- 5 分ちょうどは猶予内。判定は境界以降最初の開場 tick で行う。その tick の prepare 結果を先に照合し、回復した最新足の本来の確定時刻が境界以内なら期限内回復として扱う。境界後の足しか回復根拠がない、または依然 stalled なら degraded。prepare の完了だけが境界を越えた理由では昇格しない。成功でも古い水位のままなら回復ではない。
- scheduler は約 60.3 秒周期で、tick 冒頭の now を prepare/observe に共用する (`src/agentic_fx/service.py:2480-2485`, `:2938-2962`)。上記は観測の量子化を許す規則で、任意の遅い tick を救済するものではない。境界 tick を逃して再起動した場合、期限超過を保持し、停止中の不明な到着時刻を推測して 5 分以内回復としない。

## 1 回の取り直しと保存

穴を後続足で初めて特定した時刻を `detected_at`、区間の最初の欠けた足の本来の確定時刻 (既存 grace 込み) を `missing_since` とし、`probe_due_at=missing_since+120秒` を保存する。取り直しは後続足が確認でき、かつ due 以降となる最初の開場 tick の lock 外 prepare で開始する。長い穴を後続で発見した時点で due 済みなら、その prepare 中に確認要求を行う。検出からさらに 2 分待って 4 分の停滞を 6 分へ延ばさない。旧 C/V/D と「60 秒以内の完了」の窓は廃止する。後続がまだ無い末尾を不在認定しない。

`prepare(now, gap_probe=...)` の追加引数は省略可能。同 key の同時に due な区間は一つの range 要求にまとめる。下限は通常下限と穴の直前期待足の早い方、終端は now。同じ primary/source へ要求し、fallback・tick 取得・補間を使わない。prepare は既存の未確認末尾と通常応答の時刻集合を照合し、後続が現れて due 済みの区間も同 tick の追加 range 要求へ回す。候補 ID・due・開始印の保存は短い core_lock 区間で済ませ、通信は lock 外で行う。due の hard probe を soft/budget/backoff より優先し、通常取得と合わせた既存 ingest budget 内で打ち切る。timeout は失敗として保存し、検証後の健全 tick 待ちだけを理由に restricted を延長しない。

report は候補 ID、source、要求範囲、cutoff、開始・完了時刻、応答の確定足集合と内容、結果を持つ。通常の succeeded では probe 完了としない。要求を開始した印を先に保存し、各区間の明示的な取り直しは最大 1 回。開始後 crash は結果不明として記録し、再起動で再送して「1 回」を増やさない。

| 結果 | 扱い |
|---|---|
| 同 source の正常な非空応答、穴の前後の実足を含む、対象時刻は不在 | `absent_validated`。残った穴を取引なしで確定。穴内の期待足がすべて存在する条件は置かない |
| 穴の一部または全部が後着 | 応答した全確定足を接触検査。残る穴は正常応答の範囲内なら取引なしで確定 |
| 空・例外・時刻規則違反・切詰め・前後の既知実足不足・source 不一致 | `unverified`。不在とは区別し degraded、回復しても手動確認へ。失敗を「取引なし」にしない |
| 未開始で due 超過・閉場 | 次の開場 tick で同じ due の要求を開始。再起動時刻で due を延ばさない |
| 保存失敗・開始後結果消失 | `unverified`。gate を閉じ、成功記録・解除根拠を残さない |

候補は epoch に寿命を縛らない独立 ID を持つ。ready のまま作成・完了でき、接触時にその時点の停止 epoch に結び付ける。保存は key/source/区間、検出時刻・missing_since・due・開始/完了・結果、候補作成時の spread、後着足の指紋・接触種別・注文参照。ready 中の端点更新と候補作成は一括保存し、過去の不在を永久保持する索引・毎 tick の注文 snapshot は作らない。完了した穴を無期限に再 probe しない。

cache 保存後、core_lock 内の明示 transaction で端点・候補結果・state・接触の確認待ち・streak を確定する。cache だけ保存されて crash しても未完了候補は解除根拠にならない。source/key 世代が変わった prepare 結果は破棄する。通知記録は安全状態と別 transaction のまま。observe 失敗でも scheduler の保護 tick は続ける (`src/agentic_fx/service.py:2525-2536`)。

停滞中に回復足が来ても、必要な穴の確認がまだなら restricted を維持するのは元の S の期限まで。期限内に確認が済まなければ `unverified` で degraded にし、確認完了後の追加 3 tick 待ちは設けない。flat の既存 1800 秒は短縮しない。

### 起動・停止中の区間

開場中の observe が 1 回 commit されるまで起動 gate を閉じる。閉場中の observe は完了に数えない (`outage.py:280-284`)。初回導入・新 key/source ではその最初の水位を端点とし、過去全履歴を穴として遡らない。既存の non-ready/pending は移行で消さない。

通常の再起動では端点と候補・due・S・接触の確認待ちを読み戻す。停止中の区間も、後続足があれば内部穴として受容し、未開始候補は due 済みなら最初の prepare で 1 回確認する。穴が古い理由だけで degraded にしない。再起動時に後続も無い停滞は旧 S で期限判定する。保持期間外などで生存履歴・区間を検査不能なら `unverified`。単に端点が無い初回導入と、既存端点を失った破損は区別する。streak は再起動で 0、期限は不変。

## 後着足の接触検査

純関数 `protection_contact(orders, bars, spread)` で評価し、注文・約定・損益を書き換えない。対象は穴や一括到着した足の区間と生存期間が重なる exposure 全体。現在 closed/cancelled でも、その区間に生存した可能性があれば除外しない。

毎 tick の履歴 snapshot は保存しない。direction / requested_price / stop_loss / take_profit は insert 後不変を実装時の静的検査で固定する。生存期間は `created_at` から `closed_at`、他の終端なら `updated_at` までを保守的に長く取る。非終端は上限なし。`filled_at` が確実な場合だけ保有開始を絞る。時刻不整合・broker 結果不明は安全と断定しない。spread は候補作成時の `assumed_spread_pips × pip_size` (`scheduler.py:1837-1842`) を固定し、再起動後の設定で置換しない。

| 区間と重なる状態・証拠 | 判定 |
|---|---|
| OPEN、または確実に保有した履歴、方向・有限 SL・任意の有限 TP が有効 | `paper_fills.py:34-43` と同じ half spread で SL/TP の接触真偽を評価。long は low/open−half ≤ SL、high−half ≥ TP、short は high/open＋half ≥ SL、low＋half ≤ TP。両方も記録。決済価格の計算は不要 |
| 未約定が確実な PENDING_FILL、または約定せず終端 | 検査対象として列挙するが、仮約定は作らず接触なし。取り逃しを replay で埋めない |
| protection_pending / submitting / submitted / closing / cancelling / submit_unknown / cancel_unknown / close_unknown | 保有の有無・保護を確定できなければ `late_bar_contact_unknown`。単に OPEN でない理由で除外しない |
| 方向・SL・spread・生存時刻の欠損や非有限値、状態不明 | `late_bar_contact_unknown` |
| 正常な再取得で実足が無い | 実足の接触検査はなし。「取引なし」の裁定を適用 |

接触は `late_bar_protection_contact`、不明は `late_bar_contact_unknown` として degraded・pending・接触の確認待ちを永続化する。不明も自動解除しない。SL/TP/両方/不明、足時刻、注文参照、判定関数版を内部記録に残す。activity/Discord に価格・金額・口座情報を出さない。後から非接触の足を見てもこの確認待ちを消さない。

## 復帰と状態機械

flat は全 pair を通じて `orders.list_by_status(conn,*_EXPOSURE)` が 0 行。集合は open / pending_fill / protection_pending / submitting / submitted / closing / cancelling / submit_unknown / cancel_unknown / close_unknown (`src/agentic_fx/core/executor.py:29-31`)。quantity=0 でも非終端なら flat ではない。未知 status・読取り失敗は flat 不明。approval_pending は含めないが、承認後 submit には既存 gate を適用する。

健全は現行 `outage.py:317-326` を基礎に、1m hard 全 key の非空成功と水位回復を使う。上位 hard 足に episode 内の初回成功を追加要求しない。受容した内部穴に連続性を要求しない。degraded の flat 自動解除だけは、flat 成立後に水位が進む健全 3 tick を要求する。同じ now・同じ足の再取得は加算せず、失敗・空・deferred・停滞・exposure 再発で 0。閉場・再起動でも 0 から確認する。scheduler の決済は observe の後なので、全決済の次の observe から数える。

通常の pending は表示と履歴の印で、flat 自動解除の gate ではない。接触の確認待ちは別の永続条件として自動解除を禁止する。`auto_resume_when_flat=false` は既存どおり自動解除しない。停滞回復では検証を自動解除のために開始せず、健全 3 tick で degraded の手動待ちへ進む。穴の受容・後着接触の監視はこの設定と別であり、ready 中の背景の取り直しは続ける。設定名は維持し、example の注記に自動停滞復帰への適用と接触の例外を追記する。

状態更新は純関数 **`transition(row, facts) -> (row_next, effects)`** 一つに集める。facts は穴集合、probe 結果、接触/不明、停滞 S と期限、flat、健全、設定、手動要求、時刻。`_observe_open` と `_consume_resume_request` はこの関数の結果だけを保存し、独自の解除判定を持たない。表は上から優先し、行 ID を test に対応させる。

| ID | 入力・現状態 | 次状態・効果 |
|---|---|---|
| A | 接触または接触不明、任意 state・flat の別なし | degraded、pending=1、接触の確認待ち、streak=0、停止通知。ready なら停止 epoch を開く |
| B | 取得失敗・空・保存失敗・確認不能、任意 | degraded、streak=0。exposure/auto=false/確認不能なら人の確認待ち。ack で未確認を成功扱いしない |
| C | 後続足のある内部穴だけ、ready | ready のまま候補登録。穴の個数で epoch を開かない |
| D | 1m 停滞、猶予内、ready/restricted | restricted。exposure があっても即昇格しない |
| E | 停滞回復・必要な穴の確認済み・auto=true、期限内の restricted | ready、pending=0。追加の健全 3 tick 待ちは作らない |
| F | 停滞・未確認のまま期限超過、または期限後に回復、restricted | degraded。exposure ありなら pending=1。flat は元の 1800 秒を適用 |
| G | 上位 hard 不調、任意 | 現行の degraded 経路。内部穴・個数とは混ぜない |
| H | degraded、接触の確認待ちなし、flat、auto=true、回復・確認済み | 健全 1–2 tick は維持、3 で ready と pending=0 を一括保存 |
| I | degraded、接触の確認待ちあり / exposure あり / auto=false | 自動解除なし。健全に戻れば人の再開待ち |
| J | 手動要求、未確認 / 不健全 | `rejected_unverified` / `rejected_watermark`。ack でも確認前の解除不可 |
| K | 手動要求、確認済み・健全 | 未処理近似の注文参照と接触/不明の注文参照の和集合を ack 対象とする。未 ack は `rejected_unprocessed`。ack が必要な場合は flat でも必要。受理時に接触の確認待ちも解除 |
| L | 自動判定と手動要求の競合 | 観測→自動判定→要求消費。先に ready なら `already_ready`。要求 key の比較更新で置換済み要求を消さない |
| M | 閉場 | 要求・probe は保留、streak を増やさず、due/S を動かさない。開場後の最初の観測へ |

B の確認不能は、確認結果の回収・保存が済むか、人が結果不明を調査して安全記録を確定するまで解除条件を満たさない。単なる ack は調査の代わりにしない。auto=false でも手動解除の健全条件は同じ。

不変条件:

1. non-ready は新規・リスク増を止めるが、決済・取消・close retry・既存 SL/TP の保護を止めない (`scheduler.py:342-348`)。
2. 後続ありの穴は数・時刻で停止理由にしない。補間しない。空応答・通信失敗を「取引なし」にしない。
3. 一括到着・取り直しで入った過去の全確定足を接触検査し、最新足だけを見て安全としない。
4. 停滞の猶予・候補 due を再起動・deferred・手動 ack で延長しない。restricted は S の期限内に限る。
5. degraded の自動解除は常に全 exposure ゼロを要求し、接触の確認待ちは flat でも自動解除しない。期限内 restricted の自動復帰は exposure があっても許す。
6. 他の kill switch・リスク・承認 gate はデータ停止の解除では解除しない。

## 通知と status

outnotice を先に merge する。その実体の語彙は `tmp/wt/outnotice/src/agentic_fx/datafeed/outage.py:30-34`、表示表は `:48-81`、要求比較更新は `:854-869`。C0 の引用から行番号が動いているため、今回読み直した位置を使う。

| 事実 | 通知の記録・confirmed |
|---|---|
| ready 中の穴の発見・取り直し・非接触・不在 | Discord 無通知。停止 epoch も problem_streak も進めない。内部記録のみ |
| 期限内 restricted・期限内自動復帰 | 従来の短い restricted と同じく無通知。activity に復帰理由を記録 |
| 接触/不明による degraded | 同 tick に confirmed=1、既存固定本文の stopped を必ず 1 回記録。flat・過去の待ち印・要求中でも抑止しない |
| 通常の失敗・停滞による degraded | outnotice の連続問題 2 tick / 開場時間 10 分の停止条件を維持。restricted 中の停滞も problem_streak は進めるが通知は degraded でのみ判定 |
| probe 確認不能による degraded | confirmed=1、stopped を 1 回。内部穴だけでは進めない problem_streak と区別する |
| 回復後の人の待ち | awaiting_resume を同 epoch 1 回。接触の stopped を先に保存し、両方が初回なら順序も固定 |
| 通常 degraded の flat 自動解除 | 停止/待ち記録のある epoch だけ既存 resumed を 1 回。自動確認中は新しい awaiting_resume を出さない |
| 手動要求 | accepted/rejected/already_ready を必ず終端記録。接触 episode の受理は既存の手動再開通知を使う |

記録の一意キー・配送 retry は outnotice を継承する。安全状態 commit 後の別 transaction で保存する (`tmp/wt/outnotice/src/agentic_fx/datafeed/outage.py:879-888`, `:925-944`)。記録前 crash の欠落限界は同じだが、接触の確認待ちから起動時に stopped の未記録を再照合する。通知失敗で保護 tick や安全状態を戻さない。価格・金額・口座情報を本文に含めない。

status は確定した facts から一つの `resume_guidance` で決める。既存の unknown → 要求あり → ready を最優先にし、以後は下表を古い拒否・待ち印より先に判定する。

| 条件 | phase | next_action |
|---|---|---|
| restricted、現在停滞 | restricted | wait_tick |
| restricted、足は回復したが穴の確認待ち | gap_verifying | wait_tick |
| degraded、接触/不明の確認待ち、健全・検証済み | awaiting_resume | resume_with_ack |
| degraded、未検証 | resume_rejected (拒否結果があれば) / stopped | restore_source |
| degraded、接触待ちなし・flat・auto=true・健全確認中 | recovering | wait_tick |
| degraded、現在不健全 | stopped | restore_source |
| degraded、手動待ち、ack 対象あり / なし | awaiting_resume | resume_with_ack / resume |
| 必須事実の読取り不能 | unknown | check_status |

`rejected_unverified` を要求の終端結果へ追加する。ack 件数は既存近似だけでなく接触/不明の参照も重複除去して含めるため、近似 0 でも案内が resume に戻らない。過去の拒否は別欄に保持し、現在の recovering より優先しない。`auto_resume_possible` も pending 一つで決めず、この表の条件に揃える。

API の内部状態 4 項目の追加は取りやめ、phase と既存案内だけを公開する。`guidance_text.py` の許可組・ctl・web を同時に更新する。gap_verifying は「穴の区間の確認を待っています」、recovering は「健全な足の確認を続けています。条件が揃えば自動解除します」。旧クライアントは未知 phase を unknown と表示する。

## 実装 task と merge 順

先に outnotice を main に入れる。本件が先の分岐は作らず、schema migration はその後の番号で追加し、旧 fixture も更新する。各行に必要な単体検証を含める。今回は実行しない。

| task | 内容 | 前提 |
|---|---|---|
| T4 | 通常 pending の全 exposure ゼロ・健全 3 tick 解除、接触の確認待ちを除外する保存条件、recovering の表示・通知。既存 epoch 66 にも適用 | outnotice。最初に単独 merge できる。後着検査の機能はまだ有効にしない |
| T1 | ready 中の端点、独立候補・due/開始/結果/spread、接触の確認待ちの schema と保存口、期待時刻列。冪等 migration | T4。outnotice 後の schema_version を使用 |
| T2 | gap_probe の範囲要求・全確定足 report・最大 1 回の開始記録・cache 保存。呼出しなしでは既存動作 | T1 |
| T3a | exposure 全体と生存期間の接触純関数。不変の保護条件を静的検査で固定し、仮約定・価格計算は入れない | T1。合成 fixture で単独検証 |
| T3b | transition の表 A–M、穴の無停止受容、停滞 300/1800 秒、後着全足の検査、期限内復帰。通知・status を同時接続して有効化 | T2、T3a、T4。接触時 stopped と手動優先が揃ってから有効化 |
| T3c | 起動 gate、停止中区間・crash 復元、手動要求・ack 集合・競合、rejected_unverified | T3b。起動・手動の新経路を揃えるまで新動作を本番設定で有効にしない |
| T5 | ctl/web の案内整合、設定 example、outage docstring、service の cache 注記、関連通知設計・運用手順の同期 | T3c。flat_stall_max_sec は残し、auto_resume_when_flat の適用範囲を明記 |
| T6 | 11 夜の形・境界・通知を fake/Dunia で統合受入 | T5 |

## test と受入

以下は実装時に行う検証。今回は test を回さない。表 A–M を入口 × state のパラメタ化 test とし、同一 facts なら observe と手動要求で異なる結論にならないことを確認する。

| 境界・入力 | 期待 |
|---|---|
| 内部穴 0/1/連続 2–4/多数、反復・複数 pair・任意時間帯 | C。後続ありは ready 維持、候補登録、補間なし。穴の本数で degraded にしない |
| due 直前/ちょうど、連続 4 分後の初回発見、60.3 秒周期で直前 tick 開始・prepare が境界越え | due 以降最初の tick で 1 回。60 秒の完了窓による偶発的な拒否なし |
| 停滞 300 秒直前/ちょうど/直後、回復足の確定が期限内/外 | D/E/F。期限内の足を最初の境界 tick で認め、境界外だけの回復は degraded |
| 停滞中に穴確認待ち、deferred を反復 | 元の S の期限で F。検証後の無期限 restricted・追加 3 tick 待ちなし |
| flat の 1800 秒/設定 0、flat↔exposure 変化 | S を変えず各期限を適用。flat の従来の猶予を短縮しない |
| 上位 hard の未試行・deferred/失敗/空/停滞、soft 不調 | 前二者だけでは回復を待たず、hard 不調は G、soft は無関係 |
| 正常非空で全不在/一部後着/全部後着、空/例外/範囲不整合/source 違い | 不在だけ認定、後着全足を検査、失敗は B。成功の水位だけで検証済みとしない |
| 同 tick の複数新着足・deferred 後の一括到着 | 手前の足の SL/TP 接触も A。最新だけの通常保護から漏れない |
| long/short、SL/TP/両方、open の価格 gap、非接触 | A または無処置。価格・損益・約定は変更しない |
| 全 `_EXPOSURE` 各状態、既に terminal、区間途中の生成/決済、時刻不整合 | 生存重複を長めに評価。不確実な保有/保護は unknown で A。未約定確実な指値は仮約定しない |
| SL/TP/requested_price/direction の更新経路 | AST/検索で insert 後不変を固定。将来変更経路が追加されたら検査前提の改訂を要求 |
| pending 後 flat、quantity=0、別 pair、未知 status、flat 2 tick 後 exposure 再発 | H は全 exposure ゼロから健全 3 回。同じ足・同じ now は加算せず、不明・再発は解除しない |
| 接触検出時すでに flat/検出後に flat、auto=false | I。自動解除せず stopped と resume_with_ack。通常 pending と混同しない |
| ready の穴と auto=false、停滞と auto=false | 背景の安全確認は継続、停滞から自動復帰しない。自動解除用の追加 probe はしない |
| 再起動、due 超過、開始前/開始後/応答後/commit 前 crash | 元の端点・due・S を維持。開始後結果不明は再送せず B。接触の確認待ちを復元 |
| 初回導入・source 変更・保持期間外・端点破損、閉場中の起動 | 初回は現水位から、既存停止は維持、検査不能は B。初回開場 observe commit まで gate を閉じる |
| 金曜末尾・日曜最初の分の穴、祝日、DST 切替 | 前後は期待時刻列。休場を欠け本数や停滞時間へ誤算入せず、週末後も due を作り直さない |
| 手動要求と自動復帰・ack 置換・休場・新 epoch | J/K/L/M。一件一終端結果。未確認を ack で回避せず、接触は flat でも ack 必須 |
| 古い待ち印・拒否と recovering、接触と要求中が競合 | 現在の案内を優先。接触の stopped は抑止しない。API/ctl/web 同じ表示 |
| 保存・通知・任意注文の検査失敗 | 新規は止まり、残る注文の通常保護・取消・決済は続く。通知の失敗で安全状態を rollback しない |

fixture は上表 11 夜の時刻の有無を使い、価格は合成する。fake bridge、仮時計、一時 SQLite、通知 double で、穴を受容したままの不在確定・後着非接触・後着接触、5 分以内の停滞復帰、5 分超停止、通常 pending の flat 解除を分けて再現する。接触後は全決済しても止まり、人の ack 受理でのみ戻ることを示す。本番 DB/cache の変更、本番 bridge の停止、Discord の試験送信は行わない。

## 利用者に問う事項

追加の問いは 0 件。穴の数による停止を廃止すること、停滞 5 分・flat 1800 秒、接触時の通知と flat より人の再開を優先することは改定裁定で解決済み。取得・保存失敗を不在と認定しないこと、未知 exposure を安全としないことは既存の資金保護を維持する技術条件として明記した。

M5 と削減案 7 の「auto=false で probe なし」は自動停滞復帰のための probe に限定して適用する。ready 中の穴を受容する新方針では、接触の安全監視まで止めると裁定を満たさないため、背景の 1 回確認は設定と独立に残す。M6 の約定時刻・上位足出来高による不在の反証は採らない。新方針では穴を受け入れること自体が裁定であり、それらは欠けた足の保護接触を証明せず、上位足との比較を停止条件に追加すると別の資金保護方針になる。tick 数の強化は別件とする。

## 付録: 文書・前提とコードの食い違い

参照元とコードは今回は変更しない。実装 task に同期を含める。

| # | 文書・前提 | 実体・本案での扱い |
|---|---|---|
| 1 | 15 epoch のうち 14 件を孤立 1 本と扱う | epoch と穴は別。SQL は 11 夜 35 区間。連続 4 本もあるが、新方針では数で停止しない |
| 2 | cache 不在で無 tick を証明 | rates を写すだけ (`mt5_bridge/mt5_client.py:675-705`)。取引なしは裁定に基づく運用認定 |
| 3 | 9/24 設計の backfilling・SL/TP replay が存在 | 現行の対象外 (`outage.py:3-9`)。`docs/superpowers/specs/2026-09-24-outage-stop-and-backfill-design.md:85-96` を実装済みとしない |
| 4 | 停止後は常に手動復帰 | 現行も flat 自動解除あり (`outage.py:409-418`)。通常 pending 後の flat へ拡張 |
| 5 | restricted は flat だけ | 現行は exposure ありで即 degraded (`outage.py:373-388`)。新方針は建玉ありの停滞にも 5 分猶予 |
| 6 | 2 分が停滞の猶予 | 2 分は穴の背景再取得の待ち。停滞は 300 秒 / flat 1800 秒 (`outage.py:370-384` を変更) |
| 7 | 健全は全足連続の証明 | 最新水位・取得結果のみ (`outage.py:302-329`)。受容穴の連続性は復帰条件にしない |
| 8 | 通常 ingest が古い穴を再取得 | watermark−1 本 (`ingest.py:211-225`)。保存候補で 1 回だけ範囲を拡張 |
| 9 | 未処理近似 0 なら exposure なし | `outage.py:699-716` は OPEN/PENDING_FILL だけ。全 `_EXPOSURE` ゼロで flat 判定 |
| 10 | pending 後の flat は自動解除 | main `outage.py:410` と通知 worktree `outage.py:548-559` は not pending 必須。通常 pending は除き、接触の確認待ちは残す |
| 11 | 通知設計の帯例外・flat 解除は未裁定 | `docs/superpowers/specs/2026-11-21-datafeed-outage-notice-design.md:241-242` より改定チケット裁定を優先 |
| 12 | 待ち印・拒否だけで次の手動操作が決まる | worktree `outage.py:48-81` に現在の recovering、接触と未検証の優先行を追加 |
| 13 | auto_resume_possible は pending で false | worktree `outage.py:321-335` を全 exposure と接触の確認待ちに揃える |
| 14 | entry_same_bar=True で TP も検査 | `paper_fills.py:52-57` は TP を抑止。接触は SL/TP の真偽を直接評価、仮約定不要 |
| 15 | SL/TP が変わるので毎 tick snapshot 必須 | C0 の誤り。insert 設定 (`src/agentic_fx/core/executor.py:679-687`) 後の変更経路はない。保護条件の不変を固定し、生存期間は orders の時刻から保守的に取る |
| 16 | 通知と安全状態は同一 transaction | worktree `outage.py:879-888`, `:925-944` は後段の別保存。この分離を維持 |
| 17 | scheduler が provider 経由で 1m cache を温める | `src/agentic_fx/service.py:1602-1606` の注記と実体が不一致。本番は `:1829-1832`, `:1878` の DB 読取り、保存は `ingest.py:316-320` |
| 18 | example・docstring は flat の短停滞だけを説明 | `config/settings.yaml.example:63-65`, `outage.py:3-7` を停滞 5 分・穴受容・接触の人手優先へ同期 |

## 変更履歴

| 日付 | 版 | 変更 | 理由 | commit |
|---|---|---|---|---|
| 2026-11-21 | C0 | 短い欠けと flat 自動解除を統合。期限、接触、全 exposure、通知・merge 順、受入境界を定義 | 2 チケットの承認済み方針を具体化し、実欠けの形と現行実装を区別する | 3eaa4936 |
| 2026-11-21 | C0.1 | 穴の無停止受容と 1 回の背景確認、停滞 5 分・flat 1800 秒、接触時の人手優先へ改訂。11 夜の SQL、永続化・状態表・通知・test・task を更新し snapshot 等を削減 | 設計レビュー 1 周目の指摘と利用者の方針の改めを反映 | |
