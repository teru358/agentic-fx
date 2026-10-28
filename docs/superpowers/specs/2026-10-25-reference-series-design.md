# 参照系列 (株価指数など) を戦略の材料にする — 設計 C1

**版:** v0.2 C1（C0 のレビュー全件を反映）
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

既存 schema v4 では `pairs` / `watch_symbols` の overlap と watch 内 duplicate が通る。C1 は **settings schema v5** に上げ、両方を migration error（自動削除なし）として起動を止める。利用者は片方を明示して消してから `afx init` を再実行する。`max_watch_symbols` 超過も起動拒否のままである。`afx check` は bridge 接続可能時に `/symbols` と突合し、未知値を `watch_symbol_unknown` warning として表示する。接続不能時は既存 bridge check の safe error を使い、unknown と偽装しない。

初回 wizard は固定 stage を増やさない。段 3 の「MT5 接続済み」分岐でだけ `/symbols` 成功後に候補を任意選択させ、初回既定は空、再実行既定は現在の `watch_symbols` とする。bridge 未接続、認証、timeout は既存 probe と同じ安全なエラーへ送り、値を黙って空にしない。選択結果は段 9 の要約に出し、段 10 の commit まで settings を書かない。yfinance / Twelve Data 分岐には出さない。

参照 symbol は `settings.pairs` に決して昇格しない。T1 で `action=open and intent.pair not in settings.pairs` を executor の通常 open と five-phase の両入口で `not_tradable_symbol` reject にする。検査は `quote_fn`、`spec_fn`、broker の前であり、`risk.pair_rules` の余剰 key も起動時に拒否する。trade-intent schema の enum は `settings.pairs` のまま保つ。

### 2.2 取得、保存、状態表示

resolver が解決した `(effective_symbol, native_interval)` を `RequirementRegistry` の key とする。現行の `Requirement(pairs, interval, ...)` と `settings.pairs × required_intervals` の直積を置換し、同じ key に hard / soft が重なれば `hard = any`、要求本数は最大値に畳み込む。取引 pair の paper / MTM / HWM / health は hard、参照用 indicator の要求は soft である。

各 ingest tick は hard queue を全て先に試し、soft は残予算だけを使う。soft を deferred にしても次 tick の hard より先には置かない。soft の empty / error には interval 上限付き capped exponential backoff を適用し、FX 用 `market_hours.next_bar_confirmation()` を指数 CFD の次回時刻に流用しない。`budget_exhausted`、`IngestTickReport`、activity は hard deferred と soft deferred、attempted と backoff 待ちを区別する。soft の失敗・empty は `DataUnhealthy` と outage state を変えない。

live の参照 bar は `ohlcv_cache` に `symbol`、native `interval`、`source="mt5-live"` として保存する。取得は resolver が要求した参照足だけで、参照 1m は作らない。ただし 4h / 1d の宣言は native 1h を要求して UTC epoch 格子で resample する。`ohlcv_history` は import 専用で、live cache と混同しない。

status は `(symbol, timeframe, source)` ごとに `ok | stale(<age>) | missing | fetch_failed | budget_deferred | source_unavailable` を表示する。symbol 集約表示を残す場合は worst state と内訳を併記する。activity の `reference_stale` / `reference_recovered` は状態遷移時だけ記録し、reason はこの固定語彙だけを使う。`afx status` は offline の保存済み履歴範囲だけを示し、live の freshness を推測しない。

### 2.3 indicator 宣言、解決、worker

宣言は既存 `indicators` に optional `symbol`、`timeframe`、`max_age` を加える。

```yaml
indicators:
  rsi:    {plugin: rsi_wilder, params: {period: 14}}
  jp_ema: {plugin: ema, symbol: JP225, timeframe: 1h, params: {period: 50}}
  jp:     {plugin: ohlcv, symbol: JP225, timeframe: 1h, params: {}}
  rsi_4h: {plugin: rsi_wilder, timeframe: 4h, params: {period: 14}}
```

loader は syntax だけを扱う。`symbol` は `self` または非空文字列、`timeframe` は既存 timeframe enum、`max_age` は後述の duration grammar とし、新 field を含む allow-list と固定 reject 語彙を持つ。settings membership、`self` の実 pair、effective timeframe、default max_age は resolver / composition root が解決する。`symbol` 省略は固定 pair 名ではなく `self` である。`ResolvedIndicator` と approval payload、handshake metadata、監査表示に `symbol_mode`、literal symbol、effective timeframe、canonical max-age seconds を持たせる。indicator plugin の content hash / pin は従来どおり plugin 内容だけであり、入力座標は strategy config content hash で署名されるため pin hash には混ぜない。

