# [closed-bars-and-required-window] 設計書 v1.1 (A2-1)

対象 commit: `4321db3`。作成日・改訂日: 2026-09-21。A2 [live-price-source-mt5] を 4 束に分けた 1 本目 (後続: A2-2 tick snapshot + 要求予算 / A2-3 不通停止 + backfill / A2-4 bid/ask 約定)。下書きと設計レビュー 7 周の記録は `tmp/design-a2/` (リポジトリ外)。対象はライブ確定足、足別保存、必要本数の取得窓、primary 設定移行。根拠は現 worktree の静的読取りと §9 の実測である。

## 要点

1. A2-1a は単独で main に入る確定足契約である。書込み可能 provider の既存 `get_bars` を暫定 writer とし、保存直前の closed normalizer、closed-only cache fallback、timeout と例外隔離を備える。A2-1b で writer を scheduler tick 先頭 ingest に移す。
2. 確定は `bar_time + interval + grace[source, interval] <= cutoff_utc`。書く側は形成中の足を保存せず、読む側はこの時刻規則を満たす足だけを受ける。`ohlcv_cache` の列は変えず、新設は `data_migrations` 表 1 つである。保存は現行どおり UPSERT である。a 導入時の data migration は 1 回だけ、`ohlcv_cache` を全行削除する。
3. paper の約定判定、MTM/HWM、`latest_1m_bar`、LLM tools は forming を読まない。a の producer も時刻規則で末尾の形成中 1m を除き、producer の確定足契約は b で native closed 足（4h 以上は 1h aggregate）へ完成する。
4. b は registry/planner が必要本数を決め、primary だけから tick ingest する。通常 reader は DB のみ、worker readonly tool だけは要求時の closed-only range を source に問い合わせ、保存しない。

## 1. 前提の検証結果

|事実|根拠|設計への含意|
|---|---|---|
|現 writer は `get_bars` の native 経路で取得窓全体を `ohlcv_cache` へ UPSERT する。|`src/agentic_fx/datafeed/price_provider.py:143-194`; `src/agentic_fx/store/ohlcv.py:50-74`|a の writer はここを保存直前の closed normalizer に通し、現行 UPSERT を維持する。|
|派生足も `_derive` から base 足の cache UPSERT を通る。|`src/agentic_fx/datafeed/price_provider.py:329` 付近|closed normalizer は native と派生 base 保存の両方で成立する。|
|`latest_1m_bar` は `get_bars` の末尾を返し、scheduler は HWM/MTM、pending fill、open exit、processed marking で読む。|`src/agentic_fx/datafeed/price_provider.py:340-344`; `src/agentic_fx/core/scheduler.py:346-353,402-440,925-1029`|forming が資金保護に入る現在の穴を閉じる。実行呼出しは 938/989/1023 を含む。|
|trade healthcheck は readonly provider、init healthcheck は書込み可能 provider を用いる。|`src/agentic_fx/service.py:876-906,1100-1140`; `src/agentic_fx/loops/trade_loop.py:119-145`|前者は保存せず、後者だけが a の writer になり得る。|
|producer は `load_resampled_frame` で閉じた target bucket を読む。実 call は producer / strategy adapter / analysis のみ。|`src/agentic_fx/plugin/signal_producer.py:205-237`; `src/agentic_fx/plugin/strategy_adapter.py`; `src/agentic_fx/backtest/analysis.py:116`|b で live closed reader に切替える。|
|cache fallback は `load_cache_bars` を読む。|`src/agentic_fx/datafeed/price_provider.py:229-250`; `src/agentic_fx/store/ohlcv.py:77-99`|読み出し時の時刻規則を満たす cache を読む。|

bar 鮮度は 5 分、account snapshot 鮮度は 10 分で別の既存契約である。a 後は paper/MTM は 1m close まで遅延する。成行の約定価格は quote のまま、day 強制 close、予約失効、金曜 cutoff は bar 入力に依存しない。cursor はメモリのみである。

## 2. 範囲と段階

|段階|単独で成立する変更|source 選択・writer|
|---|---|---|
|A2-1a: 確定足契約|`[start,end)` normalizer、時刻規則 reader、paper/MTM/LLM の closed 化、cache fallback、1m timeout/隔離、導入時 1 回だけの data migration（`ohlcv_cache` を全行削除）。|旧 `_chain` のまま。`readonly=False` provider の `get_bars` が normalizer 後の writer、readonly provider は保存しない。|
|A2-1b: 本数と primary|RequirementRegistry/planner、tick ingest、native 足保存、producer 切替、primary 移行、source×interval 保持。|tick ingest が唯一 writer、`datafeed.primary` のみ。通常 reader は DB、worker readonly tool のみ primary に要求時 fetch。|

範囲外は A2-2 の要求 coalescing/予算、A2-3 の本格 backfill/replay、A2-4 の bid/ask 約定、decision timeframes/mission 周期、cache/history 統合、複数判断足である。

## 3. 設計

### 3.1 closed contract、保存

`normalize_closed_range(raw, start, end, cutoff, grace)` は live client と MT5 importer が共有する pure 関数である。UTC aware、`start <= ts < end`、かつ確定式を満たす行だけを重複なく返す。inclusive source の `ts == end` は除外する。forming は保存せず、取得失敗、cache fallback、時刻経過だけで保存済みの値を confirmed とみなさない。

時刻規則は二つの境界で掛ける。provider の Python 境界では、native 応答を `get_bars` の保存前（`src/agentic_fx/datafeed/price_provider.py:167-168`）、`_derive` の base 応答をその保存前（`:328-330`）、cache fallback（`:229-250`）、readonly の返却を共通 normalizer に通す。これは最終 return だけの filter では保存済み forming を防げないためである。producer の SQL 境界では、`signal_producer.py:208-210` から直接呼ぶ `load_resampled_frame` の SQL（`src/agentic_fx/backtest/timeframes.py:166-175`）へ評価時 `cutoff` を渡し、`until=bucket_end` とは別に live source の base 行へ `bar_time <= cutoff - base幅 - grace` を掛ける。

