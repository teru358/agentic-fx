# プラン 8 設計: サービス堅牢化 (Mission worker 隔離 + preemption + 起票返済)

**日付**: 2026-08-03
**status**: 改訂 5 — codex round 4 で収束 (「実装計画作成へ進んでよい」判定)。ユーザー最終レビュー待ち
**入力**: 分解書 (`docs/superpowers/plans/2026-08-01-phase2-decomposition.md` プラン 8 節) / 設計書 §15 Phase 2 受入条件 / プラン 7 レジャー起票束 (`.superpowers/sdd/2026-08-02-phase2-7-plugins/progress.md` 末尾) / プラン 5 レジャー park 一覧 (`.superpowers/sdd/2026-07-26-phase1-5-loop-service/progress.md` 統合裁定節) / codex round 1 (`.superpowers/sdd/2026-08-03-phase2-8-design-review/codex-round1.md`)

## 0. スコープ (ユーザー裁定: A + B 全部入れ)

- **A. 分解書プラン 8 本体**: ①Mission worker プロセス隔離 ②preemption ③資金保護の並行継続 ④スレッド監督 ⑤health ラッチ ⑥App.close 資源終端 ⑦プラン 5 park 小口返済
- **B. プラン 7 起票束**: sandbox 増強 (pytest 完全隔離・RLIMIT_NOFILE/FSIZE・PluginSession スレッド安全性) / `_parse_timeframe`・`_pair_param` 公開昇格 / maintenance 順序 / producer_source 検証 / strategy_adapter バケット実在対称化 / sqlite3.Error CLI 境界 / SQLite ≥3.35 起動 assert / description f-string 化
- **非スコープ**: 「close/cancel への gate 適用可否」は実装でなく spec 論点 — プラン 9 前の spec 小改訂束へ送付 (ユーザー確認済み)。改善ループ本体・improve registry の中身はプラン 9。

## 1. 解決する問題 (現状調査で確定した事実)

1. **停止窓が実効無界**: Mission は scheduler tick スレッド内で `core_lock` 保持のままインライン実行され (service.py:364-366)、SL/TP 監視 (`_process_exits`, scheduler.py:209) は次 tick まで走らない。1 tick に最大 4 Mission (trade 1 + reflection 3)、名目 300s×4 だが LocalRunner の deadline チェックはツール実行後のみ (local_runner.py:146-147) のため、ハングした 1 ツール呼び出しで無限にブロックする。CLAUDE.md 絶対制約 (資金保護は決定論的コードで強制) に対する構造的リスク。
2. **kill 経路が存在しない**: watchdog は超過通知のみ (service.py:448-477)。
3. **transcript は末尾一括**のみで、途中死で全喪失 + missions 行が `running` のまま残留。起動時回収も無い。
4. **ask が第 2 の停止窓**: `_LockedAsk` が main スレッドから `core_lock` を掴む。
5. **tick 冒頭のデータ hooks も停止窓** (codex I-1): news 収集・経済指標更新・RAG 書き込み (scheduler.py:93, service.py:394) は外部 I/O を含み、ハングすれば Mission を隔離しても次 tick の資金保護が止まる。
6. ツール群は `conn_core` を束縛したクロージャで、子プロセスに継承できない (worker 化には子側 registry 再構築が必要)。
7. plugin sandbox (sandbox.py / worker.py) に subprocess 隔離の実証済み部品がある: `start_new_session`+killpg・最小 env・handshake 経由の子側 rlimit・JSON 行プロトコル + reader スレッド + max_bytes・起動/実行 timeout の分離・close 順序不変条件 (kill 完了→パイプ close)。ただし①一要求一応答の同期プロトコルであり双方向 RPC は無い ②terminate→kill エスカレーションは無い ③親は同期ブロックする — 流用してもロック解放は別問題。

## 2. アプローチ裁定

**採用: 案 1 — Mission ごとの使い捨て worker プロセス + mission supervisor スレッド**。

採用根拠: spec §15 の受入条件 6 項目 (worker 隔離 / 子 DB 分離 / terminate→kill / timeout finalize / 部分 transcript / 資金保護継続) に 1 対 1 で対応し、sandbox の実証済み部品を最大限再利用できる。Mission 頻度 (毎時 + シグナル数回) に対しプロセス起動コスト (数百 ms) は無視でき、使い捨てなので状態リーク・設定 staleness が構造的に発生しない。

棄却: 案 2 (常駐 worker) は起動コスト節約の利得がほぼゼロで状態リーク・再起動監督の複雑さだけ買う。案 3 (スレッド分離のみ) は kill 不能・権限境界不成立で spec §15 に不適合。

## 3. 全体アーキテクチャ (after)

### 3.1 WorkerRunner seam と Mission 実行の三相分解 (codex C-1 対応)

既存 `AgentRunner` 抽象 (LocalRunner / ClaudeRunner) に 3 つ目の実装 **`WorkerRunner`** を追加する。`WorkerRunner.run(mission)` は使い捨て子プロセス `python -m agentic_fx.mission_worker` を spawn し、子の中で config に従い LocalRunner を組み立てて LLM ループを回す。preemption は WorkerRunner の内側に閉じ、kill 後も `MissionResult(status="timeout")` を返して既存 4 終端契約に in-band で乗る。

