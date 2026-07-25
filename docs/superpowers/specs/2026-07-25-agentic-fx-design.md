# agentic-fx 設計書

- 日付: 2026-07-26 (改訂第 6 版 — main モデルへ変更: main.py 対話シェル付きサービス + client.py 操作クライアント、typer ワンショット CLI 廃止)
- ステータス: 承認待ち
- 前身: `~/project/finance` (IFD 計画型 FX 自動トレードシステム)

## 1. 背景と目的

前身の finance は「定期実行のニュース分析 + テクニカル分析 → plan (IFD) 生成 → 発注」という固定パイプライン型だった。直近週の成績は勝率 31.8%、PF 0.02、22 件中 20 件が stop_loss 決済であり、計画時点の静的な判断が市場の変化に追随できないという構造的限界が確認された。

agentic-fx はこれを **agent loop 型**に置き換える。LLM agent が「今必要な情報」をツールで自ら収集して取引判断を下し、別の agent が成績を分析してシステム自体を改善していく。情報軸はこれまで通り**ニュースとテクニカルの2軸**を維持する。

**開発方針**: 初期実装は「基本構造 + 決定論的コア + plugin 機構 + 組み込みデフォルト実装」に絞り、拡張 (ニュースソース追加・テクニカル指標追加) は改善ループの LLM に実装させる。システム自体が自己拡張していくことを前提とした最小構造を人間側が TDD で作る。

## 2. 制約 (確定事項)

- LLM 基盤は**ローカル LLM が基本** (llama-swap, OpenAI 互換 API, :8080)。Claude はサブスクリプション認証でのみ利用可。**Anthropic API (従量課金) は使用しない**
- Claude 利用の課金実態 (2026-07 時点): `claude -p` / Agent SDK とも月次 Agent SDK クレジット枠から消費される。**usage credits (追加課金) は有効にしない** — クレジット枯渇時は ClaudeRunner が停止するだけで、従量課金は構造的に発生しない。枯渇時も local への自動フォールバックはしない (挙動を予測可能に保つ)
- 両 loop とも LLM バックエンドは config で `local` / `claude` に切り替え可能。デフォルトは両方 `local`
- 戦略改善 loop の変更採用は**必ず人間承認**を通す (トラック別のゲートは §6)
- 発注・クローズ・SL/TP 変更・資金保護は LLM に委ねず、決定論的コードで強制する (例外: 未約定指値の取消のみ LLM に許可 — 資金リスクがゼロのため)
- **live モードの新規発注は Discord/CLI の人間承認を最終ゲートとする (config で無効化不可)**
- GitHub 公開を前提とする (秘密情報は `.env` / gitignore 管理)。LLM が生成する plugin はユーザーごとに異なるため gitignore する
- 前身リポジトリの実証済み部品は「agent のツール」として移植する。判断系 (orchestrator / planner / IFD / signal_combiner) は移植しない

## 3. 全体アーキテクチャ

```
┌──────────────────────────────────────────────────────┐
│ 決定論的コア                                           │
│  scheduler / data layer / risk gate / executor       │
│  state store / notifier / 操作 API (client.py, bot 用) │
└──────┬──────────────────────┬────────────┬───────────┘
       │ ツール提供 + 判断依頼   │ 成績 + PR   │ approval_requests
┌──────▼───────────┐  ┌───────▼─────────┐ ┌▼──────────────────┐
│ 取引判断 loop      │  │ 戦略改善 loop     │ │ discord_bot        │
│ (毎時・読取専用     │  │ (週次・コード編集  │ │ (別リポジトリ,      │
│  + ask ワンショット) │  │  + Web リサーチ)  │ │  承認ボタン/status) │
└──────┬───────────┘  └───────┬─────────┘ └───────────────────┘
       │      AgentRunner 抽象層│
       └──────┬───────────┬────┘
        LocalRunner   ClaudeRunner
        (llama-swap)  (Claude Agent SDK, サブスク認証)
```

原則: **LLM が自走するのは「判断」と「改善提案」まで**。金を動かす経路 (発注・クローズ・SL/TP 変更) と資金保護は常に決定論的コアが握り、live の新規発注はさらに人間の承認を最終ゲートとする。

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

ツールは 1 箇所 (`src/agentic_fx/tools/`) に定義し、次の 3 形態に自動変換する:
1. LocalRunner 向け function calling スキーマ
2. ClaudeRunner 向け in-process MCP ツール (Agent SDK)
3. pytest から直接呼べる素の Python 関数