保存直前に normalizer を通すため、a 以降 `ohlcv_cache` に形成中の足は入らない。`ohlcv_cache` の列は変えず、取得窓内の既存行は現行どおり確定値で UPSERT する。a 導入時には既存の migration 機構へ `_migrate_ohlcv_cache_purge_pre_closed_once` を追加し、`init_db` の既存 migration 呼出し列（`src/agentic_fx/store/db.py:1297-1307`、既存の `ohlcv` split は `:576-667,1305-1306`）に載せる。現物の `db.py` には汎用の migration 版管理が無い (`PRAGMA user_version` も未使用、各 migration が自前の guard を持つ) ので、実行済みの印は新設の小さな表 `data_migrations(name TEXT PRIMARY KEY, applied_at TEXT NOT NULL)` に `ohlcv_cache_purge_pre_closed_v1` の 1 行として残す。`data_migrations` は正式な表追加であり、`TABLE_NAMES` と表一覧の完全一致テストの対象に加える。全行削除と印の INSERT は同一 transaction で行い、印があれば何もしない。この一回限り guard で、`ohlcv_cache` を全行削除し、削除件数を log に出す。再実行は no-op とし、`ohlcv_history` は一切触れない。

**残余リスク:** IV-2 のとおり、a 以降に保存される行は確定足だけであり、導入時の全削除で a より前の行を除去する。残る反例は (2) だけである。a を入れた後に旧版へ戻し、また a へ上げた場合、印が残っているため全削除は再実行されない。この旧版へ戻す運用はこのプロジェクトで支援せず、DB migration 全般は前進のみとする。runbook: 旧版へ戻してから再導入するときは `ohlcv_cache` を消してから起動する（cache のため再取得される）。

**導入時だけの既知の振る舞い:** 全削除の直後から最初の 1m 取得が成功するまで、`latest_1m_bar` は足なしを返す。建玉・予約がある状態で導入し、その直後の取得が失敗し続けると、現行なら鮮度 5 分以内の cache fallback が渡していた最後の足が無く、bar に依存する limit / SL / TP の判定と MTM はその間見送られる（day 強制 close は quote なので続く。`closed_bar_unavailable` が理由つきで出る）。これは初回起動と同じ振る舞いで、導入の 1 回だけ、最初の成功取得で解消するので受け入れる（ユーザー裁定: cache は削除してよい）。runbook: a を入れる再起動は、開場中で源に届く時か、建玉・予約が無い時に行う。確定足が取れない間の建玉保護の方針そのものは A2-3 の責務である。

### 3.2 読み手・書き手の切替表

|対象|現行|a 後|b 後|
|---|---|---|---|
|scheduler の 1m 取得と保存|`latest_1m_bar→get_bars` が chain fetch と通常 UPSERT。|書込み provider の `get_bars` が closed-only writer。1m timeout/例外隔離。|tick 先頭 ingest が primary を fetch/commit。scheduler は closed DB reader。|
|paper 約定判定|取得末尾を入力にし得る。|closed 1m のみ。forming 初観測は cursor 不変。|同左。|
|MTM/HWM|`bars_fn` が source fetch し得る。|closed cache/fetch の最新 1m。|closed DB の最新 1m。|
|producer の入力|既存 resample reader。|既存の 1m 再集計のまま、読み出し時の時刻規則で末尾の形成中 1m を除く（`src/agentic_fx/backtest/timeframes.py:166-201`）。確定足契約の完成は b。|target closed reader、4h+ は closed 1h aggregate。|
|LLM tools (worker readonly)|chain を直接 fetch、保存しない。|chain の closed-only range を最大100本返す（取得窓により不足し得る）。返却本数と不足フラグを構造化し、built-in indicator が必要本数（最大 SMA50=50）未満なら指標ごとに `insufficient_closed_bars`、保存しない。|primary の closed-only range を要求時 planner で100本保証、保存しない。|
|trade healthcheck|readonly `get_bars`、保存しない。|同じく closed-only、保存しない。|DB health（判断足 1 本+鮮度）、取得 owner にしない。|
|init healthcheck|書込み provider を構築。|normalizer 後の writer になり得る。|cold fill/ingest 完了後の DB health。|
|cache fallback|forming を含む cache を返し得る。|読み出し時の時刻規則を満たす行だけ。|closed DB reader。|
|源の選択 (旧 chain / primary)|MT5→TD→yfinance chain。|旧 chain のまま。|通常経路は primary のみ。enabled 非primary は通常 reader に選ばれない。|
|保持 GC|全 cache を時刻だけで prune。|現行保持を維持。|RetentionPlan の source×interval だけ prune。|

#### 読み手別に形成中の足を許さない理由

|読み手|受け取るもの|形成中を許さない理由|
|---|---|---|
|`get_ohlcv`、`get_indicators`|`ClosedBars` の末尾。a は最大100本で不足を構造化して返し、built-in indicator の最大 seed は SMA50=50。b は要求時 planner で100本を保証する。|LLM/indicator が同じ prompt 中に変わる close を根拠にしない。|
|paper fill (`latest_1m_bar`)|最後の確定1m。cursor は確定 `bar.ts` の後にのみ進む。|最初の観測で cursor を消費せず、SL/TP/limit は close 後の high/low を一度だけ判定する。|
|mark-to-market/HWM|最後の確定1m close。|未確定 close による equity/HWM の揺れを排除する。リアルタイム quote MTM は A2-4 まで追加しない。|
|signal/strategy producer|閉じた source の判断足。4h+ は存在する closed 1h 構成足の target bucket。|forming 値の固定化を塞ぎ、既存の「存在する足だけで aggregate」規則を維持する。|
|保存/cache fallback|時刻規則を満たす `ClosedBars` のみ。|forming 行を保存せず、読み出し時にも受け入れない。|

