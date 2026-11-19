# 同梱 web のチャートの段: 資金推移と戦略ごとの成績 (web-ui-charts-equity-indicators) 設計 C0

**チケット:** `web-ui-charts-equity-indicators` (低、機能)。関連: `web-layout-tabs` (配置の段、完了)、`web-css-design` (CSS の段、完了)
**状態:** C0 (下書き、未承認)。§6 の問いは未裁定

パスの略記: A = `afx-ops/afx_ops`、T = `afx-ops/tests`、S = `src/agentic_fx`。
本番 DB は読取専用で、件数・期間・列の分布だけを確かめた (2026-11-19)。金額・plugin の名前・取引の正確な件数は書かない (件数は概数)。

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
| F4 | HWM が下がるのは 2 つの場合だけ: 付け直しの行と、負の `cashflow` (出金) の行。付け直しは **元の行と同じ `ts`** で、`hwm = equity`、`source = 'manual_ks_rebase'` の行を 1 つ足す。台帳は `kill_switch_hwm_rebases` (追記専用) | `S/ops/rebase_accounting.py:196-213`、`accounting.py:31`。DB では HWM の下降・同じ ts の重複・台帳の行が同じ数で、出金による下降は無い |
| F5 | `cashflow` は全行 0。0 以外を書く経路は今は無い | DB。`scheduler.py:1005` は `cashflow` を渡さない |
| F6 | `source` は `paper` と `manual_ks_rebase` の 2 種。**live の値を書く経路は無い**。`orders` には paper / live を分ける列が無い | DB。`scheduler.py:1005` (既定値)、`S/store/db.py` の `orders` |
| F7 | kill switch の**今の状態**は state file にしか無い (`latched`・`generation`・`latched_at`)。履歴は持たない | `S/store/state.py:80-82`、`:359-369` |
| F8 | kill switch の**履歴**は `ops_events` の `guard_state_changed` (state は `latched` / `released`、世代つき)。best effort で、欠けた分の埋め戻しを見送ることがある。`ops_events`・`account_snapshots` を消す処理は無い | `S/ops/service.py:138-159`、`:1975-1996`。`S` 全体を grep して両表への DELETE なし (`S/store/db.py:928` の表名の一覧は定義だけで、参照が無い) |
| F9 | `ops_events` は 10 月上旬からしか無い (snapshot は 8 月末から)。kill switch の event は数件で、`released` だけがあり `latched` が欠けた世代が実際にある | DB |
| F10 | 注文の帰属は `orders.intent_id → trade_intents.mission_id → missions.trigger`。`intent_id` を書くのは注文を作るときだけで、決済では変わらない | `S/core/executor.py:682`、`S/ops/trade_event_sink.py:343-344` |
| F11 | `trigger` が NULL の mission は improve と reflection だけ (約 600 件)。trade の mission (数千件) は全件 `cron` か `signal:<plugin>` | DB。`S/store/db.py:666` |
| F12 | 注文は 20 件あまりで、全件 `intent_id` あり。閉じた取引は約 20 件で、大半が判断による決済、SL と TP は数件ずつ。部分約定・取消・期限切れは 0 件 | DB |
| F13 | 起動理由は数種 (`cron` と複数の `signal:`)。大半は閉じた取引が 10 件未満 | DB |
| F14 | `orders.stop_loss` を書くのは注文を作るときだけで、後から動かす経路は無い。intent の payload にも `stop_loss` があり、全件で一致 | `executor.py:687`、`:1212`。grep (`stop_loss` を渡す `update_fields` は無い)、DB |
| F15 | **R の定義がコードに 2 つある**。(a) 値幅: `(close − entry) × 向き ÷ |entry − SL|` (通知・event の `pnl_r`)。(b) 金額: `realized_pnl ÷ (|約定 − SL| × 数量 × 契約単位)` (backtest と床の `avg_r`、手数料を含む) | (a) `S/ops/trade_events.py:214-222`、(b) `S/backtest/metrics.py:89-97` |
| F16 | 勝率・PF・件数の定義は backtest にある。勝ちは `pnl > 0`、PF は損失が 0 なら無し、30 件未満は `evaluable` が偽 | `S/backtest/metrics.py:20`、`:100-104`、`:131-132`、`:142` |
| F17 | `realized_pnl` は手数料を引いた値。スワップ (`fees_swap`) は別の列で、今は全件 0 | `S/core/paper_broker.py:4-6`、DB |
| F18 | daemon の応答には表示 tz (`display_timezone`) が必ず付く | `S/ops/api_server.py:656` |
| F19 | `STYLE` は 2,962 byte、上限は 4,096 byte (test)。残りは 1,134 byte | 実測、`T/web/test_render.py:192` |
| F20 | web の principal (`ops-web`・`ops-web-write`) は `status.read` を持つ | `S/ops/contracts.py:106-112` |
| F21 | **ラッチは毎 tick の判定ではない**。DD が閾値以上の間、tick は予約の約定を止めるだけで、ラッチを書かない。ラッチは次に「建てる」判断が risk gate を通るときに書く | `S/core/scheduler.py:294-301`、`S/core/risk_gate.py:195`、`S/core/executor.py:653` |

## 1. 何が変わるか (動作)