`max_age` は `^[1-9][0-9]*(s|m|h|d)$`（UTC 実経過時間）とし、bool、zero、負値、小数、別単位を reject する。内部 canonical は整数秒、approval / config hash / audit は canonical seconds を使う。default は effective reference timeframe の 3 倍、default 適用後も 7d 以下を必須とする。fresh の境界は `age <= max_age`、stale は `age > max_age` である。

`evaluate(df, indicators, ...)` は変えない。startup handshake は metadata のみで `MAX_HANDSHAKE_BYTES` に従う。各 call は main `df` と、deduplicate した `(effective symbol, effective timeframe)` ごとの named frame を別 payload field で送り、alias は frame id を参照する。同一座標を複数 alias へ送る場合も frame は 1 回だけ送る。送信前の JSON bytes は新しい `MAX_CALL_INPUT_BYTES` で fail closed にする。untrusted indicator の import / compute / validator は worker 内に留める。

依存 plugin の現行 `max_bars` は「worker に渡せる最大本数」であり warmup ではない。この束は任意 indicator の warmup を推論もしないし、`max_bars` を warmup 下限として reject もしない。各 frame はその座標で利用可能な末尾 `min(required window, indicator.max_bars)` を渡す。従って保証は「主足の各 decision row に asof 整列可能な末尾 K 点」であり、seed 不足による indicator NaN は有効な unavailable 入力として strategy を skip する。`ohlcv` は seed 不要の passthrough である。作者は従来どおり `max_bars` が自身の lookback を覆うよう宣言し、例と prompt にこの制約を明記する。

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

参照市場の個別 calendar は本束で持たない。`max_age` は UTC 実経過時間であり、休場中も進む。したがって合法的休場と内部 gap を厳密には区別しない。backtest の fail closed は「各暦 bar が無い」ことではなく、参照 symbol が全く無い、指定 `source/base_interval` が無い、または最初に評価する row を計算する seed が無い、という preflight 可能な条件に限定する。休場による stale は live / backtest とも同じ unavailable/skip である。

soft はシステム全体を degraded にしない、という意味だけである。required reference frame が missing、stale、fetch failure、budget deferred、source unavailable、または指標値が unavailable の strategy は `reference_unavailable` を記録して evaluate / signal / open を行わない。他 strategy、LLM loop、既存の価格源 outage 規則、kill switch、drawdown 保護は継続する。これにより live の NaN 実行と backtest abort の非対称を無くす。

実時刻 fixture（すべて UTC）:

| ケース | 取引 pair 主足 row | 参照足 | chosen / age | 結果 |
|---|---|---|---|---|
| 通常 | USDJPY 1h, `s=2026-10-26T09:00Z`, `d=10:00Z` | JP225 1h `r=09:00Z`, `e=10:00Z`, `max_age=3h` | `e=d`, age 0 | 09:00–10:00 の同時確定 bar を使う。1 本遅れない。 |
| 週末休場 | USDJPY 1h, `s=2026-10-26T22:00Z`, `d=23:00Z` | fixture 上の JP225 最終 `r=2026-10-23T14:00Z`, `e=15:00Z`, `max_age=84h` | age 80h | calendar がなくても UTC age 内なので fresh として同じ終値を使う。`d=2026-10-27T04:00Z` では age 85h で stale/skip。 |
| 内部欠損 / stale | USDJPY 4h, `s=2026-10-27T08:00Z`, `d=12:00Z` | JP225 1h の最終 `r=07:00Z`, `e=08:00Z`, `max_age=3h` | age 4h | 08:00–12:00 に何が起きたかは calendar で判定せず stale/skip。`d=11:00Z`（age 3h）は fresh。 |
| 4h 導出 | USDJPY 4h, `s=2026-10-27T08:00Z`, `d=12:00Z` | JP225 の 1h `08:00,09:00,10:00,11:00` を UTC 4h bucket へ集約、`e=12:00Z` | age 0 | MT5 native H4 を読まず 1h から導出した 08:00–12:00 bar を使う。 |

### 2.5 履歴、分析、改善 mission、read tool

backtest は取引 pair と全参照 symbol を一つの `HistoryDataset(source, base_interval)` から読む。`run_backtest` 前に全宣言を preflight し、同じ import source/base interval に reference history と最初の必要 seed が無ければ `reference_history_missing(<symbol>, <tf>, <reason>)` で副作用前に fail closed とする。合法的休場 / age stale はこの missing 判定にしない。`analyze corr` は既存実装のまま、両 symbol が同じ `--source`、1m、30 以上の共通 timestamp を持つ場合だけ動く。live で JP225 1h を取っただけでは相関分析に使えない。`history coverage` は既存どおり単一 `--symbol` の表示であり、参照一覧の表を新設しない。