#### `closed_bar_unavailable` の状態機械

Scheduler はメモリ集合 `_closed_bar_unavailable_pairs` を持つ。対象 pair は OPEN または PENDING_FILL があるものだけである。market open で、確定 1m が bar 鮮度 5 分を超えて無いときだけ `fresh → unavailable` と遷移し、理由付き activity `closed_bar_unavailable` を pair ごとに 1 回だけ出す。確定 1m が fresh に戻る、または当該 pair の建玉・予約が無くなると集合から除去する。閉場中は判定しない。再起動後は集合が空なので、条件を満たせば 1 回発行する。a の配置は market-open 判定後・MTM 前、b の配置は lock 内 commit 後・既存 tick 前である。

### 3.3 ingest、due、lock

b の tick は同じ scheduler thread で `_scheduler_tick_once` を「取得の準備（lock 外、予算10秒）→ `core_lock` 内の commit → 既存 tick」に分ける。採用理由は、現行 `latest_1m_bar→get_bars` が scheduler の資金保護経路で同期通信し得る (`scheduler.py:346-353,416,938,989,1023`) 一方、通知も lock 外へ追い出している (`trade_loop.py:292-295`) ためである。lock 内取得案は timeout 中に SL/TP/MTM を止めるため不採用であり、保護処理は prepare の予算ぶん最大10秒遅れる。a は現行構造を崩さないが、1m に同じ明示 timeout と key/例外隔離を入れる。

ingest は `(pair,native_interval)` ごとに例外を隔離する。wall-clock 予算は設定値、既定 10 秒。優先順は 1m → 判断足 (`decision_timeframes`、束 B) → その他の足で、残時間で順に処理し未処理は次 tick に回す。判断足の key は、成功可能なら有限 tick 内に必ず probe される (1m が毎 tick 予算を使い切っても判断足が恒久に更新されない状態を作らない。fair queue の方式と上限は束 B の B1-0 spike で確定)。すべての source（yfinance を含む）に明示 timeout を渡す。予算切れ/失敗後も、paper limit/SL/TP、MTM/HWM の決定論ブロックは最後の確定行で必ず走る。

watermark は DB の key ごとの `MAX(bar_time)`、`next_probe_at` はメモリ状態とする。`market_hours` が閉場なら due を立てない。開場中の空応答は成功であり、次 probe は `min(interval, exponential_backoff)` 後とする。cold fill range は planner の必要本数窓全体、定常 range は watermark の最低 1 本ぶん手前から cutoff までである。窓内の穴は cold fill の INSERT で埋めるが、残る穴は本数再計算による不足診断だけに出し、本格 backfill は A2-3 である。

現行 scheduler 内取得回数は状態依存で概ね `2×OPEN + PENDING_FILL + pair数`、これに trade healthcheck と worker tools の要求、chain 内 fallback 試行を別に加える。b 後は起動時 `pair数×必要native足数×1`、定常は `Σ(pair,native足)[新確定足の回数]×1`、tick hard cap は `pair数×必要native足数` である。

### 3.4 registry、planner、不足分類

registry entry は `(consumer_id, pair selector, interval, required_closed_bars, hard, reason)`、同一 pair/interval は max とする。core は paper/MTM/HWM の 1m×1、health は判断足 1 本+鮮度を登録する。plugin pair は `meta.pairs ∩ settings.pairs`。loader 正規化後の `max_bars`（未宣言は `DEFAULT_MAX_BARS=200`）を strategy 足へ登録し、indicator は依存 strategy の足へ集計する。`get_ohlcv` と LLM `get_indicators` は registry に常駐せず要求時 planner である。

planner だけが期間を換算する。

```text
calendar_days = ceil(required_closed_bars × interval_duration / 24h × 7/5) + 3 + holiday_headroom
```

`holiday_headroom` は `market_hours` が知る休場日数である。取得後は closed 本数を再計算し、追加要求せず `insufficient_closed_bars(consumer,source,interval,required,available,capability)` を返す。例は 1h×1600≒96日（yfinance 1h 730日内）と、yfinance 15m の60日超（call なし）である。

不足は二分類する。(1) capability/保持期間で構造的に届かないものは起動拒否し、設定変更時だけ再評価する。(2) 祝日、空 DB、初回 offline、stale、取得失敗、穴による取得結果の不足はすべて一時不足で、producer 内の既存本数チェックで当該 plugin の評価を見送る。サービス全体の状態は定義しない。「core hard」は構造的に満たせなければ起動拒否する。

|consumer|必要本数|hard か soft か|登録先の足|根拠 file:line|
|---|---:|---|---|---|
|paper・MTM・HWM|1|core hard|1m×1|`src/agentic_fx/core/scheduler.py:346-353,416-421,938-943,989-991,1021-1028`|
|health|判断足1本 + 鮮度|core hard|health の判断足|`src/agentic_fx/datafeed/price_provider.py:354-363`; `src/agentic_fx/service.py:413-414,872-906`|
|strategy plugin|正規化後 `max_bars`（未宣言200）|plugin soft|strategy の足|`src/agentic_fx/plugin/signal_producer.py:204-237`|
|indicator plugin|依存する strategy の正規化後 `max_bars`|plugin soft|依存 strategy の足|`src/agentic_fx/plugin/signal_producer.py:204-237`|
|`get_ohlcv`|a は最大100（不足を構造化）。b は100本を満たす窓を計画し、返せた確定足が100本未満なら成功扱いにせず構造化した不足を返す。|要求時（registry 外）|要求された足|`src/agentic_fx/tools/market_tools.py:47-54`|
|`get_indicators` built-in|SMA20=20、SMA50=50、EMA12=12、EMA26=26、MACD/MACD signal=26、RSI14=15、ATR14=15、BB=20（最大50）。a は不足を指標ごとに構造化し、b も必要本数を満たせなければ成功扱いにせず構造化不足を返す。|要求時（registry 外）|要求された足|`src/agentic_fx/datafeed/indicators.py:41-49,51-83`; `src/agentic_fx/tools/market_tools.py:76-94`|
|LLM 経由の plugin indicator|plugin が要求する `max_bars`、不足は `insufficient_closed_bars`|要求時（registry 外）|plugin が要求した足|`src/agentic_fx/tools/market_tools.py:76-94`|

