# agentic-fx 設計書

- 日付: 2026-07-25 (改訂第 2 版 — レビューフィードバック 9 項目を反映)
- ステータス: 承認待ち
- 前身: `~/project/finance` (IFD 計画型 FX 自動トレードシステム)

## 1. 背景と目的

前身の finance は「定期実行のニュース分析 + テクニカル分析 → plan (IFD) 生成 → 発注」という固定パイプライン型だった。直近週の成績は勝率 31.8%、PF 0.02、22 件中 20 件が stop_loss 決済であり、計画時点の静的な判断が市場の変化に追随できないという構造的限界が確認された。

agentic-fx はこれを **agent loop 型**に置き換える。LLM agent が「今必要な情報」をツールで自ら収集して取引判断を下し、別の agent が成績を分析してシステム自体を改善していく。情報軸はこれまで通り**ニュースとテクニカルの2軸**を維持する。

## 2. 制約 (確定事項)

- LLM 基盤は**ローカル LLM が基本** (llama-swap, OpenAI 互換 API, :8080)。Claude はサブスクリプション認証でのみ利用可。**Anthropic API (従量課金) は使用しない**
- Claude 利用の課金実態 (2026-07 時点): `claude -p` / Agent SDK とも月次 Agent SDK クレジット枠から消費される。**usage credits (追加課金) は有効にしない** — クレジット枯渇時は ClaudeRunner が停止するだけで、従量課金は構造的に発生しない。枯渇時も local への自動フォールバックはしない (挙動を予測可能に保つ)
- 両 loop とも LLM バックエンドは config で `local` / `claude` に切り替え可能。デフォルトは両方 `local`
- 戦略改善 loop の変更採用は **PR + 人間承認**。自動マージは行わない
- 発注・クローズ・SL/TP 変更・資金保護は LLM に委ねず、決定論的コードで強制する (例外: 未約定指値の取消のみ LLM に許可 — 資金リスクがゼロのため)
- GitHub 公開を前提とする (秘密情報は `.env` / gitignore 管理)
- 前身リポジトリの実証済み部品は「agent のツール」として移植する。判断系 (orchestrator / planner / IFD / signal_combiner) は移植しない

## 3. 全体アーキテクチャ

```
┌─────────────────────────────────────────────────┐
│ 決定論的コア                                      │
│  scheduler / data layer / risk gate / executor  │
│  state store / notifier (Discord)               │
└──────┬──────────────────────────┬───────────────┘
       │ ツール提供 + 判断依頼       │ 成績データ + PR 受付
┌──────▼───────────┐      ┌───────▼──────────────┐
│ 取引判断 loop      │      │ 戦略改善 loop          │
│ (毎時・読取専用     │      │ (週次・コード編集可・    │
│  + ask ワンショット) │      │  Web リサーチ可)       │
└──────┬───────────┘      └───────┬──────────────┘
       │        AgentRunner 抽象層  │
       └──────┬───────────┬────────┘
        LocalRunner   ClaudeRunner
        (llama-swap)  (Claude Agent SDK, サブスク認証)
```

原則: **LLM が自走するのは「判断」と「改善提案」まで**。金を動かす経路 (発注・クローズ・SL/TP 変更) と資金保護は常に決定論的コアが握る。

## 4. AgentRunner 抽象層

```python
@dataclass
class Mission:
    prompt: str              # 任務指示 (policy/directives.md + 状態サマリを注入済み)
    tools: list[str]         # 許可ツール名 (レジストリのサブセット)
    output_schema: dict      # 最終出力の JSON Schema
    max_turns: int
    timeout_sec: float

@dataclass
class MissionResult:
    status: Literal["completed", "failed", "timeout", "max_turns"]
    output: dict | None      # output_schema で検証済み
    transcript: list[dict]   # 全ツール呼び出しと応答 (監査・デバッグ用)

class AgentRunner(ABC):
    def run(self, mission: Mission) -> MissionResult: ...
```

### LocalRunner

llama-swap の OpenAI 互換 API (`/v1/chat/completions`) に対する自前 tool-calling loop。

- ループ: 応答に tool_calls があれば実行して継続、なければ最終出力を `output_schema` で検証して終了
- `max_turns` / `timeout_sec` 超過で強制終了 (status に反映)。llama-swap の TTL デッドロック障害 (既知) に対しても `timeout_sec` が防御線となる
- JSON 崩れは修復ロジック (前身の `response_parser` を移植: `<think>` 除去、コードフェンス剥がし) を通し、修復不能ならそのターンをリトライ (上限あり)
- スキーマ検証失敗時はエラー内容を LLM に返して再出力させる (上限 2 回)