`get_ohlcv` と `get_indicators` だけは market-read 専用の `read_symbol_param()` を新設して `pairs + watch_symbols` を enum / runtime membership ともに検査する。共通 `market_tools.pair_param()` は拡張しないため、`get_signals` と trade-intent schema は pairs 限定のままである。read tool も MT5 primary / raw symbol 契約に従う。

改善 mission の `list_deployed_plugins()` には bounded な top-level `watch_symbols` と `history_available: true|false` だけを載せる。履歴の開始・終了時刻や成績・holdout 境界を inventory sink に出さない。prompt 規律には「参照系列は `symbol:` 宣言で取り、自前で別 symbol を読まない」を加える。

family identity は新 grammar `strategy-v2:<strategy_tf>@<trading-pairs>@<encoded-resolved-deps>` にする。各 dep は alias の宣言順で `alias;plugin;symbol_mode;literal_symbol_or_-;ref_tf` を percent-escape した token とし、alias、plugin、self/literal symbol、reference timeframe、順序を全て区別する。新 parser はこの正規形だけを受け、既存 `strategy:<names>@<tf>@<pairs>` は legacy として parse / 表示のみ維持する。旧 key は新 key と同一族に畳み込まない。これは scalar を落とす config shape hash だけでは JP225 と US500 を区別できないためである。

## 3. 実装 task（直列）

| task | 内容と成果物 | 依存 | 見積り | 担当 |
|---|---|---|---:|---|
| T1 契約・安全境界 | schema v5 migration、MT5-only/raw symbol、`IndicatorRef` / resolver / audit、duration、named-frame payload と call 入力上限、executor membership guard、時刻純関数と UTC fixture | なし | 650–850 行 | sonnet（通常 / five-phase の競合境界試験を含む） |
| T2 live 取得・worker 配線 | symbol-keyed registry、hard-first/soft backoff、raw MT5 adapter、1h→4h/1d、named frames、`ohlcv` 配備、strategy skip、status/activity | T1 | 700–950 行 | codex（純 Python） |
| T3 backtest・履歴・改善 | 同一 dataset preflight、live/backtest parity、family v2 / legacy、inventory sink、`run_backtest` 配線 | T1, T2 | 550–750 行 | codex（純 Python） |
| T4 wizard・read tool・check・文書 | stage 3 `/symbols`、read schema 分離、check warning、history/corr 操作文、ops / golden 更新 | T1–T3 | 400–600 行 | codex（純 Python） |

T1→T2→T3→T4 は直列である。registry は resolver の effective 座標と worker の履歴契約を必要とするため、C0 の「T1 と T2 は独立」は採らない。

## 4. 指揮者裁定（利用者判断が必要な製品・運用方針）

| # | C1 裁定、sol の推奨 | なぜ利用者の判断が要るか |
|---|---|---|
| R1 | 参照取得は **soft**。ただし sol 推奨どおり、影響 strategy は NaN のまま実行せず `reference_unavailable` hold/skip にする。システム全体は止めない。 | 参照材料を欠く時の取引機会と、当該 strategy を止める安全性の優先度は製品方針だから。 |
| R2 | backtest は sol 推奨どおり、未 import / source-base mismatch / seed 不足を preflight fail closed、合法休場・age stale は strategy skip とする。 | 比較可能な検証を強く要求するか、履歴欠損を許容するかは利用者が負う運用品質の選択だから。 |
| R4 | wizard は sol 推奨どおり段 3 の任意選択、初回既定空・再実行は現値を既定にする。 | 初回設定の利便性と「既定は安全側」の製品像をどちらも利用者が決めるため。 |
| R5 | 金利・DXY は sol 推奨どおり本束から除外する。 | 別 source の可用性・ライセンス・運用を本束の CFD 契約へ含めるかは利用者のロードマップ判断だから。 |

R3（passthrough `ohlcv`）は利用者裁定ではなく、既存 `evaluate` 契約を壊さない技術判断として §2.3 に確定した。pin を入力座標まで緩和しないことも §2.3 の安全境界で確定し、裁定には残さない。

## 5. 受入条件（C1 確定版）