現行の `TradeLoop._run_once_impl` / `ReflectionCycle._reflect_one` は claim〜runner〜executor〜finalize が単一メソッドに密結合しており、「呼び出し位置だけ supervisor に移す」ではロック粒度を変えられない。そこで **Mission 実行を三相に再構成する**:

| 相 | 内容 | core_lock | 接続 |
|---|---|---|---|
| **prepare** | healthcheck (lock 外・timeout 付き) → `missions.start` / `signals.claim_oldest` / prompt 構築 (DB 読み) | 取得 | conn_core |
| **run** | `WorkerRunner.run(mission)` — 子プロセスで LLM ループ | **非保持** | 子の RO 接続 (+RAG RPC) |
| **commit-pre** | 結果検証 → `TradeIntent.from_llm_dict` → **Risk Gate/執行に要る全外部取得**: 執行用 quote + instrument spec + 全 exposure 通貨の換算レート (`rate_fn` 経由 — PriceProvider quote に到達する外部 I/O)。取得値は timestamp 付きスナップショットにする | **非保持** | — |
| **commit-core** | **スナップショットの鮮度再検証** (期限超過は intent 拒否 — lock 内での再取得はしない) → DB 状態を読み直して GateContext 確定 → Risk Gate 判定 → paper broker 執行 (DB 書込 = 決定論) → `signals.consume` → `missions.finish` (CAS)。finally で未 consume claim の requeue | 取得 | conn_core |
| **commit-post** | Notifier 通知・activity 集約書込のうち lock 不要なもの | **非保持** | — |

**commit の 3 小相分割の理由** (codex C2-5 / C3-2): `executor.handle_intent` は執行用 quote だけでなく、GateContext 構築中に `spec_fn` と `cycle_rate_fn` (cache miss 時 `rate_fn` → `PriceProvider.to_account_rate` → quote 取得: executor.py:166,256,272-277 / service.py:298 / price_provider.py:395) の**換算レート外部 I/O** を呼ぶ。これらが lock 下に残ると換算プロバイダのハングで SL/TP 監視が止まる。したがって **commit-pre で Risk Gate に必要な全外部取得を完了**させ、commit-core は「取得済みスナップショット + DB 操作」だけにする。**鮮度の契約** (codex I3-3): commit-pre の取得値は lock 待ちの間に陳腐化しうるため、commit-core 開始時に quote/rate の timestamp を再検証し、期限超過は**発注拒否して次周期へ送る** (lock 内再取得は C3-2 の再発なので禁止)。risk_gate / kill_switch のロジック自体は不変 (受入 7 の「決定論的コア」は risk_gate/kill_switch に diff ゼロを適用し、executor は「判定ロジック不変・I/O 位置のみ移動」を差分レビューで確認する条件)。**Phase 3 の live broker submit** (外部 I/O だが順序保証が必要) の置き場所は Phase 3 設計論点として予約 — 本プランでは paper broker (DB 書込) のみ。

- 相間で保持してよいのは不変データのみ (mission_id・claim した signal の raw 行・構築済み prompt・settings スナップショット)。
- **接続契約**: `conn_core` は「core_lock 保持中のみ触れる」を規約として明文化する (docstring + レビュー観点)。
- **finalize 所有権は commit 相 (supervisor スレッド) に一本化**。`_run_recorded` の「finally で必ず finish」という不変条件は commit 相の finally に引き継ぐ。ReflectionCycle も同じ三相構造に再構成し、独自実装だった `_run_recorded` 相当を共通化する。

### 3.2 スレッド構成と tick の遅延予算 (codex I-1 対応)

| スレッド | 役割 | core_lock |
|---|---|---|
| scheduler | 毎 tick: **決定論ブロック (mark-to-market → account/予約再検証 → `fills_allowed` 判定 → `_process_limit_fills` → `_process_exits`) を現行の内部順序のまま先頭で実行** → データ hooks (timeout 必須) → Mission 起動判定 → supervisor へ `try_submit` して即 return | tick 全体で取得 (Mission を含まない) |
| **mission supervisor (新設)** | 単一スロットで Mission ジョブを直列実行 (三相) | prepare / commit-core のみ取得 |
| watchdog | 超過通知 (既存) + **スレッド監督の主体** (§6) | 取らない |
| main/shell | daemon 待機 or shell。`ask` は supervisor 経由に統一 | — |

