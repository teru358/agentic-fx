# 同梱 web のチャートの段: 資金推移と戦略ごとの成績 (web-ui-charts-equity-indicators) 設計 C0.1

**チケット:** `web-ui-charts-equity-indicators` (低、機能)。関連: `web-layout-tabs` (配置の段、完了)、`web-css-design` (CSS の段、完了)
**状態:** C0.1 (下書き、未承認)。§6 の問いは未裁定

パスの略記: A = `afx-ops/afx_ops`、T = `afx-ops/tests`、S = `src/agentic_fx`。
本番 DB は `sqlite3 -readonly data/agentic.db` で、件数・期間・列の分布だけを確かめた (2026-11-19)。金額・plugin の名前・取引の正確な件数は書かない (件数は概数)。

## 0. 何が起きているか

- **資金の推移を見る画面が無い**。web が出すのは今の `equity`・`balance` の 2 つの値だけ (Status の概要のカード)。過去の線、HWM、kill switch との距離は見えない。
- **戦略ごとの成績を見る画面が無い**。決済の event には起動理由 (`origin_code`) が載る (`S/ops/trade_events.py:253-255`) が、集計はどこにも無い。
- **図を出す仕組みが無い**。web は HTML と 1 本の CSS だけで、`<svg>`・`<script>` を出さない。

確かめた事実 (この設計の前提):

| # | 事実 | 根拠 |
|---|---|---|
| F1 | `account_snapshots` は約 6 万行、約 80 日分。1 分ごとの時価評価で 1 行。1 時間を超える空き (週末など) が約 20 か所 | DB。`S/core/scheduler.py:973-1005` |
| F2 | 全行を時間で束ねる集計は 0.04〜0.06 秒 (1 時間幅で約 1,000 束)。直近 7 日だけなら 0.003 秒 | DB で実測。index は `(ts, id)` (`S/store/db.py:743-744`) |
| F3 | HWM は前の行の HWM と今の equity の大きい方。入出金 (`cashflow`) は HWM にも足す。時刻が逆行する行は拒否 | `S/core/accounting.py:17-35` |
| F4 | 本番 daemon の今の書込み経路では、HWM が下がるのは 2 つの場合だけ: 付け直しの行と、負の `cashflow` (出金) の行。付け直しは **元の行と同じ `ts`** で、`hwm = equity`、`source = 'manual_ks_rebase'` の行を 1 つ足す。台帳は `kill_switch_hwm_rebases` (追記専用) | `S/ops/rebase_accounting.py:196-213`、`accounting.py:31`。DB では HWM の下降・同じ ts の重複・台帳の行が同じ数で、出金による下降は無い |
| F5 | `cashflow` は全行 0。0 以外を書く経路は今は無い | DB。`scheduler.py:1005` は `cashflow` を渡さない |
| F6 | `source` は `paper` と `manual_ks_rebase` の 2 種。**live の値を書く経路は無い**。`orders` には paper / live を分ける列が無い | DB。`scheduler.py:1005` (既定値)、`S/store/db.py` の `orders` |
| F7 | kill switch の**今の状態**は state file にしか無い (`latched`・`generation`・`latched_at`)。履歴は持たない | `S/store/state.py:80-82`、`:359-369` |
| F8 | kill switch の**履歴**は `ops_events` の `guard_state_changed` (state は `latched` / `released`、世代つき)。best effort で、欠けた分の埋め戻しを見送ることがある。`ops_events`・`account_snapshots` を消す処理は無い | `S/ops/service.py:138-159`、`:1975-1996`。`S` 全体を grep して両表への DELETE なし (`S/store/db.py:928` の表名の一覧は定義だけで、参照が無い) |
| F9 | `ops_events` は 10 月上旬からしか無い (snapshot は 8 月末から)。kill switch の event は数件で、`released` だけがあり `latched` が欠けた世代が実際にある | DB |
| F10 | 注文の帰属は `orders.intent_id → trade_intents.mission_id → missions.trigger`。今の呼出し箇所では `intent_id` を書くのは注文を作るときだけで、決済では変わらない | `S/core/executor.py:682`、`S/ops/trade_event_sink.py:343-344` |
| F11 | `trigger` が NULL の mission は improve と reflection だけ (約 600 件)。trade の mission (数千件) は全件 `cron` か `signal:<plugin>` | DB。`S/store/db.py:666` |
| F12 | 注文は 20 件あまりで、全件 `intent_id` あり。閉じた取引は約 20 件で、大半が判断による決済、SL と TP は数件ずつ。部分約定・取消・期限切れは 0 件 | DB |
| F13 | 起動理由は数種 (`cron` と複数の `signal:`)。大半は閉じた取引が 10 件未満 | DB |
| F14 | 今の呼出し箇所では `orders.stop_loss` を書くのは注文を作るときだけで、後から動かす経路は無い。intent の payload にも `stop_loss` があり、全件で一致 | `executor.py:687`、`:1212`。grep (`stop_loss` を渡す `update_fields` は無い)、DB |
| F15 | **R の定義がコードに 2 つある**。(a) 値幅: `(close − entry) × 向き ÷ |entry − SL|` (通知・event の `pnl_r`)。(b) 金額: `realized_pnl ÷ (|約定 − SL| × 数量 × 契約単位)` (backtest と床の `avg_r`、手数料を含む) | (a) `S/ops/trade_events.py:214-222`、(b) `S/backtest/metrics.py:89-97` |
| F16 | 勝率・PF・件数の定義は backtest にある。勝ちは `pnl > 0`、PF は損失が 0 なら無し、30 件未満は `evaluable` が偽 | `S/backtest/metrics.py:20`、`:100-104`、`:131-132`、`:142` |
| F17 | `realized_pnl` は手数料を引いた値。スワップ (`fees_swap`) は別の列で、今は全件 0 | `S/core/paper_broker.py:4-6`、DB |
| F18 | daemon の応答には表示 tz (`display_timezone`) が必ず付く | `S/ops/api_server.py:656` |
| F19 | `STYLE` は 2,962 byte、上限は 4,096 byte (test)。残りは 1,134 byte | 実測、`T/web/test_render.py:192` |
| F20 | web の principal (`ops-web`・`ops-web-write`) は `status.read` を持つ | `S/ops/contracts.py:106-112` |
| F21 | **ラッチは毎 tick の判定ではない**。DD が閾値以上の間、tick は予約の約定を止めるだけで、ラッチを書かない。ラッチは OPEN の判断が risk gate の DD 判定まで到達した場合に書く | `S/core/scheduler.py:294-301`、`S/core/risk_gate.py:195`、`S/core/executor.py:653` |

## 1. 何が変わるか (動作)

利用者 (運用する本人) は、上部の nav に増える **Performance** を開く。書込みは無い。JS は使わない。

### 1.1 資金推移のタブ (既定)

1. **期間を選ぶ**: 「7 日 / 30 日 / 90 日 / 全期間」の link (既定は 30 日)。link は `?range=` を付け替えるだけ。
2. **図を読む**: 横は時刻 (表示 tz)、縦は口座通貨の金額。
   - **equity**: 実線。その後ろの薄い帯は、束ねた時間の中の最小〜最大。帯の下端がその時間の最も悪い equity。
   - **balance**: 破線。
   - **HWM**: 点線の階段線。実際に変わった時刻で段になる。
   - **作動の水準**: HWM × (1 − kill_pct / 100)。細い階段線。equity がここに届くと予約の約定が止まり、次に建てる判断が出たときに kill switch が作動する (F21)。**線を割っても、建てる判断が出る前に戻れば「✕」は付かない**。
   - **印** (縦線と記号): kill switch の作動「✕」、解除「○」、HWM の付け直し「▽」、入出金「◇」、不明な HWM 下降「△」。
   - 記録の無い時間 (週末・停止) は線を切る。つないで埋めない。