`get_indicators` は組み込み指標 + **承認済み tech plugin** (§6) を合成して返す。`search_news` は `news_sources` (§6) に登録された全ソースから収集済みの RAG を検索する。

## 5. 取引判断 loop

### 起動と頻度

- 市場オープン中 (前身の `market_hours.py` を移植)、**1 時間毎に必ず実行する**。ポジションがなくても回し、「見送り (hold)」も判断として記録する — hold 判断も成績分析・振り返りのデータになる
- エラー時はスキップして次周期 (リトライしない)。同時実行は常に 1 (agent 実行のグローバル排他)
- **ワンショットのユーザープロンプト**: main.py 対話シェルまたは client.py の `ask "..."` で**臨時 Mission を即時実行**する (実行は常にサービスプロセス内の排他スロット)。プロンプトはその 1 回だけ Mission に注入され (ワンショット)、結果は呼び出し元と Discord に返す。定期周期には影響しない。永続的な方針にしたい場合は `policy add` を使う

### Mission プロンプト構成

上から順に注入する。①② は決定論的コードが毎回生成する:

1. 固定システム指示 + `policy/directives.md` (末尾 4000 文字)
2. **状態サマリ** (常時注入): 口座残高・累計/日次 P&L・現在ポジション・未約定指値・直近 10 件のトレード 1 行要約 (ペア/方向/損益/クローズ理由)・24 時間以内の経済指標予定
3. ワンショットプロンプト (臨時実行時のみ)

詳細情報は agent がツールで取得する (**すべて読み取り専用**):

- `get_ohlcv(pair, timeframe)` — 価格データ (SQLite キャッシュ付き)
- `get_indicators(pair, timeframe)` — テクニカル指標 (MTF リサンプル込み、承認済み tech plugin を含む)
- `search_news(query)` — ChromaDB RAG のニュース検索 (news_sources の全承認済みソースを含む)
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

### Executor と live 承認ゲート

- Phase 1 はペーパー取引のみ (前身の PositionManager 簡素版 + SQLite `orders` 状態機械)。指値のペーパー約定判定は毎分の価格監視で行う。paper モードでは risk gate 通過後に自動発注する
- MT5 ブリッジは Phase 3 で接続し、live 切替は config ではなく**人間の明示的な CLI 操作**でのみ行う
- **live モードの最終承認ゲート (Phase 3)**: risk gate を通過した open intent は `approval_requests` (kind=live_trade) に登録され、**人間の承認 (Discord ボタン or CLI) が下りるまで発注しない**。expires_at (指値は指値期限、market は 15 分) を過ぎると自動 expired となり発注しない。このゲートは kill switch と同様 **config で無効化不可**。close / cancel は資金保護方向の操作なので承認不要で即時実行する

## 6. 戦略改善 loop

- 起動: **週次** (config で日次に変更可)。手動起動 (`improve` コマンド) も可
- 前提: local バックエンド (qwen3.6 クラス) ではコード編集の品質が揺れる。品質ゲートで守り、不足なら config 1 行で `claude` に切替える

### 出力の 3 経路 (人間承認の実装手段が異なる)

| 経路 | 対象 | 置き場所 | 承認ゲート |
|---|---|---|---|
| **コア改善** | risk gate パラメータ提案、Mission プロンプト改善、plugin 機構・news fetcher 自体の修正 | リポジトリ内 (git 管理) | **PR + 人間承認** (自動マージ禁止) |
| **tech plugin 追加** | テクニカル指標の実装 (コード) | `plugins/tech/` (**gitignore**) | **approval_requests (kind=tech_plugin)** — Discord ボタン or CLI。承認前の plugin はロードされない |
| **news ソース追加** | ニュース取得先の追加 (**データ 1 行、コードなし**) | SQLite `news_sources` テーブル | **approval_requests (kind=news_source)** — 機械検証 (URL 到達性・parse 成功・重複) を自動実行した上で軽量な人間承認 |

plugin を gitignore するのは、LLM が実装するツール群がプロジェクトを clone したユーザーごとに異なるため。公開リポジトリには plugin 機構と組み込みデフォルト実装のみをコミットする。

### news はソースリスト方式 (plugin ではない)

