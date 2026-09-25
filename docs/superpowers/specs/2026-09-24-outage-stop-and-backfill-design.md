# [outage-stop-and-backfill] 設計書 v1.0 (A2-3)

対象 commit: `a689e2b`。作成日: 2026-09-24。A2 [live-price-source-mt5] 4 束のうち 3 本目
(先行: A2-1 [closed-bars-and-required-window] v1.1、B-1 [decision-timeframe-config] v1.0。
いずれも main に merge 済み)。対象は MT5 bridge (価格源) 不通時の停止境界と、復旧時の
backfill + 空白中の建玉再判定 (replay)。範囲外: A2-2 [要求予算/coalescing]、A2-4
[bid/ask 約定]、`[live-confirmed-bars-into-history]`、本番 MT5 発注 adapter。

これは**下書き**であり、承認は得ていない。「推測」と明記した箇所以外は現物の静的読取りで
確認した事実である。ユーザー裁定は `tmp/design-a23/user-verdicts.md`、前束レビューの
A2-3 向け裁定は `tmp/design-a23/r1-verdicts-a23.md` を正とし、蒸し返さない。v0.2 は
v0.1 に対する codex レビュー `tmp/design-a23/r1/codex-out.md` (C7/I7) の指揮者裁定
`tmp/design-a23/r1/verdicts.md` (全件採用、★ = ユーザー確認待ちだが推奨で進める) を
反映した改訂であり、これも蒸し返さない。v0.3 は v0.2 に対する codex レビュー r2
`tmp/design-a23/r2/codex-out.md` (C4/I5) の指揮者裁定 `tmp/design-a23/r2/verdicts.md`
(全件採用、蒸し返さない) を反映した改訂である。v0.4 は v0.3 に対する codex レビュー r3
`tmp/design-a23/r3/codex-out.md` (C3/I3) の指揮者裁定 `tmp/design-a23/r3/verdicts.md`
(全件採用、蒸し返さない) を反映した改訂である。v0.1 の全文は
`tmp/design-a23/design-A2-3.v0.1.md`、v0.2 の全文は `tmp/design-a23/design-A2-3.v0.2.md`、
v0.3 の全文は `tmp/design-a23/design-A2-3.v0.3.md` に退避してある。

## 要点

1. **現物にはすでに「不通なら止まる・再開する」骨格がある**が、3 箇所で利用者裁定と
   食い違うか、保護に穴がある。(a) `datafeed.yfinance.enabled: true` が既定 (example)
   のまま `mt5.enabled: true` にすると、新規発注・day 強制クローズの quote 取得は
   MT5 不通時に**無言で yfinance へ落ちる** (裁定「fallback は明示時だけ」に反する、
   現物 `price_provider.py:145-157` の writer chain)。(b) 復旧後、ingest は空白全体を
   1 回の range 取得で DB へ補充する (`ingest.py:96-100`) が、scheduler の SL/TP 判定は
   常に DB の**末尾 1 本**しか見ない (`service.py:1098-1100` → `scheduler.py:377-385`) —
   空白中に SL を跨いで戻った建玉が検知されない。(c) 判断 mission の停止に 2 つの
   経路がある: cron は watermark 停滞で即座に止まるが、signal 起動 mission の
   healthcheck は「最新確定足の開始 + 2×足幅 + grace + freshness」まで不健全と判定しない
   (1h 判断足で既定約 140 分、`service.py:1103-1120`) — その間 signal mission は古い
   データで動き得る。
2. 本設計は (a)(b)(c) を塞ぐ: `datafeed.fallbacks` を新設し writer chain を primary-only
   にする、ingest の backfill 直後に OPEN 建玉の SL/TP を新規委託した確定 1m 全件で
   replay する、healthcheck と cron 停止の両方を単一の永続 recovery 状態機械
   (`ready/degraded/backfilling`, epoch つき) に一本化する。
3. PENDING_FILL (指値未約定) は現行どおり account snapshot 陳腐化 (10 分, `accounting.py:70-77`)
   で自動取消される — 空白中に「未観測の価格で新規約定させる」replay は行わない
   (r1 裁定)。バー鮮度 5 分 (`scheduler.py:28`) を過ぎた時点で新規約定処理そのものが
   止まるため、10 分以内に復旧すれば実害は無い。
4. 状態機械は global 1 行 (bridge は 1 本、pair 横断で共有) + pair 別 `gap_start` を
   新設 DB 表 `datafeed_outage_state` に永続化し、`ingest.commit` と同じ core_lock
   transaction で更新する。休場でない「未知の空白」(market_hours が知らない祝日等) は
   自動 `ready` にせず、人間確認待ちの sub-flag を立てる。

## 1. 前提の検証

|事実|根拠 (file:line)|設計への含意|
|---|---|---|
|scheduler の 1m 取得元は DB 専用 reader で、末尾 1 本 (`rows[-1]`) だけを返す。|`src/agentic_fx/service.py:1098-1100`|SL/TP 判定 (`_fresh_bar`) も MTM も常にこの 1 本しか見ない。空白復旧直後、backfill された中間バーは一度も exit 判定にかからない。|
|`_fresh_bar` はバーが `now - bar.ts > 5分` (`_BAR_FRESHNESS`) なら `None` を返す。同じ判定を `_process_limit_fills`/`_process_exits`/`_evaluate_positions` が使う。|`src/agentic_fx/core/scheduler.py:28,377-385,455-479`|不通から 5 分を過ぎると、新規約定処理・時価評価・SL/TP 監視は「古い値で継続」ではなく**評価そのものを止める** (fail closed)。ユーザー裁定「最後に健全な1m cacheで継続」は文字どおりには成立せず、実際は「5分だけ継続し、その後は停止して復旧時に一括 replay」という設計にする方が現物の安全側規律と一貫する。|
|`_observe_closed_bar_availability` は OPEN/PENDING_FILL がある pair だけを対象に、5 分超で `closed_bar_unavailable` を状態変化ごとに 1 回だけ書く。|`src/agentic_fx/core/scheduler.py:386-406`|A2-1 で導入済み。本設計はこれを「pair 単位の観測」として使い、global 状態機械の入力の 1 つにする (状態機械自体は新設)。|
|`current_account` は最新 snapshot が 10 分より古いと `None` を返す (`max_age_min=10` 既定)。`tick()` は `account is None` で全 `PENDING_FILL` を `_cancel_all_pending(reason="account_unknown")` で取消す。|`src/agentic_fx/core/accounting.py:70-77`; `src/agentic_fx/core/scheduler.py:172-176,729-761`|不通が 10 分を超えると予約済み指値は自動的に消える。前束下書き (`tmp/design-a2/design-A.md`) の「既存 pending は取消さず `price_unavailable` にする」という記述は**現物の挙動と食い違う** — 引き継がない。本設計はこの自動取消を維持し、「10 分以内の短い不通なら pending は生き残り、5 分超で新規約定処理自体が止まっているので実害は無い」を前提にする。|
|cron mission の起動可否は判断足の確定 watermark だけで決まり、新しい確定足が来なければ `_trade_mission_due` は `None` を返す (即座に沈黙、追加の停止判定は不要)。|`src/agentic_fx/core/scheduler.py:270-316,317-325`|B-1 で実装済み。A2-3 が新設する必要はない — 停止は自動的に成立する。|
|signal 起動 mission (`trigger="signal"`) は cron watermark と独立に `signal_due_fn` で起動され、起動後 `TradeLoop._run_once_impl` が **prepare より前** に `self.provider.healthcheck(pair)` を呼ぶ。`healthcheck` の実体は `db_healthcheck` で、`now - 最新確定足.bar_time > freshness_max_min + 2×足幅 + grace` で `DataUnhealthy` を送出する。既定値 (`freshness_max_min=20`, 判断足 1h→120分, grace 30秒) で計算すると **約 140.5 分** 経過するまで不健全と判定されない。|`src/agentic_fx/loops/trade_loop.py:109-142`; `src/agentic_fx/service.py:1103-1120`; `config/settings.yaml.example:56-58`|cron は watermark 停滞で「次の確定足が来ない」ことにより実質即時に止まるが、signal 起動 mission は **同じ healthcheck を共有しているにもかかわらず、しきい値がはるかに緩い** (「次の足が来ない」ではなく「最後の足からどれだけ絶対時間が経ったか」で見ているため)。1h 判断足では最大 2 時間強、signal mission が古いデータのまま起動し得る。これは A2-3 が閉じるべき穴であり、r1 裁定「共有の発注停止 gate は A2-3 の責務」が指す具体的な欠陥だと判断した。|
|新規建玉 (成行) の quote 取得 `Executor._open` は `self.quote_fn(intent.pair)` を無保護 (try/except なし) で呼ぶ。`quote_fn` の既定実体は書込可能 `PriceProvider.get_quote` で、その内部 `_chain` は **readonly=False のとき** `mt5.enabled → twelvedata (鍵あり) → yfinance.enabled` の順に**個別の `enabled` フラグだけを見て**フォールバック chain を組む (`primary` 設定は見ない)。|`src/agentic_fx/core/executor.py:489-491`; `src/agentic_fx/datafeed/price_provider.py:140-157`; `src/agentic_fx/service.py:802,823-826` (`quote_fn = provider.get_quote`)|**Critical 相当の食い違い**: `config/settings.yaml.example:53` は `yfinance.enabled: true` が既定であり、MT5 利用者が `mt5.enabled: true` にしただけ (yfinance を明示的に無効化しない、という現実的な設定ミス) だと、MT5 bridge 不通時に新規成行注文・day 強制クローズ (`scheduler.py:907-960` の `quote_fn` 呼び出し) は**理由も記録もなく yfinance の quote へ切替わる**。ユーザー裁定「fallback は利用者が明示したときだけ」に反する。ingest (DB 書込み経路) は A2-1b で primary 専用化済みだが、quote 取得経路 (即時発注・クローズ判定) は取り残されている。A2-3 はこの経路を primary-only (明示 `fallbacks` があるときだけ) に直す。|
|`_force_close_day`/`_retry_close` の `quote_fn` 呼び出しは try/except で `DataUnhealthy` を捕捉し `day_close_deferred`/`close_retry_deferred` として次 tick に回す。一方 `Executor._open` の `quote_fn` 呼び出しは無保護。|`src/agentic_fx/core/scheduler.py:854-856,907-925`; `src/agentic_fx/core/executor.py:489-491`|day 強制クローズ・close 再試行は quote 障害耐性が既にある (架空価格で閉じない)。新規 open だけが穴で、上記の「無言 fallback」を塞げば、例外は `TradeLoop.run_once` の service boundary (`trade_loop.py:91-96`) で拾われ mission 失敗として記録される (fail closed、ただし理由の可視化は弱い — §9 で測る)。|
|ingest はソース取得の**例外**に対しては backoff せず、次 tick (60 秒後) に同じ key を即再試行する。「成功したが次の足がまだ確定していない」場合だけ指数 backoff する。|`src/agentic_fx/datafeed/ingest.py:110-129`|不通中は 60 秒ごとに (`ingest_budget_sec` 既定 10 秒を上限に) bridge へ再接続を試み続ける。A2-2 の budget/circuit breaker 導入前でも、このポーリング自体は資金保護の外 (lock 外 prepare) なので副作用は薄いが、bridge 側への無staggeredなリトライ負荷は残る (A2-2 に送る)。|
|復旧時、ingest は既存 watermark から `now` までを **1 回の range 取得** で埋める (`start = watermark - 1本分`, `end = now`)。`mt5_bars_range` の呼び出し自体は `[start, end]` を 1 回で要求することまでは確認できるが、**bridge 側の実装・MT5 API 側の 1 リクエストあたり上限本数・pagination の有無はこの repo に無く未確認**。|`src/agentic_fx/datafeed/ingest.py:96-100,142-151`; `src/agentic_fx/datafeed/sources.py:122-140`|r1 裁定 I5 によりダウングレード: 「空白全体が単一 commit で入る」は**推測**にとどめる。長い空白 (数時間〜) で上限を超えれば先頭/末尾だけが返る可能性があり、その場合は 3.3 の連続性検査が中間の欠落を検出できないと `ready` へ進めなくなる (検査自体は正しく働く前提)。上限の有無・値は §9 の bridge 実測項目に置く。上限が実在すれば ingest 側に chunk 取得が要る (別途 task 化、A2-3 本体は検出側の設計に留める)。|
|`prune_cache` は maintenance hook 毎 tick、`RetentionPlan` の cutoff で無条件に古い行を削除する (source/interval 単位、gap の有無を見ない)。|`src/agentic_fx/store/ohlcv.py:190-215`; `src/agentic_fx/service.py:1131-1134` (`on_cache_maintenance`)|不通が保持期間 (1m の既定は example で明示なし、`cache_retention_days` 既定 30 日) を超えて続くと、`watermark` そのものが prune で消え、復旧時の `start` は「必要本数からの計算」(`ingest.py:98` の `watermark is None` 分岐、`plan.days`) にフォールバックする。**空白の先頭を覚えているのは `watermark` だけ**であり、それが消えると空白の先頭がプランナー計算値に置き換わり、真の空白開始よりも狭い/広いレンジで backfill してしまう。gap_start を DB watermark と独立に永続化する必要がある根拠。|
|`_processed_bar_ts` (二重処理防止カーソル) はプロセスメモリのみ。|`src/agentic_fx/core/scheduler.py:98,382,1082`|不通中にプロセスが再起動すると、再起動後の最初の tick は「どこまで exit 判定済みか」を失う。復旧 replay の起点はこのメモリ変数に依存できず、DB 側 (`orders` の `filled_at`/`created_at` と、新設する永続 `gap_start`) から導出する必要がある。|
|`market_hours.is_market_open` は週末 (金 21:00 UTC 〜日 21:00 UTC) と 12/25・1/1 (取引日ラベル基準) しか休場と判定しない。閉場中は ingest の due 自体が立たない (`ingest.py:81-82`)。|`src/agentic_fx/core/market_hours.py:25-40`; `src/agentic_fx/datafeed/ingest.py:81-82`|ブローカー固有の休場・メンテナンス (例: 年末年始の短縮取引、サーバメンテナンス) は `is_market_open=True` の間に bridge が「200 だが 0 本」を返す形で現れ得る。これは MT5 不通 (接続エラー/timeout) と区別する必要がある — 現物にこの区別は無い。|
|`Ingest.prepare()` は `_pending`/`last_request_count`/`budget_exhausted` を毎回初期化するが、`last_errors` は**クリアしない** (失敗 key は次回成功しても残り続ける)。`next_probe_at` は「次にいつ取りに行くか」であり「直前 tick が成功したか」を表さない。`commit()` 後は `_pending` も消えるので、prepare 後・commit 前の「この tick で何が起きたか」を保持する属性が無い。|`src/agentic_fx/datafeed/ingest.py:78-93,107-129,135-140`|r1 裁定 C2 の指摘のとおり: `last_errors` の残留に頼ると復旧後も `degraded` に留まり続け、`next_probe_at` に頼ると「当 tick 成功」を証明できず誤って `ready` に進みうる。状態機械は ingest を変更せず観測するのではなく、`prepare()` が返す tick-local な結果 (`attempted/succeeded/failed/deferred/empty`、key ごと) を必要とする — 3.1 で `Ingest` に最小限の戻り値変更を加える。|
|`ohlcv.latest_closed_cache_bar_time` の SQL は `symbol`/`interval`/`bar_time<=cutoff` だけで絞り、`source` 条件が無い。|`src/agentic_fx/store/ohlcv.py:176-187`|r1 裁定 I3: MT5 primary が停止していても同じ `(pair, interval)` に別 source (yfinance 等、過去の設定変更や readonly healthcheck 経由) の新しい行が残っていれば、この関数は誤って「健全」を返す。outage 判定用の watermark 読み出しは `storage_source` (primary から導出、`ingest.py:26`) を引数に取る形に直す必要がある — B-1 の cron due 側 (`_latest_cron_watermarks`、`scheduler.py:283-291`) も同じ関数を呼んでいるため、修正は両方に効く。|
|`Executor.close_order` は `now = self.clock.now()` を取り、`closed_at`/`close_reason` をこの現在時刻で書く。paper 専用の「過去の bar 時刻でクローズを確定する」経路は無い。|`src/agentic_fx/core/executor.py:860-871`|r1 裁定 C5: 11:47 復旧時に 10:15 の bar で SL を検出しても、既存 `close_order` をそのまま使うと `closed_at=11:47` になり、事後判定の意味 (「10:15 に閉じるべきだった」) が記録から失われる。新設 `close_order_at(event_time, price, reason)` が要る (3.3)。|
|`store/orders.py` の `insert`/`update_fields` はどちらも呼び出しの最後に自前で `conn.commit()` する。`core_lock` は `threading.RLock` (`service.py:477`) であり SQLite のトランザクション境界ではない。|`src/agentic_fx/store/orders.py:17-29,37-45`; `src/agentic_fx/core/transitions.py:32-40`|r1 裁定 C6: 「`ingest.commit()` と同じトランザクションで状態機械を更新する」は、現物の store primitive がどれも即 commit する前提では成立しない。replay 中に 1 件の注文遷移だけ commit されてプロセスが落ちると、state/gap の永続化とずれる。commit しない store primitive (`update_fields` 相当の非 commit 版) と、呼び出し側の明示 `with conn:` が必要 (3.3)。|

