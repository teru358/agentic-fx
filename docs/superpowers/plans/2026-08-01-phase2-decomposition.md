# Phase 2 分割書 (プラン 6〜11)

> **改訂 (2026-08-11, 設計書 改訂第 17 版 `c2355e8` 準拠)**: ①**コア改善 PR 経路の廃止**を全節に反映 (改善ループの出力は `plugins/` + `news_sources` + 提案レポートの 2 経路 + レポート。`gh`・専用ブランチ・main 直 push の記述は失効) ②旧プラン 9 を**プラン 9 (前提整備) / プラン 10 (ClaudeRunner + 改善ループ)** に 2 分割し、操作 API を**プラン 11** へ繰り下げ (ユーザー裁定 2026-08-11 — 吸収候補が 27〜30 task に膨らんだため)。

> **本書の位置づけ**: Phase 2 (設計書 §15) をプラン系列に分割し、各プランの task 一覧・依存・受入条件を定める。**詳細プラン (逐語コード型) は各プランの実行直前に作成する** — プラン 5 の実測教訓 (先行作成した詳細プランは実行時に 16 項目の吸収追記が必要になった) による just-in-time 規約。本書作成時点の詳細プランはプラン 6 のみ (`2026-08-01-phase2-6-backtest.md`)。

**入力**: 設計書 **改訂第 17 版** (`docs/superpowers/specs/2026-07-25-agentic-fx-design.md`。本書の初版作成時は改訂第 14 版) + プラン 5 レジャーの Phase 2 park 一覧 (`.superpowers/sdd/2026-07-26-phase1-5-loop-service/progress.md` 末尾)。

**運用**: プラン 1〜5 と同じ SDD パターン (実装 subagent + sonnet/codex 並行レビュー + コントローラ変異照合 + 節目停止 → ユーザー許可)。**絶対制約 (CLAUDE.md) は全プランに適用**: Anthropic API 従量課金 不使用 / 発注・SL 変更・クローズ・資金保護は決定論的コードのみ / drawdown kill switch は config で無効化不可 / 秘密は .env のみ。

## プラン系列と依存 (2026-08-01 レビュー反映: 堅牢化を改善ループの前に前倒し)

```
プラン 6 (バックテスト基盤 + 履歴分析)   ← 依存なし。LLM 不要
   ↓
プラン 7 (plugin 機構 + signals + シグナル起動)   ← 6 の評価基盤を使う
   ↓
プラン 8 (サービス堅牢化: worker 隔離 + preemption + park 返済)   ← 7 の sandbox 機構を共有
   ↓
プラン 9 (前提整備: 入力 spec 3 本 + 起票返済 + 可観測性)   ← 8 完了後。改善ループの前提
   ↓
プラン 10 (ClaudeRunner + 改善ループ)   ← 6/7 の成果物 + 8 の worker 隔離 + 9 の前提整備
   ↓
プラン 11 (操作 API + client.py + Discord 承認)   ← 10 の improve 起動
```

**プラン 9 と 10 を分けた理由 (2026-08-11 裁定)**: 旧プラン 9 は「ClaudeRunner + 改善ループ」8 task だったが、プラン 8 完了までに**入力 spec 3 本 (gather deadline / ctx 超過診断 / ohlcv キャッシュ窓)・spec 小改訂由来 4 件・独立起票 2 件・可観測性 2 件**が積み上がり、単一プランでは 27〜30 task になった。改善ループ本体の着手が最後尾に押し込まれるのを避け、**基盤が固まった状態で改善ループに入る**ため 2 分割する。**「堅牢化を先に」という切り方ではない** — 入力 spec ②③ は改善ループ側の前提 (②の最長 Mission は improve、③は品質ゲートの backtest 読み経路) なので、前提整備側に置いて初めて依存が満たされる。

順序の根拠: 6 は LLM 不要で単体完結し、7 の採用ゲート・10 の品質ゲートの前提。**堅牢化 (旧プラン 10 = 現プラン 8) を改善ループの前に置くのはレビュー裁定 (codex I10 / sonnet I7)**: 改善 Mission は長時間・コード編集・pytest 実行を伴うため、worker 隔離・preemption・「Mission 中も SL/TP 監視継続」(資金保護 = CLAUDE.md 絶対制約) が無いまま先に危険な実行形を完成させない。また **holdout の到達不能性 (遮断項目 2) は worker のプロセス/権限境界で初めて構造的に成立する** — プラン 6 が提供するのは遮断の部品までであり (下記)、改善ループを動かす前に境界が要る。

