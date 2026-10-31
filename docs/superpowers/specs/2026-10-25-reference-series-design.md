# 参照系列 (株価指数など) を戦略の材料にする — 設計 C1

**版:** v0.6.6 C1 (裁定済)（T2 実装・レビューで確定した事項を反映）
**チケット:** `reference-symbols-as-indicator-inputs`（中）、`multi-timeframe-indicator-deps`（低）、`executor-pair-membership-guard`（高、T1 に含める）
**正本の関係:** indicator の loader / resolver / pin は `2026-09-14-indicator-consumption-wiring-design.md` を拡張する。価格源の outage / backfill 規則は `2026-09-24-outage-stop-and-backfill-design.md` を変更しない。

## 0. 目的・範囲

利用者は USDJPY と指数等の相関を見、参照系列に EMA 等を計算し、strategy plugin に系列として渡せるようにしたい。本束は MT5 bridge が配信する CFD の broker raw symbol を、**分析専用・発注不可**の参照系列として取得し、strategy の indicator 入力へ渡す。

対象は実測済みの `JP225 US500 US100 US30 US2000 GER30 UK100 FRA40 HK50 AUS200` と `USOIL UKOIL` である。金利・DXY、yfinance / FRED の追加経路、参照 symbol の発注、新 plugin kind、LLM prompt への自動投入、ニュース連動は対象外とする。金利・DXY は本束の運用実績後に別チケットで扱う。

## 1. 現物に合わせた前提と語彙

### 1.1 確認済みの現状

| 項目 | 現物 | C1 の決定 |
|---|---|---|
| live 取得 | `sources.mt5_bars_range()` は `VENDOR_SYMBOLS[logical]["mt5"]` を通す。`JP225` は表に無く `KeyError` | logical pair 用と混ぜず、broker raw symbol を `/ohlcv/{symbol}` へ渡す MT5 専用 adapter を追加する |
| source / 保存 | `Ingest` は全 key に単一の `datafeed.primary` と `storage_source` を使う。live は `ohlcv_cache`、MT5 の source 名は `mt5-live` | `watch_symbols` 非空なら `datafeed.primary == mt5` を必須にし、参照も `ohlcv_cache` / `mt5-live` に保存する |
| 履歴 | `history import --symbol` は既にあり、MT5 import は raw symbol を `/ohlcv/{symbol}` に送る。保存先は `ohlcv_history`、source は `mt5` | 新 CLI は作らない。同一 `source=mt5`・同一 base interval で取引 pair と参照 symbol を import する |
| 大足 | `load_closed_frame()` は live の 4h / 1d を 1h から UTC epoch 格子へ resample する | 参照系列も native MT5 H4/D1 を直接混在させず、4h / 1d は 1h 要求から導出する |
| 依存 IPC | `MAX_HANDSHAKE_BYTES` は metadata 用。worker `call()` は単一 `df` を無上限で JSON 化する | startup metadata と call data を分離し、named frames と別の `MAX_CALL_INPUT_BYTES` を導入する |
| 発注防衛 | `trade_intent_schema()` の enum は誘導だけで、`Executor` に `settings.pairs` membership guard は無い | executor の共有 open 境界で、quote / spec / broker より前に reject する |
| family key | 現行 grammar は `strategy:<indicator-names>@<tf>@<pairs>`。pair は設定所属の大文字 6 文字 | 旧 grammar は legacy として parse 継続し、新 versioned grammar を導入する |

`GET /symbols` は bridge (`mt5_bridge/server.py`) が `list[str]` を返すことまで確認した。実 broker の銘柄一覧は 2026-10-25 の実測に依る。参照 CFD 固有の取引所 calendar / holiday API は存在を確認できていないため、本束では導入しない。

| 語 | 意味 |
|---|---|
| 取引 pair | `settings.pairs` の通貨ペア。発注対象。 |
| 参照 symbol | `datafeed.watch_symbols` の MT5 raw symbol。取得・保存・分析・strategy 入力には使えるが発注対象外。 |
| 主足 | strategy の `timeframe`、strategy DataFrame の index は bucket start。 |
| 参照足 | indicator 宣言が解決した effective timeframe。4h / 1d は 1h から導出する。 |
| `s` / `d` | strategy row start と、その row を評価できる effective time。`d = s + strategy_tf`。 |
| `r` / `e` | reference row start と end。`e = r + ref_tf`。 |
| unavailable | 必要な参照入力が missing / stale / fetch failure 等で使えず、当該 strategy を deterministic hold/skip にする状態。 |

## 2. 動作契約

### 2.1 設定、source、安全境界

`datafeed.watch_symbols` は MT5 broker の raw name を保持する。空でない場合は `datafeed.primary: mt5` を必須にする。yfinance / Twelve Data を primary にしたまま参照 series を混ぜる key ごとの source routing は作らない。raw symbol 専用 adapter は `sources.mt5_bars_range()` と異なる入口にし、`VENDOR_SYMBOLS` を通さない。取引 pair の logical-to-vendor 変換と raw symbol を同じ引数型として曖昧にしない。