## 2. 目的・範囲・範囲外

**目的**: MT5 bridge (価格源) が不通になったとき、資金保護以外を安全に停止し、復旧を
「health が 1 回通ったら再開」ではなく「空白を埋めて既存建玉の見落としを事後判定してから
再開」にする。paper 執行 (現状の唯一の broker) を対象にし、将来の本番 MT5 adapter に
残す宿題を明記する。

**段階分割 (r1 裁定、範囲 ★ 採用)**: 1 本の spec に書くが、実装は 3 段に分け、各段が
単独で main に入る順で配備できるようにする (r1 の Important「1 束として大きく、失敗時の
切り分けができない」への対応)。以下、3.x 各節・task 表 (§11) の項目に段を付す。

|段|ticket / 内容|配備条件|
|---|---|---|
|**A2-3a** `[quote-primary-only]` (hotfix、先行)|書込可能 provider の quote/bars chain を primary 1 個に固定 (readonly と同じ規律)。fallback は `datafeed.fallbacks` (明示リスト、既定空) があるときだけ、その順に使う (§3.2 quote fallback 是正)。|state 機械に依存しない独立 hotfix。単独で先に main に入れられる。|
|**A2-3b** `[outage-stop-and-observe]`|停止 gate (cron/signal mission・新規約定・新規 open) + 観測 (ingest tick report、§3.1) + 永続 episode (state/gap、§3.1) を実装する。`replay`/backfill 完了条件・連続性検査は含まない。|`ready` 復帰は**手動** (人間がシェルで `afx> data resume` 相当のコマンドを叩く、§3.4/§8) — 自動 backfill/replay が無いため。停止境界の安全性だけを先に配備できる。|
|**A2-3c** `[backfill-and-replay]`|連続性検査 (既知の閉場を除外、§3.3/3.4) + paper の履歴時刻 replay (`close_order_at`、§3.3) + `backfilling→ready` の自動遷移。|A2-3b の永続 episode の上に乗る。ここで初めて自動復旧が有効になる。|

**範囲に含む** (末尾の `(a)`/`(b)`/`(c)` は上表の段):
- 復旧・不通の判定を持つ永続状態機械 (`ready`/`degraded`/`backfilling`、epoch つき、
  pair 別 `gap_start`)。**(b)**
- 停止境界の明確化 (どのフックが止まり、どれが止まらないか)。**(b)**
- cron/signal 両方の mission 停止を同じ状態機械に統一する。**(b)**
- 復旧時の backfill 完了条件 (1m 執行本数・判断足・plugin warmup・gap_start からの
  連続性、既知の閉場を除外) と `backfilling→ready` の自動遷移。**(c)**
- 空白中に SL/TP を跨いだ OPEN 建玉の replay (既存の `check_exit` の SL 優先・gap 規則を
  そのまま使い、`close_order_at` で履歴時刻を保存する)。**(c)**
- `datafeed.fallbacks` の新設 (writer/quote chain を primary-only にし、明示時だけ
  fallback を許す)。**(a)**
- 休場でない未知の空白を人間確認へ回す最小の仕組み。**(c)**
- 設定キー・活動ログ・状態可視化。**(a)/(b)/(c) 各段で追加**。

**範囲外** (それぞれ別 ticket の責務):
- A2-2 `[要求予算/coalescing]`: ingest の HTTP リトライ間隔・circuit breaker・
  レート制限そのものの設計。本設計は「不通の判定」に必要な最小限 (連続失敗回数) だけ扱う。
- A2-4 `[bid/ask 約定]`: paper 執行が bid/ask のどちらを使うか。本設計は既存の
  mid + 想定 spread のままの約定判定契約 (`paper_fills.py`) を変えない。
- `[live-confirmed-bars-into-history]`: 確定行の値訂正・版管理。
- 本番 MT5 発注 adapter: 現アプリは `PaperBroker` のみ (`src/agentic_fx/core/paper_broker.py`
  が唯一の broker 実装、MT5 発注は未配線)。3.5 で adapter 化時の設計メモだけ残す。
- `[multi-decision-timeframes]`: 複数判断足の状態機械化。本設計は B-1 と同じく単一
  判断足 (`datafeed.decision_timeframe`) を前提にする。

## 3. 設計

### 3.1 データ状態: `ready` / `degraded` / `backfilling` (段 b/c、下記参照)

**判定者・観測データの是正 (r1 裁定 C2)**: v0.1 は「ingest は変更しない」としたが、
現物の `Ingest` は「この tick で何が起きたか」を外から検証可能な形で保持しない —
`last_errors` は失敗した key が後で成功してもクリアされず残留し (`ingest.py:78-93` は
`_pending`/カウンタだけ初期化)、`next_probe_at` は「次にいつ取りに行くか」であって
「直前 tick の成否」を表さない (§1)。このまま observe すると、一度失敗した key が
永続 `degraded` に固定される、または未 probe の key を「成功」と誤認して早すぎる
`ready` に進む、のどちらかが起きる。そこで `Ingest.prepare()` に最小限の変更を加え、
tick ごとに使い捨てる **immutable な tick report** を返すようにする:

```python
@dataclass(frozen=True, slots=True)
class IngestTickReport:
    attempted: frozenset[tuple[str, str]]
    succeeded: frozenset[tuple[str, str]]
    failed: frozenset[tuple[tuple[str, str], str]]  # (key, エラー文字列)。この tick 限り
    deferred: frozenset[tuple[str, str]]  # budget 切れで持ち越し (backoff 未試行とは区別する)
    empty: frozenset[tuple[str, str]]     # 200 だが 0 本 (3.4 の休場区別に使う)
```

**r2 裁定 I1**: v0.2 は `failed: dict[...]` としていたが、`frozen=True` の dataclass でも
dict フィールドの中身は返却後に呼び出し側が変更できる (フィールド自体の再代入が
禁じられるだけ)。observe より前にこの dict が書き換われば判定が変わり得るため、
`failed` を `frozenset[tuple[key, str]]` にして真に immutable にする。あわせて
`deferred` (budget 切れで**まだ試行していない** key) と、backoff 由来で「この tick は
そもそも probe しなかった」key を区別する — 後者は `attempted` に含めない (`attempted`
に無く `failed`/`succeeded`/`deferred`/`empty` のいずれにも無い key は「backoff 待ちで
not-attempted」と読める、という契約を明記する)。`deferred` を budget 由来の
持ち越しだけに限定することで、observe 側は「試みて失敗」(`failed`) と「試みてすらいない」
(not-attempted、backoff) を混同しない。

`prepare()` の戻り値を `int` (request 数、既存互換) から `(count, report)` に拡張する
(呼び出し元は `_scheduler_tick_once` の 1 箇所のみ — §10 の grep で確認済み)。
`last_errors`/`next_probe_at` は既存の再試行制御としてそのまま残し、状態機械の入力
からは外す。

**判定者**: `_scheduler_tick_once` (`src/agentic_fx/service.py:1224-1246`) が
`app.ingest.prepare()` → `with app.core_lock: app.ingest.commit()` の直後、
`app.scheduler.tick()` を呼ぶ**前**に、新設する
`OutageStateMachine.observe(report, watermarks, now)` を `app.core_lock` 保持下で呼ぶ。
**段 b では `ingest.commit()` は現行のまま (`store/ohlcv.py` 内で自前 commit) でよく、
`observe()` の episode 記録は別の SQLite transaction で構わない (r2 裁定 C2)**: 段 b には
replay が無いため、`ingest.commit()` の commit と episode 記録の commit がずれても
(1 件だけ commit されてプロセスが落ちても) 「backfill 済みの bar と state の食い違いから
誤った巻き戻しが必要になる」という害が生じない — 次 tick で observe が改めて DB
watermark を読み直し、正しい `state`/`epoch` に収束する。段 b の同一 transaction 要件は
「(1) 停滞判定に使う watermark 読み出しが source 絞りであること」「(2) 再起動後に
`gap_start`/`epoch` が失われないこと」(IV-8) だけであり、ingest の commit 分離自体は
問題にしない。**c 段では事情が異なる** (backfill・注文遷移・pair cursor・state を
1 つの明示 transaction にまとめる必要がある — 3.3 の「トランザクションの是正」参照、
r2 裁定 C2)。`watermarks` は pair×`(1m, decision_timeframe)` の DB watermark だが、
**`storage_source` で絞る** (r1 裁定 I3): `ohlcv.latest_closed_cache_bar_time`
(`src/agentic_fx/store/ohlcv.py:176-187`) は現物では `source` 条件を持たず、MT5 primary
が止まっていても別 source の古い行が残っていれば誤って健全と判定し得る。この関数に
`source` 引数を追加する — B-1 の `_latest_cron_watermarks`
(`scheduler.py:283-291`) も同じ関数を呼んでいるため、修正はそちらにも効く (I3 は C2 に
含める)。`report.deferred` (budget 切れ) 単独は失敗として扱わない (A2-1 spec §3.3 の
「残時間で順に処理、未処理は次 tick に回す」設計と衝突しないため)。

**resume 要求の消費 (r3 裁定 C2、Critical)**: `Commands.data_resume()` (§3.1
「段 b の `ready` 復帰は手動」参照) は `app.core_lock` を取らず、`datafeed_outage_state`
に `resume_requested_at`/`resume_acknowledge` を書くだけで即座に戻る。
`OutageStateMachine.observe(...)` (上記 `app.core_lock` 保持下の呼び出し) は、
`report`/`watermarks` に基づく通常の状態判定を終えたあと、**同じ lock 区間の中で**
`resume_requested_at` が立っていれば §3.1 の前提条件 (1)(2) をこの tick の `report`/
`watermarks`/`now` で評価し、満たせば `ready` へ遷移させて `resume_requested_at` を
クリアする。満たさなければ `resume_requested_at` をクリアして拒否理由を activity に
記録する (いずれの場合も要求は 1 tick で消費し、次 tick に持ち越さない)。これにより
`Commands` 側の shell 実行と scheduler tick の間に競合があっても、判定は常に
`commit → observe → resume 判定 → scheduler.tick()` という単一の lock 区間内の、
同一 `now`・同一 `report`・同一 `watermarks` を使って行われる。

**永続化の形 — 候補 A に確定 (r3 裁定 C3、Critical。旧: b 段の spike (T0) で決める、
r1 裁定 I6)**: v0.1 は「`state.json` (`StateStore`) は追加すると `StateError`
(`_REQUIRED_KEYS` の完全一致検査、`state.py:41-43`) で既存インストールを壊すため
使わない」としたが、これは**現物の誤読**である。実装は `_REQUIRED_KEYS` に含まれる
キーの**欠落だけ**を拒否し (`missing = [k for k in _REQUIRED_KEYS if k not in raw]`)、
未知の追加キーは無視する — 完全一致ではない。したがって `AppState` に `episode`
フィールドを 1 つ追加しても既存 `state.json` の読込みは壊れない。ここまでは v0.2/v0.3
の記述どおりだが、r3 レビューは「DB 表・`state.json` のどちらでも IV-8 を満たせるので
spike で選ぶ」という枠組み自体を Critical と判定した: `state.json` は
`ingest.commit()` と別ファイルであり、そもそも SQLite の transaction に参加**できない**
(spike で確かめるまでもなく候補たり得ない)。したがって選択は spike 送りにせず、
**候補 A (DB 表 2 つ) に確定し、候補 B (`StateStore`) は退けた案とする** (§6)。

|候補|形|長所|短所|
|---|---|---|---|
|候補 A (以下 3.1 は暫定的にこちらで記述): DB 表 2 つ|`datafeed_outage_state` (1 行) + `datafeed_outage_gap` (pair×interval×epoch)|`ingest.commit()` と同じ SQLite 接続・トランザクションに素直に乗る (C6)。`TABLE_NAMES` 完全一致テストの既存規律に乗る。|表が増える。pair 別 gap を正規化する分だけ DDL/migration が増える。|
|候補 B: `StateStore` の 1 キー|`AppState.episode: {epoch, state, started_at, cursor, replay_through} \| None` (pair 別の値はこの dict の中に入れ子で持つ)|新設表が不要。|`state.json` は別ファイルであり `ingest.commit()` の SQLite トランザクションには乗らない。C6 が求める「注文遷移・cursor・state を同一 transaction に」を満たすには DB commit → state.json 書込みの 2 段になり、両者がずれる窓 (crash 順序次第で cursor だけ進んで注文が戻る、または逆) が残る。|

T0 (spike、b 段着手前) は候補選択のためのものではなくなった (r3 裁定 C3) —
fake クラッシュ注入 (§9) で候補 A のトランザクション境界 (nocommit primitive・
`with conn:`) が実際に crash-safe かどうかを確認する目的に絞る。**以下 3.1〜3.3 は
候補 A で確定して書く**。

**永続化 (候補 A の DDL)**: 新設表 `datafeed_outage_state`。

```sql
CREATE TABLE datafeed_outage_state (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  state TEXT NOT NULL,               -- 'ready' | 'degraded' | 'backfilling'
  epoch INTEGER NOT NULL DEFAULT 0,  -- ready→degraded 遷移ごとに +1 (flap は増やさない)
  confirmed INTEGER NOT NULL DEFAULT 0,     -- 2 tick 連続失敗の通知確定 (C3、gate には使わない)
  entered_degraded_at TEXT,          -- この epoch の degraded 突入時刻 (UTC ISO、tick 1 で即書く)
  ready_streak INTEGER NOT NULL DEFAULT 0,  -- backfilling→ready の連続健全 tick 数 (段 c)
  pending_human_confirmation INTEGER NOT NULL DEFAULT 0,
  resume_requested_at TEXT,          -- (r3 裁定 C2、段 b) `data resume` が要求された時刻。
                                      -- 次 tick の commit→observe→resume 判定→scheduler の
                                      -- 1 lock 区間で消費されるまでの一時フラグ。
  resume_acknowledge INTEGER NOT NULL DEFAULT 0,  -- (C2) 要求時の `--acknowledge` の有無
  updated_at TEXT NOT NULL
);
CREATE TABLE datafeed_outage_gap (
  pair TEXT NOT NULL,
  interval TEXT NOT NULL,            -- '1m' or decision_timeframe
  epoch INTEGER NOT NULL,
  gap_start TEXT NOT NULL,           -- 不通直前の watermark (bar_time)
  replay_through TEXT,               -- 段 c: この (pair, interval, epoch) について replay が完了した最新 bar_time (C6/C3)
  PRIMARY KEY (pair, interval, epoch)
);
```

**`replay_through` は global 1 列ではなく `(pair, interval, epoch)` の行に持つ
(r2 裁定 C3、Critical)**: v0.2 は `datafeed_outage_state.replay_through` という
global 1 列に置いていたが、本文 (3.3) は「この pair についてどこまで replay が
終わったか」と書いており、DDL とプローズが矛盾していた。global 1 列のままだと、
EURUSD を 11:45 まで replay した直後 (USDJPY はまだ未処理) に再起動すると、単一の
`replay_through=11:45` が USDJPY にも適用されてしまい、USDJPY の履歴を丸ごと
飛ばすか、EURUSD を重複処理するかのどちらかが起きる。是正として `replay_through` を
`datafeed_outage_gap` (pair×interval×epoch のキーを既に持つ表) の列に移し、
pair・interval ごとに独立して進捗を保持する。再開点は
`next_expected_trading_time(replay_through)` (3.3/3.4 で共有する「次の期待取引時刻」
ヘルパ、C7) とする — `replay_through` そのものの次の 1m ではなく、休場を除外した
次の期待取引時刻から再開する。

1 行固定 (`id=1`) は global 状態 (bridge は 1 本、全 pair・全 interval が同じ primary を
共有する — A2-1b で primary 専用化済み) を表す。`datafeed_outage_gap` は pair 別の
replay 起点 (`gap_start`) と pair 別の replay 進捗 (`replay_through`) を epoch ごとに
持つ (同じ epoch 内での flap は `gap_start` を書き換えない — 最初の degraded 突入時の
watermark を保持し続ける)。両表とも `app.core_lock` 保持下・`conn_core` 使用で更新する。
段 b では `ingest.commit()` とは別 transaction で構わない (判定者節、C2)。段 c では
backfill (nocommit)・注文遷移 (nocommit)・pair cursor・state の更新を 1 つの明示
`with conn:` にまとめ、その中でこの 2 表も更新する (§3.3 のトランザクションの是正)。
`data_migrations` と同じ新設表方式を踏襲する (A2-1 spec §3.1 の前例)。`TABLE_NAMES` と表一覧の完全一致
テストに追加する。