---

## プラン 6: バックテスト基盤 + 履歴分析

**目的**: 設計書 §6「バックテスト (詳細設計 2026-08-01 確定)」と「履歴分析」を実装する。LLM 非依存で、plugin 機構が無くても IntentSource (呼び出し可能) を差し込んでフルサイクルが検証できる状態にする。

**spec 参照**: §6 バックテスト全節 / §6 履歴分析 / §6 遮断 8 項目 / §12 (ohlcv 改・backtest_runs・analysis_runs・backtest.*・datafeed.watch_symbols)。

**task 一覧** (詳細は `2026-08-01-phase2-6-backtest.md`):

| # | task | 一言 |
|---|---|---|
| 0 | build_app embedding seam | chromadb 実 I/O の注入点 (オフライン CI の前提条件 — park 裁定) |
| 1 | ohlcv v2 migration | source+spread 列・PK (symbol,interval,bar_time,source) 再構築・インポート系は既存行不変 |
| 2 | settings 拡張 | backtest.* / datafeed.watch_symbols / analysis 上限 (コア所有) |
| 3 | Dukascopy デコーダ | bi5 tick 取得・復号 (+実データ照合ステップ) |
| 4 | Dukascopy 1m 集約・投入 | mid/spread 導出・冪等 import・CLI |
| 5 | MT5 一括インポータ + 照合 | bridge 経由取り込み + 価格系差レポート |
| 6 | ReplayClock + BarFeed | UTC 連続 1m 格子・market_hours 契約 |
| 7 | BacktestRunner コア | in-memory DB・synthetic Mission・先読み禁止・IntentSource |
| 8 | 成績集計 + backtest_runs | 指標・scope 3 値・再現性メタデータ |
| 9 | holdout | 分割点計算・in-sample 制限・遮断回帰テスト |
| 10 | 履歴分析 | 相関 3 種・出力契約・analysis_runs |
| 11 | 人間 CLI | backtest / history / analyze サブコマンド |
| 12 | 速度実測ゲート + E2E | 1 年相当 replay ベンチ + import→replay→記録の通し |

**受入条件**: ①Dukascopy から取り込んだ実 1m で、scripted IntentSource による open→約定→TP→closed→成績記録が通る ②holdout 遮断の**部品**の回帰テスト (期間引数なし・ビュー制限 (issuer 込み)・分析出力スキーマ・エラー固定コード) が green ③1 年分 replay の実測時間を記録 (目標オーダー: 数分/年。超過時は次プランで最適化タスク化) ④既存 791+ tests green。

**遮断の完成条件の明示 (レビュー裁定 codex C4)**: プラン 6 単体が保証するのは**遮断部品** (API 形状・ビュー・出力契約) までである。遮断 8 項目の**構造的成立** — holdout 実行の到達不能性 (項目 2)・DB 直読不能 (項目 3)・`data/` 不可視 (項目 4)・holdout 派生情報の非露出 (項目 8) — は改善 worker のプロセス/権限境界 (プラン 8) と改善ループ registry (**プラン 10**) で閉じる。**全 8 経路の統合回帰テストはプラン 10 の blocking 受入条件**とする (これが green になるまで改善ループは有効化しない)。なお設計書改訂 17 により**項目 4 の形が変わった** — 「`data/` 不可視」に加えて「**書き込み可能パスが `plugins/` と `reports/` に閉じている**」が成立条件に入る。

**プラン 7 への提供物**: `IntentSource` プロトコル (plugin アダプタの差し込み先) / `run_in_sample(settings, *, history_conn, symbol, source, intent_source, eval_timeframe, plugin_ref, content_hash, kind, now)` / `run_holdout_gate(...)` (同引数 — 採用ゲート専用、到達不能化はプラン 8) / `analyze_for_agent` / analysis_runs 参照キー。

---

## プラン 7: plugin 機構 + signals + シグナル起動

**目的**: §6 plugin 機構 (indicator / signal / strategy の 3 種別・サンドボックス実行・承認フロー) と §5「strategy シグナルによる Mission 起動」(エグジット先行) を実装する。

**spec 参照**: §6 plugin 機構・種別表・strategy の位置づけ / §5 シグナル起動 (signals 4 状態・claim 原子性・cron とシグナルのレート制限分離・非ブロッキングスロット) / §12 signals テーブル。