- tick 内の順序再編は **「hooks だけを決定論ブロックの後ろへ移す」** と定義する (codex C2-1 + C3-1 対応)。現行 tick は hooks (scheduler.py:93) → mark-to-market (:126) → account/予約再検証 → `fills_allowed` 判定 (:145) → `_process_limit_fills` (:208) → `_process_exits` (:209) の順であり、**決定論ブロック (:126 以降) は内部順序を一切変えずそのまま先頭へ繰り上がる**。fills だけを切り出して先頭に置いてはならない — mark-to-market・account snapshot 検証・予約リスク再検証が `fills_allowed` を決めており、これらを飛ばすと account 不明時や予約超過時にも指値が約定する fail-closed 破りになる (C3-1)。また `_process_exits` 単独を先頭に置いてもならない — exits 末尾の processed-bar マーキング (:949-) を fills の `_fresh_bar` (:879, :294) が参照し、順序が逆転すると当該バーの指値約定が抑止される (C2-1)。`filled_ids` 受け渡し (同一バー TP の誤確定防止) も保存する。**この契約 (決定論ブロックの内部順序 + processed-bar マーキング位置 + fills_allowed ゲート) を回帰ピンテストで固定する**。
- データ hooks (news collector / econ refresh / signal maintenance / RAG 書込) の **timeout の強制点は HTTP クライアント構築時の httpx timeout (connect/read/write/pool) を settings から必須注入すること** (codex I2-1 — 同期呼び出しのスレッド kill は不可能なため、`data_hook_timeout_sec` は「hook が内部で使う全ネットワーククライアントの timeout 上限」の規定であり wall-clock 保証ではない)。fetchers / collector のクライアント生成箇所の監査を task に含める。timeout が効かない極端なハング (DNS 等) は watchdog の heartbeat 監督 (§6) が検出し、§6 の死亡時規則 (両モードで停止) に接続する。
- 「Mission 実行中も SL/TP 監視継続」は *tick が Mission を待たない構造* + *hooks の有界化* + *hooks を資金保護の後ろに置く順序* の 3 点で成立する。

### 3.3 supervisor とスロット意味論 (codex I-2/I-3/I-4 対応)

- 実体: 容量 1 のジョブスロット + 単一スレッド。ジョブ 3 種 — `trade(trigger)` / その直後に続く `reflection バッチ (max 3)` / `ask` (Future で結果返却)。
- **`try_submit()` を原子的契約にする**: supervisor 内部 lock の下で「実行中ジョブ + 予約済みジョブの有無」を判定し、空きがあれば受理・busy なら即 False を返す。判定と投入を分離しない (TOCTOU 封鎖)。
- **直列性の保証は「supervisor スレッドが 1 本」という構造に置く**。scheduler.py:234-239 の「起動判定〜起動の原子性は単一スレッド逐次呼び出しに依存 (プラン 8 で再検討)」の前提をここへ移して文書化する。
- **cron の意味論 (遅延であって欠落ではない)**: cron 締切の前進は `try_submit` が**受理された時のみ**行う。busy で拒否された場合、cron due は成立したままなので次 tick 以降で必ず再試行され、スロットが空き次第実行される — 上位仕様の「cron は定時実行保証を優先して待つ / signal は non-blocking で諦める」(設計書 §5) と整合。signal は claim 前なので取りこぼしなし — claim は supervisor の prepare 相で行う。
- **ask の統一と Future の終了規則**: `_LockedAsk` は廃止。shell → `try_submit(ask)` + Future 待ち (UX は現行同様ブロック)。Future 待ちには **wall-clock timeout** (mission timeout + preemption 猶予 + マージン) を付け、shutdown 開始時は supervisor が **queue 内の未着手ジョブと pending Future をすべて例外で完了させる** — shell が永久に固まる経路を作らない。supervisor スレッド死亡時は watchdog が検出し (§6)、pending Future を例外完了させる。

### 3.4 親子の役割分担

子 = LLM ループ + ツール実行のみ。決定論部分 (claim / consume / Risk Gate / executor / finalize) はすべて親。子は自前の SQLite 接続を**読み取り専用**で開く — 「取引判断 loop は読み取り専用ツール」という spec 宣言が構造的強制になる。唯一の例外は RAG 検索 (chromadb PersistentClient は多プロセス同時アクセス非対応) で、これのみ親への tool-RPC で中継する (§4.4)。

**RO 接続の実装** (codex I-7): 既存 `db.connect()` は通常パス前提 + WAL PRAGMA 実行のため流用できない。**`db.connect_readonly(db_path)` を新設**する — `sqlite3.connect(f"file:{path}?mode=ro", uri=True, check_same_thread=False)`、書き込み系 PRAGMA は発行しない (journal_mode は既存 WAL を読むだけ)、`busy_timeout` のみ設定。WAL の `-wal`/`-shm` は親プロセスが作成済み (稼働中サービスが前提) なので読取可。

## 4. Mission worker 詳細

### 4.1 起動と handshake

`Popen([sys.executable, "-m", "agentic_fx.mission_worker"], start_new_session=True, cwd=<Mission ごとの専用空 workdir>)`。**cwd は必ず専用空ディレクトリを明示指定**する (codex C-2 — 親 cwd 継承による相対パス `data/agentic.db` 到達を封鎖。plugin sandbox の `cwd=plugin_dir` と同じ流儀)。sandbox の実証済みパターンを踏襲: JSON 1 行プロトコル・reader スレッド + フレーム上限・起動 timeout (子の import 完了まで) と実行 timeout の分離・`AFX_*` 非継承・close 順序不変条件 (kill 完了→パイプ close)。

handshake で渡すもの: DB パス (trade profile のみ) / settings の必要サブセット / mission 仕様 (id, loop, tools, prompt, timeout_sec, max_turns) / runner 設定 / **worker profile** / transcript 上限。

### 4.2 registry の共有再構築

build_app からツール配線を `build_mission_registry(loop, conn, settings, clock, rag)` として抽出し、**親 (起動時 `_assert_tools_registered` 検証) と子 (実行時) で同一関数を共有**する。配線の二重化を防ぎ、「親で検証したものと子で動くものが同じ」を関数の同一性で担保する。RAG 検索だけは registry 構築時に RPC プロキシ実装を注入する。