**なぜプロセスメモリでないか**: `_processed_bar_ts`/`next_probe_at` は既にメモリのみで
「今動いている間」の重複防止には十分だが、A2-1 spec が指摘したとおり (§1) 再起動で
消える。不通は数分〜数時間続き得るため、その間にサービスが再起動すれば
(watchdog/systemd 再起動、手動再起動) `gap_start` (recovery cursor) を失って replay の
起点が消える。永続の形は候補 A (DB 表 2 つ) に確定した (r3 裁定 C3、上記)が、
「プロセスメモリのみ」を選ばない理由はどちらの候補でも変わらなかった。

**遷移 (r1 裁定 C1/C3 で全面改訂: 停止は即時、「2 回連続」は通知確定にだけ使う)**:

- **`ready → degraded` は即時 (段 b)**: この tick の `report.failed` に hard 必須 key
  (`RequirementRegistry` の `core hard` — paper/MTM の 1m と health の判断足、A2-1 spec
  §3.4 の表) が 1 件でもある、**または**該当 key の (I3 で source 絞りした) DB watermark
  停滞について次の**単一の式**を満たす場合、**その tick のうちに** `state=degraded` へ
  遷移する — v0.1 の「2 回連続を要求する」は誤りであり、r1 裁定 C3 はこれを Critical とした
  (「中間足での SL/TP を永久に落とす」「signal mission が古いデータで起動できる」実害が
  ある)。

  **停滞式 (r2 裁定 I2、二重加算を廃止)**: `expected = watermark + 2×足幅 + grace`
  を過ぎた**最初の tick**で停滞と判定する (v0.2 は `grace + budget` を別途加算しており
  二重の猶予になっていた — `budget` は ingest 内部の処理時間予算であって停滞判定の
  猶予ではないため式から外す)。実時刻 AC (tick は毎分 :00 に発火、`grace=30秒` の
  共通シナリオ、§5):
  - 1m: `watermark=09:59` → `expected = 09:59 + 2分 + 30秒 = 10:01:30`。これを過ぎた
    最初の tick は `10:02:00` — この tick で停滞と判定する。
  - 1h: `watermark=09:00` → `expected = 09:00 + 120分 + 30秒 = 11:00:30`。これを過ぎた
    最初の tick は `11:01:00` — この tick で停滞と判定する。

  突入時、既に開いている episode が無ければ
  `epoch += 1`、`entered_degraded_at = now`、`confirmed = 0`、`ready_streak = 0` を書き、
  各 pair の `gap_start` (= recovery cursor) = **直前 tick で succeeded だった直近の
  watermark** (無ければ registry の必要本数からの計算開始点) を `datafeed_outage_gap`
  に挿入する。新規判断 mission (cron/signal)・新規約定・新規 open はこの瞬間から
  止まる (§3.2)。`report.deferred` (budget 切れ) 単独では突入しない (A2-1 spec §3.3 の
  「残時間で順に処理、未処理は次 tick に回す」設計と衝突しないため)。
- **`confirmed` への昇格 (2 回連続、通知専用)**: 同一 episode 内で hard failure/停滞が
  **連続 2 tick** 観測されたら `confirmed = 1` にする。`confirmed` は status 表示・
  `data_outage_degraded` activity ログの閾値としてのみ使い、mission 停止・新規約定停止の
  gate 判定には**使わない** (gate は tick 1 から既に効いている — C3 の「2 回連続は通知上の
  degraded 確定にだけ使う」)。単発の失敗で次 tick に hard 必須 key が全て succeeded に
  戻った場合、DB backfill 自体は ingest の watermark ベース range 取得で自然に埋まるが、
  **段 b には連続性検査/replay が無いため episode は自動では閉じない** (下記「段 b の
  ready 復帰」参照)。episode が開いている間 (`confirmed` の有無に関わらず)、activity に
  `data_outage_unprocessed_bars` (pair、推定未処理足数) を出す (C3 の「未処理の足 N 本」)。
- **`degraded → backfilling` (段 c のみ、段 b には無い)**: 1 回の ingest tick で全
  hard 必須 key が例外なく成功した場合に入る。まだ `ready` にはしない — 1 回成功しただけ
  では、応答が「たまたま 1 件だけ返った」「gap を埋めきっていない」可能性を排除できない
  ため (v0.1 の判断を踏襲)。
- **`backfilling` 中の判定 (段 c)**: 毎 tick、(a) 各 pair の 1m が `gap_start` から
  `now` まで連続性ギャップ無く埋まっているか (§3.3 改訂: `INTERVAL_MIN` の固定格子では
  なく「各 bar の次の期待取引時刻」で検査し、`market_hours` の既知の閉場を除外する —
  r1 裁定 C7)、(b) 判断足が同様に埋まっているか、(c) 有効な plugin の
  `warmup_bars`/`max_bars` を満たすか (A2-1 registry の `insufficient_closed_bars`
  判定を再利用)、(d) **3.3 節の SL/TP replay が `replay_through` まで全 OPEN 建玉に
  対して完了しているか** を確認する。すべて満たせば `ready_streak += 1`、
  1 つでも欠ければ `ready_streak = 0` のまま留まる (ゼロに戻すのは「新しい欠落を検出した
  場合」のみ — 単に前 tick と同じ既知の欠落が続いているだけなら streak は増やさないが
  減らしもしない、という設計は複雑になるため今回は「1 つでも満たさなければ 0」の単純規則
  を採用し、flap 頻度は §9 の実測後に調整する)。
- **`backfilling → ready` (段 c、自動)**: `ready_streak >= datafeed.outage.ready_confirm_ticks`
  (既定 3、§7) かつ `pending_human_confirmation = 0` のとき。`gap_start` 行は削除せず
  epoch 付きで残す (監査用、`prune` 対象外)。
- **flap** (`backfilling` 中に再び失敗する、段 c): `state = degraded` に戻すが `epoch` は
  増やさず `gap_start` も書き換えない (同じ episode の続き)。`entered_degraded_at` も
  据え置く。これにより断続的な瞬断が epoch を無駄に進めたり、replay 対象の履歴を
  失ったりしない。
- **段 b の `ready` 復帰は手動 (r1 裁定「範囲」、§8 ★。安全条件は r2 裁定 C1 で全面改訂)**:
  段 b は連続性検査・replay を実装しないため、`degraded → backfilling → ready` の
  自動経路そのものが無い。人間が新設シェルコマンド (仮称 `data resume`、§3.4/§8) を
  叩いたときだけ `ready` に戻す。

  **v0.2 の前提条件は不十分だった (r2 裁定 C1、Critical)**: v0.2 は「全 hard 必須 key が
  直近 tick で succeeded」だけを前提条件にしていたが、これは資金保護にならない。反例:
  10:01 に停止、10:03 の未処理足で SL 到達、10:05 に復旧して全 hard key が succeeded に
  戻った状態で `data resume` を叩くと、段 b には replay が無いため scheduler は DB 末尾
  1 本 (`service.py:1098-1100`) しか見ず、10:03 の SL 到達は永久に判定されないまま新規
  mission が再開する。`data_outage_unprocessed_bars` activity (§3.1) は通知であって
  資金保護ではなく、「全 hard key が直近 tick で succeeded」は未処理建玉の解消を証明
  しない。OPEN 建玉がある限り、activity による通知だけでは安全側にならない (IV-3 参照)。

  **是正 (両方を必須にする)**: `data resume` は次の (1)(2) の**両方**を満たさなければ
  実行を拒否する。
  1. primary で絞った (I3 の `storage_source` 引数) 全 hard 必須 key の watermark が
     健全 (直近 tick で succeeded、かつ §3.1 の停滞式を満たさない)。
  2. **未処理の確定足がある `OPEN`/`PENDING_FILL` 建玉がゼロ**であること。ゼロでない
     場合は、対象建玉を人間が手動でクローズ済みであることを (2a) 確認するか、
     (2b) コマンドに `--acknowledge` を明示して「未処理の SL/TP を捨てて resume する」
     ことをその場で承認するかのどちらかを要求する。`--acknowledge` なしで (2) を
     満たさない場合、コマンドは拒否し理由 (pair・未処理と推定される足数) を表示する。

  **「未処理の確定足がある建玉」の判定 — 保守規則 (r3 裁定 C1、Critical)**: v0.3 の
  「`filled_at` (または `entered_degraded_at`) より後に確定した 1m が 1 本以上
  存在するか」という基準は、どちらを選んでも安全側にならない。反例: `gap_start=09:59`、
  `entered_degraded_at=10:01`、10:02 に復旧すると 10:00 の足は既に確定しているが、
  「`entered_degraded_at` より後」を基準にすると 10:00 足を数えず、OPEN を残したまま
  `--acknowledge` 無しで resume できてしまう。一方 `filled_at` を基準にすると、
  outage 突入前から存在した建玉については既に適用済みの古い足まで「未処理」に
  数えてしまう。根本原因は、段 b が建玉別の適用 cursor を持たないこと (`_processed_bar_ts`
  はプロセスメモリの pair 単位カーソルであり、再起動後は建玉別の適用済み範囲を証明
  できない、§1) — 「その建玉に未適用」という判定自体が段 b では成立しない。
  **是正 (保守規則)**: 段 b では replay/適用 cursor を持たないため、**episode 内
  (`next_expected_trading_time(gap_start)` 以降、現在の確定足まで) の対象足は、
  その pair の全 `OPEN` 建玉について無条件に未処理扱いとする**。各建玉の下限は
  `max(next_expected_trading_time(gap_start), filled_at)` (`filled_at` が
  `gap_start` より後なら、その建玉が存在し得た最初の足から数える。C5 の replay
  下限と同じ考え方)。`PENDING_FILL` は約定していないため `filled_at` を持たず、
  下限は `created_at` (`orders` 表の列、注文が作られた時刻) とする。この規則で
  「未処理の足がある建玉」の**件数**を数え、ゼロでなければ (2a)/(2b) の分岐に入る。
  段 c で建玉別の replay cursor (`replay_through`、§3.1 C3) が入った後は、この保守
  規則をより精密な適用済み判定に置き換えてよいが、段 b 単独ではこの保守規則が
  唯一の安全な近似である。

  **直列化 (r3 裁定 C2、Critical)**: `data resume` (`Commands`、`commands.py`) は
  scheduler の tick スレッドとは別スレッドから叩かれ得るが、`Commands` は
  `app.core_lock` を受け取らない (現物のまま)。一方 `_scheduler_tick_once`
  (`service.py:1224-1246`) は `ingest.prepare()`→`with core_lock: ingest.commit()`
  →(lock を抜けて再度) `with core_lock: scheduler.tick()` という**別々の lock 区間**
  で動く。この隙間で `data resume` が古い (まだ observe されていない) report・
  watermark を見て前提条件 (1)(2) を判定すると、直後の scheduler tick が新しい
  hard failure を observe して `degraded` に戻すはずだった状況を `ready` のまま
  すり抜けさせ得る。**是正**: `data resume` は state を直接書き換えない。実行時刻・
  `--acknowledge` の有無を「resume 要求」として episode に永続化する
  (`datafeed_outage_state.resume_requested_at` 等、§3.1 DDL に列を追加) だけに
  留める。次の scheduler tick が、その tick の `now`・`IngestTickReport`・
  watermark を使って `commit → observe → resume 判定 → scheduler.tick()` の
  **1 つの lock 区間**の中で resume 要求を消費し、前提条件 (1)(2) を評価してから
  `ready` へ遷移させる。`Commands` はこれ以降も `core_lock` を取得しない (現物の
  責務分担のまま) — 資金保護に関わる状態遷移は常に scheduler tick の lock 区間
  内でのみ行う、という既存の規律 (§3.1 判定者節) に resume 経路も揃える。

  叩いた時点では episode を閉じない (resume 要求を記録するのみ)。**次 tick で
  前提条件 (1)(2) が満たされたことを確認できて初めて** episode を閉じ、`epoch` は
  増やさず (次の失敗時にのみ +1)、`gap_start`/`entered_degraded_at` は監査用に残す。
  前提条件を満たさなければ resume 要求は破棄され、コマンド実行者には (別途、次回
  `status`/`log` 参照時に) 拒否理由が見える。段 c 実装後、このコマンドは
  「自動 `backfilling→ready` を待たずに人間が強制復帰させる」ためのエスケープハッチ
  として残る (自動化を置き換えない) が、上記 (1)(2) の前提条件と直列化は段 c 後も
  維持する。

  **runbook (段 b の運用、r2 裁定 C1)**: MT5 bridge 不通中に建玉があれば、`data resume`
  を叩く前に**建玉を手動でクローズしてから resume する**ことを既定の運用手順とする
  (§3.4/§8 のシェル操作ガイドに明記)。`--acknowledge` は緊急時の例外経路であり、
  「未処理の SL/TP を捨てる」ことを人間が明示的に選んだ場合にのみ使う。

### 3.2 停止の境界表

|対象|不通時の扱い|典拠・理由|
|---|---|---|
|新規判断 (cron) mission|**v0.1 の「watermark が進まないので追加実装不要」は r1 裁定 C1 で Critical と判定された誤り**: backfill 直後の tick では ingest が判断足の空白を一括で埋めるため watermark が進み、`state` がまだ `backfilling` でも `_trade_mission_due` は `"cron"` を返してしまう (`scheduler.py:270-316` は outage state を一切読まない)。**是正**: `_trade_mission_due` の最前段で `state == ready` を見る gate を追加し、`state != ready` の間は cursor (`_cron_watermarks`) を進めない (`_advance_cron_watermarks` を呼ばせない)。`ready` へ遷移した同 tick でのみ、進んだ watermark に対する B-1 の coalesce (最新 1 本だけ提案) を許す。段 b から有効 (段 b は手動 `ready` 復帰なので、復帰コマンド実行の同 tick が「`ready` 遷移の同 tick」に相当する)。|`scheduler.py:270-316,294-310`|
|signal 起動 mission|**v0.1 は TradeLoop 側の healthcheck 修正だけを挙げていたが、r1 裁定 C4 は Critical: それだけでは `_run_hooks` 内の `on_signal_maintenance` (`scheduler.py:240-266`) が `degraded` 中も毎 tick 走り続け、古い判断足から signal を新規生成し続ける**。「signal maintenance を止める」は mission 本体の skip だけでは満たさない。**是正 (2 段構え)**: (1) `_run_hooks` で **signal maintenance だけ** を outage gate する (news/econ/cache maintenance は継続、C4)。(2) `_trade_mission_due` 側でも `signal_due_fn` 判定の手前 (または `on_trade_mission` 呼び出し前) に `state == ready` を要求し、mission の submit 自体も止める (現行の緩い healthcheck 閾値、約140分、を単独の gate にしない)。|`scheduler.py:240-266,317-325`; `trade_loop.py:109-142`; `service.py:1103-1120`|
|ペーパー新規約定 (成行・指値到達)|`degraded`/`backfilling` 中は行わない。既存の `_fresh_bar` (5分鮮度) が `degraded` 中は自然に `None` を返すので大半は現物のまま止まる。**新規**: `backfilling` 中は 5 分以内の一見 fresh なバーがあっても新規約定を許可しない (replay が終わるまで「新しい」だけでは信用しない) — `_process_limit_fills` の呼び出し条件に state チェックを追加する。|`scheduler.py:963-982`|
|新規成行 open (Mission が出した TradeIntent の実行)|`degraded`/`backfilling` 中は `Executor._open` の手前で state を見て拒否する (`GateContext` を作る前に fail closed、理由 `"data source degraded"` を `reasons` に積む)。**これとは独立に**、quote 取得自体を primary-only にする (3.2 直下の「quote fallback の是正」参照) ので、fallback が明示されていなければ二重に守られる。|新設 (§4 IV-2)|
|既存建玉の SL/TP (`_process_exits`)|`fresh` (5分以内) な間は継続 (現物のまま)。5分を超えたら評価を**一時停止** (現物のまま、`_fresh_bar`→`None`→スキップ)。停止中に建玉は「監視されないが放置」ではなく、**復旧時に 3.3 の replay で一括事後判定**する。|`scheduler.py:377-385,1023-1069`|
|mark-to-market / HWM / equity snapshot|`_mark_to_market` は bar 陳腐化で snapshot を見送るだけで tick は継続する (現物のまま)。10 分で `current_account` が `None` になり、新規発注は fail closed になる (`_open` の account 不明分岐、`executor.py:494-495`)。drawdown kill switch の新規判定 (`risk_gate.evaluate` の kill switch 節) は open 評価時にしか走らないため、口座不明で open 自体が拒否されればこの判定も自然に走らない。**IV: 信頼できる価格が無い間は新規の drawdown 判定を行わない、を account-unknown fail closed で満たす** (r1 裁定)。|`scheduler.py:408-454`; `accounting.py:70-77`; `executor.py:494-495`|
|day 強制クローズ (`_force_close_day`)|quote 取得失敗時は `day_close_deferred` で次 tick に回す (現物のまま、架空価格で閉じない)。quote も primary-only 化により、不通中は必ずこの「延期」経路に入る (fallback で無言成立しない)。**r1 裁定 I2: v0.1 の「次 tick に回す」は市場が開いている間しか成立しない** — `tick()` は `market_hours.is_market_open(now)=False` の間、`_force_close_day` に到達する前に早期 return する (`scheduler.py:136-147` の `if not open_now: ... return pending`)。金 20:55 に quote が落ちて 21:00 を越すと、次に `_force_close_day` へ到達するのは日曜 21:00 の開場後になる。是正ではなく**記述の訂正**: 「outage 中の day close 再試行は市場が開いている間だけ行われ、閉場を跨いだ持ち越しは再開まで待つ」と明記する。閉場境界を跨いだままの持ち越しは、資金保護 (SL/TP) の対象であり続ける (5 分以内は継続、以降は 3.3 の replay で事後判定)。|`scheduler.py:136-147,211,907-941`|
|kill switch の既存 latch|解除しない。`Commands` の明示 `kill_switch_reset` コマンド (`commands.py:179-180`) だけが解除できる。現物のまま — 不通は latch に触れない。|`commands.py:179-180`|
|予約済み指値の失効・キャンセル (`_expire_limits`, `_cancel_all_pending`)|継続。不通中も期限切れ判定・account 不明時の全取消は行う (現物のまま、資金保護に必要)。|`scheduler.py:596-660,729-761`|
|reconcile (`_resolve_unknowns`)|継続。broker (paper) 呼び出し自体は不通の影響を受けない (PaperBroker は価格源と無関係な状態機械)。|`scheduler.py:475-524`|
|scheduler process 自体|継続。`_scheduler_tick_once` は ingest 例外を握って tick を必ず呼ぶ (現物のまま)。|`service.py:1224-1246`|
|人間向け shell/API (`Commands`)|継続。status 表示に data state を追加する (§7)。`Commands` は `core_lock` を取得しない (現物のまま) — `data resume` はこの場で state を直接書かず「resume 要求」を永続化するだけであり、実際の判定・遷移は次 scheduler tick の lock 区間で行う (§3.1 C2、直列化)。|新設|
|改善 loop (`ImproveSupervisor`)|継続。改善ループはバックテスト/plugin 編集が主で live price 取得が必須ではない (design-A.md 裁定済み、A2-3 の対象外)。|`design-A.md` §4 IV|
|news / econ 収集|継続。価格源と独立 (`_run_hooks` は try/finally で常に走る)。|`scheduler.py:236-249,258-282`|

