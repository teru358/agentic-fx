# 同梱 web のチャートの段: 資金推移と戦略ごとの成績 (web-ui-charts-equity-indicators) 設計 C0.2

**チケット:** `web-ui-charts-equity-indicators` (低、機能)。関連: `web-layout-tabs` (配置の段、完了)、`web-css-design` (CSS の段、完了)
**状態:** C1 (2026-11-19 利用者承認)。§6 の問いは 6 件とも推奨どおりに裁定

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
| F7 | kill switch の**今の状態**は state file と解除途中の marker から読む。`load()` は marker が残る間ラッチ中として返す。履歴は持たない | `S/store/state.py:80-82`、`:259-267`、`:359-370` |
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
   - **HWM**: 点線の階段線。上昇は束ごとに安全側へまとめ、下降は正確な位置に残す (§2.3)。
   - **作動の水準**: HWM × (1 − kill_pct / 100)。細い階段線。equity がここに届くと予約の約定が止まり、次に建てる判断が出たときに kill switch が作動する (F21)。**線を割っても、建てる判断が出る前に戻れば「✕」は付かない**。
   - **印** (縦線と記号): kill switch の作動「✕」、解除「↑」、HWM の付け直し「▽」、入出金「◇」、不明な HWM 下降「△」。
   - 記録の無い時間 (週末・停止) は線を切る。つないで埋めない。
3. **今の状態** (図の上の別枠、毎回取得): Status と同じ「作動中 / 照合待ち / 解除 / 不明」の語と印、世代、状態取得時刻を出す。別に最新 snapshot の時刻・経過秒・鮮度、DD、作動水準、余裕を表示する。10 分超は「古い記録」。ラッチの有無と別に「新規の約定は停止中 (古い記録 / DD 到達 / 不明)」を出す。取得不能は「不明」とし、安全を示す表示にしない。
4. **数字で読む** (図の下の表): 期間の初めと終わりの equity・balance、最大・最小、**最大 DD (率と時刻)**。間引く前の全行から計算する。
5. **印の表**: 図と同じ最新 20 件。作動・解除は実時刻 / 記録時刻と出所、付け直しは「帳簿の位置」と「操作時刻」の 2 列。位置の新しい順、時刻は `YYYY-MM-DD HH:MM:SS +09:00`。
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
   - 指標の対象は期間内に**決済した**取引。保有中・決済未確定だけの起動理由も件数の行を出す。期間の link は資金推移と同じ。
3. **累積損益の図**: 起動理由ごとの線 (決済のたびに段が動く)。期間の初めを 0 とし、最新 120 決済より前は累計始点 1 点にまとめる (§2.6)。線は色と線種で区別し、凡例に記号を添える。
4. **注記** (固定文): 「累積損益の合計は equity の変化と一致しません (含み損益、期間の外で決済した取引、スワップを含まないため)」「同じ名前の plugin の版が変わっても、同じ行に数えます」。

### 1.3 変わらないもの

ほかの画面の表示・URL・タブ、書込みの画面の規律、時刻の規則、CSP の script を許さない形、daemon の発注・資金保護。Status の概要のカード (`equity`・`balance`) はそのまま。

## 2. 仕組み

### 2.1 server SVG にする (JS は入れない)

server が SVG を描く。期間は link で切り替え、JS・配信 route・CSP の script 許可は足さない。値は図下の表と印の `<title>` で読む。拡大・移動・重ねが必要な価格チャートは別の段 (§7)。

### 2.2 データの経路

```
web (afx-ops) ──GET /v1/performance?range=30d──▶ daemon (ops API、status.read)
                                                   └ S/ops/performance_view.py が DB を読んで束ねる
```

- **新しい読取 endpoint が 1 本要る**。今の `GET /v1/status` は今の値だけ、`GET /v1/events` は 10 月以降の event だけで、snapshot の列も注文の集計も返さない。
- **集計は daemon 側**。web は DB を持たず、6 万行を渡すと応答が数 MB になる。daemon は束ねた代表点 (基本 280 束、分割後も最大 280 点) と集計済みの行だけを返す。
- **scope は `status.read`**。口座の値は今も `status` で同じ principal に見せている (F20)。新しい scope は作らない。`ops-notify` (`events.read` だけ) には見えない。
- **読取専用**。監査行 (`ops_requests`) は書かない (ほかの GET と同じ)。DB への書込みは無い。
- **CLI は足さない**。daemon の生きた状態を読むので、置くなら `afx ctl` の「稼働観測・実行追跡」だが、この段は web だけにする (§7)。`cli_surface` の検査は CLI の記録から endpoint を引く向きなので、記録の無い endpoint は通る (`tests/test_cli_surface.py:183-195`)。逆向きの検査が無いことは T1 で確かめる。
- web 側: `WebClient.performance(range=)` を足す (`A/api_client.py:535-573`)。`WebWriteClient` は継承するので、書込みを有効にした web でも同じ。

応答の契約 (`data`)。全 key は必須 (marker の補助値を除く)。時刻は offset つき UTC ISO 8601、件数は非負整数 (bool 不可)、金額は口座通貨、DD・閾値は百分率、勝率は比率、R・PF は有限の無次元数。null は下表のときだけ許す。共通期間は `[from,to]` (両端を含む)、to は読取開始時刻。7d / 30d / 90d の from は to から各日数を引く。all の from は最古の有効 snapshot / 決済 / 印の位置 (全て空なら to)。保有中・決済未確定・現在状態は期間に依存しない。

