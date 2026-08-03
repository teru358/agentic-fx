# プラン 8 設計: サービス堅牢化 (Mission worker 隔離 + preemption + 起票返済)

**日付**: 2026-08-03
**status**: ユーザー承認済み (ブレインストーミング 4 セクション逐次承認)
**入力**: 分解書 (`docs/superpowers/plans/2026-08-01-phase2-decomposition.md` プラン 8 節) / 設計書 §15 Phase 2 preemption 受入条件 / プラン 7 レジャー起票束 (`.superpowers/sdd/2026-08-02-phase2-7-plugins/progress.md` 末尾) / プラン 5 レジャー park 一覧 (`.superpowers/sdd/2026-07-26-phase1-5-loop-service/progress.md` 統合裁定節)

## 0. スコープ (ユーザー裁定: A + B 全部入れ)

- **A. 分解書プラン 8 本体**: ①Mission worker プロセス隔離 ②preemption ③資金保護の並行継続 ④スレッド監督 ⑤health ラッチ ⑥App.close 資源終端 ⑦プラン 5 park 小口返済
- **B. プラン 7 起票束**: sandbox 増強 (pytest 完全隔離・RLIMIT_NOFILE/FSIZE・PluginSession スレッド安全性) / `_parse_timeframe`・`_pair_param` 公開昇格 / maintenance 順序 / producer_source 検証 / strategy_adapter バケット実在対称化 / sqlite3.Error CLI 境界 / SQLite ≥3.35 起動 assert / description f-string 化
- **非スコープ**: 「close/cancel への gate 適用可否」は実装でなく spec 論点 — プラン 9 前の spec 小改訂束へ送付 (ユーザー確認済み)。改善ループ本体・improve registry の中身はプラン 9。

## 1. 解決する問題 (現状調査で確定した事実)

1. **停止窓が実効無界**: Mission は scheduler tick スレッド内で `core_lock` 保持のままインライン実行され (service.py:364-366)、SL/TP 監視 (`_process_exits`, scheduler.py:209) は次 tick まで走らない。1 tick に最大 4 Mission (trade 1 + reflection 3)、名目 300s×4 だが LocalRunner の deadline チェックはツール実行後のみ (local_runner.py:146-147) のため、ハングした 1 ツール呼び出しで無限にブロックする。CLAUDE.md 絶対制約 (資金保護は決定論的コードで強制) に対する構造的リスク。
2. **kill 経路が存在しない**: watchdog は超過通知のみ (service.py:448-477)。
3. **transcript は末尾一括**のみで、途中死で全喪失 + missions 行が `running` のまま残留。起動時回収も無い。
4. **ask が第 2 の停止窓**: `_LockedAsk` が main スレッドから `core_lock` を掴む。
5. ツール群は `conn_core` を束縛したクロージャで、子プロセスに継承できない (worker 化には子側 registry 再構築が必要)。
6. plugin sandbox (sandbox.py / worker.py) に subprocess 隔離の実証済み部品がある: `start_new_session`+killpg・最小 env・handshake 経由の子側 rlimit・JSON 行プロトコル + reader スレッド + max_bytes・起動/実行 timeout の分離・close 順序不変条件 (kill 完了→パイプ close)。ただし terminate→kill エスカレーションは無く、親は同期ブロックする — 流用してもロック解放は別問題。

## 2. アプローチ裁定

**採用: 案 1 — Mission ごとの使い捨て worker プロセス + mission supervisor スレッド**。

採用根拠: spec §15 の受入条件 6 項目 (worker 隔離 / 子 DB 分離 / terminate→kill / timeout finalize / 部分 transcript / 資金保護継続) に 1 対 1 で対応し、sandbox の実証済み部品を最大限再利用できる。Mission 頻度 (毎時 + シグナル数回) に対しプロセス起動コスト (数百 ms) は無視でき、使い捨てなので状態リーク・設定 staleness が構造的に発生しない。

