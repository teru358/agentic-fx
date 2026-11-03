# 内部時刻を UTC に統一し、broker 時刻と市場境界を NY 規則で換算する — 設計 C0

**版:** v0.4.2 C0 (AC 43 の週の境界は FX だけ。exit 5 の復元対象を manifest で特定。設計レビュー 3 周目 sol の新指摘 Critical 1 / Important 3 と、裁定を越えた 3 点の裁定を反映。T1 + T2 の範囲と AC 1〜16・45 は v0.3 から変えない)
**チケット:** `mt5-bridge-applies-current-offset-to-all-history` (高、不具合) / `market-hours-fixed-21utc-ignores-dst` (高、不具合)。関連 `datafeed-no-tick-minutes-counted-as-gap` (高、不具合、主因が本束)、`display-timestamps-in-local-timezone` (中、表示だけで本束と独立)。
**期限:** `core/market_hours` の改修と稼働中の切替 (cutover、§3.7) は **2026-11-06 (金) 20:30 UTC より前**。現行コードは冬の間ずっと 1 時間ずれており、日次のずれは 11-02 (月) 20:55 UTC から既に出ている (§0.1-2)。
**正本の関係:** 読み取り tool の gap 方針は `2026-11-02-read-tool-gap-annotate-design.md`、econ の欠損分類は `2026-11-01-econ-window-signal-authority-shadow-design.md` §3.5。gap-annotate の規則は変えない。econ §3.5 には第三分類「既知閉場」を足すよう求める (§3.6)。報告 `tmp/review-notick/dst-probe.md` (probe) を実測の起点とし、履歴の検証は §0.2 で本書が閉じる。

## 0. 目的と非目標

**目的:** (a) 内部の時刻はすべて UTC にし、broker サーバ時刻と市場の日次境界の 2 つの換算だけを **tz 規則** (America/New_York、サーバ = NY + 7h、境界 = NY 17:00) で書く。固定の offset 値 (+3 / 21:00) は持たない。(b) その規則で bridge の足ごとの換算、`market_hours` の全関数、過去データの誤ラベルを直す。(c) 誤ラベルのデータは **dry-run の報告 → 利用者承認 → apply** の順でだけ書き換える。(d) 稼働中の daemon を旧規則から新規則へ、資金保護を緩めずに切り替える (§3.7)。

**非目標:** 表示時刻のローカル化 (別チケット) / 銘柄別の取引時間 (CFD は 1 時間遅れで開く、§6) / 早期閉場 (12/24 の金・指数) / 休場日の追加 (12/25 と 1/1 のまま) / 記録済み backtest の書き換え (J2) / `canonical_fixture` の週末マスクの変更 (§3.5) / 他の利用者の DB の repair と cache の誤り行の汎用検出 (v1 はこの DB の既知 fingerprint と定数の鍵集合だけ、§3.4。汎用化は別 version) / 切替後の旧版への rollback (forward-fix だけ、§3.7)。

### 0.1 前提の再検討

| 前提 | 検討 | 結論 |
|---|---|---|
| 「週末境界と日次ロールは 21:00 UTC 固定で DST に追従しない」 (`market_hours.py:3-4`、2026-09-06 の実測、閉鎖チケット `market-calendar-broker-mismatch`) | 実測期間 2025-05〜2026-09 の 5m 履歴は全 69 週が金 20:55 → 日 21:00 (ラベル) で揃う (§0.2)。だが履歴は取り込み時の bridge が +3 を全期間に当てて作ったラベルで、ブローカーの時計ではない | **誤り。** ブローカーは NY 17:00 = サーバ 00:00 で、夏 21:00 UTC / 冬 22:00 UTC。実測の「通年 21:00」は取り込みの換算が固定だった産物 |
| 「21:00 固定の誤りは冬に 1 時間ずれるだけで無害」 | 冬 (2025-11-02〜2026-03-06、2026-11-01〜2027-03-12) は、日曜 21:00-22:00 UTC を**開場中**と見る (実際は閉場、ここで 15m が取れず 2026-11-01 に hold が 46 件中 24 件)。金曜 21:00-22:00 は閉場と見る (実際は開場)。月〜木の日次区切り、horizon=day の強制決済 (`next_rollover - 5 分`)、日次損失の起点 (`daily_start_equity`)、signal の日次上限が 1 時間早い | **有害。** 日次の切替は 2026-11-02 (月) 20:55 UTC の day close から既に 1 時間早い。週境界の誤りは金曜 21:00 UTC と日曜 21:00 UTC に出る |
| 「履歴と `market_hours` は別々に直せる」 | 誤ラベルの履歴 (冬のバーが 1 時間早い) と固定 21:00 の `market_hours` は**互いに整合していた** (どちらもサーバ 00:00 = 21:00)。片方だけ直すと、冬の backtest で日曜 21:00-21:55 ラベルの 12 本が閉場扱い、金曜 21:00-21:55 は開場なのに足が無い。2026-09-06 には同じ形 (開場なのに足が無い) で replay が ValueError で全停止した (同チケット) | **同じ窓で直す。** `market_hours` を先に live にし、履歴の repair は承認後すぐ続ける。間は冬の境界が 1 時間ずれることを承知し、replay が止まらないことを AC 16 で確かめる (§6、J1) |
| 「切替は daemon の再起動だけでよい」 | day 建玉の期限 (`scheduler.py:1248-1252`) も日次損失の起点 (`accounting.py:50-68`) も保存値でなく毎回の再計算。再起動した瞬間に既存の建玉と当日の基準が新規則で読み直される。21:00-22:00 に約定した day 建玉は期限が約 23 時間前に動き、20:55 から決済に失敗し続けている建玉は 1 時間延びうる。22:00 の新しい区切りで、旧規則なら当日に数えていた 21:00-22:00 の損失が消える | **専用の切替規則を置く** (§3.7)。day 期限は旧新の早い方、日次損失の基準は旧新の厳しい方、signal の起点は旧新の早い方を、次の新規則の境界まで |
| 「2026-11-01 の 15m 欠損は日曜明けの tick 薄さ」 (gap-annotate 設計 §1) | 21:05-22:00 UTC は閉場だった。現行 `market_hours` が開場中と見たから「欠損 5 本」になった。薄商いの欠損は別に実在する (平日最大 9 分) | gap-annotate の動機の主因が消える。それでも欠損の注記は残す価値がある (§3.6) |
| 「bridge は offset の測定を賢くすればよい」 | 測定は tick の古さに弱い (週末に -16200〜-43200 を採用、2026-10-03)。切替直後は最大 5 分、旧 offset のまま足を ingest する。測定を正にする限り、窓は残る | **測定をやめ、規則を正にする。** 測定は規則の検査にだけ使う |
| 「規則の外れは警告で足りる」 | `/server-time` は本体から呼ばれない (`src` で grep 0 件)、`mt5_health` (`datafeed/sources.py:131`) も呼び出し元 0 件。1 回のログは rotation で消え、外れたまま誤ラベルを無期限に ingest する | **外れたとき、および検査できていないとき (未測定・300 秒より古い測定) は時刻つきの市場データを止める** (§3.2)。afx 側は既存の outage 状態機械で新規リスクを止める |
| 「切替に失敗したら旧版へ戻せばよい」 | T_c の後に旧 21:00 規則へ戻すと、日次基準と day 期限が旧規則だけになり、冬は日次損失の fail-open と期限の延長が起きる。旧版は `data_migrations` の marker も DB の版も読まない (`store/db.py` は `CREATE TABLE IF NOT EXISTS`、`store/state.py:89-130` は未知の key を無視、settings の版の比較は `run_init` の `service.py:607-617` だけで daemon の起動には無い) ので、旧版の起動を後から止める仕組みは作れない | **T_c の後は rollback しない。** 修正は新規則を保った forward-fix だけ。緊急時は afx を止めて手で確かめる (§3.7) |
| 「cache の誤り行は repair まで残る」 | cache の保持期間の cutoff は daemon の起動時刻 − 30 日で固定され (`service.py:1650-1655`、`store/ohlcv.py:62-75`)、maintenance がそれより古い行を消す (`store/ohlcv.py:209-218`)。今の daemon の起動は 2026-11-01 10:22:10 UTC (`systemctl --user show afx`) で、cutoff 2026-10-02 10:22:10 と cache の最古の行 (1m 10:23 / 15m 10:30 / 1h 11:00) が合う | 2026-11-02 11:59 UTC 以後のどの再起動でも、C1 (2026-10-02 21:00〜10-03 11:59) は保持期間の削除で全部消える (R1 の切替の再起動を含む)。C3 は 11-29 21:45、C2 は 12-01 21:04 より後の再起動で消える。repair は「残っている行は定数と一致、欠けは保持期間で説明できる」で扱う (§3.4) |
| 「cache の誤り行は 11/01 21:01-21:04 だけ」 (probe) | cache 全数を閉場時刻と 1m の集約で突き合わせると 3 種ある (§0.3)。cache の source は今は `mt5-live` だけだが、表は 3 source を許す (`store/ohlcv.py:19`) | 3 種とも dry-run の対象。検出は `mt5-live` に限る |
| 「pending の候補は誤ラベルの履歴で評価された」 | pending は 1 件 (approval 78 / archive 323、`us500_hourly_range_surprise`、2026-10-31)。**indicator** で、payload の `eval_source` / `in_sample` / `holdout` は null。indicator gate は合成 fixture (`indicator_pipeline.py:183-186,302-304`) だけを読み、履歴を読まない | strategy の pending だけが誤ラベルの数値を持つ。扱いは J2 |
| 「ブローカーの DST は US に従う」 | 秋は bridge の測定が 2026-11-01 22:05:45 UTC に 10800 → 7200 に変わった (`bridge.log:141`)、EU 切替 10/25 は変化なし。春と 2025 年秋は US500 の現物開始 (§0.2-6) が US 切替後・EU 切替前の週も 13:30 ラベルに立つ (EU 規則なら 12:30) | **規則は US。春も独立の錨で強い。** 2027-03-14 の実機確認は §6 に残す |

### 0.2 履歴の検証 (実測、2026-11-02、`data/agentic.db` を `mode=ro`、bridge は GET のみ 7 回)

**結論:** `ohlcv_history` (source `mt5`) の冬の足は、真の UTC より **1 時間早い** ラベルで入っている。夏の足は正しい。

| 履歴ラベルの範囲 | ずれ | 真の UTC |
|---|---|---|
| 〜 2025-10-31 20:55 (夏) | 0 | ラベルのまま |
| 2025-11-02 21:00 〜 2026-03-06 20:55 (冬、FX。指数・金・原油は 22:00 から) | 真の UTC = ラベル + 1h | 開場 日 22:00 / 最終足 金 21:55 |
| 2026-03-08 21:00 〜 2026-10-30 20:55 (夏) | 0 | ラベルのまま |

**証拠 (独立な時刻の錨):**

1. **経済指標。** 米 8:30 ET の発表は冬 13:30 UTC、夏 12:30 UTC。履歴で、発表の 5m 足が最大レンジ (または上位 2 本) に入る時刻: NFP 2026-01-09 (冬、2 位)、2026-03-06 (冬)、2025-12-16 (冬、遅延発表)、2026-02-11 (冬、遅延発表)、CPI 2026-01-13 / 02-13 (冬) はすべて **12:30** ラベル (真は 13:30)。夏の NFP 2025-07-03 / 08-01 / 09-05、2026-04-03 / 05-08 / 07-02 / 08-07 も 12:30 で、これは正しい。FOMC 14:00 ET は冬 19:00 UTC: 2025-12-10 / 2026-01-28 が **18:00** ラベル (真は 19:00)、夏の 2025-09-17 は 18:00 で正しい。
2. **株式市場。** US500 の 5m 出来高の立ち上がりは夏冬どちらも **13:30** ラベル。米現物の開始 9:30 ET は夏 13:30 UTC、冬 14:30 UTC。冬は 1 時間早い。
3. **bridge の現在値との突き合わせ。** bridge は今 +2 を全期間に当てるので、冬の足は正しく、夏の足は 1 時間遅れて返る。USDJPY 5m で 4 窓 (2026-01-09 NFP 窓 36 本 / 2025-12-08 72 本 / 2025-11-02 初回開場 30 本 / 2026-07-02 夏の対照 24 本) を履歴と OHLC 完全一致で突き合わせると、**全窓で「bridge ラベル − 1h = 履歴ラベル」が 100% 一致**。この一致は夏冬どちらも成り立つので向きは決まらない。向きは 1. と 2. で決まる。この照合が示すのは「履歴は +3 固定で取り込まれた」という出所で、規則の正しさではない (規則は 1. 2. 6. が示す)。
4. **構造的整合。** 履歴の 69 週すべてが 20:55 → 21:00 に揃う。冬は真の 21:55 → 22:00 になるはずで、取り込みが +3 固定だったという一つの原因で全部説明できる。`mt5_import.py:65-86` は bridge のラベルを換算せずそのまま保存する。履歴 FX の閉場時刻 (NY 規則) にある行は 1 銘柄 240 本 (日曜 21:00-21:55 × 18 週 + 12/25 と 1/1 の再開 21:00-21:55 × 2 = 20 回 × 12 本)。USDJPY の 1m 履歴 (2026-05-28〜09-02) はすべて夏で影響なし。15m の履歴は 0 行。
5. **休日の根拠も同じ向き。** `market_hours.py:27-29` が根拠にした「12/24 21:00 UTC に閉場し 12/25 21:00 UTC に再開」(2025-12) は履歴ラベルで、真は **12/24 22:00 → 12/25 22:00 UTC**。XAUUSD の 12/24 の最終足は 17:40 ラベル (真 18:40、早期閉場)。
6. **US 規則の独立な錨 (春・秋の境目)。** 現物開始 9:30 NY はサーバ 16:30 = 取り込みラベル 13:30 で、サーバ = NY + 7 のときだけ一定になる。EU 規則 (サーバ = 欧州 +2) なら US 切替後・EU 切替前の週は 12:30 ラベルに出る。判定は「13:30 ラベルの出来高が直前 6 本平均の 2 倍以上」。2026-03-09〜03-27 の 15 営業日すべてで成り立ち、12:30 では 0 日。2025-10-27〜10-31 の 5 日すべてで成り立つ (12:30 の 2 日は 8:30 ET の指標)。冬のブロックの 18 週 (2025-11-03〜2026-03-02) は全週で 3 日以上成り立ち、14:30 ラベル (サーバ = UTC + 3 固定なら冬はここ) では 0 日。
7. **境目の連続性。** EURUSD 5m は 2026-03-08 21:00 → 03-09 13:30 ラベルが 198 本、2025-11-02 21:00 → 11-03 13:30 も 198 本で穴も重なりも無い。開場中に offset が変われば 1 時間の穴か重なりが出るので、ブロックの両端は日曜の開場で切れている。