| key | 型・意味・上限 |
|---|---|
| `range`, `from`, `to`, `bucket_sec` | `7d/30d/90d/all`、期間の端、正の整数秒 (最終束幅) |
| `account_currency`, `display_timezone` | 現設定の文字列、各 16 / 64 UTF-8 byte 以下 |
| `current` | 下表。毎回取得 |
| `points` | 代表点、末尾の `(ts,id)` 昇順、最大 280、連続区間は最大 32 |
| `hwm_steps` | `{t,hwm,seg}`。束内上昇をまとめた変化点と各区間の最初・最後。`(t,id)` 順で最大 320。作動水準は hwm から算出 |
| `stats` | 全有効 snapshot の集計。図の省略に依存しない |
| `marks`, `marks_omitted` | 同世代・状態を統合し位置の降順で最新 20 件、残りの件数。図と表は同じ集合 |
| `marks_since`, `other_sources` | 最古の kill switch event 記録時刻 (無しは null)、期間内の除外 source 行数 |
| `strategies` | 下表の行、最大 32 (cron・帰属不明・合算行を含む)。順序は §2.6 |
| `thresholds` | `{min_count:5,reference_count:30}` |
| `omissions` | `{equity_points,hwm_steps,strategy_rows,strategy_series}`。図から省いた代表点・段、合算した元行、図に出さない系列の数。元 snapshot の集約数とは別 |
| `quality` | `{partial,invalid_snapshots,invalid_marks,invalid_orders,unknown_hwm_drops,unplaced_closed_count}`。partial は bool、他は件数。正常なら false / 0 |

| 入れ子 | 全 key と意味 |
|---|---|
| `current` | `kill_switch` (下記の status と同形 / 取得不能は null)、`state_fetched_at`、`snapshot_at` (最新行時刻 / null)、`snapshot_fetched_at`、`snapshot_age_sec` (非負秒 / null)、`snapshot_freshness` (`fresh/stale/unknown`)、`dd_pct,kill_level,headroom` (算出不能は null)、`kill_pct` (現閾値)、`fill_hold` (`kill_switch/stale/drawdown/none/unknown`、daemon が約定を止める理由の総合) |
| 代表点 | `start_at,end_at,last_at` (部分束の実時刻)、`equity_min,equity_max,equity_last,balance_last`、`seg` (非負整数)。null 無し。帯は `[start_at,end_at]`、代表線は last_at に置く |
| `stats` | `first_at,last_at,equity_first,equity_last,balance_first,balance_last,equity_min,equity_max,balance_min,balance_max,dd_max,dd_max_at`。期間が空・算出不能は null。極値同値は最初の `(ts,id)` |
| 印 | `kind` (`latched/released/rebase/cashflow/unknown_hwm_drop`)、`plot_at`、`occurred_at,recorded_at` (実時刻 / 記録時刻、不明は null)、`source_snapshot_ts,applied_at` (付け直し専用、他は null)、`generation` (非該当は null)、`time_source` (`state/ledger/event_ts/snapshot`)。付け直しの occurred_at / recorded_at は共に null |
| 戦略行 | `origin` (文法に合う最大 71 ASCII byte、帰属不明は unknown、合算は other)、`origin_count` (通常 1)、`closed_count,pnl_known_count,r_known_count,pnl_unknown_count,r_unknown_count,open_count,close_pending_count,wins,losses,flats`、`win_rate,pf,avg_r` (計算不能は null)、`total_pnl` (既知 0 件なら null)、`close_reasons` (`llm_close/sl/tp/other`)、`cumulative` (図の選外は空 list) |
| 累積点 | `{t,pnl}`。系列ごとに最新 120 件の決済を使い、同時刻合算、t 昇順。以前の合計を持つ始点 1 点と期間終了点を加え最大 122 点。切捨て前の開始は from / 0、切捨て時は採用した最初の決済時刻 / 直前の累計を始点とする。同時刻は始点を先に置く |

現在状態は status の読取部分 (`S/ops/service.py:1011-1024`) を関数に切り出して共用する。`kill_switch` は `latched,generation,latched_at,reconcile_required` と、marker がある場合の `requested_generation,started_at,file_latched`。web は `marks.classify_kill_switch` (`A/web/marks.py:83-95`) を使う。marker 投影で latched_at が無ければ作動印を合成しない。

現在の口座は `snapshots.latest()` (`S/store/snapshots.py:20-24`) を source で絞らず読む。daemon と同じ `drawdown_pct`・`drawdown_kill_reached` (`S/core/accounting.py:44-52`) を使い、kill_level = HWM × (1 − kill_pct / 100)、headroom = equity − kill_level。10 分超は stale、欠損・不正値・非正 equity は unknown、他は閾値到達なら drawdown、それ以外 none (`S/core/scheduler.py:291-301`)。停止判定に余裕の表示値や符号を使わない。`fill_hold` は daemon の約定停止の総合で、優先順は (1) kill switch がラッチ中、または解除途中の marker が残る (`kill_switch`)、(2) state を読めない・形が違う (`unknown`)、(3) 上の口座の理由 (`stale/drawdown/unknown`)。`none` は、約定を止める理由が一つも無いときだけ (`S/core/scheduler.py` の `_fills_allowed_by_state` と tick の account / DD の判定と同じ条件)。state を読めないときに `none` へ倒さない。`kill_switch` の表示も併せて出し、ラッチは別に示す。

同時刻の順序は daemon が id で決める。pnl_unknown_count = closed_count − pnl_known_count、r_unknown_count = closed_count − r_known_count、wins + losses + flats = pnl_known_count。
不正行の検証は daemon が担当する。snapshot は除外して区間を切り、印は除外、注文は件数を残して該当指標を不明にし quality へ数える。非有限値・bool、不正 JSON・SL 欠落は指標不明、closed_at 欠落・不正は期間へ割り当てず unplaced_closed_count へ。
web は既存の `_check_reply_shape` (`A/web/request.py:206-234`) と同じ上位の型・形だけを検査し、違えば 502。個々の表示値の異常は「不明」。再集約・合算・行検証は繰り返さず、集計値を図から再計算しない。marks_since が null なら「event の記録はありません」。

### 2.3 資金推移の間引き