棄却: 案 2 (常駐 worker) は起動コスト節約の利得がほぼゼロで状態リーク・再起動監督の複雑さだけ買う。案 3 (スレッド分離のみ) は kill 不能・権限境界不成立で spec §15 に不適合。

## 3. 全体アーキテクチャ (after)

### 3.1 WorkerRunner seam

既存 `AgentRunner` 抽象 (LocalRunner / ClaudeRunner) に 3 つ目の実装 **`WorkerRunner`** を追加する。`WorkerRunner.run(mission)` は使い捨て子プロセス `python -m agentic_fx.mission_worker` を spawn し、子の中で config に従い LocalRunner を組み立てて LLM ループを回す。

この seam の根拠: `TradeLoop._run_recorded` / `ReflectionCycle` の finalize 所有権 (finally で必ず `missions.finish`) を一切動かさずにプロセス隔離が入る。preemption は WorkerRunner の内側に閉じ、kill 後も `MissionResult(status="timeout")` を返して既存 4 終端契約に in-band で乗る。watchdog が missions 行に触らない現行の所有権不変条件も維持される。

### 3.2 スレッド構成

| スレッド | 役割 | core_lock |
|---|---|---|
| scheduler | 毎 tick `_process_exits` 等 + Mission 起動判定 → supervisor へ投入して即 return | tick 全体で取得 (Mission を含まないので常に短い) |
| **mission supervisor (新設)** | 単一スロットで Mission ジョブを直列実行 (`trade_loop.run_once` / `reflection.run_pending` の本体はここへ移る) | **決定論区間のみ取得**: claim・intent 適用 (Risk Gate→executor)・consume/requeue・missions.start/finish。`runner.run` 中は非保持 |
| watchdog | 既存の超過通知 + heartbeat 監督 (§6) | 取らない |
| main/shell | daemon 待機 or shell。`ask` は supervisor 経由に統一 | — |

「Mission 実行中も SL/TP 監視継続」は *tick が Mission を待たない構造* として成立する。停止窓は決定論区間の数 ms〜数百 ms に縮み、LLM・ツールのハングは子プロセスごと kill できる。

### 3.3 supervisor とスロット意味論

- 実体: ジョブキュー + 単一スレッド。ジョブ 3 種 — `trade(trigger)` / その直後に続く `reflection バッチ (max 3)` / `ask` (shell へ Future で結果返却)。
- **直列性の保証は「supervisor スレッドが 1 本」という構造に置く**。scheduler.py:234-239 の「起動判定〜起動の原子性は単一スレッド逐次呼び出しに依存 (プラン 8 で再検討)」の前提をここへ移して文書化する。
- tick はスロット busy なら投入しない。cron 締切の前進は**投入が受理された時のみ**行う (busy skip は次 tick へ自然に持ち越し)。signal は claim 前なので取りこぼしなし — claim は supervisor が実行直前に行う。
- `_LockedAsk` は廃止。shell → supervisor へジョブ投入 + 結果待ち (UX は現行同様ブロック)。ask が core_lock を掴んで tick を止める経路が消える。

### 3.4 親子の役割分担

子 = LLM ループ + ツール実行のみ。決定論部分 (claim / consume / Risk Gate / executor / finalize) はすべて親。子は自前の SQLite 接続を**読み取り専用 (URI `mode=ro`)** で開く — 「取引判断 loop は読み取り専用ツール」という spec 宣言が構造的強制になる。唯一の例外は RAG 検索 (chromadb PersistentClient は多プロセス同時アクセス非対応) で、これのみ親への tool-RPC で中継する。

## 4. Mission worker 詳細

### 4.1 起動と handshake

`Popen([sys.executable, "-m", "agentic_fx.mission_worker"], start_new_session=True)`。sandbox の実証済みパターンを踏襲: JSON 1 行プロトコル・reader スレッド + max_bytes・起動 timeout (子の import 完了まで) と実行 timeout の分離・`AFX_*` 非継承・close 順序不変条件 (kill 完了→パイプ close)。