**quote fallback の是正 (この節で実施する最小変更)**: `PriceProvider._chain` の
非 readonly 分岐 (`price_provider.py:140-157`) を、`d.mt5.enabled`/`td_key`/
`d.yfinance.enabled` の単純 OR ではなく、「`primary` を先頭に置き、`datafeed.fallbacks`
に明示された source だけを後続に追加する」形に変える。`fallbacks` 既定は空リストなので、
既定設定では quote chain は primary 1 本になる。これにより `_open`/day close/close retry の
quote 取得は ingest と同じ「primary only、明示 fallback だけ許可」の規律に揃う
(readonly 分岐は A2-1b で既にこの形になっている — writer 分岐だけが取り残されていた)。

### 3.3 復旧: backfill と SL/TP replay (段 c)

**復旧のトリガ**: `degraded → backfilling` (3.1)。ingest の `prepare()` は C2 (§3.1)
で戻り値に tick report を足す以外は変更しない。**段 c は `ingest.commit()` を呼ばない
(r3 裁定 C3、Critical、候補 A に確定)**: `commit()` は内部で `upsert_cache_bars`
(自前 commit、`ohlcv.py:148`) を呼ぶため、これをそのまま使うと OHLCV だけが
個別に commit され、後段の注文遷移・pair cursor・state の commit とずれる (下記
「OHLCV 側の commit も同じ問題を持つ」参照)。段 c の backfill は `prepare()` が
tick 内に取得した pending バーを pair/key 別に `upsert_cache_bars_nocommit`
(下記是正) で **nocommit** 書込みし、新設する
`replay_open_positions(conn, pairs, gap_start_by_pair, now)` による注文遷移・
pair cursor (`replay_through`)・state 更新とあわせて、**外側の 1 つの明示
`with conn:` transaction** でまとめて commit する (A2-1b の 1 回 range 取得が
そのまま空白全体を埋める、という前提自体は I5 により推測にとどまる — §9 で実測する)。

**トランザクションの是正 (r1 裁定 C6、Critical)**: v0.1 は「`core_lock` 保持下で
`ingest.commit()` と同じトランザクションに乗せる」としたが、これは誤りである。
`core_lock` は `threading.RLock` (`service.py:477`) であり SQLite のトランザクション
境界ではない。さらに `store/orders.py` の `insert`/`update_fields` はどちらも呼び出しの
最後で自前に `conn.commit()` する (`orders.py:17-29,37-45`) ため、`transitions.transition`
(`transitions.py:32-40`) が呼ばれるたびに個別の commit が起きる。この状態で「各 bar
1 回」の replay 中に 1 件の注文だけ `CLOSING`→`CLOSED` を commit したところでプロセスが
落ちると、state/gap の永続化 (候補 A、`datafeed_outage_state`/`_gap`。§3.1 で確定、
r3 裁定 C3) とはずれる。再起動後、その建玉は通常経路の `_retry_close` (現在 quote で
閉じる) に落ちてしまい、履歴時刻での事後判定という設計そのものが崩れる。

**OHLCV 側の commit も同じ問題を持つ (r2 裁定 C2、Critical)**: v0.2 は
「`ingest.commit()` 直後に」同じトランザクションで続ける、と書いていたが、
`upsert_cache_bars` (`store/ohlcv.py:125-148`、`conn.commit()` は 148 行目) は呼び出しの
中で既に自前 commit しており、`with conn:` はこの**内側で既に実行された commit を
rollback できない**。段 c の backfill は pair A の分が `upsert_cache_bars()` 内で
commit された直後、episode/replay 更新前に落ちると、DB の bar だけ進み state/cursor は
古いままになる (複数 pair・複数 key ならその都度 commit される)。「`ingest.commit()`
直後」という書き方では commit 自体を外側へ移せていない。**是正**: `upsert_cache_bars`
に commit しない版 (`upsert_cache_bars_nocommit`) を用意し、段 c の backfill はこちらを
使う。commit を ingest の外へ完全に移し、注文側の nocommit primitive と合わせて
「backfill・注文遷移・pair cursor・state」を**1 つの明示 transaction**にまとめる
(段 b の `ingest.commit()` は現行のままでよい理由は §3.1 判定者節を参照 — b は
replay しないため分断の害が無い)。

**是正 (注文側、r1 裁定 C6)**: (1) `store/orders.py` に commit しない primitive
(`update_fields_nocommit` 等) を追加し、replay と state/gap の更新はこの primitive
のみを使う。(2) 呼び出し側 (`replay_open_positions` 本体) が明示的に `with conn:`
(SQLite の暗黙トランザクション) で 1 pair・1 tick 分の backfill (nocommit) + 注文遷移
(nocommit) + pair cursor + state の更新全体を包む。(3) `datafeed_outage_gap` の
`(pair, interval, epoch)` 行の `replay_through` 列 (C3、§3.1 の DDL) に「この pair・
interval についてどこまで replay が終わったか」を同じ `with conn:` の中で書く。
再起動後は `next_expected_trading_time(replay_through)` から再開する —
「backfilling の続きから」であり、`_retry_close` の現在 quote 経路には絶対に落とさない
(この点は §5 AC-9 の逆変異で「replay_through を無視して `_retry_close` に落ちる」
実装、および「transaction 内でクラッシュしても注文と state が一緒に rollback されない」
実装を殺す)。

**backfill の範囲**: 3.1 (backfilling 判定) のとおり (a) 1m 実行本数、(b) 判断足、
(c) plugin warmup。1m は `gap_start` (degraded 突入直前の watermark、= recovery cursor)
から `now` まで連続であることを要求する — A2-1 の `RequirementPlanner` が計算する
「必要本数」だけでは**空白の途中に SL/TP イベントを見落とす**ため不十分であり、
backfill 完了条件としては「必要本数」と「gap_start からの連続性」の両方を満たす必要が
ある (前者は plugin/indicator 向け、後者は建玉保護向けで目的が異なる)。

**連続性検査の是正 (r1 裁定 C7、Critical)**: v0.1 は「`sources.INTERVAL_MIN["1m"]`
間隔で穴が無いか」という固定格子検査を提案したが、これは**市場の既知の閉場を
欠落と誤判定する**。金曜の outage が週末をまたぐと、金 21:00〜日 21:00 に 1m bar が
無いのは `market_hours.is_market_open` (`market_hours.py:25-40`) が示すとおり正常
だが、固定 1 分間隔の格子検査はこれを穴として扱い、`backfilling` から永久に抜けられ
ないか、あるいは常に `pending_human_confirmation` になる。**是正**: 連続性検査は
「前の bar の `bar_time + width` が次の bar の `bar_time` と一致するか」ではなく、
「前の bar の次に**期待される取引時刻**」(`market_hours.is_market_open` で閉場区間を
飛ばして計算する — 3.4 で導入する休場除外ロジックと共有する) と次の bar の `bar_time`
を比較する。既知の閉場で説明できる欠落は連続性違反に数えない。説明できない欠落
(3.4 の「未知の空白」) だけを `pending_human_confirmation` に送る。

**replay の手順** (`_process_exits` の直前、`core_lock` 保持下、`scheduler.tick()` の
先頭近く — 3.1 の状態機械判定と同じ `with conn:` に乗せる、上記 C6 参照):

1. `state` が `degraded→backfilling` の判定を今 tick で満たした pair について、
   `ohlcv.load_cache_bars(conn, pair, "1m", source=storage_source,
   since=next_expected_trading_time(replay_through) if replay_through else gap_start,
   until=now)` (`store/ohlcv.py:152-171`) で未処理区間の確定 1m を時系列順に取得する
   (`replay_through` が無ければ `gap_start` から。C3 参照)。**`backfilling` 状態が
   複数 tick にまたがる間は、この読み出しと下記 2〜3 を毎 tick 繰り返し
   `replay_through` を前進させる (C4、下記「replay → 通常 exit の handoff」参照)** —
   `degraded→backfilling` に入った tick 1 回だけで空白全体を処理し切れる保証は無い。
2. その pair の `OPEN` な注文それぞれに対し、**`max(gap_start の次の足, filled_at)`
   以降の bar だけを** 古い順に 1 本ずつ既存の `check_exit(row, bar, spread,
   entry_same_bar=False)` (`paper_fills.py:25-51`) に通す (r1 裁定 C5、Critical)。
   v0.1 は全 OPEN 建玉に `gap_start` から一律に流す設計だったが、これだと**障害検出
   猶予中に新規に作られた注文** (例: `entered_degraded_at` の直前〜直後に約定した
   注文) に対して、その注文が存在する前の bar を当ててしまい、「存在する前に決済
   される」という矛盾が起きる。`filled_at` (`orders` 表、`filled_at` 列) を必ず
   下限にする。`filled_at` と同一 bar の扱いは `check_exit` の既存規則
   (`entry_same_bar` 引数、同一バー内のエントリー成立と SL/TP 到達の順序は判定不能な
   ため TP は確定させない/SL はギャップ規則で確定させる) をそのまま使い、
   `replay_open_positions` は `entry_same_bar=True` を `filled_at` と同じ `bar_time`
   の bar にだけ渡す。
3. ヒットした時点でその建玉の replay を打ち切り、**新設 `Executor.close_order_at
   (row, event_time, price, reason)`** (r1 裁定 C5) でその bar の**時刻**を
   `closed_at` として確定する — 既存 `close_order` (`executor.py:860-871`) は
   `self.clock.now()` を使うため、11:47 復旧時に 10:15 の bar で SL を検出しても
   `closed_at=11:47` になり「10:15 に閉じるべきだった」という事後判定の意味が記録
   から失われる。`close_order_at` は `close_order` と同じ `transitions.transition`
   経路を使い、`now` の代わりに `event_time` (bar の `bar_time`) を渡す点だけが違う
   (spec_fn/resolve_close_rate は呼ばない — replay の price は bar から確定済み)。
   ヒットが無ければ次のバーへ進む。**SL 優先・gap 補正 (`bar.open` が SL/TP を
   越えていれば open 側へ滑らせる) は `check_exit` に既存であり、そのまま使う** —
   r1 裁定「既存の SL 優先・gap 規則で事後判定」に一致する。
4. `PENDING_FILL` (未約定指値) は replay 対象にしない。3.1 節の前提の検証で確認した
   とおり、account 不明化 (10 分) で既に取消されているのが通常経路であり、10 分未満の
   短い不通で pending が生き残っていた場合も「未観測の価格で新規に約定させない」
   (r1 裁定) ため、`try_submit`/`_process_limit_fills` は `backfilling` 完了 (`ready`)
   後、その時点の最新バーからだけ約定判定を再開する。これは「空白中の指値到達を
   遡って約定させない」という意図的な非対称であり、r1 裁定が禁じた「SL だけ遡る」
   非対称とは異なる (SL/TP は**既存の実弾ポジション**の保護、指値は**まだ存在しない
   仮想ポジション**の生成であり、後者を遡らせることは「未観測の価格イベントで新規
   エクスポージャを作る」に直接該当するため区別する)。
5. **cursor 前進はバッチの完了にのみ従属し、ヒットの有無・OPEN 件数には従属しない
   (r3 裁定 I1、Important)**: v0.3 までのこの手順は「1 本もヒットしなかった建玉」の
   場合しか書いておらず、pair 内の OPEN 建玉が**全件ヒットした**場合や、その pair に
   OPEN が**そもそもゼロ**の場合には `replay_through` を進める契機が存在しなかった
   (反例: 1 件だけの OPEN が 10:15 の bar で SL によりクローズすると、以降その pair
   では「ヒットしなかった建玉」が存在しなくなり、pair-level の `replay_through` は
   永久に進まない — 次 tick は OPEN ゼロのため更新契機がなく追いつけないまま残る)。
   **是正**: この手順 1 で読み込んだ `since`〜`now` の 1m バッチが (ヒットの有無に
   関わらず) 例外なく処理し切られた時点で、`_processed_bar_ts[pair]` と
   `datafeed_outage_gap` の `(pair, "1m", epoch)` 行の `replay_through` (C3/C6) を、
   OPEN の件数・ヒットの有無にかかわらず**この pair が読み込んだ最後の入力足**まで
   進める。全ヒット (この tick 中に全 OPEN が決済された)・OPEN ゼロ (この pair に
   建玉が無い)・一部生存 (一部だけ決済され残りは継続監視) の 3 ケースいずれでも
   同じ規則で cursor を進める (AC-1c、§5)。cursor が表すのは「pair の入力範囲が
   どこまで処理済みか」であり、個々の建玉の結果には従属しない。
6. replay は pair 単位で隔離する (1 建玉/1pair の例外が他 pair の replay や当 tick の
   通常保護処理を止めない — 既存の `_process_exits` の隔離規律 (`scheduler.py:1023-1069`)
   と同じパターン)。

**replay → 通常 exit の handoff (r2 裁定 C4、Critical)**: v0.2 は「`degraded→backfilling`
判定を満たした tick で 1 回 replay し、以降は通常の `_process_exits` (末尾 1 本だけ) へ
引き継ぐ」という書き方だったが、これは未処理足を飛ばす経路を残す。反例: 11:47 に
replay が始まり 11:45 まで完了したところで長時間処理のため次 tick が 11:50 になると、
ingest はその間に 11:46〜11:48 をまとめて DB へ入れるが、通常の `_process_exits`
(`scheduler.py:1023-1082`) は末尾 11:48 だけを見て 11:46/11:47 を落とす。また pair 単位で
replay 例外を隔離してその pair だけ通常 tick 経路へ進めると、古い足より先に最新足で
決済してしまい得る (連続性の所有者が replay と通常経路のどちらにも無い状態になる)。

**是正**: `state == backfilling` の間は、各 pair を永続 cursor
(`datafeed_outage_gap.replay_through`、無ければ `gap_start`) の次の期待取引時刻から
その tick 時点の最新確定足まで**毎 tick drain** する (1 tick で追いつかなければ次 tick
も続ける — 上記手順 1 の繰り返し)。ある pair がこの drain で最新確定足まで追いついた
時点で、その pair**だけ**を通常の `_process_exits` (末尾 1 本) 経路へ渡す。まだ
追いついていない pair は通常経路の対象から除外し続ける (`_process_exits` の走査対象
pair リストから外す、または replay 側の drain 結果だけを見て決済判定する) — 連続性の
所有者は常に replay 側であり、末尾 1 本の通常経路と drain 中の replay が同じ pair を
二重に (異なる範囲で) 判定することはない。replay に失敗した pair (例外・欠落検出で
`pending_human_confirmation` に入った pair、§3.3「空白が保持期間を超えた場合」参照) も
同様に通常の `_process_exits` から除外し続け、末尾 1 本だけを見て決済することを防ぐ。

