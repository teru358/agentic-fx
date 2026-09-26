# [decision-timeframe-config] B-2 設計書 v1.2

対象 commit: `ada582b`。作成日: 2026-09-26。先行 spec: A2-1 [closed-bars-and-required-window] v1.1、B-1 [decision-timeframe-config] v1.0、A2-3 [outage-stop-and-backfill] v1.1 (いずれも main に merge 済み)。範囲の正は `docs/superpowers/specs/2026-09-22-decision-timeframe-config-design.md` §2 の B-2 行 5 つと §4 である。範囲外: `[multi-decision-timeframes]`、`[intent-evidence-timeframe-gate]`、`[first-run-setup]`。

## 要点

1. **signal と cron は「購読」で結ぶ (推奨)**。いま 1h strategy + 15m 判断足では、同じ strategy signal が最大 4 本の cron mission に `pending` のまま見え、cron が建てた直後に signal 起動 mission が同じ signal を claim して再提案できる (§3.1 の時系列)。cron mission も**既存の `signals.claim_oldest` をそのまま使い、1 tick に最古の pending 1 件だけを claim する** (`src/agentic_fx/store/signals.py:161-186`)。signal mission と同じ「1 mission = 1 claim = 1 intent」の対応を崩さない。intent のパース成功で consume する。
2. **閉場明けの最初の tick で前セッションの最終足に mission を起こさない**。`session_start(now)` を「直近の閉場→開場遷移」の状態として定義し、週末・12/25・1/1 単独だけでなく、祝日と週末が 連結する境界 (2026-12-25 は金曜) も 1 つの閉場期間として扱う (§3.2)。
3. **再起動で同じ足を再判断しない**: 受理済み watermark を DB に永続化し、起動時に復元する。 永続化は書込みと mission 受理を同一トランザクションにできないため **at-least-once** (書込み 失敗時は同じ足を再度受理し得る) に弱め、復元値が現在の確定足より未来 (`L > W`) なら 起動を拒否して理由を出す (§3.2)。遡及は現行どおり最大 1 本 (最新の確定足へ合流) で、 途中の足は判断しない。
4. **15m の非重複は現物ですでに構造的に成立している** (supervisor の単一スロット + busy 時は cursor を進めず次 tick で最新足へ合流)。欠けているのは可観測性 (busy で見送っても何も 記録されない — supervisor は `busy_since` を既に保持している `src/agentic_fx/core/ supervisor.py:54,128,141` ので、これを起動理由へ渡す配線が要る) と、mission 上限が 足幅以上になる設定の起動時検査。上限は `timeout_sec + worker_grace_sec` の 330 秒だけでなく、 子プロセスの起動待ち・SIGTERM 猶予・SIGKILL 後の wait (再実行分を含む)・reader/dispatcher の join を含む worker 1 回の上限 Cw (既定 405 秒、backend=claude なら 415 秒) で検査し、dispatch 全体 (trade+reflection 最大 3 件) の上限 Cd (既定 1630/1670 秒) は起動時 WARNING にとどめる (§3.3)。既存の watchdog 上限 (1420 秒) は Cd と食い違っているため、Cd から導出する 1 本の値に 統一する (§1, §3.3, §7)。
5. `context_timeframes` は**宣言だけ**を足す: prompt に「参考足 (発注判断の足ではない)」と明記し、 tool 応答に `role` を付ける。health (`primary_intervals`) にも ingest にも入れない。ただし この契約は**誤用を強制的に防ぐものではなく、意味を表示して抑制する助言**にとどまる — LLM が `role=context/other` の足を理由に OPEN を返しても executor は検証しない。強制するには intent に根拠足を記録して gate する別設計が要る (`[intent-evidence-timeframe-gate]`、範囲外、§2)。
6. provenance は `missions` の単数列 2 本ではなく、pair 単位の子表 `mission_decision_bars(mission_id, pair, interval, bar_time)` を新設する。単数 2 列は複数 pair が同一 cron mission で due のとき、どの pair の watermark かを列から復元できない (最大値が別 pair の値で上書きされる) ため退ける (§3.5, §6)。orders は `orders.intent_id → trade_intents.mission_id → mission_decision_bars` の join で辿る。 4h / 1d を判断足にすると cron が永久に発火しない (registry が 1h 保存へ正規化するため watermark が常に空になる。`src/agentic_fx/datafeed/requirements.py:30-34,46-50`) ので 当面は起動拒否する。

## 1. 前提の検証

### 1.1 現物で確認した事実