handshake で渡すもの: DB パス (RO) / settings の必要サブセット / mission 仕様 (id, loop, tools, prompt, timeout_sec, max_turns) / runner 設定 / **worker profile** / 親 pid。

### 4.2 registry の共有再構築

build_app からツール配線を `build_mission_registry(loop, conn, settings, clock, rag)` として抽出し、**親 (起動時 `_assert_tools_registered` 検証) と子 (実行時) で同一関数を共有**する。配線の二重化を防ぎ、「親で検証したものと子で動くものが同じ」を関数の同一性で担保する。RAG 検索だけは registry 構築時に RPC プロキシ実装を注入する。

### 4.3 プロトコル (上りフレーム 3 種)

1. `event` — transcript メッセージを 1 件ずつ逐次送信。親がバッファし、kill 時も**部分 transcript** をそのまま `missions.finish` に渡せる。LocalRunner には「メッセージ追加 hook」を 1 点足すだけで、transcript 一括返しの既存契約は変えない。
2. `tool_rpc` — RAG 検索のみ。親側はロック無しで応答 (Rag はスレッド安全な読み取りのみ)。
3. `result` — 最終 MissionResult。

### 4.4 env / rlimit

- **ネットワーク毒入れはしない**。mission worker が動かすのは信頼済みハーネスコードで、隔離の目的は preemption・障害封じ込め・権限境界であり敵対コード封殺ではない (llama-swap :8080 とデータプロバイダへの接続が必要)。plugin sandbox の毒入れは plugin 専用のまま。
- 子側 rlimit: `RLIMIT_AS` (寛大な上限)・`RLIMIT_NOFILE`・`RLIMIT_CORE=0` のみ。**CPU 制限は付けない** — Mission の消費は LLM 待ちの壁時計であり、それは親の監視が受け持つ。

### 4.5 worker profile

- `"trade"`: RO db + network + RAG RPC。取引判断・reflection・ask で使用。
- `"improve"`: **DB パス自体を渡さない**・`data/` 不可視。中身の registry はプラン 9。プラン 8 では profile 機構と到達不能テスト (§8 受入 3) までを実装する。

到達不能の根拠が「registry に無い」ではなく「接続情報が存在しない」になるのがプロセス化の本質的な利得 (遮断項目 2/3/4 の構造的成立点)。

### 4.6 preemption エスカレーション

1. 親 (WorkerRunner 内) は壁時計 `mission.timeout_sec + worker_grace_sec` (新設定) を監視 — runner 内 soft deadline の外側の防衛線。
2. 超過で SIGTERM → 子はハンドラで現時点の transcript を flush して自主終了を試みる。
3. `worker_terminate_grace_sec` (新設定) 以内に死ななければ `killpg(SIGKILL)` (セッションリーダーなので孫ごと)。
4. WorkerRunner は部分 transcript + `status="timeout"` を返し、既存 `_run_recorded` の finally が通常どおり finalize。
5. worker の異常死 (crash / EOF / プロトコル違反) は `status="failed"` に正規化 + そこまでの部分 transcript。

### 4.7 残留の回収 (現行の穴 2 つ)

- サービス起動時に missions の `status='running'` 行を `'interrupted'` へ finalize (status に CHECK 制約が無いため migration 不要。signals の起動時 `reclaim_expired` と対をなす)。
- 孤児 worker: 子は handshake の親 pid を定期的に `os.getppid()` と照合し、親死亡 (ppid=1) で自主終了。

## 5. シャットダウンと資源終端 (App.close — park: codex I4)

- `App.close()` を新設し、構築の逆順で close を集約: 実行中 worker の kill → runner → rag → conn_core / conn_shell → notifier。
- `build_app` 途中失敗時も構築済み分を逆順 cleanup。
- `run_service` の finally は `app.close()` に一本化。**停止タイムアウト時にも close を試みる** (現行は runner すら閉じない)。
- shutdown 時に Mission 実行中なら SIGTERM→grace→kill→finalize してから join。停止時間に上限を設ける。