既存 schema v4 では `pairs` / `watch_symbols` の overlap と watch 内 duplicate が通る。C1 は **settings schema v5** に上げ、両方を migration error（自動削除なし）として起動を止める。利用者は片方を明示して消してから `afx init` を再実行する。`max_watch_symbols` 超過も起動拒否のままである。各 `watch_symbols` は UTF-8 で **32 bytes 以下**と設定検証し、超過は固定 reason `watch_symbol_too_long` で reject する。これは実測対象と MT5 broker の通常の raw symbol（短い指数名に `.cash` 等の suffix を付ける形）を十分収めながら、任意長の symbol が family key の既存 512-byte 契約を圧迫しないようにする現実的な上限である。`afx check` は bridge 接続可能時に `/symbols` と突合し、未知値を `watch_symbol_unknown` warning として表示する。接続不能時は既存 bridge check の safe error を使い、unknown と偽装しない。

初回 wizard は固定 stage を増やさない。段 3 の「MT5 接続済み」分岐でだけ `/symbols` 成功後に候補を任意選択させ、初回既定は空、再実行既定は現在の `watch_symbols` とする。bridge 未接続、認証、timeout は既存 probe と同じ安全なエラーへ送り、値を黙って空にしない。選択結果は段 9 の要約に出し、段 10 の commit まで settings を書かない。yfinance / Twelve Data 分岐には出さない。

参照 symbol は `settings.pairs` に決して昇格しない。T1 で `action=open and intent.pair not in settings.pairs` を executor の通常 open と five-phase の両入口で `not_tradable_symbol` reject にする。検査は `quote_fn`、`spec_fn`、broker の前であり、`risk.pair_rules` の余剰 key (`pairs` に無い pair の規則) は起動時に警告する (拒否しない。発注の境界は `pairs` の membership 検査で、余剰の規則は取引対象を増やさない。同梱の設定例と既存の個人設定を壊さないため)。trade-intent schema の enum は `settings.pairs` のまま保つ。

### 2.2 取得、保存、状態表示

resolver が解決した `(effective_symbol, native_interval)` を `RequirementRegistry` の key とする。現行の `Requirement(pairs, interval, ...)` と `settings.pairs × required_intervals` の直積を置換し、同じ key に hard / soft が重なれば `hard = any`、要求本数は最大値に畳み込む。取引 pair の paper / MTM / HWM / health は hard、参照用 indicator の要求は soft である。

各 ingest tick は hard queue を全て先に試し、soft は残予算だけを使う。soft を deferred にしても次 tick の hard より先には置かない。soft の empty / error には interval 上限付き capped exponential backoff を適用し、FX 用 `market_hours.next_bar_confirmation()` を指数 CFD の次回時刻に流用しない。`budget_exhausted`、`IngestTickReport`、activity は hard deferred と soft deferred、attempted と backoff 待ちを区別する。soft の失敗・empty は `DataUnhealthy` と outage state を変えない。

soft の再要求 due は key ごとに `max(backoff due, 前回要求 + 足幅)` とし、empty / 例外とも同じにする。定常状態では任意の連続する足幅の窓で外向き要求は 1 回以下である。

soft は、その key を要求する全 entry の reason が `reference` のときだけ（`RequirementRegistry.is_reference`）である。`settings.pairs` の symbol の key は参照として要求されても hard のまま毎 tick 再試行し、outage / 起動 disposition の既存の意味を変えない。

live の参照 bar は `ohlcv_cache` に `symbol`、native `interval`、`source="mt5-live"` として保存する。取得は resolver が要求した参照足だけで、参照 1m は作らない。ただし 4h / 1d の宣言は native 1h を要求して UTC epoch 格子で resample する。`ohlcv_history` は import 専用で、live cache と混同しない。

status は `(symbol, timeframe, source)` ごとに `ok | stale | missing | fetch_failed | budget_deferred | source_unavailable | plan_failed | symbol_rejected` を表示し、daemon 側 `REFERENCE_STATES` を正本として CLI は import する。

`plan_failed` は planner 失敗を毎 tick 再評価し key ごとに一度だけログし、`symbol_rejected` は許可されない symbol を key ごとに一度だけログして再起動まで再評価しない。daemon は status 行を切り詰めず、CLI は表示件数と総数から `… 他 N 件` を示す。

activity の `reference_stale` の `reason` は `FETCH_STATE_REASONS`（`source_unavailable | fetch_failed | budget_deferred | plan_failed | symbol_rejected`）に `missing | age_exceeded | indicator_unavailable` を加えた閉集合だけを使い、runtime 超過は `reference_window_exceeded_at_runtime` とする。

参照状態は `(strategy, pair, symbol, timeframe)` ごとに持ち、activity 本文にも pair を載せる。`reference_recovered` は同じ `reason` を持たず、直前値を `recovered_from` に置く。いずれも状態遷移時だけ記録する。`afx status` は offline の保存済み履歴範囲だけを示し、live の freshness を推測しない。

### 2.3 indicator 宣言、解決、worker