利用者 (運用する本人) は、上部の nav に増える **Performance** を開く。書込みは無い。JS は使わない。

### 1.1 資金推移のタブ (既定)

1. **期間を選ぶ**: 「7 日 / 30 日 / 90 日 / 全期間」の link (既定は 30 日)。link は `?range=` を付け替えるだけ。
2. **図を読む**: 横は時刻 (表示 tz)、縦は口座通貨の金額。
   - **equity**: 実線。その後ろの薄い帯は、束ねた時間の中の最小〜最大。帯の下端がその時間の最も悪い equity。
   - **balance**: 破線。
   - **HWM**: 点線。付け直しのところで段になって下がる。
   - **作動の水準**: HWM × (1 − 今の閾値)。細い線。equity がここに届くと予約の約定が止まり、次に建てる判断が出たときに kill switch が作動する (F21)。**線を割っても、建てる判断が出る前に戻れば「✕」は付かない**。
   - **印** (縦線と記号): kill switch の作動「✕」、解除「○」、HWM の付け直し「▽」、入出金「◇」。
   - 記録の無い時間 (週末・停止) は線を切る。つないで埋めない。
3. **数字で読む** (図の下の表): 期間の初めと終わりの equity・balance、最大・最小、**最大 DD (率と時刻)**、今の HWM と作動の水準までの余裕。最大 DD は間引く前の全行から出した値。
4. **印の表**: 作動・解除・付け直し・入出金の時刻 (新しい順)。時刻は `YYYY-MM-DD HH:MM:SS +09:00`。
5. **注記** (固定文): 「線は時間で束ねた表示で、判定の根拠ではありません」「kill switch は、equity が作動の水準以下のときに建てる判断が出ると作動します。線を割っただけでは印は付きません」「kill switch の印は記録がある分だけです (<最初の記録の日> より前は無し)」。

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

- 失うものへの手当て: 図の下に数字の表を置く (§1.1 の 3・4)。印には SVG の `<title>` を付け、ブラウザの標準の吹き出しで時刻が読める。
- 既定の方針 (server SVG を先に、JS は対話が要るとき) のとおり。対話が要るのは価格チャート (拡大・移動・重ね) で、それは後の段 (§7)。

### 2.2 データの経路

```
web (afx-ops) ──GET /v1/performance?range=30d──▶ daemon (ops API、status.read)
                                                   └ S/ops/performance_view.py が DB を読んで束ねる
```

- **新しい読取 endpoint が 1 本要る**。今の `GET /v1/status` は今の値だけ、`GET /v1/events` は 10 月以降の event だけで、snapshot の列も注文の集計も返さない。
- **集計は daemon 側**。web は DB を持たず、6 万行を渡すと応答が数 MB になる。daemon は束ねた点 (最大 280) と集計済みの行だけを返す。
- **scope は `status.read`**。口座の値は今も `status` で同じ principal に見せている (F20)。新しい scope は作らない。`ops-notify` (`events.read` だけ) には見えない。
- **読取専用**。監査行 (`ops_requests`) は書かない (ほかの GET と同じ)。DB への書込みは無い。
- **CLI は足さない**。daemon の生きた状態を読むので、置くなら `afx ctl` の「稼働観測・実行追跡」だが、この段は web だけにする (§7)。`cli_surface` の検査は CLI の記録から endpoint を引く向きなので、記録の無い endpoint は通る (`tests/test_cli_surface.py:183-195`)。逆向きの検査が無いことは T1 で確かめる。
- web 側: `WebClient.performance(range=)` を足す (`A/api_client.py:535-573`)。`WebWriteClient` は継承するので、書込みを有効にした web でも同じ。

応答の形 (`data`):

| key | 中身 |
|---|---|
| `range`・`bucket_sec`・`from`・`to` | 選んだ期間、束の幅 (秒)、期間の端 (UTC) |
| `mode` | 今の mode (`paper`)。`status` と同じ出所 |
| `as_of`・`computed_at` | 最新の snapshot の時刻、集計した時刻 |
| `points` | 束の列。各要素は `t` (束の初め、UTC)・`n`・`equity_min`・`equity_max`・`equity_last`・`balance_last`・`hwm`・`dd_max`・`seg` (線の切れ目の番号) |
| `stats` | 初めと終わりの値、最大・最小、最大 DD (率・時刻)、今の HWM、`kill_pct` (今の設定値) |
| `marks` | 印の列 (新しい順、最大 40)。`kind` (`latched`・`released`・`rebase`・`cashflow`)・`ts`・`generation`。切った件数を `marks_omitted` に |
| `marks_since` | kill switch の event が残っている最初の時刻 (無ければ null) |
| `other_sources` | `paper`・`manual_ks_rebase` 以外の `source` の行数 (§2.7) |
| `strategies` | 起動理由ごとの行 (最大 50)。§2.5 の項目と、累積損益の点の列 (最大 120) |
| `unattributed` | 帰属できなかった注文の件数 (§2.5) |

### 2.3 資金推移の間引き

**束の幅は期間で決まる固定の表**。束の境界は UTC の epoch から数える (読み直しても境界が動かない)。