ニュースの取得方式は実質 **feed parse (RSS/Atom)** / **web fetch (HTML 記事抽出)** の 2 つに収斂するため、fetcher は `src/` の組み込み実装に固定し、**news の拡張はすべて `news_sources` テーブルへのデータ追加**とする (feedly は使わない — 前身の feedly_fetcher は移植しない):

```
news_sources: id, name, fetcher (feed | web), url,
              enabled, added_by (user | agent), status, created_at
```

- 根拠: ①ソース追加はデータ 1 行で機械検証できるが、plugin はコード審査が重い ②新しい取得方式が必要になるのは年数回程度で、news 側に plugin 機構を持つのは YAGNI ③fetcher (HTML 抽出・外部依存) は local LLM が最も失敗しやすいコードであり、コア改善 PR の人間レビューに載せる方が安全
- 新しい取得方式 (認証付き API 等) が必要になった場合は、コア改善トラック (PR) で fetcher を追加する

### tech plugin 機構

- 配置: `plugins/tech/<name>.py` + **テストファイル (`test_<name>.py`) 同梱必須**
- インターフェースは意図的に極小、かつ**純関数に限定** (I/O・外部アクセス禁止): `Indicator.compute(df) -> dict`。qwen3.6 クラスの実装力でも品質が安定し、テストが決定論的になる粒度にする
- `tools/plugin_loader.py` が起動時に discover し、**pytest 合格 + 承認済み** (approval_requests で approved) のもののみレジストリに登録
- 素の clone でも動くよう、組み込みデフォルト実装 (基本指標 + 基本ニュースソース数件の `news_sources` 初期データ) は `src/` 側にコミットする
- サンプル plugin を `docs/examples/plugins/` にコミットし、LLM のリサーチ→実装時の参照テンプレートにする

### Mission 構成: 発見 → リサーチ → 実施 (3 ステップ 1 Mission)

1. **発見**: 注入されたコンテキストから課題を特定し、`improvement_backlog` に追記する
2. **リサーチ**: `web_search` / `fetch_article` ツールで「自分が知らないテクニカル手法・ニュースソース」を外部から調査し、候補をバックログに追記する。LLM は自分に何が足りないか自覚できない (unknown unknowns) ため、外部知識の取り込みステップを明示的に組み込む
3. **実施**: バックログから最も効果的な 1 件を選んで実装し、テスト・評価して提出する (plugin なら approval_request 発行、コア変更なら PR)

### 注入コンテキスト (決定論的コードが集計・生成)

- **成績レポート**: 勝率 / PF / ペア別 / 時間帯別 / 却下された intent の内訳 / hold 率
- **改善履歴**: 過去の improvement_runs (何を試し、採用/却下されたか)
- **現行構成インベントリ**: 組み込み + 承認済み plugin のニュースソース一覧・テクニカル指標一覧・risk gate 現行値 — 「何を追加できるか」を判断させるための自己認識材料

### 改善バックログ

- SQLite `improvement_backlog` テーブル。source: `user` / `agent` / `research`、status: `open` / `selected` / `done` / `rejected`
- ユーザーは `improve add "アイデア"` (対話シェル / client.py) でいつでも投入できる (収集へのユーザー入力受付)

### 許可ツール

成績 DB 読取、`web_search` / `fetch_article` (無料実装: ddgs + 前身 article_fetcher 移植)、リポジトリ・`plugins/tech/` のファイル読み書き (コア変更は**専用ブランチ上のみ**)、news_sources への追加提案、`uv run pytest` 実行、バックテスト実行、PR 作成 (`gh`)、バックログ読み書き、approval_request 発行

### 品質ゲート (loop 側で強制)

- テストが 1 件でも落ちる変更は PR / approval_request にしない。分析レポート (`reports/improve-YYYY-MM-DD.md`) だけ残す
- main への直 push は不可 (branch protection + ツール実装で二重に防ぐ)
- **採用は必ず人間承認** (コア改善 = PR、plugin = approval_requests)

## 7. 承認ゲートと最小 REST API

### approval_requests (SQLite)

人間承認が必要な事象を一元管理する汎用テーブル:

```
id, kind (tech_plugin | news_source | live_trade), payload_json,
status (pending | approved | rejected | expired),
reason, decided_by, decided_at, message_id, expires_at, created_at
```

- `kind=tech_plugin`: 改善ループが plugin を書いたら発行。payload は plugin パス・要約・テスト結果。承認されるまでロードされない
- `kind=news_source`: ニュースソース追加。payload はソース情報 + 機械検証結果 (URL 到達性・parse 成功・取得サンプル)。承認されるまで `news_sources.enabled` にならない
- `kind=live_trade` (Phase 3): live モードの open intent。payload は TradeIntent 全体。expires_at 超過で自動 expired
- 決定の反映は冪等 (二重承認は 409 相当で拒否)