**影響範囲:** 冬のブロックは 5m、6 銘柄、計 146,632 行 (EURUSD 25,344 / USDJPY 25,344 / JP225 23,906 / US500 23,923 / USOIL 23,987 / XAUUSD 24,128)。履歴の期間は USDJPY の 5m が 2025-05-06〜2026-09-04、他 5 銘柄が 2025-07-01〜2026-10-30。ブロックの前後に衝突しうる行は無い (前の最後が 2025-10-31 20:55、後の最初が 2026-03-08 21:00)。証明できない週は無い (6. で 18/18)。

### 0.3 cache の誤り行 (実測、`ohlcv_cache` USDJPY `mt5-live`、閉場判定は NY 規則、1m の集約と突き合わせ)

cache は今 `mt5-live` だけ (1m 31,053 / 15m 2,080 / 1h 519 行)。閉場時刻にある行は 1m 904 / 15m 64 / 1h 16 で、下の 3 種で全部説明できる (説明できない行 0)。

| 種別 | 範囲 | 行数 | 出所の証拠 |
|---|---|---|---|
| C1 週末の偽の足 | 2026-10-02 21:00 〜 10-03 11:59 (閉場の連続した塊) | 1m 900 / 15m 60 / 1h 15 | 週末に tick が古いまま測定が 30 分ごとに -1800 秒ずつ進み (bridge.log 2026-10-03)、金曜 06:00-20:59 の足が先に貼られた。塊のうち 900 分前の開場行と OHLC が一致するのは 1m 633 / 15m 39 / 1h 7。元行が cache に無い (cache 開始 2026-10-02 10:23 より前) のが 1m 263 / 15m 18 / 1h 5、塊の末尾で一致しないのが 1m 4 (11:56-11:59) / 15m 3 / 1h 3 |
| C2 切替直後の旧 offset | 2026-11-01 21:01-21:04 (1m)、21:00 (1h) | 1m 4 / 1h 1 | 切替 22:00 UTC の後 22:05:45 まで +3 のまま ingest。1h 後の同ラベル (22:01-22:04、22:00) に開場の行がすべてある。OHLC が一致するのは 1m 3 本 (残り 1 本と 1h は形成中の足) |
| C3 切替後の再取得で遅れたラベル | 2026-10-30 15m の 20:30〜21:45 | 15m 6 (20:30 / 20:45 は開場、21:00-21:45 の 4 本は閉場) | 11/01 22:05 以降に 10/30 の尾を 15m で取り直し、+2 で 1 時間遅れのラベルが `DO UPDATE` (`store/ohlcv.py:140-147`) で入った。6 本とも「ラベル − 1h の 1m 集約と一致し、同ラベルの 1m 集約とは一致しない (閉場の 4 本は同ラベルに 1m が無い)」。**20:30 / 20:45 は正しい行を誤った値で上書きしており**、同ラベルの 1m が 15 本揃うので置き換えられる |

C3 は gap-annotate 設計 §7-6 (10/30 の 15m cache に 21:00-21:45 の 4 本) の正体でもある。

**C1 の行単位で出所が取れない行 (1m 267 / 15m 21 / 1h 8)。** 900 分前の行との一致だけで数えると、塊の中に説明できない行が残る。どれも次の 2 つのどちらかで、塊の一部として C1 の定数集合 (§3.4) に含める。

- **元行が cache に無い (1m 263 / 15m 18 / 1h 5)。** 塊の先頭 (1m は 10-02 21:00〜10-03 01:22、15m は 21:00〜01:15、1h は 21:00〜01:00) で、900 分前のラベル (金曜 06:00〜10:22 / 06:00〜10:15 / 06:00〜10:00) が各 interval の cache の最古の行 (1m 10:23、15m 10:30、1h 11:00) より前にあり、保持期間の削除で既に消えている。
- **塊の末尾で値が違う (1m 4: 11:56〜11:59、15m 3: 11:15〜11:45、1h 3: 09:00〜11:00)。** 900 分前の行 (金曜 20:56〜20:59 など) は cache にあるが OHLC が違い、cache の他のどの行とも一致しない。偽の側どうしは整合する (1h 11:00 の volume 2981 = 15m 11:00〜11:45 の和、15m 11:45 と 1h 11:00 の close = 1m 11:59 の close 157.819) ので、別の取得時点の写しと見られるが、写しの元は残っていない。

どちらも土曜 (NY 規則で終日閉場) のラベルで、broker は閉場中に足を作らない。塊は 1m・15m・1h とも 10-02 21:00 から interval 刻みで穴なく続く (1m 900 / 15m 60 / 1h 15)。出所の一致が取れなくても、閉場中の偽の足であることは変わらない。

## 1. 現物の確認 (コード)

| 箇所 | 現物 |
|---|---|
| `core/market_hours.py:19` | `_ROLLOVER_UTC = time(21, 0)`。`:30` 祝日ラベルは `now + 3h`、`:33` 金 `t >= 21:00` 閉場、`:37` 日 `t < 21:00` 閉場、`:44-45` / `:52-53` は `hour=21` のリテラル (`_ROLLOVER_UTC` を使わない)。`:69-74` `session_start` は境界から 24h ずつ遡る。`:108-119` `is_friday_after` だけが NY 現地で判定 |
| `plugin/canonical_fixture.py:30-38` | 合成 5m の週末マスクが `hour >= 21` / `hour < 21` / `hour == 21` (固定)。gate が使う hash は 5m でなく `canonical_bars(timeframe)` の集約後 (`plugin/indicator_pipeline.py:183-186`)。timeframe は `settings.datafeed.decision_timeframes[0]` (`:302-304`) |
| `mt5_bridge/mt5_client.py:49-67` | `snap_server_offset`: tick - 時計、±12h 棄却、30 分丸め、残差 120 秒 |
| `mt5_client.py:334-382` | `_ensure_offset_locked`: 300 秒間隔 (`:36`) で再検出、棄却されたら直前値。**測定が換算の正**。`server_offset.json` に永続 |
| `mt5_client.py:71-79` | `to_server_datetime` (+offset) / `from_server_epoch` (-offset)。呼び出し時点の 1 値 |
| `mt5_client.py:420,500,559,616-630` | quote / position / deal / `copy_rates_range` の全部が同じ 1 値で換算 |
| `mt5_client.py:256-271` / `server.py:297-309` | `/server-time` (`server_offset_sec`、`offset_source` = live / cached)。`ServerTimeUnknownError` は各 endpoint で 503 (`server.py:308-387`)。`/health` (`:277-287`) は無認証 |
| `datafeed/sources.py:131-136` | `mt5_health` は呼び出し元 0 件。`mt5_bars_range` / `mt5_quote` は `raise_for_status` |
| `mt5_bridge/pyproject.toml` | 依存に `tzdata` が無い。bridge は Wine 上の Windows Python で動き、`zoneinfo` に OS の tz データが無い |
| `mt5_bridge/tests/` | 11 ファイル。別 venv、`cd mt5_bridge && .venv/bin/python -m pytest -q`。`test_server_time_offset.py` 24 件が緑 |
| `backtest/mt5_import.py:50,65-86,188-` | 取り込みは 1m / 5m / 15m を受ける (`_INTERVAL_SECONDS`)。bridge のラベルを UTC とみなして保存 (換算しない)。既存行と OHLC が違えば `ImportConflictError` で rc=3 (`backtest/cli.py:268-279`)。`store/ohlcv.py:308-` `import_history_bars` は `INSERT OR IGNORE`。`store/ohlcv.py:125-148` `upsert_cache_bars` は `DO UPDATE`。`data_migrations` に冪等 marker の前例 (`ohlcv_cache_purge_pre_closed_v1`、`policy_directives_managed_v1`) |
| `store/ohlcv.py:19-22` | `LIVE_SOURCES = {yfinance, twelvedata, mt5-live}`、`IMPORT_SOURCES = {dukascopy, mt5}` |
| `core/accounting.py:50-68` | `daily_start_equity` は `trading_day_start(now)` 以降の最初のスナップショットを起点にする (無ければ 48h 以内の直前値、それも無ければ None = fail closed)。保存値でなく都度計算。呼び出しは `executor.py:614,905` (risk gate の文脈) と `loops/summary.py:60` (LLM への日次損益表示) |
| `core/risk_gate.py:179-185` / `:187-195` | kill switch は latch と HWM 比の DD (境界を使わない)。日次損失は `daily_start_equity` 比 (境界を使う)。latch は `executor.py:645-648` で立ち、人間の reset (`store/state.py:395-406`、generation 照合) でだけ戻る |
| `core/executor.py:681` / `scheduler.py:954-` | limit の `expires_at` は `now + expires_in` の絶対時刻、`_expire_limits` はそれと `now` を比べるだけ (境界を使わない) |
| `core/scheduler.py:170-179` / `:947-952` | 閉場の tick で `_was_open is not False` なら `_on_market_close`。`_was_open` はメモリだけで再起動後は None。`_on_market_close` は TRADING のときだけ PENDING_FILL を全取消 (`market_close`)。本機は今 `learning` (`data/state/app_state.json`) で取消は起きない |
| `core/scheduler.py:1228-1252` | day close の期限 = `next_rollover(filled_at or created_at) - 5 分` を注文ごとに毎 tick 再計算 |
| `store/missions.py:293-307` | signal の日次上限 = `trading_day_start(now)` 以降の `signal%` 行数 |
| `plugin/strategy_gate.py:598-624` / `:626-646` / `:650-` | 配備中の版があれば `_compare_with_deployed` が**同じ `now`・dataset で再実行**して比べる。保存済み run は読まない。`no_strategy` は配備版が無いときの定数行 (trades 0) |
| `plugin/switch.py:1454-1466,2423-2428` / `plugin/approval.py:525` / `loops/improve_loop.py:2267` | approval payload の dataset の名乗りは `eval_source` / `base_interval` / `eval_timeframe` (strategy だけ、indicator は null)。承認は `switch.py:1805` `approve_candidate`、ローカル承認は `:2349` `bless_candidate`。配備操作は `deploy_guard` (`:953-987`) を共有で取る |
| `cli_surface.py:217-222,303-313` | `history import / compare / coverage` は最上位レーン (`backtest/cli.py`)。`ctl approve / reject / retry` は `daemon_required`。`plugin bless` は最上位レーン |
| `config.py:67-85` | コメント「クローズは 21:00 UTC 固定 (NY 17:00 EDT / 16:00 EST)」、`friday_swing_cutoff_ny` の validator は `< 16:00`。`config/settings.yaml.example:20` に同じ文言。NY 17:00 EST は 22:00 UTC で「16:00 EST」は誤り |
| `service.py:1650-1655` / `store/ohlcv.py:62-75,197-218` | cache の保持期間の cutoff は daemon 起動時の `clock.now()` − `cache_retention_days` (30) で固定。maintenance ごとに `bar_time < cutoff` を消す (起動し直すまで cutoff は進まない) |
| `datafeed/outage.py:23-57,297-372,550-562` / `datafeed/ingest.py:235-245` | hard key の取得失敗・空応答は 1 回で `degraded` (`immediate`)、取らなかった key は失敗に数えない (報告の契約)。停滞は `next_bar_confirmation(watermark)` を過ぎた最初の tick で、flat なら `restricted`、建玉があれば `degraded`。ingest は次の足の確定時刻 (日曜の開場なら 22:01:30Z) に初めて取りに行き、無ければ指数 backoff |
| `store/approvals.py:166-262` | `apply_decision(..., commit=False)` は承認行の CAS・backlog の遷移・archive の確定を呼び出し側の transaction の中で行う |
| `service.py:587-617` (`run_init`) / `store/state.py:89-130` / `store/db.py:305-307,1572-` | settings の版の比較は `afx init` の非対話の経路だけ。`state.json` の未知の key は無視。DB は `CREATE TABLE IF NOT EXISTS` で、旧版は新しい marker を読まない |
| `tmp/wt/econ-b1` (未 merge、econ 束 1 T2a-2) `core/scheduler.py:432-497` / `signal_admission.py:250-257` | 日次 report hook: `latest_trading_close` は境界から 24h ずつ遡る (`:463`)、marker は `alert_state` `daily_report.state` に閉じた境界の ISO 文字列、`last >= day` の文字列比較 (`:496`)。wait-budget の運用日 key は `trading_day_start(now).isoformat()` |

## 2. 利用者から見た動き

- **冬の週末。** 2026-11-06 (金) は 21:55 UTC まで取引でき、22:00 UTC に閉場する。2026-11-08 (日) は 22:00 UTC に開場する。21:00-22:00 UTC は閉場として扱われ、欠損にも outage にもならない。春 (2027-03-14) からは再び 21:00 UTC。
- **日次の区切り。** 冬の月〜木は 22:00 UTC に取引日が切り替わる。horizon=day の強制決済は 21:55 UTC (夏は 20:55)。日次損失の起点、signal の日次上限、日次 report が同じ 22:00 を使う。kill switch と limit の期限は区切りに依らず不変。
- **切替の日。** 新版の初回起動で切替時刻が記録され、activity に既存の day 建玉の期限 (旧 / 新 / 適用) と日次基準 (旧 / 新 / 適用) が 1 行出る。次の 22:00 UTC までは旧新の厳しい方で動く (§3.7)。切替の後は旧版に戻さない。おかしければ afx を止めて手で確かめ、新規則を保った修正版で起動し直す。
- **bridge。** いま 2026-10-30 の最終足を取ると 20:59 UTC で返る (いまは 21:59)。冬の 2026-01-09 の NFP は 13:30 UTC に出る。DST が切り替わる瞬間 (2027-03-14 07:00 UTC) の直後の足も最初から正しいラベルで入る。bridge は「300 秒以内に受理した測定が規則と一致している」ときだけ足と quote を返す。食い違えば 503 `time_rule_mismatch` (afx は既存の価格源不通と同じく新規リスクを止め、規則に 3 回続けて一致すると戻る)。測れていない間 (起動直後、閉場中、tick が古い間) は 503 `time_rule_unverified` で、afx はそれを失敗に数えず、足が来ない状態として既存の停滞の規則で待つ。週末は `afx history import` もできない。日曜の開場直後は最初の新しい tick を測るまで待つ。
- **過去データ。** `afx history repair-time` が dry-run の報告と plan を出す。承認後に `--apply --plan <file> --confirm <hash>` で履歴の冬の 146,632 行を +1h し、cache の誤り行を直す。apply は daemon を止めて行い、直前に DB の複製を作る。v1 はこの DB の既知の状態だけを扱い、違う DB では何もせず止まる。
- **backtest。** repair 前は冬の区間の境界が 1 時間ずれたまま。repair 後は冬の NFP が 13:30 UTC に出る。記録済みの `backtest_runs` は消えない。承認待ちの strategy 候補の扱いは J2。