| 期間 | 束の幅 | 列の数 |
|---|---|---|
| 7 日 | 1 時間 | 168 |
| 30 日 | 3 時間 | 240 |
| 90 日 | 8 時間 | 270 |
| 全期間 | 1・2・3・4・6・8・12 時間、1・2・3・7 日のうち、列が 280 以下になる最小の幅 | ≤ 280 |

**束ごとの値** (行は必ず `(ts, id)` の順に読む):

| 値 | 決め方 | 理由 |
|---|---|---|
| `equity_min`・`equity_max` | 束の中の最小・最大 | 最も悪い時点を消さない |
| `equity_last`・`balance_last` | 束の最後の行 | 線の連続 |
| `hwm` | 点の中の最大。点は下の規律 1 で、HWM が下がる行の手前で割ってあるので、点の中では単調 (F3・F4) で、最後の行と同じ | HWM を低く見せない。下がった後の値で前を上書きしない |
| `dd_max` | **1 行ごとに** `(hwm − equity) ÷ hwm` を出した最大 (`S/core/accounting.py:44-47` と同じ式) | 束の最小 equity と束の HWM から後で割り出すと、時点がずれて実際より大きくも小さくもなる |

**HWM と kill switch の判定を歪めない規律**:

1. **HWM が下がりうる行で束を割る**。対象は 2 種: 付け直しの行 (台帳の `rebase_snapshot_id` で見分ける。`source` の文字列に頼らない) と、`cashflow` が 0 でない行 (出金で HWM が下がる。F4)。その行より前を 1 つの点、その行から後を別の点にし、同じ横位置に 2 点を置く。HWM の線はそこで垂直に動く。束ねて平均したり、下がった後の値で前を上書きしたりしない。
2. **`stats` の最大 DD は全行から出す**。束ねた点から出さない。
3. **記録の無い束は点を作らない**。前の値で埋めない (`seg` を進めて線を切る)。
4. **図は表示で、判定の根拠ではない**。daemon は最新の snapshot の equity と HWM で判定する (約定の停止は tick ごと `S/core/scheduler.py:294-301`、ラッチは建てる判断の gate `S/core/risk_gate.py:195`。F21)。線を割ることとラッチは同じ時点にならないので、固定文で画面に書く (§1.1 の 5)。
5. **作動の水準の線は今の閾値で引く**。過去に閾値を変えていても、過去の値は残っていない (付け直しの台帳に、その時の `threshold_pct` があるだけ)。凡例に「今の閾値」と書く。

**`cashflow`**: 0 でない行で束を割り (上の 1)、そこに入出金の印を付ける。線は補正しない (入出金で equity・balance・HWM が一緒に動く。F3)。最大 DD は行ごとの値なので、出金で歪まない。今は全行 0 (F5)。

### 2.4 印の出所

| 印 | 出所 | 信頼度 |
|---|---|---|
| HWM の付け直し | `kill_switch_hwm_rebases` (`applied_at`・`generation`) | 追記専用の台帳。欠けない |
| kill switch の作動・解除 | `ops_events` の `guard_state_changed` (`guard_kind = kill_switch`) | best effort。10 月上旬より前は無い (F9)。欠けることがある (F8) |
| 作動 (補い) | 台帳の `latched_at` と、今ラッチ中なら state file の `latched_at` | 上の event に同じ世代の `latched` が無いときだけ足す |
| 入出金 | `cashflow` が 0 でない snapshot の行 | snapshot そのもの |

- 同じ (世代、状態) の印は 1 つにまとめる。
- **欠けを隠さない**: `marks_since` を画面に出す (§1.1 の 5)。解除の event が欠けた世代は、作動の印だけが出る。
- `ops_events` を読む SQL は `S/ops/events.py` にだけ置く (`events.py:266` の規律)。期間で絞る関数を 1 つ足す。
- 使わないもの: `ops_requests` の解除の監査行 (解除しか分からず、event と重なる)、activity の log (file で、形が自由)。

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
| `open` など約定済みで未決済 | 「保有中」の件数だけ。損益に入れない |
| `closing`・`close_unknown` | 「決済未確定」の件数だけ (あれば注記) |
| 約定せずに終わった (`cancelled`・`expired`・`rejected`・`invalidated`) | 対象外。取引ではない |
| 部分約定で決済 | 対象。損益は `realized_pnl` のまま。今は 0 件で、paper は全量を約定させる (`executor.py:720-721`、`scheduler.py:1675-1676`) |

**決済の内訳**: `close_reason` を `llm_close` (判断)・`sl`・`tp`・その他 (日替わりの強制決済 `day_rollover` など) の 4 つに数える。知らない語は「その他」。

**版**: 同じ plugin 名で版 (`content_hash`) が変わっても同じ行に数える。版ごとの分割は入れない (今は版が混ざった戦略は無い。DB)。注記に書く (§1.2 の 4)。

### 2.6 指標の定義と、少ない件数の表示

**定義は backtest と同じにする** (改善・承認の画面の数字と比べられるように)。