3. **今の状態** (図の上の別枠、毎回取得): ラッチ中 / 解除済み / 不明、世代、状態取得時刻、最新 snapshot の時刻・経過秒・鮮度を必ず出す。取得できなければ「不明」。最新値が 10 分より古ければ「古い記録」と表示する。
4. **数字で読む** (図の下の表): 期間の初めと終わりの equity・balance、最大・最小、**最大 DD (率と時刻)**、最新記録の HWM と作動の水準までの余裕 (記録時刻つき)。最大 DD は間引く前の全行から出した値。
5. **印の表**: 作動・解除・付け直し・入出金の実時刻 / 記録時刻の区別と出所、付け直しの位置と操作時刻 (位置の新しい順)。時刻は `YYYY-MM-DD HH:MM:SS +09:00`。
6. **注記** (固定文): 「線は時間で束ねた表示で、判定の根拠ではありません」「kill switch は、equity が作動の水準以下のときに建てる判断が出ると作動します。線を割っただけでは印は付きません」「kill switch の印は記録がある分だけです。event の記録は <最初の記録の日> 以降です」「期間内にも履歴の欠けがあり得ます」「余裕が正でも解除されたとは限りません」「束内の厳密な交差は再現していません」。

### 1.2 戦略別のタブ

1. **見出しの下に必ず出す文**: 「**その戦略のシグナルで起きた判断の成績**です。建てる・決済する判断は LLM が行うので、戦略そのものの成績 (backtest) とは違います」。
2. **表** (1 行 = 起動理由。下の数字は架空の例):

   | 起動理由 | 件数 | 勝 / 負 / 分 | 勝率 | PF | 平均 R | 損益の合計 | 決済の内訳 | 保有中 |
   |---|---|---|---|---|---|---|---|---|
   | `signal:<plugin>` | 12 (参考) | 7 / 4 / 1 | 58.3% | 1.40 | +0.21 | +… | 判断 8 / SL 3 / TP 1 | 1 |
   | `signal:<plugin>` | 3 | 2 / 1 / 0 | – | – | – | +… | 判断 3 | 0 |
   | 定期の判断 (`cron`、戦略に属さない) | 4 | … | – | – | – | … | … | 0 |

   - **件数は必ず出す**。件数が少ないうちは率を出さない・参考と明記する (§2.6)。
   - 帰属は**建てた判断**で決める。決済を後の定期の判断が出しても、行は変わらない。
   - 対象は選んだ期間に**決済した**取引。期間の link は資金推移と同じ。
3. **累積損益の図**: 起動理由ごとの線 (決済のたびに段が動く)。期間の初めを 0 とする。線は色と線種の両方で区別し、凡例に記号を添える。
4. **注記** (固定文): 「累積損益の合計は equity の変化と一致しません (含み損益、期間の外で決済した取引、スワップを含まないため)」「同じ名前の plugin の版が変わっても、同じ行に数えます」。

### 1.3 変わらないもの

ほかの画面の表示・URL・タブ、書込みの画面の規律、時刻の規則、CSP の script を許さない形、daemon の発注・資金保護。Status の概要のカード (`equity`・`balance`) はそのまま。

## 2. 仕組み

### 2.1 server SVG にする (JS は入れない)

**推奨: この段は server が描く SVG だけ。JS は入れない。**

| 観点 | server SVG | 同梱 JS |
|---|---|---|
| この段で要る操作 | 期間の切替は link で足りる | 重ねる・拡大・値の読取り (hover) ができる |
| CSP | `script-src` を足さない。今の「script は一切なし」が続く | `script-src 'self'` か hash。JS の配信 route と cache の規則が増える |
| 今の test | `<script` が無いことを見る 38 行がそのまま通る | 全部を見直す |
| 配布 | 変更なし | build 済みの file を同梱する (install に Node を要求しない) 手順が要る |
| 失うもの | 点ごとの値を図から読めない | — |

- 失うものへの手当て: 図の下に数字の表を置く (§1.1 の 4・5)。印には SVG の `<title>` を付け、ブラウザの標準の吹き出しで時刻が読める。
- 既定の方針 (server SVG を先に、JS は対話が要るとき) のとおり。対話が要るのは価格チャート (拡大・移動・重ね) で、それは後の段 (§7)。

### 2.2 データの経路

```
web (afx-ops) ──GET /v1/performance?range=30d──▶ daemon (ops API、status.read)
                                                   └ S/ops/performance_view.py が DB を読んで束ねる
```

- **新しい読取 endpoint が 1 本要る**。今の `GET /v1/status` は今の値だけ、`GET /v1/events` は 10 月以降の event だけで、snapshot の列も注文の集計も返さない。
- **集計は daemon 側**。web は DB を持たず、6 万行を渡すと応答が数 MB になる。daemon は束ねた代表点 (基本 280 束、分割後最大 320 点) と集計済みの行だけを返す。
- **scope は `status.read`**。口座の値は今も `status` で同じ principal に見せている (F20)。新しい scope は作らない。`ops-notify` (`events.read` だけ) には見えない。
- **読取専用**。監査行 (`ops_requests`) は書かない (ほかの GET と同じ)。DB への書込みは無い。
- **CLI は足さない**。daemon の生きた状態を読むので、置くなら `afx ctl` の「稼働観測・実行追跡」だが、この段は web だけにする (§7)。`cli_surface` の検査は CLI の記録から endpoint を引く向きなので、記録の無い endpoint は通る (`tests/test_cli_surface.py:183-195`)。逆向きの検査が無いことは T1 で確かめる。
- web 側: `WebClient.performance(range=)` を足す (`A/api_client.py:535-573`)。`WebWriteClient` は継承するので、書込みを有効にした web でも同じ。

応答の契約 (`data`)。全 key は必須。時刻は offset つき UTC ISO 8601、件数は非負整数 (bool 不可)、金額は account_currency、DD・閾値・勝率は百分率、R・PF は無次元の有限数。null は下表のときだけ許す。期間は共通の `[from,to]` (両端を含む)、to は履歴読取開始時刻 (cache では保存済みの端)。7d / 30d / 90d の from は to から各日数を引いた時刻。all の from は最古の有効 snapshot / 決済 / 印の時刻 (全て空なら to)。保有中・決済未確定・現在状態だけは期間に依存しない。

| key | 型・単位・null・順序・上限 |
|---|---|
| `range`, `from`, `to`, `bucket_sec` | enum `7d/30d/90d/all`、期間の端 2 個、正の整数秒 (最終的な束幅) |
| `mode`, `account_currency`, `display_timezone` | 現設定の文字列。mode・通貨は各 16 UTF-8 byte、tz は 64 byte 以下 |
| `as_of`, `computed_at` | 履歴読取内の最新有効 snapshot 時刻 (無しなら null)、集計完了時刻。cache 利用時も元の時刻 |
| `current` | 下表。履歴 cache の外で毎回取得 |
| `points` | 下表の代表点、`(last_at,last_id)` 昇順、最大 320。省略区間は接続しない |
| `hwm_steps` | `{t,id,hwm,seg}`。実際の変化点と各区間の最初・最後、`(t,id)` 昇順、最大 640。作動水準は各 hwm から算出 |
| `stats` | 下表。全有効 snapshot から計算し、図の省略に依存しない |
| `marks` | 下表の印、`(plot_at,kind,source_id)` 降順、最大 40 |
| `marks_total`, `marks_graph_omitted`, `marks_table_omitted` | 重複排除・期間抽出後の総数、図・表それぞれの省略数。表は最新 20 件。web でさらに削った数も各表示へ加算 |
| `marks_since`, `other_sources` | 最古の kill switch event 記録時刻 (無しなら null)、期間内の除外 source 行数 |
| `strategies` | 下表の行、最大 50 (cron・帰属不明・合算行を含む)。順序は §2.6 |
| `unattributed` | `{closed_count,open_count,close_pending_count}`。期間内決済と現在保有・決済未確定を区別 |
| `thresholds` | `{min_count:5,reference_count:30}`、率の母数に適用 |
| `omissions` | `{equity_points,hwm_steps,strategy_rows,strategy_series,strategy_points}`。各描画から省いた代表点・段・元行・系列・系列点の数 (系列点は origin 別辞書)。元 snapshot を束ねた件数とは別 |
| `quality` | `{partial,invalid_snapshots,invalid_marks,invalid_orders,invalid_series_points,unknown_hwm_drops,unplaced_closed_count}`。partial は bool、他は件数。期間不明の決済は末尾の項目へ。正常なら false / 0 |