### 3.5 primary、設定 matrix、保持

`datafeed.primary` は b 後の ingest、通常 reader、producer の唯一の runtime source、payload の値の出所である。payload field 名は互換のため `live_source` を維持する。primary 未記載かを Pydantic の既定値では判別しない。`model_validator(mode="before")` が raw mapping の key 存在を検査してから既定を入れ、既定 `producer_source=yfinance` を legacy 指定と誤認しない。

エラー優先順位は `ambiguous_primary_source > unknown_primary_source > primary_source_disabled > primary_source_unusable`。enabled 0 は起動拒否、Twelve Data を primary にして credential がなければ unusable である。

|raw primary|enabled / credential|raw legacy `producer_source`|結果|
|---|---|---|---|
|未記載|enabled 0|未記載|起動拒否。|
|未記載|enabled 1|未記載|その source を primary に正規化。|
|未記載|enabled 2以上|未記載|`primary_source_required`（`datafeed.primary を明記してください（enabled source が複数です）`）。|
|明記 yfinance/mt5/td|unknown または disabled|任意|優先順位どおり unknown/disabled。|
|明記 twelvedata|enabled、鍵なし|任意|`primary_source_unusable`。|
|未記載|legacy source enabled|あり|legacy-only として primary 化、deprecation は一度。|
|new + legacy|任意|あり|同値も `ambiguous_primary_source`。|

legacy-only の `cache_retention_days` だけを source×required interval に展開する。新 retention との併存は拒否する。RetentionPlan は source×interval cutoff を持ち、`prune_cache` は plan key だけを有界削除し history を触らない。

#### 用途別 provenance

現物の `PriceProvider.bars_origin`（`src/agentic_fx/datafeed/price_provider.py:44,84,179,192,262-268`）は既存である。判断・strategy の `bars_origin` は source の判断足（native 1h 等、4h+ は closed 1h aggregate）とし、約定判定の `bars_origin` は確定1mとする。同じ pair でもこの二つは用途が異なり、強制一致させない。activity/payload へ `bars_origin`、interval、source、bar_time を出す契約は現物にないため新設する。

## 4. 不変条件

|ID|不変条件|
|---|---|
|IV-1|limit/SL/TP/paper fill/MTM は確定 1m のみを入力にする。|
|IV-2|a 以降の cache は確定足だけ。導入時の全削除で a より前の行を除去する。読む側は時刻規則を満たさない足を受け入れない。|
|IV-3|撤回 (r4 裁定)。確定行不変は A2-1 では定義しない。`[live-confirmed-bars-into-history]` で、値の訂正・版の扱いとともに設計する。|
|IV-4|b の定常取得と cache への保存の owner は tick ingest のみ。readonly worker tool の要求時取得は保存しない例外である。a の暫定 writer は書込み provider の `get_bars` のみ。|
|IV-5|4h+ は native source に依存せず epoch 格子、存在する closed 構成足だけで aggregate する。|
|IV-6|不足は `insufficient_closed_bars` とし、構造的不足は起動拒否、取得結果の一時不足は producer 内で当該 plugin の評価を見送る。|
|IV-7|primary の二重解釈を許さず、複数 enabled で未指定なら起動前に止める。|
|IV-8|撤回（r1 裁定）。取得回数を「増やさない」とする旧不変条件は tombstone であり、§3.3 の上限式を採る。|
|IV-9|実 DB/bridge/service を使う test は作らず、HTTP fake と tmp SQLite のみ。|

## 5. 受入条件