- **先に区間を切る**: 全 snapshot を `(ts,id)` 順に読み、間引く前に `LAG(ts)` との実時刻差が **10 分超**なら新しい seg にする (鮮度の基準は `S/core/accounting.py:89-96`)。同じ束内でも分ける。不正行・未知 source を挟む場合も前後を必ず切り、補間しない。期間末尾から最後の有効記録まで 10 分超なら線を延ばさず、最終記録が古いと明記する。表示の基準であり、daemon の判定は変更しない。
- **任意期間の幅**: `w = max(1, ceil((to − from) / 279))` 秒 (差は秒数)。epoch に揃え、部分束を含めて 280 束以下。空の束に点は作らない。全期間では to の進行により束の境界が動くことを許容する。
- **束を分割する位置**: seg の境、台帳の rebase_snapshot_id、非ゼロ cashflow、不明な HWM 下降。実時刻と id を保持し、同時刻の順序を失わない。
- **代表点**: 部分束の min/max と末尾値を返す。DD は各行で `hwm <= 0` なら 0、他は `max(0,(hwm−equity)/hwm×100)`。応答も百分率 (`S/core/accounting.py:44-52`)。全体最大 DD は全行から取る。
- **HWM・作動水準は階段線**: 部分束内の上昇は最初に変わった `(ts,id)` に末尾 HWM を置く。実際より早く高く、余裕を小さく描く安全側の要約。下降は束を割り正確な位置を残す。各区間の始点・終点も返し、欠落をまたがない。作動水準は `HWM × (1 − kill_pct / 100)`、凡例は「今の閾値」。hwm が非正なら DD は 0。過去閾値は復元しない。
- **上限**: daemon だけが幅を倍にして再集約し、代表点 280・段 320・区間 32 以下にする。下降や切れ目だけで収まらなければ古い完全区間から図を省き、最新区間は連続した末尾を採る。表示部分の下降を抜いて接続しない。実際の描画期間・省略数を表示し、stats は全期間のまま。
- **帯は部分束の実際の時間幅**で描き、欠落・付け直しをまたがない。束内の equity と作動水準の厳密な交差は再現しない。図を判定の根拠にしない。
- **入出金**: 非ゼロ cashflow の行に印を置き、線は補正しない。台帳が無く cashflow でも説明できない HWM 下降は「不明な下降」の印と警告。本番の通常経路以外には任意 HWM を受ける store API と replay がある (`S/store/snapshots.py:9-16`、`S/backtest/runner.py:236-266`)。

### 2.4 印の出所

| 印 | 実時刻・記録時刻と位置 |
|---|---|
| 付け直し ▽ | source_snapshot_ts (= 元 snapshot の ts) に段と印を置き、期間への採否もこの時刻。表には操作時刻 applied_at を別列に出す (`S/ops/rebase_accounting.py:198-214`) |
| 作動 ✕・解除 ↑ | 同じ世代・状態の候補を期間抽出より先に統合。作動実時刻は state の latched_at → 台帳の latched_at の順。解除は event の記録時刻だけ。event の ts は recorded_at |
| event のみ | occurred_at は null、plot_at = recorded_at。「作動 / 解除を記録した時刻」と明記。通知時刻を状態変化時刻と断定しない |
| 入出金 ◇・不明な下降 △ | snapshot の ts に置く。台帳なしの manual_ks_rebase も確認不能として警告 |

今の通知 payload は状態と世代だけで、event の ts は記録時の時計 (`S/ops/service.py:1987-1991`、`S/ops/events.py:137-148`)。state は解除時に latched_at を消す (`S/store/state.py:362-370`)。実時刻候補が矛盾すれば優先順位で選び警告。統合後の plot_at で期間抽出し、最新 20 を図と表へ同じ順序で渡し、省略数も 1 つにする。state の補完も取得ごとに統合する。位置・種別が同じなら daemon の出所 id で順序を固定する。

marks_since に加え「期間内にも履歴の欠けがあり得ます」を必ず出す。event の欠けは解除済みの根拠にならない。SQL は `S/ops/events.py` に置き、同じ接続・読取 transaction を使う。重複排除に必要な期間外候補も読む。監査行・自由形式 log は使わない。

### 2.5 戦略への帰属と対象

**帰属は建てた判断で決める**: `orders.intent_id → trade_intents.mission_id → missions.trigger`。今の決済 event の `origin_code` と同じ結合 (`S/ops/trade_event_sink.py:343-344`)。`intent_id` は決済で変わらない (F10) ので、後の定期の判断が決済しても帰属は動かない。

| `trigger` の値 | 行 |
|---|---|
| `signal:<plugin>` (形が `S/ops/trade_events.py:43` の文法に合う) | その戦略の行 |
| `cron` | 「定期の判断 (戦略に属さない)」の行 |
| それ以外 (`intent_id` が NULL、結合できない、形が合わない) | 「帰属不明」の行。件数を必ず出し、黙って落とさない |

- 行は「期間内に決済がある、または今保有中・決済未確定がある起動理由」。帰属不明もこの 1 行に決済と保有・未確定の件数を出す。
- 今は「帰属不明」は 0 件 (F11・F12)。schema は NULL を許すので、行の扱いは決めておく。
- 依頼の前提の訂正: `trigger` が NULL の約 600 件は improve と reflection の mission で、取引の帰属には関わらない (F11)。

**対象にする注文**:

| 注文 | 扱い |
|---|---|
| `closed` で `realized_pnl` がある | 集計の対象。期間は `closed_at` で決める |
| `closed` で `realized_pnl` が NULL (決済のレートが取れず未確定のまま残る経路がある。`S/core/executor.py:451-453`、`:955-961`) | 「損益未確定」の件数だけ。勝敗・損益に入れない |
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
| 勝率 | 勝 ÷ pnl_known_count (比率、backtest と同じ) |
| PF | 既知の利益合計 ÷ 損失絶対値合計。損失 0 または既知 0 件なら null |
| 平均 R | 有効 R の算術平均。母数は r_known_count、0 件なら null |
| 損益合計 | 既知の realized_pnl 合計、口座通貨。スワップは含めない |

