# 参照系列 (株価指数など) を戦略の材料にする — 設計 C0

**版:** v0.1 C0 (指揮者の清書、レビュー前)
**チケット:** `reference-symbols-as-indicator-inputs` (中)、`multi-timeframe-indicator-deps` (低、同束)
**正本の関係:** plugin 契約は `2026-09-14-indicator-consumption-wiring-design.md` (indicators 宣言・resolver・pin) を土台にし、本書はその拡張だけを書く。価格源の規則は `2026-09-24-outage-stop-and-backfill-design.md` を変えない。

## 0. 目的と非目標

利用者の理想像 (2026-10-08): (a) 株価指数・金利などの参照系列とドル円の相関を見る、(b) 参照系列に EMA 等の標準指標を計算する、(c) 戦略 plugin に参照系列を**系列として**渡す。

本束で作るのは、MT5 ブローカーが CFD として配信する参照 symbol を取引 pair と同じ経路で取得・保存し、strategy が indicators 宣言で参照できる形である。**金利と DXY は本束の非目標**とする。本機のブローカー symbol 一覧 (67 本、2026-10-25 に bridge `/symbols` で実測) には債券・金利 CFD と USD index が無く、指数 CFD は `JP225 US500 US100 US30 US2000 GER30 UK100 FRA40 HK50 AUS200`、商品は `USOIL UKOIL` である。金利・DXY は取得経路 (yfinance / FRED) が別物なので、本束の機構が実機で動いた後に別チケットで足す。

非目標 (本束で作らない): 参照 symbol への発注 (永久に作らない)、yfinance / FRED 経路、参照系列専用の新しい plugin kind、LLM への参照系列の自動注入 (tool で読めるだけにする)、ニュース連動。

## 1. 用語

| 語 | 意味 |
|---|---|
| 取引 pair | `settings.pairs` の通貨ペア。発注対象。 |
| 参照 symbol | `datafeed.watch_symbols` の symbol。取得・保存・分析・strategy の入力に使うが**発注対象にならない**。`pairs` との重複は設定エラー。 |
| 参照系列 | 参照 symbol の OHLCV (足別)。 |
| 整列 (asof) | strategy の足 `t` に対し、参照系列の「`t` 時点で確定済みの最新 bar」を対応させること。 |
| 鮮度切れ (stale) | 整列した bar が古すぎる (後述の `max_age`) とき。値は NaN になり、理由が残る。 |

## 2. 利用者から見た動き

### 2.1 設定と初回設定

`config/settings.yaml` の `datafeed.watch_symbols` にブローカーの symbol 名をそのまま書く (例 `[JP225, US500]`)。`afx init` の価格源の段 (bridge 接続後) で、`/symbols` の一覧から指数 CFD 候補を提示して任意に選べるようにする (選ばなければ空のまま。既定値を入れない。製品像「既定は安全側」)。`afx check` は watch_symbols の各 symbol が bridge の `/symbols` に実在することを検査し、無ければ fatal ではなく warning (`watch_symbol_unknown`) で名前を出す。

`pairs` と `watch_symbols` の重複、`analysis.max_watch_symbols` 超過は起動拒否 (現行どおり)。

### 2.2 取得と保存

ingest は参照 symbol を取引 pair と同じ `Ingest` の鍵 (`(symbol, interval)`) として扱い、bridge `/ohlcv/{symbol}` から取り、`ohlcv` に `symbol` 列で区別して保存する。取る足は **strategy / indicator が宣言した参照 interval だけ** (後述の宣言から registry が要求を作る。宣言が無ければ取らない)。1m は取らない (paper / mtm / hwm は取引 pair だけの要求)。

参照 symbol の要求は **soft** (`hard=False`) とする。参照系列が取れなくても取引は止めず、`DataUnhealthy` にもしない。取れない状態は `activity` の `reference_stale` (symbol、最終 bar、理由) と `afx ctl status` の `references:` 行 (symbol ごと `ok | stale(<age>) | missing`) で見える。取引 pair の価格源が止まる既存の規則 (停止 + backfill) は変えない。

`afx history import` は `--symbol JP225` を受け、`ohlcv_history` に保存する (backtest 用)。`afx history coverage` は参照 symbol も表に出す。

### 2.3 相関を見る

`afx analyze corr --a USDJPY --b JP225` は既存の `corr_matrix` / rolling 相関がそのまま動く (`ohlcv_history` に参照 symbol が入るため)。新しい表示は作らない。

### 2.4 strategy が参照系列を使う

indicators 宣言に任意キー `symbol` と `timeframe` を足す。