宣言は既存 `indicators` に optional `symbol`、`timeframe`、`max_age` を加える。

```yaml
indicators:
  rsi:    {plugin: rsi_wilder, params: {period: 14}}
  jp_ema: {plugin: ema, symbol: JP225, timeframe: 1h, params: {period: 50}}
  jp:     {plugin: ohlcv, symbol: JP225, timeframe: 1h, params: {}}
  rsi_4h: {plugin: rsi_wilder, timeframe: 4h, params: {period: 14}}
```

loader は syntax だけを扱う。`symbol` は `self` または非空文字列、`timeframe` は既存 timeframe enum、`max_age` は後述の duration grammar とし、新 field を含む allow-list と固定 reject 語彙を持つ。settings membership、`self` の実 pair、effective timeframe、default max_age は resolver / composition root が解決する。literal は `settings.pairs + settings.datafeed.watch_symbols` のいずれかであり、取引 pair を参照するときの key は §59 により hard のままである。`symbol` 省略は固定 pair 名ではなく `self` である。`ResolvedIndicator` と approval payload、handshake metadata、監査表示に `symbol_mode`、literal symbol、effective timeframe、canonical max-age seconds を持たせる。indicator plugin の content hash / pin は従来どおり plugin 内容だけであり、入力座標は strategy config content hash で署名されるため pin hash には混ぜない。

`max_age` は `^[1-9][0-9]*(s|m|h|d)$`（UTC 実経過時間）とし、bool、zero、負値、小数、別単位を reject する。内部 canonical は整数秒、approval / config hash / audit は canonical seconds を使う。default は effective reference timeframe の 3 倍、default 適用後も 7d 以下を必須とする。fresh の境界は `age <= max_age`、stale は `age > max_age` である。

`evaluate(df, indicators, ...)` は変えない。startup handshake は metadata のみで `MAX_HANDSHAKE_BYTES` に従う。各 call は main `df` と、deduplicate した `(effective symbol, effective timeframe)` ごとの named frame を別 payload field で送り、alias は frame id を参照する。同一座標を複数 alias が共有しても frame は 1 回だけ送る。untrusted indicator の import / compute / validator は worker 内に留める。

`MAX_CALL_INPUT_BYTES = 3_145_728`（3 MiB）とする。計測対象は `_df_to_wire` 後に pipe へ書く request の UTF-8 JSON byte 列であり、末尾改行 1 byte を含む。`max_bars_limit=1000`、OHLCV 5 列、main 1 frame + 最大 8 alias の重複排除後 reference frame 8 個という代表的な最大 fixture は **1,444,312 bytes の見積り**である。ただし timestamp の小数秒、float の文字列表現、params と wrapper の serialization から一定の事前上限は導かない。安全境界は唯一、pipe write の直前に request を serialize した後の UTF-8 byte 数（改行込み）の実測とする。超過は固定 error code `call_input_too_large` で call だけを reject し、worker session は死亡扱いにせず再利用可能とする。

依存 plugin の現行 `max_bars` は「worker に渡せる最大本数」であり warmup ではない。この束は任意 indicator の warmup を推論もしないし、`max_bars` を warmup 下限として reject もしない。参照系列の必要 effective bar 数は config 時の名目値と実行時の実測値を分ける。resolver は `K_ref_nominal = ceil(((strategy.max_bars - 1) × strategy_tf) / ref_tf) + 1` を用い、これが当該 indicator の `max_bars`（設定上限 1000）を超える明白な不成立だけを固定 reason `reference_window_exceeds_max_bars` で config reject する。main `df` 取得後は、その最初と最後の判定時刻 `d_first` / `d_last` が覆う実時間範囲から `K_ref_actual = ceil((d_last - d_first) / ref_tf) + 1` を再計算する。`+1` は両端を包含する境界分である。

参照 frame は `d_last − max(K_ref_actual × ref_tf, max_age + ref_tf)` まで読み、鮮度判定に必要な直近 bar を落とさない。`K_ref_actual` が上限を超えたときは main 窓を縮小したり先頭の参照値を黙って欠損させたりせず、当該 strategy を固定 reason `reference_window_exceeded_at_runtime` で fail-closed skip する (activity に記録し、判断足ごとに 1 回)。既存の self・同一 timeframe dependency は従来どおり main `df` を使い、参照用 `K_ref` の config reject / runtime 上限判定の対象外とする。native 要求本数は `ceil(K_ref_actual × ref_tf / native_tf)` とし、native 1h から導く 4h は `4K_ref_actual`、1d は `24K_ref_actual` を要求する。不足や未完成 bucket は値を捏造せず unavailable とする。従って seed 不足を含む indicator 出力 NaN は live / backtest とも有効な unavailable 入力として strategy を skip する。`ohlcv` は seed 不要の passthrough である。`K_ref_nominal` は名目であり週末・欠損では `K_ref_actual` が延びるため、作者は参照 indicator の `max_bars` を名目より余裕をもって宣言し、例と prompt にこの制約を明記する。