## 3. 規則

### 3.1 時刻規則 (UTC 内部、換算は 2 つ)

固定値の源は 3 つの定数だけ: `NY = ZoneInfo("America/New_York")`、`SERVER_AHEAD_OF_NY = 7h`、`ROLLOVER_NY = time(17, 0)`。

1. **broker サーバ時刻 → UTC** `server_epoch_to_utc(server_epoch: int) -> datetime` (aware UTC)。`server_epoch` は MT5 が返すサーバ時刻のエポック秒。`datetime.fromtimestamp(server_epoch, UTC)` の壁時計から 7h 引いた値を NY の壁時計として解釈し、UTC にする。逆 `utc_to_server_epoch(utc: datetime) -> int` は aware UTC だけを受け (naive と UTC 以外の aware は `TypeError`)、`utc.astimezone(NY)` の壁時計 + 7h をエポック秒にする。
2. **市場の日次境界 → UTC** `session_boundary_utc(day: date) -> datetime`: NY の `day` の 17:00 を UTC にする。日次の境界はすべてこの 1 つから作る (§3.3)。

主: `core/fx_time.py` (新規)。bridge は別 package で `agentic_fx` を import できないので、同じ規則を `mt5_client.py` 内に複製する。一致は**同じ金表**で守る: `mt5_bridge/tests/fixtures/dst_rule_table.json`。値は実装から生成せず、tz の規則から手で固定した期待値を書く (レビュー対象のリテラル)。必須の件: 夏冬の通常日、3 つの切替 (2026-03-08、2026-11-01、2027-03-14) の前後 1 分、二重の時刻、存在しない時刻、金曜の閉場と日曜の開場の前後 1 分 (夏冬)、12/24-12/26 と 12/31-1/2。bridge 側の test と本体側の test がどちらもこの表を読む。

**曖昧な時刻。** DST の切替は日曜 NY 02:00 (閉場中)。壁時計 (NY = サーバ − 7h) が秋は 01:00-01:59 で二重、春は 02:00-02:59 で存在しない。判定は fold だけに頼らず UTC 往復で行う: fold=0 と fold=1 の UTC が違い、両方とも往復で同じ壁時計に戻れば**二重**、往復で戻らなければ**存在しない**。二重は fold=0 (夏時間側)、存在しないは切替前の offset (fold=0) で換算し、どちらも warning を出す (足は捨てない。ラベルの誤りより欠落の方が気づきにくい)。足がそこに来るのは規則と実機が食い違ったときだけ。

### 3.2 bridge の換算と規則の検査

- `copy_rates_range` は要求の from / to を規則の逆で変換し、返った足を**足ごとに**規則で換算する (`mt5_client.py:616-630`)。quote / position / deal / `/server-time` の時刻も同じ関数を通す (`:420,500,559`)。測定値は換算に使わない。
- **5 分の古い窓は無くなる。** 換算は呼び出しの瞬間の関数で、キャッシュしないから。
- **測定の契機。** 時刻つきの呼び出し (`/quote`、`/ohlcv`、`/positions`、`/server-time`) を受けたとき、規則上その瞬間が開場中で、前回の測定の試み (受理・棄却・失敗を問わない) から 60 秒以上たっていれば、応答の前に測る。専用のタイマーは持たない。測定は probe symbol (既定 USDJPY、`mt5_client.py:166`) と、`/ohlcv/{symbol}`・`/quote/{symbol}` ではその symbol の tick を読み、新しい方の `tick.time` を使う (日次ロールオーバーで 1 銘柄だけ tick が止まっても測れるようにする)。閉場中は測らない (週末の古い tick から -16200〜-43200 を採る経路が消える)。既存のガード (±12h、30 分丸め、残差 120 秒) を通った測定だけを「受理」とし、棄却・tick 取得の失敗は受理の履歴を変えない。`server_offset.json` は読みも書きもしない。
- **状態 (メモリだけ、毎回の判定で決まる)。** `mismatch` = 最後に受理した測定が規則の offset (夏 10800 / 冬 7200) と違う、または最後に違った後で一致した受理が 3 回続いていない。`ok` = `mismatch` でなく、最後に受理した測定が規則と一致し、その受理から 300 秒以内。`unverified` = それ以外 (起動後に受理が無い、最後の受理から 300 秒を超えた。閉場中は常にここ)。`mismatch` は古くなっても `mismatch` のまま (3 回の一致でだけ抜ける、間隔 60 秒以上なので最短 2 分)。`unverified` からは一致する受理 1 回で `ok`。状態が変わるたびに WARNING を 1 行 (同じ状態の間は出さない)。
- **fail closed。** `/ohlcv` と `/quote` は `ok` のときだけ返す。`mismatch` は 503 (detail `time_rule_mismatch`)、`unverified` は 503 (detail `time_rule_unverified`)。測定が要る呼び出しは応答の前に測るので、返る市場データは常に 300 秒以内の受理に裏づけられる。口座・建玉・決済照合の endpoint は止めない。
- **公開。** `/server-time` (200) は `server_offset_sec` (規則から導いた値)、`offset_source="rule"`、`measured_offset_sec`、`measured_at`、`measured_age_sec` (最後の受理から)、`expected_offset_sec` (測定時点の規則の値)、`offset_matches_rule`、`match_streak`、`time_rule_state` を返す (未測定の項目は null)。`/health` (200、無認証) に `time_rule_state` と `measured_at` を足す (秘密は含まない)。
- **afx 側。** `datafeed/sources.py` の `mt5_bars_range` / `mt5_quote` は 503 の detail を型つきの例外にする (固定文言)。`BridgeTimeRuleMismatch` は失敗として数え (`Ingest.last_errors` に固定文言)、既存の outage 状態機械が 1 回で `degraded` に入れて新規リスクを止める (`outage.py:305,358-372`。flat なら健全 3 tick で自動復旧、建玉があれば `afx ctl data resume`)。`BridgeTimeUnverified` は失敗に数えず、その tick は「取りに行かなかった key」と同じ扱い (`IngestTickReport` の契約、`outage.py:23-57`) で、ingest の既存の backoff で取り直す。足が来ないまま確定時刻を過ぎれば既存の停滞式 (`outage.py:550-562`) が flat なら `restricted`、建玉があれば `degraded` にする (broker が足を作らない日次ロールオーバーの数分と同じ規則、閉鎖チケット `outage-stalled-on-broker-daily-rollover-gap`)。新しい通知の経路は作らない。`afx history import` はどちらの例外でも rc=1、0 行で止まる (週末は `unverified` なので import できない)。afx は `/health` を読まない (新しい配線を作らない)。
- **tz データが読めないとき bridge は起動しない。** 起動時に `ZoneInfo("America/New_York")` を読んで 2026-07-01 と 2026-01-15 の offset を自己検査し、失敗なら起動を拒否する (`ServerTimeUnknownError` は規則が常に定まるので不要になり、この起動拒否が後継)。`pyproject.toml` に `tzdata` を足し、`uv.lock` を更新する。

### 3.3 `market_hours` (本体)

すべて `session_boundary_utc` と NY の壁時計から導く。`_ROLLOVER_UTC` と `hour=21` のリテラルは消す。

| 関数 | 規則 |
|---|---|
| `is_market_open(now)` | `local = now.astimezone(NY)`。金 17:00 以降閉、土閉、日 17:00 前閉。祝日: `trading_date(now)` が 12/25 または 1/1 なら閉 |
| `trading_date(now)` (新規) | `(local + 7h).date()` = サーバ日付 = 取引日ラベル。日次の dedupe・report のラベルはこれを使う (§3.6) |
| `trading_day_start(now)` | `local.time() >= 17:00` なら `local.date()`、そうでなければ前日。その日の `session_boundary_utc` |
| `next_rollover(now)` | `trading_day_start(now)` の NY 日付 +1 日 (**日付演算**。UTC の 24h 加算にしない) の `session_boundary_utc` |
| `session_start(now)` | 直近の境界から NY 日付で 1 日ずつ遡り、開場かつ 1 分前が閉場の最初の境界。上限 14 日は変えない |
| `next_expected_trading_time` / `next_bar_confirmation` | 変更なし (`is_market_open` と `next_rollover` を通る) |
| `is_friday_after` | 変更なし (NY 現地)。docstring を「クローズは NY 17:00 で、cutoff はそれより前」に改める |

開場中の日次の境界は常に 24h 間隔。25h (秋、土 17:00 EDT → 日 17:00 EST) と 23h (春) の日は閉場中に入るので、挙動に出ない (AC 8)。

**`friday_swing_cutoff_ny` の上限 `< 16:00` は変えない。** NY 17:00 が正しい境界になると 16:00-16:59 も境界前だが、swing の新規を閉場の 1 時間以上前に止める余白として残す。DST とは独立の保守的な制限で、`config.py:67-68` と `settings.yaml.example:20` のコメントをそう書き直す (「市場クローズは NY 17:00 (夏 21:00 UTC / 冬 22:00 UTC)。cutoff はクローズの 1 時間以上前に限る」)。validator のエラー文は不変。

### 3.4 データ repair (dry-run → 承認 → apply)

**コマンド。** `afx history repair-time --indicator-pending keep|invalidate [--out <plan.json>]` (dry-run、既定) と `afx history repair-time --apply --plan <plan.json> --confirm <plan_hash>`。`--indicator-pending keep|invalidate` は indicator の pending の扱いで必須 (省略は exit 2。`keep` = J2 (a)、`invalidate` = J2 (b))。strategy の pending の invalidate は常に行う。J2 (c) の配備版の再評価は repair とは別の実行・記録項目で、このコマンドの引数でも plan の項目でもない。apply は plan に固定した値を使い、引数で変えられない。**時刻:** dry-run は bridge の検査が要るので開場中 (平日) に、apply は bridge に触れず daemon の停止が要るので閉場中 (週末) に行う。最上位レーン (daemon の状態は要らず、apply は daemon 停止が条件)。`cli_surface.py` に `Group.MARKET` / `Lane.LOCAL` / `Label.CHANGE`、`confirm=True`、condition「apply は daemon 停止中」で 1 行。実装は `backtest/time_repair.py` (検出・plan・apply)、入口は `backtest/cli.py`。plan の既定の置き場は `data/time-repair/plan-<UTC>.json`。

**終了コード。** 0 = dry-run が plan を書いた / apply が完了 / 再実行で migration marker が有り修復後の検証が一致。1 = 前提不成立 (daemon 稼働中で instance lock が取れない、bridge が規則換算でない・不通・`time_rule_state` が `ok` でない・最後の受理から 300 秒を超えた、backup 失敗、空きが DB サイズの 2 倍未満)。2 = 引数誤り。3 = 未知の DB (履歴の fingerprint 不一致、cache の定数集合の不一致、説明できない閉場行、錨の不成立、bridge 照合の不一致)。4 = plan 不一致 (hash 違い、marker が無いのに fingerprint / 対象が plan と違う、dry-run 後・apply 中に DB が変わった)。5 = commit 後の検査失敗、または marker が有るのに修復後 digest・不変条件が一致しない (DB は書き換え済み。daemon は停止を維持し、backup から戻す、下記)。

**v1 の範囲 (既知 fingerprint)。** 他の DB の誤ラベルは取り込み時期の記録が無く、規則だけでは「+3 で入った冬の行」と「+2 で正しく入った冬の行」を区別できない。v1 は次の fingerprint が**この DB と一致するときだけ**動き、違えば exit 3 で何もしない (一般化は T5 で別チケットに起票)。fingerprint (定数として `time_repair.py` に置く): 冬のブロックのラベル範囲 2025-11-02 21:00 〜 2026-03-06 20:55、銘柄別の行数 (§0.2) と最小・最大ラベル、ブロックの範囲にある `ohlcv_history` の interval が 5m だけであること (1m・15m の行があれば明示拒否)、ブロックの全 146,632 行の主キー集合の sha256 `08ce136a3d8c2f98ccae9e039b2a9566559f3a0c5d414b5c57c5b4292ee6fddf` と旧 OHLCV の sha256 `9df5d76f50417d0672c61b7c730d88a2c29657ba7ffa345180f3293c2d8f242b`。

**digest の作り方 (履歴・cache 共通)。** 行を `(symbol, interval, bar_time)` の保存文字列で整列し、主キーは 1 行 `symbol|interval|bar_time|source`、旧値はその後ろに `|open|high|low|close|volume` (Python の `repr(float)`) を足し、`\n` で連結した UTF-8 の sha256。値はすべて 2026-11-02 に `mode=ro` で実測した。

**cache の定数集合 (v1)。** cache の対象は検出の規則とは別に、鍵集合を定数で持つ。どれも `source='mt5-live'`、symbol `USDJPY`。汎用の検出 (別の日付・別の k の偽の足) は v1 に入れず、別 version と利用者承認に分ける。