| 入れ子 | 全 key と意味 |
|---|---|
| `current` | `latched` (bool / null)、`generation` (整数 / null)、`latched_at` (時刻 / null)、`state_fetched_at` (取得を試みた時刻)、`snapshot_at` (最新有効 snapshot 時刻 / null)、`snapshot_fetched_at` (取得時刻)、`snapshot_age_sec` (非負秒 / null)、`snapshot_freshness` (`fresh/stale/unknown`)。10 分超は stale、取得不能・欠損は unknown。状態の値も取得不能なら null |
| 代表点 | `bucket_from,bucket_to` (epoch 束の端)、`start_at,end_at,last_at,last_id` (部分束の実時刻と最後の行 id)、`n` (行数)、`equity_min,equity_max,equity_last,balance_last` (金額)、`dd_max` (百分率)、`seg` (非負整数の区間番号)。null 無し。帯は実データのある `[start_at,end_at]` の幅、代表線は last_at に置く |
| `stats` | `first_at,last_at,equity_first,equity_last,balance_first,balance_last,equity_min,equity_max,balance_min,balance_max,dd_max,dd_max_at,hwm_last,kill_level,headroom,kill_pct`。kill_pct は現設定、他は期間が空または算出不能なら null。headroom = equity_last − kill_level。極値同値は最初の `(ts,id)` |
| 印 | `kind` (`latched/released/rebase/cashflow/unknown_hwm_drop`)、`plot_at` (図と期間所属の基準)、`occurred_at` (実時刻 / null)、`recorded_at` (記録時刻 / null)、`source_snapshot_ts,applied_at` (付け直し以外 null、付け直しは occurred_at = source_snapshot_ts、recorded_at = applied_at)、`generation` (非該当は null)、`time_source` (`state/ledger/event_payload/event_ts/snapshot`)、`time_basis` (`occurred/recorded`)、`source_id` (同時刻の順序用、64 byte 以下の文字列)。実時刻が不明なら recorded |
| 戦略行 | `origin` (256 UTF-8 byte 以下、合算行は other)、`origin_count` (合算した起動理由数、通常 1)、`closed_count,pnl_known_count,r_known_count,pnl_unknown_count,r_unknown_count,open_count,close_pending_count,wins,losses,flats` (件数)、`win_rate,pf,avg_r` (計算不能なら null、少数判定は web)、`total_pnl` (既知分の合計、既知 0 件なら null)、`close_reasons` (`llm_close/sl/tp/other` の全件数)、`cumulative` (下記)。r_unknown_count = closed_count − r_known_count |
| 累積点 | `{t,pnl,seg}` (時刻、口座通貨、非負整数の連続区間番号)。seg が違えば接続しない。t 昇順、同時刻合算、期間開始 0 と終了点を含め最大 120 点。開始ちょうどの決済は開始 0 点の後に置く。既知損益が無くても 0 の線と「損益不明」を併記 |

同時刻は id 順で並べる (累積点だけ同時刻の合算、開始 0 点を先に置く)。hwm_steps は描画開始の直前値を開始位置へ持ち越してよいが、欠落をまたがない。stats の初め・終わりは期間内最初・最後の有効行。pnl_unknown_count = closed_count − pnl_known_count、wins + losses + flats = pnl_known_count。閉じた取引以外の注文件数は読取時点の状態による。

構造・必須 key・enum・上限が壊れた応答は web で 502。nullable な未確定値は通常表示。個々の不正行は、snapshot なら除外して区間を切り、印なら除外、注文なら件数を残して該当指標を不明にし、quality と警告へ数える。非有限値・bool は数と認めず、不正 JSON・SL 欠落は R 不明。closed_at 欠落・不正なら期間に割り当てず unplaced_closed_count に数える。web が応答内の不正な数・時刻を検出した場合も同じ部分表示の規則とし、stats の当該値は不明、系列は接続を切る。marks_since が null の場合は「event の記録はありません」と表示する。集計値を図の点から再計算しない。

### 2.3 資金推移の間引き

- **先に区間を切る**: 全 snapshot を `(ts,id)` 順に読み、間引く前に `LAG(ts)` との実時刻差が **10 分超**なら新しい seg にする (鮮度の基準は `S/core/accounting.py:89-96`)。同じ束内でも分ける。不正行・未知 source を挟む場合も前後を必ず切り、補間しない。期間末尾から最後の有効記録まで 10 分超なら線を延ばさず、最終記録が古いと明記する。表示の基準であり、daemon の判定は変更しない。
- **任意期間の幅**: `ceil((to−from の秒数 + 1)/280)` 秒以上を基本幅とし、epoch 境界をまたぐ部分束も含めて 280 束以下になるまで増やす。空の束に点は作らない。
- **束を分割する位置**: seg の境、台帳の rebase_snapshot_id、非ゼロ cashflow、不明な HWM 下降。実時刻と id を保持し、同時刻の順序を失わない。
- **代表点**: 部分束の min/max と末尾値を返す。DD は各行で `hwm <= 0` なら 0、他は `max(0,(hwm−equity)/hwm×100)`。応答も百分率 (`S/core/accounting.py:44-52`)。全体最大 DD は全行から取る。
- **HWM・作動水準は階段線**: 通常の上昇も実際に変わった `(ts,id)` に段を置く。束頭へ末尾 HWM を移さない。`HWM × (1 − kill_pct / 100)` を現閾値で描き、凡例は「今の閾値」。hwm が非正なら DD は 0 と注記。過去閾値は復元しない。
- **点数の調整**: 分割後 320 代表点または 640 段を超えたら幅を倍にして再集約。最新点と段の点は必ず保持する。段だけで上限を超えるなど幅を増やしても収まらなければ、古い完全区間から図の対象を外し、最新区間は連続した末尾を採る。段だけ抜いて前後を接続しない。表示部分内の全段・最新点は残し、部分表示・実際の描画期間・省略数を出す。stats は全期間のまま。
- **帯は部分束の実際の時間幅**で描き、欠落・付け直しをまたがない。束内の equity と作動水準の厳密な交差は再現しない。図を判定の根拠にしない。
- **入出金**: 非ゼロ cashflow の行に印を置き、線は補正しない。台帳が無く cashflow でも説明できない HWM 下降は「不明な下降」の印と警告。本番の通常経路以外には任意 HWM を受ける store API と replay がある (`S/store/snapshots.py:9-16`、`S/backtest/runner.py:236-266`)。

### 2.4 印の出所