人間提供の `ohlcv` indicator は approval 済み inventory に materialize → submit / bless して初めて利用できる。outputs は順に `open high low close volume`、各 output は同じ index の numeric Series で対応する入力列と完全一致、入力 mutation なし、NaN を新規生成しない、全 prefix で出力 prefix が一致、を validator と prefix test で pin する。追加 field を省略した既存 config は旧 effective 値へ解決し、既存 2 strategy と example の content hash は不変とする。

### 2.4 UTC 時刻契約と strategy 単位 fail-closed

共通純粋関数 `align_reference` の座標は次だけである。

```text
strategy row start s; decision time d = s + strategy_tf
reference row start r; reference end e = r + ref_tf
eligible: e <= d
chosen: eligible な最大 e
age: d - e
fresh: age <= max_age; stale: age > max_age
```

live loader はその前段で `e + closed_bar_grace <= now` の bar だけを渡す。backtest loader は historical `d` までの `e <= d` の履歴を渡す。loader / availability 判定は source ごとに異なるが、閉じた input frame への asof・age 判定は同じ純粋関数を使う。`load_resampled_frame(until=d)` の排他 SQL と bucket end `<= d` はこの契約に一致する。

参照市場の個別 calendar は本束で持たない。`max_age` は UTC 実経過時間であり、休場中も進む。FX 取引 pair 自身が週末 21:00 UTC 境界で休場していても参照 symbol の `max_age` は進む。これは calendar を持たない方針の帰結である。従って live の取得側でも、参照 symbol (watch_symbols の raw name) の frame には FX 前提の鮮度判定と開場中 gap の連続性検査を掛けない (zero / NaN / spike の検査だけ残す)。鮮度は alignment の `max_age` が唯一の判定点である。取引 pair の検査は従来どおり。

したがって合法的休場と内部 gap を厳密には区別しない。backtest preflight の検査対象は「参照 symbol / source / base interval の履歴が完全に存在しない」だけであり、seed や indicator 出力を事前判定しない。休場、seed 不足、indicator NaN は live / backtest とも同じ unavailable/skip である。

soft はシステム全体を degraded にしない、という意味だけである。required reference frame が unavailable、bucket が欠損、または指標値が unavailable の strategy は `reference_unavailable` / `reference_stale` を記録して evaluate / signal / open を行わない。

どちらも最古 bucket で停止し cursor を進めず、参照が戻れば同じ bucket から再評価する。他 strategy、LLM loop、既存の価格源 outage 規則、kill switch、drawdown 保護は継続する。これにより live の NaN 実行と backtest abort の非対称を無くす。

実時刻 fixture（すべて UTC）:

| ケース | 取引 pair 主足 row | 参照足 | chosen / age | 結果 |
|---|---|---|---|---|
| 通常 | USDJPY 1h, `s=2026-10-26T09:00Z`, `d=10:00Z` | JP225 1h `r=09:00Z`, `e=10:00Z`, `max_age=3h` | `e=d`, age 0 | 09:00–10:00 の同時確定 bar を使う。1 本遅れない。 |
| 週末休場 | USDJPY 1h, `s=2026-10-26T22:00Z`, `d=23:00Z` | fixture 上の JP225 最終 `r=2026-10-23T14:00Z`, `e=15:00Z`, `max_age=84h` | age 80h | calendar がなくても UTC age 内なので fresh として同じ終値を使う。`d=2026-10-27T04:00Z` では age 85h で stale/skip。 |
| 内部欠損 / stale | USDJPY 4h, `s=2026-10-27T08:00Z`, `d=12:00Z` | JP225 1h の最終 `r=07:00Z`, `e=08:00Z`, `max_age=3h` | age 4h | 08:00–12:00 に何が起きたかは calendar で判定せず stale/skip。`d=11:00Z`（age 3h）は fresh。 |
| 4h 導出 | USDJPY 4h, `s=2026-10-27T08:00Z`, `d=12:00Z` | JP225 の 1h `08:00,09:00,10:00,11:00` を UTC 4h bucket へ集約、`e=12:00Z` | age 0 | MT5 native H4 を読まず 1h から導出した 08:00–12:00 bar を使う。 |
| native 本数 | main の `d_first`〜`d_last` | `K_ref_actual = ceil((d_last-d_first)/ref_tf)+1`。4h は完全な 1h bucket を `4K_ref_actual` 本、1d は UTC 00:00 起点の完全 bucket を `24K_ref_actual` 本で作る | 先頭が 1 本不足なら最初の effective bar は unavailable、末尾の部分 bucket は除外 | 部分 bucket を有効な 4h / 1d bar として渡さない。resolver は `K_ref_nominal = ceil(((strategy.max_bars-1)×strategy_tf)/ref_tf)+1` のみで明白な不成立を `reference_window_exceeds_max_bars` reject し、実測 `K_ref_actual > indicator.max_bars` は当該 strategy を fail-closed skip する。 |

### 2.5 履歴、分析、改善 mission、read tool