```yaml
indicators:
  rsi:    {plugin: rsi_wilder, params: {period: 14}}                       # 従来どおり: 取引 pair、strategy の足
  jp_ema: {plugin: ema, symbol: JP225, timeframe: 1h, params: {period: 50}}  # 参照 symbol の 1h に EMA(50)
  jp:     {plugin: ohlcv, symbol: JP225, timeframe: 1h}                      # 系列そのもの (passthrough indicator)
  rsi_4h: {plugin: rsi_wilder, timeframe: 4h, params: {period: 14}}          # 取引 pair の別足 (multi-timeframe)
```

- `symbol` 省略 = 取引 pair、`timeframe` 省略 = strategy の足。`symbol` は `watch_symbols` か `pairs` の要素でなければ loader が reject (`indicator_ref_unknown_symbol`)。
- indicator plugin は従来どおり df を受けて計算するだけで、symbol を知らない。**系列そのものを渡す**のは、人間提供の indicator `ohlcv` (outputs `open high low close volume`、入力をそのまま返す) で行う。`evaluate(df, indicators, ...)` の契約は変えない。strategy は `indicators["jp"]["close"]` で JP225 の終値系列 (strategy の足に整列済み、鮮度切れは NaN) を読む。
- ハーネス (strategy worker の composition root) が、宣言ごとに `(symbol, timeframe)` の df を `load_resampled_frame` 相当で作り、indicator を計算し、結果を strategy の足へ asof 整列して渡す。`timeframe` だけ違う宣言 (`multi-timeframe-indicator-deps`) も同じ機構で解ける。
- 1 strategy の宣言数の上限は現行 `MAX_INDICATOR_DEPS = 8` の内数。handshake の総 byte 上限 (`MAX_HANDSHAKE_BYTES`) に、参照系列の df も数える。

### 2.5 整列と鮮度の規則 (時刻の契約)

strategy の足の bar `t` (bar_time) に対して、参照系列 `(symbol, tf)` から **`bar_time + tf ≤ t` を満たす最新の bar** を取る (未確定足は使わない)。その bar の `age = t - (bar_time + tf)` が `max_age` を超えたら、その `t` の値は NaN とし、`stale` を記録する。`max_age` の既定は `tf × 3` (宣言で `max_age: 6h` のように上書き可、上限 `7d`)。

この規則は live (strategy worker) と backtest (`run_backtest` / `afx backtest run`) で**同一の関数**を通す。参照 symbol は取引 pair と取引時間が違う (休場・祝日・取引所時間) ので、週明けや休場日の挙動を**実時刻の fixture で pin** する ([[time-contracts-need-wallclock-integration-tests]]: 鮮度の契約はレビューを通っても実機で退行した)。

backtest で参照履歴が無い区間は、NaN にせず **`reference_history_missing(<symbol>, <tf>, <from>..<to>)` で fail closed** とする (無言の NaN は「参照していない strategy」と区別できず、PF の比較を汚す)。live では NaN + stale 記録 (取引を止めない)。

### 2.6 LLM と改善 mission

- 取引判断 loop の tool `get_ohlcv(pair, timeframe)` / `get_indicators(pair, timeframe)` は `pair` に参照 symbol も受ける (読取のみ)。発注 tool は従来どおり `settings.pairs` だけを受ける (決定論的)。prompt に参照系列を自動では入れない (方針文で指示できる)。
- 改善 mission: `list_deployed_plugins()` の応答に `watch_symbols` と各 symbol の履歴範囲を足す。族表の key に参照 symbol を含める (`strategy @1h [JP225]`)。`run_backtest` は参照系列込みで動く。prompt の規律 7 の「無い指標は自前計算しない」はそのまま、「参照系列は `symbol:` 宣言で取る (自前で別 symbol を読まない)」を 1 文足す。
- `afx ctl status` の `references:` 行と、`afx status` (offline) の `references:` 行 (保存済み履歴の範囲だけ)。

### 2.7 発注対象との区別 (安全)

- `settings.pairs` と `watch_symbols` の重複は起動拒否 (現行)。
- executor / risk gate は `Order.pair ∈ settings.pairs` を既存の決定論的検査で強制しており、参照 symbol は到達しない。本束で LLM の発注 tool に参照 symbol が渡ったときの reject 文言 (`not_tradable_symbol`) を pin する test を足す。
- 参照系列が止まっても kill switch / drawdown の規則は不変。

## 3. 内部の変更 (大きさの見積り)