## 6. スレッド監督 (park: codex I2) と health ラッチ (park: codex I3)

- 各スレッド (scheduler / supervisor / watchdog) が heartbeat タイムスタンプを更新。**main スレッドの待機ループ** (daemon 時は既に 1 秒周期) が全スレッドの生存 + heartbeat 鮮度を監視し、死亡検出時は activity + Notifier 通知を試みて**非ゼロ終了** — monit 再起動の現行運用とかみ合わせる。
- activity 書き込み失敗 (ディスクフル等) は App 内の **latched health 状態**に記録し、以後は別経路 (notifier + stderr) で警告。`status` コマンドでラッチ内容を表示。**ラッチは解除しない** (プロセス再起動でのみクリア — 「一度でも記録が欠けた稼働」を人間が確実に知るため)。

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
| プラン 5 park 小口 | retry policy 明文化 (`_last_trade` 前進 = 1 回/時再試行を意図として文書化 + テスト) / scheduler 時刻源の clock 配線 / provider ctor seam / policy OSError / shell readline 中断 / fable M3〜M7 残 minor |

## 8. 受入条件

分解書の 3 項目 + 調査で確定した穴の回収:

1. **ハング注入で kill**: 子内 runner を無限ブロックする fake に差し替え → SIGTERM 無視時も SIGKILL され、missions 行が `timeout` finalize + 部分 transcript が保存される。
2. **資金保護継続**: Mission 実行中 (worker ブロック中) に SL 到達 → 次 tick の `_process_exits` がクローズを実行する統合テスト。
3. **improve profile 到達不能**: worker から `run_holdout_gate` / `ohlcv` 直読 / `data/` が構造的に到達不能であることのテスト。
4. 起動時 `running` → `interrupted` 回収 / スレッド死亡 → 非ゼロ終了 / App.close 全経路 (正常・停止タイムアウト・build 途中失敗)。
5. **決定論的コア diff ゼロ**: risk_gate / executor / kill_switch の実装は不変 (呼び出し位置のみ supervisor へ移動)。最終ブランチレビューで照合。
6. 既存 1404 tests green。

## 9. テスト戦略と SDD 運用

- TDD 継続。worker はプロトコルをインプロセスで喋る **FakeWorker** でユニット、実サブプロセスは E2E 帯 (実 spawn・実 kill・実 rlimit)。
- テスト規約継続: `pytest.raises(match=...)` はエラー文言固有の部分文字列に絞る / 変異注入を実装者・レビュアー双方に必須化。
- SDD 運用はプラン 7 と同一: implementer sonnet + (sonnet spec/変異 + codex 敵対) 並行レビュー + scoped 再レビュー + 節目停止。最終ブランチレビューは最上位モデル + codex で cross-task 接合部。
- 想定 task 順序 (詳細は writing-plans で確定): 公開昇格 rename → B 小口束 → worker 基盤 (registry 抽出 + プロトコル) → WorkerRunner + preemption → supervisor + core_lock 粒度 → 監督 / health / close → park 返済 → E2E。

## 10. 新設定キー (settings.yaml / example 同期)

- `worker_grace_sec` — runner soft deadline の外側マージン (壁時計監視)
- `worker_terminate_grace_sec` — SIGTERM 後 SIGKILL までの猶予
- (必要に応じ) worker 起動 timeout・shutdown 上限。既定値と ge/gt 制約は writing-plans で確定。

## 11. プラン 9 への接続

- improve worker profile の registry 中身・改善ループ本体はプラン 9。遮断 8 項目の全経路統合回帰テストはプラン 9 の blocking 受入条件 (分解書どおり)。
- spec 小改訂束 (exit_mode ② / signals UNIQUE 意図明文化 / approved+rejected 併存規則 / claimed_by FK / **close/cancel gate 論点**) はプラン 9 前に実施。