### ClaudeRunner (Claude Agent SDK)

**`claude-agent-sdk` (PyPI, MIT License) を採用する**。subprocess + stdio MCP サーバーの自前実装はしない。

- ツールレジストリを SDK の in-process MCP サーバーとして公開し、`allowed_tools` で Mission の許可リストに制限
- 認証は `claude login` (サブスクリプション)。API キーは使わない
- SDK は依存として `pyproject.toml` に宣言するのみ。SDK にバンドルされる Claude Code CLI は Anthropic が PyPI 経由で配布するものであり、本リポジトリは何も再配布しないため OSS 公開に支障はない
- 出力は SDK の structured output / JSON を `output_schema` で検証

### ツールレジストリ

ツールは 1 箇所 (`src/tools/`) に定義し、次の 3 形態に自動変換する:
1. LocalRunner 向け function calling スキーマ
2. ClaudeRunner 向け in-process MCP ツール (Agent SDK)
3. pytest から直接呼べる素の Python 関数

## 5. 取引判断 loop

### 起動と頻度

- 市場オープン中 (前身の `market_hours.py` を移植)、**1 時間毎に必ず実行する**。ポジションがなくても回し、「見送り (hold)」も判断として記録する — hold 判断も成績分析・振り返りのデータになる
- エラー時はスキップして次周期 (リトライしない)。同時実行は常に 1 (agent 実行のグローバル排他)
- **ワンショットのユーザープロンプト**: CLI `ask "..."` で排他スロットを取得して**臨時 Mission を即時実行**する。プロンプトはその 1 回だけ Mission に注入され (ワンショット)、結果は標準出力と Discord に返す。定期周期には影響しない。永続的な方針にしたい場合は `policy add` を使う

### Mission プロンプト構成

上から順に注入する。①② は決定論的コードが毎回生成する:

1. 固定システム指示 + `policy/directives.md` (末尾 4000 文字)
2. **状態サマリ** (常時注入): 口座残高・累計/日次 P&L・現在ポジション・未約定指値・直近 10 件のトレード 1 行要約 (ペア/方向/損益/クローズ理由)・24 時間以内の経済指標予定
3. ワンショットプロンプト (臨時実行時のみ)

詳細情報は agent がツールで取得する (**すべて読み取り専用**):

- `get_ohlcv(pair, timeframe)` — 価格データ (SQLite キャッシュ付き)
- `get_indicators(pair, timeframe)` — テクニカル指標 (MTF リサンプル込み)
- `search_news(query)` — ChromaDB RAG のニュース検索
- `get_econ_calendar(days)` — 経済指標カレンダー (SQLite)
- `get_positions()` / `get_account()` — 現在ポジション・残高
- `get_recent_reflections(pair, n)` / `search_reflections(query)` — 過去トレードの振り返り (意味検索含む)

### 出力: TradeIntent v2

```json
{
  "action": "open | close | cancel | hold",
  "order_id": 123,
  "pair": "USDJPY",
  "direction": "long | short",
  "entry_type": "market | limit",
  "limit_price": 148.20,
  "expires_in": "4h",
  "stop_loss": 147.80,
  "take_profit": 149.00,
  "confidence": 0.7,
  "reasoning": "..."
}
```

- **発注方式はハイブリッド**: `market` は即時発注、`limit` は有効期限付き指値。IFD 的な条件監視ループは作らない
- `limit_price` / `expires_in` は `entry_type: limit` のみ。`expires_in` の上限は 24h。期限切れは scheduler が決定論的に自動取消
- `stop_loss` は open 時必須。SL/TP は発注時にブローカー (paper/live) 側に添付して管理する
- `close` / `cancel` は対象を `order_id` で指定する (状態サマリに ID を含めて提示する)
- `cancel` は未約定指値の取消 (資金リスクゼロのため LLM に許可)。**約定済みポジションの SL/TP 変更は LLM 不可** (決定論的な資金保護のみが変更できる)
- 次周期の agent は状態サマリで未約定指値を見て、取消/維持も判断できる

### Risk Gate (決定論的、LLM の外)

TradeIntent は発注前に全ルールを通過しなければならない。1 つでも違反すれば却下し、却下理由を記録・通知する:

| ルール | 初期値 |
|---|---|
| 最小リスク報酬比 (RR) | 1.5 |
| drawdown kill switch | 累計 P&L -2% で新規停止 (常時有効、config で無効化不可) |
| 最大同時ポジション | 2 (未約定指値も枠にカウント) |
| 日次損失上限 | -1% で当日新規停止 |
| SL 必須 | SL なしの open は無条件却下 |
| limit 乖離上限 | limit_price が現値から 0.5% 超乖離なら却下 (config 可) |
| 指値期限上限 | expires_in > 24h は却下 |

### Executor

Phase 1 はペーパー取引のみ (前身の PositionManager 簡素版 + SQLite `orders` 状態機械)。指値のペーパー約定判定は毎分の価格監視で行う。MT5 ブリッジは Phase 3 で接続し、live 切替は config ではなく**人間の明示的な CLI 操作**でのみ行う。

## 6. 戦略改善 loop

- 起動: **週次** (config で日次に変更可)。手動起動 (`improve` コマンド) も可
- 前提: local バックエンド (qwen3.6 クラス) ではコード編集の品質が揺れる。品質ゲートで守り、不足なら config 1 行で `claude` に切替える

### Mission 構成: 発見 → リサーチ → 実施 (3 ステップ 1 Mission)

1. **発見**: 注入されたコンテキストから課題を特定し、`improvement_backlog` に追記する
2. **リサーチ**: `web_search` / `fetch_article` ツールで「自分が知らないテクニカル手法・ニュースソース」を外部から調査し、候補をバックログに追記する。LLM は自分に何が足りないか自覚できない (unknown unknowns) ため、外部知識の取り込みステップを明示的に組み込む
3. **実施**: バックログから最も効果的な 1 件を選んで実装し、テスト・評価して PR を出す

### 注入コンテキスト (決定論的コードが集計・生成)

- **成績レポート**: 勝率 / PF / ペア別 / 時間帯別 / 却下された intent の内訳 / hold 率
- **改善履歴**: 過去の improvement_runs (何を試し、採用/却下されたか)
- **現行構成インベントリ**: ニュース取得先一覧・実装済みテクニカル指標一覧・risk gate 現行値 — 「何を追加できるか」を判断させるための自己認識材料

### 改善バックログ

- SQLite `improvement_backlog` テーブル。source: `user` / `agent` / `research`、status: `open` / `selected` / `done` / `rejected`
- ユーザーは CLI `improve add "アイデア"` でいつでも投入できる (収集へのユーザー入力受付)

### 許可ツール

成績 DB 読取、`web_search` / `fetch_article` (無料実装: ddgs + 前身 article_fetcher 移植)、リポジトリのファイル読み書き (**専用ブランチ上のみ**)、`uv run pytest` 実行、バックテスト実行、PR 作成 (`gh`)、バックログ読み書き

### 品質ゲート (loop 側で強制)

- テストが 1 件でも落ちる変更は PR にしない。分析レポート (`reports/improve-YYYY-MM-DD.md`) だけ残す
- main への直 push は不可 (branch protection + ツール実装で二重に防ぐ)
- **採用は必ず PR + 人間承認**

## 7. ユーザー入力チャネル (3 系統)

| チャネル | 用途 | 消化タイミング |
|---|---|---|
| `policy add "..."` → `policy/directives.md` | 永続的な方針 (例: 「USDJPY は当面見送り」) | 全 Mission に毎回全文注入 (末尾 4000 文字) |
| `ask "..."` | ワンショットの質問・指示 | 臨時 Mission を即時実行、その 1 回だけ注入 |
| `improve add "..."` | 改善アイデアの投入 | 次回の改善 loop がバックログから選択 |

`policy/directives.md` はタイムスタンプ付き追記 (削除は手動編集)。サイズ超過時は起動時に警告する。

## 8. 前身からの移植資産

| 移植 (ツール/コア部品化) | 移植しない |
|---|---|
| PriceProvider + SQLite キャッシュ | orchestrator / planner / IFD 系一式 (trade_plans / order_intents / 条件監視) |
| 指標計算 + MTF リサンプル (`resample.py`, `mtf.py`, `technical_scorer.py`) | signal_combiner の weight 合成 |
| ニュース収集 + ChromaDB RAG (embedder 含む) | shadow metrics / cadence resolver |
| 経済指標カレンダー (`econ_event_store` → SQLite 一本化) | RAG の insights / econ_analyses コレクション |
| **reflection 機構 (`cycles/reflection.py` + `analysis/reflector.py`)** | candle_patterns (当面。必要になれば改善 loop が提案) |
| **資金保護系 (`position_protection.py` / `portfolio_guard.py` / `bridge_health_gate.py`、Phase 3 で本格接続)** | 旧 config 3 分割 merge 機構 |
| **article_fetcher (改善 loop のリサーチ用)** | TUI / REST API サーバー |
| MT5 ブリッジ (Phase 3) | PriorityJobSlot の完全版 (簡素版を新規実装) |
| Discord 通知 | 小物ユーティリティ (circuit_breaker / job_guard 等は必要時に個別判断) |
| PositionManager (簡素化) + StateStore (atomic write) | |
| `response_parser` (JSON 修復) | |
| `market_hours.py` | |