| 層 | 変更 | 大きさ |
|---|---|---|
| config | `IndicatorRef` に `symbol` / `timeframe` / `max_age`、loader reject 語彙 3 つ、`watch_symbols` 実在検査 (check) | 小 |
| datafeed | `build_registry` が宣言から `(watch_symbol, tf)` の soft 要求を作る、ingest の鍵に参照 symbol、`reference_stale` activity、status の `references:` | 中 |
| store | 変更なし (`ohlcv` / `ohlcv_history` は symbol 列で足りる) | 無 |
| plugin / resolver / worker | 宣言ごとの df 作成と asof 整列 (共通関数 `align_reference`)、handshake に系列を複数、`max_bars` の参照側換算 (`ceil(max_bars × strategy_tf / ref_tf) + warmup`)、人間提供 indicator `ohlcv` | 中〜大 |
| backtest | `run_backtest` / `afx backtest run` が同じ整列関数で供給、`reference_history_missing` | 中 |
| tools / prompt | `get_ohlcv` / `get_indicators` の symbol 許容、`list_deployed_plugins` の watch_symbols、族表 key、規律 1 文 | 小 |
| wizard / check / history | 価格源の段で候補提示、`check` の実在検査、`history import --symbol` | 小 |
| 文書 | ops-api (status 行)、first-run-acceptance、plugin 作者向け契約の追補 | 小 |

task 分割案: **T1** 設定・取得・保存・status (ingest/registry/check/history) → **T2** 契約・resolver・整列関数・worker・`ohlcv` indicator (時刻 pin を含む) → **T3** backtest と改善 mission → **T4** wizard・tools・文書。T2 が本丸で、T1 と独立に test-first できる (整列関数は純関数)。

## 4. 裁定が要る点 (ユーザー)

技術で決まるものは問いにしない。製品方針・運用前提に当たる 5 点だけ。

| # | 問い | 指揮者の推奨と根拠 |
|---|---|---|
| R1 | 参照系列の取得失敗で取引を止めるか | **止めない (soft)**。参照系列は材料であり資金保護の前提ではない。止めると指数の休場日に毎回停止する。stale は activity と status で見せる |
| R2 | backtest で参照履歴が無い区間 | **fail closed**。無言の NaN は PF 比較を汚す (改善目標 [[improve-objective-filtered-signal-quality]] は選別後の期待値が測れること) |
| R3 | 系列を渡す形 | **passthrough indicator `ohlcv`** で契約不変。`evaluate` の引数を増やす案は配備済み strategy 2 本と example、改善 prompt、validator の全面改訂になる |
| R4 | 初回設定で候補を提示するか、既定値を入れるか | **提示するが既定は空**。製品像「既定は安全側、使う人が育てる」 |
| R5 | 金利・DXY を本束に入れるか | **入れない**。bridge に無く取得経路が別。機構が実機で動いてから別チケット |

## 5. 受入条件 (下書き、C1 で確定)

- AC-1 `watch_symbols` と `pairs` の重複、未知 symbol (check の warning)、上限超過の 3 経路。
- AC-2 ingest が宣言された `(symbol, tf)` だけを soft で取り、取引 pair の hard 要求と `DataUnhealthy` の判定が不変 (参照 symbol の全停止でも取引継続)。`reference_stale` activity と status の `references:` 行。
- AC-3 整列関数: 未確定足を使わない、`max_age` の境界 (`L-1 / L / L+1` 足)、週明け・休場日の実時刻 fixture、live と backtest で同じ関数 (配線 test)。
- AC-4 loader: `symbol` / `timeframe` / `max_age` の受理と reject 語彙、`MAX_INDICATOR_DEPS` と handshake 上限の内数。
- AC-5 worker: `indicators["jp"]["close"]` が strategy の index に整列した Series、stale 区間が NaN、`max_bars` の参照側換算で warmup が足りる (全 NaN にならない)。
- AC-6 backtest: `reference_history_missing` の fail closed、同じ strategy の live / backtest で同一入力なら同一 signal。
- AC-7 発注 tool に参照 symbol → `not_tradable_symbol`、executor に到達しない。
- AC-8 `analyze corr` が参照 symbol で動く、`history import --symbol`、`coverage` の表示。
- AC-9 改善 mission: `list_deployed_plugins` に watch_symbols、族表の key、prompt の 1 文、`run_backtest` が参照系列込み。
- AC-10 zero-write / 既存 golden (`afx --help` 等) の不変、`afx status` の `references:` 行。

## 6. 残余と後続

- 金利・DXY (yfinance / FRED 経路、別チケット)。
- 参照系列を LLM prompt に自動注入する案 (方針文で足せるので保留)。
- 参照 symbol の 1m 取得 (paper / mtm に不要。相関分析を 1m で行う要求が出たら)。
- 複数の取引 pair と複数の参照 symbol の直積で handshake が肥大する場合の上限見直し。

## 7. 変更履歴

| 日付 | 版 | 変更 | 理由 | commit |
|---|---|---|---|---|
| 2026-10-25 | v0.1 C0 | 新規作成。ブローカー symbol の実測 (指数 CFD 10 本、金利・DXY なし) を前提に、取得 (soft)、宣言 (`symbol` / `timeframe` / `max_age`)、passthrough indicator `ohlcv`、整列と鮮度の規則、fail closed の backtest、裁定 5 点、AC 下書き、task 4 分割 | チケット reference-symbols-as-indicator-inputs の設計待ちを C0 に | |