| 指標 | 定義 | 出せないとき |
|---|---|---|
| 件数 | 対象の閉じた取引の数 | — |
| 勝 / 負 / 分 | `realized_pnl` が正 / 負 / 0 | — |
| 勝率 | 勝 ÷ 件数 (`metrics.py:132`) | 件数が足りない (下) |
| PF | 利益の合計 ÷ 損失の合計。損失が 0 なら無し (`metrics.py:131`) | 「–」 |
| 平均 R | 金額の R (F15 の (b)) の平均 | 「–」 |
| 損益の合計 | `realized_pnl` の合計 (スワップを含まない。F17) | — |

- **R は金額の定義 (b) に揃える** (§6 の問い 6)。床の `avg_r` と同じで、手数料を含む。通知の `pnl_r` (値幅の定義 (a)) とは 1 件ごとに少し違う。画面の列名は「平均 R (手数料込み)」。定義を 3 つ目に増やさず、`metrics.py` の 1 行ぶんの計算を関数に切り出して両方から使う。
- **初期 SL**: `orders.stop_loss` を使う。今は後から動かす経路が無い (F14)。将来 SL を動かす機能が入ったときに黙って狂わないよう、daemon は intent の payload の `stop_loss` と見比べ、**違う注文は R を「不明」にして平均から外す** (件数・勝率・PF には入れる)。
- R を出せない注文 (SL が無い、約定価格と同じ、数量が不正、未知の pair): 同じく「不明」。backtest の側は例外で止める (`metrics.py:85-93`) が、表示は止めずに「R 不明 N 件」と出す。

**少ない件数の表示** (しきい値は §6 の問い 2):

| 件数 | 表示 |
|---|---|
| 5 件未満 | 勝率・PF・平均 R は「–」。件数・勝 / 負 / 分・損益の合計・内訳だけ |
| 5〜29 件 | 数字を出し、件数の横に「(参考)」。表の上に「30 件未満は参考値です」 |
| 30 件以上 | 数字だけ |

- 30 は backtest の `evaluable` と同じ値 (F16)。daemon 側の定数を応答に載せ、web は値を持たない。
- **成績に正常・危険の色を付けない**。損益は符号 (`+`・`−`) で示す。緑や赤は、少ない件数の数字を良し悪しの合図に見せる。状態の印 (○・△・✕) も付けない。
- 今のデータでは大半の起動理由が 10 件未満 (F13) なので、当面はほとんどの行が「–」か「(参考)」になる。それが正しい表示。

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
- **目盛り**: 横は表示 tz の日付 (`MM-DD`、4〜8 個)、縦は金額 (4〜5 本、丸めた値)。日付だけの短い表記は時刻の表記の原則の例外になる (§6 の問い 3)。表の時刻は原則どおりの表記。
- **数値**: 金額は小数 2 桁、率は `12.3%` (`metric_words.PERCENT_FORMAT` と同じ)、R は符号つき小数 2 桁。この画面の表は数値の列を右に寄せる (`table.num`、cell に class は付けない)。ほかの画面の表は変えない。
- **再読込**: 30 秒の自動再読込はそのまま。操作の状態を持たない画面なので、読み直しで失うものは無い。

### 2.9 CSS と CSP

- **CSP は変えない**。inline の `<svg>` は文書の一部で、画像の読込み (`img-src`) にならない。`script-src` は足さない。
- **色は class と `STYLE` の規則で付ける**。`style=""` 属性は CSP で効かず、test も無いことを見る (`T/web/test_render.py:125`)。色の presentation 属性 (`stroke="#…"`) は CSP の対象外だが、暗い配色に追従しないので使わない。
- **系列色の token を出荷する**。値は CSS の段が決めた 4 色 (`2026-11-18-web-css-design.md` 付録 B、明・暗とも地と縞で 4.7 以上)。今の `STYLE` に系列色の token は無い (`render.py:33-39`)。
- **色だけで区別しない**: 系列は線種 (実線・破線・点線・鎖線) も変え、凡例に記号を添える。作動の印は危険の色 + 「✕」、付け直しは注意の色 + 「▽」。
- **`STYLE` に足す規則** (下書き、775 byte。合計 3,737 byte で上限 4,096 の内。残り 359 byte):

```css
  --c1: #0969da; --c2: #bc4c00; --c3: #1a7f37; --c4: #8250df;   /* :root (明) に */
  --c1: #4493f8; --c2: #f0883e; --c3: #3fb950; --c4: #ab7df8;   /* 暗の :root に */
svg { display: block; width: 100%; max-width: 60rem; height: auto; }
svg * { fill: none; stroke: var(--mut); vector-effect: non-scaling-stroke; }
svg text { fill: var(--mut); stroke: none; font-size: 11px; }
.ax { stroke: var(--line); }
.c1 { stroke: var(--c1); }
.c2 { stroke: var(--c2); stroke-dasharray: 6 3; }
.c3 { stroke: var(--c3); stroke-dasharray: 2 3; }
.c4 { stroke: var(--c4); stroke-dasharray: 8 3 2 3; }
.bd { fill: var(--c1); stroke: none; opacity: 0.15; }
.ev { stroke: var(--bad); stroke-dasharray: 3 3; }
.rb { stroke: var(--warn); stroke-dasharray: 3 3; }
.num td:not(:first-child), .num th:not(:first-child) { text-align: right; }
```