### 4.3 プロトコル (codex C-3 対応 — 双方向・完全定義)

**フレーム定義** (全フレームに `seq` を付す):

| 方向 | type | 内容 |
|---|---|---|
| 親→子 | `handshake` | §4.1 の初期化データ (起動時 1 回) |
| 子→親 | `ready` | 初期化完了 (起動 timeout の対象) |
| 子→親 | `event` | transcript メッセージ 1 件 (逐次) |
| 子→親 | `tool_rpc` | RPC 要求 `{rpc_id, name, args}` |
| 親→子 | `tool_rpc_result` | RPC 応答 `{rpc_id, ok, result|error}` |
| 子→親 | `result` | 最終 MissionResult (正常終端で 1 回) |

**同時実行と待ち合わせの規則** (デッドロック封鎖):
- 子は単一スレッドで動き、`tool_rpc` は**常に同時 1 件以下** (in-flight 1)。要求送信後は `tool_rpc_result` (rpc_id 一致) をブロッキング待ちする。
- 親側の **writer は 2 時点で排他**: handshake は spawn 直後に WorkerRunner 呼び出しスレッド (supervisor) が書き、以後 stdin へ書くのは RPC 応答の返送のみ。防御的に stdin 書込 lock を置く。
- 親の reader スレッドはフレームを種別処理する: `event` → transcript バッファへ append / `tool_rpc` → **専用 dispatcher スレッドへ引き渡す** (reader 自身は読み続ける) / `result`・EOF → 完了 queue へ。supervisor (呼び出し元) は完了 queue を壁時計 timeout 付きで待つだけ — 「子が RPC 応答待ち・親が result 待ち」の相互待ちは発生しない。
- **RPC を reader インラインで実行しない理由** (codex I2-2): RAG 実装がハングした場合、reader ごと回収不能になり EOF 検出も止まる。dispatcher スレッド + **`rpc_timeout_sec` の応答待ち打ち切り** (超過は子へ tool error を返す) とし、reader は常に生かしておく。
- **dispatcher リークの裁定** (codex I3-1): timeout でハングした dispatcher スレッドは終了できず、Rag lock を保持したまま残りうる。これを「累積許容」しない — **リーク発生 1 本目で latched health fatal とし、§6 の停止シーケンス (非ゼロ終了 → monit 再起動) へ倒す**。RAG の別プロセス化はこの頻度 (ローカル計算のハングは稀) に対して過剰と裁定し、採らない。§4.4 の lock 取得 timeout は「fatal 検出〜停止完了までの間」の波及止めとして残す。
- **`seq` の検証規則** (codex M2-1): 方向別に 1 起点の単調増加。受信側は重複・逆行・欠番を**プロトコル違反としてセッション死** (fail closed — sandbox の `_dead` 意味論と同じ)。
- kill 時は応答不要 — 子は SIGKILL で死に、reader は EOF で終端する。SIGTERM 中に write が詰まる経路は「kill 完了後にパイプを閉じる」順序不変条件 + 応答書込の broken pipe を握って kill へ進むことで封鎖。

**部分 transcript の保存範囲** (codex I-6, spec §15 の「保存範囲の定義」):
- LocalRunner の messages への append を**単一 sink 関数に集約**し (初期 user prompt を含む全 append site: local_runner.py:50, :99, :144, :173, :190, :201)、sink が `event` を送出する。
- 保証は「**親が受信済みの event まで**」— 送信後・受信前に死んだ分は失われる (パイプの性質上の下限)。kill/crash 時は受信済みバッファ + 終端理由を transcript として保存する。

**総量上限** (codex M-3): 1 フレーム上限 (sandbox の行上限を継承) と **Mission 累積上限** (新設定) を分離。累積超過時は truncate marker を置いて以後の `event` を破棄する (Mission 自体は続行 — transcript は監査ログであり実行の前提ではない)。

### 4.4 RAG RPC と Rag のスレッド安全化 (codex I-8 対応)

- 現行 `Rag` にはロックも close() もなく、同一インスタンスを scheduler tick の news 書込 (service.py:303)・reflection 書込・worker RPC 読取が共有することになる。chromadb のスレッド安全性は保証に頼れないため、**`Rag` に内部 lock を追加して全公開メソッドを直列化**する (低頻度・短時間なので十分)。RPC 応答はこの lock 経由 (改訂 1 の「ロック無しで応答」は撤回)。
- **lock 取得は timeout 付き** (codex I2-2 の波及止め): RPC dispatcher が RAG ハングでリークして lock を保持し続けた場合でも、news collector / reflection 書込が永久ブロックしないよう、`Rag` の lock 取得は有限 timeout とし、失敗は「RAG 一時不可」として fail soft (該当機能 skip + latched health 記録)。RAG は補助機能であり、資金保護経路には無い。
- `Rag.close()` は chromadb PersistentClient の実 API を writing-plans で確認して best-effort 実装 (無ければ参照破棄のみで可 — 読み書きは lock で直列化済み)。

### 4.5 env / rlimit (codex M-2 対応)