- **R の単位**: realized_pnl は口座通貨 (`S/core/paper_broker.py:17-32`)、`abs(avg_fill_price−SL)×quantity×contract_size` は quote 通貨 (`S/backtest/metrics.py:89-97`)。**quote 通貨 = 口座通貨の注文だけ**両者を割り、他は R 不明。example の既定は JPY・USDJPY (`config/settings.yaml.example:4-6`) で一致する。backtest の異通貨 avg_r は `backtest-avg-r-currency-mismatch` に起票済み。共通関数化だけでは正しくならず、この段で backtest の式は変えない。
- 列名は「平均 R (手数料込み)」。通知の値幅 R と手数料などで異なる。異通貨なら換算の有無でも異なり、通知・backtest と無条件に一致するとはしない (§6 の問い 6)。
- 初期 SL は orders と intent payload の一致を確認。payload 欠落・不正 JSON・SL 欠落・不一致、SL = 約定価格、非有限値・bool、不正数量、未知 pair、通貨不一致は R 不明。分母・結果の非有限値も不明。R 不明でも既知損益・勝敗は残す。現行呼出しでは SL は後から変わらないが store API の強制条件ではない (F14)。
- **少数判定**: 勝率・PF は pnl_known_count、平均 R は r_known_count。各母数が 5 未満なら「–」、5〜29 は「(参考)」、30 以上は通常表示。平均の横に「R 算出 N / 決済 M 件」、損益既知・不明の件数も出す。しきい値は thresholds で返す (30 の根拠は `S/backtest/metrics.py:20,142`)。
- **行の選抜**: closed_count 降順、同数は origin 昇順。対象の cron と帰属不明を保持し、32 行超なら残り枠から 1 行を「ほか N 件」に使う。daemon が選外の件数・既知損益・勝敗・保有・未確定・R の総和と母数を合算し、率は合算母数から出す。合計を失わない。期間不明決済は別枠。
- **累積損益**: daemon が系列ごとに最新 120 件の決済を選び、それ以前は直前までの累計を持つ始点 1 点へまとめる。同時刻決済の既知損益を合算し、始点・終了点を含め最大 122 点。web は水平→垂直の階段線を描く。始点が from より後なら描画開始時刻と古い決済の省略件数を注記する (closed_count − 120、下限 0)。終点は表の既知損益合計と一致。既知なしは 0 の線と「損益不明」。
- 成績に正常・危険の色や状態の印は付けず、損益は符号で示す。

### 2.7 paper と live

- 今は区別する材料が無い (F6)。`account_snapshots.source` は行の種類 (`manual_ks_rebase`) も兼ね、`orders` には列が無い。
- **資金推移**: `source` が `paper` と `manual_ks_rebase` の行だけを描く。ほかの値の行があれば `other_sources` に数え、画面の上に「paper 以外の記録が N 行あります。この画面は paper だけを描いています」(注意の印) を出す。**混ぜて 1 本の線にしない**。
- **戦略別**: 分けられない。live を入れる設計で `orders` に区別の列を足し、この画面を分けることを、その設計の必須の項目として残す (§7)。
- 画面の h1 の下に、固定文「paper の記録です」を出す。

### 2.8 描画

- **route**: `/performance`。`ROUTE_TABLE` に 1 行、nav に「Performance」(`A/web/routes.py:20-30`、`A/web/render.py:86-96`)。
- **タブ**: `equity` (資金推移、既定)・`strategy` (戦略別)。`A/web/tabs.py` の表に足す。
- **query**: `tab` と `range` だけ。`range` は `7d`・`30d`・`90d`・`all` の完全一致。ほかの key・値は 400 (ほかの route と同じ規律、`A/web/request.py:283-349`)。期間の link は `range` を、帯は `tab` を付け替え、互いの値を引き継ぐ。
- **renderer**: 新しい module `A/web/perf_pages.py` (HTML) と `A/web/svg.py` (図)。`render.py` には足さない (1,334 行ある)。
- **SVG の形**:
  - `<svg viewBox="0 0 960 260" role="img" aria-label="…">`。幅は CSS で 100%。
  - 線は `<polyline>` (切れ目ごとに 1 本)、帯は `<polygon>`、軸と印は `<line>`、目盛りと凡例は `<text>`。座標は整数、x は 0〜960、y は 0〜260。
  - 使わない要素: `<script>`・`<foreignObject>`・`<image>`・`<use>`・`<a>`・`<style>`・event の属性。test で固定する。
  - 文字 (凡例の起動理由など) は `html.escape` を通す。起動理由は文法に合うものだけを出す (§2.5)。
- **目盛り**: 横は表示 tz の日付 (4〜8 個)。同一年内は MM-DD、年をまたぐ期間は YYYY-MM-DD。図の横に tz と期間の両端を添える。縦は金額 (4〜5 本、丸めた値)。表の時刻は原則どおりの表記。
- **数値**: 金額は小数 2 桁、勝率は既存の `_percent` (`A/metric_words.py:16-18`)、DD は百分率のまま `12.3%`、R は符号つき小数 2 桁。この画面の表は数値の列を右に寄せる (`table.num`、cell に class は付けない)。ほかの画面の表は変えない。
- **再読込**: 30 秒の自動再読込はそのまま。操作の状態を持たない画面なので、読み直しで失うものは無い。

### 2.9 CSS と CSP

- **CSP は変えない**。inline の `<svg>` は文書の一部で、画像の読込み (`img-src`) にならない。`script-src` は足さない。
- **色は class と `STYLE` の規則で付ける**。`style=""` 属性は CSP で効かず、test も無いことを見る (`T/web/test_render.py:125`)。色の presentation 属性 (`stroke="#…"`) は CSP の対象外だが、暗い配色に追従しないので使わない。
- **系列色の token を出荷する**。値は CSS の段が決めた 4 色 (`2026-11-18-web-css-design.md` 付録 B、明・暗とも地と縞で 4.7 以上)。今の `STYLE` に系列色の token は無い (`render.py:33-39`)。
- **色だけで区別しない**: 系列は線種 (実線・破線・点線・鎖線) も変え、凡例に記号を添える。作動は危険色 +「✕」、解除は中立色 +「↑」、付け直しは注意色 +「▽」、入出金は c2 +「◇」、不明な下降は注意色 +「△」。凡例にも同じ記号・色と text 用 class を使う。解除は既存状態印の ○・△・✕ (`A/web/marks.py:13`) と異なる ↑ にする。cron は中立色の長破線 (10 4)。軸は mut、文字は fg とし、明暗両方で軸・印・線は背景に 3:1、文字は 4.5:1 以上を確認する。
- **`STYLE` に足す規則** (下書き。コメント・空白を除いた追加分は 981 byte、現在の STYLE と合わせて 3,943 byte。実装時も 4,096 byte 以下を確認):