backtest は取引 pair と全参照 symbol を一つの `HistoryDataset(source, base_interval)` から読む。`run_backtest` 前に全宣言を preflight し、同じ import source/base interval に reference history が完全に無い場合だけ R2 で選んだ処理を業務副作用前に適用する。推奨 fail-closed は `reference_history_missing(<symbol>, <tf>, <reason>)` (reason は固定語 `no_rows`) の固定応答 `started:false` として **`backtest_runs` の run 行を作らない**。signal、order、候補 archive 等の業務副作用も 0 件とする一方、RPC ledger、mission transcript、activity の監査記録は残す。seed 不足、indicator NaN、合法的休場 / age stale はこの missing 判定にしない。`analyze corr` は既存実装のまま、両 symbol が同じ `--source`、1m、30 以上の共通 timestamp を持つ場合だけ動く。live で JP225 1h を取っただけでは相関分析に使えない。`history coverage` は既存どおり単一 `--symbol` の表示であり、参照一覧の表を新設しない。

`get_ohlcv` と `get_indicators` だけは market-read 専用の `read_symbol_param()` を新設して `pairs + watch_symbols` を enum / runtime membership ともに検査する。共通 `market_tools.pair_param()` は拡張しないため、`get_signals` と trade-intent schema は pairs 限定のままである。read tool も MT5 primary / raw symbol 契約に従う。

改善 mission の `list_deployed_plugins()` には bounded な top-level `watch_symbols`、参照できる全 symbol (取引 pair + watch_symbols) の `reference_symbols`、`history_available` (reference symbol ごとの `{symbol: true|false}`、同じ dataset に 1 行以上あるか) だけを載せる。履歴の開始・終了時刻や成績・holdout 境界を inventory sink に出さない。prompt 規律には「参照系列は `symbol:` 宣言で取り、自前で別 symbol を読まない」を加える。

family identity は新 grammar `strategy-v2:<strategy_tf>@<trading-pairs>@<encoded-resolved-deps>` にする。各 dep は alias の宣言順で `alias;plugin;symbol_mode;literal_symbol_or_-;ref_tf` を percent-escape した token とし、alias、plugin、self/literal symbol、reference timeframe、順序を全て区別する。escape `E` は UTF-8 byte 列に対し RFC 3986 unreserved（`A-Z a-z 0-9 - . _ ~`）だけをそのまま出し、それ以外を大文字 hex の `%XX` にする。各 variable field に `E` を適用してから grammar の literal separator `@`、`,`、`;` を結ぶ。decoder は不正な `%`、小文字 hex、safe 文字の過剰 escape、decode 後に再 encode して原文と一致しない値を reject する。既存の `FAMILY_KEY_MAX_BYTES = 512` は上げない。resolver は生成後の encoded family key が 512 bytes 以下であることを検査し、超過は固定 reason `family_key_too_long` で config reject する。literal symbol は `watch_symbols` membership を満たし、その UTF-8 32-byte 上限にも従う。新 parser はこの正規形だけを受け、既存 `strategy:<names>@<tf>@<pairs>` は legacy として parse / 表示のみ維持する。旧 key は新 key と同一族に畳み込まない。これは scalar を落とす config shape hash だけでは JP225 と US500 を区別できないためである。

## 3. 実装 task（直列）

| task | 内容と成果物 | AC | 依存 | exit criteria | 見積り | 担当 |
|---|---|---|---|---|---:|---|
| T1 契約・安全境界 | schema v5 migration、MT5-only/raw symbol、`IndicatorRef` / resolver / audit、duration、named-frame payload と call 入力上限、executor membership guard、時刻純関数と UTC fixture | AC-1, 3–6, 11, 14 | なし | schema / resolver、`risk.pair_rules` 余剰 key の警告、all-open-path I/O 0、protocol の limit / limit+1 / shared-frame、UTC / 4h / 1d の安全境界試験が全て通る | 650–850 行 | sonnet（通常 / five-phase の競合境界試験を含む） |
| T2 live 取得・worker 配線 | symbol-keyed registry、hard-first/soft backoff、raw MT5 adapter、1h→4h/1d、named frames、`ohlcv` 配備、strategy skip、status/activity | AC-2, 5–7, 12, 14 | T1 | capped exponential backoff の増加列と cap、`K_ref_actual`→native 本数、4h / 1d の完全・先頭不足・末尾部分 bucket、固定 activity reason の試験が通る。レビュー閉鎖 2026-10-30 (codex r1 + /code-review high 12 件、閉鎖確認 2 回) | 700–950 行 | codex（純 Python） |
| T3 backtest・履歴・改善 | 同一 dataset preflight、live/backtest parity、family v2 / legacy、inventory sink、`run_backtest` 配線 | AC-7–9, 12, 15 | T1, T2 | 推奨 fail-closed で history 完全不在なら `backtest_runs` の run 行、signal、order、候補 archive 等の業務副作用は 0 件。ただし RPC ledger・mission transcript・activity の監査記録は残す。NaN/seed は skip、live/backtest の入力列一致、v2 round-trip / legacy 読取の試験が通る。gate は参照履歴不在を構造化 verdict (`evaluable=False`、`reference_history_missing`) で返し、improve の commit 相はそれを `backtest_data_unavailable` の固定語で fail-closed に畳む。replay の adapter は (symbol, ref_tf) ごとに参照 frame を 30 日チャンクで cache し live の整列関数を共有する。レビュー閉鎖 2026-10-30 (codex r1 Critical 1 + /code-review high 7 件、閉鎖確認) | 550–750 行 | codex（純 Python） |
| T4 wizard・read tool・check・文書 | stage 3 `/symbols`、read schema 分離、check warning、history/corr 操作文、ops / golden 更新 | AC-9–10, 13 | T1–T3 | raw adapter 経路と runtime membership を組にした read-tool 試験、wizard zero-write と golden / 操作文更新が通る | 400–600 行 | codex（純 Python） |