- **ネットワーク毒入れはしない**。mission worker が動かすのは信頼済みハーネスコードで、隔離の目的は preemption・障害封じ込め・権限境界であり敵対コード封殺ではない (llama-swap :8080 とデータプロバイダへの接続が必要)。plugin sandbox の毒入れは plugin 専用のまま。
- 子側 rlimit: `RLIMIT_AS`・`RLIMIT_NOFILE`・`RLIMIT_FSIZE`・`RLIMIT_CORE=0`。**方針を契約化**: hard=soft で設定、**設定失敗は worker 起動失敗 (fail closed)** (plugin worker の NPROC のみ例外的に握る先例とは異なり、mission worker は全項目 fail closed)。具体値は writing-plans で確定するが、方向: AS は寛大 (数 GB — httpx/pandas が動く水準)、NOFILE は通常動作に十分 + リーク検知が効く水準、FSIZE は小さく (子は原則ファイルを書かない — workdir への想定外書込を異常として検知)。
- **CPU 制限は付けない** — Mission の消費は LLM 待ちの壁時計であり、それは親の監視が受け持つ。

### 4.6 worker profile と権限境界 (codex C-2 対応 — 機構を 2 層に)

- `"trade"`: RO db + network + RAG RPC。取引判断・reflection・ask で使用。
- `"improve"`: **DB パス自体を渡さない**。中身の registry はプラン 9。**network の制限もプラン 9 スコープ** (codex I2-5 — 本プランの improve profile が提供するのは FS 境界 + DB 非提供のみ。network 強制の機構候補 (env ベース毒入れの流用 / プロキシ allowlist) はプラン 9 の improve registry 設計と併せて裁定する)。

**「構造的到達不能」の担保は 2 層で行う** (「パスを渡さない」だけでは同一 UID の子は絶対パス/相対パスでファイルを開けるため不十分 — codex C-2):
1. **接続情報の非提供**: improve profile には DB パス・`data/` の位置を一切渡さない + cwd は専用空 workdir (相対パス到達の封鎖)。
2. **Landlock による FS 自己制限**: worker bootstrap (mission コード実行前) で Linux Landlock (kernel 5.13+、本環境 7.0 で利用可) により FS アクセスを allowlist (コードツリー読取 + 専用 workdir 読書き) に制限し、`data/`・DB ファイルへの絶対パスアクセスを OS レベルで遮断する。実装は ctypes による syscall 直叩き (`landlock_create_ruleset` / `landlock_add_rule` / `landlock_restrict_self`) の小モジュール。**Landlock が利用不能な環境では improve profile の worker は起動拒否 (fail closed)**。trade profile では Landlock は任意 (RO 接続が主防御)。

**到達不能の意味論** (codex C2-4 対応): 分解書の「`run_holdout_gate`・`ohlcv` 直読・`data/` が構造的に到達不能」(2026-08-01-phase2-decomposition.md:96,104) は、本節の「実行不能」意味論で読み替える (writing-plans 時に分解書へ注記を入れる)。Landlock allowlist はコードツリーの読取を許すため、`run_holdout_gate` の**関数 import 自体は可能**である。遮断項目 2 の脅威モデルは「改善ループが holdout 成績を入手して過学習する」ことであり、その実体は**データ到達**にある — `run_holdout_gate` は `history_conn` (履歴 DB 接続) を必須引数に取り、improve worker は DB パス非提供 + Landlock の data/ 遮断により接続を構成できないため、import できても**実行が必ず失敗する**。受入条件は「import 不能」ではなく「**実行不能 (holdout 結果の入手不能)**」で定義する: improve profile の実 worker プロセス内から ①`data/agentic.db` の絶対パス open が失敗 ②`data/` 列挙が失敗 ③`run_holdout_gate` を呼んでもデータ到達不能で失敗 — の 3 点を実測する (§9 受入 3)。

### 4.7 preemption エスカレーション

1. 親 (WorkerRunner 内) は壁時計 `mission.timeout_sec + worker_grace_sec` (新設定) を監視 — runner 内 soft deadline の外側の防衛線。
2. 超過で SIGTERM → 子はハンドラで現時点の transcript を flush して自主終了を試みる。
3. `worker_terminate_grace_sec` (新設定) 以内に死ななければ `killpg(SIGKILL)` (セッションリーダーなので孫ごと)。
4. WorkerRunner は部分 transcript + `status="timeout"` を返し、supervisor の commit 相 finally が finalize する。
5. worker の異常死 (crash / EOF / プロトコル違反) は `status="failed"` に正規化 + そこまでの部分 transcript。

**finalize 所有権と二重終端防止** (codex C-4):
- **missions 行へ終端を書くのは supervisor の commit 相 (finally) ただ一箇所** (稼働プロセス内)。App.close も shutdown 経路も missions 行には書かない — shutdown は worker を kill して `WorkerRunner.run` を返させ、commit 相の finally が通常経路で finalize してから join する (§5)。
- `missions.finish` を **CAS 化**: `UPDATE ... WHERE id=? AND status='running'` とし、影響行数 0 (= 既に終端済み) は activity 警告 + 上書きしない。無条件 UPDATE (missions.py:23) の後勝ち上書きを構造的に封鎖する。

### 4.8 残留の回収と孤児対策 (codex C-5 / I-5 対応)

