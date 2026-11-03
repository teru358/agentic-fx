# 読み取り tool は足の欠損で止めず、注記して返す設計 (v0.2)

作成: 2026-11-02。改訂: 2026-11-03。対象は判断 mission の `get_ohlcv` / `get_indicators` である。ユーザー裁定 (2026-11-02) は、15m を含む全 timeframe の読み取り tool に適用し、欠損そのものでは OPEN を禁止しない、である。この裁定は変更しない。

関連: チケット `datafeed-no-tick-minutes-counted-as-gap`、DST 設計 `2026-11-02-utc-time-rules-dst-design.md` §3.6、quiet-market 設計 `2026-10-01-quiet-market-restricted-state-design.md` IV-7 / AC-9。

## 0. 目的と非目標

目的は、開場中の実在する足の欠損だけで判断 mission が情報を失わず、欠損の位置と量を LLM が読めるようにすること。返す価格列は取得した実バーのままとし、補完・並べ替え・間引きはしない。

非目標は、欠損バーの生成、`_MAX_GAP_BARS` の変更、outage 状態機械・`datafeed_outage_gap`・DB schema の変更、共通 validator の既定 `raise` の緩和である。stale、未来時刻、zero/NaN、spike、empty は引き続き fatal である。

### 0.1 前提の再検討（2026-11-03 実測）

11/02 の hold 多発の主因は no-tick ではなかった。冬時間の日曜 21:00–22:00 UTC を開場と誤判定していたためであり、R1 で `market_hours` は NY 17:00 境界へ切り替わった。DST 設計 §3.6 のとおり、旧 AC-1 の 21:00 境界 fixture は使えない。

以下は `file:data/agentic.db?mode=ro` だけを開き、`ohlcv_cache` / `ohlcv_history` の USDJPY バーを `Bar` に復元して、現行 `datafeed.health.validate_bars` を import して実行した結果である。各隣接バー対を、その後側のバーを `now` として検査し、実装どおり `gap_units > 3`、`_closed_minutes`、24 時間窓を通った `DataUnhealthy("gap of ...")` を 1 区間として数えた。これは stale を数えず、gap の位置を重複計上しない方法である。

| データ・期間 (UTC) | 1m | 5m | 15m | 1h | 日次 rollover | 日曜開場 |
|---|---:|---:|---:|---:|---:|---:|
| `ohlcv_cache` / `mt5-live`、2026-11-02 22:00–11-03 08:00 | 0 / 600 行 | 0 / 1m fallback 600 行 | 0 / 40 行 | 0 / 10 行 | 0 | 0 |
| `ohlcv_cache` / `mt5-live`、2026-10-04 21:00–10-30 23:59（夏時間） | 7 / 28,706 行 | 7 / 1m fallback | 0 / 1,924 行 | 0 / 480 行 | 1 | 5 |
| `ohlcv_history` / `mt5`、2026-09-01–09-04 20:55（夏時間、存在する行だけ） | 0 / 2,878 行 (09-02 23:59 まで) | 0 / 1,116 行 | 行なし | 行なし | 0 | 0 |

`ohlcv_cache` には 5m 行が無い。現行 `_cached_bars` は 5m を要求して cache に無ければ 1m を base として先に検査してから導出する (`src/agentic_fx/datafeed/price_provider.py:276-323`) ため、表の 5m は実際に効く 1m 検査の件数である。現在の設定の `intervals` は `[1m, 5m, 15m, 1h, 4h]` (`config/settings.yaml:60`) である。履歴にはこの 9–10 月範囲で 1m/5m しかないため、存在しない 15m/1h を 0 と推定してはいない。

cache の 7 区間は、日曜開場が 5（10/04 に 2、10/11 に 1、10/25 に 2）、夏時間の真の 21:00 UTC 日次 rollover が 1（10/27 21:03–21:09）、それ以外の平日が 1（10/16 09:03–09:13）である。冬時間の実際の rollover は 22:00 UTC であり、R1 後に観測できる 11/02 22:00 以後は 0 だった。

結論は **続ける**。DST 起因の 15m 障害は消えたが、実在する 1m の開場中 gap が 27 日の cache 観測で 7 区間あり、5m cache fallback も同じ検査で止まる。15m/1h で同期間の再現は無かったことは明記するが、裁定済みの全 timeframe 契約を 1m/5m だけに縮めない。これにより、将来の粗い足の実欠損も同じ安全な読み取り契約で扱える。