### 操作 REST API (FastAPI)

稼働中サービスへの操作窓口。利用者は **client.py** (§8) と **discord_bot** (§9) の 2 つ。§16 の「汎用 REST API サーバーは作らない」の唯一の例外:

| エンドポイント | 内容 | 主な利用者 |
|---|---|---|
| `GET /status` | 残高・ポジション・直近 mission・kill switch 状態 | client.py / bot |
| `GET /log?n=` | 技術ログの直近 n 行 | client.py |
| `GET /activity?n=&category=` | activity ログの直近 n 行 | client.py |
| `GET /approvals?status=pending` | 承認待ち一覧 (bot が polling) | bot |
| `POST /approvals/{id}/approve` / `reject` | 承認 / 却下 (理由付き) | client.py / bot |
| `POST /approvals/{id}/message` | Discord message_id の保存 (bot の reconcile 用) | bot |
| `POST /ask` | 臨時 Mission の実行依頼 (実行は常にサービスプロセス内) | client.py |
| `POST /policy` | 方針書への追記 | client.py |
| `POST /backlog` | 改善アイデアの投入 | client.py |

- `X-API-Key` 認証 (finance 方式踏襲)
- **載せない一線**: 発注操作・risk gate 等の設定変更・`go-live`・サービス停止。金を動かす経路と重大操作はホスト上の明示操作のみ
- Mission 実行 (`/ask`) も含め、**Mission を実行するのは常にサービスプロセスだけ**。排他制御はプロセス内の Mission スロットで完結する (クロスプロセスロックは不要)

## 8. エントリポイントと操作体系 (main モデル)

エントリは **main.py (サービス本体)** と **client.py (稼働中サービスの操作クライアント)** の 2 つ。前身 finance と同じ運用感を踏襲しつつ、旧 client.py の問題 (ログのストリーミング表示が入力に割り込む) を「**ログ表示の pull 型コマンド化**」で解消する。

### main.py — 対話シェル付きサービス

```
uv run main.py              # サービス起動 + スプラッシュ + コマンド受付 (TTY のとき)
uv run main.py --daemon     # systemd 用: コマンド受付なし (非 TTY 時は自動でこちら)
uv run main.py init         # 設定ウィザード (サービスは起動しない)
uv run main.py bootstrap    # 初期構築ウィザード (サービス非起動、--runner claude|local)
uv run main.py go-live      # live 切替 (Phase 3、人間の明示操作。API には載せない)
```

- 起動時**スプラッシュ**: mode (paper/live)・対象ペア・runner・risk gate 現行値・承認待ち件数・未約定指値・直近成績サマリを 1 画面表示
- **コマンド受付 (対話シェル)**: プロンプトは常に静かで、ログを勝手に流さない。ログは `log` / `activity` コマンドで**必要なときに引く** (pull 型)
- `stop` (または Ctrl-C) で **graceful shutdown**: スケジューラ停止 → 実行中 Mission の完了待ち (タイムアウト付き) → 終了。daemon モードの停止は `systemctl stop` の仕事
- `[project.scripts]` に `afx` として同エントリを登録し、`uv run afx` でも起動可能にする
- **起動ガード**: init 未完了なら通常起動・--daemon とも起動拒否 (systemd の Restart=always でも即終了を繰り返すだけで稼働しない)

### 操作コマンド体系 (main.py 対話シェルと client.py で共通)

コマンド定義・パーサは 1 箇所で共有する。main.py はプロセス内で直接実行、client.py は操作 API (§7) 経由で同じ操作を実行する:

| コマンド | 内容 |
|---|---|
| `status` | 残高・ポジション・直近 mission・kill switch 状態 |
| `log [n]` | **技術ログ**の直近 n 行 (表示して終わり。ストリーミングしない) |
| `activity [n] [news\|tech\|trade\|improve]` | **activity ログ**の直近 n 行 (カテゴリ絞り込み可) |
| `ask "..."` | 臨時 Mission (実行は常にサービスプロセス内の排他スロット) |
| `approve <id>` / `reject <id> [理由]` | 承認操作 |
| `policy add "..."` | 方針書へ追記 |
| `improve add "..."` / `backlog` | 改善アイデア投入 / バックログ一覧 |
| `stop` | graceful shutdown (**main.py 対話シェルのみ**。API には載せない) |