|ID|受入条件|逆変異|殺すテストの形|
|---|---|---|---|
|AC-1|forming は reader に出ない。|forming を通常行として返す。|08:59 forming、09:00 fetch failure の fake。|
|AC-2|forming 初観測は cursor 不変、閉じた同 bar の high/low は一回だけ判定。|cursor 先行消費/二重判定。|paper fake bar sequence。|
|AC-3|LLM と MTM/HWM は closed close のみ。|forming close を使用。|forming値だけ変える比較。|
|AC-4|`[start,end)`、inclusive end、grace 境界を normalizer/importer で共有。|end 包含・naive 時刻。|parameterized pure test。|
|AC-5|b cold fill の exact key set、due key 1回、失敗再試行1回。|tick内重複要求。|recording source fake。|
|AC-6|producer の時刻規則が末尾の forming 1m を直接除外し、存在する足だけ aggregate の golden を維持する。|producer が forming を読む/完全 bucket 化。|末尾 forming を含む欠損 fixture golden。|
|AC-7|撤回 (r4)。revision dedupe は A2-1 の対象外。|—|—|
|AC-8|planner capability 判定と要求時 tool 不足。|追加 fetch/誤ったcap。|1h×1600 と15m超過 fake。|
|AC-9|normalized max_bars=未宣言200、indicator は strategy 足。|未宣言 disable。|loader/registry fake。|
|AC-10|構造不足 disabled、一時不足 skip→自動復帰。|一時不足で plugin 恒久disable。|availability を回復する fake。|
|AC-11|primary×enabled×legacy raw×credential matrix、payload `live_source`。|default を legacy と誤認。|before validator parameterization。|
|AC-12a|a 単独で既存 YAML と plugin 集合を維持し、writer→fallback が成立。|forming を fallback から返す。|chain fake、closed cache。|
|AC-12b|b 後 legacy-only は同 source/plugin 集合で動く（range変化可）。|primary化で集合変更。|settings fixture。|
|AC-13a|建玉または予約があり、確定1m が bar 鮮度5分を超えて無いとき、理由付き activity `closed_bar_unavailable` を状態変化まで1回だけ出す。|無音継続/再送/10分snapshot混同。|fake clock/order。共有の発注停止 gate は A2-3 `[outage-stop-and-backfill]` の範囲外。|
|AC-13b|撤回 (r4)。cold fill 失敗を理由に発注を止める共有 gate は A2-3 `[outage-stop-and-backfill]` へ送る。|—|—|
|AC-14a|a の1m単独 timeout・key例外隔離後も保護ブロックが走る。|一key失敗でtick中断。|slow/failing source fake。|
|AC-14b|b の総予算10秒後も保護ブロックが最後のclosed足で走る。|予算超過で保護停止。|slow/failing source fake。|
|AC-15|閉場時 dueなし、空応答backoff、穴は不足診断。|週末毎tick fetch。|market clock/watermark fake。|
|AC-16|撤回 (r4): 全面 migration。導入時の cache 全削除は AC-23。|—|—|
|AC-17|b fetch prepare 中は core lock を保持せず、commit は保持する。|fetch を lock 内へ戻す/lock 外 commit。|blocking source と lock observer。|
|AC-18|撤回 (r4)。`closed_at == cutoff` の契約は A2-1 の対象外。|—|—|
|AC-19|b 後の通常 reader・ingest は primary だけを叩き、chain fallback しない。|非primary fallback。|recording sources fake。|
|AC-20|構造的不足は起動拒否、取得結果の不足は producer 内で当該 plugin の評価を見送る。|一時不足でサービス全体を停止する。|capability/empty DB/offline fake。|
|AC-21|a tool は最大100本と50本不足を構造化し、b tool は100本を満たす窓を計画し、未達なら構造化不足を返す。|不足を100本成功として返す。|short-range tool fake。|
|AC-22|activity/payload は `bars_origin`、source、interval、bar_time を出す。|provenance 欠落。|activity/payload fake。|
|AC-23|a 導入 migration は `ohlcv_cache` を全行削除し、`data_migrations` に印を残し、再実行では何も削除せず、`ohlcv_history` を不変にする。fresh DB では no-op で印だけを残す。削除後〜最初の成功取得までは初回起動と同じ振る舞いである。|一部だけ削除/二回目も削除/history を削除/fresh DB で印を残さない/削除後に初回起動と異なる振る舞い。|fresh DB と既存 DB の tmp SQLite fixture、failing fetch fake、表一覧テスト。|

既存の AC 条件・逆変異を変えず、殺すテストは次の fake-only の具体形で固定する。

|AC|使用する fixture|assert|
|---|---|---|
|AC-1|fake clock、recording fake transport、tmp SQLite|08:59 のformingと09:00のfetch failure後、reader/tool/latest が直前closedだけを返す。|
|AC-2|fake clock と fake paper broker|forming 時はcursor不変・fill 0、同一 ts がclosed後はhigh/low判定1回、次tickでも増えない。|
|AC-3|recording fake transport|forming closeだけを二値に変えてもtool出力とMTM/HWMは不変、次closed足でのみ変化する。|
|AC-4|fake clock と fake transport|`start`、`end-interval`、`end` を含む応答でkeysが`[start,end)`、grace境界、live/importer同一結果をassertする。|
|AC-5|recording fake transport と fake clock|cold fillの要求key集合と各key 1回、失敗keyだけ次tick 1回の再試行をassertする。|
|AC-6|tmp SQLite の末尾formingを含む欠損fixture|producer の時刻規則が forming を直接除外し、存在足aggregateのgolden OHLCV/tsを維持する。|
|AC-7|—|撤回 (r4)。revision dedupe は A2-1 の対象外。|
|AC-8|recording fake transport|1h×1600はcapability内の計画、15mの60日超はcall 0かつ構造化不足をassertする。|
|AC-9|loader/registry fake|未宣言を200へ正規化し、indicator requirementが依存strategy足へmax集計される。|
|AC-10|availability を変える fake|構造不足はdisabledのまま、一時不足はskip後に本数回復で同じpluginが自動復帰する。|
|AC-11|before validator parameterization|raw primary×enabled×legacy×credentialの全組合せでeffective primary/errorとpayload `live_source`をassertする。|
|AC-12a|recording chain fake と tmp SQLite|既存YAMLのplugin集合を保ち、writerからclosed fallbackまでをassertする。|
|AC-12b|settings fixture|legacy-only後のsource/plugin ID集合が同じで、rangeのみ変わり得ることをassertする。|
|AC-13a|fake clock/order fake|建玉または予約がある pair だけを対象に、market open で fresh→unavailable の遷移時 `closed_bar_unavailable` が 1 回だけ出ること、fresh回復または建玉・予約消滅で集合から外れること、閉場中は出ないこと、再起動後は再度 1 回出せることをassertする。|
|AC-13b|—|撤回 (r4)。共有の発注停止 gate は A2-3 の範囲外。|
|AC-14a|slow/failing source fake と fake clock|1m単独 timeout/一key例外後も、保護ブロックが最後のclosed足で実行される。|
|AC-14b|slow/failing source fake と fake clock|10秒総予算後も、残る保護ブロックが最後のclosed足で実行される。|
|AC-15|market clock/watermark fake|閉場中call 0、空応答後`next_probe_at` backoff、定常 range が `MAX(bar_time)` の最低1本前からであること、応答に含まれる形成中の足を normalizer が保存しないことをassertする（a 前の行の除去と空 cache の振る舞いは AC-23 に一元化）。|
|AC-16|—|撤回 (r4): 全面 migration。導入時の cache 全削除は AC-23。|
|AC-17|blocking source と lock observer|prepare中にcore lock未保持、commit中に保持をassertする。|
|AC-18|—|撤回 (r4)。`closed_at == cutoff` の契約は A2-1 の対象外。|
|AC-19|recording sources fake|b後の通常reader/ingestがprimaryだけを呼び、chain fallbackしないことをassertする。|
|AC-20|capability/empty DB/offline fake|構造不足は起動拒否、空DB/初回offline/staleは当該plugin評価の見送りをassertする。|
|AC-21|short-range tool fake|aは返却本数・100本不足・SMA50不足を構造化し、bは100本を満たす窓を計画して未達を構造化することをassertする。|
|AC-22|activity/payload fake|`bars_origin`、source、interval、bar_timeをassertする。|
|AC-23|fresh DB と既存 DB の tmp SQLite fixture、failing fetch fake、表一覧テスト|既存 DB は `ohlcv_cache` の全行が 1 回だけ消え、`data_migrations` に印が残り 2 回目は削除 0、`ohlcv_history` は byte-for-byte 不変であることをassertする。fresh DB は no-op で印だけを作ること、削除後〜最初の成功取得までは初回起動と同じ振る舞いであること、`TABLE_NAMES` と表一覧完全一致テストに `data_migrations` を含むことをassertする。|