**この drain/handoff 規律を専用の受入条件で反証可能にする (r3 裁定 I2、Important)**:
v0.3 まではこの規律を文章でのみ記述しており、既存 AC-1/AC-4/AC-9 はこれを殺す
逆変異 (11:47 に一部だけ replay し、11:50 の通常 tick では末尾 11:48 だけを処理する
実装や、失敗 pair を通常の `_process_exits` に混入させる実装) を通してしまい得た。
11:47→11:50 の時刻列を使う専用 AC-1d (§5) を新設する。

**backfill 中に判断を再開しない条件**: 3.1 の `backfilling → ready` 条件そのもの
(全 pair の replay 完了 + 本数充足 + `ready_streak` 消化)。判断 mission (cron/signal)
は `state == ready` になるまで起動しない (3.2 表)。

**空白が保持期間を超えた場合**: `datafeed_outage_gap.gap_start` は `prune_cache`
(`ohlcv.py:190-215`) の対象外 (別テーブル) だが、`gap_start` が指す 1m バー自体は
`ohlcv_cache` から prune され得る (既定 30 日保持、通常の不通ならまず起きない)。
`replay_open_positions` が `gap_start` からの読み出しで連続性の欠落
(prune による欠落、または元々ソース側に無かった欠落) を検出した場合、その建玉は
「replay 不能」として `state` を `ready` にせず `pending_human_confirmation=1` を立て、
`activity` に `outage_replay_incomplete` を pair・欠落区間つきで記録する。この場合の
自動復旧はしない — 保有ポジションの安全な扱いは人間判断に委ねる (「量は問題では
ないという実測はあるが、保持期間超えは想定外の長さの不通であり自動化しない」という
安全側の判断。これは自分で決めた点であり、8 節で裁定を仰ぐ)。

**bridge が flap する場合**: 3.1 のとおり `backfilling` 中の再失敗は `degraded` へ
戻すが epoch は維持する。`entered_degraded_at` から現在までの累計不通時間が
設定可能な上限 (§7 `datafeed.outage.max_flap_duration_min`、既定は未設定 = 無制限、
§8 未裁定) を超えたら `pending_human_confirmation=1` にする案もあるが、実測 (§9) 前に
既定値を置かない。

### 3.4 休場との区別 (段 c。既知閉場の除外規則は 3.3 の連続性検査と共有)

`ingest.prepare()` はそもそも `market_hours.is_market_open(now)=False` なら due を
立てない (`ingest.py:81-82`) ため、週末・12/25・1/1 の間は `degraded` に入らない
(現物のまま、変更不要)。この既知閉場の判定は 3.3 (r1 裁定 C7) の連続性検査が使う
「次の期待取引時刻」計算と同じロジックを共有する。

**未知の空白** (market_hours が知らない祝日・ブローカーメンテナンス等) は、
`is_market_open=True` の間に bridge が「例外」ではなく「200 だが 0 本」を返す形で
現れると**推測する** (§9 で実測が要る — 現状 bridge 不通・未ログイン時の応答は未測定)。
§3.1 で新設する `IngestTickReport.empty` (C2) がこれを表す —
`Ingest._fetch_primary` (`ingest.py:142-159`) の戻り値、または
`normalize_closed_range` (`closed_bars.py`、A2-1 spec §3.1) 通過後の結果が空リストの
場合、その key を `report.failed` (例外) ではなく `report.empty` に分類する。連続 N tick
(既定 3、判断足 1 本分をおおむねカバーする回数) `empty` が続いたら、`degraded` と
同じ扱いで判断 mission を止めつつ、`pending_human_confirmation=1` を立てて
`activity` に `outage_gap_needs_confirmation` (pair、空応答が始まった時刻、
`market_hours.is_market_open` が True であること) を記録する。この状態からの
`ready` 復帰は、人間が新設シェルコマンド (`confirm_data_gap`、案。設計名は未確定
— 実装 task で確定する。3.1 の段 b 用 `data resume` とは別コマンド — こちらは
「休場と説明できない空白を人間が承認した」ことだけを意味し、`pending_human_confirmation`
だけを落とす) を叩いて `pending_human_confirmation=0` に戻すか、実際に新しいバーが
届いて `degraded→backfilling→ready` の通常経路に合流するまで自動では進まない。

### 3.5 paper と本番 adapter の表の分離

**ユーザー裁定 (2026-09-24)**: 実口座 (MT5 発注 adapter、未配線) では建玉の正本はブローカーにあり、SL/TP はブローカー側で約定する。復旧時はこちらで replay せず、bridge の `/positions` と `/positions/{ticket}/closed-deal` で照合して DB をブローカーの結果に合わせる (reconcile)。paper の replay は「ブローカーが居ない環境でブローカー側 SL/TP を模倣する」代替であり、両者の結果が一致するよう SL/TP とも触れた足の時刻・価格で決済扱いにする。

現アプリの唯一の broker は `PaperBroker` (`src/agentic_fx/core/paper_broker.py`)。
MT5 への実発注は未配線であり、3.2〜3.3 の停止境界・replay 規則はすべて **paper 専用**
である。本番 MT5 adapter 導入時 (未実装、この節はその際の設計メモであり
**推測を含む**):

|観点|paper (本設計)|本番 MT5 adapter (未実装、推測)|
|---|---|---|
|SL/TP の実体|プロセス内 `check_exit` による事後判定 (このプロセスが不通だと保護が止まる)|broker (MT5 サーバ) 側の native stop order。プロセスが不通でも broker 側で執行され得る|
|不通中の建玉保護|3.3 の replay (バーを遡って事後判定)|broker 側で既に成立しているので、復旧後は「reconcile」(broker の実ポジション・約定履歴を問い合わせて DB と突き合わせる) が主体になる。replay ロジックの大半は不要になる可能性が高い (推測)|
|新規発注の停止|`Executor._open` 手前の state gate|同様の gate が要るが、broker 側の pending order (指値) が生き残っている場合の扱い (取消すか、broker に委ねるか) は adapter 設計時に別途裁定が要る|
|復旧確認|DB backfill + replay 完了|上記に加え broker への `/positions`・`/order` 照会が「復旧した」の定義に加わる可能性が高い|

この表は adapter 設計の出発点を残すためのものであり、A2-3 の実装対象ではない。

## 4. 不変条件

|ID|不変条件|
|---|---|
|IV-1|`degraded`/`backfilling` 中は新規判断 mission (cron/signal 双方、cron は cursor 停止つき・signal は maintenance 自体も含め §3.2 C1/C4)・新規ペーパー約定・新規成行 open を起動しない。判定は単一の永続状態機械を経由し、healthcheck の緩い閾値だけに頼らない。|
|IV-2|信頼できる価格が無い間 (account 不明・`degraded`/`backfilling`) は新規の drawdown/kill switch 判定を行わない。既存 latch は解除しない (§3.2)。|
|IV-3|不通中に「未観測の価格イベント」を作らない: 確定済で未処理の 1m バーは SL/TP を 1 回だけ判定し (replay、対象は `max(gap_start の次の足, filled_at)` 以降のみ — 存在前の bar を当てない、§3.3 C5)、`PENDING_FILL` は遡って約定させない。|
|IV-4|`ready → degraded` の停止 gate は**最初の** hard failure/期待 watermark の停滞で直ちに効く (§3.1 C3)。「2 回連続」は通知確定 (`confirmed`) にのみ使い、gate の条件にしない。`backfilling → ready` (段 c) は `ready_confirm_ticks` 回の連続健全を要求する (flap で無用に epoch を進めない・無用に ready を宣言しない)。|
|IV-5|`gap_start` (recovery cursor) は degraded 突入 tick の直前 watermark から不変であり、`backfilling` 中の flap で書き換わらない。段 c の replay はこの `gap_start` から `now` までの連続性を、既知の閉場を除外して検証してから `ready` に遷移する (§3.3 C7)。cron の watermark cursor (`_cron_watermarks`) は `state != ready` の間、進めない (§3.2 C1)。|
|IV-6|quote 取得 (成行 open・day close・close retry) は `datafeed.fallbacks` に明示された source だけを予備として使い、`enabled` フラグのみでの無言 fallback をしない (段 a)。|
|IV-7|市場が公式に休場 (`market_hours.is_market_open=False`) の間は `degraded` に入らない。休場と説明できない空白 (open 中の空応答継続) は自動で `ready` にせず人間確認を要求する (段 c)。|
|IV-8|状態機械の永続化はプロセス再起動をまたいで `gap_start`/`epoch`/`pending_human_confirmation`、および pair×interval×epoch ごとの `replay_through` (C3) を保持する。段 c の backfill・注文遷移・pair cursor・state の更新は 1 つの明示 SQLite transaction (`with conn:`、commit しない primitive のみ使用、C2/C6) にまとめる。段 b の `ingest.commit()` は現行のまま (別 transaction) でよい (§3.1 判定者節、C2)。永続の形は候補 A (DB 表 2 つ) に確定した — 候補 B (`StateStore`) は SQLite transaction に参加できないため退けた案 (§6、r3 裁定 C3)。|
|IV-9|(新設、C2) 状態機械は ingest の `last_errors`/`next_probe_at` の残留に依存しない。tick ごとの成否は真に immutable な `IngestTickReport` (attempted/succeeded/failed: frozenset/deferred/empty) から判定する (I1)。|
|IV-10|(新設、I3) outage 判定用の watermark 読み出しは常に `storage_source` (primary から導出) で絞る。別 source の残存行を健全の根拠にしない。|
|IV-11|(新設、r2 裁定 C1、r3 裁定 C1 で保守規則を追加) `OPEN`/`PENDING_FILL` 建玉が未処理の確定足を残したまま存在する限り、`data_outage_unprocessed_bars` 等の activity 通知だけでは安全側にならない。段 b の手動 `ready` 復帰 (`data resume`) は、全 hard 必須 key の watermark 健全性に加え、未処理足のある OPEN/PENDING_FILL がゼロであること (人間の手動クローズ確認) または `--acknowledge` による明示承認のどちらかを必須とする (§3.1)。段 b は建玉別の適用 cursor を持たないため、「未処理」の判定は**保守規則**による: episode 内 (`next_expected_trading_time(gap_start)` 以降) の対象足は、その pair の全 OPEN 建玉について無条件に未処理扱いとし、各建玉の下限は `max(next_expected_trading_time(gap_start), filled_at)`、`PENDING_FILL` の下限は `created_at` とする (§3.1)。|
|IV-12|(新設、r2 裁定 I3 実装注記) 1 tick の内部では `now` を 1 回だけ採取し、`prepare`/`commit`/`observe`/`replay`/`scheduler`/`healthcheck` の全ステップへ同じ値を引き回す。tick の途中で時刻源を再度読まない (§5/§11)。|
|IV-13|(新設、r3 裁定 I1) pair のバッチ (手順 1 で読み込んだ入力範囲) が例外なく処理し切られたら、`replay_through` は OPEN の件数・ヒットの有無にかかわらず最後の入力足まで進む。cursor の停滞が許されるのは「バッチがまだ処理し切られていない」場合だけであり、「ヒットが無かった/OPEN が無い」ことを理由に停滞してはならない (§3.3)。|
|IV-14|(新設、r3 裁定 C2) `data resume` は state を直接更新しない。実行時刻・`--acknowledge` の有無を episode に「resume 要求」として永続化するだけであり、実際の判定・遷移は次 scheduler tick の単一 lock 区間 (`commit → observe → resume 判定 → scheduler.tick()`、同一 `now`・`report`・`watermarks`) でのみ行う。`Commands` はこの経路でも `core_lock` を取得しない (§3.1)。|

## 5. 受入条件 (逆変異つき)

すべて fake transport (httpx を叩かない)・fake clock・tmp SQLite。bridge/pytest 実 DB・
実 service 起動はしない。時刻は具体値で pin する。**r1 裁定 I4: tick ごとに `now_utc`
を 1 回だけ採り、その 1 値を `prepare`/`observe`/`replay`/`scheduler` の全ステップへ
渡す** (tick の途中で `datetime.now()` を再度呼ばない — 実装・テストの両方で徹底する)。

**共通シナリオ (決定足 1h、grace 30秒、`ready_confirm_ticks=3`、tick 間隔 60 秒、
ticks は毎分 :00 に発火する前提)**: T=10:00:00Z 直前まで健全。1m watermark = 09:59、
1h watermark = 09:00。**v0.1 のこの節は I4 により 3 箇所の時刻が誤っていたため
全面的に引き直した** (以下、訂正箇所には † を付す)。

**T=10:01:00Z** bridge が `ConnectError` を返し始める (以降のリクエストは全て失敗)。
- **10:01:00 (この tick で 1m key の fetch が失敗、r1 裁定 C3)**: v0.1 は「2 回連続の
  失敗で degraded」としていたが、これは Critical (C3) — **最初の hard failure の
  その tick で直ちに** `ready → degraded` に遷移する。epoch=1、
  `entered_degraded_at=10:01:00`、`confirmed=0`、`gap_start[pair]="09:59"` (1m) /
  `"09:00"` (1h、1h key 自体は未 probe だが degraded 突入時点の watermark をそのまま
  記録) を記録。**この瞬間から** signal 起動 mission は `state != ready` により
  healthcheck 前段で skip される (§3.2 C4)。cron の watermark cursor も進行を止める
  (§3.2 C1)。
- **10:02:00 (2 tick 連続失敗)**: `confirmed=1` に昇格。`data_outage_degraded`
  activity が (この時点で初めて) 出る。gate の効き方はここでは変わらない (tick 1 から
  既に止まっている) — `confirmed` は通知の重み付けだけ (§3.1)。
- **10:04:00† (09:59 bar の年齢がちょうど 5 分)**: `_fresh_bar` の条件は
  `now - bar.ts > 5分` (厳密不等号) であり、5:00 ちょうどはこれを満たさない — **この
  tick ではまだ fresh** (v0.1 は「10:04:00 に stale 判定を始める」としており誤り)。
- **10:05:00† (09:59 bar の年齢が 6 分)**: ここで初めて `> 5分` を満たし stale になる。
  `closed_bar_unavailable` activity が OPEN/PENDING がある pair に 1 回だけ出る
  (既存 A2-1 挙動、時刻だけ訂正)。
- 10:11:00 (最新 snapshot 10:00 + 10分): `current_account` が `None` →
  `_cancel_all_pending` が PENDING_FILL を全取消 (既存挙動)。

**T'=11:47:00Z** bridge 復旧。ingest が `start=09:58, end=11:47` の 1m range と
`start=08:00, end=11:47` の 1h range を 1 回ずつ取得し成功 (I5: この 1 回取得で空白
全体が返る前提は**推測**、§9 参照)。
- 11:47:00: 全 hard 必須 key が succeeded → `degraded → backfilling`。同一 `with conn:`
  (C6) で `replay_open_positions` が 09:59〜11:45 の 1m (下記 † 参照) を古い順に
  `max(gap_start の次の足, filled_at)` 以降だけ OPEN 建玉へ通す (C5)。10:15 の bar で
  SL 到達を検出した建玉は `close_order_at` で `closed_at=10:15` として `CLOSED` になる
  (以降の bar は判定しない)。ヒットしなかった建玉は `_processed_bar_ts` と
  `replay_through` が読み込んだ最後の bar に進む。
- **†11:47:00 時点で読み込める「確定した」最新 1m は 11:45 (11:46 ではない)**:
  `latest_closed_cache_bar_time` の cutoff は `now - width - grace` =
  `11:47:00 - 1分 - 30秒 = 11:45:30`。`bar_time <= 11:45:30` を満たす最新の 1m は
  `11:45:00` (`11:46:00` は cutoff を超える)。v0.1 の「11:46 に進む」は誤り。
- **†11:47:00 時点で確定している最新の 1h は 10:00 (11:00 ではない)**: cutoff =
  `11:47:00 - 60分 - 30秒 = 10:46:30`。`bar_time <= 10:46:30` を満たす最新の 1h は
  `10:00:00` — `11:00` の足は `11:00:00 + 60分 + 30秒 = 12:00:30` まで確定しない。
  v0.1 の「11:00 の 1h 足は backfill 済み」は誤り (backfill 自体は 11:00 の途中まで
  行くが「確定足」としては未成立)。