### client.py — 稼働中サービスの操作クライアント

- 操作 API への薄い HTTP クライアント (`X-API-Key`)。`uv run client.py status` のようにワンショット実行し、即終了する
- **--daemon 運用中の手元操作と、cron・シェルスクリプトからの連携**はこちらが担う (finance で実証済みのパターン)
- サービス停止中は使えない。停止中に必要な操作は main.py サブコマンド (init / bootstrap / go-live) と、ログファイルの直接閲覧 (`tail logs/activity.log` — txt ベースなのでそのまま読める) でカバーする

### 初期起動フロー

1. **`uv run main.py init`** — 設定ウィザード (settings.yaml 生成、DB 初期化、llama-swap / Discord / 価格ソースの接続確認)。完了までサービス起動を拒否。systemd unit 化はユーザーが明示的に行う (ドキュメントのみ提供)
2. **`uv run main.py bootstrap`** (任意) — 初期構築ウィザード。対話で必要情報を収集 (取引ペア、リスク許容度、関心のあるニュース分野、好みのソース等) し、Mission に整形して改善 loop を手動起動。runner は `claude` / `local` を選択式 (ClaudeRunner = Agent SDK はサブスク認証で利用可能)。成果 (news_sources 追加・tech plugin) は通常の承認フローに乗る
3. **`uv run main.py`** — サービス開始。bootstrap を省略しても組み込みデフォルト実装 (基本指標 + 基本ニュースソース) で稼働できる

### ユーザー入力チャネル

| チャネル | 用途 | 消化タイミング |
|---|---|---|
| `policy add "..."` → `policy/directives.md` | 永続的な方針 (例: 「USDJPY は当面見送り」) | 全 Mission に毎回全文注入 (末尾 4000 文字) |
| `ask "..."` | ワンショットの質問・指示 | 臨時 Mission を即時実行、その 1 回だけ注入 |
| `improve add "..."` | 改善アイデアの投入 | 次回の改善 loop がバックログから選択 |
| `main.py bootstrap` | 初期構築の指示・必要情報の入力 | 改善 Mission を即時手動起動 |
| Discord ボタン / `approve` | tech plugin・news ソース・live 発注の承認 | approval_requests の決定 |

`policy/directives.md` はタイムスタンプ付き追記 (削除は手動編集)。サイズ超過時は起動時に警告する。

## 9. 外部連携: discord_bot (別リポジトリ)

`~/project/discord_bot` (汎用 cog ベース bot、稼働中) に **`cogs/agentic_fx/` を新設**する。finance cog の実証済み承認ゲート実装 (gate_cog / gate_ui / client / settings) をコピー・改修して再利用する。本設計書が定義するのは §7 の API 契約までで、bot 側の実装は discord_bot リポジトリで agentic-fx の Phase 2 (plugin 承認) / Phase 3 (live_trade 承認) に合わせて行う。

再利用する実証済みパターン:
- polling → ボタン付き embed 投稿 → 決定 POST → 結末反映 (10 秒間隔、認証は X-API-Key)
- `discord.ui.DynamicItem` + custom_id 正規表現 (`agentic_fx_gate:approve:{id}` — finance cog と衝突しない名前空間) で bot 再起動を跨いでボタンが生きる
- 承認者ロール deny-by-default、却下理由 Modal、TOCTOU 再チェック、起動時 reconcile、重複投稿ガード

表示: `kind=tech_plugin` は plugin パス・要約・テスト結果、`kind=news_source` はソース情報 + 機械検証結果、`kind=live_trade` は TradeIntent の内容 (ペア/方向/entry/SL/TP/理由) をそれぞれ embed 表示。`?afx status` は `GET /status` の内容を表示する。

## 10. 前身からの移植資産