```css
--c1:#0969da;--c2:#bc4c00;--c3:#1a7f37;--c4:#8250df;/* 明の :root */
--c1:#4493f8;--c2:#f0883e;--c3:#3fb950;--c4:#ab7df8;/* 暗の :root */
svg{display:block;width:100%;max-width:60rem;height:auto;}
svg *{fill:none;stroke:var(--mut);vector-effect:non-scaling-stroke;}
svg text{fill:var(--fg);stroke:none;font-size:11px;}
.ax{stroke:var(--mut);}
.c1{stroke:var(--c1);}
.c2{stroke:var(--c2);stroke-dasharray:6 3;}
.c3{stroke:var(--c3);stroke-dasharray:2 3;}
.c4{stroke:var(--c4);stroke-dasharray:8 3 2 3;}
.bd{fill:var(--c1);stroke:none;opacity:0.15;}
.ev{stroke:var(--bad);stroke-dasharray:3 3;}
.rb{stroke:var(--warn);stroke-dasharray:3 3;}
.rl{stroke:var(--mut);stroke-dasharray:2 3;}
.cf{stroke:var(--c2);stroke-dasharray:6 3;}
.cr{stroke:var(--mut);stroke-dasharray:10 4;}
text.ev{fill:var(--bad);stroke:none;}
text.rb{fill:var(--warn);stroke:none;}
text.rl{fill:var(--mut);stroke:none;}
text.cf{fill:var(--c2);stroke:none;}
text.cr{fill:var(--mut);stroke:none;}
.num td:not(:first-child),.num th:not(:first-child){text-align:right;}
```

- `STYLE` は全ページが払う。承認詳細の「diff 以外 15 KiB」は骨格を数えるので、**追加 CSS の byte ぶん申告の切り詰めが早まる例がある** (CSS の段の §2.5 と同じ構図)。上限 4,096 は変えない。図の画面だけに 2 本目の `<style>` と hash を出す案は、hash の pin と「全ページの `<style>` が `STYLE` と一致」の test を 2 系統にするので採らない。
- 戦略別の図は系列 4 色 + 定期の判断 (中立の色)。戦略が 5 つ以上あれば、件数の多い 4 つだけを図に描き、残りは表にだけ出す (図の下に「ほか N 戦略は表のみ」)。
- **未確認**: inline SVG の中で class の `stroke: var(--token)` が明・暗とも効くこと、`vector-effect` の対応。実ブラウザの受入で確かめる (§4)。

### 2.10 予算

1 ページ 64 KiB (`A/web/render.py:18`) と STYLE 4,096 byte の上限は変えない。整数座標・束ごとの段・同じ最新 20 印で合成文字列を測り直した。上限は次の部品予算から決める。

| 部品 | 静的な最悪値の計算・合成計測 (UTF-8 byte) | 部品予算 |
|---|---|---|
| 資金推移の線・帯・軸 | 280 点、320 段、32 区間。頂点は `6×280 + 4×(320+32)` 個、各最大 8 byte、5 種の要素の包み 32 組と軸・凡例 1,536 byte を加え **31,328** | 32 KiB |
| 資金推移の印 | escape 後 title 最大 160 byte と line・text の固定属性、1 印 279 byte × 20 = **5,580** | 6 KiB |
| 印の表 | 1 行 280 byte × 20 + 見出し等 1,024 = **6,624** | 7 KiB |
| 戦略の図 | 5 系列 × 122 点 × 2 頂点 × 8 byte + 要素の包み 160、軸 1,536、凡例 5×160 = **12,256** | 14 KiB |
| 戦略の表 | 71 byte の名前、16 数値 cell (各 24 文字)、行の包みで 617 byte × 32 行 + 見出し等 2,048 = **21,792** | 22 KiB |

帯は部分束ごとに上下各 2 頂点を取り、上辺を時刻順・下辺を逆順に並べ、区間ごとに 1 polygon にする。頂点の区切り込みで最大 8 byte、要素の包みは 5 種合計 159 byte / 区間。階段の区間端も上の頂点数に含む。表は内訳を数値 cell に分け、不明・参考表示も各 24 byte 内。印の表は 7 cell (32 byte が 5 個、24 byte が 2 個) と包み 72 byte。見出しなどの固定部分も上限を実装時に固定する。

| ページ | 骨格・CSS・nav・期間 | 固定文・現在状態・集計・省略注記 | 図 | 印 | 表 | 合計予算 |
|---|---|---|---|---|---|---|
| 資金推移 | 6 KiB | 8 KiB | 32 KiB | 6 KiB | 7 KiB | **60 KiB** (予備 1 KiB 込み) |
| 戦略別 | 6 KiB | 4 KiB | 14 KiB | — | 22 KiB | **48 KiB** (予備 2 KiB 込み) |

- 表示名は最大 71 byte のまま切らない。title・凡例は escape 後 160 byte、数値・件数は最大 24 文字 (巨大な有限数は指数表記)。時刻・固定文は短縮しない。最大長・全上限を同時に与える test 1 本で、部品・完成ページの byte を確認する。
- 再集約・戦略の合算は daemon だけ。web の予備動作は予算を超えた部品を丸ごと説明文へ置き換える 1 段のみ。固定文・現在状態・集計値・省略数を残す。`_page` の Output clipped (`A/web/render.py:197-202`) に頼らない。
- 短命の `connect_readonly` (`S/store/db.py:1032-1061`、status の例は `S/service.py:2177-2180`) を使う。ops の lane・共有接続を使わず、BEGIN → snapshot・台帳・event・注文の読取を 1 本の transaction で行う。finally で ROLLBACK して close。共通の to と DB 視点を使い、現在の snapshot も同じ接続で読む。state は別取得で state_fetched_at を表示し、DB との同時性を主張しない。
- F2 は snapshot 集約だけの値。T1・T2 で本番相当と 10 倍の行数の全経路 (event・台帳・帰属 JOIN・注文集計・整形) を計測し、**10 倍の全期間で 0.5 秒以下**を合格とする。closed_at 索引はこの段で足さない。超過時は日次集計を別途検討する。