## 6. 退けた案

|案|理由|
|---|---|
|reader ごとの `get_bars`|責任、確定時点、回数が分散する。|
|forming を後の時刻で確定化|未観測 high/low を捏造する。|
|末尾 1 行だけ削除|次点の形成中行が最新に昇格し得るため、a より前の形成中行を除去し切れない。|
|何もしない|a 前の forming 行が、取得失敗後に時刻経過だけで confirmed に見える経路を残す。|
|代案 A: 導入時に `ohlcv_cache` を全削除|採用。a より前の cache を全て除去し、削除後〜最初の成功取得までは初回起動と同じ振る舞いにする。|
|代案 B: `closed_at` 列を戻す|取得時点を識別して残余リスクを塞げるが、r1〜r4 で繰り返した入力枯渇と移行手順の Critical が戻る。|
|確定行不変を A2-1 で定義|値の訂正・版の扱いと分離できないため、`[live-confirmed-bars-into-history]` で扱う。|
|A2-1 で不足時の共有 gate を作る|発注停止は A2-3 `[outage-stop-and-backfill]` の停止 gate の責務であり、A2-1 は producer 内の評価見送りと観測に限定する。|
|完全 bucket 必須|週末/rollover の既存評価規則を変える。|
|全 interval×400 / warmup 未宣言停止|互換を壊す。|
|1m長期保存だけで再集計|yfinance 1m 8日上限で不足する。|
|native 4h/1d|MT5 21:00 UTC 格子が epoch 錨と異なる。|
|payload 名を `primary_source` に改名|承認系との互換を壊す。|
|ingest を core lock 内で取得|timeout が資金保護を停止する。|
|別 thread で ingest 取得|停止・重複起動・commit 順の契約が増えるため、同じ scheduler thread の prepare→commit→既存 tick に固定する。|
|後続の足がある既存行を migration で昇格|停止をまたいだ末尾の窓外化、応答途中欠落、使われなくなった source の末尾では、確定値で上書きされた証明にならない。|
|forming 対策として producer の bucket 規則を大きく変える|完全 bucket 化等は週末/rollover の既存評価を変え、forming 排除の目的を越える。|
|cache と history の物理統合を同時に行う|schema/保持/移行のリスクを closed contract の検証と混ぜる。統合は別束で扱う。|
|A2-1 に fallback・retry・request budget・backfill を入れる|primary の閉じた取得契約を越える横断的な失敗制御であり、A2-2/A2-3 の範囲である。|

## 7. 設定例

```yaml
datafeed:
  primary: yfinance
  intervals: [1m, 5m, 15m, 1h, 4h]
  primary_intervals: [1h] # legacy health input
  retention:
    yfinance: {"1m": "2d", "1h": "30d"}
plugin:
  # producer_source は legacy-only raw input。新設定には書かない。
```

既存 YAML 無改変互換は parse・起動し、同じ effective primary と approved/deployed plugin 集合になることをいう。同一 OHLCV、signal、fill は保証しない。`intervals` は保存集合でなく registry が保存集合を決める。

## 8. 裁定と未裁定

**裁定済:** tick ingest 一 owner（b）、a 暫定 writer、grace 付き確定、形成中を保存しない writer と読み出し時時刻規則、`ohlcv_cache` の列変更なし・`data_migrations` 表 1 つの新設、現行 UPSERT、完全 bucket 不採用、planner 式、normalized max_bars、二分類不足（取得結果の不足は producer 内の評価見送り）、primary matrix/credential、`live_source` 維持、a/b 分割、timeout/隔離。I1 として `TABLE_NAMES` および表一覧の完全一致テストを新設表に更新する。

**ユーザー裁定済 (2026-09-21): 導入時に cache 全削除:** a 導入時に既存 migration 機構で `ohlcv_cache` を 1 回だけ全行削除し、`data_migrations` に印を残し、削除件数を log に出す。削除後〜最初の成功取得までは cache が空であり、初回起動と同じ振る舞いである。反例 (2) の旧版 rollback 後の再導入は支援せず、runbook に従う。代案 B は `closed_at` 列の復活（r1〜r4 の Critical が戻る）であり、採らない。`closed_bar_unavailable` は pair 単位のメモリ集合による fresh→unavailable 状態機械とし、閉場中は判定しない。b の定常取得・cache 保存 owner は tick ingest だけで、readonly worker tool の要求時 fetch は保存しない例外である。b3 は撤回済み AC-13b を参照しない。

**★ 裁定済（r4）:** `closed_at`、既存行の全面 migration、確定行不変、revision dedupe、起動時 warm、移行に伴う共有 gate を A2-1 から外す。確定行不変は `[live-confirmed-bars-into-history]` へ、共有の発注停止 gate は A2-3 `[outage-stop-and-backfill]` へ申し送る。

**ユーザー裁定済 (2026-09-21、確認 3 点):** (1) 取得の唯一の owner を scheduler tick 先頭の ingest 段にし、外部取得は lock 外 (予算 10 秒)・commit だけ lock 内とする。要求の増分 (1h 足 1 個で 24 回/日/pair) を許す。「現状その設計でよい、テスト運用で問題があれば対応する」。(2) 有効な源が 2 個以上で `datafeed.primary` 未記載なら起動拒否でよい。(3) 実装は A2-1a → A2-1b の 2 段。動作確認は両方の実装が完了してから行う。