- `STYLE` は全ページが払う。承認詳細の「diff 以外 15 KiB」は骨格を数えるので、**この 775 byte ぶん申告の切り詰めが早まる例がある** (CSS の段の §2.5 と同じ構図)。上限 4,096 は変えない。図の画面だけに 2 本目の `<style>` と hash を出す案は、hash の pin と「全ページの `<style>` が `STYLE` と一致」の test を 2 系統にするので採らない。
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
- **図に描く印は最新 40 まで**。超えた分は描かず、「ほか N 件は表示していません」と出す。100 まで描くと図だけで 41 KiB になり、表と骨格を足して 64 KiB に近づく。
- **資金推移のタブの上限**: 図 (280 列・切れ目 30・印 40) + 数字の表 + 印の表 (最新 20 行) + 骨格で **48 KiB 以下**を test で固定する (上の実測の合計は約 38 KiB)。
- **戦略別のタブ**: 表は最大 50 行 (`MAX_ITEMS`)、図は 5 系列 × 最大 120 点 (超える系列は daemon が束ねる)。**32 KiB 以下**を test で固定する (この図は未実測。5 × 120 点は上の線 3 本 × 240 列より少ない)。
- **daemon の応答**: 点 280 (割った点を含めて最大 320)・印 40・戦略 50・系列の点 120 を上限とし、超えた分は切って件数を返す。応答は数十 KB で、client の上限 8 MiB (`A/api_client.py:60`) に対して十分小さい。
- **daemon の負荷**: 集計はほかの読取と同じ lane で 1 回 0.06 秒以下 (F2)。**期間ごとに 30 秒の cache を持つ** (最大 4 件)。開いたままの画面が 30 秒ごとに読み直しても、走査は期間ごとに 30 秒に 1 回まで。cache の中身は応答そのもので、`computed_at` を画面の下に出す。読取専用の表示なので、cache を捨てる合図 (書込みとの連動) は作らない。
- 行数が今の 10 倍 (約 2 年分) になっても、30 日・90 日の走査は index の範囲読みで変わらない。全期間だけが行数に比例する (約 0.5 秒)。それを超えたら日次の集計表を別チケットで検討する (§7)。

## 3. 失敗時・運用

| 起きること | 画面 |
|---|---|
| daemon に届かない・timeout | ほかの画面と同じ 502 の error ページ (`request.py:237-246`) |
| 古い daemon (endpoint が無い) | 502。web と daemon は同じ版で動かす前提 (ほかの route と同じ) |
| 応答の形が違う (`points` が list でない等) | 502 (`_check_reply_shape` に route を足す) |
| 点の値が数でない・有限でない・bool | その点を捨て、線を切る。捨てた件数を注記に出す |
| snapshot が 1 行も無い期間 | 図の代わりに「この期間の記録はありません」。印の表と注記は出す |
| 閉じた取引が 0 件 | 表の代わりに「この期間に決済した取引はありません」。保有中の件数は出す |
| kill switch の event が欠けている | 印が出ないだけ。`marks_since` の注記で分かる |
| 印が 40 を超える | 最新 40 だけを描き、残りの件数を出す |
| `source` に知らない値 | 描かずに件数を警告 (§2.7) |
| 帰属できない注文 | 「帰属不明」の行に件数 |
| 64 KiB を超えた | 今の規則どおり「Output clipped」(`render.py:197-202`)。上限の test で先に止める |
| `STYLE` と hash のずれ | CSS が全部外れ、図は色なし (線は既定の黒、塗りは黒) で崩れる。test (`test_render.py:114-125`) が止める。`STYLE` と hash 2 か所は同じ commit で変える |

- **反映**: daemon (endpoint) と web (画面) の両方を再起動する。daemon の再起動は取引を止めるので、時間は利用者が決める。
- **戻し方**: task ごとに 1 commit を revert する。DB・設定・state に変更は無い。
- **資金保護との関係**: この画面は読むだけで、図・集計の値は発注・kill switch の判定に使わない。

## 4. test と受入

### 今の test への影響

web の test は 725 件 (`T/web/`)。影響する pin を grep で数えた。

| 変更 | 書き換える test | 中身 |
|---|---|---|
| web の route を 1 本足す | `T/web/test_routes.py:7` | route 表の全数の一致。`ROUTE_TABLE`・`client_method` を参照する test は 14 行 / 7 file で、全数を見るのはここだけ (ほかは T3 で確かめる) |
| nav に項目を足す | `T/web/test_render.py:135`、`T/web/test_tabs.py:192` (nav を取り出す 2 か所)、`T/web/test_simplified_views.py:224` | nav・route 表に触れる行は 22 行 / 9 file。nav の中身を見る箇所を T3 で確かめる |
| `STYLE` | `A/web/headers.py:7`、`T/web/test_render.py:75` | hash の値 (`STYLE` に触れる行は 18 行 / 3 file)。`STYLE_MAX_BYTES` (`:192`) は変えない |
| daemon の endpoint を 1 本足す | `tests/ops/test_ac01_02_43_api_routing.py:106`、`:313` (本数 35 が 2 か所と期待の表) | 36 に |
| 同 | `tests/ops/test_scope_enforcement.py:161` | 全 endpoint の呼出しの表に 1 件 |
| 運用の文書 | `docs/operations/ops-api-2026-10-09.md` の route の表、`2026-10-04-ops-api-design.md` §4.2 | endpoint を 1 行 |