T1→T2→T3→T4 は直列である。T1 の protocol / 安全境界試験を通過してから T2、T2 の native frame 試験を通過してから T3 に進む。registry は resolver の effective 座標と worker の履歴契約を必要とするため、C0 の「T1 と T2 は独立」は採らない。

## 4. 指揮者裁定（利用者判断が必要な製品・運用方針）

**2026-10-28 ユーザー裁定済**: R1・R2 (run 全体を `reference_history_missing` で失敗にする)・R4 は表の推奨どおり、R5 は MT5 限定。

| # | C1 裁定、sol の推奨 | なぜ利用者の判断が要るか |
|---|---|---|
| R1 | 参照取得は **soft**。ただし sol 推奨どおり、影響 strategy は NaN のまま実行せず `reference_unavailable` hold/skip にする。システム全体は止めない。 | 参照材料を欠く時の取引機会と、当該 strategy を止める安全性の優先度は製品方針だから。 |
| R2 | 履歴が完全に無いとき、run 全体を `reference_history_missing` で失敗にするか、その strategy を skip するかだけを利用者裁定に残す。sol 推奨は前者。seed 不足と indicator NaN、合法休場・age stale は常に strategy skip とする。 | 履歴欠損を許容して比較可能性を下げるかは、利用者が負う運用品質の選択だから。 |
| R4 | wizard は sol 推奨どおり段 3 の任意選択、初回既定空・再実行は現値を既定にする。 | 初回設定の利便性と「既定は安全側」の製品像をどちらも利用者が決めるため。 |
| R5 | 2026-10-28 のユーザー方針により、取得先は MT5 で得られるものに限定し、金利・DXY 等の追加 source はチケット `reference-series-additional-sources` に送る。本束では金利・DXY を除外する。 | 別 source の可用性・ライセンス・運用を本束の CFD 契約へ含めるかは利用者のロードマップ判断であり、今回は裁定済みだから。 |

R3（passthrough `ohlcv`）は利用者裁定ではなく、既存 `evaluate` 契約を壊さない技術判断として §2.3 に確定した。pin を入力座標まで緩和しないことも §2.3 の安全境界で確定し、裁定には残さない。

## 5. 受入条件（v0.6.2 C1 確定版）