**未裁定:** grace の既定値（実測待ち）、保持 margin の数値、ingest 予算の既定値以外の調整値（本文の初期既定は10秒）。既存行の全面 migration は項目ごと削除し、未裁定には置かない。

## 9. 実測

### 外向き通信なしで測る

最初の spike は fake transport/tmp SQLite のみで、forming→fetch failure、primary matrix、planner、loader 正規化後 max_bars、現行集約 golden、既存 DB gap 分布の read-only 集計を測る。pytest、service 起動、bridge 接続はしない。

### bridge GET 数回で測る（指揮者が r2 前に実施）

実測済 (2026-09-21 10:38〜10:42 UTC、平日開場中、GET 22 回、`tmp/design-a2/measure/a2-1-summary-20260921.md`。各 1 回の観測で、構造的な保証ではない):

* **MT5 の源 1h と 5m→1h (epoch 錨、存在する足だけで集約) は確定 143 本すべてで OHLCV 完全一致。** 1m→1h も取得窓の端の 1 bucket を除き一致。→ MT5 については live (源 1h) と backtest (5m 基底) の 1h 系列が食い違う証拠は無い。yfinance / Twelve Data は未測定 (外向き通信になるため測らない)。
* `/server-time` の `utc_time` とローカル UTC の差は 0.002 秒 (5 回)、`server_time` は厳密に UTC+3。→ MT5 primary ではローカル時計のずれは当面無視できる。
* 確定済み 1m 358 本・1h 13 本を 30 秒あけて再取得し全件一致。→ 確定した足を源が後から訂正する観測は無い (確定行の不変契約は r4 裁定で [live-confirmed-bars-into-history] に送った) (長い間隔・週末またぎは未測定)。
* 欠損: bridge の 1m 直近 5 暦日で週末以外の gap は 7 件、2〜4 分、UTC 20〜21 時 (rollover) に集中。5m は 15 分が 1 件。実 DB (mt5 5m) で 5m が 12 本そろわない 1h bucket は 8,325 中 3 (0.04%)、1h 相当が 4 つそろわない 4h bucket は 2,084 中 76 (3.65%、金曜 20:00 UTC 起点 = 1 時間分・日曜 20:00 UTC 起点 = 3 時間分を含む)。→ 完全 bucket 規則を採らない裁定の根拠。
* **grace は未確定**: 分境界直後の 1 回の観測は「最後の足 = いまの分の形成中の足」を確かめただけで、直前の分の足が境界の何秒後に最終値になるかは測れていない。A2-1a の spike の後、bridge GET で測り直す (境界 +0 / +1 / +2 / +5 / +10 秒で直前の足を取得し、+60 秒の値と比べる)。それまで既定値は置かない。
* 参考: `ohlcv_history` (mt5) は 1m が 9/2、5m が 9/4 で止まっている (import は手動操作で、常時の書き込み経路ではないため想定どおり)。

## 10. 呼び出し元・検索根拠

```bash
rg -n --glob '!tmp/design-a2/design-A2-1.md' 'def (get_bars|latest_1m_bar|load_cache_bars|upsert_cache_bars|prune_cache|load_resampled_frame)|\b(get_bars|latest_1m_bar|load_cache_bars|upsert_cache_bars|prune_cache|load_resampled_frame|producer_source|yf_bars)\b' src tests
```

|対象|実行可能な call graph|非実行出現との区別|
|---|---|---|
|cache writer|source → `PriceProvider.get_bars` (`src/agentic_fx/datafeed/price_provider.py:143-194`) → `upsert_cache_bars`; derived は `_derive` → base保存 (`:329`)。|`ohlcv_history` importer は別テーブル。|
|init healthcheck|`init` コマンド → 書込み可能 `PriceProvider(conn, settings, clock).healthcheck` (`src/agentic_fx/service.py:390-424`) → `get_bars`。`build_app` は provider を構築するだけ。|trade healthcheck は別のreadonly node。|
|scheduler bar|service wiring (`src/agentic_fx/service.py:729-760`) → `latest_1m_bar` → `get_bars`; 実行はfresh取得 `src/agentic_fx/core/scheduler.py:348`、MTM `:416`、pending fill `:938`、open exit `:989`、marking `:1023`。|scheduler doc/testの語句は実callでない。|
|producer/resample|`load_resampled_frame` の実callは live producer `src/agentic_fx/plugin/signal_producer.py:208`、strategy adapter `src/agentic_fx/plugin/strategy_adapter.py:134`、analysis `src/agentic_fx/backtest/analysis.py:116,374` → `backtest/timeframes.py`。|`backtest/timeframes.py` の `producer_source` はdocstring。|
|`producer_source` と payload|設定 `src/agentic_fx/config.py:392` → startup validation `src/agentic_fx/service.py:558-562` → producer `:621-625`。payloadは approval `src/agentic_fx/plugin/approval.py:510`、switch `src/agentic_fx/plugin/switch.py:1207-1209,2072-2074`、improve `src/agentic_fx/loops/improve_loop.py:1625` の `live_source`。b 後は値の出所だけprimaryにする。|payload field名は変えない。|
|quote 内部 `yf_bars`|`yf_quote` → `yf_bars(pair, "1m", 1)` (`src/agentic_fx/datafeed/sources.py:93-97`) はquote経路でありingest対象外。|bars chain のyfinanceとは別用途。|
|mission registry|親は `build_mission_registry` にproviderを渡す (`src/agentic_fx/service.py:838-843`)。worker はreadonly connection → registry (`src/agentic_fx/mission_worker.py:923-950`) → readonly `PriceProvider` (`src/agentic_fx/tools/mission_registry.py:57-77,79-111,125-134`) で、保存せず要求時fetchする。|improve分岐はtrade provider/readonly seamを使わず、holdout/improve RPCはbar writerでない。|