- 11:47:00〜11:49:00 (3 tick 連続で全 hard 要求が健全): `ready_streak` が 1→2→3。
- **T''=11:49:00**: `backfilling → ready`。`pending_human_confirmation=0` であること。
- **†T'''=11:49:00 の同一 tick 以降**: cron watermark 判定・signal maintenance/mission
  が再び機能する。上記のとおり 11:49:00 時点で確定している最新 1h は依然として
  **10:00** (11:00 の足はまだ確定前) なので、cron watermark は `09:00 → 10:00` へ
  1 段だけ進み mission が 1 回起動する。`11:00` への到達は `12:00:30` 以降の tick で
  別途 due になる (これは B-1 の通常挙動であり A2-3 の追加事項ではない)。v0.1 の
  「09:00→11:00 へ進み coalesce される」は上記の確定時刻の誤りに基づく誤記であり、
  正しくは 1 段ずつ進む。

|ID|AC|逆変異|殺すテストの形|
|---|---|---|---|
|AC-1|`gap_start` の次の足〜`replay_through` の確定 1m が OPEN 建玉の exit 判定に**古い順で 1 回ずつ**かかる (`filled_at` より前は当てない、C5)。SL/TP どちらもヒットしない bar は何も起こさない。|末尾 1 本 (`rows[-1]`) だけを判定する現行のまま (未修正)。|10:15 に SL、10:40 に TP と読める fake 1m 列で、末尾だけ見る実装は「保護されなかった」を再現し、修正後は 10:15 の SL で `close_order_at(event_time=10:15,...)` により確定させることを assert する。|
|AC-1b|(新設、C5) 障害検出猶予中 (`entered_degraded_at` 直後) に約定した注文には、その `filled_at` より前の bar を当てない。|`gap_start` から一律に replay し、存在前の bar で決済してしまう。|`gap_start=09:59`, ある注文の `filled_at=10:20` の fake で、10:05 に SL を示す bar があっても replay がその注文をヒットさせず、10:20 以降の bar だけを見ることを assert。|
|AC-1c|(新設、r3 裁定 I1) pair のバッチが例外なく処理された時点で、OPEN の件数・ヒットの有無にかかわらず `replay_through` が最後の入力足まで進む (全ヒット・OPEN ゼロ・一部生存の 3 ケース)。|cursor 前進をヒットの有無に従属させ、全ヒットや OPEN ゼロの pair で `replay_through` が進まないまま停滞する。|3 つの pair (A: 唯一の OPEN が this tick 中に SL でヒットし全件決済/B: OPEN がそもそも無い/C: 2 件の OPEN のうち 1 件だけヒットし残り 1 件は継続監視) を含む fake 1m 列で、いずれの pair でも `replay_through` がその tick で読み込んだ最後の入力足まで進んでいることを assert する。|
|AC-1d|(新設、r3 裁定 I2、drain/handoff 専用) `state==backfilling` の間、各 pair は永続 cursor から毎 tick drain され、追いついた pair だけが通常の `_process_exits` へ渡る。失敗 pair (`pending_human_confirmation` に入った pair) は通常経路から除外され続ける。|11:47 に一部だけ replay し、11:50 の通常 tick では末尾 11:48 だけを処理して 11:46/11:47 を落とす。または失敗 pair を通常の `_process_exits` に混入させる。|11:47 tick で `replay_through` が 11:45 まで進み、11:50 tick までに ingest が 11:46/11:47/11:48 を取得している時刻列の fake で、11:46/11:47/11:48 の 3 本が drain で順に 1 回ずつ処理され、この pair が通常の `_process_exits` へ渡るのは 11:48 まで追いついた後の tick でだけであることを assert する。同じ列に別途「失敗して `pending_human_confirmation=1` になった pair」を 1 つ含め、その pair に対する通常 `_process_exits` の呼び出し回数が (drain が続く間) 常に 0 回であることも assert する。|
|AC-2|`ready → degraded` は最初の hard failure/期待 watermark の停滞で**直ちに**発生する (単発の `budget_exhausted` 単独では発生しない)。「2 回連続」は `confirmed` (通知専用) にのみ影響し、gate 判定を遅らせない (C3)。|1 回目の失敗を無視し 2 回連続を待ってから degraded にする、または `confirmed` を gate 条件に混ぜる。|prepare が 1 tick だけ hard key の例外を返す fake で、その **同じ tick** から cron cursor 停止・signal skip が効き、`confirmed=0` のまま `state=degraded` であることを assert。budget_exhausted のみの 1 tick では `state=ready` のままであることも assert。|
|AC-2b|(新設、C1) `state != ready` の間、`_trade_mission_due` の cron cursor (`_cron_watermarks`) は進まない。`ready` へ遷移した同一 tick でのみ、進んだ watermark に対する coalesce (最新 1 本) を許す。|`backfilling` 中でも watermark が進めば cron mission を submit してしまう。|`state=backfilling` の tick で判断足 watermark が 2 段進む fake に対し、mission が submit されないこと、`ready` 遷移後の同 tick で 1 回だけ submit されることを assert。|
|AC-2c|(新設、r2 裁定 I2) 停滞式は `expected = watermark + 2×足幅 + grace` の 1 つのみで、二重に猶予を加算しない。共通シナリオ (毎分 :00 tick、`grace=30秒`) の実時刻: 1m `watermark=09:59` は `10:02:00` の tick で初めて停滞と判定 (`10:01:30` を過ぎた最初の tick)。1h `watermark=09:00` は `11:01:00` の tick で初めて停滞と判定 (`11:00:30` を過ぎた最初の tick)。|`grace` に加え `budget` 等の追加猶予を足し込み、1m は `10:03:00` まで、1h は `11:02:00` まで停滞と判定しない (二重加算)。|1m `watermark=09:59` 固定・毎分 tick が進む fake clock で、`10:01:00` の tick では `state=ready` のまま、`10:02:00` の tick で初めて `state=degraded` になることを assert。1h `watermark=09:00` でも同様に `11:00:00` の tick は `ready`、`11:01:00` の tick で `degraded` になることを assert。|
|AC-3|`degraded`/`backfilling` 中は signal maintenance (`on_signal_maintenance`) 自体が `_run_hooks` の時点で skip され、signal 起動 mission も submit されない (140 分の緩い healthcheck 閾値を待たない、C4)。|`TradeLoop` 側の healthcheck だけを直し、`on_signal_maintenance` は毎 tick 走り続ける (v0.1 の実装漏れ)。|`state=degraded` かつ最新確定足が 1 分前 (=既存 healthcheck 的には健全) の fake で、`on_signal_maintenance` の呼び出し回数が 0 であること、signal mission が起動しないことの両方を assert。|
|AC-4|`backfilling` は `ready_confirm_ticks` 回連続で全 hard 要求充足を確認してから `ready` になる。1 つでも欠ければ `ready_streak` は 0 に戻る (§3.1)。|1 回成功で `ready`。または「成功→欠落」で単に足踏み (streak 維持) して停止し、streak が 0 に戻ることを検証しない。|**(r2 裁定 I5 で強化)** `success → missing → success → success` の 4 tick fake 列 (1 回目全 hard 要求成功、2 回目に一部 pair 欠落、3・4 回目は再び全成功) で、(1) 2 回目終了時点で `ready_streak == 0` であることを**直接 assert** する、(2) `ready_confirm_ticks=3` の設定で 4 tick 目終了時点でも `ready` になっていない (3 回連続成功が要るため 5 tick 目まで待つ) ことを assert する — 「成功→欠落」で終わるだけの列では streak を据え置く実装 (0 に戻さない実装) を殺せないため、`ready_streak` の値そのものを検証点に含める。|
|AC-5|`backfilling` 中の再失敗 (flap) は `degraded` に戻すが `epoch`/`gap_start` を維持する。|flap のたびに `epoch` 加算・`gap_start` 更新。|success→fail→success の fake 列で `epoch` が 1 のまま、`gap_start` が最初の値のまま、最終的な replay 範囲が最初の `gap_start` からであることを assert。|
|AC-6|`PENDING_FILL` は replay で遡って約定しない。account 不明で既取消された指値も、10 分未満で生存していた指値も、`ready` 後の最新バーからだけ評価される。|replay 中に指値の到達判定も行う。|不通中に到達し得た指値価格を含む fake 1m 列で、replay 後もその指値が `PENDING_FILL` のまま (または account 不明で既に `CANCELLED`) であり、`ready` 後の新しいバーでのみ約定判定が走ることを assert。|
|AC-7|quote/bars 取得 (成行 open・day close・close retry の quote、および writer chain の bars 取得) は `datafeed.fallbacks` が空なら primary 1 本のみを試し、失敗を fallback で隠さない (段 a、state 機械に非依存)。|`yfinance.enabled=true` だけで primary 障害時に quote/bars のどちらか一方だけ無言成功する。|`mt5.enabled=true, yfinance.enabled=true, fallbacks=[]` の設定 fake で、MT5 quote 失敗時に成行 open が `DataUnhealthy`/拒否になり、yfinance への quote リクエストが 0 回であることを assert。**(r2 裁定 I5 で強化)** `_chain` は quote/bars を同じ非 readonly 分岐 (`price_provider.py:117-157`) で組み立てるため、**writer 側の `get_bars` 呼び出しについても**同じ fake 設定で MT5 bars 失敗時に yfinance への bars リクエストが 0 回であることを別途 assert する — quote だけを検証して bars 側の暗黙 fallback を見逃す逆変異を殺す。|
|AC-7b|(新設、I2) 市場閉場中は day 強制クローズの再試行自体が (`tick()` の早期 return により) 走らない。開場中だけ `day_close_deferred` の再試行が効く。|閉場中も再試行が走る、または閉場を跨いだ持ち越しを「次 tick で再試行される」と誤って扱う。|金 20:55 に quote 障害発生、21:00 で閉場する fake clock で、21:00〜日 21:00 の間 `_force_close_day` 呼び出しが 0 回であり、日 21:00 の開場後 tick で再試行が再開することを assert。|
|AC-8|`market_hours` が休場の間は `degraded` に入らない。開場中の連続空応答 (例外でなく 0 本、`IngestTickReport.empty`) は `outage_gap_needs_confirmation` を立て、自動では `ready` にしない。連続性検査 (C7) は既知の閉場を欠落と誤判定しない。|週末に `degraded` を出す、または開場中の空応答を無条件 `degraded` (人間確認なし) で処理する、または金〜日をまたぐ replay 対象区間を欠落として `pending_human_confirmation` にしてしまう。|週末 fake clock で `state` 不変を assert。開場中に N tick 連続で 0 本を返す fake で `pending_human_confirmation=1` になり、次バーが実際に届くまで `ready` にならないことを assert。金曜 outage が週末をまたぐ fake 1m 列 (週末区間に bar が無い) で、連続性検査が違反を検出せず `backfilling→ready` へ正常に進むことを assert (C7 の逆変異はこれが主眼)。|
|AC-8c|(新設、r2 裁定 I4) 既知閉場のうち 12/25・1/1 の取引日境界 (`market_hours.py:21-40`、21:00 UTC 起点の取引日ラベル) を、週末の境界と同じ規律 (連続性検査・`is_market_open` の両方) で 1m・1h の双方について扱う。|週末だけ skip し 12/25・1/1 を通常取引時刻として扱う実装でも、週末だけを対象にした AC は通過してしまう。|12/24 21:00 UTC 閉場〜12/25 21:00 UTC 開場 (取引日 12/25 が休場) の fake clock/1m 列で `is_market_open` が期間中 `False` を返し `degraded` に入らないこと、および 12/24 20:59 の次に**期待される取引時刻**が `12/25 21:00` と計算されることを 1m・1h 双方で assert。同様に 12/31 20:59 の次の期待取引時刻が `1/1 21:00` であることを assert。連続性検査 (C7) がこの年末年始の閉場をまたぐ outage で `backfilling→ready` へ正常に進むことも assert する (週末だけでなく祝日をまたぐケースを既存 AC-8 に追加する形)。|
|AC-9|状態機械はプロセス再起動をまたいで `epoch`/`gap_start`/`pending_human_confirmation`、pair 別 `replay_through` を保持し、再起動後は `replay_through` から replay を再開する (`_retry_close` の現在 quote 経路に落ちない、C6)。段 c の 1 つの明示 transaction (backfill・注文遷移・pair cursor・state、C2) はクラッシュ時に全体が rollback される。|メモリのみで再起動後に空白開始点を失う、または再起動後の該当注文が通常の `_retry_close` (現在 quote) で閉じてしまう、または「commit 後の再起動」だけを模して transaction 内部の未commitクラッシュを検証しない。|`degraded`→`backfilling` の途中、1 件の注文だけ `close_order_at` が commit された直後にプロセスを模した再構築 (新しい `Ingest`/`Scheduler` インスタンス、同じ conn) を行い、`replay_through` が保持されたまま残りの建玉の replay が正しい範囲で継続し、`_retry_close` が一切呼ばれないことを assert。**(r2 裁定 I5 で強化) transaction 内クラッシュの fault injection**: `with conn:` の内部で「注文 UPDATE (nocommit) は実行済みだが `replay_through`/state の書込みはまだ、かつ transaction commit 前」の時点で例外を注入し、conn を再オープンして (1) その注文の UPDATE が rollback されている (commit 前の状態に戻っている) こと、(2) `replay_through`/state も同時に更新前の値のままであること、の両方を assert する — 「commit 直後」だけを模した従来のクラッシュ注入では、nocommit primitive が正しく機能していない (個別 commit してしまっている) 実装を殺せないため、commit **前**への fault injection を独立した検証点として追加する。|
|AC-10|保持期間超えで `gap_start` の 1m が prune 済みの場合、replay は連続性欠落 (既知の閉場では説明できない欠落、C7) を検出し `ready` へ進まず `pending_human_confirmation=1` にする。|欠落を無視して部分 replay のまま `ready` にする、または既知の閉場による欠落まで人間確認にしてしまう。|`gap_start` 以降の一部区間 (閉場では説明できない) が cache に無い fake DB で、`state` が `backfilling` のまま人間確認待ちになることを assert。|
|AC-11|kill switch の既存 latch は不通中も解除されず、`Commands.kill_switch_reset` 以外の経路では変化しない。|`degraded→ready` 遷移が latch を触る。|latch 済み state で全遷移を通し、`kill_switch_latched` が不変であることを assert。|
|AC-12|(新設、C2) `Ingest.prepare()` が返す tick report は `last_errors` の残留に影響されない: 一度失敗した key が次 tick で succeeded になれば、その tick の `report.succeeded` に含まれる。|`last_errors` の残留を理由に `degraded` から抜けられない。|1 tick 目で key 失敗・2 tick 目で同じ key が成功する fake で、2 tick 目の `report.failed` が空であり、`report.succeeded` にその key が含まれることを assert。|
|AC-13|(新設、I3) outage 判定の watermark 読み出しは `storage_source` で絞る。primary が止まっていても別 source の新しい行を健全の根拠にしない。|`source` 条件無しで watermark を読み、別 source の残存行で誤って `ready` 相当に判定する。|同じ `(pair, interval)` に `storage_source="mt5-live"` の古い行と `source="yfinance"` の新しい行が混在する fake DB で、`storage_source` を渡した読み出しが古い方 (停滞) を返し、`degraded` 判定が正しく効くことを assert。|
|AC-14|(新設、r3 裁定 C1、b 専用) `data resume` の「未処理の確定足がある建玉」判定は保守規則 (episode 内の対象足は pair の全 OPEN について無条件に未処理扱い、下限 `max(next_expected_trading_time(gap_start), filled_at)`、`PENDING_FILL` は `created_at`) に従う。|`filled_at` のみ、または `entered_degraded_at` のみを基準にして「未処理」を判定し、いずれかのケースで実際には未処理の SL/TP が残る建玉を「処理済み」と誤判定して `--acknowledge` なしで resume を許してしまう。|`gap_start=09:59`, `entered_degraded_at=10:01`, 10:02 復旧の fake で、10:00 の bar が既に確定しているにもかかわらず `entered_degraded_at` 基準の実装は 10:00 足を数えず resume を許してしまう、というケースを含め、保守規則 (`max(next_expected_trading_time(gap_start), filled_at)`) を使う実装だけが「未処理の足が 1 件以上ある」と判定し resume を拒否することを assert する。同じ fake に `PENDING_FILL` (下限 `created_at`) を 1 件加え、その下限が `filled_at` ではなく `created_at` で評価されることも assert する。|
|AC-15|(新設、r3 裁定 C2、b 専用) `data resume` は state を直接書かず「resume 要求」を永続化し、次 tick の `commit → observe → resume 判定 → scheduler.tick()` という 1 つの lock 区間で、同一 `now`・`report`・`watermarks` を使って消費される。`Commands` は `core_lock` を取得しない。|`data resume` が別スレッドから直接 `state=ready` を書き込み、scheduler tick の lock 区間と競合する。|scheduler が tick 内で hard failure を observe し `degraded` に遷移させたのと同じ実行順序で、別スレッドの `data resume` 呼び出しが (要求の永続化のみ行い) その tick 内では `state` を変えないこと、かつ次 tick の `observe` 呼び出しの中で resume 要求が消費され前提条件 (1)(2) を満たさなければ `state` が `degraded` のままであることを assert する。前提条件を満たす別ケースでは、次 tick の 1 回の `observe` 呼び出し内で `ready` へ遷移し `resume_requested_at` がクリアされることも assert する。|

## 6. 退けた案

|案|理由|
|---|---|
|healthcheck の閾値 (`freshness_max_min + 2×足幅 + grace`) だけで signal mission も止める|cron と signal で二つの異なる基準が併存し、閾値の意味 (「DB 健全性の緩い上限」) と「不通の判定」(厳しい停止条件) を混同する。専用の状態機械を切り出す方が、cron の即時停止と一貫した挙動になる。|
|不通中も最後の 1m を「凍結値」として SL/TP に使い続ける (前束下書きの grace 継続案)|現物は 5 分を超えると評価そのものを止める (fail closed) 設計が既にあり、これは r1 裁定「未観測の価格イベントを作らない」の精神と整合する。凍結値での継続は、5 分を超えて時間が経つほど根拠薄弱な判定を続けることになり、退ける。|
|末尾 1 本だけでなく毎 tick 全期間を re-scan する (恒常的な全件 replay)|平常時は無駄な DB 読出しと CPU を毎 tick 発生させる。replay は `degraded→backfilling` 遷移時だけの特別処理とし、平常時は現行どおり末尾 1 本で十分 (通常運転では tick 間隔 60 秒 > 1m 足幅なので取りこぼしが無い)。|
|`ready→degraded` に 2 回連続失敗を要求する (v0.1 案)|r1 裁定 C3 (Critical): 中間足の SL/TP を永久に落とし、signal mission も同じ猶予中に古いデータで起動できる。最初の hard failure で直ちに停止するよう改めた (§3.1)。|
|`backfilling → ready` を 1 回成功で確定する|bridge が瞬間的に 1 回だけ応答して再び落ちる (flap) ケースで、判断 mission が古いデータのまま再開してしまう。3 回連続 (既定) を要求する。|
|休場でない空白も含め、市場が空応答を返したら常に自動 `ready`|「祝日は 12/25・1/1 だけ」という `market_hours` の既知の不完全性 (r1 裁定/task 前提) を放置すると、未知の休場を不通と誤認し続けるか、逆に不通を休場と誤認して保護なしで放置するかのどちらかになる。人間確認を挟む。|
|quote fallback の是正を A2-3 の範囲外 (A2-2 送り) にする|A2-2 の主題は要求予算/coalescing (取得回数・レート制限) であり、「不通時に無言で別ソースへ切り替わる」ことの是正は本ticketの核心 (「fallbackは利用者が明示したときだけ」) そのものである。A2-1b で ingest 側は既に primary-only 化されており、quote 側だけ残すと同じ設計原則が経路によって食い違う状態が続く。範囲に含めた。|
|本番 MT5 adapter の SL/TP replay まで本設計で確定する|adapter 自体が未実装であり、broker 側 stop order の有無で保護責務の配分が根本的に変わる。3.5 に設計メモを残すに留め、確定は adapter 設計時に送る。|
|永続を `StateStore` の 1 キー (候補 B、`AppState.episode`) にする|r3 裁定 C3 (Critical)。v0.1 は state.json 側を「`_REQUIRED_KEYS` の完全一致検査で壊れる」という誤った理由で退けていたが、実際は追加キーに寛容 (§3.1 で訂正済み) であり、その点では成立し得た。しかし `state.json` は `ingest.commit()` と別ファイルであり、SQLite の transaction に原理的に参加できない — c 段が要求する「backfill・注文遷移・pair cursor・state を 1 つの transaction にまとめる」(r1 裁定 C6) を満たせない。候補 A (DB 表 2 つ) に確定し、候補 B は退ける。|

**旧: 退けたのではなく未決 (r1 裁定 I6) → r3 裁定 C3 で確定**: v0.1〜v0.3 は「永続を
DB 表 2 つ (候補 A) にするか `StateStore` の 1 キー (候補 B) にするか」を b 段 spike
(T0) 送りの未決事項としていた。r3 レビュー (Critical C3) は、候補 B が `state.json`
という別ファイルであるため `ingest.commit()` と同じ SQLite トランザクションに
そもそも参加できない (spike で確かめるまでもない) ことを指摘し、候補 A に確定
させた。上表の行として記録する。T0 spike の役割は候補選択から、候補 A のトランザクション
境界が実際に crash-safe かの確認 (§3.1/§9) へ縮小した。

## 7. 設定キー

```yaml
datafeed:
  # 既定は空 — MT5 primary で bridge 不通なら fail closed。明示したソース名だけが
  # quote/新規発注の予備になる (ingest の DB 取得経路は A2-1b から primary-only)。
  fallbacks: []
  outage:
    ready_confirm_ticks: 3       # backfilling → ready に必要な連続健全 tick 数
    # max_flap_duration_min は §9 実測後に既定値を置く (未裁定)
```

`config/settings.yaml.example` にも同期する。既存 `settings.yaml` は無改変で起動でき、
`fallbacks` 未記載は空リストとして正規化する (primary-only、現行の暗黙 fallback とは
互換が変わる — 移行時に `yfinance.enabled=true` かつ `mt5.enabled=true` の設定は
deprecation warning を 1 回出し、明示的な `fallbacks: [yfinance]` を促す)。

**状態の可視化**: `Commands` の `status` 出力 (`build_splash`、`service.py:1178-1195`
相当) に 1 行追加する。

```text
data: DEGRADED (epoch 3, since 10:05 UTC, gap 1h46m) — 判断 Mission 停止中
```

活動ログ (`Category.SYSTEM`) に新設するイベント名: `data_outage_degraded` (`confirmed=1`
昇格時、段 b) / `data_outage_unprocessed_bars` (episode 継続中、未処理足数つき、C3、
段 b) / `data_outage_backfilling` / `data_outage_ready` / `data_outage_replay_hit`
(pair・価格・kind=sl/tp、段 c) / `data_outage_replay_incomplete` /
`data_outage_gap_needs_confirmation` (段 c)。

## 8. 人間の裁定が要る点

**裁定済み (`user-verdicts.md`/`r1-verdicts-a23.md`、蒸し返さない)**:
- MT5 不通 = 稼働停止。fallback は利用者が明示したときだけ。
- 復旧時に空白を取り直す (backfill)。
- 既存建玉の SL/TP 監視は最後に健全な 1m cache で継続する (本設計では「5分は継続、
  以降は一時停止して復旧時 replay」という精密化を提案 — 3.2/8 未裁定 2 参照)。
- 「古い bar で SL だけ」の非対称は採らない。未観測の価格イベントを作らない。
- kill switch 不変条件 2 つ (latch 非解除・信頼できる価格が無い間は新規 drawdown 判定なし)。
- paper 専用、本番 adapter は表を分離。
- 休場と説明できない空白は自動 ready にせず人間確認へ。

**未裁定 (実装開始前に選ぶ)**:
1. PENDING_FILL の自動取消 (account 不明 10 分) を現行のまま維持するか、`price_unavailable`
   で保持し復旧後に再評価する方式へ変えるか。本設計は前者 (現行維持) を前提にした —
   後者は「未観測の価格で新規約定させない」という r1 裁定と両立しにくい (保持した
   pending をどう扱うかという新しい非対称を生む) ため。
2. §3.2 の「5分で評価停止、復旧時に一括 replay」という精密化を、ユーザー裁定
   「最後に健全な1m cacheで継続」の**具体化**として採用してよいか。文字どおりの
   「凍結値で継続」とは異なる。
3. `ready_confirm_ticks` の既定値 3 (=3分、1h判断足に対して十分小さい)。実測 (§9) 前の
   暫定値。
4. 保持期間超えの空白 (§3.3) を「人間確認必須・自動復旧なし」にする設計判断。
   自分で決めた点であり、ユーザー確認が要る。
5. `datafeed.outage.max_flap_duration_min` を設けて長時間の flap を強制的に人間確認へ
   送るか。値と要否は §9 実測後。
6. 未知の空白 (§3.4) を確認する具体的な操作 (シェルコマンド名・確認後の遷移) の UX。
   本設計は `confirm_data_gap` という仮称のみ置いた。

**ユーザー裁定済 (2026-09-24 22:30 JST、`tmp/design-a23/user-verdicts.md`)**: 以下の 7・8 を承認。あわせて (i) replay の SL/TP は両方とも「実口座と同じ結果」= 触れた足の時刻・価格で決済扱い (SL は復旧時に価格が戻っていても損切り。決済理由 `outage_replay_sl` / `outage_replay_tp` と復旧時価格との差を activity に残す。設定での切替は入れない) (ii) 実口座 (MT5 発注、未配線) では replay ではなく reconcile (建玉の正本はブローカー。復旧時に bridge の `/positions` と `/positions/{ticket}/closed-deal` で照合し DB をブローカーに合わせる。§3.5 の本番 adapter の表に明記)。

**(旧) ★ ユーザー確認待ち → 承認済**:
7. **3 段分割** (§2): 1 本の spec に 3 ticket (A2-3a quote-primary-only hotfix / A2-3b
   停止 gate + 観測 + 永続 episode / A2-3c backfill 連続性 + paper replay + 自動 ready)
   を書き、単独で main に入る順に実装する、という進め方そのものの承認。
   **r2 裁定 (進め方 ★)**: **a 段 (quote/bars の primary 固定) は spec 清書 → 実装へ
   先行して進める** (b/c のレビュー収束を待たない — a 段は state 機械に依存しない独立
   hotfix であり、C1〜C4 の Critical 是正とは無関係に単独で安全に配備できるため)。b 段は
   本 v0.3 で C1 の安全条件 (§3.1) を入れた上で r3 レビューへ送る。c 段は b 段の実装が
   終わった後に改めてレビューする。
8. **段 b の `ready` 復帰は手動**: 段 b には連続性検査/replay が無いため、
   `degraded → ready` は人間が新設シェルコマンド (仮称 `data resume`) を叩いたときだけ
   起きる。段 c 実装まで自動復旧が無い運用 (不通のたびに人間の操作が要る) をこの順序で
   進めてよいかの承認。**v0.3 で安全条件を強化** (未処理足のある OPEN/PENDING_FILL が
   ゼロであること、または `--acknowledge` の明示承認を必須化、§3.1 C1) しており、
   承認を求める対象はこの強化後の手順である。

## 9. 実装前に測る項目

**r1 裁定 I7: 分類を 2 つに分ける** (v0.1 は「外向き通信なし」の T0 に、実際は bridge へ
繋がないと測れない項目 (bridge の応答内容そのもの) を混ぜていた。前者は T0 (spike、
どの段の着手前にも先に回せる) で安価に反証でき、後者は別途「bridge を止める実験」
(実 bridge プロセスに対して行う、ネットワーク到達性はローカル環境内で完結し外部
サービスへは繋がない) が要る)。

**分類 1: 外向き通信なし (scripted fake + tmp SQLite、T0 spike でそのまま実施可能)**:

|項目|状態|
|---|---|
|単発失敗 (1 tick だけ失敗し次 tick 回復) で `state`/`epoch`/`gap_start`/`confirmed` がどう振る舞うか (C3 の「即時停止・2回連続は通知専用」の実装可否)|T0 で fake により検証可能|
|プロセス再起動 (新しい `Ingest`/`Scheduler` インスタンス、同じ conn) をまたいだ `gap_start`/`epoch`/`replay_through` の保持 (C6/AC-9)|T0 で fake により検証可能|
|週末を跨ぐ outage での連続性検査 (C7、既知閉場の除外) が正しく機能するか|T0 で fake により検証可能|
|replay 中の commit 分断 (1 件だけ commit された直後のクラッシュ) から `replay_through` で正しく再開できるか、候補 A/B (§3.1 I6) のどちらがこれを満たすか|T0 spike の主目的|
|`replay_open_positions` の所要時間 (長い空白・多数の OPEN 建玉での `with conn:` 保持時間)|T0 で fake (多件数の合成データ) により測定可能 — 長時間の replay がロックを握り続けると他の tick 処理を遅延させる懸念がある|
|`datafeed.fallbacks` 是正 (writer chain の primary-only 化) が既存の統合テスト・E2E に副作用を与えないか|T0/T2 で全数 grep + 既存テスト実行により確認可能 (§10)|

**分類 2: bridge を止める実験 (実 bridge プロセスに対して行う。外部の MT5/ブローカーへの
新規発注は行わない — 接続応答の観測のみ)**:

|項目|状態|
|---|---|
|bridge プロセス停止時の応答 (接続拒否か timeout か、例外型)|未測定|
|MT5 未ログイン (bridge は起動、`mt5_connected=false`) 時の `/health`・`/ohlcv`・`/quote` の応答内容|未測定|
|不通からの復帰時間 (bridge 再起動〜応答復旧、MT5 再ログイン〜約定可能までの実測)、flap 列の実際の頻度・持続時間 (`ready_confirm_ticks` の既定値 3 の妥当性)|未測定|
|開場中に bridge が「200 だが 0 本」を返す事例が実際に起きるか (未知の休場・シンボル一時停止)、その識別方法|未測定 (§3.4 は推測)|
|MT5 の 1 リクエストあたりの range 取得上限・pagination の有無 (短 range・長 range 双方の件数・端点を bridge GET 数回で確認、I5 — v0.1 の「空白全体が単一 commit で入る」は推測に格下げ)|未測定|
|`current_account` の 10 分猶予・`_BAR_FRESHNESS` の 5 分猶予が、実際の不通復帰時間分布に対して妥当か|未測定 (現行の定数を流用する前提)|

## 10. 影響ファイルと呼び出し元

```bash
rg -n --glob '!tmp/**' 'def get_quote|_chain\(|quote_fn|def _open\(|def _force_close_day|def _retry_close|healthcheck_provider|db_healthcheck|def _scheduler_tick_once|OutageStateMachine|datafeed_outage_state|datafeed_outage_gap|replay_open_positions|kill_switch_latched|current_account|_BAR_FRESHNESS|_closed_bar_unavailable_pairs|latest_closed_cache_bar_time|_latest_cron_watermarks|close_order\(|_trade_mission_due|on_signal_maintenance' src tests
rg -n --glob '!tmp/**' 'TABLE_NAMES' src tests
```

|ファイル|変更/呼び出し元|
|---|---|
|`src/agentic_fx/datafeed/price_provider.py`|(段 a) `_chain` の writer 分岐 (140-157行) を `datafeed.fallbacks` ベースに変更。呼び出し元は `service.py:802,823-826` の `quote_fn`/`get_bars` 束縛。|
|`src/agentic_fx/config.py`|`DatafeedSettings` に `fallbacks: list[str] = []`(段a)、`outage: OutageSettings` (`ready_confirm_ticks` 等、段b) を追加。|
|`config/settings.yaml.example`|新キーの同期。|
|`src/agentic_fx/store/ohlcv.py` または新設 `src/agentic_fx/datafeed/outage.py`|(段 b) `datafeed_outage_state`/`datafeed_outage_gap` の DDL・CRUD (候補 A の場合、§3.1 spike 待ち。`datafeed_outage_gap` に `replay_through` 列、C3)。`TABLE_NAMES` 完全一致テストの更新対象。`ohlcv.py:176-187` の `latest_closed_cache_bar_time` に `source` 引数を追加 (I3)。(段 c) `upsert_cache_bars` (`ohlcv.py:125-148`、`conn.commit()` は 148 行目) の commit しない版 `upsert_cache_bars_nocommit` を追加し、`replay_open_positions` の `with conn:` から使う (C2)。|
|`src/agentic_fx/datafeed/ingest.py`|(段 b) `prepare()` の戻り値を `(count, IngestTickReport)` に拡張 (C2)。`_watermark`/`_fetch_primary` 自体は変更なし。呼び出し元は `service.py:_scheduler_tick_once` (1224行) の 1 箇所のみ (grep 確認要)。|
|`src/agentic_fx/service.py`|(段 b) `_scheduler_tick_once` (1224-1246行) に `OutageStateMachine.observe(report, watermarks, now)` の呼び出しを追加。`_latest_cron_watermarks`/`db_healthcheck` (`scheduler.py:283-291`; `service.py:1103-1120`) を source 絞り watermark (I3) に対応させる。|
|`src/agentic_fx/core/scheduler.py`|(段 b) `_trade_mission_due` (270-316行) の最前段に `state==ready` gate と cursor 停止 (C1)。`_run_hooks` (240-266行) で `on_signal_maintenance` だけを outage gate (C4)。`tick()` (101行〜、136-147行の早期 return) に day close の閉場中スキップの明記 (I2)。(段 c) replay 呼び出しと `_process_limit_fills`/`Executor._open` 手前の state gate。`_observe_closed_bar_availability` (386行) は現状維持しつつ state machine の入力にする。|
|`src/agentic_fx/core/executor.py`|(段 b) `_open` (489-491行) 手前に state gate。(段 c) 新設 `close_order_at(row, event_time, price, reason)` を `close_order` (860-871行) と `transitions.transition` 経路を共有する形で追加 (C5)。|
|`src/agentic_fx/core/transitions.py`, `src/agentic_fx/store/orders.py`|(段 c) `orders.py:17-29,37-45` の `insert`/`update_fields` は呼び出しの最後で自前に `conn.commit()` する — commit しない primitive を追加し、`replay_open_positions` の `with conn:` から使う (C6)。|
|`src/agentic_fx/core/market_hours.py`|(段 c) 「次の期待取引時刻」を返すヘルパを追加し、3.3 の連続性検査・3.4 の休場区別で共有する (C7)。|
|`src/agentic_fx/loops/trade_loop.py`|(段 b) healthcheck 呼び出し (109-142行) に state チェックを追加 (現行の緩い閾値と併用)。|
|`src/agentic_fx/commands.py`|(段 b) status 出力に data state 行を追加。新設シェルコマンド `data resume` (手動 ready 復帰)。(段 c) `confirm_data_gap` (未知の空白の人間確認)。|
|`src/agentic_fx/core/paper_fills.py`|変更なし (`check_exit`/`check_limit_fill` を replay からそのまま呼ぶだけ)。|
|tests: `datafeed/test_outage.py` (新設)、`core/test_scheduler.py`、`test_service_app.py`、`loops/test_trade_loop.py`、`datafeed/test_price_provider.py`、`store/test_orders.py`|AC-1〜15 の fake transport/clock、状態永続化、状態機械 flap・crash 再開のテストを追加。|

## 11. task 分割素案

r1 裁定 (範囲★) の 3 段分割をそのまま task 束の境界にする。各段は前段が main に
入った状態で単独に着手・配備できる。

```text
[段 a: A2-3a quote-primary-only]
Ta1 quote fallback 是正 (単独、他段に依存しない)

[段 b: A2-3b outage-stop-and-observe]  (Ta1 と並行可、依存なし)
T0 spike (分類1: 外向き通信なし、候補A確定済み・crash-safe確認のみ) ─┬→ T1 state schema/永続化 (候補A) ─┬→ T3 scheduler 統合 (gate のみ) ─┬→ T5b 段 b 統合検収
                                 └→ T4 healthcheck/signal maintenance 統合 ─────────┘                              │
                                                                                    Tb-cmd `data resume` シェルコマンド ─┘

[段 c: A2-3c backfill-and-replay]  (段 b の T5b 完了後に着手)
T6 market_hours 「次の期待取引時刻」ヘルパ + 連続性検査 (C7) ─┬→ T8 replay 本体 (close_order_at, C5) ─┬→ T10 段 c 統合検収
T7 store/orders の commit しない primitive + with conn: (C6) ─┘                                        │
                                                            T9 休場区別 (§3.4)・confirm_data_gap ─────┘
```

|task|段|内容|主な AC|
|---|---|---|---|
|Ta1|a|`datafeed.fallbacks` 設定キー、`PriceProvider._chain` writer 分岐の primary-only 化、legacy `enabled` 併用時の migration warning。|AC-7|
|T0|b|spike (分類 1: 外向き通信なし、§9)。永続方式は候補 A (DB 表 2 つ) に確定済み (r3 裁定 C3、§3.1/§6) — 候補選択はもう T0 の目的ではない。fake transport/tmp SQLite のみで、ready/degraded の即時遷移・`confirmed` 昇格・crash からの再開 (nocommit primitive・`with conn:` の境界) が候補 A の DDL で実際に crash-safe に成立するかを確認する。bridge 不通時の実際の例外型 (§9 分類 2) はここでは測らない。|なし (準備)|
|T1|b|`IngestTickReport` (真に immutable、frozenset の `failed`、C2/I1) を返す `Ingest.prepare()` の拡張、`OutageStateMachine` (observe/即時 degraded/confirmed 昇格、停滞式は §3.1 の単一式、I2)、永続化 (候補 A の DDL、§3.1)、`latest_closed_cache_bar_time` への `source` 引数 (I3)、`TABLE_NAMES` 更新。`resume_requested_at`/`resume_acknowledge` の消費ロジック (C2、§3.1) を `OutageStateMachine.observe` に含める。**`_scheduler_tick_once` で tick 冒頭に `now` を 1 回だけ採取し、`prepare`/`commit`/`observe` へ同じ値を渡す (I3/I5、service task に明記)。**|AC-2,AC-2c,AC-12,AC-13,AC-15|
|T3|b|`_trade_mission_due` の `state==ready` gate・cursor 停止 (C1)。`_process_limit_fills`/`Executor._open` の state gate。`data resume` (Tb-cmd) が使う「未処理足のある OPEN/PENDING_FILL」の保守規則判定 (§3.1 C1) の土台。|AC-14 (b 専用 resume 存在チェック)、AC-2b,AC-11|
|T4|b|`_run_hooks` での signal maintenance gate (C4)、`TradeLoop`/`db_healthcheck` の state 統合。**`healthcheck` 呼び出しも T1 で採取した同一 `now` を受け取る形にし、独自に時刻を読み直さない (I3/I5)。**|AC-3|
|Tb-cmd|b|`commands.py` に `data resume` (手動 ready 復帰、§3.1 C1: 全 hard key 健全 **かつ** 未処理足のある OPEN/PENDING_FILL がゼロ or `--acknowledge`)・status 表示への data state 行追加。`Commands` は `core_lock` を取らず「resume 要求」の永続化のみ行う (C2、§3.1)。runbook (建玉があれば先に手動クローズ) を §3.1/§8 の記述と揃える。|AC-14,AC-15 (§8 ★ 承認後)|
|T5b|b|段 b 統合検収 (fake、実 bridge なし)。ここで段 b を単独で main に入れられる状態にする。|AC-2,AC-2b,3,11,12,13,14,15 の統合|
|T6|c|`market_hours` の「次の期待取引時刻」ヘルパ (`next_expected_trading_time`、12/25・1/1 の取引日境界を週末と同じ規律で扱う、I4)、3.3 連続性検査 (C7)、3.4 休場区別 (`IngestTickReport.empty` 活用)。|AC-8,AC-8c,10|
|T7|c|`store/orders.py` の commit しない primitive、`ohlcv.upsert_cache_bars_nocommit` (C2)、`replay_open_positions` の `with conn:` 化 (backfill + 注文遷移 + pair cursor + state を 1 transaction に)、`datafeed_outage_gap.(pair,interval,epoch).replay_through` の永続 (C3/C6)。|AC-9|
|T8|c|`Executor.close_order_at` (C5)、`replay_open_positions` 本体 (`max(gap_start の次の足, filled_at)` 下限つき、`state==backfilling` 中の毎 tick drain、追いついた pair だけ通常 exit へ、失敗 pair は `_process_exits` から除外、C4)、cursor をバッチ完了で無条件前進させる (I1)、`backfilling→ready` 自動遷移。|AC-1,AC-1b,AC-1c,AC-1d,4,6|
|T9|c|`confirm_data_gap` シェルコマンド、休場を跨ぐ backfill の実 E2E 確認 (§9 分類 2 の bridge 実験結果を反映)。年末年始境界 (12/25・1/1) の実 E2E も含める (I4)。|AC-8,AC-8c|
|T10|c|段 c 統合検収 (fake + §9 分類 2 の bridge 実測を踏まえた調整)。transaction 内クラッシュ fault injection (I5) を含める。|AC-1,1b,1c,1d,4,5,6,8,8c,9,10 の統合|

## 変更履歴

|日付|版|変更|理由|commit|
|---|---|---|---|---|
|2026-09-24|v0.1|初稿。現物 (A2-1b/B-1 実装済み) の再読取りに基づき、前束下書きの「pending 維持」「health 1回で ready」等の誤りを引き継がず書き直した。quote fallback の無言切替 (Critical 相当) と末尾 1 本しか replay しない欠陥を新規に特定し、設計に組み込んだ。|ユーザー依頼 (下書き 1 本)、前提の再検証|—|
|2026-09-24|v0.2|codex レビュー r1 (`tmp/design-a23/r1/codex-out.md`、C7/I7) の指揮者裁定 (`tmp/design-a23/r1/verdicts.md`、全件採用) を反映。範囲を 3 段 (A2-3a quote-primary-only / A2-3b 停止gate+観測+永続episode、ready復帰は手動 / A2-3c backfill連続性+paper replay+自動ready) に分割 (§2)。C1 cron gate の欠落・C2 ingest tick report・C3 即時停止 (2回連続は通知専用)・C4 signal maintenance 個別 gate・C5 `close_order_at`+filled_at下限・C6 commitしないprimitive+`with conn:`+`replay_through`・C7 既知閉場を除外した連続性検査、をそれぞれ Critical として §3.1〜3.4/§4/§5/§10/§11 に反映。I1(=段a)・I2 day close の閉場中スキップ明記・I3 watermark readerのsource絞り・I4 AC実時刻3箇所の訂正(10:04ちょうどはfresh/11:47時点の確定1mは11:45・1hは10:00/tickごとにnowを1回)・I5 MT5 range上限を推測へ格下げ・I6 永続を候補A(DB表2つ)/候補B(StateStore 1キー)のどちらにするかをb段spikeで決定 (state.jsonの「完全一致検査」という誤記も訂正)・I7 §9を「外向き通信なし」と「bridgeを止める実験」に分離、を反映。§8に3段分割・段bの手動ready復帰の★を追加。§1に現物再検証で見つかった根拠 (ingest.last_errors残留、latest_closed_cache_bar_timeのsource条件欠如、close_orderのclock.now()依存、store/ordersの都度commit) を追加。|指揮者裁定 (全件採用、蒸し返さない)|—|
|2026-09-24|v0.3|codex レビュー r2 (`tmp/design-a23/r2/codex-out.md`、C4/I5) の指揮者裁定 (`tmp/design-a23/r2/verdicts.md`、全件採用) を反映。**C1 (Critical)**: 段 b の手動 `data resume` 前提条件を「全 hard key succeeded」だけから「(1) 健全 watermark **かつ** (2) 未処理足のある OPEN/PENDING_FILL がゼロ、または `--acknowledge` 明示承認」の両方必須に強化 (§3.1)。IV-11 新設、runbook (建玉は先に手動クローズ) を明記 (§3.1/§11 Tb-cmd)。**C2 (Critical)**: `upsert_cache_bars` に nocommit 版を追加し、段 c は backfill・注文遷移・pair cursor・state を 1 つの明示 transaction に、段 b の `ingest.commit()` は現行のままでよい理由 (b は replay しないため分断の害が無い) を明記 (§3.1 判定者節/§3.3/IV-8/§10/§11 T7)。`IngestTickReport.failed` を dict から `frozenset[(key,error)]` に変更し真に immutable にし、budget 由来 `deferred` と backoff 由来 not-attempted を区別 (I1、§3.1)。**C3 (Critical)**: `replay_through` を `datafeed_outage_state` の global 1 列から `datafeed_outage_gap` の `(pair, interval, epoch)` 行へ移し、再開点を `next_expected_trading_time(replay_through)` に (§3.1 DDL/§3.3/IV-8/AC-9/§10/§11 T7)。**C4 (Critical)**: replay→通常 exit の handoff を「1 tick だけ replay して以降は末尾 1 本」から「`state==backfilling` の間は毎 tick drain、追いついた pair だけ通常 exit へ、失敗 pair は `_process_exits` から除外」に改訂 (§3.3「replay → 通常 exit の handoff」新設、IV-3/AC-4 (streak 0 復帰)/§11 T8)。**I2**: 停滞式の二重 grace 加算を廃止し `expected = watermark + 2×足幅 + grace` の単一式に統一、実時刻 AC (1m 10:02:00・1h 11:01:00) を AC-2c として新設 (§3.1/§5)。**I3 (実装注記)**: tick 冒頭で `now` を 1 回だけ採取し `prepare`/`commit`/`observe`/`replay`/`scheduler`/`healthcheck` へ引き回すことを IV-12・§11 T1/T4 に明記。**I4**: 12/25・1/1 の取引日境界 (21:00 UTC) を 1m・1h 双方でカバーする AC-8c を新設 (§5/§11 T6/T9)。**I5 (逆変異強化)**: AC-7 に writer の `get_bars` も fallback 0 回である検証を追加、AC-4 に「成功→欠落」で `ready_streak` が 0 に戻ることの直接 assert を追加、AC-9 に transaction commit 前への fault injection (nocommit primitive が個別 commit していないことの検証) を追加 (§5/§11 T10)。**進め方 ★**: a 段 (quote/bars primary 固定) は b/c のレビュー収束を待たず spec 清書→実装へ先行させる方針を §8 に明記。|指揮者裁定 (全件採用、蒸し返さない)|—|
|2026-09-24|v0.4|codex レビュー r3 (`tmp/design-a23/r3/codex-out.md`、C3/I3) の指揮者裁定 (`tmp/design-a23/r3/verdicts.md`、全件採用) を反映。**C1 (Critical)**: `data resume` の「未処理の確定足がある建玉」判定を、根拠薄弱な「`filled_at` または `entered_degraded_at`」の単純存在チェックから**保守規則**に置き換えた — 段 b は建玉別の適用 cursor を持たないため、episode 内の対象足は pair の全 OPEN について無条件に未処理扱いとし、各建玉の下限は `max(next_expected_trading_time(gap_start), filled_at)`、`PENDING_FILL` の下限は `created_at` とする (§3.1、IV-11 更新、AC-14 新設)。**C2 (Critical)**: `data resume` を tick-local な直列化に改めた — `Commands` は `core_lock` を取らず state を直接書かない (現物のまま)。resume は「resume 要求」(`resume_requested_at`/`resume_acknowledge`、§3.1 DDL に列追加) として永続化するだけにとどめ、次 scheduler tick の同一 `now`・`report`・`watermarks` を使う `commit → observe → resume 判定 → scheduler.tick()` という単一 lock 区間で消費する (§3.1「resume 要求の消費」新設、§3.2 Commands 行、IV-14 新設、AC-15 新設)。**C3 (Critical)**: c 段の永続方式を候補 A (DB 表 2 つ) に確定し、候補 B (`StateStore`) を退けた案に移した — `state.json` は別ファイルであり SQLite transaction に原理的に参加できないため (§3.1 永続化パラグラフ、§6 退けた案に行追加、IV-8 更新、§11 T0/T1 の記述を候補選択から crash-safe 確認へ縮小)。§3.3 冒頭を「段 c は `ingest.commit()` を呼ばない、`upsert_cache_bars_nocommit` で pair/key 別に nocommit 書込みし外側の 1 つの明示 `with conn:` transaction でまとめて commit する」に書き換え、現物の内部 commit を呼ぶ経路を残さないようにした。**I1 (Important)**: replay cursor (`replay_through`) の前進をヒットの有無・OPEN 件数に従属させない — pair のバッチが例外なく処理された時点で最後の入力足まで無条件に進めるよう手順 5 を改訂し、全ヒット・OPEN ゼロ・一部生存の 3 ケースを AC-1c として新設 (§3.3、IV-13 新設)。**I2 (Important)**: replay→通常 exit の drain/handoff 規律を専用の受入条件 AC-1d (11:47→11:50 の時刻列で 11:46/47/48 が順に 1 回ずつ処理され、失敗 pair の通常 exit 呼出しが 0 回) で反証可能にした (§3.3)。**I3 (Important)**: §11 の b 段 task–AC 対応の誤りを是正 — T3 が誤って参照していた (c 段の) AC-1b を外し、新設 AC-14 (b 専用 resume 存在チェック) を割当てた。Tb-cmd に AC-14/AC-15 を割当てた (従来「なし」)。T5b の統合対象に AC-2b・AC-14・AC-15 を追加した (従来 AC-2b が漏れていた)。T8/T10 にも AC-1c/AC-1d を追加した。|指揮者裁定 (全件採用、蒸し返さない)|—|
|2026-09-24|v1.0|ユーザー裁定 (3 段分割・b 段の手動復帰を承認、replay の SL/TP は実口座と同じ結果で決済扱い、実口座は reconcile) を §8 に記録し、spec として清書。r4 (codex terra、C0/I1/M1) で Critical 0|||
| 2026-09-25 | v1.1 | 段 b 実装完了 (`dea38d7`)。実装で確定した細部: resume 前提 (1) は「hard key ごとの最後の試行が非 empty で succeeded かつ停滞なし」(同一 tick 要求は 15m 足で運用不能のため)、新 epoch で証拠全クリア、再起動直後は不健全。閉場 tick の cron baseline は ready 時のみ。復旧待ち通知の一回性は `recovered_notified_epoch` 列で永続。`data_outage_unprocessed_bars` は内訳変化時のみ。resume 時の強制 probe と IngestTickReport への source 付与は段 c の設計項目 | 実装レビュー terra 4 周の是正 | `dea38d7` |