- AC-1: schema v4 の overlap / duplicate watch 設定は v5 migration error、空でない watch + non-MT5 primary も reject。各 watch symbol は UTF-8 32 bytes 以下、33 bytes は `watch_symbol_too_long` で reject。unknown raw symbol は bridge 接続時 `watch_symbol_unknown` warning、件数上限超過は reject。
- AC-2: `JP225` が `VENDOR_SYMBOLS` に無くても raw adapter で `/ohlcv/JP225` を取得し `ohlcv_cache(source="mt5-live")` に保存する。hard key は soft timeout / retry 中でも先に attempted され、soft deferred / backoff が hard deferred と区別される。
- AC-3: UTC fixture で `s=2026-10-26T09:00Z` の 1h strategy は `d=10:00Z`、`r=09:00Z,e=10:00Z` の JP225 bar を選ぶ。`age=3h,max_age=3h` は fresh、`age=3h+1s` は stale。4h / 1d は 1h rows からのみ導出し、実測 `K_ref_actual = ceil((d_last-d_first)/ref_tf)+1` effective bars にはそれぞれ完全な `4K_ref_actual` / `24K_ref_actual` native rows を要求する。resolver の名目値 `K_ref_nominal = ceil(((strategy.max_bars-1)×strategy_tf)/ref_tf)+1` では、`strategy_tf > ref_tf` の 4h strategy・100 row・1h reference は 397 を通し、1d strategy・1000 row・1h reference は 23,977 を `reference_window_exceeds_max_bars` で reject する。`strategy_tf < ref_tf` の 1h strategy・1000 row・4h reference は 251 を通す。実測値が上限を超える場合 (週末・欠損で実時間範囲が延びた例の fixture) は当該 strategy を `reference_window_exceeded_at_runtime` で fail-closed skip する。
- AC-4: loader は新 field の構文だけを検査し、resolver は self/literal membership・effective 座標・canonical max-age と `K_ref_nominal` を解決する。既存の self・同一 timeframe dependency は main `df` を使い、参照用 `K_ref` の reject 対象外である。週末を跨ぐ既存 400 行 strategy（`rsi_bb_reversion_200` と同形）の self・同一 timeframe dependency が実測時間差によって reject / skip されず従来どおり実行される非退行試験を pin する。既存 config の hash / 配備済 strategy / example は不変。approval / handshake metadata は解決座標を持つが pin hash は plugin content hash のまま。
- AC-5: startup handshake に DataFrame は無く、call payload は main df と deduplicate named frames を持つ。同じ `(JP225,1h)` の複数 alias は 1 frame だけ送る。UTF-8 JSON と改行を含む `MAX_CALL_INPUT_BYTES=3_145_728` の limit / limit+1、共有 frame の 1 回分だけの byte 計上を試験し、超過は pipe write 前に `call_input_too_large`、session は再利用可能とする。
- AC-6: `ohlcv` は outputs の順・値・index・prefix 因果性・non-mutation・NaN 非生成を満たし、approval 済みで初めて resolver に見つかる。warmup / seed 不足は任意 indicator の NaN として扱い、保証外の値を捏造しない。
- AC-7: `2026-10-27T12:00Z` の 4h row で JP225 最終 end が `08:00Z`、`max_age=3h` なら strategy は `reference_unavailable` skip、signal / order は 0 件。他 strategy は実行を続ける。金曜末尾・月曜再開の UTC fixture も同一 `align_reference` を live / backtest が使う。
- AC-8: backtest は main と reference が同じ `HistoryDataset(mt5, 1m)` にあれば実行する。参照 symbol / source / base interval の履歴が完全に無い場合だけ、R2 の選択に従い、推奨 fail-closed は `reference_history_missing` の固定応答 `started:false` とする。この場合は `backtest_runs` の run 行、signal、order、候補 archive 等の業務副作用が 0 件で、RPC ledger と activity の監査記録が各 1 件残ることを試験する（mission transcript も通常どおり記録する）。または当該 strategy skip とする。seed 不足、indicator NaN、休場由来 stale は run 全体を missing にしない。
- AC-9: `analyze corr` は両 symbol を `source=mt5, interval=1m` で import し 30 共通観測以上ある場合に通る。`history import --symbol JP225` と単一-symbol `history coverage` は既存 CLI 契約を保つ。
- AC-10: `get_ohlcv/get_indicators` は watch symbol を read-only で受け、`get_signals` と trade-intent schema は拒否する。runtime membership も再検査する。
- AC-11: open intent `JP225` は通常 `_open`、`gather_open_snapshot`、`open_from_snapshot` の各経路で `not_tradable_symbol`。tool/schema 拒否、executor 拒否、quote/spec/broker 呼出し 0 回を別テストで pin する。
- AC-12: `references:` の状態語彙は `REFERENCE_STATES` の `ok | stale | missing | fetch_failed | budget_deferred | source_unavailable | plan_failed | symbol_rejected` とし、daemon は全行、CLI は `… 他 N 件` を示す。`reference_stale` の reason は `FETCH_STATE_REASONS + missing | age_exceeded | indicator_unavailable`、状態 key は `(strategy,pair,symbol,timeframe)`、`reference_recovered` は `recovered_from` を持ち、いずれも遷移時に一度だけ出る。improve inventory は `watch_symbols` と bool だけで履歴範囲を漏らさず、family v2 は JP225 / US500、alias 順、reference timeframe を区別し legacy key を読める。
- AC-13: wizard は stage 3 の MT5 connected 成功時だけ選択し、初回空・再実行現値・stage 9 summary・stage 10 まで zero-write を保つ。既存 `afx --help` 等 golden と取引 pair の outage/backfill 規則は不変。
- AC-14: 4h / 1d native-frame fixture は完全 bucket で `4K_ref_actual` / `24K_ref_actual` rows を渡し、先頭不足では対応する最初の effective row を unavailable、末尾の部分 bucket は除外する。soft backoff は増加列と cap を満たす。
- AC-15: family v2 は `@`、`;`、`%`、Unicode、broker suffix を含む symbol を UTF-8 percent-escape で round-trip し、不正 `%`、小文字 hex、safe 文字の過剰 escape、decode 後の再 encode 不一致を reject する。8 dependency と percent-escape を含む正規 key を round-trip し、encoded family key が 512 bytes ちょうどは通し、513 bytes は `family_key_too_long` で reject する。watch symbol の 32-byte ちょうど / 33-byte も AC-1 の設定検証で pin する。

## 6. 残余と後続

- 金利・DXY（yfinance / FRED または別 source routing）。
- CFD symbol ごとの session / holiday calendar と、合法休場と内部 gap の厳密判定。
- 参照系列の LLM prompt への自動注入。
- 参照 1m の取得、複数取引 pair × 複数参照で call input 上限を超える場合の製品上限見直し。
- `GET /symbols` の候補分類（指数 / 商品）の broker metadata は未確認。C1 は実測リストを UI 候補に使うが、symbol 種別の自動推論はしない。
- 再起動直後は `_soft_last_request_at` が失われ、再起動前後の 2 要求が足幅未満になり得る。取引 pair の hard key も再起動で全部再要求する既存挙動と同じ性質で、crash loop は systemd の `RestartSec` が律するため永続化しない（2026-10-30 裁定）。