## 11. 実装順と AC 対応

|task|内容|主な AC|依存|
|---|---|---|---|
|a0|外向き通信なし spike: forming→取得失敗、normalizer が現物writer（native/derived）で成り立つか、1m timeout、`ohlcv_cache` を全行削除する一回 migration（再実行 no-op・history 不変・fresh DB は印だけ・削除後〜最初の成功取得までは初回起動と同じ振る舞い）を fake transport/tmp SQLite で確認し、実 DB（read-only）で producer が読める1h本数を前後比較する。|AC-1,4,14a,15,23|なし|
|a1|`ohlcv_cache` の列は変えず、`data_migrations` 表を新設して normalizer、時刻規則 reader、timeout・key隔離、既存 `init_db` migration 列に載せる全行削除 migration を実装する。`TABLE_NAMES` と表一覧の完全一致テストを更新する。provider/Python の native・derived base・cache fallback・readonly返却と、producer SQL の `load_resampled_frame` cutoff の二境界を確認する。|AC-1–4,12a,13a,14a,15,23|a0|
|a2|paper/MTM/HWM/LLMをclosed入力へ配線し、activityとfallbackを統合する。|AC-2,3,12a,13a,21,22|a1|
|a3|fake-only integrated acceptance とgrace再実測の準備を固める。|AC-1–4,12a,13a,14a,15,21,22|a2|
|b0|spike: registry、loader正規化、planner、設定直積、aggregate golden、同じscheduler threadのprepare→commit→既存tick分割が現物構造で可能かを外向き通信なしで確認する。|AC-5,6,8–11,15,17,20|a3|
|b1|primary before validator とpayload値の出所切替を実装する。|AC-11,12b|b0|
|b2|registry/planner/RetentionPlanと二分類不足を実装する。|AC-8–10,15,20,21|b1|
|b3|tick ingest/cold fill/due/足別保存を、同じscheduler threadで外部取得prepareはlock外・commitのみlock内・既存tick後続で実装する。|AC-5,14b,15,17,19–20|b2|
|b4|producer target closed readerとintegrated acceptanceを実装する。|AC-6,9,10,12b|b3|

## 12. 変更履歴

| 日付 | 版 | 変更 | 理由 | commit |
|---|---|---|---|---|
| 2026-09-21 | v0.1 | 初版。 | 初稿 | — |
| 2026-09-21 | v0.2 | r1 裁定を圧縮反映。 | codex 設計レビュー r1 (C7/I7/M1) | — |
| 2026-09-21 | v0.3 | r2 全9裁定を反映。a writer/warm/fallback、migration、ingest隔離・due、二分類不足、設定直積、AC復元を追加。 | r2 (C2/I7) | — |
| 2026-09-21 | v0.3.1 | consumer/seed inventory、読み手別forming理由、provenance、退けた案、ACテスト具体、file:line call graph、a0/b0 spike task表を復元。 | v0.3 の改訂時に圧縮で落ちた契約の復元 | — |
| 2026-09-21 | v0.4 | r3 全7裁定を反映。既存行全NULL/warm昇格、構造的拒否とdegraded起動、a/b tool本数、同一scheduler threadのprepare/commit/tick、AC分割・追加、init/warm経路を訂正。 | r3 (C2/I4/M1) | — |
| 2026-09-21 | v0.5 | r4 裁定を反映。`closed_at`、既存行 migration、確定行不変、revision dedupe、起動時 warm、移行由来の共有 gate を外し、保存時 normalizer・読み出し時時刻規則・限定した残余リスクへ改めた。 | r4 (C1/I4)。既存行の移行まわりが 4 周連続で Critical を生んだため部品ごと外した | — |
| 2026-09-21 | v0.6 | r5 裁定全4件を反映。各 cache 系列末尾の一回削除 migration、`closed_bar_unavailable` の pair 状態機械、IV-4 の readonly 例外、b3 から AC-13b の除去を追加。 | r5 (C1/I2/M1) | — |
| 2026-09-21 | v0.7 | r6 指揮者裁定を反映。C1 は末尾 1 行削除の不完全性を残余リスクとして受け入れ、反例・解消条件・rollback runbook・代案 A/B を明記。I1 として `data_migrations` を正式な表追加とし、`TABLE_NAMES`・表一覧テスト、fresh/既存 DB の migration テストを追加対象にした。r4 の全面 migration 撤回と r5 の末尾 1 行削除再採用を明確化。 | r6 (C1/I1) | — |
| 2026-09-21 | v0.8 | ユーザー裁定 C1 を反映。a 導入時の migration を `ohlcv_cache` 全行の一回削除へ変更し、初回起動と同じ空 cache の振る舞い、印・history 不変・再実行 no-op を AC-23 と作業表へ明記した。残余リスクは旧版へ戻して再導入する場合だけとし、代案 A を採用へ移した。 | ユーザー裁定 (cache は削除してよい、初回起動と同じ状態なら問題ない) | — |
| 2026-09-21 | v0.9 | r7 (codex terra、C0/I2/M1) を反映。全削除直後に建玉があり取得が失敗し続ける場合の導入時だけの既知の振る舞いと runbook を明記、AC-15 から全削除前の前提を除去、見出しの版を訂正。 | r7 (C0/I2/M1) | — |
| 2026-09-21 | v1.0 | 確認 3 点のユーザー裁定を §8 に記録し、spec として清書 (内容は下書き v0.9 と同一) | codex 設計レビュー 7 周で Critical 0、ユーザー承認待ち | (本 commit) |
| 2026-09-22 | v1.1 | §3.3 ingest の優先順を 1m → 判断足 → その他に固定し、判断足の有限 tick 内 probe を契約に | 束 B 設計レビュー r2 I4: 1m が予算を使い切ると判断足の watermark が進まず cron mission が起動しない | (本 commit) |