## 3. 失敗時・運用

| 起きること | 画面 |
|---|---|
| daemon に届かない・timeout | ほかの画面と同じ 502 の error ページ (`request.py:237-246`) |
| 古い daemon (endpoint が無い) | 502。web と daemon は同じ版で動かす前提 (ほかの route と同じ) |
| 応答の形が違う (`points` が list でない等) | 502 (`_check_reply_shape` に route を足す) |
| 不正な値・時刻 (stats・印・戦略・系列も含む) | daemon で除外・不明化して件数を注記。web は形の違いだけ 502、個々の表示異常は「不明」 |
| snapshot が 1 行も無い期間 | 図の代わりに「この期間の記録はありません」。印の表と注記は出す |
| 閉じた取引が 0 件 | 「この期間に決済した取引はありません」。保有中・決済未確定がある行は表に出す |
| kill switch の event が欠けている | 印が出ないだけ。marks_since と期間内欠落の固定文を出す |
| 印が 20 を超える | 図・表とも最新 20 だけを出し、残りの件数を出す |
| `source` に知らない値 | 描かずに件数を警告 (§2.7) |
| 帰属できない注文 | 「帰属不明」の行に件数 |
| 表示予算を超える | §2.10 のとおり部品を説明文へ置き換え、固定文・現在状態・集計値・省略数を保持 |
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
| nav に項目を足す | `T/web/test_render.py:135`、`T/web/test_tabs.py:192` (nav を取り出す 2 か所)、`T/web/test_simplified_views.py:224` | nav・route 表に触れる行は 22 行 / 9 file。nav の中身を見る箇所を T3a で確かめる |
| `STYLE` | `A/web/headers.py:7`、`T/web/test_render.py:75` | hash の値 (`STYLE` に触れる行は 18 行 / 3 file)。`STYLE_MAX_BYTES` (`:192`) は変えない |
| daemon の endpoint を 1 本足す | `tests/ops/test_ac01_02_43_api_routing.py:106`、`:313` (本数 35 が 2 か所と期待の表) | 36 に |
| 同 | `tests/ops/test_improve_repeat_api.py:38`、`tests/ops/test_ops_web_write_principal.py:56` | 本数 35 を 36 に |
| endpoint の本数 | `tests/test_news_cli_surface.py:106` | 35 を 36 に |
| 描画の共通検査 | `T/web/test_render.py:78-111` | `ReadClient.performance` と `PAGES` に新画面を追加。CSP・STYLE・nav の検査対象に含める |
| 応答の形 | `T/web/test_host_policy.py:205-212` | 新 route の不正応答で GET / HEAD とも 502 |
| principal の許可 | `tests/ops/test_ops_web_write_principal.py:23-28` | READS の handler 集合に performance を追加 |
| 同 | `tests/ops/test_scope_enforcement.py:161` | 全 endpoint の呼出しの表に 1 件 |
| 運用の文書 | `docs/operations/ops-api-2026-10-09.md` の route の表、`2026-10-04-ops-api-design.md` §4.2 | endpoint を 1 行 |

`T/web/test_runtime_isolated.py:173-177` の READS に T3a で新 route を必ず追加する。T3b では `:422` の `<svg` 禁止をこの route の server 描画要素に限り許す。`<script`・style 属性禁止、既存タブの値は維持する。

### 足す test

1. **束・段・時刻** (daemon): epoch 境界、任意期間、同時刻 id 順。束内上昇は最初の変化位置に末尾 HWM、下降は正確な位置。帯の幅、全行の最大 DD、閾値ちょうど / 直前 / 直後、hwm <= 0 と equity > hwm を確認。
2. **付け直し・欠落** (daemon): 同時刻の元行と付け直し、非ゼロ cashflow、台帳なし下降、source 不一致。帳簿の位置と操作時刻が期間境界の両側にある例。同じ束内の 10 分超・ちょうど 10 分、不正行・未知 source、末尾陳腐化で線を接続しない。
3. **印と現在状態** (daemon / web): 期間抽出前の統合、遅延 event、記録時刻だけの解除、同世代の重複・矛盾。marker の有無、latched_at 無しの投影、state 障害、source で絞らない最新行、snapshot 陳腐化、ラッチ無しで DD による停止。Status と分類が一致し、余裕の丸めで停止判定を変えない。
4. **帰属・指標** (daemon): 別 mission の決済、cron、signal、不正 trigger、全注文状態、期間不明決済。保有・未確定だけの行と帰属不明の単一行。同通貨の指標・勝率比率を backtest と比較。異通貨、payload 欠損・不正、SL 欠落・不一致・ゼロリスク、非有限値・bool、不正数量、未知 pair は R 不明。率別の母数、PF の損失 0、決済 30 件・R 既知 1 件を確認。
5. **累積・上限・整合性** (daemon / web): 最新 120 決済と以前の累計始点、同時刻合算、境界ちょうどの決済、階段線と表の終点一致。280 点・320 段・32 区間、20 印、32 行、5 系列 × 122 点。daemon の幅倍増と完全区間省略、合算母数からの率、読取中の別接続更新、例外時の transaction 終了を確認。T1・T2 で全経路の計測 (§2.10)。
6. **認可と隔離** (daemon / web): scope、ops-web-write の許可 handler 集合、監査書込みなし。`READS` に新 route を追加し、GET / HEAD (header 一致・HEAD 本文なし)、認証なし・誤認証・正常認証、敵対的入力を既存と同じ条件で検査。
7. **SVG と byte** (web): 要素・属性の allowlist、style 属性・script・event 属性・色の直書きなし。凡例・title・表示名のタグや引用符を構文解析で検査。静的な最悪計算 (§2.10) を最大長・全上限の 1 本の test で確認。部品の丸ごと省略後も固定文・集計・状態・省略数が残り Output clipped にならない。
8. **表示・query・CSS** (web): 上位の形違いは 502、個々の異常は不明。null、母数と「–」「(参考)」、空期間・保有だけの行、固定文。tab / range の完全一致・400、link の相互保持、JST 境界・年またぎ・夏時間、実時刻 / 記録時刻 / 操作時刻の区別。STYLE 4,096 byte 以下と hash、明暗両方の線・印・軸 3:1、文字 4.5:1、記号の fill と凡例を確認。