## 1. 現物と安全境界

| 項目 | 確認済みの現物 |
|---|---|
| gap 検査 | `validate_bars` は `_MAX_GAP_BARS = 3` (`src/agentic_fx/datafeed/health.py:13`) を用い、末尾 24 時間の隣接バー間隔を検査する (`:162-191`)。|
| 数え方 | `gap_units` は両端の開始時刻の間隔であり、欠損本数ではない (`health.py:178`)。連続列から 3 本を除くと `gap_units == 4` で、現在も raise 対象である。|
| 5 経路 | validator 呼出しは直接 native (`price_provider.py:211`)、直接 derived (`:229`)、cache base (`:306`)、cache derived (`:319`)、`_derive` base (`:416`) の 5 箇所。|
| tool 入口 | `_frame` が readonly provider の `get_bars` を呼ぶ (`tools/market_tools.py:70-89`)。`get_ohlcv` は `:96-108`、`get_indicators` は `:110-175`。|
| 資金側 | `db_healthcheck` は outage state と DB 最新足の鮮度だけを検査し、gap は読まない (`service.py:1686-1712`)。executor の OPEN gate も state が `ready` なら通す (`core/executor.py:518-537`)。|
| timeframe 集合 | 設定値は `settings.datafeed.intervals` (`config.py:190-194`)、実装が知る全集合は `1m,5m,15m,30m,1h,4h,1d` (`datafeed/sources.py:18-19`)。4h/1d は base 足から導出される。|

`ready + gaps` の OPEN は実行可能である。本変更はそれを gate で止めないという裁定を明示的に受容する。したがって「outage state と risk gate が gap 一般を止める」とは書かない。state が non-ready、または stale・不正値・spike・empty なら従来どおり止まる。

`datafeed_outage_gap` は欠損の原因判定には使わない。stall でも記録され、終了時刻と原因を持たないためである。DST 設計 §3.6 が econ に引き渡した「既知閉場」の扱いとも独立である。

## 2. 利用者が受ける契約

`get_ohlcv` と `get_indicators` は成功時に常に envelope を返す。どちらにも必ず `bars` と `gaps` がある。`gaps` が空でも省略しない。

```json
{
  "bars": [{"ts":"2026-11-08T22:05:00+00:00","interval":"1m","open":157.0,"high":157.1,"low":156.9,"close":157.0}],
  "gaps": [{"interval":"1m","from":"2026-11-08T22:02:00+00:00","to":"2026-11-08T22:04:00+00:00","missing_bars":3}],
  "indicators": {"sma_20":157.0}
}
```

- `get_ohlcv.bars` は従来の最大 100 本の OHLCV 行である。
- `get_indicators.bars` は組み込み指標へ渡した確定 OHLCV 行であり、`indicators` は従来 top-level だった組み込み値と plugin 値を収める。`interval`、`role`、`insufficient_closed_bars` は `indicators` の中へ移す。これにより top-level envelope は常に同じになる。
- `gaps` の各要素は `interval`、`from`、`to`、`missing_bars` の 4 キーだけを持つ。時刻は UTC ISO 8601、`from` と `to` は観測済み両端の間にある最初と最後の格子バー開始時刻、`missing_bars` は開場中の実効欠損本数である。原因の推測、自由文、synthetic bar は入れない。
- provider が発見した gap のうち、返す `bars` の時刻範囲と交わるものだけを公開する。返却バーの外の 24 時間窓の gap を見せて、前後の実バーが無い状態にはしない。
- 結果は呼出しごとの immutable な値である。別 pair / interval / source の前後呼出しで gap が漏出・消失しない。
- 10 件を超えると新しい 10 件を時刻昇順で返し、top-level `gaps_truncated: true` を付ける。10 件以下は `false`。これは tool 出力の上限を固定し、`true` のときに古い gap が無いとは読ませないためである。

## 3. gap 方針と境界

### 3.1 `raise` と `annotate`

`GapPolicy = Literal["raise", "annotate"]` とし、未知値は `ValueError` にする。既定は `raise` のままである。read tool だけが `annotate` を指定し、取り込み、直接取得、cache の通常経路、`afx init`、healthcheck、scheduler は無指定の `raise` を維持する。