**task 一覧 (概略 — 詳細化は実行直前)**:

| # | task | 一言 |
|---|---|---|
| 1 | plugin 契約 + discovery | plugins/ 配置規約・kind 宣言・メタデータ・コンテンツハッシュ |
| 2 | サンドボックス実行 | サブプロセス隔離・I/O 禁止・import allowlist・入力 DataFrame はハーネス供給・timeout |
| 3 | indicator 検証パス | pytest 実行要求 + `get_indicators` への合成点 (market_tools) |
| 4 | signal 検出精度評価 | ラベル付きサンプルの再現率/適合率 (§6 種別表の検証手段) |
| 5 | strategy 評価アダプタ | plugin → IntentSource 変換 (プラン 6 接続)・exit 必須検証 |
| 6 | 承認フロー (kind=plugin) | approval_requests 発行 (テスト結果 + バックテスト結果添付)・承認までロードしない |
| 7 | signals テーブル + 永続化 | 4 状態・重複排除キー (content_hash 込み)・claim/再キュー/lease 回収 |
| 8 | シグナル起動 | `_trade_mission_due` 拡張 ("signal:<plugin>")・cron 締切と別変数・レート制限 fail closed (起動のみ)・エグジット先行 |
| 9 | get_signals ツール | 取引判断 loop 専用・since 最大 lookback (24h)・改善ループには含めない回帰テスト |
| 10 | E2E | 承認済み strategy plugin → シグナル → 前倒し Mission → trigger 記録 |

**受入条件**: 未承認 plugin がロードされない / シグナル起動が Risk Gate・承認ゲートを一切迂回しない (増えるのは LLM を起こす回数のみ — §5) / signals の取りこぼし規則 (読んだ Mission がマークする) のテスト。

**park 織り込み**: なし (プラン 6 提供物に依存)。

---

## プラン 8: サービス堅牢化 (worker 隔離 + preemption + park 返済)

**目的**: §15 Phase 2 の preemption 受入条件を満たすプロセス隔離と、プラン 5 最終レビューで park した堅牢化項目の返済。**前提整備 (プラン 9) と改善ループ (プラン 10) の前提**。

**spec 参照**: §15 Phase 2 受入条件 (worker process 隔離 / 子プロセス DB 分離 / terminate→kill / timeout finalize / 部分 transcript / **Mission 実行中も SL/TP 監視継続**)。

**task 一覧 (概略)**:

| # | task | 一言 |
|---|---|---|
| 1 | Mission worker プロセス | runner.run のプロセス隔離・子側 DB 接続分離・部分 transcript。**holdout/履歴 DB へ worker から到達不能な権限境界** (遮断項目 2/3/4 の構造的成立点) |
| 2 | preemption | 親の壁時計監視・terminate→kill・timeout finalize (missions 行) |
| 3 | 資金保護の並行継続 | Mission 中も _process_exits が走る構造 (core_lock 粒度の再設計) — CLAUDE.md 絶対制約 |
| 4 | スレッド監督 | scheduler/watchdog の heartbeat・死亡時 activity+通知+非ゼロ終了 (park: codex I2) |
| 5 | health ラッチ | activity 書き込み失敗の latched health + 別経路警告 (park: codex I3) |
| 6 | 資源終端 | App.close 集約・build 失敗の逆順 cleanup (park: codex I4) |
| 7 | 小口 park 返済 | retry policy 再設計 (_last_trade) / clock 配線 / provider ctor seam / policy OSError / shell readline 中断 / 残 minor 群 |

**受入条件**: llama-swap ハング注入で Mission が kill され missions 行が timeout finalize される / Mission 実行中に SL 到達 → クローズが遅延なく実行されるテスト / worker プロセスから `run_holdout_gate`・`ohlcv` 直読・`data/` が構造的に到達不能であることのテスト。

> **注記 (プラン8, 設計書 §4.6)**: 「構造的に到達不能」は「実行不能」の意味論で読み替える
> (**本文書中の同表現すべてに適用する** — 「プラン系列と依存」の順序の根拠 / プラン 6 の「遮断の完成条件の明示」/ プラン 8 の task 1 と受入条件。**行番号で参照しない** — 改訂で失効するため) —
> Landlock の allowlist はコードツリーの読取を許すため `run_holdout_gate` の**関数 import
> 自体は可能**。遮断の実体は**データ到達**にあり、`run_holdout_gate` は `history_conn`
> (履歴 DB 接続) を必須引数に取るため、improve worker は DB パス非提供 + Landlock の
> data/ 遮断により接続を構成できず、import できても**実行が必ず失敗する**
> (プラン8 Task 18 で実測検証)。