| 組 | 範囲 (ラベル) | 行数 | 主キー sha256 | 旧 OHLCV sha256 |
|---|---|---|---|---|
| C1 1m | 2026-10-02 21:00 〜 10-03 11:59 | 900 | `6255e3e0eb0d34fb5b3479759d26e1f725440ab31afd0c29740579222ccfd132` | `780b963266c7fd049a7c2b30b64eb5555ba7fc4a12a90fd2119daf8f1a2a7062` |
| C1 15m | 2026-10-02 21:00 〜 10-03 11:45 | 60 | `f9cb1db7aadd38ce8ee2d0b89d9307eafc99bc53cc25f74acdd5992382ce5fa6` | `b42de498fee87089aaa3b5decb8ec46379bbcfd47cd1ae2b9eab6015fdf20565` |
| C1 1h | 2026-10-02 21:00 〜 10-03 11:00 | 15 | `d91a2822cc175d30b7f330917f8b2b99ce9b10d239162ce74b40c08b650e425d` | `7500ba2a673e2c531c81c3882680637d242b77e6710fad7814b754688b4c76c7` |
| C2 1m | 2026-11-01 21:01 〜 21:04 | 4 | `7972b2c141c283c93870ad216ef94fbb92f980ec42871eb3285af993fa3be19e` | `88aae8a3bd45ac8602c613213c89affc4439c5e323df7f1da2817c3161720c8d` |
| C2 1h | 2026-11-01 21:00 | 1 | `54b6ea638368521943d3699dd6d3a3395422768c6595528a782848292c692f6a` | `c7d2bff6916c6adc43615cc0126d0ca4c41e56fa7b64fa056b79603b58d7a274` |
| C3 15m | 2026-10-30 20:30 〜 21:45 | 6 | `6c4f8a938e2b9f61d471c92d42eba91bdedc1e4c451b8a7fd12132683d5ec5d0` | `f52ae8e514046ce1c1a4f782ab389ba0e2fea6915876bf71ac88307fed914a49` |

C1 は k = 900 分も定数で持ち、組が残っているときは「900 分前の開場行と OHLC が一致する行」が 1m 633 / 15m 39 / 1h 7 であることも照合する (§0.3、行単位で出所が取れない 1m 267 / 15m 21 / 1h 8 も組に含む)。**組ごとに全部あるか全部無いか。** 全部あれば 2 つの digest が一致すること。全部無いのは、その組の最後のラベルが同じ interval の cache の最古の行より前、つまり現在の cache window より前で群全体が欠落しているときだけ許す (`absent_before_cache_window`。判定は位置だけで、削除の原因は主張しない。実際には保持期間の削除で消える、§0.1)。一部だけ残る、digest が違う、全部無いのに cache window より前でない、のどれも exit 3。R1 の再起動の後は C1 の 3 組は全部無くなる (§0.1)。

**dry-run の手順。** (1) 履歴の fingerprint と cache の定数集合を照合する。(2) 錨を週ごとに再計算する (§0.2-6 の判定。冬の週は 13:30 ラベルで 2 日以上、14:30 ラベルで 0 日)。成り立たない週は plan の `excluded_weeks` に理由つきで載り、対象と apply 後の検査から外れる (この DB では 0 週)。(3) 修正済みの bridge から照合窓を GET する。bridge の `/server-time` が `offset_source="rule"`、`time_rule_state="ok"`、`measured_age_sec` が 300 以下であることが前提で、照合窓の GET の前後で 2 回確かめる (どちらかで外れれば exit 1。閉場中は `unverified` なので dry-run は開場中にだけ走る)。窓は銘柄ごとに 4 つ (2025-11-02 の初回開場から 2.5h、2026-01-09 12:00-15:00、2026-03-06 の最後の 2h、夏の対照 2026-07-02 12:00-14:00)、計 24 回。冬の窓は「bridge ラベル = 履歴ラベル + 1h」、夏の窓は同ラベルで、本数と OHLC が 100% 一致すること (違えば exit 3)。これが「この行は +3 固定で取り込まれた」という出所の証明。(4) 対象を数える (下表)。(5) plan を書く。DB には書かない。

**対象と修復。** cache の対象は **`source = 'mt5-live'` の定数集合だけ**。他の source の行は読みもしない。規則の検査は定数集合の正しさの裏づけで、規則が新しい行を対象に足すことはない。

| 種別 | 対象 | 規則の検査 (外れれば exit 3) | 修復 |
|---|---|---|---|
| H1 履歴の冬のずれ | fingerprint のブロックの全行 (`excluded_weeks` を除く) | 各行で `label + 1h == server_epoch_to_utc(label を +3 のサーバ時刻とみなしたエポック)` | `bar_time` を +1h。付け替え後のキーが既存行と衝突すれば exit 3 |
| C1 週末の偽の足 | 定数の 3 組のうち残っている組 | 組の全行が閉場時刻 (NY 規則) にあり、interval 刻みで穴なく続き、k = 900 の一致数が定数どおり | 削除 |
| C2 切替直後の旧 offset | 定数の 2 組のうち残っている組 | 全行が閉場時刻にあり、ラベル + 1h に同じ symbol / interval の開場行がある | 削除 |
| C3 派生足の遅れ | 定数の 1 組 (残っていれば) | 全行でラベル − 1h の 1m 集約が揃って一致し、同ラベルの 1m 集約が無いか一致しない | 20:30 / 20:45 は同ラベルの 1m 15 本の集約で置換 (1m が揃わなければ exit 3)、21:00〜21:45 は削除 |
| 説明できない閉場行 | 閉場時刻の `mt5-live` 行で定数集合に入らない行 | 1 行でもあれば plan を作らない | 何もしない |

OHLC の一致は既存の許容誤差 (`store/db.py` の `FLOAT_TOL`)、volume は完全一致 (digest は保存値そのもの)。

**plan (`afx.time_repair.plan/1`、JSON)。** `repair_id` (`time_repair_dst_v1`)、`created_at`、`db` (path、サイズ、空き、`data_migrations` の名前一覧)、`fingerprint` (id と照合結果)、`cache_sets` (組ごとに present / absent_before_cache_window と digest の照合結果)、`anchors` (週ごとの日数)、`excluded_weeks`、`bridge_check` (前後 2 回の `/server-time` と窓ごとの本数と一致)、`targets` (H1 / C1 / C2 / C3 の行数・銘柄別・置換と削除の内訳・対象の主キーと旧 OHLCV の sha256、および修復後の状態の主キーと OHLCV の sha256 = H1 の付け替え後の行と C3 の置換後の行。削除する行は修復後に無いことを検証対象とする)、`samples` (各種別 3 行の前後)、`collisions` (0 が条件)、`unexplained` (0 が条件)、`indicator_pending` (`keep` / `invalidate`、dry-run に渡した値)、`approval_changes` (strategy の pending と、`invalidate` のときの indicator の pending について、状態を変える approval の id・kind・変更後の状態。plan に固定する)、`estimate_sec`、`plan_hash` (`plan_hash` 以外の全項目の正規化 JSON の sha256)。

**apply の手順。** (1) plan を読み、`--confirm` と `plan_hash` を照合 (違えば exit 4)。(2) instance lock を取る (取れなければ daemon 稼働中、exit 1)。(3) `deploy_guard` を排他で取る (approve / bless / disable などの配備操作はここで待つ = 承認の凍結)。(4) **最初に migration marker `time_repair_dst_v1` を確認する。** marker が有れば再実行で、更新はせず、修復後の digest (plan の `targets`) と不変条件 (AC 43) を DB だけで検証し、一致すれば exit 0、一致しなければ exit 5 (daemon は停止を維持し、下記の手順で扱う)。marker が無ければ (5) へ。(5) lock の後、対象の再計算より前に `PRAGMA data_version` を読んで v0 とする。続けて fingerprint・cache の定数集合・対象・`approval_changes` の対象 (pending のまま、payload が同じ) を再計算し、plan と一致を確かめる (違えば exit 4)。(6) `sqlite3` backup API で `data/agentic.db.bak-time-repair-<UTC>` を作り、`integrity_check` と表ごとの行数で検める (失敗は exit 1)。(7) `BEGIN IMMEDIATE`。直後に `data_version` を読み直して v0 と照合し、違えば (= (5) の前から今までに他の接続が commit した) rollback して exit 4。さらに同じ transaction の中で、更新の前に対象の digest (履歴の fingerprint・cache の定数集合・対象・`approval_changes` の対象) を再確認し、違えば rollback して exit 4。(8) 同じ transaction で H1 (一時表を経由して削除と挿入)、C1、C2、C3、`approval_changes` (既存の `approvals.apply_decision(conn, id, "invalidated", decided_by="system:time_repair", now=<(7) の BEGIN IMMEDIATE の直前に読んだ aware UTC>, reason="history_time_repaired", commit=False)`。承認行の CAS・backlog・archive が同じ transaction に入る、`store/approvals.py:166-262`。`decided_by` と `now` は必須の引数で、`system:time_repair` は `store/db.py:1560` の `system:migration` と同じ形)、`data_migrations` に `time_repair_dst_v1` を書く。(9) COMMIT。repair 済みで承認だけ pending のまま、という中間状態は作らない。(10) 候補ディレクトリなどファイルの後始末は COMMIT の後に既存の経路で行い、失敗しても DB の状態 (invalidated) が正。(11) DB だけで不変条件を検める (AC 43)。外れれば exit 5。(12) lock を放す。

**commit 後の検査が外れたとき、および再実行で marker が有るのに検証が外れたとき (exit 5)。** daemon は止めたまま (起動しない)、`data/agentic.db` と `-wal` / `-shm` を退け、backup を `data/agentic.db` に戻す。**戻す backup は manifest で特定する:** apply は backup を取った直後に、backup の絶対 path・sha256・plan_hash・取得時刻 (aware UTC) を sidecar manifest `data/agentic.db.time-repair.manifest.json` (backup と同じ `data/`) に fsync して書き、COMMIT の成功後に同じ manifest へ `committed: true` を追記する。exit 5 の復元は、manifest が `committed: true` で backup の sha256 が一致するものだけを使う。manifest が無い・sha256 が合わない・`committed` が無いときは復元せず停止を保ち、手順書の手動復元に回す (fail closed)。失敗した apply の backup は manifest の対象にならない。dry-run を 1 回走らせて fingerprint が元に戻ったことを確かめてから daemon を起動する。手順は `docs/operations/time-repair.md` (T5)。

**承認の凍結と CAS。** apply 中は (3) の排他で配備操作が待つ。daemon が止まっているので `ctl approve / reject / retry` も通らない。apply の後は、履歴に依る payload が修復前のものなら承認できない: strategy の payload を作る 4 箇所 (`switch.py:1454-1466,2423-2428`、`approval.py:525`、`improve_loop.py:2267`) で `eval_source` / `base_interval` と並べて `history_epoch` (その時点の `data_migrations` にある `time_repair_*` の最新の名前、無ければ `"none"`) を記録し、`approve_candidate` は strategy の payload の `history_epoch` (無ければ `"none"`) が現在の値と違えば状態を変えずに `history_epoch_changed` で拒否する。indicator の payload は履歴を読まないので `history_epoch` を持たず、照合もしない。CAS は (8) の invalidation に対する二重の守りで、修復前の履歴で計算した strategy の payload が apply の後に approval 行になる経路 (gate の計算と行の書込みの間に apply が入る) も拒否する。

### 3.5 backtest の記録と合成 fixture

- `backtest_runs` 481 行 (candidate 429 / no_strategy 52、scope は in_sample / holdout_gate / human_custom)、`candidate_archives` 363 行はすべて期間に冬を含み (in_sample の開始は 2025-05-06)、誤ラベルの履歴で算出された。本書の task では**記録を書き換えない**。承認待ちの候補と配備中の版の扱いは J2。
- **baseline 比較は保存値を読まない** (`strategy_gate.py:598-624`)。配備中の版を候補と同じ `now`・dataset で再実行して比べるので、repair 後の候補は repair 後の履歴で再実行した配備版と比べられ、修復の前後が混ざらない。`no_strategy` は配備版が無いときの定数行で、比較の相手ではない。
- `plugin/canonical_fixture.py` は合成の UTC 格子で、`market_hours` と結びつけない。週末マスクを DST 化すると hash が変わり、`indicator_fixture_cache` と保存済み gate payload の `fixture_data_hash` が無効になる。**挙動は変えず**、コメントを「合成。固定の週末格子で DST に追従しない」に改め、gate が使う timeframe ごとの hash をリテラルで pin する (AC 45)。

### 3.6 gap-annotate と econ への影響と引渡し

- **gap-annotate (`2026-11-02-read-tool-gap-annotate-design.md`)。** 11/01 の再現 (その §7-1) は、閉場だった 21:05-22:00 を開場と見た前提と、C2 / C3 の誤り行を含む cache の形に依る。`market_hours` の修正後は「5 本の open-market 欠損」が存在せず、その AC 1 の fixture は作り直しになる。注記方式は薄商いの欠損 (平日 9 分、日曜 22:00 以降の開場直後) のために残す。**順序: 本書の R1 (§7) を先に merge。** gap-annotate の T1 はその後に着手し、21:00 境界に pin した test を更新する。
- **econ §3.5 (`2026-11-01-econ-window-signal-authority-shadow-design.md`)。**
  - (a) 反応統計・shadow の 1m は cache の live 行 (正) を読む。履歴の 5m 冬は使わないので、repair を待たずに誤りは入らない。
  - (b) **第三分類「既知閉場」を足す。** 今の §3.5-3 は outage に無い欠落 minute をすべて「tick 無し」として flat を作るので、冬の日曜 21:00-22:00 のような真の閉場 minute にも flat を作りうる。`is_market_open` が偽の minute は、期待格子・flat の生成・障害欠損のどれにも入れない。fixture は週末、12/25、1/1、両方の DST 切替を通す。
  - (c) shadow の day close は `next_rollover` を共有しており、T2a-2 の期待値で冬の日付を使うものは 22:00 基準に改める。
  - (d) C1 の偽の足は 1m cache に 900 本あり、`ohlcv_slices` を作る T4a は (b) により閉場の行を読まない。
  - (e) `Scheduler.latest_trading_close` (econ-b1 `core/scheduler.py:453-464`) は 24h ずつ遡るので、`session_start` と同じく NY 日付で遡る形に改める。
  - (f) **日次のラベルを `market_hours.trading_date` に統一する。** 日次 report の marker (`:473-497`、閉じた境界の ISO 文字列を文字列比較) は「閉じた境界の 1 分前の `trading_date`」で比べる。旧版が書いた ISO 値は同じ式で読み替える (旧 21:00Z と新 22:00Z の close が同じ取引日になる)。wait-budget の運用日 key (`signal_admission.py:250-252`) は `trading_date(now)` にし、旧版の key (取引日の開始 ISO) は「開始 + 24h − 1 分の `trading_date`」で読み替える。§3.7 の過渡期間は旧新のラベルの遅い方。
  - (g) **merge の順。** econ-b1 が先に main に入れば R1 が (e)(f) を直す。R1 が先なら econ-b1 の rebase が直す。後に入る側は (e)(f) の実装と AC 22 が通るまで merge しない。**R1 の期限 (2026-11-05 merge、11-06 20:30Z 切替) はこれで止めない。** econ-b1 が期限までに main に入らなければ R1 は (e)(f) を持たずに入り、AC 22 は econ-b1 の merge 条件になる。