| 印 | 実時刻・記録時刻と位置 |
|---|---|
| 付け直し ▽ | source_snapshot_ts (= 元 snapshot の ts) に段と印を置き、期間への採否もこの時刻。表には操作時刻 applied_at を別列に出す (`S/ops/rebase_accounting.py:198-214`) |
| 作動 ✕・解除 ○ | 同じ世代・状態の候補を期間抽出より先に統合。作動実時刻は state の latched_at → 台帳の latched_at → event payload の実時刻の順。解除は実時刻を明記した payload がある場合だけ使う。event の ts は recorded_at |
| event のみ | occurred_at は null、plot_at = recorded_at。「作動 / 解除を記録した時刻」と明記。通知時刻を状態変化時刻と断定しない |
| 入出金 ◇・不明な下降 △ | snapshot の ts に置く。台帳なしの manual_ks_rebase も確認不能として警告 |

今の通知 payload は状態と世代だけで、event の ts は記録時の時計 (`S/ops/service.py:1987-1991`、`S/ops/events.py:137-148`)。state は解除時に latched_at を消す (`S/store/state.py:362-370`)。実時刻候補が矛盾すれば優先順位で選び警告。統合後の plot_at で期間抽出し、最新 40 を図、最新 20 を表へ。図と表の省略数は別々に示す。state の補完は毎回行い、cache した履歴のコピーへ合成する。

marks_since に加え「期間内にも履歴の欠けがあり得ます」を必ず出す。event の欠けは解除済みの根拠にならない。SQL は `S/ops/events.py` に置き、同じ接続・読取 transaction を使う。重複排除に必要な期間外候補も読む。監査行・自由形式 log は使わない。

### 2.5 戦略への帰属と対象

**帰属は建てた判断で決める**: `orders.intent_id → trade_intents.mission_id → missions.trigger`。今の決済 event の `origin_code` と同じ結合 (`S/ops/trade_event_sink.py:343-344`)。`intent_id` は決済で変わらない (F10) ので、後の定期の判断が決済しても帰属は動かない。

| `trigger` の値 | 行 |
|---|---|
| `signal:<plugin>` (形が `S/ops/trade_events.py:43` の文法に合う) | その戦略の行 |
| `cron` | 「定期の判断 (戦略に属さない)」の行。必ず出す |
| それ以外 (`intent_id` が NULL、結合できない、形が合わない) | 「帰属不明」の行。件数を必ず出し、黙って落とさない |

- 今は「帰属不明」は 0 件 (F11・F12)。schema は NULL を許すので、行の扱いは決めておく。
- 依頼の前提の訂正: `trigger` が NULL の約 600 件は improve と reflection の mission で、取引の帰属には関わらない (F11)。

**対象にする注文**:

| 注文 | 扱い |
|---|---|
| `closed` で `realized_pnl` がある | 集計の対象。期間は `closed_at` で決める |
| `closed` で `realized_pnl` が NULL (決済のレートが取れず未確定のまま残る経路がある。`S/core/executor.py:186-190`) | 「損益未確定」の件数だけ。勝敗・損益に入れない |
| `open` (または約定数量が正で未決済、closing / close_unknown を除く) | 「保有中」の件数だけ。損益に入れない |
| `closing`・`close_unknown` | 「決済未確定」の件数だけ (あれば注記) |
| 約定せずに終わった (`cancelled`・`expired`・`rejected`・`invalidated`) | 対象外。取引ではない |
| 部分約定で決済 | 対象。損益は `realized_pnl` のまま。今は 0 件で、paper は全量を約定させる (`executor.py:720-721`、`scheduler.py:1675-1676`) |

**決済の内訳**: `close_reason` を `llm_close` (判断)・`sl`・`tp`・その他 (日替わりの強制決済 `day_rollover` など) の 4 つに数える。知らない語は「その他」。

**版**: 同じ plugin 名で版 (`content_hash`) が変わっても同じ行に数える。版ごとの分割は入れない (今は版が混ざった戦略は無い。DB)。注記に書く (§1.2 の 4)。

### 2.6 指標の定義と、少ない件数の表示

**勝敗・PF は backtest に揃え、R は通貨条件を付ける**。

| 指標 | 定義・母数 |
|---|---|
| 決済件数 closed_count | 期間内決済の全件、損益未確定を含む |
| 損益既知 pnl_known_count | 有限の realized_pnl がある決済。勝 / 負 / 分は正 / 負 / 0 |
| 勝率 | 勝 ÷ pnl_known_count × 100 (%) |
| PF | 既知の利益合計 ÷ 損失絶対値合計。損失 0 または既知 0 件なら null |
| 平均 R | 有効 R の算術平均。母数は r_known_count、0 件なら null |
| 損益合計 | 既知の realized_pnl 合計、口座通貨。スワップは含めない |

- **R の単位**: realized_pnl は口座通貨 (`S/core/paper_broker.py:17-32`)、`abs(avg_fill_price−SL)×quantity×contract_size` は quote 通貨 (`S/backtest/metrics.py:89-97`)。**quote 通貨 = 口座通貨の注文だけ**両者を割り、他は R 不明。現設定は JPY 口座・USDJPY (`config/settings.yaml:4-6`) で一致する。backtest の avg_r にも異通貨の問題があり、指揮者が別チケットに起票する。共通関数化だけでは正しくならず、この段で backtest の式は変えない。
- 列名は「平均 R (手数料込み)」。通知の値幅 R と手数料などで異なる。異通貨なら換算の有無でも異なり、通知・backtest と無条件に一致するとはしない (§6 の問い 6)。
- 初期 SL は orders と intent payload の一致を確認。payload 欠落・不正 JSON・SL 欠落・不一致、SL = 約定価格、非有限値・bool、不正数量、未知 pair、通貨不一致は R 不明。分母・結果の非有限値も不明。R 不明でも既知損益・勝敗は残す。現行呼出しでは SL は後から変わらないが store API の強制条件ではない (F14)。
- **少数判定**: 勝率・PF は pnl_known_count、平均 R は r_known_count。各母数が 5 未満なら「–」、5〜29 は「(参考)」、30 以上は通常表示。平均の横に「R 算出 N / 決済 M 件」、損益既知・不明の件数も出す。しきい値は thresholds で返す (30 の根拠は `S/backtest/metrics.py:20,142`)。
- **行の選抜**: closed_count 降順、同数は origin 昇順。cron と帰属不明は常設し、50 行超なら残り枠から 1 行を「ほか N 件」に使う。選外の件数・既知損益・勝敗・保有・未確定・R の総和と母数を合算し、率は合算母数から出す。合計を失わない。期間不明決済は別枠。
- **累積損益**: `(closed_at,id)` 昇順、同時刻の既知損益を合算。期間開始の 0 点 (開始ちょうどの決済はその後に垂直移動)、各決済時刻、期間終了点を返し、水平→垂直の階段線にする。通常は束ねない。120 点超だけ期間から幅を求め、束の開始・終了・最小・最大を実時刻で保持。超過時は幅を倍にし再集約、最新点・期間両端は残す。収まらなければ部分表示と省略数を出し、欠落をつながない。終点は表の既知損益合計 (既知なしは 0 の線と不明表示) と一致する。
- 成績に正常・危険の色や状態の印は付けず、損益は符号で示す。

### 2.7 paper と live

- 今は区別する材料が無い (F6)。`account_snapshots.source` は行の種類 (`manual_ks_rebase`) も兼ね、`orders` には列が無い。
- **資金推移**: `source` が `paper` と `manual_ks_rebase` の行だけを描く。ほかの値の行があれば `other_sources` に数え、画面の上に「paper 以外の記録が N 行あります。この画面は paper だけを描いています」(注意の印) を出す。**混ぜて 1 本の線にしない**。
- **戦略別**: 分けられない。live を入れる設計で `orders` に区別の列を足し、この画面を分けることを、その設計の必須の項目として残す (§7)。
- 画面の h1 の下に、応答の `mode` (今は `paper`) を 1 行出す。