同じ `gap_detail` を 5 箇所の validator 呼出しで使う。`raise` は actionable gap の先頭で従来と同じ `DataUnhealthy` を送出し、`annotate` はその gap を返値へ追加して残りの検査を続ける。`_closed_minutes` がサンプル上限で `None` を返す場合は、両方の方針で fatal のままである。

### 3.2 off-by-one を固定する定義

隣接する既知バー開始時刻を `prev_ts`, `ts`、足幅を `w` 分とする。

1. `gap_units = (ts - prev_ts) / w`。これは両端を含む間隔単位である。
2. 既存の `_closed_minutes(prev_ts, ts, w)` を使い、`open_gap_units = gap_units - closed_minutes / w` とする。休場跨ぎでも health の現在の判定量を変えない。
3. actionable は現在と同じ `open_gap_units > 3` である。
4. `missing_bars = max(0, ceil(open_gap_units) - 1)` とする。よって休場の無い列では、欠損 2 本 = `gap_units 3` は非 actionable、欠損 3 本 = `gap_units 4` は actionable かつ `missing_bars 3` である。`ceil` は `_closed_minutes` の標本推定で端数が出ても、閾値を緩めず整数の欠損本数を過少表示しない丸めである。

`from = prev_ts + w`、`to = ts - w` とする。休場を跨ぐとこの区間には既知閉場の格子も含み得るが、`missing_bars` は上記の開場中実効本数だけである。このため日時範囲と本数の意味が混ざらない。境界 fixture は金曜閉場と日曜開場を含め、`_closed_minutes` のこの丸めを pin する。

### 3.3 derived interval と併合

4h/1d は base 足での検査を維持する。base で発見した annotation は base の `interval`（例 `1h`）のまま carry し、導出後の列で発見したものは要求 interval（`4h` または `1d`）で入れる。同一 `(interval, from, to, missing_bars)` は 1 件に重複除去し、`from`, `to`, `interval` の昇順で整列する。interval を要素に含めるので、導出要求の利用者もどの粒度の欠損かを読める。

`PriceProvider.get_bars` は `BarsRead(bars: tuple[Bar, ...], gaps: tuple[Gap, ...], source: str, origin: str)` を返す。この frozen result が各経路で蓄積した annotation の唯一の運搬物であり、`_bars_source` / `_bars_origin` に似た mutable な gap 状態は作らない。既存の list を必要とする呼出し元は `.bars` を使う。例外時に前回結果を返す経路もない。

### 3.4 fatal のまま残すもの

`annotate` は gap 以外を緩めない。empty (`health.py:152-153`)、stale (`:155-159`)、未来時刻 (`:171-173`)、zero/NaN (`:174-176`)、spike (`:192-194`) は従来どおり fatal である。`check_calendar=False` の参照銘柄は stale と gap を検査しない既存契約のままである。

## 4. agent 規則と quiet-market の改訂

`loops/prompts/trade_mission.md` の「判断の原則」に次の規則を追加する。

> tool 応答の `gaps` が空でないとき、gap をまたぐ、又は gap 後のバーだけでは lookback を満たさない bar-count 指標を単独の根拠にしない。価格列の連続部分、別 timeframe、ニュースで裏付ける。`gaps` だけを理由に OPEN を禁止・HOLD を強制してはならない。`gaps_truncated=true` なら、表示より古い gap がある前提で同じ規則を適用する。

組み込みの SMA/EMA/ATR 等は bar-count で計算され、gap をまたぎ得る。plugin 指標の計算方法は保証しない。これは `ready + gaps` でも OPEN を許す裁定と両立する品質規則であり、risk gate の追加ではない。

同一変更で quiet-market 設計を次の文言に改訂する。

- IV-7: 「判断 mission の read tool は `settings.datafeed.intervals` の全 timeframe と導出 4h/1d について、gap を含む実バーを加工せず、`{bars, gaps}` で返す。欠落だけでは mission を止めない。」
- AC-9: native 1m と 1m fallback の既存 pin を残し、全 configured interval と導出 4h/1d へ共通契約を拡張する。gap は正しい `interval` と区間・本数を含み、cache fallback でも mission を止めないことを検証する。

## 5. 実装配線