- **起動時回収は missions と signals を同一トランザクションで**: `status='running'` の missions 行を `'interrupted'` へ finalize すると同時に、`claimed_by_mission_id` がそれらの行を指す `claimed` signals を requeue (requeue_count 上限超過は abandoned) する。分離すると「mission は終端済みなのに signal は lease 満了 (最大 15 分) まで不可視」の不整合窓が生じる。`'interrupted'` は **DB 回収専用の状態値**であり、`MissionResult.status` の 4 値契約 (base.py:38) には現れない (codex M-1 — status 表示・集計はこの区別を明記)。
- **孤児 worker 対策の主手段は `prctl(PR_SET_PDEATHSIG, SIGTERM)`** — 子 bootstrap で設定し、親死亡時に OS がシグナルを配送する (同期ブロック中でも効く。Linux 前提は本プロジェクトの動作環境と整合)。**設定前レースの封鎖** (codex I2-4): handshake で `expected_parent_pid` を受け取り、prctl 設定**直後**に `os.getppid()` と照合— 不一致 (= 設定前に親が死んで再親付けずみ) なら即終了する。ppid 監視は補助 (belt-and-suspenders)。改訂 1 の「定期 ppid 監視のみ」は、LLM HTTP・tool 実行の同期ブロック中に確認できない (codex I-5) ため主手段から降格。

## 5. シャットダウンと資源終端 (App.close — park: codex I4、停止状態機械: codex I-9 対応)

停止は以下の**状態機械として一意に定義**する:

1. **新規受付停止**: stop_event セット → scheduler は起動判定・hooks をスキップ (資金保護区間は最後の tick まで実行)、shell は新規コマンド拒否。同時に実行中 worker へ SIGTERM を発行 (3 と並行開始)。
2. **supervisor drain**: queue 内の未着手ジョブを cancel し、pending Future を例外完了 (shell 解放)。
3. **scheduler join を先に行う** (codex C2-2 — supervisor の commit-core は core_lock を要するため、lock を周期取得する scheduler を先に終わらせて lock 競合を消す。stop_event により scheduler は現 tick 完了で必ず抜け、lock を解放する)。
4. **実行中 worker の終了と supervisor join**: SIGTERM → `worker_terminate_grace_sec` → SIGKILL。`WorkerRunner.run` が返り、commit 相 finally が finalize (missions 行の終端は shutdown でもこの 1 経路のみ)。その後 supervisor join → watchdog join。
5. **資源 close (逆順)**: runner → rag → conn_core / conn_shell → notifier。
6. **join タイムアウト時の close は所有権で線引きする** (codex I2-3): スタックしたスレッドが使用中の資源 (scheduler スタック時の conn_core、supervisor スタック時の conn_core 等、対応表を実装で定義) は **close しない** — 使用中 close の未定義動作より fd リークを選ぶ。close できたものだけ close し、スキップした資源を activity/stderr に記録して exit 1。

- `App.close()` がこの 2〜6 を集約する。`build_app` 途中失敗時は構築済み分のみ逆順 cleanup。
- reader スレッドの終端は sandbox の順序不変条件 (kill 完了 → パイプ close) を踏襲。

## 6. スレッド監督 (park: codex I2) と health ラッチ (park: codex I3) — 監督所在の一意化 (codex I-4 対応)

- **監督の主体は watchdog スレッドに一本化**する (改訂 1 の「main の待機ループ」は対話モードで main が shell にブロックし成立しない — codex I-4)。watchdog は scheduler / supervisor の heartbeat 鮮度と生存を 30 秒周期で監視し、死亡・鮮度超過を検出したら: activity 書込 + Notifier 通知 + supervisor 死亡時は pending Future の例外完了。
- **watchdog 自身の監督**: scheduler tick が watchdog の heartbeat を相互確認し、死亡検出時は activity + 通知。両者同時死は monit (プロセス外) が最後の防波堤。
- **終了規則 (モード共通)**: scheduler / supervisor の回復不能死亡を検出したら、**対話モードでも即座に停止シーケンス (§5) を開始して非ゼロ終了**する (codex C2-3 — scheduler 死亡 = SL/TP 監視の永久停止であり、資金保護が動かないプロセスを生かしておくこと自体が絶対制約違反。改訂 2 の「対話モードは通知のみ」は撤回)。モード差は通知の出し方のみ: daemon は activity+Notifier、対話はそれに加えて shell へ警告を表示してから終了する。
- **停止シーケンスの実行主体の一意化** (codex I3-2): **watchdog は fatal event の記録 + `stop_event` セット + 通知までしか行わない** (watchdog 自身が `App.close()` を実行すると最後に自分自身を join する矛盾が生じる)。**停止状態機械 (§5) の実行主体は常に main スレッド**とする。daemon モードの main は 1 秒周期の `stop_event.wait` で即座に反応できる。対話モードの main は `input()` でブロックしているため、**shell readline 中断 seam (§7 のプラン 5 park 返済項目) を本設計の必須依存に昇格**し、stop_event セットで readline を確実に wake して main が §5 を実行する。
- **health ラッチ**: activity 書き込み失敗 (ディスクフル等) は App 内の latched health 状態に記録し、以後は別経路 (notifier + stderr) で警告。`status` コマンドでラッチ内容を表示。**ラッチは解除しない** (プロセス再起動でのみクリア — 「一度でも記録が欠けた稼働」を人間が確実に知るため)。

## 7. B 束 (プラン 7 起票) と park 小口の方式