---

## プラン 9: 前提整備 (入力 spec 3 本 + 起票返済 + 可観測性)

**目的**: プラン 8 完了時に積み上がった**確定済み設計 spec 3 本**と、spec 小改訂束 (設計書改訂 17) から落ちた実装項目・独立起票・可観測性の欠落を返済し、**改善ループ (プラン 10) が乗る基盤を固める**。新機能は増やさない。**18 task** (旧 Task 14 = `plugins/` 入れ子 git はプラン 10 へ移送 — ユーザー裁定 2026-08-11)。

**詳細設計**: `docs/superpowers/specs/2026-08-11-phase2-9-foundation-design.md` (本プランの設計書)。**入力 spec は再記述せずポインタで参照する**。

**入力 spec (すべてレビュー済み・未裁定ゼロ)**:

| spec | 場所 |
|---|---|
| ① gather deadline | `docs/superpowers/specs/2026-08-11-gather-deadline-design.md` (codex 1 周・改稿方針が正) |
| ② ctx 超過の診断 | `docs/superpowers/specs/2026-08-10-context-overflow-diagnosis-design.md` (改訂 3、codex 1 周) |
| ③ ohlcv キャッシュ窓 + 配線 pin | `docs/superpowers/specs/2026-08-10-ohlcv-cache-fallback-design.md` (改訂 4、codex 3 周) |

**task 一覧 (概略。詳細は本プランの設計書)**:

| # | task | 一言 |
|---|---|---|
| 1 | `MissionResult.reason` 契約 | spec ②: base.py の契約 + AgentRunner docstring 規範 (以降すべての前提) |
| 2 | ctx 超過の検知 | spec ②: LocalRunner の error envelope 解析・`safe_text` 安全化・**status code で分岐しない** |
| 3 | reason の worker 運搬 | spec ②: mission_worker → worker_runner の frame |
| 4 | trade / reflection 出口 | spec ②: activity + 通知に reason / `reflection_mission_failed` 新設 |
| 5 | init の n_ctx 可視化 | spec ②: モデル重複除去・improve→trade 順・限定 except |
| 6 | monotonic 注入 + gather deadline | spec ①: `Clock` と分離した monotonic・OPEN/**CLOSE 両方**・検査点テーブル駆動 |
| 7 | deadline の脚伝播 | spec ①: `to_account_rate` の `for spec in legs` 各脚前まで伝播 |
| 8 | 窓計算ヘルパ + floor | spec ③: `live_window_days` / `1d` を含む floor (要求 interval 基準) |
| 9 | `lookback_days` 配線 | spec ③: `get_bars` → `_cached_bars` 必須引数 (**既定値の推測禁止**) |
| 10 | `_cached_bars` 窓適用 | spec ③: `since` 付き `load_bars`・直読/導出の分岐・**source 単位 try の中**に窓計算 |
| 11 | 本番連鎖 E2E pin + 実測 | spec ③: `build_app` + 空注文 DB + tick + ライブ全滅 → `cache(1m→1h derived)` / 実データ実測 |
| 12 | approval 最新決定優先 | 設計書 §7: kind=plugin の (name, content_hash) 単位で承認取り消しを成立させる |
| 13 | `signals.claimed_by_mission_id` FK | 設計書 §12: migration + 配線不整合の書き込み時検出 |
| 15 | reflection の再試行ポリシー | 独立起票①: 恒久失敗 order の starvation と `missions` 行の無制限増大 |
| 16 | **`ohlcv` 分割** + 保持ポリシー | 独立起票②: `DELETE` も保持ポリシーも無い (1 ペア 1 日 1,440 行)。**`ohlcv_cache` / `ohlcv_history` に分割**し、キャッシュ削除が履歴に到達できないことを構造で保証する (設計書 §12 改訂・ユーザー承認 2026-08-11) |
| 17 | `gate_rejected` の可観測性 | Task 20 申し送り③: 集計も通知も無く「健全に見えるが取引しない」が誰にも上がらない |
| 18 | `llama_swap.timeout_sec` 実測 | Task 20 申し送り②: **設計ではなく計測 task**。TTL unload 後の cold load 込み初回が未計測 |
| 19 | `improvement_runs` の PR 列 | 設計書 §12: `result='pr'` / `pr_url` の扱い (残置 or migration) を裁定し反映 |

**受入条件**: **各入力 spec の現行受入条件をすべて満たす** (件数はここに転記しない — 各 spec を正とする) / 変異は 1 つずつ独立に当て、**殺した固有のテスト名を記録した mutation ledger** を成果物とする / 決定論的コア (`risk_gate.py` / `paper_broker.py` / `transitions.py`) は diff ゼロ / `executor.py` の変更は判定ロジックを変えない / 既存テストが 1 本も壊れない。詳細は本プランの設計書 §3。

---

## プラン 10: ClaudeRunner + 改善ループ

**目的**: §4 ClaudeRunner (claude-agent-sdk、**サブスク認証**) と §6 戦略改善 loop (発見→リサーチ→実施の週次 Mission・品質ゲート・approval / 提案レポート出力) を実装する。

**spec 参照**: §4 / §6 改善 loop 全節・**出力の 2 経路**・品質ゲート・許可ツール / §12 improvement_backlog・improvement_runs / §8 policy チャネル。

**task 一覧 (概略)**:

| # | task | 一言 |
|---|---|---|
| 1 | ClaudeRunner | claude-agent-sdk で AgentRunner 実装・config 切替・**従量課金 API 不使用の構造的担保** (子 env から `ANTHROPIC_API_KEY` を除去) + **ユーザー設定の非継承を強制** (`setting_sources=[]` / `strict_mcp_config=True` / `plugins=[]`) |
| 1b | **CodexRunner** | **公式 Python SDK `openai-codex`** (PyPI 0.144.4、`openai-codex-cli-bin` 同梱 = CLI ラッパ) で AgentRunner 実装。**ClaudeRunner と両方導入し config で切替** (`RunnerChoice.backend` を `^(local|claude|codex)$` へ拡張)。用途は**改善ループ**で、取引 loop は既定どおり `local`。**実装順序が決まっている (ユーザー承認)**: ①codex の実現可能性実測 (worker 隔離下で 1 ターン完走するか) → ②共通基盤の設計 → ③ClaudeRunner → ④CodexRunner → ⑤両者で同一不変条件の検証。**①を基盤構築の前に置く** — 動かなければ CodexRunner を落としスコープを縮める。**契約は揃えるが防御機構は揃えない** (claude は Landlock が唯一の線、codex は SDK 層でも絞れる)。**サブスク認証は実測で確定** (`auth.json` が `auth_mode=chatgpt` / `OPENAI_API_KEY=null` / `plan_type=plus`、API キー経路なし)。**ローカル LLM 駆動も成立** — llama-swap をカスタムプロバイダにして応答を得た (**`wire_api="responses"` 必須。codex は Chat Completions を捨てている**) ので、構成は ①サブスク ②**ローカル + codex harness (費用ゼロ)** ③LocalRunner の 3 択になる。詳細はプラン 9 設計書 §5 |
| 2 | 改善用 registry | research_tools (web_search=ddgs / fetch_article 移植) + 書き込み系 — 取引 loop registry と分離 |
| 3 | バックログ + 注入コンテキスト | improvement_backlog CRUD・決定論的な成績集計注入 |
| 4 | 改善 Mission 本体 | 3 ステップ 1 Mission・週次スケジュール |
| 5 | 品質ゲート | テストが 1 件でも落ちる変更は **approval_request 化禁止**・run_backtest (in-sample のみ) ツール・最低 30 取引・提案レポート出力 (`reports/`) |
| 6 | approval / レポート出力 | approval_request (kind=plugin)・explore 履歴 (analysis_run_id) 添付・本体コードに関わる発見は提案レポートへ |
| 7 | policy チャネル | policy add コマンド + 全 Mission への注入は実装済み (Phase 1) — 追記経路のみ |
| 8 | E2E | FakeRunner で発見→レポート→approval 化禁止分岐 / approval 発行 |
| 9 | `plugins/` 入れ子 git + Landlock 拡張 | プラン 9 から移送。原本保全と**承認有効化の不変条件ⓐ〜ⓓ**、`read_write_paths` への `plugins/` 追加。**設計はプラン 9 設計書 §1 D6 が入力** (codex 3〜6 周ぶんの指摘を反映済み・要再収束) |

**受入条件 (blocking)**: **遮断 8 項目の全経路統合回帰テスト** (プラン 6 の部品 + プラン 8 の worker 境界を通しで検証 — これが green になるまで改善ループを有効化しない。**registry Task 2 直後に red で書き始める** — 最後の E2E に置くと 3 プラン跨ぎの blocking 条件が終盤まで検証されない) / 改善ループのツールセットに履歴 DB 直読・期間指定バックテスト・`get_signals` が**無い**ことの回帰テスト / **改善ループの書き込み可能パスが `plugins/` と `reports/` に閉じていること** — registry にツールが無いことだけでなく、**worker 実プロセスからリポジトリ本体へ書けないこと**まで踏む (Landlock allowlist と registry の 2 層で成立する) / ClaudeRunner はサブスク認証のみ。改善 Mission は プラン 8 の worker 隔離上で実行する (in-process 実行の改善ループは作らない)。

**設計段階で裁定が要る事項 (task に埋めない)**: ①**D3 の前提放棄を受けた受入条件の組み直し** — 「改善ループに汎用シェルを与えない」は放棄され (プラン 9 設計書 §5)、代替不変条件は**「改善 worker から `data/` と DB パスに到達できないこと」**。したがって検証すべきは「シェルが無いこと」ではなく「`data/` へ到達できないこと」であり、**`plugins/` を `read_write_paths` に足す際に `data/` を巻き込まないこと**が最重要の検査点になる。②**同梱 CLI バイナリ (336 MB) を使うか `codex_bin` でシステム側を指すか** (「clone + init で動く」原則とのトレードオフ)。③**improve worker 内で `uv run pytest` を回すか** — DB パス非提供 + 空 cwd + コードツリー読取専用の上で pytest を回すには EXECUTE 権と一時書き込み先が必要で、Landlock 設計 (プラン 8 Task 18) に跳ね返る。設計書 §6 許可ツールが再評価を求めている項目であり、**本プランの設計段階で確定させる** (task に埋めると Landlock 配線の変更が実装中に発覚する)。

---

## プラン 11: 操作 API + client.py + Discord 承認

**目的**: §7 操作 REST API (FastAPI・キー 2 段・autopilot 中の変更系拒否) と §8 client.py、§9 discord_bot 連携 (cog は別リポジトリ)、news_sources 承認フローを実装する。

**spec 参照**: §7 全節 / §8 client.py・操作コマンド体系 (Phase 2 コマンド: policy add / improve / news / model) / §6 news ソースリスト・機械検証。

**task 一覧 (概略)**:

| # | task | 一言 |
|---|---|---|
| 1 | FastAPI 骨格 + 認証 | operator/approver キー 2 段・decided_by は認証主体から・localhost bind |
| 2 | 読み取り系 + ask | GET status/log/activity/approvals + POST /ask (回答専用) — App/core_lock 共有・API 専用 conn |
| 3 | 承認系 | approve/reject (冪等・409)・live_trade の TOCTOU 再検証は Phase 3 前提の骨格のみ |
| 4 | 変更系 + autopilot 制限 | policy/backlog/improve/news/model + trading+autopilot 中の全面拒否 |
| 5 | client.py | 対話クライアント (dispatch 共有)・番号付きモデル選択 |
| 6 | news 承認フロー | fetcher 自動判定 + 機械検証 + agent 追加時の approval |
| 7 | Discord bot 連携 | polling 用エンドポイント・message_id reconcile (bot 本体は別リポジトリ) |
| 8 | E2E | キー 2 段・autopilot 拒否・ask 発注不可の回帰 |

**受入条件**: 「載せない一線」(発注操作・資金設定・mode・autopilot・停止) のエンドポイント不存在テスト / ask から TradeIntent 経路が無いこと。

---

## 全体の完了条件 (Phase 2 done の定義)

§15 Phase 2 の列挙項目がすべて実装され、①改善ループが「バックログ → リサーチ → **plugin 提案 (approval) / 提案レポート** → 人間承認 → 採用」を一巡できる (**設計書改訂 17 で PR 経路は廃止**。本体コードの変更は改善ループの能力に無く、提案レポート止まり) ②strategy シグナルがエグジット先行で Mission を前倒し起動できる ③操作が client.py / Discord から可能 ④preemption 受入条件充足 — の 4 点を、各プランの節目レビュー + Phase 2 最終ブランチレビューで確認する。