## 7. 変更履歴

| 日付 | 版 | 変更 | 理由 | commit |
|---|---|---|---|---|
| 2026-10-25 | v0.1 C0 | 新規作成 | チケットの設計待ちを C0 に | |
| 2026-10-28 | v0.2 C1 | C-1 を §1.1/§2.1–2.2（MT5 raw adapter、MT5-only source）、C-2 を §2.2（symbol-keyed registry、hard-first、soft backoff）、C-3 を §2.4/AC-3（`d=s+tf`、1h 導出）、C-4 を §2.3/AC-5–6（metadata/call 分離、named frames、入力上限、warmup 保証の限定）、C-5 を §2.1/AC-11（executor membership guard）へ反映。I-1 を §2.4/§4 R1/AC-7、I-2 を §2.4/§4 R2/AC-7–8、I-3 を §2.3/AC-4、I-4 を §2.3/AC-6、I-5 を §1.1/§2.5/AC-8–9、I-6 を §2.5/AC-12、I-7 を §2.5/AC-12、I-8 を §2.1/AC-1、I-9 を §2.1/AC-13、I-10 を §2.5/AC-10、I-11 を §2.2/AC-12、M-1 を §2.3/AC-3、M-2 を §1.1/§2.2、M-3 を AC-11、M-4 を §3 へ反映。R3 は技術判断として §2.3 に移し、R1/R2/R4/R5 は §4 に残置 | sol 1 周目の Critical 5 / Important 11 / Minor 4 を全件採用し、実コードの source、保存先、grammar、IPC、発注境界に合わせて C1 確定版にするため | |
| 2026-10-28 | v0.3 C1 | I-C1-1〜5（seed 方針、3 MiB call 上限、K/native 本数、canonical escape、task exit criteria）と M-C1-1（activity reason）を反映 | 設計レビュー 2 周目 (sol) | |
| 2026-10-28 | v0.4 C1 | I-1〜I-4（3 MiB の実測境界、時間範囲ベースの `K_ref`、512-byte family key / symbol 長上限、fail-closed の run 行契約）を反映 | 設計レビュー 3 周目 (sol) | |
| 2026-10-28 | v0.5 C1 | I-5・I-6・R5 | 設計レビュー 4 周目 (sol) | |
| 2026-10-28 | v0.5.1 C1 | runtime 超過の固定 reason `reference_window_exceeded_at_runtime` (§2.3、AC-3)、AC-14 を `K_ref_actual` に統一 | 設計レビュー 5 周目 (sol、I-5 部分・I-7) | |
| 2026-10-28 | v0.6 C1 | §4 の R1・R2・R4 を推奨どおりユーザー裁定済に | ユーザー承認、実装着手 | |
| 2026-10-29 | v0.6.1 C1 | `risk.pair_rules` の余剰 key は起動拒否でなく警告 (§2.1、T1 の exit criteria) | T1a で設定例 (EURUSD の規則) と衝突。membership 検査が境界で余剰規則は取引対象を増やさない | |
| 2026-10-30 | v0.6.2 C1 | T2 で確定した soft 再要求上限、状態・activity・frame 読込・週末・`max_bars`・再起動境界を反映 | T2 実装・レビューで確定 | |
| 2026-10-30 | v0.6.3 C1 | T3 で確定: `history_available` は symbol ごと、`reference_history_missing` の reason は `no_rows`、gate の構造化 verdict と commit 相の固定語、replay の frame cache | T3 実装・レビューで確定 | |
| 2026-10-31 | v0.6.4 C1 | 参照 symbol の live frame には FX 前提の鮮度・開場 gap 検査を掛けない (§ calendar の節)。daemon 起動検査は bridge に問い合わせず `afx check` だけが突合。wizard: 非 MT5 選択時は現在の watch を表示して外す、一覧取得を飛ばす経路でも overlap を検査、区切りはカンマのみ | T4 レビュー 2 周目 (/code-review) #1・#2・#4・#6・#9 の是正に合わせる | |
| 2026-10-31 | v0.6.5 C1 | literal の membership を `pairs + watch_symbols` に拡張し、取引 pair の参照 key が hard のままであることを明記 | `reference-literal-symbol-allow-trading-pairs` | |
| 2026-10-31 | v0.6.6 C1 | inventory view に `reference_symbols` (pairs + watch_symbols) を足し `history_available` をその全体に広げた。取引 pair の参照 key は registry でも hard (reason `reference_pair`)、status の参照行は読める symbol の参照 key を対象にした。`symbol_not_watched` は閉じた語彙のまま (pairs または watch_symbols に無い意味) | `reference-literal-symbol-allow-trading-pairs` /code-review 2 周目 | |