| 項目 | 方式 |
|---|---|
| pytest 完全隔離 (submit 時 test_plugin.py) | mission worker 機構ではなく **plugin sandbox 側の拡張**: pytest を最小 env + rlimit + ネットワーク毒入れ環境変数つきサブプロセスで実行。現行の `--noconftest` + AST 防御は維持し層を足す。approval.py の「信頼できるソースのみ」docstring は緩和せず維持 |
| RLIMIT_NOFILE / FSIZE | plugin/worker.py の rlimit 設定に 2 項目追加 |
| PluginSession スレッド安全性 | 「単一スレッド所有」を docstring + 実行時 assert (owner thread 記録) で明文化。ロックは追加しない (全使用箇所が単一スレッド) |
| `_parse_timeframe` / `_pair_param` 公開昇格 | プラン 8 冒頭の機械的 rename 1 コミット (プラン 7 最終レビュー裁定どおり) |
| maintenance 順序 | expire→reclaim を reclaim→expire に入れ替え + 順序ピンテスト (codex M⑤: stale 行の 1 tick 無駄 mission 解消) |
| producer_source 検証 | 起動時に既知プロバイダ集合と照合して typo を fail fast (codex M⑥) |
| strategy_adapter バケット実在対称化 | producer 側 (Task 8 F3) と同じ検証を adapter に (Fable M-1) |
| sqlite3.Error CLI 境界 | CLI 外側 except に sqlite3.Error を追加し診断 + rc=1 (Task 6 deferred ④) |
| SQLite ≥3.35 起動 assert | RETURNING 前提の版数ガードを起動時に (Task 7 deferred ①) |
| description f-string 化 | get_signals ToolDef の「既定 24h」を設定値から生成 (Task 9 deferred ①) |
| プラン 5 park 小口 | retry policy 明文化 (`_last_trade` 前進 = 1 回/時再試行を意図として文書化 + テスト) / scheduler 時刻源の clock 配線 / provider ctor seam / policy OSError / **shell readline 中断 (§6 の停止実行主体の必須依存に昇格 — 対話モードで main を wake する唯一の経路)** / fable M3〜M7 残 minor |

## 8. 新設定キー (settings.yaml / example 同期)

- `worker_grace_sec` — runner soft deadline の外側マージン (壁時計監視)
- `worker_terminate_grace_sec` — SIGTERM 後 SIGKILL までの猶予
- `worker_startup_timeout_sec` — 子の import〜ready まで
- `transcript_max_bytes` — Mission 累積 transcript 上限 (超過は truncate marker)
- `data_hook_timeout_sec` — tick 内データ hooks が内部で使う全ネットワーククライアントの timeout 上限 (§3.2 — wall-clock 保証ではない)
- `rpc_timeout_sec` — RAG RPC の親側応答待ち上限 (§4.3)
- shutdown 上限 (join タイムアウト)。既定値と ge/gt 制約は writing-plans で確定。

## 9. 受入条件

分解書の 3 項目 + 調査・レビューで確定した穴の回収:

1. **ハング注入で kill**: 子内 runner を無限ブロックする fake に差し替え → SIGTERM 無視時も SIGKILL され、missions 行が `timeout` finalize + 部分 transcript が保存される。
2. **資金保護継続**: Mission 実行中 (worker ブロック中) に SL 到達 → 次 tick の `_process_exits` がクローズを実行する統合テスト。
3. **improve profile 到達不能 (実行不能の意味論 — §4.6)**: improve profile の**実 worker プロセス内**から ①`data/agentic.db` 絶対パス open 失敗 ②`data/` 列挙失敗 ③`run_holdout_gate` を呼んでもデータ到達不能で失敗 — の実測テスト (Landlock 層 + 非提供層)。Landlock 不能環境で improve worker が起動拒否することのテスト。
4. **終端の一意性**: `missions.finish` CAS の二重終端拒否テスト / 起動時 `running`→`interrupted` + claimed signals 同時 requeue の同一トランザクションテスト。
5. スレッド死亡 → **両モードとも停止シーケンス + 非ゼロ終了** (対話は shell 警告表示を追加 — §6。codex N4-1) / App.close 全経路 (正常・join タイムアウト・build 途中失敗) / shutdown 時の pending Future 例外完了。
6. **tick 順序契約の保存**: 決定論ブロック (mark-to-market → account/予約再検証 → `fills_allowed` → fills → exits) の内部順序・`fills_allowed` ゲート・`filled_ids` 受け渡し・processed-bar マーキング位置の回帰ピンテスト (account 不明時に指値が約定しないこと / 指値約定がバー到達で成立し続けること — codex C2-1/C3-1)。commit-core の鮮度再検証 (stale スナップショットで発注拒否) のテスト (I3-3)。
7. **決定論的コア**: risk_gate / kill_switch は diff ゼロ。executor は「判定ロジック不変・I/O 位置のみ 3 小相へ移動」を差分レビューで確認 (§3.1)。最終ブランチレビューで照合。
8. 既存 1404 tests green。

## 10. テスト戦略と SDD 運用

