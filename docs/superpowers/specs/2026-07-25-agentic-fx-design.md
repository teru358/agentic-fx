# agentic-fx 設計書

- 日付: 2026-07-25
- ステータス: 承認待ち
- 前身: `~/project/finance` (IFD 計画型 FX 自動トレードシステム)

## 1. 背景と目的

前身の finance は「定期実行のニュース分析 + テクニカル分析 → plan (IFD) 生成 → 発注」という固定パイプライン型だった。直近週の成績は勝率 31.8%、PF 0.02、22 件中 20 件が stop_loss 決済であり、計画時点の静的な判断が市場の変化に追随できないという構造的限界が確認された。

agentic-fx はこれを **agent loop 型**に置き換える。LLM agent が「今必要な情報」をツールで自ら収集して取引判断を下し、別の agent が成績を分析してシステム自体を改善していく。情報軸はこれまで通り**ニュースとテクニカルの2軸**を維持する。

## 2. 制約 (確定事項)

- LLM 基盤は**ローカル LLM が基本** (llama-swap, OpenAI 互換 API, :8080)。Claude はサブスクリプション内の `claude -p` (headless Claude Code) でのみ利用可。**Anthropic API (従量課金) は使用しない**
- 両 loop とも LLM バックエンドは config で `local` / `claude` に切り替え可能。デフォルトは両方 `local`
- 戦略改善 loop の変更採用は **PR + 人間承認**。自動マージは行わない
- 発注・資金保護は LLM に委ねず、決定論的コードで強制する
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
│ (高頻度・読取専用)  │      │ (低頻度・コード編集可)   │
└──────┬───────────┘      └───────┬──────────────┘
       │        AgentRunner 抽象層  │
       └──────┬───────────┬────────┘
        LocalRunner   ClaudeRunner
        (llama-swap)  (claude -p, サブスク内)
```

原則: **LLM が自走するのは「判断」と「改善提案」まで**。金を動かす経路 (発注・クローズ・SL 変更) と資金保護は常に決定論的コアが握る。

## 4. AgentRunner 抽象層

```python
@dataclass
class Mission:
    prompt: str              # 任務指示 (policy/directives.md を注入済み)
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
- `max_turns` / `timeout_sec` 超過で強制終了 (status に反映)
- JSON 崩れは修復ロジック (前身の `response_parser` を移植: `<think>` 除去、コードフェンス剥がし) を通し、修復不能ならそのターンをリトライ (上限あり)
- スキーマ検証失敗時はエラー内容を LLM に返して再出力させる (上限 2 回)

### ClaudeRunner

`claude -p --output-format json` を subprocess 起動。ツールはレジストラが MCP サーバー (stdio) として同じツールレジストリを公開し、`--allowedTools` で Mission の許可リストに制限する。API キー不要、サブスクリプション認証。

### ツールレジストリ

ツールは 1 箇所 (`src/tools/`) に定義し、次の 3 形態に自動変換する:
1. LocalRunner 向け function calling スキーマ
2. ClaudeRunner 向け MCP ツール
3. pytest から直接呼べる素の Python 関数

## 5. 取引判断 loop

- 起動: 市場オープン中 (前身の `market_hours.py` を移植)、**1 時間毎**。エラー時はスキップして次周期 (リトライしない)
- 同時実行は常に 1 (agent 実行のグローバル排他。前身 PriorityJobSlot の簡素版)
- Mission: 「対象ペアの市場状況を調査し、取引意図または見送りを出せ」
- 許可ツール (**すべて読み取り専用**):
  - `get_ohlcv(pair, timeframe)` — 価格データ (SQLite キャッシュ付き)
  - `get_indicators(pair, timeframe)` — テクニカル指標 (MTF リサンプル込み)
  - `search_news(query)` — ChromaDB RAG のニュース検索
  - `get_econ_calendar(days)` — 経済指標カレンダー
  - `get_positions()` / `get_account()` — 現在ポジション・残高
  - `get_recent_reflections(n)` — 過去トレードの振り返り
- 出力 (`TradeIntent`): `{action: open|close|hold, pair, direction, entry, stop_loss, take_profit, confidence, reasoning}`

### Risk Gate (決定論的、LLM の外)

TradeIntent は発注前に全ルールを通過しなければならない。1 つでも違反すれば却下し、却下理由を記録・通知する:

| ルール | 初期値 |
|---|---|
| 最小リスク報酬比 (RR) | 1.5 |
| drawdown kill switch | 累計 P&L -2% で新規停止 (常時有効、config で無効化不可) |
| 最大同時ポジション | 2 |
| 日次損失上限 | -1% で当日新規停止 |
| SL 必須 | SL なしの open は無条件却下 |

### Executor

Phase 1 はペーパー取引のみ (前身の PositionManager 簡素版 + JSON state)。MT5 ブリッジは Phase 3 で接続し、live 切替は config ではなく**人間の明示的な CLI 操作**でのみ行う。