|事実|根拠 (file:line)|設計への含意|
|---|---|---|
|cron の due は `(pair, 判断足)` の確定 watermark が in-memory cursor より新しいときだけ。cron が due の tick では `signal_due_fn` を評価しない (else-if)。|`src/agentic_fx/core/scheduler.py:256,333-356`|同じ tick で両方 due なら cron が勝ち、signal は次 tick 以降。|
|cursor は受理 (`try_submit` が `None` でない) のときだけ前進。busy なら `None` で cursor 不変、次 tick に再判定し、その間に進んだ足は最新 1 本へ合流する。見送り時の記録は無い。|`scheduler.py:256-260`; `src/agentic_fx/core/supervisor.py:61-71`; `service.py:1077-1081`|非重複と coalesce は成立済み。欠けているのは観測だけ。|
|`Supervisor` は `busy_since` (dispatch 開始時刻の `time.monotonic()`、非 busy 時は `None`) を既に保持しているが、`try_submit` の戻り値は `Future \| None` だけで理由を運ばない。|`supervisor.py:54,61-71,128,141`|deferred activity の `running N.N min` は `busy_since` から出せる。callback を構造化する配線だけが要る (§3.3)。|
|`MissionSupervisor._run` は `future.set_result`/`set_exception` を呼んだ**後**に `finally` でスロット (`_busy`/`busy_since`) を解放する。`future.result()` の戻り値が呼び出し元 (`try_submit` の呼び出し元・`ask` 経路) に届いた直後でも、`finally` がまだ実行されていなければ、その場で発行した次の `try_submit` は依然 busy として拒否され得る (現物の潜在欠陥、未修正)。|`supervisor.py:106,128,132,136,141`|B-2 では `_phase`/`busy_since`/`checked_at` の解放を future 解決の**前**に置き換える (§3.3)。順序を変えない限り、構造化後もこの短い拒否窓は残る。|
|cursor `_cron_watermarks` はプロセスメモリだけ。開場中の再起動では最初の tick で最新確定足の mission が 1 本出て、閉場中なら baseline だけ。catch-up 専用の関数は無い。|`scheduler.py:105,149-168,333-347`|再起動のたびに、判断済みの足を再判断し得る (実例 #173/#174、premises §2)。|
|閉場中は ingest が何も取らない。金曜 21:00 UTC 以降は閉場。祝日は UTC 暦日でなく取引日ラベル (`now + 3h` の日付) で判定する。|`src/agentic_fx/datafeed/ingest.py:88-89`; `src/agentic_fx/core/market_hours.py:25-40`|金曜 20:45 の 15m 足 (確定 21:00:30) は日曜 21:00 まで DB に入らない → 開場 tick で「新しい watermark」として発火 (推測: 実行未確認、§9-1)。|
|tick 内の順序は ingest prepare → commit → outage observe → scheduler.tick。tick 内では hooks (signal maintenance = producer) が cron 判定より先に走る。|`service.py:1273-1290`; `scheduler.py:241,256,266-296`|strategy 足の確定と判断足の確定が同じ境界なら、同じ tick で signal が作られ、直後の cron mission がそれを見られる。|
|producer の bucket は strategy の `config.yaml: timeframe` で、`floor_to_bucket(now, tf)` と「対象 bucket の行が DB に在るか」で評価する (DB watermark ではない)。cursor はメモリのみ。|`src/agentic_fx/plugin/signal_producer.py:169-197,227-250`|signal の時刻は strategy 足の境界。判断足とは無関係。|
|signal 起動は「OPEN か PENDING_FILL の注文がある」かつ「pending signal がある」かつ「前回 signal mission から 10 分以上・日次 12 本未満」のときだけ。|`service.py:1163-1171`; `src/agentic_fx/store/missions.py:219-236`; `config.py:442,444`|建玉が無いとき strategy signal は signal 起動では消費されない。cron が `get_signals` で見るだけ。|
|signal を consume するのは `trigger=="signal"` の mission だけ。cron mission は claim も consume もしない。`claim_oldest` は bar_ts 最古の pending 1 行を単一 UPDATE で原子的に claim する。|`src/agentic_fx/loops/trade_loop.py:165-190,305-320,508-530`; `src/agentic_fx/store/signals.py:161-186`|cron が見た signal は `pending` のまま残る。cron を claim/consume の主体に足すには、既存 `claim_oldest`/`consume` をそのまま呼べばよい (新規 store 関数は不要)。|
|`abandoned` は (a) 鮮度切れ `bar_ts < now - tf幅×signal_freshness_bars` の pending を maintenance が一括付与、(b) requeue / lease 回収で `requeue_count >= signal_requeue_max` (既定 2)。|`signals.py:189-203,223-271`; `service.py:692-696`|1h・鮮度 2 本なら bar_ts 13:00 の signal は 15:00 を過ぎた最初の maintenance で abandoned。|
|`get_signals` は status を問わず直近 24h を返す。行の `timeframe` は strategy の足。|`src/agentic_fx/tools/signal_tools.py:53,99-122`; `signals.py:280-296`|LLM から見て「どの足の signal か」は読めるが、「判断足と違う」ことは明示されない。|
|trade mission の実行時上限は `llama_swap.timeout_sec` (300) と `worker.worker_grace_sec` (30) の合計 (330 秒、`deadline_budget`)。その前に `worker.worker_startup_timeout_sec` (30) の起動待ちがあり、後に `_escalate_kill` の `worker_terminate_grace_sec` (10、SIGTERM→poll→SIGKILL) と `finally` の `_ensure_dead`→`_kill`→`proc.wait(5)`・`reader.join(5.0)`・`dispatcher.join(rpc_timeout_sec(15)+5.0=20)` が続く。`_escalate_kill` 内の `_kill` も同じ `proc.wait(5)` を持つため、正常に `_escalate_kill` が既に殺している場合以外は `finally` の `_ensure_dead` がこの wait をもう一度実行し得る (`worker_runner.py:697-712`、上界には 2 回分を積む)。backend が claude でも同じキー。|`src/agentic_fx/runners/worker_runner.py:509,568,617,628-630,685-712`; `config.py:465-467,474`; `config/settings.yaml.example:48,148-150,153`|1 mission が supervisor スロットを占有し得る上限 (worker 1 回の Cw、§3.3) はこの直列合計。`try_submit` が `None` を返す期間 = このスロット占有時間そのもの。終了保証は runner の deadline にあり tool 予算ではない。|
|watchdog の `_default_dispatch_ceiling_sec` は `(llama_swap.timeout_sec + worker.worker_grace_sec + worker.worker_terminate_grace_sec) × 4 + 60` = `(300+30+10)×4+60 = `**1420 秒**。起動待ち・SIGKILL 後の `proc.wait` 再実行分・`reader`/`dispatcher` の join・(claude なら) CLI 回収猶予を含まない別式で、§3.3 の Cd (1630 秒 local / 1670 秒 claude) より小さい — 同じ「1 dispatch (trade+reflection 最大 3 件) が supervisor スロットを占有し得る上限」を指しながら値が食い違う。|`src/agentic_fx/service.py:1392-1396`|watchdog・`supervisor_join_timeout_sec` の上限を Cd から導出する 1 本の値に統一する (§3.3、§7)。|
|prompt の判断足は `_build_prompt` の固定節 (`decision_timeframe: …` と signal の注記) だけ。状態サマリは時刻と建玉 (horizon 含む) を出すが、rollover や金曜 cutoff の残り時間は出さない。|`trade_loop.py:594-607`; `src/agentic_fx/loops/summary.py:58-78`|context 足・day 期限の提示点はここ。|
|`get_ohlcv` / `get_indicators` の `timeframe` enum は `datafeed.intervals` 全体。省略時は判断足。取得は readonly provider が **primary へ毎回取りに行き** (保存しない)、失敗時だけ cache。executor は intent の発注根拠にどの足を使ったかを検証しない。|`src/agentic_fx/tools/market_tools.py:19-22,44-72,95-103`; `src/agentic_fx/datafeed/price_provider.py:155-157,192-257`; `src/agentic_fx/core/executor.py:435-481`|15m 化で mission 数が 4 倍になると tool 経由の外向き要求も比例して増える。role 表示は表示だけで gate ではない。|
|`primary_intervals` は判断足から導出され、読むのは `db_healthcheck` と registry の health 登録と ingest の優先順だけ。tool の許容足には関与しない。|`config.py:566-612`; `service.py:1127-1152`; `src/agentic_fx/datafeed/requirements.py:55-61`; `ingest.py:46-58`|context 足を health に入れる理由は無い。入れると context の欠損で判断が止まる。|
|registry は 4h 以上を 1h×倍数に書き換え、4h・1d は派生専用 (保存しない)。ingest の key は registry の interval。|`requirements.py:30-34,46-50`; `src/agentic_fx/datafeed/cache_window.py:24`; `ingest.py:40-44`|判断足 `[4h]` では cron watermark (`ohlcv_cache` の `interval='4h'`) が常に `None` → cron 不発、healthcheck も常に不健全。静的読取りで確定 (§9 では測らない)。|
|`orders` と `trade_intents` に足の列は無い。`missions.trigger` はある。activity は 5 列の TSV (`ts, category, event, summary, ref_id`)。|`src/agentic_fx/store/db.py:88-114,196-222`; `src/agentic_fx/activity.py:47-53`|判断足の provenance は現在 DB から復元できない。|
|day horizon は `filled_at` の次の 21:00 UTC − 5 分で強制決済。swing は Risk Gate の金曜 cutoff (NY 14:00) と risk 係数だけ。どちらも判断足を読まない。|`scheduler.py:953-1007`; `src/agentic_fx/core/risk_gate.py:202-204`; `src/agentic_fx/core/sizing.py:73-75`|horizon は判断足と独立に成立している。|
|`afx init` は `settings.yaml.example` を複製するだけの非対話処理。ウィザードの実装は無い。|`src/agentic_fx/entry.py:19-25`; `service.py:381-411`|ウィザードは要件の受け渡しだけにする。|

実測 (DB `missions`、`loop='trade'`、2026-09-15 以降、設計側の read-only 集計の転記): local qwen3.8 completed 70 本 avg 2.1 / max 3.1 分、max_turns 6 本 avg 3.5 / max 4.4 分、 timeout 1 本 5.0 分 (= 300 秒)。claude sonnet completed 5 本 avg 0.8 / max 1.3 分。 15m の通常発火は確定 +30 秒前後 (#171 12:30:31、#172 12:45:35、#173 13:00:41)。 配備 strategy `sma_cross_10_30` は `timeframe: 1h`、`max_bars: 400` で、起動ごとに 「strategy timeframe 1h differs from decision timeframe 15m」の warning が出る (`service.py:624-635`)。

### 1.2 spec §2 / §4 の記述が現物と合わない点

|spec の記述|現物|扱い|
|---|---|---|
|「Claude/Codex の所要時間では次 due と重なり得る」(§2)|sonnet は avg 0.8 分。上限に当たっているのは local (timeout 5.0 分)。codex は trade backend として拒否される (`config/settings.yaml.example:26`)。|重なりの主因は backend ではなく mission 上限の end-to-end ceiling。検査は §3.3 で行う。|
|「restart catch-up を一般化」(§2/§4)|一般化すべき catch-up 関数は無い。実体は「メモリ cursor が空 → 最新 1 本で発火」。|問題は遡及数ではなく、(a) 再起動で判断済みの足を再判断する、(b) 閉場明けに前セッションの足で発火する、の 2 点 (§3.2)。|
|「signal_due_fn、producer bucket、cron mission の三者を…同一化するか購読するか」(§4)|signal 起動は建玉がある時だけ (D2 条件) で、strategy signal の主な消費者はすでに cron の `get_signals`。ただし consume しない。|論点は「誰が signal を一度だけ引き受けるか」。§3.1 で購読 (既存 `claim_oldest` の共用) を推奨。|
|「primary interval/必要本数/保持を context にどう要求するか」(§4)|tool は registry に載らず、要求時に primary へ直接取りに行く (A2-1 の契約)。`primary_intervals` は tool を制限しない。|context は registry・health に入れない (§3.4)。|
|「provider 別 LLM の最大実行時間を測定」(§4)|trade の上限キーは backend に関係なく `llama_swap.timeout_sec` 1 本。|provider 別のキーは作らず、end-to-end ceiling を起動時に足幅と比べる (§8-9)。|
|B-1 AC-B1-10「金曜足を開場時に誤発火」を殺すべき実装としている|閉場後に確定する金曜最終足は開場 tick で初めて取り込まれ、発火する (推測)。既存試験 `tests/core/test_scheduler.py:288-303` は金曜足を閉場中に DB へ置いて baseline させる形で、「開場 tick で初めて commit される金曜足」は試していない。|§3.2 のセッション規則で塞ぐ。§9-1 で実 DB の日曜 21:0x の cron 行を確認する。|

前提調査の訂正: (1) abandoned は requeue 上限だけでなく鮮度切れ (`expire_stale`, `signals.py:189-203`) でも付く。(2) prompt の判断足の注入点は `trade_loop.py:594-607` で特定済み。 (3) 稼働中の `datafeed.intervals` は `[1m, 5m, 15m, 1h, 4h]` (`config/settings.yaml:58`) なので、 15m 判断で `get_ohlcv(timeframe="1h")` は取得できる。

## 2. 目的・範囲・範囲外

**目的**: 判断足を 15m にしても (a) 同じ情報で二度建てない、(b) 古い足で判断しない、 (c) 見送り・合流・停止の理由が利用者に読める、状態にする。判断回数を増やすこと自体は 目的ではない (製品像: 失敗や空振りの理由が見えること)。

|段|内容|単独で main に入る理由|
|---|---|---|
|**B-2a** 観測と構造検査|busy 見送り・合流の activity (`busy_since` の配線を含む)、mission end-to-end ceiling ≥ 足幅の起動時検査、派生専用足 (4h/1d) の判断足を起動拒否。schema 変更なし。|現行の 5m でも busy を挟みながら coalesce しつつ起動できてしまう — この段は「1 確定足に 1 回」という製品契約を明文化・強制する互換差であり、既存動作の追認ではない。|
|**B-2b** 時刻モデル|閉場明けのセッション規則 (`session_start` の状態遷移定義)、受理 watermark の永続化と起動時復元 (dirty state・未来値の fail-fast を含む)、`mission_decision_bars` の新設。|scheduler・store に加え、`mission_decision_bars`/`cron_cursor` の migration で `store/db.py` が変わる。cursor 復元は `Scheduler.__init__` (`init_db` が先行) で完結し、`service.py` の起動処理は変えない (§10)。LLM に見える prompt 本文は変えない。|
|**B-2c** signal 購読|cron mission が既存 `claim_oldest`/`consume` を使って新鮮な pending signal を最古 1 件だけ引き受け、prompt に載せる。get_signals の行に足の注記。|trade_loop と signals store だけ。a/b に依存しない。|
|**B-2d** context 足と期限の提示|`datafeed.context_timeframes`、prompt の参考足節、tool 応答の `role` (表示のみ、gate ではない)、day 強制決済時刻の提示。|config と LLM 向け表示だけ。|

**範囲外**: 複数判断足の実行・予算・両建て (`[multi-decision-timeframes]`)、horizon の再定義と 判断足からの換算、ウィザードの実装 (`[first-run-setup]`、本書は要件だけ渡す)、tool を DB 読みへ 切り替えること (A2-1 の契約変更。§6)、A2-2 の要求予算、戦略の 15m 化そのもの (利用者の判断、§8-7)、 **intent に根拠足を記録して executor が gate する強制設計 (`[intent-evidence-timeframe-gate]`。 本書の role 表示は助言止まりで、誤用の強制防止はこの起票の範囲)**。

## 3. 設計

### 3.1 signal 起動 ↔ cron mission ↔ producer bucket

**時系列 (現物、判断足 15m、strategy 1h、鮮度 2 本、grace 30 秒、USDJPY、2026-09-24 木)**:

|時刻 (UTC)|起きること|
|---|---|
|14:00:30 の tick|ingest が 13:45 の 15m 足と 13:00 の 1h 足を commit → hooks で producer が bucket 13:00 を評価し `open` → signals 行 (bar_ts 13:00、pending) → cron due (watermark 13:45) → cron mission A 受理|
|14:02|mission A が `get_signals` で bucket 13:00 を見て指値を出す。signal は pending のまま|
|14:03:30 の tick|cron は due でない。建玉 (PENDING_FILL) と pending signal があり、signal mission は 10 分以上前 → **signal mission B が bucket 13:00 を claim して再提案し得る**|
|14:15:30 / 14:30:30 / 14:45:30|cron mission C/D/E も同じ signal を `pending` (B が consume 後は `consumed`) として見る|
|15:00:30 の tick|maintenance の鮮度切れで未 consume なら abandoned (bar_ts 13:00 < 15:00:30 − 120 分)|

**三案と推奨**:

|案|動作|採否|
|---|---|---|
|**購読 (推奨)**|cron mission が prepare (core_lock 内) で、`missions.start(commit=False)` + `mission_decision_bars` INSERT + commit の**後** (§3.5 の書込み順序)、既存の `signals.claim_oldest` を使って**最古の pending 1 件だけ**を自分へ claim し、prompt の「この判断で扱う signal」節に載せる。intent のパース成功で consume する (hold でも「判断済み」として consume)。mission 失敗・timeout・prepare 例外は既存の requeue 経路で戻す。1 mission = 1 claim = 1 intent の対応を signal mission と共有する。|採用|
|独立維持 + 表示|現状の経路のまま、get_signals の行に `strategy_timeframe`・`fresh_until` を足し、prompt で再提案を禁じる。|退ける: 二度建ての防止を LLM の読解に委ねる。|
|同一化|signal 起動 mission を廃止し cron だけにする。|退ける: strategy 足 < 判断足 (例: 15m strategy + 1h 判断) では signal が最大 45 分待たされる。建玉管理の早期起動 (D2) も失う。|

**採用根拠**: (1) 一度だけ引き受ける主体を決定論的に 1 つにできる — claim は既存の単一 UPDATE で原子的 (`signals.py:161-186`)、consume は CAS (`signals.py:206-220`)。cron と signal のどちらの trigger でも同じ `claim_oldest`/`consume` を呼ぶだけで、新規 store 関数は不要。(2) 足がそろう既定 構成 (strategy 1h、判断足 15m または 1h、どちらも epoch 基準) では strategy の確定と cron が同じ tick に来るので、signal は発生直後の cron mission がそのまま扱う。signal 起動は「cron の間に出た signal」(strategy 足 < 判断足、または cron が busy だった場合) だけに残る。(3) 既存の consume/ requeue の規則 (パース成功で consume、失敗で requeue、上限で abandoned) をそのまま使える。 複数 pending の signal (異なる pair・方向を含む) は、1 mission が扱う TradeIntent が 1 件である 現物の出力契約と対応させるため、**claim も最古 1 件に限る**。残りは pending のまま次の cron mission が拾う (§6 の disposition schema 案は退ける)。

**strategy 足 ≠ 判断足のときの signal の意味**: signal は「strategy 足の確定 bucket に対する plugin の判定」であり、判断足の mission への**入力**であって発注指示ではない。有効期間は `bar_ts + tf幅 × signal_freshness_bars` (1h・2 本なら 2 時間)。prompt と get_signals の行に `strategy_timeframe`・`bar_close` (= bar_ts + tf幅)・`fresh_until` を明示する。

**abandoned の付与条件 (明文化、変更なし)**: 鮮度切れ、または requeue 回数の上限。購読後は 「どの mission にも引き受けられないまま鮮度が切れた」(不通・busy 継続) の意味になる。

**失敗時**: cron mission の timeout・max_turns・パース失敗・prepare 例外 → claim 分を requeue (requeue_count+1)。lease 切れ (15 分) の回収も同じ上限判定。claim 後にプロセスが落ちた場合は 起動時の `reclaim_expired` が回収する (`service.py:1187-1191`)。

### 3.2 restart catch-up の一般化と閉場境界

**定義**: `L(pair)` = **処理済み watermark** (last processed decision bar)。受理 (cron mission accepted) だけでなく、閉場 baseline (`_baseline_cron_watermarks`) と前セッション足 skip (IV-3) でも前進する (下記の遷移表)。旧称「最後に受理した判断足」は受理以外の前進経路を含意しないため誤解を招く (`scheduler.py:149-168,375-385` の `_baseline_cron_watermarks` が受理を介さず cursor を進める実例)。 `W(pair)` = `latest_closed_cache_bar_time(pair, 判断足)` (latest closed decision bar)。 `session_start(now)` = 「直近の閉場→開場」遷移の状態を返す関数。単純な曜日パターン (「直近の金曜 21:00 UTC」) では祝日と週末が連結する境界を誤る (下記) ため、 `is_market_open` の状態遷移として定義する。**遷移は必ず 21:00 UTC の rollover 境界でのみ起きる** (`market_hours.py` の `is_market_open`/`trading_day_start`/`next_rollover` は曜日判定・祝日ラベル 判定のいずれも 21:00 UTC 固定の境界だけを見ており、それ以外の時刻では状態が変わらない) ため、 1 分刻みの線形走査は不要: `B = trading_day_start(now)` (直近の 21:00 UTC 境界) から `next_rollover`/`timedelta(days=1)` で 1 境界ずつ過去へ戻り、`is_market_open(B) and not is_market_open(B - 1分)` になる最初の `B` を返す (最悪 72 時間の連続閉場でも 3〜4 境界の探索で足りる)。

**cron due の規則**:

```text
due(pair) = W(pair) is not None and W(pair) > L(pair)
current_session(pair) = due(pair) and W(pair) + width > session_start(now)  # 今セッションの足
previous_session(pair) = due(pair) and not current_session(pair)  # 前セッションの足 (IV-3)
fire = any(current_session(pair) for pair in pairs)  # 1 pair でも今セッションの足が due なら mission は起動する
受理時、mission に含める pair は current_session(pair) を満たす pair だけ。この pair だけ L(pair) := W(pair) (単調 max、現行どおり)
previous_session(pair) を満たす pair は、mission の起動有無に関わらずその pair だけ L(pair) := W(pair)、起動せず activity を 1 回
```

**pair の混在**: 同一 tick である pair は前セッションの足、別の pair は今セッションの足で同時に due になり得る (例: 祝日明けの復旧タイミングが pair によってずれる)。判定と `L` の前進は pair ごとに独立に行う — 前セッション側の pair は mission に含めず個別に遷移③ (skip) を適用し、今セッション側の pair だけを mission に含めて起動する。1 mission が複数 pair を扱う場合、`mission_decision_bars` に書くのも今セッション側の pair だけになる (§3.5)。

- **最大遡及数 = 1 本 (最新の確定足へ合流)**。途中の足は判断しない (現行 coalesce の追認)。
- **L の永続化**: 受理時に DB 表 `cron_cursor(pair, interval, bar_time, updated_at)` へ upsert する (scheduler thread、core_lock 内)。upsert は `bar_time = max(existing.bar_time, excluded.bar_time)` で、後退を書かない。起動時に読み込んで `_cron_watermarks` を初期化する。起動時の読み込みは `Scheduler.__init__` で行う (`store.init_db` によるスキーマ作成が先行しているため、`cron_cursor` 表は読み込み時点で必ず存在する。単一スレッドの初期化中なので他スレッドとの競合もない。`service.py` の起動処理は変更しない)。表の行は `(pair, interval)` 単位なので、将来の複数判断足でも schema を変えずに行が増えるだけ。

**cursor 遷移表**:

|遷移|`L` は前進するか|`cron_cursor` 永続化|dirty (書込み失敗) 時|activity|
|---|---|---|---|---|
|① 受理 (cron mission accepted)|`L := W`|upsert (`max(existing, excluded)`)、core_lock 内|dirty 集合へ追加 (下記)|失敗時のみ `cron_cursor_write_failed`|
|② 閉場 baseline (`_baseline_cron_watermarks`、閉場 tick の入口)|`L := W` (mission は起動しない)|`L` が実際に前進した pair だけ upsert する — 閉場中は毎 tick 呼ばれるが、前回 baseline から `W` が変わっていない pair は書き込みをスキップし、閉場が続く間 `cron_cursor` への無駄な書込みを積み上げない (メモリ側の `_cron_watermarks` は毎 tick 計算し直しても副作用が無いので、比較対象は前回の永続値ではなく前回このループで書いた値でよい)|同上|通常は無し|
|③ 前セッション足 skip (IV-3)|`L := W` (mission は起動しない)|同じ upsert 経路 (② と同じく前進時のみ)|同上|状態変化時のみ `cron_previous_session_bar_skipped`|
|④ dirty 再試行 (前回の upsert 失敗分を次 tick に引き継ぐ)|(既にメモリ側は前進済み)|成功すれば dirty から除去、失敗が続く限り毎 tick 再試行|—|失敗が続く間は tick ごとに 1 回 (「状態変化時のみ」の例外 — 継続失敗を無音にしない)|
|⑤ `L(pair) > W(pair)` (未来値。起動時の復元直後だけでなく、稼働中に他 pair の受理・baseline で `cron_cursor` を読み直した際にも同じ条件で検出し得る)。**復元は 2 段**: 起動時の読み (⑤ なら起動拒否の判定に使う) と、毎 tick の再読込 (max で合流、通常の復元はこちらが包含する。稼働中の ⑤ は hold)|その **pair だけ** 前進を止める (hold)。他 pair の due 判定・受理には影響しない (AC-B2-11)。起動時に検出すれば起動時点から、稼働中に検出すれば検出した tick から hold に入る — 挙動は同一のメカニズム (pair 単位の恒久的な特別扱いではなく、条件が成立している間だけの状態)|しない (fail-fast、upsert とは別ロジック)|—|hold に入った tick で 1 回 `cron_cursor_future_watermark` (pair, L, W を含む)。hold が続く間は再送しない (状態変化時のみ)|
|⑤' hold の解除 (`W(pair)` が `L(pair)` に追いつく、`W ≥ L`)|通常の due 判定 (①) に自動復帰。人手の解除操作は無い — 時計巻き戻し由来なら `W` の前進で自然に解消し、DB 破損 (`W` が恒久的に追いつかない) なら hold が恒久的に続くこと自体が可観測になる|通常の upsert 経路|—|復帰した tick で 1 回 `cron_cursor_future_watermark_resolved` (pair, L, W)|
|⑥ 復元 cursor に対応する `W` が未観測 (`W is None`)|前進も比較もしない (「未検証 cursor」として保留)|しない|—|通常は無し。outage 解消後、最初に `W` が非 `None` になった tick で初めて ⑤/① の判定へ合流|

- **書込み失敗と at-least-once (dirty 再試行)**: queue 受理 (メモリ cursor 前進) と SQLite commit は 原子的にできない。書込み失敗時、IV-2 は「高々 1 回」ではなく **at-least-once** — 同じ足を次回以降の 再起動でも再判断し得る (現行の「再起動のたびに必ず 1 回再判断する」より悪化はしない)。失敗した `(pair, interval)` はプロセスメモリの dirty 集合へ入れ、**同一プロセスが生き続ける限り次 tick 以降も upsert を再試行する** (再起動を待たない)。**再試行の 置き場所は `Scheduler.tick` の既存 `finally:` 節** (`_run_hooks(now)` を呼ぶブロック、 `scheduler.py:236-` 付近) **であり、`on_trade_mission` 受理分岐の中 (`_advance_cron_watermarks` 呼び出し位置、`scheduler.py:258-260`) ではない** — この `finally` は閉場中の早期 `return pending` (`tick()` 冒頭、市場閉鎖時) や `mark_to_market` 失敗時の早期 `return pending` を含む**全ての return 経路**で構造的に実行されるのに対し、受理分岐 (`if reason is not None: … if accepted and reason=="cron":`) は市場が開いていて mission due 判定まで到達した tick でしか実行されない。 dirty 再試行を受理分岐の中に置くと、閉場中・degraded 中 (`state_fn() != "ready"` で `_trade_mission_due` が早期 return する間) は前回の書込み失敗が一切再試行されないまま放置される。`finally` は資金保護 (`_force_close_day`・`_process_exits`、 tick 冒頭で既に実行済み) より**後**に位置するため、資金保護の実行順序は変えない (AC-B2-24)。 成功したら dirty 集合から取り除く。**失敗が続く間 tick ごとに 1 回** activity へ記録する (無音の継続失敗を作らない)。 **不変条件**: SQL 例外後、`conn_core` に未 rollback のトランザクションを残さない。`cron_cursor` の upsert は Python `sqlite3` の `with conn:` 規約 (例外時に自動 `ROLLBACK`、既存の `missions.start`/`finish` と同じ規約、`store/missions.py:14-21`) に従う。
- **`L > W` のとき (未来値)**: `cron_cursor` から読んだ `L` が `W` より新しい (時計巻き戻し・別環境の DB 混入・手動編集を想定) 場合、**その pair だけ** due 判定を止め (hold)、理由を activity に書く (fail-fast、遷移表⑤)。この条件は起動時の復元直後だけで検査するのではなく、`_latest_cron_watermarks` を評価する毎 tick、pair ごとに `L(pair) > W(pair)` を見る — 稼働中に (他 pair の受理や baseline で) 同じ条件が新たに成立しても同じ hold に入る。他 pair の due 判定・受理は影響を受けない (AC-B2-11)。 **解除に人の操作は要らない**: `W(pair)` が `L(pair)` に追いつけば (`W ≥ L`) 次 tick から自動的に 通常の due 判定へ戻る (遷移表⑤')。時計ずれが原因なら `W` の前進で自然に解消し、DB 破損など恒久的な 原因なら hold が解消せず続くこと自体が activity から見える (ログが 1 回で終わらず `hold` が観測期間 ずっと続く)。`upsert` の `max(existing, excluded)` はメモリ内 cursor の後退防止と同じ規律を永続値に も適用するだけで、hold の判定 (読込み時の比較) とは別ロジック (upsert は書込み時)。 **`W is None` のとき**: 起動直後・pair 単位の欠損で対応する確定足が一度も観測されていない `(pair, interval)` は、`L > W` の比較を行わず「未検証 cursor」として保留する (遷移表⑥)。即座に 起動拒否すると正常な不通復旧起動まで拒否し得る一方、比較を飛ばして due 判定へ通すと古い `L` が 後日 cron を無言停止させ得るため、どちらの経路にも入れない。outage state が `ready` でない間は `_trade_mission_due` が最前段で早期 return する (`scheduler.py:333-334`) ため、 この保留は「outage 解消後、最初に `W` が観測されるまで」の窓に限られる。
- **金曜 cutoff との交差**: 金曜 20:45 足は閉場後に確定するので、日曜 21:00:30 の開場 tick で `W=金 20:45 > L=金 20:30` になるが、`W+15m = 金 21:00 ≤ session_start = 日 21:00` なので起動しない。 swing の新規は金曜 NY 14:00 (EDT で 18:00 UTC) 以降 Risk Gate が拒否する (`risk_gate.py:202-204`)。 本書は cutoff 自体を判断足から換算しない。
- **祝日と週末の連結 (12/25)**: 2026-12-25 は金曜 (`date -d 2026-12-25` で確認)。取引日ラベル 12/25 (木 24 21:00 UTC 起点) は祝日として閉場、直後の取引日ラベル 12/26 (金 25 21:00 UTC 起点) は土曜 のため `wd==5` でも閉場 — 金 21:00 UTC の境界で一度も開場せず、木 24 21:00 UTC から日 27 21:00 UTC まで連続閉場になる。「曜日パターンで直近の金曜 21:00 を session_start とみなす」実装は、この連結 区間の途中 (金 21:00) を誤って session_start と判定し得るため、`is_market_open` の状態遷移で定義 する (AC-B2-08)。
- **祝日と週末の連結 (1/1)**: 2027-01-01 も金曜 (`date -d 2027-01-01` で確認)。 取引日ラベルが 2027-01-01 の祝日へ切り替わるのは 2026-12-31 (木) 21:00 UTC — 12/25 と同じ理屈で 木 31 21:00 UTC から日 2027-01-03 21:00 UTC (`date -d 2027-01-03` で確認、日曜) まで連続閉場になる (祝日 1 日 + 金曜ルール + 土曜閉場 + 日曜 21:00 未満、通常の週末 48 時間より丸 1 日長い 72 時間)。
- **day / swing horizon との交差**: horizon は保有期限で、判断足を読まない (§1)。15m では 20:45:31 に建てた day 建玉の強制決済は 20:55:00 で、保有は最大 9 分になる (1h 判断足では最短でも 20:00:31 → 20:55 の約 55 分)。換算規則は入れず、B-2d で prompt に「day の強制決済時刻と残り分」を 出す (§8-8)。
- **A2-3 との関係**: `degraded` 中は cursor を進めない現行規則 (`scheduler.py:159-167,333-334`) を維持する。resume 後の最初の開場 tick は、前セッションの足でなければ最新 1 本で発火する。

**失敗時**: `cron_cursor` の読み込み失敗 → 空の cursor で起動し現行どおり最新 1 本で発火 (activity `cron_cursor_restore_failed`)。書き込み失敗 → mission は受理済みのまま、メモリ cursor は 前進、dirty 集合へ追加して次 tick 以降も同一プロセス内で再試行 (成功するまで、または再起動まで at-least-once)、失敗が続く間は tick ごとに activity へ記録。`L > W` (未来値) → その pair だけ due 判定 を hold、activity に理由付きで記録 (`cron_cursor_future_watermark`)、他 pair は影響を受けず、`W` が `L` に追いつけば人手なしで自動的に通常判定へ復帰する (`cron_cursor_future_watermark_resolved`)。 対応する `W` が未観測 (`None`) → 比較せず保留 (activity は通常無し、outage 解消後の最初の `W` 観測で 判定に合流)。

### 3.3 15m の非重複実行

**方針 (現状の明文化)**: 判断 mission は常に高々 1 本 (supervisor の単一スロット、signal / ask mission も同じスロットを使う)。新しい確定足が busy で受理されなければ cursor は進めず、 空いた最初の tick で**最新の確定足 1 本**に合流する。待ち行列は持たない。

**mission 上限は 2 段に分ける (★ ユーザー確認待ちだが推奨で進める、§8)**: 1 dispatch は trade mission 完了後、**同じ dispatch 内で最大 3 件の reflection を連鎖する** (reflection は trade と同じ runner を使う前提で Cd を trade backend の Cw で見積もる。backend を分ける設計になったら Cd を再計算する) (`MissionSupervisor._dispatch` が `trade_fn` の後に `reflection_fn` を呼び、実体 `ReflectionCycle.run_pending(max_items=3)` は 未 reflect の closed order を最大 3 件処理する。`supervisor.py:166-171`、`reflection_cycle.py:89,159-165`)。 「1 mission (worker 1 回) の上限」と「1 dispatch (trade + 最大 3 reflection) の上限」は別物で、算出のやり直しでは SIGKILL 後の `proc.wait` と CLI 回収猶予も含めて再計算した。

**Cw (worker 1 回の上限)**: 各区間は独立に最大へ達し得る打ち切り経路の直列合計 (保守的な上界)。

|区間|既定値 (秒)|設定キー / 根拠|
|---|---|---|
|子の起動待ち|30|`worker.worker_startup_timeout_sec`|
|実行 deadline|300+30=330|`llama_swap.timeout_sec` + `worker.worker_grace_sec`|
|SIGTERM 猶予 (`_escalate_kill`)|10|`worker.worker_terminate_grace_sec`|
|SIGKILL 後の `proc.wait` (`_escalate_kill`→`_kill`)|5|固定 (`_kill`、`worker_runner.py:704-712`)|
|`finally` の `_ensure_dead`→`_kill`→`proc.wait` 再実行|5|固定 (`worker_runner.py:697-712`)。`_escalate_kill` の `_kill` が既に子を殺していれば `_ensure_dead` は `proc.poll() is not None` で no-op だが、直前の `proc.wait(5)` が期限内に終端を確認できなかった場合はここで SIGKILL と `proc.wait(5)` を再度実行するため、上界にはこの 5 秒も別途積む。|
|`reader.join`|5|固定 (`worker_runner.py:617`)|
|`dispatcher.join` (`rpc_timeout_sec`+5、trade/reflection は kind 別 timeout を使わない)|15+5=20|`worker.rpc_timeout_sec` (`worker_runner.py:628-630`)|
|CLI 回収猶予 (`_terminate_cli_pgid`、backend=claude のみ。local は CLI 子プロセスを持たず該当なし、`worker_runner.py:615-616,714-735`)|+10 (claude のみ)|`runner.cli_terminate_grace_sec`|

`Cw = 30+330+10+5+5+5+20 = `**405 秒 (backend=local、既定)**、backend=claude なら **415 秒** (`+runner.cli_terminate_grace_sec`)。終了を保証するのは worker の deadline と SIGKILL であり、tool の予算や拒否ではない。

**余白の性質差 (実測、`tmp/design-b2/measurements.md` #1)**: 上表の区間はすべて「打ち切り経路の直列合計」として同列に足しているが、実測では性質が異なる。SIGTERM 猶予後の SIGKILL wait・`_ensure_dead` の再実行分・`reader.join` の計 15 秒は、通常の (busy-loop で居座るだけの) 子プロセスでは SIGKILL が捕捉不能なため実質ゼロ (実測 0.001 秒台) しか消費されない — 5 秒級の遅延が起きるのは子が D-state 等で本当に反応しない異常時のみで、正常系のための保守的な安全マージンである。一方 `dispatcher.join` (`rpc_timeout_sec+5`) は、子が `tool_rpc` を投げた直後にハンドラがハングした場合、mission timeout 経路自体が短時間で終わっても `finally` がこの分だけ Cw を実際に押し上げる — 実測でも支配的になり得る実在の経路である (fake 子による実測シナリオ、mission timeout 経路は 1.2 秒で終わるのに `dispatcher.join` の 3.0 秒が総所要時間を決めた)。数値・起動拒否の閾値は変えないが、AC-B2-03 に「RPC を投げっぱなしにする fake 子」のケースを 1 本足し、この経路の回帰を検知できるようにする。

**Cd (dispatch 全体の上限)**: reflection mission も `settings.runner.trade` と同じ backend/model・ `llama_swap.timeout_sec` を使う (`reflection_cycle.py:159,163-164`) ので Cw と同値。 `Cd = Cw(trade) + 3 × Cw(reflection) + 余白 (10、lock/SQL 等の未計測分への保守的な固定上乗せ。 実測値ではない、§9 で fake worker により検証する) = 4×Cw+10 = `**1630 秒 (local)** / **1670 秒 (claude)**。 `try_submit` が busy を返す期間は Cd 全体とほぼ一致する (AC-B2-03)。15m (900 秒) に対し Cw(local)=405 秒は足幅内 (45%) だが Cd(local)=1630 秒は足幅の 181% — 現行運用が回っているのは、 実測 local 平均 2.1 分・claude sonnet 平均 0.8 分 (§1) が Cw の理論上限よりはるかに短く、reflection 対象 (closed order) が毎 dispatch 3 件同時に溜まることが稀なため。

**起動時検査 (B-2a)**: **Cw ≥ 判断足幅のときだけ起動拒否** — 1 mission すら構造的にスロットへ 収まらない設定を弾く。**Cd ≥ 判断足幅は起動拒否しない** — 起動時に WARNING を activity へ 1 回書き、 稼働中は下記の deferred/coalesced event で実際の見送り・合流を観測する。Cd で拒否すると、現行 15m 運用 (Cd(local)=1630 秒 > 900 秒) が実測では 2〜3 分で回っているにもかかわらず起動できなくなる (★ この線引きが変わると本節・§5 の AC をやり直す)。文面に数値を入れる: `判断足 5m (300 秒) に対し worker 1 回の上限 Cw 405 秒 (内訳は上表) が足幅以上です。1 確定足に 1 回の判断が成立しません。 判断足を 15m 以上にするか llama_swap.timeout_sec を下げてください`。`timeout_sec` だけ (300 < 300) や `deadline_budget` だけ (330) の比較では起動待ち・kill wait 再実行分・cleanup 分が漏れ、5m を 誤って許可し得る。

**watchdog 上限の統一**: 現物の `_default_dispatch_ceiling_sec` (`service.py:1392-1396`、§1) は `(timeout_sec+worker_grace_sec+worker_terminate_grace_sec)×4+60=1420 秒` という別式で、Cd (1630/1670 秒) より小さい — 「1 dispatch がスロットを占有し得る上限」を watchdog と起動時検査の 2 箇所で別々に計算しており、値が食い違う。**上限の定義を Cd から導出する 1 本の値に統一する**: `dispatch_ceiling_sec = Cd(backend=runner.trade.backend) + 60` (60 は現行 watchdog と同じ tolerance マージン。Cd 内の余白 10 が dispatch 内部の未計測分、この 60 が watchdog 側の検出遅延余裕という 役割の違いを持つ)。backend=claude なら CLI 回収猶予を含む Cw(claude) 由来の Cd(claude)=1670 を使う ため既定 1730 秒、backend=local なら既定 1690 秒。`_watchdog_check` の `dispatch_ceiling_sec` 引数と `supervisor_join_timeout_sec` (`service.py:1650`) は同じこの値を使う (現状も同じ値を共有しているので 配線は変わらない、式だけ揃える)。値を利用者が上書きできる設定キーにする場合は、**Cd 未満を許容しない 検査を §7 の検査列に持つ** — Cd 未満に設定すると、正常な dispatch の途中で watchdog が発火し `_record_fatal` がサービスを止め得る (§7)。

**構造化 SubmitResult**: `try_submit`/`on_trade_mission` の戻り値を bool から `accepted: bool` / `reason: "queued" | "running" | "shutdown" | None` / `busy_since: float | None` / `checked_at: float` (`self._lock` 保持中に読む `clock_fn()` の値。`busy_elapsed_sec = checked_at - busy_since` で経過時間を出す — scheduler の `now` (壁時計) と `busy_since` (monotonic) は直接引き算できないため、同じ monotonic 時計の値をペアで返す) / `future: Future | None` を持つ構造化結果にする。`ask` 経路 (`_SupervisorAsk.ask_once`、`service.py:657`) は現行どおり `future.result(timeout=…)` を使い続ける — `SubmitResult.future` がそのまま今の `Future | None` を 運ぶ。cron 経路 (`service.py:1077-1081`) は `accepted` だけを見て bool 相当に落とす (現行の `is not None` を `.accepted` に置き換えるだけ)。`MissionSupervisor` は「実行中/予約済み」を `_phase: Literal["idle","queued","running"]` として `self._lock` 下でのみ遷移させ、`try_submit` は `_phase != "idle"` なら `reason` にその時点の `_phase` (`"queued"` は `_run` が `queue.get` する前、 `"running"` は `_dispatch` 実行中) を積んで拒否する。**4 つの値 (`accepted`/`reason`/`busy_since`/`checked_at`) を `self._lock` 保持中に**まとめて 1 回で**読み書きする** — 現行は `_busy` を lock 内で立てる一方 `busy_since` は `_run` 側 (lock 外、`supervisor.py:128,141`) で設定するため、`_busy=True` かつ `busy_since=None` の短い区間が生じ得る (`supervisor.py:61-71`)。`_run` が `queue.get` 直後に `_phase="running"`・`busy_since` の設定を `self._lock` 下で行い、**`_dispatch` 完了後、future を解決する前に** `self._lock` 下で両方をクリアする — 現物 (`supervisor.py:106-141`) は `future.set_result`/`set_exception` の**後**に `finally` でクリアしており、`future.result()` が呼び出し元に返った直後の `try_submit` がまだ busy として拒否され得る潜在欠陥がある (§1)。この順序を入れ替え、future が解決した時点で次の `try_submit` を必ず受理できる状態にする。逆変異は「解決順序を現行のまま (future 解決の後にクリアする) に戻す」だけを行う実装に限る — `finally` 内の future 解決ブロック自体を削除する変異は二重 `set_result` (`InvalidStateError`) という別の壊れ方になり、意図した検出対象ではない。`fail_pending` (shutdown 時に pending Future を例外完了させる 経路、`supervisor.py:79-95`) も同じ `_phase`/`busy_since` をクリアする — 現行はここで `_busy` だけ 戻し `busy_since` に触れないため、shutdown 中に発行された旧 `busy_since` が残留し得る。`time.monotonic` は `MissionSupervisor.__init__` に `clock_fn: Callable[[], float] = time.monotonic` として注入可能に し、AC は fake clock で経過秒数を実時間待機なしに検査する (AC-B2-03)。§10 に `test_service_app.py: 452-463` の `try_submit` spy (`Future` を捕捉するテスト) を SubmitResult 対応へ書き換える task を足す。

**過負荷の可観測性 (B-2a)**: 状態が変わったときだけ 1 回書く (`price_source_fallback` と同じ流儀、 `price_provider.py:166-190`)。busy の `reason` と経過時間は `SubmitResult.busy_since` からそのまま 読める。coalesced の skip 本数は、L と受理した W の間に実在する閉じた足の行数を `store/ohlcv.py:152-175` の `load_cache_bars` で数えて出す (足幅からの格子計算はしない — 休場・ 遅延で欠けた足を「スキップした」と誤カウントしないため)。

|event|書く時|summary の例|
|---|---|---|
|`cron_mission_deferred`|cron が due で `try_submit` が `None` だった watermark ごとに初回だけ|`USDJPY 15m bar=12:30 busy (running 16.0 min)`|
|`cron_mission_coalesced`|受理した watermark と前回 L の間に (DB に実在する) 確定足が 1 本以上あった時|`USDJPY 15m accepted=12:45 skipped=1 (12:30)`|
|`cron_previous_session_bar_skipped`|§3.2 の前セッション規則で起動しなかった時|`USDJPY 15m bar=2026-09-25T20:45 session_start=2026-09-27T21:00`|

**provider 別**: trade backend は local と claude だけ。どちらも Cw/Cd で打ち切られるので provider 別のキーは足さない (backend の違いは Cw への CLI 回収猶予 10 秒の加算だけに反映する)。実測上、 上限に近いのは local (timeout 1 本 5.0 分) で、sonnet ではない。改善 loop は別 supervisor だが、 improve backend が local のときは llama-swap のモデル入替で trade mission が遅れ得る (推測、現稼働は improve=codex なので観測できない)。

### 3.4 `context_timeframes`

**設定**: `datafeed.context_timeframes: []` (既定は空 = 現行と同じ)。要素は `datafeed.intervals` の 部分集合、判断足を含まない、重複なし、を起動時に検査する。list なので将来の拡張を塞がない。

|扱う場所|動作|
|---|---|
|prompt (`_build_prompt`)|「## 判断足」節に `context_timeframes: 1h, 4h (参考足。発注判断の足ではない。確定足だけ)` を足す。空なら行を出さない。|
|tool 応答|`get_ohlcv` の各行と `get_indicators` の dict に `role` を付ける: 判断足 = `decision`、context に宣言 = `context`、それ以外の `intervals` = `other`。enum は `intervals` のまま (狭めない、§8-5)。**取得失敗時 (`insufficient_closed_bars`) の shortage dict にも `interval`・`role` を必須にする** — 現物は成功時と不足時で戻り shape が異なり (`market_tools.py:70-80,102-119`)、shortage 応答には現在 `role` が無い。decision/context/other いずれの足でも失敗時に role が読める必要がある。|
|registry / health|**入れない**。`primary_intervals` は判断足だけ。context 足が取れなくても判断 mission は止めず、tool は既存の `insufficient_closed_bars` を返す (`market_tools.py:53-64`)。registry の reason 欄に載せる必要は生じない (登録しないため)。|
|必要本数|tool の現行値のまま: `get_ohlcv` 100 本、`get_indicators` は max(50, indicator の `max_bars`)。planner が要求時に日数へ換算する (`market_tools.py:44-64`)。|
|保持|tool は primary から直接取る。cache は取得失敗時の予備だけなので、context 用の保持期間は足さない。|

**契約の範囲 (誤用防止ではなく抑制)**: `role` は tool 応答に足の意味を表示するだけで、 executor は intent がどの足を根拠にしたかを検証しない (`executor.py:435-481`)。LLM が `role=context/other` の足だけを理由に OPEN を返しても、現物の gate はそれを弾かない。この設計は 「誤用を表示で抑制する」ところまでで、「誤用を構造的に防ぐ」ことは意図的に含めない — 強制する には TradeIntent に根拠足 (evidence timeframe) を記録し gate で検証する別設計が要り、範囲外の 起票 `[intent-evidence-timeframe-gate]` に切り出す (§2)。

**外向き要求の予算**: context の宣言自体は要求を増やさない。増えるのは LLM が tool を呼んだ回数で、`tool 経由の要求/日/pair ≈ cron mission 数/日 × mission あたりの market tool 呼出し数`。15m では mission 数が 96/日 (1h の 4 倍)。**§9-4 を B-2d 着手前の必須測定にする** — source 別 (yfinance / MT5 bridge) に `missions × pair × calls` の実測値を出してから、日次上限または 警告閾値を設定キー化する (§7 の `datafeed.context_daily_call_budget`、既定値は本書では定めず §9-4 実測後に確定する — 未測定のまま数値を先に決めない)。閾値超過は WARNING を activity へ 1 回書くだけ で tool 呼出しを拒否しない (context は表示のみの契約、IV-5 と整合させる)。**自動 prefetch は無い** — tool は LLM が呼んだ回数だけ primary へ要求する (`market_tools.py:61-68`)。MT5 bridge (ローカル) なら 外部への影響は無いが、yfinance・Twelve Data を primary にする利用者には効くので、ウィザードの説明に 載せる (§3.5)。

**失敗時**: context 足の取得失敗は tool の結果として LLM に返るだけで、health・outage には入らない。

### 3.5 初回ウィザードへの要件と provenance

**ウィザードへ渡す要件 (`[first-run-setup]` で実装。本書は実装しない)**:

|項目|選択肢と既定|説明に必ず含めること|
|---|---|---|
|判断足|15m / 1h (既定 1h)。30m は yfinance にネイティブ足が無い (`sources.py:27-31`) ため未検証 (推測)。4h/1d は B-2a で拒否中のため出さない。|cron mission 数/日 (15m=96、1h=24)、使用中 runner の実測所要時間と Cw (worker 1 回の上限)、配備 strategy の足と違うときの意味 (signal は strategy 足でしか更新されない)、判断回数を増やしても成績は保証されないこと。|
|参考足|`intervals` − 判断足 から複数選択、既定は空|「発注判断の足ではない (表示のみ、強制ではない)」。tool 呼出しごとに primary へ要求が出ること。|
|検査|Cw ≥ 足幅の組合せは選ばせない (Cd ≥ 足幅は選択可、warning のみ)。|拒否の理由を B-2a と同じ文面で出す。|

**provenance (B-2b)**:

|記録先|何を持つか|
|---|---|
|新表 `mission_decision_bars`|列 `mission_id INTEGER, pair TEXT, interval TEXT, bar_time TEXT`。PK `(mission_id, pair, interval)`、FK `mission_id REFERENCES missions(id) ON DELETE CASCADE` (`store/db.py:378` の `connect()` が通常接続で `PRAGMA foreign_keys=ON` を張るため CASCADE は有効に効く — migration 中の一時的な `PRAGMA foreign_keys=OFF` 区間だけは対象外)。**書くのはその cron mission で前進した (due になった) pair のみ** — `L(pair)` の定義 (処理済み watermark) と一致させ、同じ `(pair, bar_time)` が 2 つの mission に付かないようにする。signal / ask mission は行を書かない。値は `TradeLoop` が cron mission の prepare (core_lock 内) で書くが、渡すのは `_pending_cron_watermarks` (due か否かに関わらずその tick に確定済みの `W` を全 pair 分持つ、`scheduler.py:335-336`) そのものではない — `_pending_cron_watermarks` を「前進した pair」だけに絞った部分集合が必要 (下記の snapshot 節)。|
|`missions`|新列は足さない。単数 2 列案 (`decision_timeframe`/`decision_bar_time`) は複数 pair で偽の値を持ち得るため退ける (§6)。|
|`orders`|列は足さない。`orders.intent_id → trade_intents.mission_id → mission_decision_bars` で辿る (mission_id と pair で絞る)。|
|`signals`|`timeframe` は strategy の足のまま。tool 応答では `strategy_timeframe` の名で見せる (列名は変えない)。|
|activity|新設の「mission 開始」event は無い (該当する event が現物に存在しない)。人が読める経路は §3.3 の 3 event (`cron_mission_deferred`/`cron_mission_coalesced`/`cron_previous_session_bar_skipped`) の summary (`<pair> <interval> bar=…`) にとどめ、mission 単位の正確な provenance は `mission_decision_bars` (DB 参照) で持つ。TSV の列は変えない。|
|`cron_cursor`|§3.2 の受理 watermark。`(pair, interval)` 単位の行で、集合を許す形。|

集合への拡張経路: 複数判断足では `mission_decision_bars` の `interval` 列がそのまま複数値を 持てるため、schema 変更は不要 (行が増えるだけ)。

**snapshot の受渡しと単一 transaction**: `_latest_cron_watermarks(now)` は **due か否かに関わらず** 確定済み `W` を持つ全 pair を返し、`_trade_mission_due` はそれをそのまま `self._pending_cron_watermarks` へ代入する (`scheduler.py:335-336`) — 「前進したか (`watermark > cursor`)」は現状ループ内の一時変数 `advanced: bool` としてしか判定しておらず、どの pair が前進したかは捨てている。**provenance を pair 単位で正しく復元するため `_trade_mission_due` を変更し、前進した `(pair, interval): bar_time` の部分集合を別途保持する** (例: `self._due_cron_watermarks` — 全 pair 分の `_pending_cron_watermarks` とは別の属性)。この **前進した pair だけの部分集合**を、その tick に限り immutable な snapshot として `on_trade_mission` の引数経由で `TradeLoop._run_once_impl` まで渡す (現状は `kwargs={"trigger"}` のみ — pair 別 watermark を追加。呼び出し元は `service.py:1077-1081`・`scheduler.py:256-260`)。次 tick で `_pending_cron_watermarks`/`_due_cron_watermarks` が上書きされても prepare 済み mission が見る値は 変わらない。

`TradeLoop` の prepare (`with self._core_lock:`、`trade_loop.py:163-215`) での**確定した書込み順序**: (1) `missions.start(…, commit=False)` (既存の `commit: bool = True` 引数、 `store/missions.py:15,19-20`) → (2) 渡された部分集合の pair 数分 `mission_decision_bars` へ `INSERT` → (3) (1)(2) を**同じ SQLite トランザクションで一括 commit** → (4) (cron mission が signal も 引き受ける場合) `signals.claim_oldest` を呼ぶ — `claim_oldest` 自身が内部で `conn.commit()` する 別トランザクションのため (`signals.py:181-182`)、mission 行と decision_bars 行が確定 (commit 済み) してから claim する順序に固定し、claim 済みなのに mission がまだ commit されていない (または rollback された) 状態を作らない。

(1)(2) の間で例外が出れば `with conn:` 規約により両方とも rollback される — `mission_id` だけ発行 されて子表行が無い不整合を防ぐ (AC-B2-23)。**呼び出し元の `mid` は rollback 経路で構造的に `None` のままにする**: prepare を担う関数 (`_start_cron_mission` 相当) を「mission 行・子表行の commit に成功した場合にのみ `mission_id` を返す」構成にすれば、例外発生時は呼び出し元の `mid` に何も代入されない — `mid = None` を明示的に代入する分岐を別途書く必要はなく、関数の戻り値契約だけで成立する (関数構成に強制力を持たせる規約であり、例外ハンドラでの追加代入ではない)。`missions.start` 自体は commit 前でも SQLite の `lastrowid` を返す (AUTOINCREMENT の 採番自体はロールバックしても再利用され得る — 「新 id 発行」という記述は誤りである) ため、この構成を取らずに `missions.start` の戻り値をそのまま呼び出し元の `mid` として保持すると、 rollback 後も `mid` に整数が残ったままになり、呼び出し元の `finally` (`trade_loop.py:434` `if mid is not None and not finalized: finalize_mission(...)`) が「commit されていない」mission を `failed` として finalize しようとして偽の `mission_finalize_conflict` を書く。 AC-B2-16 (mission id が確定 (commit) し、signal も claim 済みの**後**で例外を注入 — mission は `failed` で finalize され、claim 済み signal は `finally` の `_requeue_signal` により `requeue_count=1` の `pending` へ戻る) と AC-B2-23 (子表 INSERT 中の例外 → rollback、行なし、 `mid=None`、claim はまだ呼ばれていないので signal 側の状態も変化しない) は、この順序に沿って 注入点を分ける。

## 4. 不変条件

|ID|不変条件|
|---|---|
|IV-1|判断 mission (cron / signal / ask) は同時に高々 1 本。busy 中の cron due は cursor を進めず、受理時に最新の確定足 1 本へ合流する。|
|IV-2|同じ `(pair, 判断足, bar_time)` の cron mission は、永続 cursor の書き込みが成功していれば再起動をまたいでも高々 1 回。**書き込みに失敗した場合は at-least-once** — 失敗した `(pair, interval)` は dirty 集合に入り、同一プロセスが生きている限り次 tick 以降も (再起動を待たず) 再試行する。失敗が続く間は tick ごとに activity へ 1 回記録する。|
|IV-3|`W + 足幅 ≤ session_start(now)` の足 (前セッションの足) では cron mission を起動しない。cursor は進める。|
|IV-4|strategy signal は高々 1 つの mission に consume される。claim は既存 `claim_oldest` を cron・signal どちらの trigger でも共用し、1 tick あたり最古 1 件だけを claim する。パース成功で consume、失敗・timeout・prepare 例外で requeue。|
|IV-5|`context_timeframes` は health・`primary_intervals`・registry・cron due に入らない。context の欠損で判断 mission は止まらない。`role` 表示は tool 応答上の助言であり、executor の gate 条件ではない (取得失敗時の shortage 応答にも role を必須にする)。|
|IV-6|判断足の変換 (4h を 1h×4 とみなす等) と horizon の換算は行わない。派生専用足の判断足は起動拒否。|
|IV-7|worker 1 回の上限 Cw (起動待ち + 実行 deadline + SIGTERM 猶予 + SIGKILL 後の wait + join + (backend=claude なら CLI 回収猶予)) が判断足幅以上の設定では起動しない。dispatch 全体の上限 Cd (Cw(trade)+3×Cw(reflection)+余白) が判断足幅以上でも起動は妨げず、起動時 WARNING を 1 回書く。|
|IV-8|見送り・合流・前セッション足の skip は状態が変わったときだけ activity に 1 回書く。毎 tick 書かない (ただし dirty cursor の継続失敗は例外 — IV-2)。coalesced の skip 本数は DB に実在する閉じた足だけを数える。|
|IV-9|既存 settings (`decision_timeframes` 未記載 or `[1h]`、context 未記載) では、cron の発火時刻・回数と prompt の既存節は AC-B2-21 が列挙する意図的な差分 (再起動時の再判断なし・前セッション足 skip・signal claim と provenance の追加) を除き B-1 と同じ。|
|IV-10|`L(pair) > W(pair)` (未来値、起動時の復元直後・稼働中のどちらで検出しても同じ) の間は **その pair だけ** due 判定を hold し、理由を activity に記録する。他 pair は影響を受けない。`W(pair)` が `L(pair)` に追いつけば (`W ≥ L`) 人手の操作なしに次 tick から通常判定へ自動復帰する。永続化の upsert は `bar_time = max(existing, excluded)` でのみ前進する。復元 cursor に対応する `W` が未観測 (`None`) の間はこの比較自体を行わず「未検証 cursor」として保留する。|
|IV-11|`cron_cursor` の upsert・`mission_decision_bars` への書込みを含め、SQL 例外後に `conn_core` へ未 rollback のトランザクションを残さない。|
|IV-12|`SubmitResult` (`accepted`/`reason`/`busy_since`/`checked_at`) は `MissionSupervisor` の同一 lock 内で原子的に生成する。`_busy=True` かつ `busy_since=None` の観測可能な区間を作らない。スロット解放は future の解決より**前**に行い、`future.result()` が返った直後の `try_submit` が拒否される窓を作らない。|

## 5. 受入条件 (逆変異つき)

共通: fake clock、tmp SQLite、fake transport (外向き通信なし)。**service の 1 tick (prepare → commit → observe → tick) を通し**、時刻の値そのものを assert する。 grace 30 秒、tick は毎分 :30 付近。 日付は 2026-09-24 (木)、2026-09-25 (金)、2026-09-27 (日)、2025-12-24〜26 (祝日が週末と非連結、 AC-B2-06)、2026-12-24〜28 (祝日=金曜で週末と連結、AC-B2-08)、2026-12-31〜2027-01-03 (同じ連結パターンの 1/1 版、AC-B2-07。2026-12-25・2027-01-01 は金曜、2027-01-03 は日曜、`date -d` で確認済み)。

|ID|段|正例 (実時刻)|逆変異 (殺すべき実装)|
|---|---|---|---|
|AC-B2-01|a|mission が 12:30:31 に bar=12:15 を受理・12:47:00 終了。**12:30 足が 12:45:30 に確定** (= bar_time + 足幅 15m + grace 30 秒) → 12:45:30 と 12:46:30 の tick は受理されず `cron_mission_deferred` は 1 行だけ、summary に `busy_since` 由来の経過分を含む。12:47:30 に **bar=12:30 で**受理、cursor=12:30。|毎 tick 記録する / 記録しない / 受理前に cursor を進める / `busy_since` を使わず経過分を出さない / 確定時刻を `bar_time + grace` (次の足の開始を足し忘れる) で計算し bar=12:45 を確定済みとして扱う (誤り)。|
|AC-B2-02|a|busy が 12:30:31〜13:02:00。12:30 足と 12:45 足が確定済み → 13:02:30 に 12:45 で 1 本受理、`cron_mission_coalesced skipped=1 (12:30)` (DB に実在する閉じた足を数えた値)、12:30 の mission は 0 本。|中間足も enqueue する / skipped を数えない / 足幅からの格子計算で skipped を出す (欠けた足があると過大カウント)。|
|AC-B2-03|a|fake worker double が dispatch から Cw(local)=405.0 秒後に完了を返す設定で、`try_submit("trade", …)` は受理直後から 404.9 秒後まで `accepted=False, reason="running"`、405.0 秒経過後に `accepted=True` (fake `clock_fn` 注入、supervisor が実際に解放する時刻を検査、算術ではない)。判断足 5m (300 秒) は起動時に Cw 405 秒 ≥ 300 秒で拒否、文面に 5m・405・300 を含む。15m・900 秒は Cw 405 秒 < 900 秒で起動可、かつ Cd(local)=1630 秒 ≥ 900 秒のため起動時 activity に `mission_ceiling_dispatch_warning` を 1 回 (拒否はしない)。watchdog の `dispatch_ceiling_sec` は Cd(local)+60=1690 秒で計算される。backend=claude なら Cw=415・Cd=1670・watchdog=1730 で同じ判定。**別途**: ready 応答後に `tool_rpc` を投げてハンドラをハングさせ続ける fake 子 (RPC 投げっぱなしシナリオ) では、`_escalate_kill`/`_ensure_dead` が完了した後も `finally` の `dispatcher.join(rpc_timeout_sec+5)` が満了するまで `try_submit` は busy のままで、この経路だけで Cw が理論値 (`rpc_timeout_sec+5` 分の上乗せを含む) の範囲に収まり、超えないことを確認する (§3.3 の余白の性質差)。|`deadline_budget` (330) だけで比較する (kill wait 再実行分・CLI 回収を落とす) / 起動待ち・cleanup を検査式に含めない / Cd ≥ 足幅でも起動拒否する (現行 15m 運用が止まる) / 15m を拒否する / `_phase`/`busy_since`/`checked_at` を `self._lock` 外で更新し `accepted=False` かつ `busy_since=None` の区間を許す実装で経過秒数の検査をすり抜ける / watchdog の `dispatch_ceiling_sec` を旧式 (`(timeout_sec+worker_grace_sec+worker_terminate_grace_sec)×4+60`) のまま残し Cd と食い違わせる / RPC 投げっぱなし fake に対して `dispatcher.join` を待たずにスロットを解放する実装 (ハンドラがハングしたまま次の `try_submit` を受理してしまう) / スロット解放を future 解決の後 (現物のまま) にする実装 (`result()` 直後の `try_submit` が拒否され得る窓を残す)。|
|AC-B2-04|a|`decision_timeframes: [4h]` と `[1d]` は起動拒否、文面に「派生専用」と `[1h]` の案内。`[1h]`・`[15m]` は起動。|4h を受理する (cron が永久に発火しない) / 1h も拒否する。|
|AC-B2-05|b|金 20:30:30 受理 (bar 20:15)、20:45:30 受理 (bar 20:30)、20:59:59 は依然開場中で通常どおり due 判定、21:00:00 に `is_market_open` が False へ遷移、21:00:30 閉場で baseline のみ。日 21:00:30 の開場 tick で金 20:45 足が commit されても mission 0 本、cursor=金 20:45、`cron_previous_session_bar_skipped` 1 行。日 21:15:30 に bar 日 21:00 で 1 本。|日 21:00:30 に金曜足で発火 / 日 21:00 足まで skip する / UTC 日付の比較で判定する (日曜 21:00 の足を落とす) / 20:59:59 を既に閉場として扱う。|
|AC-B2-06|b|**祝日が週末と非連結の対照例 (2025 年、Christmas が水曜)**。2025-12-24 (水) 20:45 足は 12-24 21:00:30 に閉場 (取引日ラベルが 12/25 の祝日へ切替わるため) のため未取込。2025-12-25 (木) は祝日で終日閉場、閉場は 1 日だけで済む (週末には連結しない)。2025-12-25 21:00:30 の開場 tick (=木 25 の次、実質は 12-25 21:00 UTC の通常ロールオーバー) で commit されても mission 0 本。12-25 21:15:30 に bar 12-25 21:00 で 1 本。|セッション開始を「日曜 21:00」固定で書く (祝日明けで誤発火)。|
|AC-B2-07|b|**祝日が金曜に当たり週末と連結する例、1/1 版**。2026-12-31 (木) 20:30 足は 20:45:30 に確定・通常受理 (cursor=12-31 20:30)。20:45 足は本来 21:00:30 に確定するはずだが、12-31 21:00:00 UTC (取引日ラベルが 2027-01-01 の祝日へ切替わる瞬間) に ingest が止まり未取込。**2027-01-01 は金曜 (`date -d 2027-01-01` で確認) — 祝日ラベルに加えて金曜ルールでも終日閉場する**。祝日 1 日 + 金曜ルール + 土曜閉場 (2027-01-02) + 日曜 21:00 未満 (2027-01-03) が連結し、12-31 21:00 UTC 〜 2027-01-03 21:00 UTC (`date -d 2027-01-03` で確認、日曜) まで 72 時間連続閉場になる (通常の週末 48 時間より丸 1 日長い)。2027-01-03 21:00:30 の開場 tick で 12-31 20:45 足が commit されても前セッションの足のため mission 0 本 (`cron_previous_session_bar_skipped`)、cursor は最終的に 12-31 20:45 のまま。01-03 21:15:30 に bar 01-03 21:00 で 1 本。|1/1 をチェックせず 12/25 だけ祝日として扱う / 月境界をまたぐ日付比較を誤る / 2027-01-01 (金) 21:00:30 に開場 tick があるものとして金曜最終足を発火させる / 閉場区間を通常の週末 48 時間で固定計算し、祝日で延びる 24 時間を見落とす。|
|AC-B2-08|b|2026-12-24 (木) 20:45 足は木 24 21:00:30 に確定するはずだったが、取引日ラベルが 12/25 の祝日へ切り替わる 木 24 21:00:00 UTC で市場が閉じ未取込 (AC-B2-07 と同じ 72 時間連結パターンの 12/25 版)。取引日ラベル 12/26 (金 25 21:00 UTC 起点) は土曜のため引き続き閉場 — `session_start` を金 25 21:00 UTC と誤判定しない。日 27 21:00:30 の開場 tick まで mission 0 本、cursor は最終的に木 24 20:45 のまま。日 27 21:15:30 に bar 日 27 21:00 で 1 本。|「直近の金曜 21:00 UTC」の曜日パターンだけで session_start を計算し、祝日で閉じたままの金 25 21:00 を開場境界として誤発火させる。|
|AC-B2-09|b|13:00:41 に bar 12:45 を受理 → `cron_cursor` に 12:45 を upsert。13:08:43 に再起動、13:08:44 の tick は W=12:45 で mission 0 本。13:15:30 に bar 13:00 で 1 本。|cursor を復元しない (現行の再判断) / signal mission の行から復元する / `started_at` から復元する。|
|AC-B2-10a|b|(閉場中 tick での dirty 再試行) 13:00:41 に bar 12:45 を受理するが `cron_cursor` への書込みが例外で失敗 → activity に `cron_cursor_write_failed` 1 行、メモリ cursor は 12:45 のまま、`(pair,15m)` が dirty 集合に入る。その後、市場が閉場する tick (`tick()` 冒頭の早期 return 経路) になっても、`Scheduler.tick` の `finally` 節が dirty 分の upsert を再試行する。|dirty 再試行を `on_trade_mission` 受理分岐の中にだけ置き、閉場中の tick (早期 return で受理分岐に到達しない) では一切再試行しない。|
|AC-B2-10b|b|(degraded tick での dirty 再試行) 同じ dirty 状態のまま、outage state が `ready` でなくなり `_trade_mission_due` が最前段で早期 return する tick になっても、同じ `finally` 節が dirty 分の upsert を再試行する。|受理分岐の中にだけ再試行を置き、degraded 中は受理分岐に到達しないため再試行が止まる。|
|AC-B2-10c|b|(継続失敗の可観測性) 書込みが連続 3 tick 失敗する場合、`cron_cursor_write_failed` を tick ごとに 1 回ずつ計 3 回記録する (無音の継続失敗を作らない)。|最初の失敗時だけ記録し以降は無音にする / 1 tick 内で複数回書く。|
|AC-B2-10d|b|(成功後は書かない) 4 tick 目で upsert が成功すると `(pair,15m)` は dirty 集合から除去され、以後は成功後の tick でも activity を書かない。**再起動を挟まなければ**以後同じ足を再判断しない。仮に dirty のまま 13:08:43 に再起動すると (未永続のため復元は前回値 12:30) 13:08:44 の tick で bar 12:45 を再度受理し得る (at-least-once)。|成功後も毎 tick activity を書き続ける / IV-2 を「高々 1 回」のまま実装し失敗を無視する / dirty 再試行を実装せず再起動でしか回復しない。|
|AC-B2-11|b|`cron_cursor` に `(USDJPY, 15m, 13:15)` (現在の `W=13:00` より未来) が残っている状態で起動 → cron 起動を拒否、activity に `cron_cursor_future_watermark` (pair=USDJPY, interval=15m, L=13:15, W=13:00) を記録。EURUSD 側の正常な cursor は影響を受けず通常どおり due 判定する。|未来値を無視してそのまま採用する (該当足を永遠に再判断しない) / 全 pair の起動を止める / 未来値を現在の W に丸めて受理する。|
|AC-B2-12|b|USDJPY=12:45、EURUSD=13:00 が同一 cron mission で due → `mission_decision_bars` に `(mid, USDJPY, 15m, 12:45)` と `(mid, EURUSD, 15m, 13:00)` の 2 行。USDJPY の order を `intent_id → trade_intents.mission_id → mission_decision_bars WHERE pair='USDJPY'` で join すると 12:45 が引ける。|`missions` に単数列で書く (どちらか一方の pair の値で上書きされ、他方が復元不能) / 受理時刻を bar_time に書く。|
|AC-B2-13|c|14:00:30 の tick で bucket 13:00 の strategy signal 生成 → 同 tick の cron mission が `claim_oldest` で 1 件 claim、パース成功で consumed。14:03:30 に `signal_due_fn` は False (pending 無し)。14:15:30 の cron mission の get_signals では status=consumed。|LLM が建てたときだけ consume / claim せず表示だけする。|
|AC-B2-14|c|cron mission が timeout → claim した signal は pending・requeue_count=1。requeue が 3 回目 (count=2) で abandoned。鮮度では 15:00:00 の tick は pending、15:00:30 の tick で abandoned。|失敗でも consume / 鮮度判定を `<=` にする (15:00:00 で落とす)。|
|AC-B2-15|c|pending が USDJPY(bar 12:45)・EURUSD(bar 12:30)・USDJPY(bar 13:00) の 3 件あるとき、cron mission は最古 (EURUSD 12:30) の 1 件だけを claim、残り 2 件は pending のまま次の cron mission へ持ち越される。|全件 claim する (複数 pair の intent を 1 mission で扱おうとする) / 新しい順に claim する。|
|AC-B2-16|c|mission 行・`mission_decision_bars` 行が commit 済み・`claim_oldest` で signal も claim 済みの**後**に例外を注入 (順序: (1)(2)(3) commit → (4) claim → ここで注入) → mission は `failed` で finalize (`mid` は committed 済みの実値のまま)、claim 済み signal は `finally` の `_requeue_signal` で `pending`・`requeue_count=1` へ戻る。次の cron mission で同じ signal が正常に claim される。|例外時に mission を `running` のまま残す / claim 済みなのに finalize せず signal が孤立する (claimed のまま放置) / requeue せず consumed 扱いにする。|
|AC-B2-17|d|`context_timeframes: [1h]`、判断足 15m: prompt に「参考足」「発注判断の足ではない」と `1h`。`get_ohlcv()` の行は interval 15m・role decision、`get_ohlcv(timeframe="1h")` は role context、`"4h"` は role other。**必要本数不足で `insufficient_closed_bars` になった場合も** shortage dict に `interval`・`role` が入る (decision/context/other 各ケース)。|context を判断足として表示 / role を付けない / enum から 4h を外す (互換差) / shortage 応答には role を付けない (成功時にだけ付ける)。|
|AC-B2-18|d|context 1h の足が取れない (source 失敗・cache 無し) 状態でも 15m の cron mission は発火し、healthcheck は 15m だけを見る。|context を `primary_intervals` か health に足す。|
|AC-B2-19|d|context に判断足を含む・`intervals` 外・重複 → 起動拒否 (理由つき)。|黙って除去する。|
|AC-B2-20|d|2026-09-24 20:45:31 開始の mission の prompt に「day の強制決済 20:55:00 UTC (残り 9 分)」。21:00:31 開始なら翌日 20:55:00。|20:55〜21:00 開始の mission で、新規 day 建玉の期限 (当日 20:55) が既に過ぎているのに「残り 0 分」や翌日の時刻を出す (正しくは「新規 day は次 tick で強制決済される」と出す) / 5 分の buffer を引かない。|
|AC-B2-21|全|`decision_timeframes` 未記載・context 未記載の設定 (1h) で、**意図的な差分を列挙**: (a) 再起動をまたいでも `cron_cursor` から復元した足は再判断しない (B-1 の `tests/core/test_scheduler.py:306-315`、旧仕様「再起動のたびに最新 1 本を再判断する」を書き換える対象そのもの)、(b) 日曜開場 tick に前セッション (金曜) の最終足では mission が起動しない (§3.2 IV-3)、(c) cron mission が pending signal を `claim_oldest` で引き受け prompt に signal 節・provenance を追加する (§3.1, §3.5)。**それ以外の cron 発火時刻・回数・prompt の既存節は B-1 と変わらない** (毎正時 :30 の発火リズム、busy 時の coalesce 挙動、通常 tick の trade_reasons 系列)。|(a)(b)(c) を含めて 1h の挙動が一切変わらないと主張する (`test_open_restart_runs_latest_watermark_once` を無改訂のまま green と誤認する) / 逆に (a)(b)(c) 以外の発火リズムまで変えてしまう。|
|AC-B2-22|b|復元 cursor に **行がある** `(USDJPY, 15m, L=12:45)` が、対応する `W(USDJPY,15m)` は未観測 (`None`、その pair だけ確定足キャッシュが空) の状態、かつ outage は **`ready`** (state_fn は早期 return しない) で起動・tick → `_trade_mission_due` は due 判定まで進むが `W is None` のため `L > W` の比較自体を行わず保留 (`cron_cursor_future_watermark` は出ない、mission も起動しない)。EURUSD 側は `W` が観測できているので通常どおり due 判定する。**別途**: state_fn が `ready` でない (MT5 不通) 間は `_trade_mission_due` が最前段で早期 return し比較自体に到達しない (この経路は保留ロジックを経由しないので ⑥ の検証にはならない)。outage が `ready` に戻り最初に `W` (例: 13:00) が観測された tick で、初めて通常の due 判定 (受理 or 前セッション skip) に合流する。|`W is None` を `L > W` の特殊値 (例: 0) として比較し誤って拒否/受理する / outage 未 ready の早期 return だけを検査して ⑥ の比較スキップ自体を検証しない / outage 解消を待たず比較を始める / EURUSD も一緒に保留する。|
|AC-B2-23|b|`missions.start(commit=False)` の直後・`mission_decision_bars` への INSERT 実行中 (claim はまだ呼ばれていない) に例外を注入 → トランザクション全体が rollback され、`missions` に該当行が残らない。呼び出し元の `mid` はこの例外ハンドラで明示的に `None` へ戻され、`finally` の `if mid is not None and not finalized:` は no-op になる (偽の `mission_finalize_conflict` を書かない)。再実行では `AUTOINCREMENT` の採番がロールバック分を再利用し得るため「新しい mission_id が必ず発行される」とは限らない。正常系では `missions` 行と `mission_decision_bars` 行 (pair 数分) が同一 commit で確定する。|`missions.start` だけ即 commit し `mission_decision_bars` の失敗を切り離す (mission_id だけ残る) / 例外を握りつぶして片方だけ書く / rollback 後も `mid` を整数のまま残し `finally` が存在しない mission を finalize しようとする。|
|AC-B2-24|全|(a) `_advance_cron_watermarks` (cron 受理後の cursor 前進、`scheduler.py:258-260`) に例外を注入した tick、(b) `session_start(now)` の判定 (§3.2) に例外を注入した tick、の両方で、同じ tick 内で先行する `_force_close_day`・`_process_exits` (資金保護、`scheduler.py:232,239`) は例外前に実行済みで、day 建玉の強制決済・OPEN の SL/TP 監視が 1 回実行されている。既存の scheduler 安全系テスト (day close / SL・TP / drawdown kill switch) は B-2 の変更後も全数 green。|cursor 永続化・session 判定のコードを `_force_close_day`/`_process_exits` より前に移動する / 例外注入で資金保護がスキップされる / (a) だけ検査し (b) の session 判定側の例外注入を欠く。|
|AC-B2-25|b|AC-B2-11 の続き: `(USDJPY, 15m)` が hold 中 (L=13:15>W)、13:30:30 の tick で `W` が 13:15 まで進む (`W ≥ L`) → 人手の操作なしにその tick から USDJPY の due 判定が通常どおり再開し、`cron_cursor_future_watermark_resolved` が 1 回だけ記録される。以後 hold には戻らない限り再送しない。EURUSD は hold の影響を受けていない。|`W ≥ L` に追いついても hold を解除しない (人手操作を要求する) / 解除の度に activity を毎 tick 書き続ける / hold と無関係な EURUSD の cursor まで一緒に前進させる。|

## 6. 退けた案

|案|理由|
|---|---|
|再起動時に取りこぼした判断足を全部順に判断する (N 本遡及)|過去の足の判断は現在の市場に対して古い。LLM 時間を N 倍消費し、1 本目の結果で状況が変わる。最新 1 本への合流で足りる。|
|cursor を `missions` の `decision_bar_time` の最大から復元する|mission は全 pair を扱うので pair 別の cursor を 1 列に畳めない。`cron_cursor` の行単位の方が将来の複数判断足にもそのまま広がる。|
|signal を「独立維持 + prompt で再提案禁止」で扱う|二度建ての防止を LLM の読解に任せる。決定論的な claim/consume がすでにある。|
|signal 起動 mission の廃止 (同一化)|strategy 足 < 判断足で signal の待ちが最大で判断足 1 本分になる。建玉管理の早期起動も失う。|
|cron mission が pending signal を複数件 (上限つき) 一括 claim し、signal ごとの disposition を TradeIntent 出力 schema に追加する|1 mission が返す TradeIntent は 1 件であり、複数 signal を claim しても発注/HOLD は 1 件分にしかならない。残りを「判断済み」として consume すると異なる pair・方向の signal が実際には未判断のまま失われる。disposition を schema に足すと出力契約と executor の解釈が両方増え、部分成功 (一部 consume・一部失敗) の中間状態も生む。claim を最古 1 件に限れば現物の 1 mission=1 claim=1 intent の対応を壊さずに済む (§3.1)。|
|`missions` に判断足の provenance を単数列 2 本 (`decision_timeframe`/`decision_bar_time`) で持つ|mission は 1 tick で複数 pair を扱い得る (`_pending_cron_watermarks` は pair 別)。単数列では複数 pair が同一 cron mission で due のとき、最後に書かれた pair の値で上書きされ、他の pair の watermark が復元できなくなる (join しても偽の値が返る)。pair 単位の子表 `mission_decision_bars` にすれば schema 変更なしに複数判断足へも拡張できる (§3.5)。|
|context 足を registry に登録し ingest で保持、tool を DB 読みに切替|外向き要求は mission 数に依存しなくなるが、A2-1 の「tool は要求時取得・registry 外」の契約を変える。MT5 ローカル bridge では利得が無い。予算が効く source の利用者が出てから別束。|
|context 足を `primary_intervals` に入れる|参考足の欠損で判断が止まる。health は判断に必須の足だけ。|
|4h 判断足を 1h watermark の 4 本ごとで発火させる|epoch 基準の 4h 境界の導出規則を scheduler に持ち込む換算であり、「安易に換算しない」裁定に触れる。需要が出るまで起動拒否。|
|busy 時に cron を queue に積む|待っている間に次の足が確定するので、古い足の判断が後から走る。合流の方が常に新しい。|
|trade 専用の timeout キーを backend 別に新設|実測で上限に当たるのは local だけで、Cw/Cd と足幅の検査で足りる。キーを増やすと二重設定になる (§8-9)。|
|day 建玉を強制決済の直前は Risk Gate で拒否|判断足から保有期限への換算になる。まず残り時間を見せて観測する (§8-8)。|
|role 表示に加え、executor が発注根拠の足を検証して強制的に gate する|判断ロジックへの介入が大きく、TradeIntent の出力 schema・executor・prompt の 3 箇所を同時に変える必要がある。B-2 の範囲 (時刻モデル・購読・表示) を超えるため `[intent-evidence-timeframe-gate]` に切り出す (§2)。|

## 7. 設定キー

|キー|段|既定|検査|
|---|---|---|---|
|`datafeed.context_timeframes`|d|`[]`|`intervals` の部分集合、判断足を含まない、重複なし。違反は起動拒否。|
|(新キーなし) Cw ≥ 足幅の検査 (起動拒否)|a|—|既存 `llama_swap.timeout_sec`・`worker.worker_startup_timeout_sec`・`worker.worker_grace_sec`・`worker.worker_terminate_grace_sec`・`worker.rpc_timeout_sec`・`runner.cli_terminate_grace_sec` (backend=claude のときのみ) から計算 (固定の `reader.join(5.0)`・SIGKILL 後 `proc.wait(5)`・その再実行分 5 秒を含む)。|
|(新キーなし) Cd ≥ 足幅の検査 (起動時 WARNING のみ)|a|—|`Cw(trade) + 3×Cw(reflection) + 10 (余白、固定)`。拒否はしない。|
|(新キーなし) 派生専用足の判断足拒否|a|—|`decision_timeframes` が `4h`/`1d` なら起動拒否。|
|`worker.dispatch_ceiling_sec`|a|`None` (未設定時は `Cd(runner.trade.backend) + 60` を自動導出、既定 1690 秒 local / 1730 秒 claude)|watchdog (`_watchdog_check`) と `supervisor_join_timeout_sec` が共有する単一の上限。明示設定する場合は `Cd(runner.trade.backend)` 未満を拒否 (起動拒否) — 未満だと正常な dispatch の途中で watchdog が発火し `_record_fatal` がサービスを止め得る。|
|`datafeed.context_daily_call_budget`|d|未設定 (無制限)|source 別 (yfinance/MT5 bridge) の日次上限または警告閾値。**既定値は §9-4 の実測前は定めない** — B-2d 着手前に §9-4 を実施し、実測を根拠に確定する。超過は WARNING のみ、tool 呼出しは拒否しない。|

`config/settings.yaml.example` には `context_timeframes: []` とコメント「参考足。発注判断の足ではない。 判断足を含めない (表示のみ、強制ではない)」を足す。`worker.dispatch_ceiling_sec` は既定 `None` (自動導出) のままコメントで式を示し、明示値は必要になるまで書かない。`context_daily_call_budget` は §9-4 実測後に確定した既定値と共に B-2d で追加する (本書の時点ではキー名の予約のみ)。個人の `settings.yaml` は書き換えない。DB 表 `cron_cursor`・`mission_decision_bars` は設定キーにしない (内部状態)。

## 8. 人間の裁定が要る点

**裁定済み事項**: ★ が付いた項目は本書の承認をもって確定する。

1. 判断足は当面 1 個 (list で 2 個以上は起動拒否)。mission 周期は判断足の幅、新しい確定足のときだけ cron が起動する。複数判断足・両建ては `[multi-decision-timeframes]` に送る。horizon は判断足から換算しない。MT5 不通は稼働停止とし、fallback は明示時だけ許す。ウィザードの実装は `[first-run-setup]` に送る。
2. 複数 signal の一括 claim・出力 schema への disposition 追加は行わない。claim は既存 `claim_oldest` のまま最古 1 件に限り、現物の 1 mission=1 claim=1 intent の対応を保つ。
3. mission end-to-end ceiling は `deadline_budget` (timeout + grace) だけでなく、起動待ち・SIGTERM 猶予・reader/dispatcher の join を含めて定義し、起動時検査もこの値で行う。AC は算術ではなく fake worker で実際の supervisor 解放時刻を検査する。
4. provenance は `missions` の単数 2 列ではなく、pair 単位の子表 `mission_decision_bars(mission_id, pair, interval, bar_time)` を B-2b で新設する。
5. cursor 書込み失敗時の IV-2 は「高々 1 回」ではなく at-least-once に弱め、書込み失敗 × 再起動を繰り返す AC を追加する。
6. 永続 cursor の復元値が現在の確定足より未来なら、起動を拒否し理由を記録する。upsert は既存値と新値の max でのみ前進させる。
7. `try_submit`/`on_trade_mission` の戻り値を bool から accepted/reason/busy_since の構造化結果にし、deferred の経過分は既存の `Supervisor.busy_since` から出す。coalesced の skip 本数は DB に実在する閉じた足だけを数える。
8. `context_timeframes` の role 表示は「誤用を構造的に防ぐ」契約ではなく「意味を表示して抑制する」助言にとどめる。強制する設計は範囲外の起票 `[intent-evidence-timeframe-gate]` に切り出す。
9. 閉場境界の `session_start` は曜日パターンの固定文言ではなく、`is_market_open` の閉場→開場の状態遷移として定義する。1/1・祝日と週末の連結・境界ちょうど (20:59:59/21:00:00/21:00:30) を AC に加える。
10. B-2a の単独投入理由は「現に動かない設定だけを拒否する」ではなく、「1 確定足に 1 回」という製品契約を明文化・強制する互換差として記述する (5m は現行でも coalesce しながら起動し得る)。
11. §9-6 (busy 頻度の測定) は現行 DB からは busy を識別できないため、B-2a の `cron_mission_deferred` 導入後に測る対象へ置き換える。4h/1d の cron 不発は静的読取りで確定済みのため測定項目から外す。
12. **★**: mission 上限を worker 1 回の Cw と dispatch 全体の Cd に分離する。起動拒否は Cw ≥ 判断足幅のときだけ。Cd ≥ 判断足幅は起動時 WARNING + 稼働中は activity 観測にとどめ、拒否しない (現行 15m 運用は Cd(local)=1630 秒 > 900 秒でも実測 2〜3 分で回っており、拒否すると現稼働が止まる)。この線引きが変わると §3.3・AC-B2-03 をやり直す。watchdog 上限も Cd から導出する 1 本の値に統一する。
13. AC-B2-07 を 2027-01-03 (日) 21:00 UTC 再開まで延ばす。2027-01-01 は金曜のため木 31 21:00 UTC 〜 日 01-03 21:00 UTC の 72 時間連続閉場 (通常の週末より丸 1 日長い)。AC-B2-08 (2026-12-25) は数値の修正は不要。
14. dirty 再試行 (同一プロセス内で次 tick 以降も再試行) と `conn_core` の rollback 規律を §3.2 に明記する。再起動せず次 tick で永続化される挙動を AC-B2-10 に含める。
15. watermark snapshot を `on_trade_mission` の引数経由で不変に受け渡し、`TradeLoop` prepare で `missions.start(commit=False)` + `mission_decision_bars` INSERT を単一トランザクションにする。rollback 試験を AC-B2-23 に追加する。
16. `L` を「処理済み watermark」と定義し直し、受理・閉場 baseline・前セッション skip の 3 遷移を表で明記する。
17. AC-B2-01 の確定足時刻を 1 区間補正する (12:45:30→bar 12:30、13:00:30→bar 12:45)。他 AC は個別に点検済みで追加の誤りは無かった。
18. `SubmitResult` を supervisor の同一 lock 内で原子的に生成し、`time.monotonic` を注入可能にして実時間待機なしに AC 化する。
19. 復元 cursor に対応する `W` が未観測 (`None`) の間は「未検証 cursor」として保留し、outage 解消後・最初の `W` 出現時に判定へ合流させる。
20. 実 DB の実測は「日曜 21:0x の `trigger='cron'` mission の実在」までに限定する (§9-2)。bar の同定はログ/transcript の証拠がある場合だけ行う。
21. 既存 scheduler 安全系テストの全実行を AC に明記する。cursor/session 判定に例外を注入しても `_force_close_day`・`_process_exits` が先行して実行される回帰 AC (AC-B2-24) を追加する。
22. §9-4 (tool 呼出し量の実測) を B-2d 着手前の必須測定にする。source 別の実測値と日次上限/警告閾値キー (`datafeed.context_daily_call_budget`) は §9-4 の後に確定する。
23. 取得失敗 (`insufficient_closed_bars`) の shortage 応答にも `interval`・`role` を必須にする。
24. watchdog の上限 (`_default_dispatch_ceiling_sec`=1420 秒) は Cd (1630/1670 秒) と食い違っていた。上限を Cd から導出する 1 本の値 (`worker.dispatch_ceiling_sec`、既定 `Cd+60`) に統一し、claude backend の CLI 回収猶予も Cw(claude) 経由でこの値に含める。
25. `MissionSupervisor` を `accepted`/`reason`/`busy_since`/`checked_at`/`future` の `SubmitResult` にし、`_phase` 遷移も含めて `self._lock` 下で原子的に更新、`fail_pending` も `busy_since` をクリアする。ask 経路は `SubmitResult.future` を使い続ける。スロット解放は future 解決の前に行う (現物は解決後で潜在的な拒否窓がある、§1・§3.3)。
26. `L(pair) > W(pair)` の稼働中動作を明文化する: その pair だけ due 判定を hold し、`W ≥ L` に追いつけば人手なしで自動再開する。起動時に検出した場合も同じメカニズム。
27. dirty 再試行の置き場所を `on_trade_mission` 受理分岐の中から `Scheduler.tick` の既存 `finally` 節 (全 return 経路をカバー) へ移す — 閉場中・degraded 中は受理分岐に到達しないため。
28. AC-B2-21・IV-9 を「意図的な差分 (再起動時の再判断なし・前セッション足 skip・signal claim と provenance の追加) を列挙し、それ以外は B-1 と同じ」に書き直す — 「完全に同じ」は既存の再判断 pin テストの書き換えと矛盾する。
29. `mission_decision_bars` に書くのは前進した (due になった) pair だけにする。`_pending_cron_watermarks` は due か否かに関わらず全 pair の `W` を持つため、`_trade_mission_due` に前進した pair だけの部分集合を別途保持させる配線を追加する。
30. `TradeLoop` prepare の書込み順序を確定する: `missions.start(commit=False)` → `mission_decision_bars` INSERT → commit → `claim_oldest` (`claim_oldest` 自身が別トランザクションで commit するため)。rollback 時は Python 側の `mid` も明示的に `None` へ戻し、`finally` の偽 `mission_finalize_conflict` を防ぐ。AC-B2-16 (claim 後の例外) と AC-B2-23 (子表 INSERT 中の例外) の注入点を分ける。
31. §9-1 は「実 `WorkerRunner` + SIGTERM を無視する fake 子プロセスで打ち切り経路を実時間で測る」を対象にする。config 値の単純合計は算術で確定済みとし、OS レベルの打ち切り経路 (SIGKILL 後の `proc.wait` 再実行など) だけを実測対象にする。
32. AC-B2-08 の日付誤記 (未取込の足は「12-25 (金)」ではなく「12-24 (木)」) を修正する。§9 表 2 行目は「B-2 適用前の実データ裏取り」であることが分かる文言にする。AC-B2-24 に session 判定側の例外注入を追加する。`_baseline_cron_watermarks` の upsert は前進した pair だけ行う。`session_start` は 21:00 UTC の rollover 単位でのみ状態が変わることを使い、1 分刻みではなく境界単位で遡る。Cw に SIGKILL 後 `proc.wait` の再実行分 5 秒を追加し 405/415 秒、Cd を 1630/1670 秒に再計算する。§10 に `backtest/runner.py:419` と snapshot 経路 3 か所 (`supervisor.py:168`・`service.py:1001-1006`・`trade_loop.py:91`) を追加する。`mission_decision_bars` の PK を `(mission_id, pair, interval)`、FK を `missions(id) ON DELETE CASCADE` にする (`store/db.py:378` の `PRAGMA foreign_keys=ON` が通常接続に効いているため CASCADE は有効)。
33. §10 に `on_trade_mission` (52 か所・16 ファイル、src 内訳 scheduler 5/service 3/backtest 1)・`try_submit` (51 か所・6 ファイル)・`TABLE_NAMES` pin 2 本・cursor 復元位置の差し替え箇所の全数を記載し、実装プランの入力にする。

**未裁定 (実装前に選ぶ)**:

1. **signal と cron の関係**: 購読・最古 1 件 claim (推奨、§3.1) / 独立維持 + 表示 / 同一化。
2. **閉場明けの前セッション足**: 起動せず消費だけ (推奨、§3.2) / 現行どおり日曜 21:00 に金曜最終足で 1 本起動。
3. **再起動時の再判断**: `cron_cursor` で判断済みの足を再判断しない (推奨、書込み失敗時は at-least-once) / 現行どおり再起動ごとに最新 1 本を再判断。
4. **4h / 1d の判断足**: 当面起動拒否 (推奨) / 1h watermark からの導出を実装する (§6 の換算に当たる)。
5. **context 足の tool enum**: `intervals` のまま + role 表示 (推奨、互換) / `{判断足} ∪ context` に狭める (互換差: 未宣言の足が取れなくなる)。
6. **provenance を今入れるか**: `mission_decision_bars` を B-2b で入れる (推奨、pair 単位で正確) / `[multi-decision-timeframes]` まで待つ (その間、判断足は DB から復元できない)。
7. **配備 strategy `sma_cross_10_30` (1h)**: 1h のまま 15m 判断足で使う (推奨: 購読で二度建ては塞がる) / 15m 版を作る (`max_bars: 400` = 15m×400 = 100 時間、planner 換算で約 9 日分、保持 30 日で足りるが承認のやり直しが要る)。利用者の判断。
8. **day 建玉の短命化 (15m では 20:45:31 → 20:55 の 9 分)**: 残り時間を prompt に出すだけ (推奨) / Risk Gate で残り N 分未満の day open を拒否。
9. **trade mission の上限キー名**: `llama_swap.timeout_sec` を claude backend にも使う現状を文書化だけする (推奨) / trade 専用キーへ移す (移行と二重設定の検査が要る)。

## 9. 実装前に測る項目

外向き通信なし (tmp SQLite・fake・実 DB の読み取り専用集計は設計側が行う)。**設計を覆し得る順に 並べる** (Cw/Cd の実測を先頭に): Cw/Cd の値そのものが変わると §3.3 の起動 拒否閾値・AC-B2-03 をやり直す必要があり最優先。次いで閉場境界・signal 多重性・tool 呼出し量の 実測。静的読取りで既に確定した項目 (4h/1d の cron 不発) は対象から外す:

|#|項目|方法|効く先|
|---|---|---|---|
|1|**実 `WorkerRunner`** + `SIGTERM` を無視して居座る fake 子プロセス (`os.fork`/subprocess スタブ、実際の handshake フレームだけ最小実装) で、`_escalate_kill`→`_ensure_dead`→`reader.join`→`dispatcher.join` の打ち切り経路を実時間で計測する (local/claude 両 backend、`MissionSupervisor._dispatch()` は fake `trade_fn`/`reflection_fn` で trade+最大 3 reflection を通す)。**Cw/Cd の各区間のうち config 値の単純合計 (起動待ち・実行 deadline・SIGTERM 猶予・`rpc_timeout_sec` 由来の join 上限) は算術で確定済みとし、測定するのは「SIGKILL 後に本当に `proc.wait` が 2 回発生し得るか」「`reader`/`dispatcher` の実際の join 所要時間が理論上限に収まるか」の実測でしか確かめられない部分に絞る**。「指定秒後に完了を返す fake worker」は supervisor 層の時間計算 (fake clock) しか通さず、この OS レベルの打ち切り経路を一切 exercise しないため測定として成立しない。|tmp 環境、実 subprocess + fake clock は使わない (実時間計測)。外部通信なし|§3.3 の Cw の「SIGKILL 後 wait 再実行」区間の実測裏取り・AC-B2-03 の fake worker 設計。B-2a 着手前必須|
|2|**現行 (B-2 適用前) の**日曜 21:0x UTC 開始の `trigger='cron'` mission が実在し、前セッション (金曜) の最終足で起動していたか — B-2b が塞ぐ対象を実データで裏取りする (修正後の期待は「起動しない」で、これは AC-B2-05 が検証する)|実 DB `missions` の read-only 集計 (2026-09-15 以降の日曜 21:00〜21:15)。無ければ AC-B2-05 の fake で再現|§3.2 の推測 (前セッション足の誤発火) を事実にする|
|3|15m 判断足で cron mission が見た strategy signal を signal mission が再 claim した実例|実 DB の `signals.claimed_by_mission_id` と `missions.trigger` の read-only 突合|§3.1 の優先度|
|4|mission あたりの market tool 呼出し数 (get_ohlcv / get_indicators、足別)|実 DB `missions.transcript_json` の read-only 集計|§3.4 の外向き予算式の係数、`datafeed.context_daily_call_budget` の既定値。B-2d 着手前必須|
|5|`cron_cursor` の upsert が core_lock 内の tick 時間に与える影響|tmp SQLite で 1000 tick|B-2b|
|6|busy 見送りの頻度|B-2a で `cron_mission_deferred` を導入した後、実 DB の該当 activity 行を集計する (現行 DB には拒否された submit が残らず、受理時刻だけからは busy を識別できないため今回は測らない)|§3.3 の event の重要度 (次段の観測)|

bridge や外部 source を叩く測定は無い。

## 10. 影響ファイルと呼び出し元

|ファイル|段|変更|
|---|---|---|
|`src/agentic_fx/core/scheduler.py`|a,b|見送り・合流 event (構造化 `on_trade_mission` 結果の消費)、`session_start` を使うセッション規則、cursor の永続化・復元・未来値 fail-fast・`W is None` 保留・dirty 再試行、`_pending_cron_watermarks` を pair 別 snapshot として mission へ渡す配線。|
|`src/agentic_fx/core/market_hours.py`|b|`session_start(now)` (既存 `is_market_open`・`next_rollover` の状態遷移として実装)。|
|`src/agentic_fx/core/supervisor.py`|a|`try_submit`/`_run` を `SubmitResult` (accepted/reason/busy_since) の同一 lock 内での原子的な生成に変更、`clock_fn` (既定 `time.monotonic`) を注入可能に。|
|`src/agentic_fx/service.py`|a,b|起動時の Cw/Cd 検査 (Cw は拒否、Cd は WARNING)・派生専用足拒否、`on_trade_mission` の戻り値を構造化。cursor 復元は `Scheduler.__init__` (`init_db` が先行) で完結するため、この行に配線の変更はない。|
|`src/agentic_fx/config.py`, `config/settings.yaml.example`|a,d|`context_timeframes`、検査。|
|`src/agentic_fx/store/db.py`, 新 `store/cron_cursor.py`, 新 `store/mission_decision_bars.py`|b|`cron_cursor` 表・`mission_decision_bars` 表の migration、`TABLE_NAMES`。`missions` の列は変更しない。|
|`src/agentic_fx/loops/trade_loop.py`|a,b,c,d|cron mission での `claim_oldest`/`consume`/requeue 呼び出し、`mission_decision_bars` への書込み、prompt 節 (signal・context・day 期限)。|
|`src/agentic_fx/tools/signal_tools.py`, `tools/market_tools.py`|c,d|`strategy_timeframe`・`bar_close`・`fresh_until`、`role`。|
|`src/agentic_fx/backtest/runner.py`|a|`on_trade_mission=lambda reason: None` (`:419`) を新しい戻り値契約 (bool ではなく accepted 相当) に合わせて更新。B-2b の pair 別 watermark 引数は backtest には無い (バックテストは cron watermark を進めない) ため `None`/no-op のまま。|
|テスト|全|scheduler / service tick / store / trade_loop / tools。実 DB を触らない (conftest の session ガードを使う)。既存 `tests/core/test_scheduler.py:306-315` は再起動での再判断 (現行) を pin しているので、§8-3 の裁定に合わせて B-2b で書き換える。|

**呼び出し元の全数 (実装前に grep で数え直す前提の現状値)**: `on_trade_mission` は `grep -rn` で 52 か所・16 ファイル (src 9: `scheduler.py` 5 [`:52,68,258,309,312`]・`service.py` 3 [`:1077,1177,1602`]・`backtest/runner.py` 1 [`:419`]、残りはテスト 13 ファイル)。`try_submit` は 51 か所・6 ファイル (src 2: `service.py`・`supervisor.py`、テスト 4 ファイル)。`ask` 経路 (`service.py:657` の `_SupervisorAsk.ask_once`) は `future.result()` を呼ぶため `SubmitResult.future` をそのまま使う。cron 経路 (`service.py:1077-1081`) は `accepted` だけを見る。

**snapshot 受渡しの経路 3 か所 (§3.5 の pair 別 watermark を通す配線)**: `MissionSupervisor._dispatch` が `self._trade_fn(kwargs["trigger"])` を呼ぶ箇所 (`supervisor.py:168`、`kwargs` に watermark 分を 追加) → `service.py` の `_trade_fn` クロージャが `trade_loop.run_once(trigger)` を呼ぶ箇所 (`service.py:1001-1006`、引数追加) → `TradeLoop.run_once(self, trigger=…)` が `self._run_once_impl(trigger)` を呼ぶ箇所 (`trade_loop.py:91`、引数追加)。3 か所とも「今は `trigger` 1 個だけを転送している」ため、pair 別 watermark 引数を通す変更が要る。

**TABLE_NAMES pin 2 本**: `tests/store/test_db.py:27`・`tests/store/test_db_migrations.py:31` (`TABLE_NAMES == frozenset({...})` の網羅リスト)。`cron_cursor`・`mission_decision_bars` を追加する migration は既存の `_ensure_column`/`_SCHEMA` 追記パターン (前例 `db.py:1360` 付近) で足りる。

**`try_submit`/`on_trade_mission` の bool 契約が pin されているテスト**: `tests/core/test_scheduler.py` の `_trade` ヘルパ (`:107-110`、`return True # on_trade_mission は bool を返す`)・busy/coalesce 経路 (`:189-196`、`lambda reason: False`/`lambda reason: … or True`)・例外時の bool 契約コメント (`:2935-2968`) を `SubmitResult` 対応へ書き換える (概算 15 か所、実装着手時に再 grep して確定する)。 `tests/test_service_app.py:452-463・506-517・635-647` の 3 本の `try_submit` spy (`Future` を捕捉) も `SubmitResult.future` を捕捉する形へ書き換える。watchdog 統一に伴い `tests/test_stop_sequence.py:57` (`_default_dispatch_ceiling_sec` の旧式 `(42+3+2)*4+60` を pin) と `tests/test_service_app.py:1409-1441` (`test_run_service_derives_supervisor_join_timeout_from_dispatch_ceiling`。`_seam_app` の設定 (`llama_swap.timeout_sec=42.0`・`worker_grace_sec=3.0`・`worker_terminate_grace_sec=2.0`、`worker_startup_timeout_sec`=既定 30・`rpc_timeout_sec`=既定 15・backend=local) から `Cw=30+(42+3)+2+5+5+5+(15+5)=112`、`Cd=112×4+10=458`、pin を `pytest.approx(112.0*4+10.0+60.0)` (=518.0) に書き換える) を新式 (`Cd+60`) に書き換える。`tests/test_service_app.py:1487-1589` の複数テストは `_default_dispatch_ceiling_sec` の式そのものを pin しておらず (順序関係の確認、または patch で値を固定するテスト)、この式変更による書換えは不要。 `busy_since` を lock 内に移す変更 は `tests/core/test_supervisor.py:187` (`test_busy_since_is_set_during_dispatch_and_cleared_after`) の観測点に影響するため、`_phase` の 遷移も合わせて検査するよう更新する。

## 11. task 分割素案

```text
[B-2a 観測と構造検査]  Ta1 Cw/Cd 検査 (Cw 拒否・Cd warning) + 派生専用足拒否 ─┐
                        Ta2 構造化 SubmitResult (busy_since) + deferred/coalesced event ─┴→ Ta3 統合 (service tick)
[B-2b 時刻モデル]      Tb0 spike (§9-2, §9-5) → Tb1 session_start + セッション規則 ─┐
                        Tb2 cron_cursor 表 + mission_decision_bars 表 (at-least-once・dirty 再試行・W=None 保留・未来値 fail-fast 含む) ┴→ Tb3 復元配線 + 単一トランザクション + 統合
[B-2c signal 購読]     Tc0 spike (§9-3) → Tc1 trade_loop の cron claim (claim_oldest 共用)/consume/requeue + prompt → Tc2 統合
[B-2d context と期限]  Td1 config + example → Td2 prompt 節 + tool role (shortage 応答含む) → Td3 day 期限表示 → Td4 統合
```

|task|主な AC|備考|
|---|---|---|
|Ta1|AC-B2-03, 04|起動拒否の文面を利用者が読める形で。Cw/Cd は §9-1 の実 WorkerRunner 実測を先に済ませる。watchdog `dispatch_ceiling_sec` を Cd から導出する式に統一 し、`tests/test_stop_sequence.py:57`・`test_service_app.py:1487-1589` を書き換える。|
|Ta2, Ta3|AC-B2-01, 02, 21 (段 a で通すのは「1h の発火リズム・回数が B-1 と同じ」ことの確認のみ。(a)(b)(c) の意図的差分は Tb2/Tb1/Tc1 で通す), 24(a)|event は状態変化で 1 回。coalesced は DB 実在足だけ数える。AC-B2-24(a) (cron 受理後の cursor 前進側への例外注入、資金保護の実行順序回帰) はここで通す — (b) の `session_start` 判定側への例外注入は `session_start` 新設後の Tb1 で通す。`try_submit`/`on_trade_mission` を `SubmitResult` 化し、§10 に挙げた `tests/core/test_scheduler.py` の bool 契約サイト (概算 15 か所)・`test_service_app.py:452-463・506-517・635-647` の spy・`test_supervisor.py:187` の `busy_since` 観測をここで書き換える。|
|Tb1|AC-B2-05, 06, 07, 08, 21(b), 24(b)|1/1・祝日+週末連結を週末と同じ規律で。AC-B2-07 は 1/3 再開まで、AC-B2-06 は非連結の対照例、AC-B2-08 は日付誤記 (12-24木) を修正済み。AC-B2-21(b) (前セッションの足では起動しない) と AC-B2-24(b) (`session_start` 判定側の例外注入でも資金保護が先行する回帰) は `session_start` 新設後のここで通す。|
|Tb2, Tb3|AC-B2-09, 10, 11, 12, 21(a), 22, 23, 25|migration は既存行を変更しない (新表のみ追加、`TABLE_NAMES` pin 2 本を更新)。AC-B2-21(a) (再起動をまたいだ再判断なし)・AC-B2-22 (W=None 保留)・AC-B2-23 (単一トランザクション rollback、`mid=None` は構造的に成立)・AC-B2-25 (hold からの自動復帰) をここで通す。`mission_decision_bars` へは前進した pair だけを書く配線 (§3.5 の snapshot 部分集合) をここに含める。cursor 復元は `Scheduler.__init__` (`init_db` が先行) で行い、`service.py` の起動処理は変更しない。|
|Tc1, Tc2|AC-B2-13, 14, 15, 16, 21(c)|§8-1 の裁定後に着手。新規 store 関数は不要 (`claim_oldest` 共用)。claim は mission commit の後に行う順序に従う。AC-B2-21(c) (signal claim と provenance の追加) はここで通す。|
|Td1〜Td4|AC-B2-17〜20|§8-5, 8 の裁定後に着手。§9-4 (tool 呼出し量) の実測後に `context_daily_call_budget` を確定する。|

各段は他段に依存しない (b の provenance は c・d が無くても成立、c は b の cursor 永続化が無くても 成立)。ただし b は scheduler・store に加え service.py の起動処理と trade_loop.py の prepare 配線を 含む (scheduler・store だけでは provenance を渡せない)。推奨順は a → b → c → d (観測を先に入れて 15m の実運用を読めるようにするため)。

## 変更履歴

|日付|版|変更|理由|commit|
|---|---|---|---|---|
|2026-09-25|v0.1|初稿。spec B §4 の 5 項目を現物 (`ada582b`) と `tmp/design-b2/premises.md` に照らして設計。金曜最終足の閉場明け発火 (推測)、4h 判断足の cron 不発 (推測)、signal の二度引受けを新たに特定し、4 段 (a 観測 / b 時刻モデル / c 購読 / d context) に分割。|B-2 設計の依頼、前提の再検証|—|
|2026-09-25|v0.2|複数 signal の一括 claim・disposition schema 案を撤回し claim を最古 1 件に限定 (§3.1, §6)。mission 上限を end-to-end ceiling (既定 395 秒) に拡張し起動検査とAC を実測ベースに変更 (§3.3, §5)。provenance を `missions` 単数 2 列から pair 単位の子表 `mission_decision_bars` へ変更 (§3.5, §6)。cursor 永続化の IV-2 を at-least-once に弱め、未来値 fail-fast の不変条件・AC を追加 (§3.2, §4, §5)。`on_trade_mission`/`try_submit` を構造化結果にし `busy_since` を配線、coalesced は DB 実在足を数える方式に変更 (§3.3)。`context_timeframes`/role の契約を「誤用防止」から「表示による抑制」に弱め、`[intent-evidence-timeframe-gate]` を範囲外に起票 (§2, §3.4, §6)。`session_start` を状態遷移として定義し 1/1・祝日と週末の連結・境界秒の AC を追加 (§3.2, §5)。§2/§11 の段別説明を現物 (5m でも coalesce しつつ起動できる) に合わせて修正。§9 の測定項目を優先度順に並べ替え、静的確定済み (4h/1d) と現 DB から測れない busy 頻度を整理。|外部設計レビュー 1 周目|—|
|2026-09-26|v0.3|mission 上限を worker 1 回の Cw (現物再計算: SIGKILL 後の wait・CLI 回収猶予を追加し 400/410 秒) と dispatch 全体の Cd (trade+reflection 最大 3 件、1610/1650 秒) に分離。起動拒否は Cw ≥ 足幅のときだけ、Cd ≥ 足幅は起動時 WARNING に弱める (★ ユーザー確認待ちだが推奨で進める、§3.3, §4, §5, §8)。AC-B2-07 (1/1) を 2027-01-03 (日) 21:00 UTC 再開まで延長 (2027-01-01 は金曜、72 時間連続閉場。`date -d` で暦を確認) — v0.2 の「金曜 21:00:30 開場 tick」は誤りだった。AC-B2-01 の確定足時刻を 1 区間補正 (bar_time+足幅+grace の計算違い)。cursor `L` を「処理済み watermark」と定義し直し、受理/閉場 baseline/前セッション skip/dirty 再試行/`W=None` 保留の遷移表を追加、dirty 再試行と `conn_core` の未 rollback 禁止を明記 (§3.2, §4)。watermark snapshot の受渡しと `missions.start(commit=False)`+`mission_decision_bars` の単一トランザクションを追加 (§3.5)。`SubmitResult` を supervisor 同一 lock 内で原子的に生成し `clock_fn` を注入可能に (§3.3)。§9-4 (tool 呼出し量) を B-2d 着手前必須測定にし `datafeed.context_daily_call_budget` を予約 (§3.4, §7, §9)。取得失敗 (shortage) 応答にも role/interval を必須化 (§3.4, §5)。既存 scheduler 安全系テストの全実行と資金保護の実行順序回帰 AC を追加 (AC-B2-24)。新規 AC-B2-22 (W=None 保留)・AC-B2-23 (単一トランザクション rollback) を追加、AC-B2-10 を dirty 再試行の記述に更新。§8 に「裁定済み (外部設計レビュー反映)」12 件を追加、解決済みの未裁定項目 (end-to-end ceiling の扱い) を除去。§9 を「設計を覆し得る順」に並べ替え (Cw/Cd 実測を先頭)。|外部設計レビュー 2 周目|—|
|2026-09-26|v0.4|watchdog 上限 (1420 秒) が Cd と食い違っていたのを Cd から導出する 1 本の値 (`worker.dispatch_ceiling_sec`) に統一。Cw に SIGKILL 後 `proc.wait` 再実行分を足し 405/415 秒、Cd を 1630/1670 秒に再計算 (§1, §3.3, §7)。`SubmitResult` に `queued`/`running`/`shutdown` の `reason` と `future` を持たせ `_phase` 遷移を lock 内に、`fail_pending` も `busy_since` をクリア (§3.3)。`L>W` の稼働中 hold と `W≥L` での自動再開を明文化 (§3.2, AC-B2-11, AC-B2-25)。dirty 再試行の置き場所を受理分岐の中から `tick` の `finally` (全 return 経路) へ訂正 (§3.2)。AC-B2-21/IV-9 を「意図的な差分の列挙+それ以外は同じ」に書き直し (§4, §5)。`mission_decision_bars` へ書くのは前進した pair だけに限定し、snapshot の部分集合配線を追加 (§3.5)。`TradeLoop` の書込み順序を `missions.start(commit=False)`→子表 INSERT→commit→`claim_oldest` に固定し、rollback 時の `mid=None` を明記、AC-B2-16/23 の注入点を分離 (§3.1, §3.5, §5)。§9-1 を実 `WorkerRunner`+fake 子プロセスでの実時間計測に書き直す (§9)。AC-B2-08 の日付誤記 (12-24 木) を修正、AC-B2-24 に session 判定側の例外注入を追加、`mission_decision_bars` の PK/FK を明記、§10 に `backtest/runner.py:419`・snapshot 経路 3 か所・`on_trade_mission`/`try_submit`/`TABLE_NAMES` の差し替え箇所全数を追加 (§5, §9, §10)。§8 に指摘事項の再反映を追記。|外部設計レビュー 3 周目 (Critical 0、収束)|—|
|2026-09-26|v1.0|spec として清書 (内容は下書き v0.4 + AC-B2-10 の 4 分割)。|外部設計レビュー 4 周 (Critical 1 → 2 → 0 → 0) で収束。**ユーザー承認 2026-09-26** (★ 上限の線引きも確定)|—|
|2026-09-26|v1.1|§11 の task 割当を修正: AC-B2-21 を (a)Tb2/(b)Tb1/(c)Tc1 に分割し段 a では「1h の発火リズムが B-1 と同じ」ことだけを確認、AC-B2-24 を (a)Ta3/(b)Tb1 に分割 (§11)。§10 の記述を訂正: `try_submit` spy は `:452-463・506-517・635-647` の 3 本 (§10)、`test_service_app.py:1487-1589` は書換え不要で、実際に旧式を pin しているのは `:1409-1441` (書換え式・新 pin 値 518.0 を明記、§10)。cursor 復元は `Scheduler.__init__` (`init_db` が先行) で完結し `service.py` の起動処理は変えないと訂正 (§2, §3.2, §10, §11)。`SubmitResult` に `checked_at` を追加し、`_run` のスロット解放を future 解決の**前**に置き換える設計に修正、現物 (`supervisor.py:106-141`) がスロットを future 解決の**後**に空ける潜在欠陥を §1 に事実として追加、AC-B2-03 に対応する逆変異を追加 (§1, §3.3, §4, §5, §8)。§3.3 の Cw 内訳に余白の性質差 (SIGKILL 系 15 秒は正常系でほぼ未消費 / `dispatcher.join` の `rpc_timeout_sec+5` は RPC 張り付きで実測でも支配的) を注記し、AC-B2-03 に RPC 投げっぱなし fake のケースを追加 (§3.3, §5)。§3.5 の provenance 表から「mission 開始 summary に `decision_tf`」の記述を削除 (該当 event が無いため。provenance は `mission_decision_bars` が正、§3.5)。§3.5 の `mid=None` の記述を「例外ハンドラでの明示代入」から「prepare 関数が成功時のみ値を返す構成による構造的な `None`」に訂正 (§3.5)。§3.2 の cron due 規則に pair 単位の判定を明記: 同一 tick で前セッション足の pair と今セッション足の pair が混在し得ることを式・遷移表に反映 (§3.2)。|実装プラン執筆・着手前検証・外部レビューで判明した現物との差|—|
|2026-09-26|v1.2|§3.2 に復元の 2 段 (起動時読み = 起動拒否判定、毎 tick 再読込 = 通常復元) を明記。§3.3 に Cd の前提 (reflection = trade と同じ runner) を明記。|実装後の変異スイープの申し送り (起動時復元と毎 tick 再読込の重複、Cd の見積り前提)|—|