### 3.7 稼働中の切替 (cutover)

**切替時刻 T_c と過渡期間。** 新版の daemon は起動時、最初の tick の前に (instance lock を持った状態で)、`data_migrations` の `market_hours_ny_rule_v1` (marker、`applied_at` = T_c) と `alert_state` の `time_rule_cutover.capture` (採取、下記) を見て、次の 4 通りに分ける。

| marker | capture | 動き |
|---|---|---|
| 無 | 無 | 切替。marker (`applied_at` = 起動時刻) と capture を **1 つの DB transaction** で書く。COMMIT の後に activity を 1 行書き (下記「activity の一回化」)、readback が成功したときだけ capture の `activity_logged` を true にする |
| 有 | 有 | 何もしない (T_c は動かない)。`activity_logged` が false (activity の書込み・確認の前に落ちた、または readback が失敗した) なら「activity の一回化」を行う |
| 有 | 無 | 同じ transaction で書くので通常は起きない (手作業など)。最初の tick の前にその時点の内容で capture を 1 回だけ補完する (`supplemented_at` つき、T_c は marker のまま)。activity も「activity の一回化」で 1 行 |
| 無 | 有 | 手順外の操作 (marker を消した)。起動を拒否する (`PreflightFailure("time_rule_marker_missing")`、理由は stderr と activity) |

過渡期間は `[T_c, T_b)`、T_b = 新規則の `next_rollover(T_c)` (T_c の後の最初の正しい境界)。新規の DB (他の利用者の初回起動を含む) でも同じく書かれ、旧規則で始まった建玉が無いので過渡規則は効かない。夏の切替では旧新の境界が同じなので過渡規則は何も変えない。

**過渡規則 (`core/time_cutover.py`、`now` が `[T_c, T_b)` のときだけ)。** 旧規則は現行の 21:00 UTC 固定の `trading_day_start` / `next_rollover` を凍結した複製 (`legacy_*`) で持ち、T_b 以後は呼ばない。

| 対象 | 規則 | 理由 |
|---|---|---|
| day 建玉の期限 (`scheduler.py:1248-1252`) | 起点 (filled_at or created_at) が T_c より前の行は `min(旧期限, 新期限)` | 延命の禁止。20:55 から決済に失敗し続けている建玉を 21:55 まで止めない。21:00 以後に旧版で約定した建玉の翌日 20:55 まで延びる期限にも乗せない。起点が T_c より前の行の新期限は T_b − 5 分以下なので、T_b 以後は新規則だけで足りる |
| 日次損失の基準 (`accounting.py:50-68`) | 旧新それぞれの `daily_start_equity` (入出金調整込み) の大きい方。どちらかが None なら None (fail closed) | 基準が大きいほど損失率が大きく、厳しい。旧版が当日に数えていた損失を切替で消さない |
| signal の日次上限の起点 (`missions.py:305`) | 旧新の `trading_day_start` の早い方 | 早い起点ほど数が多く、厳しい |
| 日次ラベル (§3.6-f の wait-budget key) | 旧新の `trading_date` の遅い方 | 旧版が出した通知を同じ取引日に出し直さない |

**採取。** capture は JSON で、OPEN / PENDING_FILL の全行 (id、pair、horizon、起点、day 期限の旧 / 新 / 適用)、日次基準 (旧 / 新 / 適用、それぞれのスナップショット時刻)、T_c、T_b、`activity_logged`、補完なら `supplemented_at` を持つ。activity (SYSTEM) `time_rule_cutover` は同じ内容の 1 行。

**activity の一回化。** activity はファイルなので DB の transaction に入れられない。しかも `ActivityLog.write` は失敗しても例外も戻り値も返さない (`activity.py:32`、書込みの失敗は技術ログの warning だけ) ので、書けたかどうかは書込みの戻りでは分からない。そこで (1) 行の `ref_id` を T_c から決まる安定した値 (`time_rule_cutover:<T_c の ISO>`) にする。(2) 書く前に activity ファイルを読み、同じ `ref_id` の行が既にあれば追記しない。(3) 無ければ追記し、読み直して同じ `ref_id` の行があることを確かめる。(4) 確かめられたとき (既存行があった場合を含む) だけ capture の `activity_logged` を true にする。確かめられなければ false のまま残し、次の起動で (2) からやり直す。追記が成功したのに確認だけ失敗した場合も、次の起動は (2) で既存行を見つけるので 2 行にならず、行の欠落は false が残ることで検出される。

**手順書 `docs/operations/time-rule-cutover.md` (T-deploy)。**

1. 時刻を決める。推奨は 2026-11-05 (木) 10:00-12:00 UTC (境界から遠く、週次改善と重ならない)。**期限は 2026-11-06 (金) 20:30 UTC**。旧版が金曜 21:00 UTC を越えると閉場処理をする (TRADING なら未約定 limit を取消し、新版でも戻らない)。停止時間に 20:30-22:30 UTC を含めない (停止中に境界を越えると、起動後の最初のスナップショットが基準になる)。
2. 実行中の mission が無いことを `afx ctl status` で確かめ、OPEN / PENDING_FILL と日次損益を記録する。
3. `systemctl --user stop afx`。`afx status` で instance lock が空いたことを確かめる。停止から起動までの間に `afx plugin bless` / `submit` と `afx history import` を走らせない。
4. R1 の commit に更新し `uv sync`。`systemctl --user start afx`。
5. activity の `time_rule_cutover` を 2. の記録と照合する (day 期限の適用値が旧期限より遅い行が無いこと、日次基準の適用値が旧新の大きい方であること)。`afx ctl status` の data が ready であること。
6. 当日の旧境界 (21:00 UTC) で日次が切り替わらず、22:00 UTC で切り替わることを観測する。
7. 不具合が出たら: `systemctl --user stop afx` で止める (新規発注が止まる。停止中は afx の監視、つまり day close と SL / TP の判定も止まる)。`afx status` と OPEN / PENDING_FILL を手で確かめる。R1 の上に修正を足した版 (新規則と marker を保つ) で起動する。

**rollback はしない (forward-fix だけ)。** T_c の後に旧版へ戻すと、日次基準と day 期限が旧 21:00 規則だけになり、冬は日次損失の fail-open と期限の延長が起きる (T_b の後なら過渡規則も無い)。旧版は marker も DB の版も読まないので、旧版の起動を後から止める仕組みは作れない (§0.1 の根拠)。そこで (a) 手順書に旧版へ戻す手順を書かず、禁止を明記する、(b) 新版は marker を消す経路を持たない、(c) marker を消した DB では新版が起動しない (上表の 4 行目)、で担保する。旧版を起動してしまった場合も、新版へ戻せば marker は残っているので T_c は動かず、新規則で動く。

## 4. 配線表 (利用者 = `market_hours` と 21:00 / +3 の全箇所、分類つき)

| 分類 | 箇所 | 変更 |
|---|---|---|
| live の取引境界 | `core/scheduler.py:170` (`open_now`)、`:496-508` (`session_start`: 判断足が前セッションの足を除く)、`:947-952` (`_on_market_close`) | 呼び出しは不変 |
| | `core/scheduler.py:1228-1252` (day close の期限) | 期限の計算を `time_cutover` の関数に通す (§3.7)。docstring `:1231` の 20:55-21:00 の例を改める |
| | `loops/trade_loop.py:880-887` (閉場なら見送り、day close の期限の案内) | 不変 (起点は常に T_c 以後) |
| | `datafeed/ingest.py:150,235` / `datafeed/outage.py:276,561-565,684` / `datafeed/health.py:108` | 不変。outage fixture (`tests/fixtures/outage/*sunday-open.json` など) は日曜 21:00 開場前提なので冬の日付なら作り直し |
| | `plugin/signal_producer.py:163-165` | 不変 |
| 日次損失 (境界に依る) | `core/accounting.py:50-68` → `executor.py:614,905` (risk gate)、`loops/summary.py:60` (LLM の日次損益) | `daily_start_equity` を `time_cutover` に通す。保存値でなく都度計算であることを T2 で grep 確認 |
| signal 日次上限 (境界に依る) | `store/missions.py:293-307` | 起点を `time_cutover` に通す |
| kill switch (境界に依らない) | `core/risk_gate.py:179-185`、latch `executor.py:645-648`、reset `store/state.py:395-406` | 不変。AC 13 で不変を pin |
| limit の期限 (境界に依らない) | `core/executor.py:681`、`scheduler.py:954-` `_expire_limits` | 不変。AC 21 で cutover をまたいでも不変を pin |
| 切替 | `core/time_cutover.py` (新規、T_c / T_b / `legacy_*` / 過渡規則 / marker と capture の 4 通り)、`service.py` の起動 (最初の tick の前、`PreflightFailure("time_rule_marker_missing")`) | §3.7 |
| 日次 report / dedupe | econ-b1 `core/scheduler.py:453-497`、`signal_admission.py:250-257` | §3.6-e/f/g (merge の順で担当が決まる) |
| backtest の再生 | `backtest/runner.py:300,452,461,475` / `backtest/analysis.py:372` | 不変。repair 前の冬は境界が 1 時間ずれる (§6、J1)。replay が止まらないことを AC 16 で確認 |
| 合成 fixture | `plugin/canonical_fixture.py:30-38` | コメントのみ、hash を pin (§3.5) |
| 本体 | `core/fx_time.py` (新規)、`core/market_hours.py` (全面、`trading_date` 追加) | §3.1 / §3.3 |
| bridge | `mt5_bridge/mt5_client.py` (換算・測定 60 秒・状態 3 つ・`_OFFSET_RECALC_INTERVAL_SEC` の置き換え)、`server.py` (`/server-time`、`/health`、503 の 2 つの detail)、`pyproject.toml` / `uv.lock` (`tzdata`)、`README.md` (時刻の節、週末は 503) | §3.2 |
| afx の bridge 読み手 | `datafeed/sources.py:131-` (`mt5_bars_range` / `mt5_quote` の 503 → `BridgeTimeRuleMismatch` / `BridgeTimeUnverified`)、`datafeed/ingest.py` (`BridgeTimeUnverified` は失敗に数えず backoff)、`backtest/cli.py` (`history import` の rc=1) | §3.2 |
| repair | `backtest/time_repair.py` (新規、履歴の fingerprint と cache の定数集合)、`backtest/cli.py`、`cli_surface.py` (1 行) | §3.4 |
| 承認の CAS | `plugin/switch.py:1454-1466,2423-2428`、`plugin/approval.py:525`、`loops/improve_loop.py:2267` (`history_epoch`)、`plugin/switch.py:1805` (`approve_candidate` の照合) | §3.4 |
| 文言 | `config.py:67-68` と `config/settings.yaml.example:20` (**両方**、§3.3 の文)、`market_hours.py` の docstring、`datafeed/price_provider.py:29-31` (D1 の 21:00 = NY クローズ 17:00 EDT。冬は 22:00)、`datafeed/bars.py:28-34`、`setup/wizard.py:1093` と `docs/operations/first-run-acceptance.md:337` (yfinance の欠けの観測で時刻は夏の実測。「日曜の開場直後」に言い換える) | 数値の 21:00 を残さない |
| 文書・チケット (T5) | gap-annotate 設計 (動機の主因が消えた、fixture の作り直し)、econ 設計 §3.5 と §3.6 (§3.6-b〜g)、`docs/operations/time-repair.md` と `time-rule-cutover.md` (新規)。チケット: `docs/tickets/open/` の `mt5-bridge-applies-current-offset-to-all-history.md` / `market-hours-fixed-21utc-ignores-dst.md` / `datafeed-no-tick-minutes-counted-as-gap.md` (原因の注記)、`docs/tickets/closed/` の `market-calendar-broker-mismatch.md` と `outage-stalled-on-broker-daily-rollover-gap.md` (本文は変えず「後続の実測で覆った」注記と新チケットへのリンク)、新規起票 2 件 (repair と cache の誤り行検出の一般化 / cache の保持期間の cutoff が起動時刻で固定され再起動まで進まない件、`service.py:1650-1655` と `store/ohlcv.py:204-206` の docstring「毎 tick 呼ぶことで日次増分に追いつく」が食い違う) | R1 の merge 条件 |
| test | `tests/core/test_market_hours.py`、`test_scheduler*.py`、`test_accounting.py`、`tests/loops/test_trade_loop.py`、`tests/backtest/{test_runner,test_replay,test_timeframes,test_base_interval_convergence,test_plugin_deploy_cli}.py`、`tests/datafeed/{test_health,test_weekend_open_integration,test_cache_window}.py`、`tests/plugin/{test_signal_producer,test_canonical_fixture}.py`、`tests/store/{test_ohlcv,test_plugin_deploy_ops}.py`、`tests/fixtures/outage/*`、`tests/fixtures/settings_examples/*.yaml` (コメントのみ)、`tests/test_wiring.py`、`tests/test_config.py` | 全数 grep (`21:00` / `hour=21` / `T21:` / `22:00`)。golden-v1〜v3 は夏の日付なので不変の見込み、判定で確認 |

## 5. 受入条件

実時刻の例で pin する。日付はすべて UTC。

**規則 (T1)**