| 場所 | 変更 |
|---|---|
| `src/agentic_fx/datafeed/health.py` | `Gap`、`GapPolicy`、共通 `gap_detail` を置く。`validate_bars` は `BarsValidation` を返す。`raise` / `annotate` の gap 処理だけを分岐し、他の検査順序と `_closed_minutes` は維持する。|
| `src/agentic_fx/datafeed/price_provider.py` | 5 箇所すべてに policy を渡し、その invocation の `BarsValidation.gaps` を immutable `BarsRead` に併合する。native、cache、derived base、derived output の成功結果だけを採用する。|
| `src/agentic_fx/tools/market_tools.py` | `_frame` が `annotate` の `BarsRead` を受け、返却バー範囲で gaps を絞る。両 tool を §2 の常時 envelope へ移行し、ToolDef 説明にも `bars` / `gaps` を明記する。|
| `src/agentic_fx/loops/prompts/trade_mission.md` | §4 の規則を 1 段落だけ追加する。|
| `outage.py` / `store/db.py` / `service.py` / `core/executor.py` | 変更しない。`ready + gaps` の OPEN をこの変更で拒否しない。|
| quiet-market 設計 | §4 の IV-7 / AC-9 を同一変更で改訂する。別 commit へ逃がさない。|

## 6. 受入条件

1. **DST 後 fixture。** 旧 21:00 UTC fixture を使わない。NY 冬時間の日曜 2026-11-08 で、22:01 と 22:05 の間の 1m 3 本を除いた固定 OHLC 列を作る。`now=22:06Z`、`market_hours.is_market_open` が gap 内で真であること、`raise` は `gap of 4 open-market bars`、`annotate` は `from=22:02Z`、`to=22:04Z`、`missing_bars=3` を返すことを pin する。以後に連続バーを足して tool の返却範囲に gap を残す。
2. **全 timeframe。** `settings.datafeed.intervals` を `[1m,5m,15m,30m,1h,4h,1d]` とした parameterized test で、各値の `get_ohlcv` と `get_indicators` が gap だけでは例外にせず常時 envelope を返すことを確認する。4h/1d は 1h base の annotation を含む導出経路で確認する。個別設定で interval を減らしても、含まれる全要素に同じ test が掛かる。
3. **常時 response shape。** gap 有無、shortage、indicator plugin 有無で、両 tool の top-level に `bars` と `gaps` が必ずあり、`gaps=[]` を省略しない。`get_ohlcv.bars` は最大 100 本、`get_indicators.bars` は計算入力と一致する。旧 list golden と `insufficient_closed_bars` golden をこの契約へ更新する。
4. **境界。** 1m/5m/15m/30m/1h と derived 4h/1d の各 relevant base で、開場中の欠損 2 本は無注記、3 本は注記かつ `missing_bars=3` とする。`raise` は同じ 3 本で DataUnhealthy とする。金曜閉場から冬時間の日曜 22:00 開場を跨ぐ case は、`_closed_minutes` の端数を `ceil` して §3.2 の本数になることを pin する。
5. **区間・併合・切詰め。** Gap 要素のキーが正確に 4 個で、UTC 時刻・interval・整数本数であること、base と derived の同一要素を重複させないこと、異なる interval は残すこと、返却バー外の gap を公開しないことを確認する。11 件を与えると新しい 10 件を昇順で返し `gaps_truncated=true` とする。
6. **5 経路。** direct native、direct derived、cache direct、cache derived、`_derive` base の全 5 validator 呼出しに `annotate` が透過することを spy で確認する。policy を渡さない呼出しは各経路で従来どおり raise する。`tests/datafeed/test_watch_symbol_bars_health.py` の `validate_bars(` 5 回と `check_calendar=not self._is_raw_watch(pair)` 5 回の pin は維持する。
7. **fatal 維持。** stale、未来、zero/NaN、spike、empty、`_closed_minutes is None` は annotate でも DataUnhealthy である。実取得障害（bridge `Connection refused`）は取得失敗または stale cache で `all bar sources failed` のまま、回復後に gap が無ければ `gaps=[]` である。
8. **quiet-market。** 改訂 IV-7/AC-9 を test で pin する。native 1m、1m cache fallback、設定された 5m/15m/30m/1h、導出 4h/1d のいずれも実バーを補完せず gap だけで mission を停止しない。
9. **OPEN 裁定。** `outage.state="ready"`、tail fresh、gap annotation 有りで mission が OPEN を提案し、既存 RR 等を満たす intent が executor の data-state gate で gap を理由に拒否されないことを characterization test にする。prompt fixture は gap を跨ぐ built-in 指標を唯一の根拠にしないことも評価する。
10. **変異。** (a) annotate を raise に戻す、(b) `missing_bars` を `gap_units` にする、(c) cache または derived base に policy を渡さない、(d) stale を annotate で外す、(e) tool の空 gap を省略する、の各変異で上記の少なくとも 1 test が失敗する。