### 2.8 描画

- **route**: `/performance`。`ROUTE_TABLE` に 1 行、nav に「Performance」(`A/web/routes.py:20-30`、`A/web/render.py:86-96`)。
- **タブ**: `equity` (資金推移、既定)・`strategy` (戦略別)。`A/web/tabs.py` の表に足す。
- **query**: `tab` と `range` だけ。`range` は `7d`・`30d`・`90d`・`all` の完全一致。ほかの key・値は 400 (ほかの route と同じ規律、`A/web/request.py:283-349`)。期間の link は `range` を、帯は `tab` を付け替え、互いの値を引き継ぐ。
- **renderer**: 新しい module `A/web/perf_pages.py` (HTML) と `A/web/svg.py` (図)。`render.py` には足さない (1,334 行ある)。
- **SVG の形**:
  - `<svg viewBox="0 0 960 260" role="img" aria-label="…">`。幅は CSS で 100%。
  - 線は `<polyline>` (切れ目ごとに 1 本)、帯は `<polygon>`、軸と印は `<line>`、目盛りと凡例は `<text>`。座標は小数 1 桁。
  - 使わない要素: `<script>`・`<foreignObject>`・`<image>`・`<use>`・`<a>`・`<style>`・event の属性。test で固定する。
  - 文字 (凡例の起動理由など) は `html.escape` を通す。起動理由は文法に合うものだけを出す (§2.5)。
- **目盛り**: 横は表示 tz の日付 (4〜8 個)。同一年内は MM-DD、年をまたぐ期間は YYYY-MM-DD。図の横に tz と期間の両端を添える。縦は金額 (4〜5 本、丸めた値)。表の時刻は原則どおりの表記。
- **数値**: 金額は小数 2 桁、率は `12.3%` (`metric_words.PERCENT_FORMAT` と同じ)、R は符号つき小数 2 桁。この画面の表は数値の列を右に寄せる (`table.num`、cell に class は付けない)。ほかの画面の表は変えない。
- **再読込**: 30 秒の自動再読込はそのまま。操作の状態を持たない画面なので、読み直しで失うものは無い。

### 2.9 CSS と CSP

- **CSP は変えない**。inline の `<svg>` は文書の一部で、画像の読込み (`img-src`) にならない。`script-src` は足さない。
- **色は class と `STYLE` の規則で付ける**。`style=""` 属性は CSP で効かず、test も無いことを見る (`T/web/test_render.py:125`)。色の presentation 属性 (`stroke="#…"`) は CSP の対象外だが、暗い配色に追従しないので使わない。
- **系列色の token を出荷する**。値は CSS の段が決めた 4 色 (`2026-11-18-web-css-design.md` 付録 B、明・暗とも地と縞で 4.7 以上)。今の `STYLE` に系列色の token は無い (`render.py:33-39`)。
- **色だけで区別しない**: 系列は線種 (実線・破線・点線・鎖線) も変え、凡例に記号を添える。作動は危険色 +「✕」、解除は中立色 +「○」、付け直しは注意色 +「▽」、入出金は c2 +「◇」、不明な下降は注意色 +「△」。凡例にも同じ記号・色を載せる。cron は中立色の長破線 (10 4)。軸は mut、文字は fg とし、明暗両方で軸・印・線は背景に 3:1、文字は 4.5:1 以上を確認する。
- **`STYLE` に足す規則** (下書き。追加規則を含め実装時に UTF-8 で 4,096 byte 以下を確認):

```css
  --c1: #0969da; --c2: #bc4c00; --c3: #1a7f37; --c4: #8250df;   /* :root (明) に */
  --c1: #4493f8; --c2: #f0883e; --c3: #3fb950; --c4: #ab7df8;   /* 暗の :root に */
svg { display: block; width: 100%; max-width: 60rem; height: auto; }
svg * { fill: none; stroke: var(--mut); vector-effect: non-scaling-stroke; }
svg text { fill: var(--fg); stroke: none; font-size: 11px; }
.ax { stroke: var(--mut); }
.c1 { stroke: var(--c1); }
.c2 { stroke: var(--c2); stroke-dasharray: 6 3; }
.c3 { stroke: var(--c3); stroke-dasharray: 2 3; }
.c4 { stroke: var(--c4); stroke-dasharray: 8 3 2 3; }
.bd { fill: var(--c1); stroke: none; opacity: 0.15; }
.ev { stroke: var(--bad); stroke-dasharray: 3 3; }
.rb { stroke: var(--warn); stroke-dasharray: 3 3; }
.rl { stroke: var(--mut); stroke-dasharray: 2 3; }
.cf { stroke: var(--c2); stroke-dasharray: 6 3; }
.cr { stroke: var(--mut); stroke-dasharray: 10 4; }
.num td:not(:first-child), .num th:not(:first-child) { text-align: right; }
```

- `STYLE` は全ページが払う。承認詳細の「diff 以外 15 KiB」は骨格を数えるので、**追加 CSS の byte ぶん申告の切り詰めが早まる例がある** (CSS の段の §2.5 と同じ構図)。上限 4,096 は変えない。図の画面だけに 2 本目の `<style>` と hash を出す案は、hash の pin と「全ページの `<style>` が `STYLE` と一致」の test を 2 系統にするので採らない。
- 戦略別の図は系列 4 色 + 定期の判断 (中立の色)。戦略が 5 つ以上あれば、件数の多い 4 つだけを図に描き、残りは表にだけ出す (図の下に「ほか N 戦略は表のみ」)。
- **未確認**: inline SVG の中で class の `stroke: var(--token)` が明・暗とも効くこと、`vector-effect` の対応。実ブラウザの受入で確かめる (§4)。

### 2.10 予算

- **1 ページ 64 KiB は変えない** (`render.py:18`)。
- **資金推移の図**: 合成データで実測 (線 3 本 + 帯、座標は小数 1 桁)。

  | 列の数 | 線 3 本 + 帯 |
  |---|---|
  | 168 | 9,710 |
  | 240 | 13,724 |
  | 320 | 18,730 |
  | 480 | 27,577 |
  | 1,028 (全期間を 1 時間幅) | 59,708 |

  1 時間幅のまま全期間を出すと、図だけで 64 KiB に届く。列の上限を 280 にする理由: 960 px の図で 1 列が約 3.4 px で、これより細かくしても画面で見分けられない。

  280 列で、設計どおりの部品を足した実測 (別の合成データ):

  | 280 列の図の中身 | SVG の byte |
  |---|---|
  | 線 3 本 + 帯 + 作動の水準 + 軸と目盛り | 19,632 |
  | 同、線の切れ目 30 | 24,537 |
  | 同 + 印 20 (`<title>` つき) | 27,604 |
  | 同 + 印 40 | 31,140 |
  | 同 + 印 100 | 41,617 |

  印は 1 つ約 167 byte。印の表は 20 行で約 1.7 KiB、骨格 (`STYLE` を含む) は約 4.5 KiB。

**実行時の予算 (UTF-8 byte)**。上の実測は目安で、段・切れ目が増えた場合の保証ではない。

| 部品 | 資金推移 | 戦略別 |
|---|---|---|
| 骨格・CSS・nav・期間 | 6 KiB | 6 KiB |
| 固定文・現在状態・集計値・省略の注記 | 8 KiB | 4 KiB |
| 図の線・帯・軸・凡例 | 24 KiB | 10 KiB |
| 図の印 | 6 KiB | — |
| 印の表 / 戦略の表 | 4 KiB | 12 KiB |
| 合計上限 | **48 KiB** | **32 KiB** |