| 移植 (ツール/コア部品化) | 移植しない |
|---|---|
| PriceProvider + SQLite キャッシュ | orchestrator / planner / IFD 系一式 (trade_plans / order_intents / 条件監視) |
| 指標計算 + MTF リサンプル (`resample.py`, `mtf.py`, `technical_scorer.py`) | signal_combiner の weight 合成 |
| ニュース収集 + ChromaDB RAG (embedder 含む、feedly_fetcher は除く) | shadow metrics / cadence resolver / feedly_fetcher |
| 経済指標カレンダー (`econ_event_store` → SQLite 一本化) | RAG の insights / econ_analyses コレクション |
| reflection 機構 (`cycles/reflection.py` + `analysis/reflector.py`) | candle_patterns (当面。必要になれば改善 loop が提案) |
| 資金保護系 (`position_protection.py` / `portfolio_guard.py` / `bridge_health_gate.py`、Phase 3 で本格接続) | 旧 config 3 分割 merge 機構 |
| article_fetcher (改善 loop のリサーチ用) | TUI / 汎用 REST API サーバー |
| MT5 ブリッジ (Phase 3) | PriorityJobSlot の完全版 (簡素版を新規実装) |
| Discord 通知 (webhook) | 小物ユーティリティ (circuit_breaker / job_guard 等は必要時に個別判断) |
| PositionManager (簡素化) + StateStore (atomic write) | |
| `response_parser` (JSON 修復) | |
| `market_hours.py` | |
| 承認ゲート API パターン (finance の approval gate spec F-5、X-API-Key 認証) | |

discord_bot 側の finance cog (gate_cog / gate_ui / client) も再利用資産 (§9)。移植はコピーして新リポジトリの規約に合わせて整理する (依存として旧リポジトリを参照しない)。

## 11. ディレクトリ構造

レイヤー型 src レイアウト。**依存方向は一方向** (`loops → runners/tools → datafeed/store → core`) とし、**`core/` は LLM 関連を一切 import しない** (「金を動かす経路は決定論的」の構造的担保)。

```
agentic-fx/
├── pyproject.toml             # [project.scripts] afx = main.py と同エントリ (uv run afx でも起動可)
├── main.py                    # サービス本体エントリ (対話シェル / --daemon / init / bootstrap / go-live, §8)
├── client.py                  # 操作クライアントエントリ (操作 API 経由のワンショット実行, §8)
├── config/
│   ├── settings.yaml.example  # コミット (新規キーは必ず両方同期)
│   └── settings.yaml          # gitignore
├── policy/
│   └── directives.md          # ユーザー方針 (タイムスタンプ付き追記)
├── plugins/                   # gitignore — LLM 生成、ユーザーごとに異なる
│   └── tech/                  #   Indicator 実装 (純関数) + test_*.py 同梱
├── data/                      # gitignore (agentic.db / rag/ / state/)
├── reports/                   # 改善 loop の分析レポート
├── docs/
│   ├── superpowers/specs/     # 設計書
│   └── examples/plugins/      # サンプル plugin (コミット対象、LLM の参照テンプレート)
├── src/agentic_fx/
│   ├── service.py             # サービス起動シーケンス (起動ガード・スプラッシュ・scheduler 統合)
│   ├── shell.py               # 対話シェル (コマンド受付・stop、ログは pull 型)
│   ├── commands.py            # 操作コマンド定義 (main.py シェルと client.py で共有, §8)
│   ├── ops_client.py          # client.py の実体 (操作 API への HTTP クライアント)
│   ├── config.py              # settings.yaml ロード + 検証 (1 ファイル、3 分割しない)
│   ├── policy.py              # directives.md 読込・追記・4000 字注入
│   ├── logging_setup.py       # 技術ログ (severity → logs/agentic.log + logrotate)
│   ├── activity.py            # activity ログ (カテゴリ別イベント → logs/activity.log)
│   ├── core/                  # ── 決定論的コア (LLM を一切 import しない) ──
│   │   ├── scheduler.py       # 毎時起動・排他スロット・指値期限切れ取消
│   │   ├── risk_gate.py       # 全ルール (テーブルテスト対象)
│   │   ├── executor.py        # broker 抽象 + 発注経路 + live 承認ゲート待ち
│   │   ├── paper_broker.py    # ペーパー約定 (指値判定含む)
│   │   ├── market_hours.py    # (移植)
│   │   └── notifier.py        # Discord webhook (移植)
│   ├── store/                 # ── ストレージ層 ──
│   │   ├── db.py              # SQLite 接続 + 11 テーブルスキーマ
│   │   ├── orders.py / missions.py / reflections.py / snapshots.py
│   │   ├── backlog.py / econ_events.py / approvals.py / news_sources.py
│   │   └── rag.py             # ChromaDB (news / reflections)
│   ├── datafeed/              # ── データ収集 (移植が中心) ──
│   │   ├── price_provider.py / indicators.py / resample.py / mtf.py
│   │   ├── news_collector.py  # news_sources を読んで組み込み fetcher で収集
│   │   ├── fetchers.py        # 組み込み fetcher: feed / web (§6)
│   │   ├── embedder.py / article_fetcher.py
│   │   └── econ_calendar.py
│   ├── runners/               # ── AgentRunner 抽象層 ──
│   │   ├── base.py            # Mission / MissionResult / AgentRunner
│   │   ├── local_runner.py    # llama-swap tool-calling loop
│   │   ├── claude_runner.py   # Agent SDK
│   │   ├── fake_runner.py     # テスト用
│   │   └── response_parser.py # JSON 修復 (移植)
│   ├── tools/                 # ── ツールレジストリ (datafeed/store の薄いラッパー) ──
│   │   ├── registry.py        # 定義 → OpenAI schema / MCP / 素関数 の 3 形態変換
│   │   ├── plugin_loader.py   # plugins/ discover + 承認済みのみ登録
│   │   ├── market_tools.py    # get_ohlcv / get_indicators / get_econ_calendar
│   │   ├── news_tools.py      # search_news
│   │   ├── account_tools.py   # get_positions / get_account
│   │   ├── reflection_tools.py# get_recent_reflections / search_reflections
│   │   └── research_tools.py  # web_search / fetch_article (改善 loop 専用)
│   ├── api/                   # ── 最小 FastAPI (§7: 承認 + 読み取り status のみ) ──
│   └── loops/                 # ── 2 つの agent loop ──
│       ├── trade_loop.py      # 状態サマリ生成 → Mission 実行 → intent 処理
│       ├── improve_loop.py    # 成績レポート生成 → 3 ステップ Mission → 品質ゲート
│       ├── reflection_cycle.py# クローズ後の振り返り生成 (移植)
│       └── prompts/           # Mission プロンプトテンプレート (.md、コード外)
└── tests/                     # src/ とミラー構造
```