1. 換算の型と値: `server_epoch_to_utc` はサーバ壁時計 2026-07-24 (金) 23:59 → 20:59Z、2026-11-06 (金) 23:59 → 21:59Z、2026-11-02 (月) 00:00 → 2026-11-01 (日) 22:00Z、2026-03-07 (土) 00:00 → 2026-03-06 (金) 22:00Z、2026-03-09 (月) 00:00 → 2026-03-08 (日) 21:00Z を返し、戻り値は aware UTC。`utc_to_server_epoch` は naive と UTC 以外の aware で `TypeError`。2025-01-01〜2027-12-31 の全分で、二重・存在しない時刻を除き往復が恒等。
2. 曖昧な時刻: サーバ壁時計 2026-11-01 08:30 (NY 01:30、二重) → 05:30Z と warning `ambiguous`。2026-03-08 09:30 (NY 02:30、存在しない) → 07:30Z と warning `nonexistent`、2027-03-14 09:30 も同じ。判定は UTC 往復で、fold の値だけを見る実装では赤になる。
3. `session_boundary_utc`: 2026-07-24 → 21:00Z、2026-11-06 → 22:00Z、2026-03-06 → 22:00Z、2026-03-09 → 21:00Z、2027-03-12 → 22:00Z、2027-03-15 → 21:00Z。
4. 金の閉場: 2026-10-30 20:59Z 開 / 21:00Z 閉。2026-11-06 21:59Z 開 / 22:00Z 閉。2026-03-06 21:59Z 開 / 22:00Z 閉。2026-03-13 20:59Z 開 / 21:00Z 閉。日の開場: 2026-10-25 20:59Z 閉 / 21:00Z 開、2026-11-01 21:59Z 閉 / 22:00Z 開、2026-11-08 21:59Z 閉 / 22:00Z 開、2026-03-08 20:59Z 閉 / 21:00Z 開、2027-03-14 20:59Z 閉 / 21:00Z 開。
5. 祝日: 2025-12-25 (木) は 12-24 21:59Z 開 / 22:00Z 閉、12-25 21:59Z 閉 / 22:00Z 開 (履歴の実測と一致)。2026-01-01 (木) は 2025-12-31 22:00Z 閉 / 2026-01-01 22:00Z 開。2026-12-25 (金) は 12-24 (木) 22:00Z から 12-27 (日) 21:59Z まで連続して閉、22:00Z に開。2027-01-01 (金) は 2026-12-31 22:00Z から 2027-01-03 21:59Z まで閉、22:00Z に開。
6. `trading_day_start` / `next_rollover` / `trading_date`: 2026-11-04 12:00Z → 11-03 22:00Z / 11-04 22:00Z / 11-04。2026-07-22 12:00Z → 07-21 21:00Z / 07-22 21:00Z / 07-22 (現行 test の値と同一)。境界ちょうど 2026-11-04 22:00Z では start がその時刻、next が翌 22:00Z、`trading_date` が 11-05。2026-11-01 23:00Z → 11-01 22:00Z / 11-02 22:00Z。2026-03-08 21:30Z → 03-08 21:00Z / 03-09 21:00Z。
7. `session_start`: 2026-11-04 12:00Z → 2026-11-01 22:00Z。2026-03-10 12:00Z → 2026-03-08 21:00Z。2026-12-28 03:00Z → 2026-12-27 22:00Z (金曜 12-25 の境界を開場と誤らない)。2027-01-04 03:00Z → 2027-01-03 22:00Z。
8. 25h / 23h の日: `next_rollover(2026-10-31 21:30Z)` = 2026-11-01 22:00Z (土 17:00 EDT の後、次は日 17:00 EST)。`next_rollover(2026-03-07 22:30Z)` = 2026-03-08 21:00Z (23h)。どちらも閉場中の区間で、開場中の日次境界は 24h 間隔 (2026-11-02 〜 11-05 を全日確認)。
9. `next_expected_trading_time`: (2026-11-06 21:55Z, 5m) → 2026-11-08 22:00Z。(2026-10-30 20:45Z, 15m) → 2026-11-01 22:00Z。(2026-03-06 21:55Z, 5m) → 2026-03-08 21:00Z。
10. `friday_swing_cutoff_ny`: `"15:59"` は通り `"16:00"` は拒否 (現行どおり)。`is_friday_after(2026-11-06 19:00Z, "14:00")` は真 (NY 14:00 EST)、`(2026-11-06 18:59Z, "14:00")` は偽。

**呼び出し元 (T2)** — 切替の後 (T_c より後に起点を持つ行) の定常の動き

11. day close: 2026-11-04 10:00Z に約定した horizon=day の期限は 2026-11-04 21:55Z、夏 2026-07-22 10:00Z は 20:55Z (不変)。
12. 日次の境界: 2026-11-04 21:30Z の `daily_start_equity` は 2026-11-03 22:00Z 以降の最初のスナップショットが起点で、2026-11-03 21:30Z のスナップショットは前日扱い (現行は当日扱い)。signal の日次上限も同じ境界。
13. 境界に依らない資金保護: HWM 比の DD と latch の判定は、同じ equity / HWM なら 2026-11-04 21:30Z と 22:30Z で同じ結果。latch は 22:00Z を越えても人間の reset まで残る。
14. outage / health: 2026-11-08 21:00-22:00Z は閉場で、15m の取得失敗が outage の `failed` に数えられず、`_closed_minutes` が 60 分を閉場として引く。22:00Z 以降の欠損だけが `open_gap_units` に入る。
15. 2026-11-01 の再現: 修復済みの cache で `now=2026-11-01 22:47Z` の `get_ohlcv(USDJPY, 15m)` が `DataUnhealthy("gap of …")` を出さない (gap 方針が `raise` のままでも通る)。
16. repair 前の replay: 次の固定条件で、T1 + T2 の版の backtest が repair 前の履歴で例外なく完走する (rc 0)。root は `tmp/dst-ac16/` に作る (`config/settings.yaml` の複製、`data/agentic.db` を `sqlite3` backup API で複製、`plugins/` を symlink ごと `cp -a`)。plugin は配備中の `rsi_bb_reversion_200` (live 版 `dc59a62892cafeb987b85e1731387dac966b8840a997a574d8d01697cac9d164`、content hash `a8fe63c3a111837bd22c9b2c4e77b461489eb3f7b55ef3a94d3f1f39dba179d8`、USDJPY 1h、依存 `bollinger` / `rsi`)。複製の `ohlcv_history` の USDJPY 5m `[2025-10-27, 2026-03-13)` が 27,936 行で、§3.4 の作り方の旧 OHLCV sha256 が `78e10eacfdf9e3de450e7ca0ff59d83725c9dcf3ad33b54a53352cbae99102ad` であること (違えば判定しない)。コマンドは T1 + T2 の版の `afx` で `afx --root <tmp/dst-ac16 の絶対パス> backtest run --symbol USDJPY --source mt5 --base-interval 5m --from 2025-10-27 --to 2026-03-13 --plugin rsi_bb_reversion_200 --timeframe 1h` (`--root` が DB を `<root>/data/agentic.db`、plugin を `<root>/plugins/` に決める、`entry.py:157-168` → `backtest/cli.py:460-468`)。冬の金曜 21:00-21:55Z (足が無い) と冬の日曜 21:00-21:55Z (閉場) に発注・約定した注文が 0 件。冬の金曜 21:00Z の判断足から提案が出れば `proposal_dropped_no_bar` で捨てられる (`backtest/runner.py:447-464`)。trades / pf は判定記録に残す (合否には使わない)。

**cutover (T-deploy)** — 断りが無ければ冬、T_c = 2026-11-03 (火) 21:30Z、T_b = 2026-11-03 22:00Z

17. marker と採取: 新版の初回起動で marker (`applied_at` = T_c) と capture が 1 つの transaction で書かれ、最初の tick より前に揃う。marker の INSERT の後・COMMIT の前に例外を入れると、どちらも無い (次の起動で切替をやり直す)。COMMIT の後・activity の追記の前で落とすと、次の起動で activity が `ref_id` つきで 1 行だけ足されて `activity_logged` が true になる。追記の後・readback の前で落とす (または readback を失敗させる) と `activity_logged` は false のままで、次の起動は同じ `ref_id` の既存行を見つけて追記せず true にする (どちらも 2 行にならない)。`ActivityLog.write` が失敗しても何も返さない Fake では `activity_logged` が true にならない。2 回目以降の起動で T_c は動かない。marker 有・capture 無の DB では最初の tick の前に capture が 1 回だけ補完される (`supplemented_at` つき、T_c は marker のまま)。capture と activity には OPEN / PENDING_FILL の day 期限 (旧 / 新 / 適用)、日次基準 (旧 / 新 / 適用)、T_c、T_b が出る。
18. day 建玉 2 種: A (2026-11-03 21:10Z に旧版で約定) は期限 11-03 21:55Z で決済され、旧期限 11-04 20:55Z まで残らない。B (2026-11-03 10:00Z 約定、20:55Z から quote 失敗で `day_close_deferred`) は期限 20:55Z のままで、T_c 直後の tick から再試行される (新規則だけなら 21:55Z まで止まる)。T_c より後に約定した C (21:40Z) は新規則だけ (21:55Z)。
19. 日次損失の fail-open 防止: スナップショット 2026-11-02 22:00Z が 100,000、11-03 21:00Z が 100,600、21:45Z の equity が 99,500、`daily_loss_limit_pct` 1.0 のとき、21:45Z の基準は 100,600 (損失 1.09%) で新規は拒否される (新規則だけなら 100,000 で 0.5%、通ってしまう)。旧新のどちらかの基準が None なら fail closed。22:05Z は 11-03 22:00Z 以降の最初のスナップショットだけが基準。
20. signal の上限: T_c = 2026-11-04 12:00Z、`signal_daily_max` 12、signal 行が 11-03 21:00-22:00Z に 2 本、11-03 22:00Z-11-04 12:00Z に 10 本のとき、12:00Z に signal 起動しない (旧起点で 12 本)。新規則だけなら 10 本で起動してしまう。11-04 22:00Z 以後は新起点だけ。
21. limit の期限: 11-03 21:00Z に `expires_in` 2h で出した limit は、T_c をまたいでも 23:00Z に 1 回だけ失効する。
22. report 1 回: 旧版が 11-03 21:00Z に `daily_report.state = {"day": "2026-11-03T21:00:00+00:00"}` を書いた後に T_c、新版は 21:30Z と 22:00Z では日次 report を起動せず、11-04 22:00Z に 11-04 の分を 1 回起動する。夏の旧 marker (`2026-07-22T21:00:00+00:00`) は 07-22 と読める。wait-budget の通知も同じ取引日で 2 回出ない (§3.6-f)。
23. 金曜: TRADING、T_c = 2026-11-06 20:30Z、PENDING_FILL の limit 1 本。21:00Z に取消されず、22:00Z に 1 回だけ取消される (`limit_cancelled` 1 行)。22:30Z に再起動しても行は増えない。旧版が 21:00Z を越えて取消した後に 21:10Z で切り替えた場合も取消はそれ以上増えない。21:00-21:55Z の足は開場として ingest される。
24. 過渡期間の終わりと rollback の禁止: T_b 以後の tick は `legacy_*` を呼ばない (`legacy_*` を例外にしても 22:01Z 以降の tick が通る)。新版のコードに `data_migrations` の行を消す経路が無い (`src/` で `DELETE FROM data_migrations` の grep 0 件を test で pin)。marker 無・capture 有の DB では新版が `time_rule_marker_missing` で起動を拒否し、tick は 1 回も走らない。forward-fix の版 (R1 + 修正) で再起動しても T_c は動かず、T_b の後なら新規則だけで動く。旧版の起動は止められない (§3.7) ので、手順書に旧版へ戻す手順が無く禁止と緊急時の停止手順 (§3.7 の 7.) が書いてあることを T-deploy の検収で確かめる。

**bridge (T3)**

25. 足ごとの換算: Fake MT5 の rates が夏 (2026-10-30) と冬 (2026-11-02) をまたぐ範囲を返すとき、返るラベルは真の UTC (10-30 の最終足 20:59Z、11-02 の最初の足 22:00Z 以降)。呼び出しの「いま」を 2026-07-28 と 2026-11-02 の両方にして**同じ結果**。
26. 要求側の換算: from / to が DST 切替日 (2026-11-01、2027-03-14) をまたぐ窓でも、MT5 に渡るサーバ時刻が規則の逆と一致する。
27. 切替直後: Fake の時計を 2026-11-01 22:00:05Z にして、最初の ingest が 22:01Z のラベルで返る (21:01 でない)。offset の再検出を待たない (再検出間隔の test は消す)。
28. 食い違い: 開場中に受理した測定が 7200、規則が 10800 のとき、`/ohlcv` と `/quote` は 503 `time_rule_mismatch`、`/server-time` は 200 で `measured_offset_sec=7200` / `expected_offset_sec=10800` / `offset_matches_rule=false` / `time_rule_state="mismatch"` と `measured_at` / `measured_age_sec`、`/health` は 200 で `time_rule_state="mismatch"`。`/account` と `/positions` は止まらない。規則に一致する受理が 2 回 (`match_streak=2`) では戻らず、3 回続いて `ok` に戻り `/ohlcv` が 200 (60 秒間隔で最短 2 分)。間に棄却が挟まっても連続数は切れず、違う値の受理で 0 に戻る。`mismatch` のまま 300 秒を超えても `mismatch` (`unverified` に変わらない)。WARNING は状態の変化ごとに 1 回。
29. 未検査 (`unverified`): (a) 起動直後、開場中 (2026-11-04 12:00Z) で受理が無いとき、最初の `/ohlcv` は応答の前に測り、tick が新しければ 200 (`ok`)、tick が古い (残差 120 秒超) か tick の取得に失敗すれば 503 `time_rule_unverified`。(b) 12:00:00Z に一致を受理し、12:01〜12:05 の測定がすべて棄却されると、12:05:00Z は 200 (age 300)、12:05:01Z は 503 `time_rule_unverified` で、`/server-time` が `measured_age_sec=301` / `time_rule_state="unverified"`。次に一致を 1 回受理すれば `ok`。(c) 60 秒未満の間隔の呼び出しでは tick を読まない (Fake の `symbol_info_tick` の呼出数で確かめる)。(d) probe symbol の tick が 8 分止まり (2026-09-28 21:01-21:08 の日次ロールオーバーの形)、要求した EURUSD には新しい tick があるとき、`/ohlcv/EURUSD` は `ok` のまま 200。(e) 閉場中 (2026-11-07 土) は測定を試みず、`/ohlcv` と `/quote` は 503 `time_rule_unverified`。
30. 日曜の開場 (afx と結合、Fake bridge + Fake MT5): 2026-11-08 (日) 22:00Z 開場、最初の新しい tick が 22:03:10Z、ingest の初回の取得が 22:01:30Z。(a) flat のとき、22:01:30Z〜22:03:10Z の `/ohlcv` は 503 `time_rule_unverified`、ingest は失敗に数えず backoff で取り直し、outage は停滞式で `restricted` に 1 回入る (`datafeed_restricted` の activity と通知が各 1 件、`datafeed_degraded` は 0 件)。22:03:10Z 以後の測定で `ok`、足が入り健全 3 tick で `ready`。(b) 金曜から持ち越した swing 建玉が 1 本あるとき、同じ区間で `degraded` に 1 回入り (activity と通知が各 1 件)、復旧は既存どおり `afx ctl data resume` 待ち。(a)(b) とも kill switch は latch せず、bridge の WARNING は `unverified → ok` の 1 行、開場から 10 分間の `datafeed_*` の activity は 2 行以下。
31. 週末の偽 offset: 2026-10-03 の再現 (土曜、tick 凍結、測定すれば 30 分ごとに -1800 秒) で、測定は行われず (`measured_offset_sec` が null のまま)、状態は `unverified` のまま、換算は規則のまま。
32. tz データ: `ZoneInfo` の読み込みに失敗する環境 (Fake) で bridge は起動せず `/health` は ok にならない。実機 (Wine) の受入: 同じ起動自己検査が通ること。
33. 金表: `dst_rule_table.json` は §3.1 の必須の件をすべて含み、全件が本体の `fx_time` と bridge の複製の両方で期待値と一致する。
34. afx の受け手: Fake bridge が `/ohlcv` に 503 `time_rule_mismatch` を返すとき、`mt5_bars_range` は `BridgeTimeRuleMismatch` を出し、ingest の失敗理由に固定文言が載り、outage は既存の規則で 1 回で `degraded` になり新規の発注が止まる (flat なら健全 3 tick で自動復旧)。503 `time_rule_unverified` のときは `BridgeTimeUnverified` で、その key は `IngestTickReport` の attempted / failed / succeeded のどれにも入らない。SL / TP の tick 処理はどちらでも例外で止まらない。`afx history import --source mt5` はどちらでも rc=1 で 0 行、stderr に固定文言。
35. 実 bridge の確認 (T3 の後、指揮者が GET のみ): `USDJPY 5m` の 2026-10-30 最終足が 20:55Z、2025-11-02 の最初の足が 22:00Z、2025-12-16 の最大レンジが 13:30Z、2026-01-09 の 13:30Z の足が上位 2 本、2026-03-08 の最初の足が 21:00Z。開場中の `/server-time` が `offset_source="rule"` / `time_rule_state="ok"` / `measured_age_sec` 300 以下。土曜の `/ohlcv` 1 回が 503 `time_rule_unverified`。