- AC-1: schema v4 の overlap / duplicate watch 設定は v5 migration error、空でない watch + non-MT5 primary も reject。unknown raw symbol は bridge 接続時 `watch_symbol_unknown` warning、上限超過は reject。
- AC-2: `JP225` が `VENDOR_SYMBOLS` に無くても raw adapter で `/ohlcv/JP225` を取得し `ohlcv_cache(source="mt5-live")` に保存する。hard key は soft timeout / retry 中でも先に attempted され、soft deferred / backoff が hard deferred と区別される。
- AC-3: UTC fixture で `s=2026-10-26T09:00Z` の 1h strategy は `d=10:00Z`、`r=09:00Z,e=10:00Z` の JP225 bar を選ぶ。`age=3h,max_age=3h` は fresh、`age=3h+1s` は stale。4h / 1d は 1h rows からのみ導出する。
- AC-4: loader は新 field の構文だけを検査し、resolver は self/literal membership・effective 座標・canonical max-age を解決する。既存 config の hash / 配備済 strategy / example は不変。approval / handshake metadata は解決座標を持つが pin hash は plugin content hash のまま。
- AC-5: startup handshake に DataFrame は無く、call payload は main df と deduplicate named frames を持つ。同じ `(JP225,1h)` の複数 alias は 1 frame だけ送る。`MAX_CALL_INPUT_BYTES + 1` は pipe write 前に fail closed。
- AC-6: `ohlcv` は outputs の順・値・index・prefix 因果性・non-mutation・NaN 非生成を満たし、approval 済みで初めて resolver に見つかる。warmup / seed 不足は任意 indicator の NaN として扱い、保証外の値を捏造しない。
- AC-7: `2026-10-27T12:00Z` の 4h row で JP225 最終 end が `08:00Z`、`max_age=3h` なら strategy は `reference_unavailable` skip、signal / order は 0 件。他 strategy は実行を続ける。金曜末尾・月曜再開の UTC fixture も同一 `align_reference` を live / backtest が使う。
- AC-8: backtest は main と reference が同じ `HistoryDataset(mt5, 1m)` にあり seed もあれば実行し、参照が全く無い / source-base 不一致 / seed 不足なら `reference_history_missing` で run 行なし。休場由来 stale は run 全体を missing にしない。
- AC-9: `analyze corr` は両 symbol を `source=mt5, interval=1m` で import し 30 共通観測以上ある場合に通る。`history import --symbol JP225` と単一-symbol `history coverage` は既存 CLI 契約を保つ。
- AC-10: `get_ohlcv/get_indicators` は watch symbol を read-only で受け、`get_signals` と trade-intent schema は拒否する。runtime membership も再検査する。
- AC-11: open intent `JP225` は通常 `_open`、`gather_open_snapshot`、`open_from_snapshot` の各経路で `not_tradable_symbol`。tool/schema 拒否、executor 拒否、quote/spec/broker 呼出し 0 回を別テストで pin する。
- AC-12: `references:` は `(symbol,timeframe,source)` と固定 reason を示し、stale/recovered activity は遷移時に一度だけ出る。improve inventory は `watch_symbols` と bool だけで履歴範囲を漏らさず、family v2 は JP225 / US500、alias 順、reference timeframe を区別し legacy key を読める。
- AC-13: wizard は stage 3 の MT5 connected 成功時だけ選択し、初回空・再実行現値・stage 9 summary・stage 10 まで zero-write を保つ。既存 `afx --help` 等 golden と取引 pair の outage/backfill 規則は不変。

## 6. 残余と後続

- 金利・DXY（yfinance / FRED または別 source routing）。
- CFD symbol ごとの session / holiday calendar と、合法休場と内部 gap の厳密判定。
- 参照系列の LLM prompt への自動注入。
- 参照 1m の取得、複数取引 pair × 複数参照で call input 上限を超える場合の製品上限見直し。
- `GET /symbols` の候補分類（指数 / 商品）の broker metadata は未確認。C1 は実測リストを UI 候補に使うが、symbol 種別の自動推論はしない。

## 7. 変更履歴

| 日付 | 版 | 変更 | 理由 | commit |
|---|---|---|---|---|
| 2026-10-25 | v0.1 C0 | 新規作成 | チケットの設計待ちを C0 に | |
| 2026-10-28 | v0.2 C1 | C-1 を §1.1/§2.1–2.2（MT5 raw adapter、MT5-only source）、C-2 を §2.2（symbol-keyed registry、hard-first、soft backoff）、C-3 を §2.4/AC-3（`d=s+tf`、1h 導出）、C-4 を §2.3/AC-5–6（metadata/call 分離、named frames、入力上限、warmup 保証の限定）、C-5 を §2.1/AC-11（executor membership guard）へ反映。I-1 を §2.4/§4 R1/AC-7、I-2 を §2.4/§4 R2/AC-7–8、I-3 を §2.3/AC-4、I-4 を §2.3/AC-6、I-5 を §1.1/§2.5/AC-8–9、I-6 を §2.5/AC-12、I-7 を §2.5/AC-12、I-8 を §2.1/AC-1、I-9 を §2.1/AC-13、I-10 を §2.5/AC-10、I-11 を §2.2/AC-12、M-1 を §2.3/AC-3、M-2 を §1.1/§2.2、M-3 を AC-11、M-4 を §3 へ反映。R3 は技術判断として §2.3 に移し、R1/R2/R4/R5 は §4 に残置 | sol 1 周目の Critical 5 / Important 11 / Minor 4 を全件採用し、実コードの source、保存先、grammar、IPC、発注境界に合わせて C1 確定版にするため | |