- **tools/ と datafeed/ の分離**: ツールは「LLM に見せる薄い口」、実装は datafeed/store 側。pytest から素関数として直接叩ける
- **prompts/ をコード外の .md に**: 改善 loop がプロンプト改善の PR を出すとき、テンプレート差分になりレビューしやすい

## 12. 設定・ストレージ

- 設定: `config/settings.yaml` 1 ファイル (gitignore) + `config/settings.yaml.example` (コミット)。前身の 3 分割はしない
- 秘密情報: `.env` (`DISCORD_WEBHOOK_URL`, `TWELVEDATA_API_KEY`, `AFX_API_KEY` など)

### SQLite スキーマ (`data/agentic.db`、前身 18 テーブル → 11 テーブルに再設計)

| テーブル | 内容 |
|---|---|
| `ohlcv` | 価格データ (symbol, interval, bar_time, OHLCV)。前身と同形 |
| `missions` | 全 Mission 実行記録 (loop 種別, runner, status, output_json, transcript_json) |
| `trade_intents` | LLM の全出力 + risk gate 判定 (accepted / rejected + 却下理由) |
| `orders` | ポジション/指値の単一状態機械: pending → open → closed / cancelled / expired。entry_type, SL/TP, realized_pnl, close_reason |
| `reflections` | トレード振り返り (order_id 主キー。前身から簡素化) |
| `account_snapshots` | 残高・エクイティ推移 (kill switch 判定と成績レポートの根拠) |
| `improvement_backlog` | 改善アイデア (source: user/agent/research, status: open/selected/done/rejected) |
| `improvement_runs` | 改善実施記録 (選択 backlog, PR URL / approval_request ID / レポートパス) |
| `econ_events` | 経済指標カレンダー (前身 econ_event_store 移植) |
| `approval_requests` | 人間承認の一元管理 (§7: kind = tech_plugin / news_source / live_trade) |
| `news_sources` | ニュース取得先リスト (§6: name, fetcher, url, enabled, added_by) |

前身の `trade_plans` に相当するテーブルは**意図的に持たない** (IFD 計画型の廃止に伴う)。旧 plan の役割は 3 テーブルに分担される: LLM の判断内容 = `trade_intents`、「価格が来たら入る」予約 = `orders` の `status=pending` (期限付き指値)、live の承認待ち = `approval_requests` (kind=live_trade)。

### ChromaDB (`data/rag/`、前身 4 コレクション → 2 コレクション)

- `news` — ニュース記事 (48h で掃除)
- `reflections` — トレード振り返り (類似局面の意味検索用。SQLite と二重保存)