- escape 後の byte で計る。戦略表示名は 48 byte、title は 160 byte、数値は 24 文字までに短縮。時刻・固定文は短縮しない。大きな有限数は指数表記。元 origin は API に保持する。
- **縮退順**: 印の表の古い行 → 図の古い印 → 図の代表点を幅倍増で再集約 → 古い完全区間を図から省略 → 表の下位戦略を「ほか N 件」に合算 → 図を省略して説明文。各部品予算と全体上限を満たすまで行う。固定文・集計値・今の状態は最後まで残す。図・表それぞれに期間と省略数。`_page` の Output clipped (`A/web/render.py:197-202`) に頼らず、完成 byte 列で上限を確認する。
- **応答上限**: 320 代表点、640 段、40 印、50 戦略行、累積は各 120 点。超過の扱いは §2.3・§2.6。図は件数上位 4 戦略と cron の最大 5 系列、選外は表へ。omissions は行・系列・点を区別する。
- **整合性**: 1 本の接続で BEGIN → snapshot・台帳・event・注文の読取 → 読取 transaction 終了。共通の to を使い、別接続が更新しても同じ DB 視点で読む。state は別取得で state_fetched_at を表示し、DB との同時性を主張しない。
- **cache**: 重い履歴集計だけ期間ごとに 30 秒 (最大 4 件)。current は snapshot 鮮度も毎回取得し、state 補完印は表示時に合成。cache 検索・再確認・集計・格納を既存 lane 内に置く。`S/ops/service.py:778,838-855` の単一 lock が要求を直列にするので、待機後の再確認で同一期間の miss 重複を防げる。別 lock は不要。
- F2 は snapshot 集約だけの実測で endpoint 全体の保証ではない。T1・T2 で本番相当と 10 倍の行数、cold / warm cache の全経路 (event・台帳・帰属 JOIN・注文集計・整形) の時間を測り記録。注文は 1 日数件で数年でも全走査は小さく、この段で closed_at 索引は足さない。全期間が重くなれば日次集計を別途検討する。

## 3. 失敗時・運用

| 起きること | 画面 |
|---|---|
| daemon に届かない・timeout | ほかの画面と同じ 502 の error ページ (`request.py:237-246`) |
| 古い daemon (endpoint が無い) | 502。web と daemon は同じ版で動かす前提 (ほかの route と同じ) |
| 応答の形が違う (`points` が list でない等) | 502 (`_check_reply_shape` に route を足す) |
| 不正な値・時刻 (stats・印・戦略・系列も含む) | §2.2 の共通規則で部分表示と件数の注記。構造・契約違反は 502 |
| snapshot が 1 行も無い期間 | 図の代わりに「この期間の記録はありません」。印の表と注記は出す |
| 閉じた取引が 0 件 | 表の代わりに「この期間に決済した取引はありません」。保有中の件数は出す |
| kill switch の event が欠けている | 印が出ないだけ。marks_since と期間内欠落の固定文を出す |
| 印が 40 を超える | 最新 40 だけを描き、残りの件数を出す |
| `source` に知らない値 | 描かずに件数を警告 (§2.7) |
| 帰属できない注文 | 「帰属不明」の行に件数 |
| 表示予算を超える | §2.10 の順に縮退し、固定文・現在状態・集計値・省略数を保持 |
| `STYLE` と hash のずれ | CSS が全部外れ、図は色なし (線は既定の黒、塗りは黒) で崩れる。test (`test_render.py:114-125`) が止める。`STYLE` と hash 2 か所は同じ commit で変える |

- **反映**: daemon (endpoint) と web (画面) の両方を再起動する。daemon の再起動は取引を止めるので、時間は利用者が決める。
- **戻し方**: task ごとに 1 commit を revert する。DB・設定・state に変更は無い。
- **資金保護との関係**: この画面は読むだけで、図・集計の値は発注・kill switch の判定に使わない。

## 4. test と受入

### 今の test への影響

web の test は 725 件 (`T/web/`)。影響する pin を grep で数えた。

| 変更 | 書き換える test | 中身 |
|---|---|---|
| web の route を 1 本足す | `T/web/test_routes.py:7` | route 表の全数の一致。`ROUTE_TABLE`・`client_method` を参照する test は 14 行 / 7 file で、全数一致は `T/web/test_approval_change.py:415-421` にもある |
| nav に項目を足す | `T/web/test_render.py:135`、`T/web/test_tabs.py:192` (nav を取り出す 2 か所)、`T/web/test_simplified_views.py:224` | nav・route 表に触れる行は 22 行 / 9 file。nav の中身を見る箇所を T3 で確かめる |
| `STYLE` | `A/web/headers.py:7`、`T/web/test_render.py:75` | hash の値 (`STYLE` に触れる行は 18 行 / 3 file)。`STYLE_MAX_BYTES` (`:192`) は変えない |
| daemon の endpoint を 1 本足す | `tests/ops/test_ac01_02_43_api_routing.py:106`、`:313` (本数 35 が 2 か所と期待の表) | 36 に |
| 同 | `tests/ops/test_improve_repeat_api.py:38`、`tests/ops/test_ops_web_write_principal.py:56` | 本数 35 を 36 に |
| principal の許可 | `tests/ops/test_ops_web_write_principal.py:23-28` | READS の handler 集合に performance を追加 |
| 同 | `tests/ops/test_scope_enforcement.py:161` | 全 endpoint の呼出しの表に 1 件 |
| 運用の文書 | `docs/operations/ops-api-2026-10-09.md` の route の表、`2026-10-04-ops-api-design.md` §4.2 | endpoint を 1 行 |

変わらないことを確かめる pin (そのまま通る想定):

- `<script` が無いことを見る箇所 (38 行)。JS を入れないので全部そのまま。
- `T/web/test_runtime_isolated.py:422` は 5 つの route の本文に `<svg`・`<b>` などが無いことを見る (敵対的な入力が素通りしないことの検査)。対象の表 `READS` (`:173-177`) に新しい route は無いので通る。**新しい route を `READS` に足すときは、`<svg` の検査をこの route だけ「server が描いた 1 つだけ」に変える** (T3)。
- `" style="` が無い (`test_render.py:125`)。SVG に style 属性を出さない。
- SVG の `<title>` (印の吹き出し): `T/web/` にページの `<title>` を見る test は無い (grep で 0 行) ので、当たる pin は無い。
- tabs の表を見る箇所 (`TABS`・`tab_names` は 50 行 / 10 file)。表に 1 route を足すだけで、今の route の値は変えない。

### 足す test