変わらないことを確かめる pin (そのまま通る想定):

- `<script` が無いことを見る箇所 (38 行)。JS を入れないので全部そのまま。
- `T/web/test_runtime_isolated.py:422` は 5 つの route の本文に `<svg`・`<b>` などが無いことを見る (敵対的な入力が素通りしないことの検査)。対象の表 `READS` (`:173-177`) に新しい route は無いので通る。**新しい route を `READS` に足すときは、`<svg` の検査をこの route だけ「server が描いた 1 つだけ」に変える** (T3)。
- `" style="` が無い (`test_render.py:125`)。SVG に style 属性を出さない。
- SVG の `<title>` (印の吹き出し): `T/web/` にページの `<title>` を見る test は無い (grep で 0 行) ので、当たる pin は無い。
- tabs の表を見る箇所 (`TABS`・`tab_names` は 50 行 / 10 file)。表に 1 route を足すだけで、今の route の値は変えない。

### 足す test

1. **間引き** (daemon): 束の境界が UTC で固定。`(ts, id)` の順。`equity_min`・`equity_max`・`hwm`・`dd_max` が行ごとの値と一致。
2. **付け直しと入出金** (daemon): 同じ ts の 2 行で束が割れ、HWM が前の点で高く・後の点で低い。台帳の無い `manual_ks_rebase` の行、台帳にあるが `source` が違う行の扱い。負の `cashflow` の行でも束が割れ、前の点の HWM が下がった後の値にならない。
3. **最大 DD** (daemon): 束ねた点からでなく全行から出る (束の中で equity が戻る例で、束ねた値と違うことを見る)。
4. **空き** (daemon): 記録の無い束に点が無く、`seg` が進む。
5. **印** (daemon): event・台帳・state の 3 つの出所の合成。同じ (世代、状態) が 1 つになる。event が欠けた世代。`marks_since`。
6. **帰属** (daemon): 建てた判断で決まる (決済を別の mission が出した例)。`cron`、`signal:`、`intent_id` NULL、形の合わない `trigger`。入口 (注文の状態) × 帰属の表で。
7. **指標** (daemon): 勝率・PF・平均 R が `backtest/metrics.py` と同じ値 (同じ注文の列を両方に通す)。PF の損失 0。R 不明 (SL の食い違い、SL と約定が同じ)。件数のしきい値。
8. **期間** (daemon): `closed_at` が境界ちょうど。保有中は期間に関わらず数える。
9. **上限と cache** (daemon): 点 280・印 40・戦略 50。30 秒の間は走査が 1 回。scope (`status.read` のある principal だけ、`ops-notify` は 403)。監査行を書かない。
10. **SVG** (web): 許した要素・属性だけ (allowlist で全要素を調べる)。`style=`・`<script`・event の属性・色の直書きが無い。文字が escape 済み (起動理由に `<` を入れた応答で)。
11. **予算** (web): 最大の入力で、資金推移のタブが 48 KiB 以下、戦略別のタブが 32 KiB 以下。印が 40 を超える入力で、残りの件数が出る。
12. **表示** (web): 件数のしきい値ごとの表示 (「–」「(参考)」)。損益未確定・R 不明・帰属不明の件数。固定文 (§1.1 の 5、§1.2 の 1・4) が必ず出る。時刻の表記。`other_sources` の警告。空のとき。
13. **query** (web): `tab`・`range` の完全一致、ほかは 400。link が互いの値を引き継ぐ。
14. **時刻** (web): 実時刻の例で pin する。JST で日付が変わる前後の束が、目盛りで正しい日付に出る。夏時間のある tz でも 1 例。
15. **CSS** (web): `STYLE` が 4,096 以下。系列色の token が明・暗の両方にある。コントラストの test (今ある) に系列色 × 地・縞を足す (3:1 以上)。

判定は `tmp/tools/judge.sh` 経由。`T/web/`・`tests/ops/` に加えて、endpoint の本数を見る suite と `tests/test_cli_surface.py` を含める。

### 実ブラウザの受入 (利用者)

配色・CSP・SVG の描画は test で見られない。利用者が実ブラウザで、**明・暗の両方**で確かめる。

1. 資金推移: 4 つの期間。線 3 本と帯・作動の水準・印が見分けられる (色と線種の両方)。付け直しの段。週末の切れ目。
2. 戦略別: 表の「–」と「(参考)」、固定文、累積損益の図と凡例。
3. 印に pointer を置くと時刻が出る。
4. スマホ幅 (LAN 越しを使っている場合): 図が幅に収まり、表は節の中で横にスクロールする。
5. ブラウザの console に CSP の違反が出ない。
6. ほかの画面 (Status・承認詳細) の見た目が変わっていない。承認詳細の申告が前より早く切れていないか、手元の承認 1 件で見る。
7. 作動中・live の記録が混ざる・帰属不明がある、の見た目は本番で起こさない。fixture から描いた HTML を file で開いて見る。

## 5. task 表

各 task は単独で merge できる。T1・T2 は daemon だけ、T3 以降は web。`STYLE` を触るのは T3 だけ。