- TDD 継続。worker はプロトコルをインプロセスで喋る **FakeWorker** でユニット、実サブプロセスは E2E 帯 (実 spawn・実 kill・実 rlimit・実 Landlock)。
- テスト規約継続: `pytest.raises(match=...)` はエラー文言固有の部分文字列に絞る / 変異注入を実装者・レビュアー双方に必須化。
- SDD 運用はプラン 7 と同一: implementer sonnet + (sonnet spec/変異 + codex 敵対) 並行レビュー + scoped 再レビュー + 節目停止。最終ブランチレビューは最上位モデル + codex で cross-task 接合部。
- 想定 task 順序 (詳細は writing-plans で確定): 公開昇格 rename → B 小口束 → worker 基盤 (プロトコル + registry 抽出 + connect_readonly + Landlock) → WorkerRunner + preemption → 三相分解 + supervisor + tick 再編 → 監督 / health / close → park 返済 → E2E。
- **writing-plans への申し送り** (codex round 4): ①N4-2 — commit-pre と commit-core の間に scheduler が新規 exposure を確定し、スナップショットに必要通貨が無い場合は **lock 内取得せず intent 拒否** (既定原則からの導出だが明示分岐 + テストを plan に置く) ②分解書のプラン 8 節へ「到達不能 = 実行不能の意味論」の注記を入れる (§4.6) ③rlimit 具体値の確定と通常起動の実測 (M-2 残余)。

## 11. プラン 9 への接続

- improve worker profile の registry 中身・改善ループ本体はプラン 9。遮断 8 項目の全経路統合回帰テストはプラン 9 の blocking 受入条件 (分解書どおり)。
- spec 小改訂束 (exit_mode ② / signals UNIQUE 意図明文化 / approved+rejected 併存規則 / claimed_by FK / **close/cancel gate 論点**) はプラン 9 前に実施。

## 12. レビュー履歴

- **round 1 (codex, 2026-08-03)**: C5/I9/M3 — 全件反映。主変更: 三相分解 (C-1) / Landlock 2 層境界 + cwd 明示 (C-2) / 双方向 RPC プロトコル完全定義 (C-3) / finalize 一本化 + finish CAS (C-4) / 起動時 missions+signals 同時回収 (C-5) / tick 資金保護先行 + hooks timeout (I-1) / cron 遅延意味論 (I-2) / try_submit 原子化 (I-3) / Future 終了規則 + 監督の watchdog 一本化 (I-4) / PDEATHSIG (I-5) / transcript sink 集約 + 保存範囲定義 (I-6) / connect_readonly (I-7) / Rag lock 直列化 (I-8) / 停止状態機械 (I-9) / interrupted の位置づけ (M-1) / rlimit fail closed 方針 (M-2) / transcript 累積上限 (M-3)。全文: `.superpowers/sdd/2026-08-03-phase2-8-design-review/codex-round1.md`
- **round 4 (codex, 2026-08-03)**: **収束 — 「実装計画作成へ進んでよい」**。round 3 指摘は全件「解消」判定、新規 blocking なし。N4-1 (受入 5 の対話モード表記) は本改訂で修正、N4-2 (commit 相間の exposure 増加時の fail-closed 分岐) は writing-plans 申し送り。全文: `.superpowers/sdd/2026-08-03-phase2-8-design-review/codex-round4.md`
- **round 3 (codex, 2026-08-03)**: 判定「実装着手不可 (残 3 点 + 裁定 2 件)」— 全件反映。主変更: tick 再編を「決定論ブロック (mark-to-market〜exits) は内部順序不変のまま先頭、hooks のみ後段へ」に精密化 (C3-1 — fills 単独前倒しは fails_allowed ゲート迂回) / commit-pre を「Risk Gate に要る全外部取得 (quote + spec + 換算レート)」に拡張し commit-core を「スナップショット + DB 操作のみ」に (C3-2) / commit-core 開始時の鮮度再検証 + stale は発注拒否 (I3-3) / RPC dispatcher リークは 1 本目で health fatal → 停止 (I3-1 裁定) / 停止実行主体は main に一意化 (watchdog はイベントセットのみ)、shell readline 中断 seam を必須依存に昇格 (I3-2) / 分解書文言の読み替え注記 (C2-4 残余)。全文: `.superpowers/sdd/2026-08-03-phase2-8-design-review/codex-round3.md`
- **round 2 (codex, 2026-08-03)**: round 1 判定 ADDRESSED 10 / PARTIALLY 8 / NOT ADDRESSED 0 + 新規 C5/I5/M1 — 全件反映。主変更: fills→exits ペア保存の tick 再編 + processed-bar 契約ピン (C2-1、コントローラが実コードで CONFIRMED) / shutdown join を scheduler 先行に修正 (C2-2) / scheduler 死亡は両モードで停止 (C2-3) / holdout 到達不能を「実行不能」の意味論で定義 (C2-4) / commit を pre/core/post の 3 小相に分割し外部 I/O を lock 外へ (C2-5) / hooks timeout の強制点 = httpx クライアント注入 (I2-1) / RPC dispatcher スレッド + rpc_timeout + Rag lock timeout (I2-2) / join タイムアウト時 close の所有権線引き (I2-3) / PDEATHSIG 設定前レース照合 (I2-4) / improve network 制限はプラン 9 スコープと明記 (I2-5) / seq 検証規則 (M2-1)。全文: `.superpowers/sdd/2026-08-03-phase2-8-design-review/codex-round2.md`