1. **間引きと時刻** (daemon): epoch の束、任意の長さの期間、同時刻の id 順。束内後半の HWM 上昇でも実時刻で段になる。帯の開始・終了と代表値の実時刻を保持し、DD は各行から算出。
2. **付け直しと入出金** (daemon): 同時刻の元行と付け直し行、非ゼロ cashflow、台帳なしの下降、source と台帳の不一致。source_snapshot_ts と applied_at が期間境界の反対側にある fixture で、段・印が同じ期間に入る。表には操作時刻も出る。
3. **DD と作動水準** (daemon): 間引く前の最大 DD・時刻、hwm <= 0、equity > hwm、閾値ちょうど / 直前 / 直後。作動水準の 100 による換算と百分率が accounting と一致。
4. **欠落** (daemon): 同じ束内の 10 分超の空き、ちょうど 10 分、不正行・未知 source を挟む区間、末尾の陳腐化で線・帯がつながらない。
5. **印と現在状態** (daemon): event・台帳・state の統合を期間抽出より先に行う。通知の遅延、実時刻優先、event のみは「記録時刻」、同世代の重複、時刻の矛盾。cache 中に state が変わる、取得不能、snapshot が古い場合も current が更新される。
6. **帰属と対象** (daemon): 決済を別 mission が出した例、cron、signal、intent_id NULL、不正 trigger、全注文状態。期間内決済と保有・決済未確定の帰属不明を分ける。closed_at 欠損・不正は期間へ混ぜない。
7. **指標** (daemon): 同通貨だけ backtest と比較。異通貨、payload 欠落・不正 JSON、SL 欠落・不一致・ゼロリスク、非有限値・bool、不正数量、未知 pair。決済 30 件・有効 R 1 件でも平均 R は非表示。母数・不明件数、PF の損失 0 を確認。
8. **累積損益** (daemon / web): 同時刻決済の合算、期間開始・終了と境界ちょうどの決済、損失後の回復、束ねない少数決済。水平・垂直の階段線で、表の既知損益合計と図の終点が一致。再集約・部分表示後も一致。
9. **上限と整合性** (daemon): 基本 280 束、分割 320 点・640 段、印 40、戦略 50、累積 120。多数の段で倍幅再集約と部分表示、最新点・全表示段の保持。選外戦略の件数・損益を合算。読取中の別接続更新でも台帳・段・印・注文の視点が同じ。待機要求が同期間 cache を再確認して走査は 30 秒に 1 回。T1・T2 で本番相当と 10 倍の全経路を計測。
10. **認可と隔離** (daemon / web): scope、ops-web-write の許可 handler 集合、監査書込みなし。新 route の隔離実行、GET / HEAD (header 一致・HEAD 本文なし)、認証なし・誤認証・正常認証、敵対的入力を既存と同じ条件で確認。
11. **SVG** (web): 全要素・属性の allowlist。style 属性・script・event 属性・色の直書きなし。凡例・title・表示名にタグ、引用符、閉じタグを含む入力を渡し、文字が SVG 要素や属性にならないことを構文解析して確認。「SVG が 1 つ」だけで済ませない。
12. **byte 予算** (web): 最大長の UTF-8 文字列、巨大な有限数、全分割・全切れ目・最大印・最大戦略行で、48 / 32 KiB と部品予算を確認。縮退順・合算・図と表の別省略数・表示期間、固定文・集計値・現在状態が残り Output clipped にならないこと。
13. **表示と不正値** (web): stats・印・戦略・系列を含む不正値と null、502 / 部分表示 / 通常の不明表示の境界。率ごとの母数と「–」「(参考)」、損益未確定・R 不明・帰属不明、空期間。§1.1 の固定文と現在状態、§1.2 の注記を必ず出す。
14. **query と時刻** (web): tab・range の完全一致と不正入力 400、相互の値を保つ link。JST の日付境界・年またぎ・夏時間、印の実時刻 / 記録時刻と操作時刻の区別。
15. **CSS** (web): STYLE 4,096 byte 以下と hash。明暗両方の全系列・印・cron・軸は背景 / 縞に 3:1 以上、文字は 4.5:1 以上。記号・色・線種・凡例が仕様どおり。

判定は `tmp/tools/judge.sh` 経由。`T/web/`・`tests/ops/` に加えて、endpoint の本数を見る suite と `tests/test_cli_surface.py` を含める。

### 実ブラウザの受入 (利用者)

配色・CSP・SVG の描画は test で見られない。利用者が実ブラウザで、**明・暗の両方**で確かめる。

1. 資金推移: 4 つの期間。線 3 本と帯・作動の水準・印が見分けられる (色と線種の両方)。付け直しの段。週末の切れ目。
2. 戦略別: 表の「–」と「(参考)」、固定文、累積損益の図と凡例。
3. 印に pointer を置くと時刻が出る。
4. スマホ幅 (LAN 越しを使っている場合): 図が幅に収まり、表は節の中で横にスクロールする。
5. ブラウザの console に CSP の違反が出ない。
6. ほかの画面 (Status・承認詳細) の見た目が変わっていない。承認詳細の申告が前より早く切れていないか、手元の承認 1 件で見る。
7. 作動中・live の記録が混ざる・帰属不明がある、の見た目は本番で起こさない。fixture を実際の HTTP route で配信し、本番と同じ CSP 等の header を付けて見る。

## 5. task 表

各 task は単独で merge できる。T1・T2 は daemon だけ、T3 以降は web。`STYLE` を触るのは T3 だけ。

| task | 内容 | 触る file | 単独で merge したとき |
|---|---|---|---|
| T1 | `GET /v1/performance` の資金推移の部 (束ね・付け直し・最大 DD・印・`other_sources`・cache・上限)。`strategies` は空で返す。endpoint の本数・許可 handler 集合 (`test_improve_repeat_api.py`、`test_ops_web_write_principal.py` を含む)、transaction・cache の直列性、全経路の計測、運用の文書 | `S/ops/performance_view.py` (新)、`S/ops/service.py`、`S/ops/api_server.py`、`S/ops/events.py`、`tests/ops/` | API が増えるだけ。画面は変わらない |
| T2 | 戦略別の集計 (帰属・対象・指標・R の通貨条件・しきい値・階段線)、注文込みの全経路の計測 | `S/ops/performance_view.py`、`tests/ops/` | 応答に `strategies` が入る。画面は変わらない |
| T3 | web の route・nav・タブ・client、SVG の部品と `STYLE`、資金推移のタブ。hash の 2 行。test_approval_change.py の route 全数、隔離・HEAD・認証・敵対的入力の検査 | `A/web/routes.py`、`tabs.py`、`request.py`、`render.py`、`headers.py`、`perf_pages.py` (新)、`svg.py` (新)、`A/api_client.py`、`T/web/` | Performance が開き、資金推移が見える。戦略別のタブは「準備中」を出さず、タブ自体をまだ足さない |
| T4 | 戦略別のタブ (表・累積損益の図・固定文) | `A/web/perf_pages.py`、`tabs.py`、`T/web/` | 戦略別が見える |
| T5 | 実ブラウザの受入 (§4)。fixture を header つき HTTP で配信する手順を添える | — | — |

- T1 と T2 は直列 (同じ module)。T3 は T1 の後、T4 は T2・T3 の後。
- 実装は、T1・T2 が純 Python の集計、T3・T4 は `T/web/` を踏む。

## 6. 利用者に問う事項