| task | 内容 | 触る file | 単独で merge したとき |
|---|---|---|---|
| T1 | `GET /v1/performance` の資金推移の部 (束ね・付け直し・最大 DD・印・`other_sources`・cache・上限)。`strategies` は空で返す。endpoint の本数の pin と運用の文書 | `S/ops/performance_view.py` (新)、`S/ops/service.py`、`S/ops/api_server.py`、`S/ops/events.py`、`tests/ops/` | API が増えるだけ。画面は変わらない |
| T2 | 戦略別の集計 (帰属・対象・指標・R の関数の切り出し・しきい値) | `S/ops/performance_view.py`、`S/backtest/metrics.py`、`tests/ops/`、`tests/backtest/` | 応答に `strategies` が入る。画面は変わらない |
| T3 | web の route・nav・タブ・client、SVG の部品と `STYLE`、資金推移のタブ。hash の 2 行 | `A/web/routes.py`、`tabs.py`、`request.py`、`render.py`、`headers.py`、`perf_pages.py` (新)、`svg.py` (新)、`A/api_client.py`、`T/web/` | Performance が開き、資金推移が見える。戦略別のタブは「準備中」を出さず、タブ自体をまだ足さない |
| T4 | 戦略別のタブ (表・累積損益の図・固定文) | `A/web/perf_pages.py`、`tabs.py`、`T/web/` | 戦略別が見える |
| T5 | 実ブラウザの受入 (§4)。fixture から異常の画面を描く手順を添える | — | — |

- T1 と T2 は直列 (同じ module)。T3 は T1 の後、T4 は T2・T3 の後。
- 実装は、T1・T2 が純 Python の集計、T3・T4 は `T/web/` を踏む。

## 6. 利用者に問う事項

| # | 問い | 推奨 | 理由 |
|---|---|---|---|
| 1 | この段の範囲。(a) 資金推移と戦略別の成績だけ。(b) 加えて「判断による決済と、そのまま SL / TP まで持った場合の R の比較」。(c) 加えて価格チャートへの指標・シグナル・建玉の重ね | (a)。(b) と (c) は別の段 (別チケット) | (b) は OHLCV を再生する反実仮想で、資金推移・集計とは別の仕組み (約定の規則の再利用、同じ足で SL と TP の両方に届いたときの扱い、価格の欠け、決済後どこまで追うか) を決める必要がある。対象 (判断による決済) は今 20 件に満たず、結論を出すには少ない。(c) は拡大・移動が要るので JS を入れる段になり、CSP と配布の設計が別に要る。(a) だけなら daemon 1 本 + 画面 1 つで閉じる |
| 2 | 少ない件数の表示のしきい値。5 件未満は率を出さない、30 件未満は「(参考)」 | このとおり | 30 は backtest が「評価できる」とする件数と同じで、改善・承認の画面と基準が揃う。5 件未満の勝率・PF は 1 件で大きく動き、読む人を誤らせる。今は大半の起動理由が 10 件未満。下の値 (5) は根拠が弱いので、変えたければここで決める |
| 3 | 図の横の目盛りを日付だけ (`MM-DD`、表示 tz) にしてよいか | よい。表と印の時刻は原則どおりの表記 | 原則の表記 (`YYYY-MM-DD HH:MM:SS +09:00`) は 25 文字で、目盛りに並ばない。tz は画面の上の行に出ている |
| 4 | 画面の構成。(a) 1 つの画面に 2 つのタブ (資金推移・戦略別)。(b) 1 つの画面に縦に並べる | (a) | 要望は「資金推移のページに戦略ごとの成績」。同じ画面・同じ期間の選択で 2 つを切り替えられるので要望に合う。縦に並べると図 2 つと表で 1 ページの予算 (64 KiB) の半分を超え、戦略が増えたときに切り詰めが要る。配置の段の「大項目はタブ」とも揃う |
| 5 | 作動の水準の線 (HWM × (1 − 今の閾値)) を出すか | 出す | DD と閾値は status / web で見せる、という前の裁定に沿う。equity との距離が図で分かる。注意は 2 つ: 過去に閾値を変えた分は再現できない (凡例に「今の閾値」と書く)。線を割っても、建てる判断が出るまでラッチしない (F21) ので、線と「✕」の位置はずれる (固定文で説明する) |
| 6 | 平均 R をどちらの定義で出すか。(a) 金額 (手数料込み。backtest・床・承認の画面と同じ)。(b) 値幅 (通知・bot の `pnl_r` と同じ) | (a) | この画面の用途は、戦略の実際の成績を床や backtest の数字と見比べること。(a) なら同じ式で比べられる。代わりに、通知に出た 1 件ごとの `pnl_r` を平均しても画面の値と一致しない (差は手数料の分)。列名に「手数料込み」と書く |

技術で決めたもの (問いにしない): server SVG (§2.1)、daemon 側の集計と `status.read` (§2.2)、束ねの方式 (§2.3)、帰属は建てた判断 (§2.5)、初期 SL の扱い (§2.6)、`STYLE` を 1 本のままにする (§2.9)、図の印は最新 40 まで (§2.10)。

## 7. 残余・やらないこと