## 7. 残余と作業単位

- 注記は「ソースにバーが無い」という事実であり、no-tick、bridge 障害、休場誤判定のいずれかを断定しない。
- 長大な gap でも read tool の停止条件にはしない。停止条件は §3.4 の fatal 検査と outage state のままである。
- 4h の週明け stale は gap annotation で直さない。これは stale 規則の別件である。
- econ の障害欠損 / no-tick 分類は `datafeed_outage_gap` から導かない。DST 設計 §3.6 の既知閉場分類と、原因・終了を持つ取得証跡の設計で扱う。

### 7.1 r1-terra 指摘からの反映

| r1-terra 指摘 | 反映節 |
|---|---|
| Critical: `gap_units` と欠損本数の off-by-one | §3.2、AC 4。3 本欠損を `gap_units=4` / `missing_bars=3` と固定。|
| Critical: `ready + gaps` を state/risk gate が止めるという誤記 | §1、§4、AC 9。OPEN 可能を明記し、prompt で品質を割り引く。|
| Important: derived base の interval が応答から不明 | §2、§3.3、AC 2・5。各 gap に `interval` を入れ、併合規則を固定。|
| Important: gap 時だけ response shape が変わる | §2、AC 3。両 tool を常時 `{bars, gaps}` envelope にする。|
| Important: 全 timeframe の範囲不足 | §1、§4、AC 2・8。設定の全要素と 4h/1d を列挙して pin。|
| Important: 旧 DST fixture は stale も混ぜ再現不能 | §0.1、AC 1。冬時間 22:00 開場後の fresh fixture に交換。|
| Important: provider mutable state に annotation を置く危険 | §2、§3.3、AC 6。per-call frozen `BarsRead` だけで運ぶ。|
| Important: quiet-market IV-7 / AC-9 が 1m のまま | §4、AC 8。同一変更で全 timeframe の文言と test を更新。|
| Minor: plugin 指標まで gap 跨ぎ計算を断言 | §4。保証は組み込み bar-count 指標だけに限定。|
| Minor: 返却バー外の 24h gap を示す | §2、AC 5。返却範囲と交わる annotation だけを公開。|
| Minor: 未知の gap policy | §3.1。`Literal` と `ValueError` に閉じる。|
| Minor: 10 件上限の根拠と欠落表示 | §2、AC 5。token 上限、最新 10 件、`gaps_truncated` を固定。|
| Minor: econ への引渡し先が曖昧 | §7。DST 設計 §3.6 の既知閉場分類と取得証跡設計を明記。|

実装は `health.py`、`price_provider.py`、`market_tools.py`、mission prompt、quiet-market 設計と各 test を同一変更に含める。DB migration と設定キーは不要である。

## 8. 続けるか

推奨は **続ける** である。R1 後の 10 時間には gap が無く、15m/1h の過去 cache にも検出は無かった。一方で DST と独立な 1m の実在 gap は 7 区間あり、5m fallback はそれで止まる。全 timeframe という裁定を保った本設計は、その実害を annotation に変え、OPEN の権限を変えない最小の方法である。

利用者への問い: 実測根拠を DST 誤判定ではなく 1m/5m の実在 gap に改めたこの全-timeframe 設計を、このまま実装へ進めるか。

## 9. 変更履歴

| 日付 | 版 | 変更 | 理由 | commit |
|---|---|---|---|---|
| 2026-11-02 | v0.1 | C0 起票 | read tool の穴は閾値でなく注記で扱う | 未コミット |
| 2026-11-03 | v0.2 | DST 是正後の実測、常時 envelope、off-by-one、全 interval、immutable な per-call annotation、OPEN の prompt 規則、quiet-market 改訂と受入条件を追加 | 11/02 の主因が DST 誤判定だったため、残る実在 gap に根拠と fixture を更新 | 未コミット |