| # | 問い | 推奨 | 理由 |
|---|---|---|---|
| 1 | この段の範囲。(a) 資金推移と戦略別の成績だけ。(b) 加えて「判断による決済と、そのまま SL / TP まで持った場合の R の比較」。(c) 加えて価格チャートへの指標・シグナル・建玉の重ね | (a)。(b) と (c) は別の段 (別チケット) | (b) は OHLCV を再生する反実仮想で、資金推移・集計とは別の仕組み (約定の規則の再利用、同じ足で SL と TP の両方に届いたときの扱い、価格の欠け、決済後どこまで追うか) を決める必要がある。対象 (判断による決済) は今 20 件に満たず、結論を出すには少ない。(c) は拡大・移動が要るので JS を入れる段になり、CSP と配布の設計が別に要る。(a) だけなら daemon 1 本 + 画面 1 つで閉じる |
| 2 | 少ない件数の表示のしきい値。各指標の有効母数 5 件未満は率を出さない、30 件未満は「(参考)」 | このとおり | 30 は backtest が「評価できる」とする件数と同じで、改善・承認の画面と基準が揃う。5 件未満の勝率・PF は 1 件で大きく動き、読む人を誤らせる。今は大半の起動理由が 10 件未満。下の値 (5) は根拠が弱いので、変えたければここで決める |
| 3 | 省略する量: 図の印 40、表の印 20、戦略の表は合算行込み 50 行、図は上位 4 戦略 + cron でよいか | この上限を推奨。最新の印を優先し、選外戦略は「ほか N 件」に合算 | 読める密度と byte 予算を保ち、省略数・対象期間と全件の集計値を併記できる。より多く必要なら予算と構成を見直す |
| 4 | 画面の構成。(a) 1 つの画面に 2 つのタブ (資金推移・戦略別)。(b) 1 つの画面に縦に並べる | (a) | 要望は「資金推移のページに戦略ごとの成績」。同じ画面・同じ期間の選択で 2 つを切り替えられるので要望に合う。縦に並べると図 2 つと表で 1 ページの予算 (64 KiB) の半分を超え、戦略が増えたときに切り詰めが要る。配置の段の「大項目はタブ」とも揃う |
| 5 | 作動の水準の線 (HWM × (1 − kill_pct / 100)) を出すか | 出す | DD と閾値は status / web で見せる、という前の裁定に沿う。equity との距離が図で分かる。注意は 2 つ: 過去に閾値を変えた分は再現できない (凡例に「今の閾値」と書く)。線を割っても、建てる判断が出るまでラッチしない (F21) ので、線と「✕」の位置はずれる (固定文で説明する) |
| 6 | 平均 R を (a) 金額 (手数料込み) と (b) 値幅 (通知の pnl_r) のどちらで出すか | (a)。quote 通貨 = 口座通貨の注文だけ計算し、他は不明 | 実現損益との対応が読みやすい。現設定の USDJPY / JPY なら backtest の金額式と同じ単位。異通貨は分母が quote 通貨、損益が口座通貨でそのまま割れない。backtest の avg_r も別途対応が必要。通知との差を手数料だけとは説明しない |

技術で決めたもの (問いにしない): server SVG (§2.1)、daemon 側の集計と `status.read` (§2.2)、束ねの方式 (§2.3)、帰属は建てた判断 (§2.5)、初期 SL の扱い (§2.6)、`STYLE` を 1 本のままにする (§2.9)、年をまたぐ日付目盛り (§2.8)、DD と閾値の百分率・100 による換算 (§2.3)。

## 7. 残余・やらないこと

- **反実仮想の比較・価格チャートの重ね**: この段に入れない (§6 の問い 1)。裁定が (a) なら、チケットを 2 つに分けて起票する。
- **JS**: 入れない。値の読取り (hover)・拡大は無い。
- **kill switch の履歴の欠け**: 10 月上旬より前と、event が欠けた世代は台帳・現在 state の実時刻で補える範囲だけ印を出す。材料のない履歴は復元しない。確かな履歴が要るなら、ラッチと解除を台帳に書く変更が別に要る。
- **過去の閾値**: 残っていないので、作動の水準は今の閾値で引く。
- **paper / live の分離**: 戦略別は分けられない (§2.7)。live を入れる設計の必須の項目にする。
- **版ごとの成績**: 同じ名前の plugin は版をまたいで 1 行。
- **日次の集計表**: 作らない。全期間の走査が重くなったら別チケット (§2.10)。
- **CLI (`afx ctl`) と bot**: この集計を出さない。
- **スワップ**: 損益の合計に含めない (F17。今は全件 0)。
- **R の 2 つの定義の統一**: しない。通知の `pnl_r` は値幅のまま。この画面は通貨が一致する注文だけ金額の定義で計算し、列名に書く。異通貨の backtest avg_r は指揮者が別チケットに起票する。
- **ほかの画面の桁揃え・右寄せ**: しない。この画面の表だけ。
- **SVG の `forced-colors`・印刷**: 対応しない。
- **承認詳細の予算**: `STYLE` の増分で申告の切り詰めが早まる例がある。予算の見直しは `web-approval-detail-budget-review` で扱う。

## 付録 A. 依頼の前提と確かめた結果

| # | 依頼の前提 | 結果 |
|---|---|---|
| 1 | snapshot は約 6 万行で、そのままでは応答の上限を超える | そのとおり (F1)。1 時間幅でも全期間は図だけで約 60 KiB (§2.10)。基本 280 束以下にし、分割後は上限内へ再集約または部分表示。HWM の全変化時刻と欠落の境を守る (§2.3) |
| 2 | ラッチの履歴が DB にあるか | 一部だけ。今の状態は state file、履歴は `ops_events` (best effort、10 月上旬以降)、付け直しは台帳 (F7〜F9)。ラッチは線を割った時点でなく、次に建てる判断の時点 (F21) |
| 3 | 閉じた取引は約 20 件 | そのとおり。起動理由は数種で、大半が 10 件未満 (F12・F13) |
| 4 | `intent_id` が NULL の注文・部分約定・取消の扱い | 今は 0 件。扱いは §2.5 に決めた |
| 4 | `trigger` が NULL の mission 約 600 件の扱い | improve と reflection の mission で、取引の帰属に関わらない (F11) |
| 5 | `orders.stop_loss` は後で動く可能性がある | 今は動かす経路が無い。payload と全件一致 (F14)。食い違えば R を不明にする守りを入れる (§2.6) |
| 6 | 反実仮想は重い | そのとおり。別の段を推奨 (§6 の問い 1) |
| 7 | 新しい読取 endpoint が要るか | 要る (1 本)。集計は daemon 側、30 秒の cache (§2.2、§2.10) |
| 8 | paper と live が混ざるとき | 今は live の記録を書く経路が無く、`orders` には区別の列も無い (F6)。資金推移は混ぜずに警告、戦略別は残余 (§2.7) |

## 付録 B. 関連文書の位置づけ

| 分類 | 文書・前提 | 現在の扱いと根拠 |
|---|---|---|
| 記述が古い | `docs/operations/ops-api-2026-10-09.md:60` の web 書込み配線は未提供 | 現在は write 設定で WebWriteClient を使う (`A/web/runtime.py:86`、`A/isolation/startup.py:884`) |
| 記述が古い | `2026-10-04-ops-api-design.md` §4.1 の外部主体は ops-bot のみ | 現在は ops-web-write も許す (`S/ops/contracts.py:126-129`) |
| 記述が古い | `2026-11-18-web-css-design.md` §2.2・§2.4・付録 D の b と `.kpi b`、STYLE 2,957 byte | 現在は `.kpi strong` (`A/web/render.py:72`)、STYLE は 2,962 byte |
| 範囲が違う | チケットの「当面、読取専用・JS なし」 | 過去時点・対象機能の方針。現在は web 全体に書込みがある (`A/web/write_pages.py`、`A/web/runtime.py:86`) が、この画面は読取専用・JS なしで矛盾しない |
| 範囲が違う | NULL trigger の mission 約 600 件 | 現 DB 分布は improve / reflection。取引とは別 (`S/store/db.py:666`、F11)。将来の NULL 注文帰属は §2.5 で扱う |
| 範囲が違う | stop_loss は後で動く可能性 | 将来可能性であり、現行呼出しでは変更しない (`S/core/executor.py:687,1212`、F14)。store API の不変条件ではないので照合を残す |
| 未決定 | チケットの「平均 R」 | 金額 / 値幅が未指定。コードには両方ある (`S/ops/trade_events.py:214-222`、`S/backtest/metrics.py:89-97`)。§6 の問い 6 に推奨と通貨条件を示す |

## 変更履歴

| 日付 | 版 | 変更 | 理由 | commit |
|---|---|---|---|---|
| 2026-11-19 | C0 | 初版 (下書き) | 利用者の方針 (配置 → CSS → チャート。資金推移のページに戦略ごとの成績) | — |
| 2026-11-19 | C0.1 | 単位・時刻・集約・現在状態・R・応答契約・予算・検証・問い・関連文書の分類を改訂 | 設計レビュー 1 周目の指摘を反映 | |