**repair (T4)** — 実 DB は複製に対してだけ試験する

36. dry-run は DB を変えない (実行前後でファイルの hash が同一)。plan は `afx.time_repair.plan/1` の全項目を持ち、`plan_hash` が他の全項目から再計算できる。
37. 現 DB の複製での dry-run の期待値: 履歴の fingerprint (2 つの digest) 一致、錨 18/18 週、`excluded_weeks` 0、bridge 照合 24 窓すべて一致、H1 = 146,632 行 (銘柄別は §0.2)、C3 = 15m 6 (置換 2、削除 4)、説明できない閉場行 0、衝突 0。cache の定数集合は、R1 の再起動の前の複製なら 6 組すべて present で digest 一致、C1 の k = 900 の一致数 633 / 39 / 7 (C1 = 1m 900 / 15m 60 / 1h 15、C2 = 1m 4 / 1h 1)。R1 の再起動の後の複製なら C1 の 3 組は `absent_before_cache_window`、C2 / C3 の 3 組は present。`approval_changes` は `--indicator-pending keep` (J2 (a)) で 0 件 (approval 78 は indicator で変えない)、`--indicator-pending invalidate` (J2 (b)) で approval 78 の `invalidated` 1 件。`--indicator-pending` の省略は exit 2。数が違えば原因を調べるまで apply しない。
38. 未知の DB: 複製から (a) ブロックの 1 行の close を変える、(b) ブロックの範囲に 15m の履歴行を 1 本足す、(c) 1 銘柄のブロックを消す、(d) ブロックに 1 行足す、のどれでも exit 3 で plan を作らず、DB は不変。cache では (e) 定数集合に入らない閉場時刻の `mt5-live` 行を 1 本足す (例 2026-10-03 12:00 の 1m)、(f) C1 1m の 1 行の close を変える、(g) C1 1m の組から 1 行だけ消す (一部だけ残る)、(h) C1 1m の組を全部消し、それより前のラベルの 1m 行を 1 本足す (cache window より前でない欠け)、のどれも exit 3。(i) C1 の 3 組を全部消し、各 interval の最古の行を 10-03 12:00 より後にした複製は `absent_before_cache_window` で通る。
39. source の隔離: 複製に `yfinance` / `twelvedata` の cache 行を閉場時刻・C1 と同じずれ・C3 と同じ 1 時間遅れの形で足しても、dry-run の対象に入らず、apply の前後で行数と内容の hash が同じ (説明できない閉場行にも数えない)。
40. bridge の事前照合: bridge の `offset_source` が `"rule"` でない、`time_rule_state` が `unverified` か `mismatch`、`measured_age_sec` が 300 を超える、照合窓の GET の後の 2 回目の確認でそのどれかになる、のどれも exit 1。照合窓の 1 本の OHLC を Fake で変えると exit 3。
41. apply の拒否: plan hash の不一致、dry-run 後に対象行を 1 本変えた (digest)、dry-run 後に strategy の pending が 1 件増えた (`approval_changes` の不一致)、`data_version` の v0 を読んだ後の各点 (対象の再計算の後、backup の後、`BEGIN IMMEDIATE` の直前) に別接続が 1 行 commit した、transaction の中の対象 digest の再確認の前に対象行を変えた、のどれも exit 4、daemon 稼働中 (instance lock) と backup 失敗は exit 1。どの場合も DB は apply 前と同一。
42. 原子性: backup の後、H1 の後、C1 の後、C2 の後、C3 の後、承認の変更の後、marker の後、COMMIT の呼出しの直前の各点で例外を入れる (= COMMIT が成功する前の fault) と全部 rollback され、DB は apply 前と同一 (承認も pending のまま) で backup は残る。COMMIT が成功した後の fault (COMMIT 直後、後始末、不変条件の検査の前) は rollback できないので exit 5 で、daemon は停止を維持し、DB は修復済みのまま backup から戻す手順 (§3.4) で扱う。
43. apply 後の不変条件 (DB だけで検める): FX の 5m 行に閉場時刻のものが 0。FX (EURUSD / USDJPY) の履歴の全週末で、冬の週は最終足が金 21:55Z・最初の足が日 22:00Z、夏の週は金 20:55Z・日 21:00Z。NFP 2025-12-16 / 2026-03-06 の最大レンジが 13:30Z (2026-01-09 は上位 2 本)。`mt5-live` の閉場時刻の行が 0。`data_migrations` に `time_repair_dst_v1`。再実行は最初に marker を確認し、有れば修復後 digest と不変条件が一致して exit 0 (更新なし、行数・内容の hash が同一)。marker が有るまま修復後の行を 1 本変える Fake で exit 5 (daemon は起動しない)。marker が無い複製で fingerprint を変えると exit 4。不変条件を 1 つ壊す Fake で exit 5 になり、手順どおり backup から戻すと dry-run の fingerprint が元と一致する。 指数・金・原油は米国の祝日に早く閉じ、開場直後の足が無い週もあるので、週の境界ではなく閉場時刻の足が 0 本であることと digest の一致で検める (2026-11-03、現 DB の複製での試し apply で判明)。
44. 承認の凍結と CAS: apply が `deploy_guard` を排他で持つ間、`approve_candidate` と `bless_candidate` は待つ。複製に strategy の pending を 1 件足した plan では、COMMIT の時点でその行が `invalidated` (固定文言 `history_time_repaired`)、backlog と archive も同じ transaction で進む。`--indicator-pending keep` (J2 (a)) では indicator の pending (78) は pending のまま、`invalidate` (J2 (b)) では同じ transaction で `invalidated`。apply の `approvals.apply_decision` 呼出しは `decided_by="system:time_repair"`、`now` は aware UTC、`reason="history_time_repaired"`、`commit=False`。apply の後、`history_epoch` が無い strategy の payload は `approve_candidate` で `history_epoch_changed` になり状態が変わらない。indicator の payload は照合されない。

**その他**

45. 合成 fixture の hash: `fixture_data_hash(build_canonical_5m())` = `dc010588fc44ff4516eb783be6142c69289fb6e7d07a011860f7a080344975de`、`canonical_bars(tf)[1]` が 15m `ed41035a113b39b5a6c041a72003c1bee88699b64317f416f8733f3b022f9c76` / 1h `7881c93a468ee967bf9d6e06f04469a8803e98af832d231088b78fa26a58161d` / 4h `ef92d788da7348ab13fa821c78e90adec33805635a89920ab799a0c27dba7a95` / 1d `5e156fad4ca4b98af28aa478df89d4e3c524293225369ea0ef65b330dfccf2c6` (2026-11-02 の main で実測)。gate payload の `fixture_data_hash` と `indicator_fixture_cache` の key も変わらない。
46. 変異 (段 0): 22:00 を 21:00 に戻す / 祝日ラベルの +7h を +3h に戻す / `next_rollover` を 24h 加算に戻す / bridge の換算を測定 offset に戻す / 二重・存在しない時刻の判定を fold だけにする / H1 の付け替え方向を逆にする / day 期限の min を max にする / 日次基準を新規則だけにする / signal の起点を新規則だけにする / 過渡規則を T_b 以後も効かせる / marker と capture を別 transaction にする / marker 無・capture 有で起動を通す / cache の検出から `source='mt5-live'` を外す / cache の定数集合の照合を外す (規則の検出だけにする) / 「一部だけ残る組」を通す / `mismatch` の 503 を外す / `unverified` で市場データを返す / 鮮度 300 秒の判定を外す / 測定の間隔 60 秒を外す / 復帰の 3 回を 1 回にする / afx で `BridgeTimeUnverified` を失敗に数える / apply の digest の照合 (transaction の内側を含む) または `data_version` の照合 (v0 を対象の再計算より前に読む順を含む) を外す / marker を最初に見ない / 承認の変更を COMMIT の後に回す / 承認の CAS を外す、のいずれも上の AC のどれかが赤になる。
47. 判定 (指揮者が回す。tmpfs 上の `--basetemp`、2 本直列まで、別プロセス束): ① `tests/core tests/store`、② `tests/datafeed tests/backtest`、③ `tests/plugin tests/loops`、④ `tests/service tests/commands tests/ops tests/setup tests/ops_setup`、⑤ `tests/tools tests/integration tests/acceptance tests/runners tests/scripts tests/egress` と最上位の `tests/test_*.py` (e2e を含む)、⑥ `cd mt5_bridge && .venv/bin/python -m pytest -q`。①〜⑤の和が `tests/` の全 test ファイルであることを `find` で確かめる。新規・改訂した test の skip は失敗扱い。束ごとの skip 件数を同じ束の main と比べ、増えたら失敗。未実行の束があれば判定しない。

## 6. 残余

- **規則が外れている間は MT5 の市場データが止まる。** ブローカーが DST の扱いを変えると、bridge は `/ohlcv` と `/quote` を止め、afx は価格源の不通と同じ扱い (新規リスク停止、SL / TP は既存の不通時の規則と復旧時の再生) になる。規則の更新はコードの変更で、利用者が設定で逃がす経路は作らない。
- **検査できない間も止まる。** 週末は `afx history import` と repair の dry-run ができない (開場中に走らせる)。日曜の開場直後と日次ロールオーバーで新しい tick が 300 秒以上無いと、afx は足が来ない状態として既存の停滞の規則で待ち、建玉があれば `afx ctl data resume` が要る (AC 30)。要求銘柄の tick も測定に使うが、本機の cache は USDJPY だけで要求銘柄が probe symbol と同じなので、本機では USDJPY の無 tick がそのまま効く。複数銘柄を取る構成でだけ、1 銘柄の無 tick で止まりにくくなる。
- **旧版の起動は止められない。** 旧版は marker を読まないので、T_c の後に手順外で旧版を起動すると旧 21:00 規則で動く。防ぎは手順書の禁止だけで、新版へ戻せば marker が残っているので新規則に戻る (§3.7)。
- **定数集合の外の誤り行は v1 では直さない。** T3 (bridge の規則換算) より前に同じ種類の偽の足が新しく cache に入れば、v1 の定数集合に無いので dry-run は「説明できない閉場行」で exit 3 になり、別 version と利用者承認で扱う。
- **C1 は repair より先に保持期間で消える。** R1 の再起動で cutoff が進み、C1 の 975 行は保持期間の削除で消える (§0.1)。J1 の承認に依らず消えるが、消える前の 1 か月 (2026-10-02 以後) の cache を読んだ結果 (指標・判断) には含まれていた。
- **春の実機確認。** 規則は US で、春も独立の錨 (§0.2-6) で裏づけた。2027-03-14 の開場が 21:00Z で `time_rule_state` が `ok` のままであることを実時刻で確かめる (受入には含めない)。
- **repair の承認待ちの間** 冬の backtest は日次・週次の境界が 1 時間ずれる (履歴は旧ラベル、`market_hours` は新規則)。冬の金曜 21:00-21:55Z は足が無く発注しない (AC 16)。ライブには影響しない。
- **切替の日の日次 report** は旧版が 21:00Z に出した 1 本 (最後の 1 時間を含まない) が正になり、出し直さない (AC 22)。
- 銘柄別の取引時間 (指数・金・原油は FX より 1 時間遅く開く、サーバ 01:00) は `is_market_open` に入れない。早期閉場 (12/24) と祝日の追加 (感謝祭など) も同様。C1〜C3 の閉場判定も FX の規則で、FX より短い銘柄の閉場は見ない。
- 他の価格源 (yfinance / Twelve Data) は UTC が正で影響しない。MT5 の `1d` / `4h` ネイティブはサーバ時刻の格子だが `DERIVE_ONLY_INTERVALS` により取らない。
- `ohlcv_history.spread` は bridge が返さないため全行 NULL のまま。
- 公開リポジトリの他の利用者は同じ +3 固定の履歴を持ちうるが、v1 の repair は扱えない (exit 3)。取り込み時期の記録 (provenance) か bridge との全量照合で一般化する案を T5 で別チケットに起票する。

## 7. 変更一覧と task 分割