- **反実仮想の比較・価格チャートの重ね**: この段に入れない (§6 の問い 1)。裁定が (a) なら、チケットを 2 つに分けて起票する。
- **JS**: 入れない。値の読取り (hover)・拡大は無い。
- **kill switch の履歴の欠け**: 10 月上旬より前と、event が欠けた世代は印が出ない。埋め戻しはしない (材料が無い)。確かな履歴が要るなら、ラッチと解除を台帳に書く変更が別に要る。
- **過去の閾値**: 残っていないので、作動の水準は今の閾値で引く。
- **paper / live の分離**: 戦略別は分けられない (§2.7)。live を入れる設計の必須の項目にする。
- **版ごとの成績**: 同じ名前の plugin は版をまたいで 1 行。
- **日次の集計表**: 作らない。全期間の走査が重くなったら別チケット (§2.10)。
- **CLI (`afx ctl`) と bot**: この集計を出さない。
- **スワップ**: 損益の合計に含めない (F17。今は全件 0)。
- **R の 2 つの定義の統一**: しない。通知の `pnl_r` は値幅のまま。この画面は金額の定義で、列名に書く。
- **ほかの画面の桁揃え・右寄せ**: しない。この画面の表だけ。
- **SVG の `forced-colors`・印刷**: 対応しない。
- **承認詳細の予算**: `STYLE` の増分 775 byte で申告の切り詰めが早まる例がある。予算の見直しは `web-approval-detail-budget-review` で扱う。

## 付録 A. 依頼の前提と確かめた結果

| # | 依頼の前提 | 結果 |
|---|---|---|
| 1 | snapshot は約 6 万行で、そのままでは応答の上限を超える | そのとおり (F1)。1 時間幅でも全期間は図だけで約 60 KiB (§2.10)。列を 280 以下に束ねる。HWM は付け直しと入出金の行で束を割って守る (§2.3) |
| 2 | ラッチの履歴が DB にあるか | 一部だけ。今の状態は state file、履歴は `ops_events` (best effort、10 月上旬以降)、付け直しは台帳 (F7〜F9)。ラッチは線を割った時点でなく、次に建てる判断の時点 (F21) |
| 3 | 閉じた取引は約 20 件 | そのとおり。起動理由は数種で、大半が 10 件未満 (F12・F13) |
| 4 | `intent_id` が NULL の注文・部分約定・取消の扱い | 今は 0 件。扱いは §2.5 に決めた |
| 4 | `trigger` が NULL の mission 約 600 件の扱い | improve と reflection の mission で、取引の帰属に関わらない (F11) |
| 5 | `orders.stop_loss` は後で動く可能性がある | 今は動かす経路が無い。payload と全件一致 (F14)。食い違えば R を不明にする守りを入れる (§2.6) |
| 6 | 反実仮想は重い | そのとおり。別の段を推奨 (§6 の問い 1) |
| 7 | 新しい読取 endpoint が要るか | 要る (1 本)。集計は daemon 側、30 秒の cache (§2.2、§2.10) |
| 8 | paper と live が混ざるとき | 今は live の記録を書く経路が無く、`orders` には区別の列も無い (F6)。資金推移は混ぜずに警告、戦略別は残余 (§2.7) |

## 付録 B. 文書とコードの食い違い

コードの書き方に従った。見つけたものは次の 8 件。

1. チケット `web-ui-charts-equity-indicators` の「読み取り専用、JS なし」。コードには書込みの画面がある (`A/web/write_pages.py`、`A/web/runtime.py:86`)。JS が無いのはそのとおり。
2. `docs/operations/ops-api-2026-10-09.md:60` は `ops-web-write` について「この principal の鍵を web が読む配線は、この版では未提供」。コードは設定の `write` が真のとき `WebWriteClient` を使う (`A/web/runtime.py:86`、`A/isolation/startup.py:884`)。
3. `2026-10-04-ops-api-design.md` §4.1 は、外部主体 (`X-Afx-Asserted-Actor`) を申告できるのは `ops-bot` だけとする。コードは `ops-web-write` にも許す (`S/ops/contracts.py:126-129`)。
4. `2026-11-18-web-css-design.md` §2.2・§2.4・付録 D は、カードの見出しを `<b>` と `.kpi b` とする。コードは `.kpi strong` (`A/web/render.py:72`)。
5. 同 付録 D は `STYLE` の下書きを 2,957 byte とする。今のコードは 2,962 byte。
6. この設計の下書きの依頼は「`trigger` が NULL の mission (約 600 件)」を取引の帰属の問題として挙げる。コードと DB では、NULL は `loop` が trade 以外の mission だけ (`S/store/db.py:666` のコメント、F11)。
7. 同じ依頼は「`orders.stop_loss` は後で動く可能性がある」とする。コードに動かす経路は無い (F14)。
8. チケットは「平均 R」を 1 つの値として書く。コードには R の定義が 2 つある (F15)。文書と食い違うのではなく、文書がどちらかを決めていない。この設計は金額の定義にした (§2.6)。

## 変更履歴

| 日付 | 版 | 変更 | 理由 | commit |
|---|---|---|---|---|
| 2026-11-19 | C0 | 初版 (下書き) | 利用者の方針 (配置 → CSS → チャート。資金推移のページに戦略ごとの成績) | — |