判定は `tmp/tools/judge.sh` 経由。`T/web/`・`tests/ops/`・`tests/test_news_cli_surface.py`・`tests/test_cli_surface.py` を含める。

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

各 task は単独で merge できる。T1・T2 は daemon、T3a 以降は web。STYLE を触るのは T3b だけ。

| task | 内容 | 触る file | 単独で merge したとき |
|---|---|---|---|
| T1 | 資金推移 API、束・段・印・current、status と状態読取の共通化、短命の読取 transaction、上限・全経路計測、認可・本数・運用文書。strategies は空 | `S/ops/performance_view.py` (新)、`S/ops/service.py`、`S/ops/api_server.py`、`S/ops/events.py`、`S/service.py`、`tests/ops/`、`tests/test_news_cli_surface.py`、§4 の運用文書 | API が増える |
| T2 | 戦略別の帰属・指標・R・行の合算・累積、注文込みの全経路計測 | `S/ops/performance_view.py`、`tests/ops/` | 応答に strategies が入る |
| T3a | route・nav・client・形の検査・隔離・HEAD、今の状態の枠と資金推移の表。SVG はまだ出さない。READS・PAGES・ReadClient と route 全数を更新 | `A/web/routes.py`、`tabs.py`、`request.py`、`render.py`、`perf_pages.py` (新)、`A/api_client.py`、`T/web/test_render.py`、`test_host_policy.py`、`test_runtime_isolated.py`、`test_approval_change.py`、関連 web test | Performance で表と現在状態を読める。戦略タブはまだ足さない |
| T3b | SVG・STYLE・hash、整数座標、資金推移の図と印、静的 byte 上限の検査 | `A/web/svg.py` (新)、`perf_pages.py`、`render.py`、`headers.py`、`T/web/` | 資金推移の図が見える |
| T4 | 戦略タブ、表・累積図・固定文、全上限の byte 検査 | `A/web/perf_pages.py`、`tabs.py`、`T/web/` | 戦略別が見える |
| T5 | 実ブラウザの受入。fixture を header つき HTTP で配信する手順 | — | — |

T1 → T2、T1 → T3a → T3b、T2・T3b → T4 → T5 の順。

## 6. 利用者に問う事項 (2026-11-19 裁定: 6 件とも推奨どおり)

| # | 問い | 推奨 | 理由 |
|---|---|---|---|
| 1 | この段の範囲。(a) 資金推移と戦略別の成績だけ。(b) 加えて「判断による決済と、そのまま SL / TP まで持った場合の R の比較」。(c) 加えて価格チャートへの指標・シグナル・建玉の重ね | (a)。(b) と (c) は別の段 (別チケット) | (b) は OHLCV を再生する反実仮想で、資金推移・集計とは別の仕組み (約定の規則の再利用、同じ足で SL と TP の両方に届いたときの扱い、価格の欠け、決済後どこまで追うか) を決める必要がある。対象 (判断による決済) は今 20 件に満たず、結論を出すには少ない。(c) は拡大・移動が要るので JS を入れる段になり、CSP と配布の設計が別に要る。(a) だけなら daemon 1 本 + 画面 1 つで閉じる |
| 2 | 少ない件数の表示のしきい値。各指標の有効母数 5 件未満は率を出さない、30 件未満は「(参考)」 | このとおり | 30 は backtest が「評価できる」とする件数と同じで、改善・承認の画面と基準が揃う。5 件未満の勝率・PF は 1 件で大きく動き、読む人を誤らせる。今は大半の起動理由が 10 件未満。下の値 (5) は根拠が弱いので、変えたければここで決める |
| 3 | 最新の印を優先して古い印を省き、件数の少ない戦略を合算してよいか | 最新優先と「ほか N 件」への合算を推奨 | 読める密度と byte 予算を保ち、省略数・対象期間と全件の集計値を併記できる。より多く必要なら予算と構成を見直す |
| 4 | 画面の構成。(a) 1 つの画面に 2 つのタブ (資金推移・戦略別)。(b) 1 つの画面に縦に並べる | (a) | 要望は「資金推移のページに戦略ごとの成績」。同じ画面・同じ期間の選択で 2 つを切り替えられるので要望に合う。縦に並べると図 2 つと表で 1 ページの予算 (64 KiB) の半分を超え、戦略が増えたときに切り詰めが要る。配置の段の「大項目はタブ」とも揃う |
| 5 | 作動の水準の線 (HWM × (1 − kill_pct / 100)) を出すか | 出す | DD と閾値は status / web で見せる、という前の裁定に沿う。equity との距離が図で分かる。注意は 2 つ: 過去に閾値を変えた分は再現できない (凡例に「今の閾値」と書く)。線を割っても、建てる判断が出るまでラッチしない (F21) ので、線と「✕」の位置はずれる (固定文で説明する) |
| 6 | 平均 R を (a) 金額 (手数料込み) と (b) 値幅 (通知の pnl_r) のどちらで出すか | (a)。quote 通貨 = 口座通貨の注文だけ計算し、他は不明 | 実現損益との対応が読みやすい。example の既定 (`config/settings.yaml.example:4-6`) なら backtest の金額式と同じ単位。異通貨は分母が quote 通貨、損益が口座通貨でそのまま割れない。backtest の avg_r は `backtest-avg-r-currency-mismatch` で対応する。通知との差を手数料だけとは説明しない |