移植はコピーして新リポジトリの規約に合わせて整理する (依存として旧リポジトリを参照しない)。

## 9. 設定・ストレージ

- 設定: `config/settings.yaml` 1 ファイル (gitignore) + `config/settings.yaml.example` (コミット)。前身の 3 分割はしない
- 秘密情報: `.env` (`DISCORD_WEBHOOK_URL`, `TWELVEDATA_API_KEY`, `FEEDLY_ACCESS_TOKEN` など)

### SQLite スキーマ (`data/agentic.db`、前身 18 テーブル → 9 テーブルに再設計)

| テーブル | 内容 |
|---|---|
| `ohlcv` | 価格データ (symbol, interval, bar_time, OHLCV)。前身と同形 |
| `missions` | 全 Mission 実行記録 (loop 種別, runner, status, output_json, transcript_json) |
| `trade_intents` | LLM の全出力 + risk gate 判定 (accepted / rejected + 却下理由) |
| `orders` | ポジション/指値の単一状態機械: pending → open → closed / cancelled / expired。entry_type, SL/TP, realized_pnl, close_reason |
| `reflections` | トレード振り返り (order_id 主キー。前身から簡素化) |
| `account_snapshots` | 残高・エクイティ推移 (kill switch 判定と成績レポートの根拠) |
| `improvement_backlog` | 改善アイデア (source: user/agent/research, status: open/selected/done/rejected) |
| `improvement_runs` | 改善実施記録 (選択 backlog, PR URL / レポートパス) |
| `econ_events` | 経済指標カレンダー (前身 econ_event_store 移植) |

### ChromaDB (`data/rag/`、前身 4 コレクション → 2 コレクション)

- `news` — ニュース記事 (48h で掃除)
- `reflections` — トレード振り返り (類似局面の意味検索用。SQLite と二重保存)

insights / econ_analyses は廃止。必要になれば改善 loop 自身が PR でコレクション追加を提案できる。

すべて gitignore (`data/`)。

## 10. エラーハンドリング

- LocalRunner: max_turns / timeout / JSON 修復不能 → MissionResult.status に記録し、その周期は「判断なし」として終了。ログ + Discord 通知。llama-swap TTL デッドロック時も timeout_sec で必ず抜ける
- ClaudeRunner: SDK エラー・レート制限・クレジット枯渇 → 同上 (local への自動フォールバックはしない。挙動を予測可能に保つ)
- 全 MissionResult (transcript 含む) を SQLite `missions` に保存し、後から「なぜこの判断をしたか」を追跡可能にする

## 11. テスト戦略

- TDD (failing test → 実装 → green)
- **FakeRunner**: 定型 MissionResult を返す AgentRunner 実装。loop 制御・risk gate・executor・policy 注入・ask 差し込みを LLM なしで全件テスト
- LocalRunner のループ制御は httpx モック (tool_calls 応答系列) でテスト
- risk gate は全ルール × 境界値のテーブルテスト (limit 乖離・期限上限含む)
- 指値のペーパー約定判定・期限切れ取消はクロックモックでテスト
- CI (GitHub Actions): pytest を PR 必須チェックにする (改善 loop の品質ゲートの土台)

## 12. 段階導入

- **Phase 1**: 決定論的コア + LocalRunner + 取引判断 loop (ペーパー、ハイブリッド発注) + `ask` ワンショット。ツールは get_ohlcv / get_indicators / search_news / get_positions の最小セット
- **Phase 2**: ClaudeRunner (Agent SDK) + 戦略改善 loop (バックログ + Web リサーチ) + policy チャネル
- **Phase 3**: MT5 ブリッジ接続 + 資金保護系の本格接続 + live 切替 (人間の明示操作のみ)

## 13. 非スコープ (YAGNI)

- REST API サーバー (前身にはあったが、必要になるまで作らない。操作は CLI + Discord 通知)
- 改善の自動採用 (完全自動マージ)
- local ⇔ claude の自動フォールバック / エスカレーション
- IFD 的な条件監視付き予約注文 (前身の構造的限界の原因。指値 + 期限で代替)
- マルチアセット対応 (FX 2 ペアから始める)