insights / econ_analyses は廃止。必要になれば改善 loop 自身が PR でコレクション追加を提案できる。

すべて gitignore (`data/`)。

## 13. ログ設計・エラーハンドリング

### ログの 2 軸分離

前身の反省 (技術ログと行動記録が同一ストリームに混在して煩雑) から、役割で完全分離する。どちらも `.log` テキストファイル + logrotate で管理し、DB には保存しない (肥大化防止):

| 軸 | 内容 | 出力先 | 読者 |
|---|---|---|---|
| **技術ログ** | severity (debug / info / warning / error / critical)。例外・接続失敗・リトライ | `logs/agentic.log` (+ journald) | 開発者・障害調査。CLI の通常出力には一切混ぜない |
| **activity ログ** | カテゴリ別イベント (`news` / `tech` / `trade` / `improve` / `approval` / `system`)。「システムが何をしたか」の構造化 1 行記録 (ts, category, event, summary, ref_id) | `logs/activity.log` | ユーザー。`activity` コマンド (対話シェル / client.py)・`tail -f logs/activity.log`・Discord 通知・`?afx status` はこちらだけを読む |

### エラーハンドリング

- LocalRunner: max_turns / timeout / JSON 修復不能 → MissionResult.status に記録し、その周期は「判断なし」として終了。ログ + Discord 通知。llama-swap TTL デッドロック時も timeout_sec で必ず抜ける
- ClaudeRunner: SDK エラー・レート制限・クレジット枯渇 → 同上 (local への自動フォールバックはしない。挙動を予測可能に保つ)
- 承認ゲート: bot 停止時も main.py 対話シェル / client.py の `approve` で承認可能。live_trade の承認待ちは expires_at で必ず決着する (無限待ちなし)
- 全 MissionResult (transcript 含む) を SQLite `missions` に保存し、後から「なぜこの判断をしたか」を追跡可能にする

## 14. テスト戦略

- TDD (failing test → 実装 → green)
- **FakeRunner**: 定型 MissionResult を返す AgentRunner 実装。loop 制御・risk gate・executor・policy 注入・ask 差し込みを LLM なしで全件テスト
- LocalRunner のループ制御は httpx モック (tool_calls 応答系列) でテスト
- risk gate は全ルール × 境界値のテーブルテスト (limit 乖離・期限上限含む)
- 指値のペーパー約定判定・期限切れ取消・approval expires はクロックモックでテスト
- plugin_loader は「未承認はロードされない」「テスト不合格はロードされない」を含めてテスト
- news_sources の機械検証 (URL 到達性・parse 成功・重複) は fetcher モックでテスト
- activity ログはカテゴリ・イベント形式の書き出しをテスト (技術ログと混ざらないこと)
- CI (GitHub Actions): pytest を PR 必須チェックにする (改善 loop の品質ゲートの土台)

## 15. 段階導入

- **Phase 1**: 決定論的コア + LocalRunner + 取引判断 loop (ペーパー、ハイブリッド発注) + main.py (スプラッシュ + 対話シェル + init 起動ガード + stop) + ログ 2 軸。ツールは get_ohlcv / get_indicators / search_news / get_positions の最小セット (組み込み実装 + news_sources 初期データのみ)
- **Phase 2**: ClaudeRunner (Agent SDK) + 戦略改善 loop (バックログ + Web リサーチ) + tech plugin 機構 + news_sources 承認フロー + `bootstrap` + 操作 API + client.py + Discord 承認 (discord_bot 側 cog 含む) + policy チャネル
- **Phase 3**: MT5 ブリッジ接続 + 資金保護系の本格接続 + live 切替 (`go-live`、人間の明示操作のみ) + live_trade Discord 承認ゲート

## 16. 非スコープ (YAGNI)

- 汎用 REST API サーバー (§7 の操作 API のみ例外として持つ。発注操作・設定変更・go-live・サービス停止のエンドポイントは作らない)
- 対話シェルへのログのストリーミング表示 (旧 client.py の混線の原因。ログ表示は pull 型コマンドと tail で行う)
- 改善の自動採用 (完全自動マージ / 無承認 plugin ロード)
- local ⇔ claude の自動フォールバック / エスカレーション
- IFD 的な条件監視付き予約注文 (前身の構造的限界の原因。指値 + 期限で代替)
- マルチアセット対応 (FX 2 ペアから始める)