技術で決めたもの (問いにしない): server SVG (§2.1)、daemon 側の集計と `status.read` (§2.2)、束ねの方式 (§2.3)、帰属は建てた判断 (§2.5)、初期 SL の扱い (§2.6)、`STYLE` を 1 本のままにする (§2.9)、年をまたぐ日付目盛り (§2.8)、DD と閾値の百分率・100 による換算 (§2.3)、印の時刻と付け直しの 2 列 (§2.4)、段を安全側にまとめる規則 (§2.3)、byte 予算から決めた 280 点・320 段・32 区間・20 印・32 行・最大 5 系列 (§2.10)。

## 7. 残余・やらないこと

- **反実仮想の比較・価格チャートの重ね**: この段に入れない (§6 の問い 1、裁定 (a))。`web-decision-close-vs-hold-r-compare` と `web-price-chart-overlays` に分けて起票した。
- **JS**: 入れない。値の読取り (hover)・拡大は無い。
- **kill switch の履歴の欠け**: 10 月上旬より前と、event が欠けた世代は台帳・現在 state の実時刻で補える範囲だけ印を出す。材料のない履歴は復元しない。確かな履歴が要るなら、ラッチと解除を台帳に書く変更が別に要る。
- **過去の閾値**: 残っていないので、作動の水準は今の閾値で引く。
- **paper / live の分離**: 戦略別は分けられない (§2.7)。live を入れる設計の必須の項目にする。
- **版ごとの成績**: 同じ名前の plugin は版をまたいで 1 行。
- **日次の集計表**: 作らない。全期間の走査が重くなったら別チケット (§2.10)。
- **CLI (`afx ctl`) と bot**: この集計を出さない。
- **スワップ**: 損益の合計に含めない (F17。今は全件 0)。
- **R の 2 つの定義の統一**: しない。通知の `pnl_r` は値幅のまま。この画面は通貨が一致する注文だけ金額の定義で計算し、列名に書く。異通貨の backtest avg_r は `backtest-avg-r-currency-mismatch` に起票済み。
- **ほかの画面の桁揃え・右寄せ**: しない。この画面の表だけ。
- **SVG の `forced-colors`・印刷**: 対応しない。
- **承認詳細の予算**: `STYLE` の増分で申告の切り詰めが早まる例がある。予算の見直しは `web-approval-detail-budget-review` で扱う。

## 付録 A. 依頼の前提と確かめた結果

| # | 依頼の前提 | 結果 |
|---|---|---|
| 1 | snapshot は約 6 万行で、そのままでは応答の上限を超える | そのとおり (F1)。基本 280 束以下にし、段の上昇を束ごとにまとめる。分割後は daemon が上限内へ再集約または部分表示。下降と欠落の境を守る (§2.3・§2.10) |
| 2 | ラッチの履歴が DB にあるか | 一部だけ。今の状態は state file と marker、履歴は `ops_events` (best effort、10 月上旬以降)、付け直しは台帳 (F7〜F9)。ラッチは線を割った時点でなく、次に建てる判断の時点 (F21) |
| 3 | 閉じた取引は約 20 件 | そのとおり。起動理由は数種で、大半が 10 件未満 (F12・F13) |
| 4 | `intent_id` が NULL の注文・部分約定・取消の扱い | 今は 0 件。扱いは §2.5 に決めた |
| 4 | `trigger` が NULL の mission 約 600 件の扱い | improve と reflection の mission で、取引の帰属に関わらない (F11) |
| 5 | `orders.stop_loss` は後で動く可能性がある | 今は動かす経路が無い。payload と全件一致 (F14)。食い違えば R を不明にする守りを入れる (§2.6) |
| 6 | 反実仮想は重い | そのとおり。別の段を推奨 (§6 の問い 1) |
| 7 | 新しい読取 endpoint が要るか | 要る (1 本)。集計は daemon 側、要求ごとの短命の読取 transaction (§2.2、§2.10) |
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

| 記述が古い | 今の kill switch 状態は state file だけという前提 | marker が残ると load() はラッチ中を返す (`S/store/state.py:259-267`)。§2.2 に反映 |
| 範囲が違う | status の mode と paper の区別 | status は state.mode.value (`S/ops/service.py:1030`)、paper は取引 event の既定 (`S/ops/trade_event_sink.py:347`)。§2.7 は固定文 |
| 記述が古い | 今の状態を「解除済み」とする語 | Status の分類は作動中 / 照合待ち / 解除 / 不明 (`A/web/marks.py:83-95`)。同じ関数を共用 |
| 記述が古い | 束頭 HWM と小数座標による予算の見積り | §2.10 は束の幅の帯・安全側の段・整数座標・区間数を含めて再計算 |
| 記述が古い | 異通貨 avg_r を新たに起票する前提 | `docs/tickets/open/backtest-avg-r-currency-mismatch.md:2-7` で起票済み |
| 範囲が違う | 勝率も DD と同じ百分率という前提 | backtest は比率 (`S/backtest/metrics.py:132`)、web の `_percent` は 100 倍 (`A/metric_words.py:16-18`)。勝率だけ比率で返す |
| 記述が古い | 決済未確定の根拠を executor の説明文に置く参照 | 実経路はレート取得失敗 (`S/core/executor.py:451-453`) と realized_pnl = None の保存 (`:955-961`) |

## 変更履歴

| 日付 | 版 | 変更 | 理由 | commit |
|---|---|---|---|---|
| 2026-11-19 | C0 | 初版 (下書き) | 利用者の方針 (配置 → CSS → チャート。資金推移のページに戦略ごとの成績) | — |
| 2026-11-19 | C0.1 | 単位・時刻・集約・現在状態・R・応答契約・予算・検証・問い・関連文書の分類を改訂 | 設計レビュー 1 周目の指摘を反映 | |
| 2026-11-19 | C0.2 | 現在状態・段・印・累積・応答契約を簡素化し、予算・検証・作業分割・関連文書を更新 | 設計レビュー 2 周目の指摘を反映 | |
| 2026-11-19 | C1 | §6 を裁定済みにし、範囲外の 2 つをチケットへ | 利用者承認 (6 件とも推奨どおり) | |