## 6. 戦略改善 loop

- 起動: **週次** (config で日次に変更可)。手動起動 (`improve` コマンド) も可
- Mission: 「直近成績と方針書を読み、最も効果的な改善を 1 つ実施し、評価して PR を出せ」
- 改善対象は限定しない (agent の判断に委ねる)。想定例: risk gate パラメータの調整提案、**ニュース取得先の追加**、**テクニカル分析手法の追加** (コード)、取引判断 Mission のプロンプト改善
- 許可ツール: 成績 DB 読取、リポジトリのファイル読み書き (**専用ブランチ上のみ**)、`uv run pytest` 実行、バックテスト実行、PR 作成 (`gh`)
- 品質ゲート (loop 側で強制):
  - テストが 1 件でも落ちる変更は PR にしない。分析レポート (`reports/improve-YYYY-MM-DD.md`) だけ残す
  - main への直 push は不可 (branch protection + ツール実装で二重に防ぐ)
- **採用は必ず PR + 人間承認**
- local バックエンド (qwen3.6 クラス) ではコード編集の品質が揺れる前提。品質ゲートで守り、不足なら config 1 行で `claude` に切替える

## 7. ユーザー方針チャネル

- `policy/directives.md` — ユーザーが方針を書く場所。両 loop の Mission プロンプトに毎回全文注入する
- CLI から `policy add "USDJPY は当面見送り"` で追記可能 (タイムスタンプ付き。削除は手動編集)
- サイズ上限 (注入は末尾 4000 文字まで) を設け、超過時は起動時に警告する

## 8. 前身からの移植資産

| 移植 (ツール/コア部品化) | 移植しない |
|---|---|
| PriceProvider + SQLite キャッシュ | orchestrator / planner / IFD 系一式 |
| 指標計算 + MTF リサンプル (`resample.py`, `mtf.py`, `technical_scorer.py`) | signal_combiner の weight 合成 |
| ニュース収集 + ChromaDB RAG (embedder 含む) | shadow metrics / cadence resolver |
| 経済指標カレンダー | 旧 config 3 分割 merge 機構 |
| MT5 ブリッジ (Phase 3) | TUI |
| Discord 通知 | PriorityJobSlot の完全版 (簡素版を新規実装) |
| PositionManager (簡素化) + StateStore (atomic write) | |
| `response_parser` (JSON 修復) | |
| `market_hours.py` | |

移植はコピーして新リポジトリの規約に合わせて整理する (依存として旧リポジトリを参照しない)。

## 9. 設定・ストレージ

- 設定: `config/settings.yaml` 1 ファイル (gitignore) + `config/settings.yaml.example` (コミット)。前身の 3 分割はしない
- 秘密情報: `.env` (`DISCORD_WEBHOOK_URL`, `TWELVEDATA_API_KEY`, `FEEDLY_ACCESS_TOKEN` など)
- ストレージ: SQLite (`data/prices.db` — OHLCV / 成績 / mission transcript)、ChromaDB (`data/rag/`)、JSON (`data/state/`)。すべて gitignore

## 10. エラーハンドリング

- LocalRunner: max_turns / timeout / JSON 修復不能 → MissionResult.status に記録し、その周期は「判断なし」として終了。ログ + Discord 通知
- ClaudeRunner: subprocess 非ゼロ終了・レート制限 → 同上 (local への自動フォールバックはしない。挙動を予測可能に保つ)
- 全 MissionResult (transcript 含む) を SQLite に保存し、後から「なぜこの判断をしたか」を追跡可能にする

## 11. テスト戦略

- TDD (failing test → 実装 → green)
- **FakeRunner**: 定型 MissionResult を返す AgentRunner 実装。loop 制御・risk gate・executor・policy 注入を LLM なしで全件テスト
- LocalRunner のループ制御は httpx モック (tool_calls 応答系列) でテスト
- risk gate は全ルール × 境界値のテーブルテスト
- CI (GitHub Actions): pytest を PR 必須チェックにする (改善 loop の品質ゲートの土台)

## 12. 段階導入

- **Phase 1**: 決定論的コア + LocalRunner + 取引判断 loop (ペーパー)。ツールは get_ohlcv / get_indicators / search_news / get_positions の最小セット
- **Phase 2**: ClaudeRunner (切替の実証) + 戦略改善 loop + policy チャネル
- **Phase 3**: MT5 ブリッジ接続 + live 切替 (人間の明示操作のみ)

## 13. 非スコープ (YAGNI)

- REST API サーバー (前身にはあったが、必要になるまで作らない。操作は CLI + Discord 通知)
- 改善の自動採用 (完全自動マージ)
- local ⇔ claude の自動フォールバック / エスカレーション
- マルチアセット対応 (FX 2 ペアから始める)