| task | 内容 | 担当 | 規模 | 順序・期限 |
|---|---|---|---|---|
| T1 | `core/fx_time.py` と `core/market_hours.py` の全面改修 (`trading_date` を含む)、`config.py` と `settings.yaml.example` の文言、AC 1〜10 | codex terra | 実装 約 120 行 + test 約 250 行 | **R1** (T1 + T2 + T-deploy のコード) を 1 つの release として即着手、2026-11-05 (木) までに merge |
| T2 | 呼び出し元の test と fixture の全数 grep と更新 (AC 11〜16、45)、`canonical_fixture` のコメントと hash pin、文言 (`price_provider` / `bars` / `wizard` / `first-run-acceptance`)、`accounting` が保存値を持たないことの確認 | codex terra (T1 と同じ worktree) | 中 | R1 |
| T-deploy | `core/time_cutover.py` (T_c / T_b / `legacy_*` / 過渡規則 / marker と capture の 4 通り) と 3 箇所の配線、起動の `time_rule_marker_missing`、AC 17〜21・23・24 (AC 22 は §3.6-g により econ-b1 と R1 のうち後に main へ入る側が持つ。R1 の期限は止めない)、手順書 `docs/operations/time-rule-cutover.md` (rollback の禁止と緊急時の停止を含む)。本番の切替の実行と観測 (§3.7 の 1〜6) | codex terra (コード、T1 と同じ worktree) + sonnet (systemd の停止・起動と観測) | 実装 約 120 行 + test 約 300 行 | コードは R1。切替の実行は R1 の merge 後、**2026-11-06 (金) 20:30Z まで** (推奨 11-05 10:00-12:00Z) |
| T0 | Wine 上の Python で `ZoneInfo("America/New_York")` が通るか、`tzdata` の追加で通るかを実機で確認 (`uv add tzdata` 後の起動自己検査)。bridge の再起動手順の確認 | sonnet (Wine) | 小 | R1 と並行。T3 の前提 |
| T3 | bridge: `fx_time` の複製、足ごとの換算、測定は検査のみ (60 秒、要求銘柄の tick も使う)、状態 3 つと 503 の 2 つの detail、`/server-time` と `/health` の項目、`tzdata`、起動自己検査、`dst_rule_table.json`、afx 側の `BridgeTimeRuleMismatch` / `BridgeTimeUnverified` と ingest の扱い、AC 25〜34、README の時刻の節 | codex terra (実装) + sonnet (T0 の結果を受けて Wine 再起動、AC 35) | 実装 約 260 行 + test 約 450 行 | T0 の後、R1 と並行。**bridge 再起動までは `afx history import` を新規に走らせない** (夏の期間を取ると 1 時間遅れで入る)。ライブの足は冬の間は今も正しい |
| T4 | `afx history repair-time` (§3.4 の全部、履歴の fingerprint と cache の定数集合、承認の変更を同じ transaction で)、承認の CAS、AC 36〜44 | codex terra (純 Python)、原子性・lock・`data_version` の競合 test は sonnet で変異 | 実装 約 480 行 + test 約 600 行 | R1 と T3 の後。**dry-run の結果を利用者へ出すまで。apply は J1 / J2 の承認後** |
| T5 | §4 の「文書・チケット」行の全部 | 指揮者 | 小 | **R1 の merge 条件** (同じ commit にチケット状態) |

**順序:** R1 (T1 + T2 + T-deploy のコード + T5) → 本番の切替 (2026-11-06 20:30Z まで)。T0 → T3 は並行。T4 は R1 と T3 の後、dry-run の結果を見て J1 / J2。gap-annotate の T1 は R1 の後。econ-b1 との順は §3.6-g。

## 8. 裁定 (利用者判断が要るもの)

**J1 履歴を書き換えてよいか。** 内容: (1) `ohlcv_history` の冬のブロック 146,632 行 (5m、6 銘柄、2025-11-02 21:00〜2026-03-06 20:55 ラベル) の `bar_time` を +1h する。(2) `mt5-live` の cache の誤り行を定数集合のとおり直す (すべて USDJPY):

| 組 | 行 | 処置 | 内訳 |
|---|---|---|---|
| C1 1m / 15m / 1h | 900 / 60 / 15 (計 975) | 削除 | 900 分前の行と一致 633 / 39 / 7。元行が保持期間で消えた先頭 263 / 18 / 5、末尾の値違い 4 / 3 / 3 (§0.3。どれも土曜の閉場時刻の連続した塊の一部) |
| C2 1m / 1h | 4 / 1 (計 5) | 削除 | 2026-11-01 21:01〜21:04 と 21:00 |
| C3 15m | 6 | 置換 2 (10-30 20:30 / 20:45)、削除 4 (21:00〜21:45) | |

計 986 行 (削除 984、置換 2)。T4 は R1 の後なので、apply の時点で C1 は R1 の再起動による保持期間の削除で消えており、対象は C2 / C3 の 11 行 (削除 9、置換 2)。2026-11-29 21:45 より後の再起動があれば C3、12-01 21:04 より後なら C2 も同じく消える。dry-run が組ごとに present / `absent_before_cache_window` を示す。v1 はこの DB の既知 fingerprint、cache の定数集合、修正済み bridge (`ok` かつ 300 秒以内の受理) との照合が通るときだけ動き、汎用の検出はしない。実行前に dry-run の報告を提示する。推奨: **承認する** (dry-run の数が AC 37 と一致することを条件)。根拠: 履歴は「削除しない・不変」という契約の対象で、これを破る唯一の例外になる。ただし誤ラベルのまま残す害 (冬の 5m に基づく指標・session 型の戦略・econ の整列が 1 時間ずれた結果を、承認の根拠にし続ける) が、一度きり・バックアップあり・再実行しても 0 件の処置より大きい。代案: 旧行は触らず、修正ラベルの行を別 source (例 `mt5-tz`) に書き、dataset がそちらを優先する (非破壊。ただし同じ `(symbol, interval, bar_time)` の二重化と source 選択の規則が要り、`compare_sources` などの JOIN に影響する)。利用者判断の理由: 不変契約の例外であり、元に戻すにはバックアップからの復元になる。

**J2 保存済みの記録と承認待ちの候補をどう扱うか。** 対象: `backtest_runs` 481 行 (candidate 429 / no_strategy 52)、`candidate_archives` 363 行 (すべて冬を含む誤ラベルの履歴で算出)、承認待ち 1 件 (approval 78 / archive 323、`us500_hourly_range_surprise`、2026-10-31 提出、**indicator**)、配備中の plugin。

J2 は 2 つの別の事柄からなる。(1) repair の中で決める、indicator の pending の扱い (a)(b)。(2) repair の後に別に決める、配備中の版の再評価 (c)。

- 共通。記録は残し、修復の前後は `data_migrations` の `time_repair_dst_v1` と payload の `history_epoch` で区別する。strategy の pending (`eval_source` あり) は (a)(b) どちらでも repair と**同じ transaction** で `invalidated` (固定文言 `history_time_repaired`) にし、改善 loop の再提出を待つ (現 DB では 0 件)。
- (a) **推奨。** `afx history repair-time --indicator-pending keep`。indicator の pending (78) は pending のまま残す。
- (b) `--indicator-pending invalidate`。(a) に加え、indicator の pending (78) も同じ transaction で `invalidated` にする (指揮者の r1 裁定の当初案)。
- (c) 配備版の再評価。repair とは別の実行・記録項目で、repair の引数にも plan にも入らず、(a)(b) のどちらとも組み合わせられる。推奨は、配備中の strategy を repair の後に 1 回だけ `human_custom` で測って差を報告し、次を決めること。差が大きければ、配備中の plugin の in_sample と holdout を自動で全部やり直し、承認の根拠を差し替える。

推奨の理由: strategy の数値は誤ラベルの履歴に依り、修復後に承認すれば古い根拠で採用することになるので閉じる。indicator gate は合成 fixture だけを読み (`indicator_pipeline.py:183-186,302-304`)、78 の gate 結果は修復で変わらないので、閉じても再提出で同じ結果が出るだけになる。ただし 78 は「同じ UTC 時刻の過去 10 観測の中央値で正規化する」指標で、着想した mission は誤ラベルの履歴を見ていた。その点を重く見るなら (b)。配備中の版は、(c) の 1 回の測定の差が小さければ再評価と承認のやり直しが要らず、大きければその数値を見て全部やり直しを選べる。baseline 比較は保存値でなく配備版の再実行なので、repair 後の候補と repair 前の baseline は混ざらない (§3.5)。利用者判断の理由: 「採用は人間承認」の根拠となる数値を差し替えるか、承認待ちの候補を閉じるかは運用方針で、承認済み・承認待ちの候補の地位に触れる。

## 9. 変更履歴

| 日付 | 版 | 変更 | 理由 | commit |
|---|---|---|---|---|
| 2026-11-02 | v0.1 C0 | 初版 (レビュー前)。履歴の検証 (§0.2)、cache の誤り行 3 種 (§0.3)、時刻規則 2 関数 (§3.1)、bridge の規則換算 (§3.2)、`market_hours` (§3.3)、repair (§3.4)、受入条件 28 本、task 6 本、裁定 2 件 | チケット `mt5-bridge-applies-current-offset-to-all-history` / `market-hours-fixed-21utc-ignores-dst` の設計化、期限 2026-11-06 | |
| 2026-11-02 | v0.2 C0 | sol 1 周目の全件を反映: cutover 節 §3.7 と T-deploy を新設 (day 期限は旧新の早い方、日次基準は厳しい方、signal 起点は早い方を次の正しい境界まで、marker と採取、手順書と rollback)、規則外れは `/ohlcv` `/quote` を 503 で止め 3 回一致で復帰、H1 は既知 fingerprint と bridge 事前照合に限定し未知の DB は exit 3、cache 検出は `mt5-live` 限定で C1〜C3 を出所の条件で強化 (実測で数を固定)、plan の digest・`data_version`・単一 transaction・commit 後の復元手順、承認の凍結と `history_epoch` の CAS、春の独立の錨 (US500 現物開始、境目の連続性)、型契約と曖昧時刻の UTC 往復判定、`friday_swing_cutoff_ny` `< 16:00` の維持理由、econ への第三分類と日次ラベルの引渡し、baseline 比較の事実を §1 / §3.5 へ、fixture hash をリテラルで pin、CLI 名・module・終了コード・plan schema を確定、受入条件 45 本 (cutover 8 本と判定 suite の列挙を追加)、裁定は J1 / J2 の 2 つ (J2 に承認待ちを含め、indicator の pending は残す推奨) | 設計レビュー 1 周目 (sol、Critical 5 / Important 9 / Minor 4、指揮者裁定で全採用) | |
| 2026-11-02 | v0.3 C0 | terra 2 周目の新指摘と逸脱の裁定を反映: bridge は「300 秒以内に受理した測定が規則と一致」のときだけ足と quote を返し、未測定・古い測定・閉場中は 503 `time_rule_unverified` (測定は 60 秒ごと、要求銘柄の tick も使う。afx は失敗に数えず既存の停滞の規則で待つ)、repair は bridge の `ok` かつ鮮度を必須に。切替後の旧版への rollback を禁止して forward-fix だけに (旧版の起動拒否は作れない根拠を §0.1 に、新版は marker を消す経路を持たず marker 無・capture 有で起動拒否)。marker と capture を 1 つの transaction に、capture の補完と activity の 1 回化。C1 の汎用規則をやめ、cache の 6 組を範囲・k = 900・主キーと旧 OHLCV の digest の定数集合に (行単位で出所の取れない 1m 267 / 15m 21 / 1h 8 は理由つきで C1 に含め、J1 の一覧へ)、保持期間で消えた組は `absent_by_retention`。履歴の fingerprint の digest を実測で固定。J2 の承認の変更を repair と同じ transaction に。AC 16 を plugin・hash・履歴 digest・コマンドで固定、AC 22 の所有と R1 の期限を明文化。受入条件 47 本 (未検査と日曜開場の 2 本を追加)。§1 に保持期間・outage・approvals・旧版の起動の現物を追加、§0.2 の履歴期間を銘柄別に訂正 | 設計レビュー 2 周目 (terra、Critical 3 / Important 2、逸脱 4 点のうち C1 の一般化を却下、指揮者裁定) | |
| 2026-11-02 | v0.4 C0 | sol 3 周目の指摘と裁定を反映 (T1 + T2 の範囲、AC 1〜16・45 は不変): §3.4 apply の TOCTOU を閉じる (lock の後、対象の再計算より前に `data_version` の v0 を読み、backup の後の `BEGIN IMMEDIATE` で v0 と再照合し、transaction の内側でも対象 digest を再確認してから更新。手順番号を 1〜12 に振り直し)。apply の再実行は最初に migration marker を確認 (有れば修復後 digest・不変条件が一致で exit 0、不一致で exit 5。無くて fingerprint / 対象が plan と違えば exit 4)、plan に修復後の digest を足す。§3.7 の activity の一回化を安定した `ref_id` での既存行の照合と readback 成功時だけ `activity_logged` を立てる形に (`ActivityLog.write` は失敗を返さない、`activity.py:32`)。AC 42 を COMMIT 成功前の全 rollback と COMMIT 成功後の exit 5 に分割。`absent_by_retention` を `absent_before_cache_window` に改称 (判定は現在の cache window より前での群全体の欠落で、削除原因は主張しない)。`--j2 a|b` を `--indicator-pending keep|invalidate` に改名 (strategy の pending の invalidate は常に行う)、J2 (c) の配備版の再評価は repair と別の実行・記録項目に分け J2 の文言を合わせる。`apply_decision(commit=False)` の `decided_by` / `now` / `reason` を明記 | 設計レビュー 3 周目 (sol、Critical 1 / Important 3、裁定を越えた 3 点、指揮者裁定で全採用) | |
| 2026-11-02 | v0.4.1 C0 | terra 4 周目: exit 5 の復元対象を sidecar manifest (path・sha256・plan_hash・committed) で特定、無ければ fail closed。承認 CAS の手順番号を (8) に訂正 | 失敗済み apply の backup を誤って戻す経路を閉じる | (dst-r1 に同梱) |
| 2026-11-03 | v0.4.2 C0 | AC 43 の週の開場・閉場の足の検査を FX 2 銘柄に限り、指数・金・原油は閉場時刻の足 0 本と digest で検める | 現 DB の複製で試した apply が、米国の祝日の早い閉場 (感謝祭翌日・聖金曜日・ジューンティーンス・7/3) と金の開場直後の欠けで COMMIT 後に exit 5 になった | (dst-t4) |
