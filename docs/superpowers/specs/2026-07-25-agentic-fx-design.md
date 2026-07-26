# agentic-fx 設計書

- 日付: 2026-07-26 (改訂第 11 版 — codex 4 回目レビュー 2 件反映: learning 切替ガードを実注文・中間状態ゼロ + reconcile 成功に強化 / 市場クローズ移行時に実指値を全取消)
- ステータス: 承認待ち
- 前身: `~/project/finance` (IFD 計画型 FX 自動トレードシステム)

## 1. 背景と目的

前身の finance は「定期実行のニュース分析 + テクニカル分析 → plan (IFD) 生成 → 発注」という固定パイプライン型だった。直近週の成績は勝率 31.8%、PF 0.02、22 件中 20 件が stop_loss 決済であり、計画時点の静的な判断が市場の変化に追随できないという構造的限界が確認された。

agentic-fx はこれを **agent loop 型**に置き換える。LLM agent が「今必要な情報」をツールで自ら収集して取引判断を下し、別の agent が成績を分析してシステム自体を改善していく。情報軸はこれまで通り**ニュースとテクニカルの2軸**を維持する。

**開発方針**: 初期実装は「基本構造 + 決定論的コア + plugin 機構 + 組み込みデフォルト実装」に絞り、拡張 (ニュースソース追加・テクニカル指標追加) は改善ループの LLM に実装させる。システム自体が自己拡張していくことを前提とした最小構造を人間側が TDD で作る。

## 2. 制約 (確定事項)

- LLM 基盤は**ローカル LLM が基本** (llama-swap, OpenAI 互換 API, :8080)。Claude はサブスクリプション認証でのみ利用可。**Anthropic API (従量課金) は使用しない**
- Claude 利用の課金実態 (2026-07 時点): `claude -p` / Agent SDK とも月次 Agent SDK クレジット枠から消費される。**usage credits (追加課金) は有効にしない** — クレジット枯渇時は ClaudeRunner が停止するだけで、従量課金は構造的に発生しない。枯渇時も local への自動フォールバックはしない (挙動を予測可能に保つ)
- 両 loop とも LLM バックエンドは config で `local` / `claude` に切り替え可能。デフォルトは両方 `local`。稼働中の切替 (runner / ローカルモデル) は `model` コマンド (§8、一覧から番号選択式) でも行える
- 戦略改善 loop の変更採用は**必ず人間承認**を通す (トラック別のゲートは §6)
- LLM ができるのは open / close / cancel の**提案** (TradeIntent) まで。**執行・注文数量 (position sizing)・SL/TP 管理・資金保護は決定論的コードが握る**。資金保護クローズ (SL/TP・day 期限) は決定論的トリガーのみ (kill switch は新規停止であってクローズはしない — §5 Risk Gate)。裁量クローズと未約定指値の取消は LLM 提案を決定論的検証の上で実行する (エクスポージャー縮小方向のため)
- **取引モード (実資金) の新規発注は、初期は Discord/CLI の人間承認を最終ゲートとする**。成績実績を確認した上で、人間の明示操作 (`autopilot on`) でのみ自動発注へ移行できる — 最終目標は自動発注。切替は config 編集では不可 (明示コマンドのみ)、kill switch 等の risk gate は自動発注時も常時有効
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

原則: **LLM が自走するのは「判断」と「改善提案」まで**。金を動かす経路 (発注・クローズ・SL/TP 変更) と資金保護は常に決定論的コアが握り、取引モードの新規発注は初期は人間承認をゲートとする (自動発注への移行は人間の明示操作のみ、§5)。

### 稼働モード (前身の paper / shadow / live_test を 2 モードに集約)

前身はモード間の違いが分かりにくかったため、役割の異なる 2 モードに集約する:

| モード | 内容 |
|---|---|
| **学習モード** (learning) | ペーパー取引 + 情報蓄積 (ニュース RAG・reflection・成績データ)。初期状態。risk gate 通過後は自動でペーパー発注 |
| **取引モード** (trading) | 実資金運用 (Phase 3)。発注方式を **手動承認** (Discord/CLI 承認が最終ゲート) ⇔ **自動発注** で切替可能。初期は手動承認、成績実績を確認してから自動へ |

- モード切替は `main.py mode` (サービス停止中の人間の明示操作、§8)。config 編集では切替できない
- 現在モード・発注方式は settings.yaml ではなく `data/state/` に保存する (config をいじっても実資金運用にならない構造的担保)
- **モード遷移ガード** (mode コマンドが決定論的に検査): trading への切替は MT5 接続・口座 ID 確認 + 未約定ペーパー指値の整理 + 未決 approval の全失効を通過してから。**learning への切替は、実ポジション・実口座の全 working order (未約定指値含む)・未解決の中間状態 (`submitting` / `protection_pending` / `closing` / `*_unknown`) がすべてゼロで、MT5 との最終 reconcile が成功した場合のみ許可** (実注文を監視外に残さないため)。切替は必ず監査ログ (activity SYSTEM) に記録する

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
- JSON 崩れは修復ロジック (前身の `response_parser` を移植: `<think>` 除去、コードフェンス剥がし) を通し、修復不能ならそのターンをリトライ (**上限 2 回。リトライも max_turns に算入し、timeout_sec が常に優先**。使い切ったら status=failed)
- スキーマ検証失敗時はエラー内容を LLM に返して再出力させる (上限 2 回、同上)

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
- **市場クローズ中は処理を極力停止する** (前身と同様): 取引判断 loop・価格取得・指値約定監視は止め、**ニュース収集のみ継続**する (econ カレンダー更新もクローズ中に行ってよい)
- **例外 (取引モードの実注文)**: クローズ中に実指値を監視外に残さないため、**市場クローズ移行時に取引モードの未約定実指値をすべて決定論的に取消す** (指値期限の上限が 24h であることとも整合)。取消と約定が競合した場合は約定済みとして扱い、即座に保護確認 (`protection_pending`) へ進む。broker との reconcile はクローズ中も継続する (頻度は下げてよい)
- エラー時はスキップして次周期 (リトライしない)。同時実行は常に 1 (agent 実行のグローバル排他)
- **ワンショットのユーザープロンプト**: main.py 対話シェルまたは client.py の `ask "..."` で**臨時 Mission を即時実行**する (実行は常にサービスプロセス内の排他スロット)。プロンプトはその 1 回だけ Mission に注入され (ワンショット)、結果は呼び出し元と Discord に返す。定期周期には影響しない。永続的な方針にしたい場合は `policy add` を使う
- **ask Mission は回答・分析専用**: 出力スキーマは回答テキストのみで **TradeIntent を受理しない** (「買え」と指示しても発注経路には乗らない)。TradeIntent を生成できるのは **scheduler 起動の定期 Mission だけ**とし、executor は intent の起動元 (origin) を検証して定期 Mission 由来以外を拒否する — これがないと `POST /ask` が事実上の発注 API になり §7 の一線 (発注操作を API に載せない) が破れるため。取引判断に反映したい情報は `policy add` (永続) か次周期の定期 Mission を待つ

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

### データ取得元

新規ユーザーが clone + init 直後に、口座・API キーなしで動かせることを優先し、**デフォルトはすべて yfinance** (キー不要) とする。MT5 / Twelve Data は**追加設定** (settings.yaml) で有効化する:

| 構成 | 取引ペアの OHLCV・現在価格 | 関連指標 (指数・金・原油・金利など) |
|---|---|---|
| デフォルト (追加設定なし) | yfinance | yfinance |
| + MT5 ブリッジ設定 | MT5 (リアルタイム) | MT5 に銘柄 (CFD) があるものは MT5 (OANDA MT5 は主要指数・XAUUSD・WTI 等を提供) |
| + Twelve Data キー設定 | — | MT5 にないもの (DXY・米10年債利回り等) を TD で補完 |

- 優先順位は **MT5 → Twelve Data → yfinance**。設定済みソースの取得失敗時は下位へフォールバックし、yfinance を最終フォールバックとする
- **フォールバック採用条件は「取得成功」ではなくデータ健全性検証の通過**: timestamp の鮮度 (許容遅延は config)・バーの連続性・異常値 (スパイク/ゼロ/NaN) を検証する。**全ソース不健全なら取引判断 Mission を実行せず fail closed** (「データ不健全」として記録・通知)。yfinance も非公式 API ゆえ恒久的に使える保証はない前提で扱い、yfinance のみで蓄積した判断・成績にはデータ品質フラグを付けて保存する
- yfinance はタイムラグ (数分〜15 分程度) と非公式 API ゆえの仕様変更リスクがある。学習モードの判断・ペーパー約定判定には許容範囲とし、**PriceProvider 抽象でシンボル毎にソース解決**して差し替え可能にしておく。キー不要の代替は実質 Stooq のみ (日足中心で intraday が弱く、常用には不適 — 障害時の臨時フォールバック候補に留める)
- MT5 の価格取得 (読み取り専用) は資金リスクがないため、設定すれば **Phase 1 から利用できる** (発注系の接続は Phase 3)。ブリッジ死活は bridge_health で監視
- **取引モード (Phase 3) は MT5 接続が前提**。さらに、**発注前検証 (Risk Gate)・position sizing・未約定指値の管理には、執行口座と同一の MT5 接続から取得した quote・銘柄仕様のみを使用する** — 他ソースへのフォールバック不可 (執行市場と異なる価格で検証を通過して実発注する経路を塞ぐ)。MT5 quote が不健全・陳腐・取得不能な場合、分析用データは他ソースにフォールバックしてよいが、**新規実発注は fail closed**
- 経済指標カレンダー: 前身 econ_event_store の移植

### 出力: TradeIntent v2

```json
{
  "action": "open | close | cancel | hold",
  "order_id": 123,
  "pair": "USDJPY",
  "direction": "long | short",
  "entry_type": "market | limit",
  "horizon": "day | swing",
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
- **トレード時間軸 (`horizon`) は agent が判断する** (open 時必須)。`day` = 当日決済想定 / `swing` = 数日保有想定。orders に保存し、状態サマリ・成績集計・reflection で horizon 別に扱う。決定論的な扱いの差: `day` は日次ロールオーバー (NY 17:00 クローズ) 前に scheduler が強制クローズ (FX は 24 時間市場のため「当日」の境界をロールオーバーで定義する)、`swing` は持ち越し可。**週末ギャップは SL では防げない** (ギャップで指定価格より不利に約定し得る)。1 取引リスク上限は**通常約定時の想定リスクであり、ギャップ時の損失上限を保証しない**ことを明記する。決定論的なギャップリスク規則: **`horizon=swing` は建てた時点から 1 取引リスク率を半減 (config) して sizing** する — swing は定義上週末持ち越しの可能性が常にあるため、発注時に確定できるルールにする (事後の部分クローズはしない)。金曜終盤の新規 swing 建ては制限する (時間帯は config)。SL 幅・TP の妥当性判断は horizon を踏まえて agent が行う
- `stop_loss` は open 時必須。SL/TP は発注時にブローカー (ペーパー / MT5) 側に添付して管理する
- `close` / `cancel` は対象を `order_id` で指定する (状態サマリに ID を含めて提示する)
- `cancel` は未約定指値の取消 (資金リスクゼロのため LLM に許可)。**約定済みポジションの SL/TP 変更は LLM 不可** (決定論的な資金保護のみが変更できる)
- 次周期の agent は状態サマリで未約定指値を見て、取消/維持も判断できる
- **TradeIntent は注文数量を持たない** — 数量は LLM に決めさせず、決定論的 position sizing (下記) が算出する

### Position Sizing (決定論的、LLM の外)

注文数量は決定論的に算出する: `数量 = 口座エクイティ × 1 取引リスク率 (初期 0.5%、`horizon=swing` は半減) ÷ SL 距離` を pip value・口座通貨換算した上で、broker の lot step に**切下げのみで丸める** (リスクを超えない方向。四捨五入・切上げ禁止)。リスク額には spread・想定 slippage・手数料を含める (config)。丸め後の数量で損失額を再計算し、上限以下であることを最終検証する。**算出量が最小 lot 未満なら発注拒否**。算出に必要な値 (価格・エクイティ・pip value) が欠けている場合は **fail closed** (発注しない)。SL があっても数量が未定義では過大損失が可能になるため、サイジング自体を資金保護の一部としてコアに置く。

### Risk Gate (決定論的、LLM の外)

TradeIntent は発注前に全ルールを通過しなければならない。1 つでも違反すれば却下し、却下理由を記録・通知する:

| ルール | 初期値 |
|---|---|
| 最小リスク報酬比 (RR) | 1.5 (spread 込みで計算) |
| **SL/TP 妥当性検証** | 有限数・正値であること。方向整合 (long: `SL < entry < TP` / short: `TP < entry < SL`)。SL 距離の最小/最大 (pair 毎に config)。違反は無条件却下 |
| 1 取引リスク上限 | エクイティの 0.5% (position sizing で強制) |
| 最大総エクスポージャー | 全ポジション + **未約定指値** (全量約定時のリスク・必要証拠金を**予約額として算入**) の合算に上限 (レバレッジ上限含む、config) |
| drawdown kill switch | エクイティの高値 (high-water mark) から **-2%** (unrealized 込み) で新規停止 (常時有効、config で無効化不可) |
| 最大同時ポジション | 2 (未約定指値も枠にカウント) |
| 日次損失上限 | 日初エクイティ比 **-1%** で当日新規停止 (日境界は NY 17:00 ロールオーバー、入出金は調整) |
| SL 必須 | SL なしの open は無条件却下 |
| limit 乖離上限 | limit_price が現値から 0.5% 超乖離なら却下 (config 可) |
| 指値期限上限 | expires_in > 24h は却下 |

- `market` の entry 価格は判断時点でなく**発注直前の最新 bid/ask で再計算**する (取引モードでは MT5 quote 限定 — データ取得元参照)。RR・SL 検証も再計算後の値で行い、判断時点から許容スリッページ (config) を超えて動いていたら失効させる
- **未約定指値のリスク予約の維持**: 指値は非同期に約定するため、毎分監視で口座状態の変動 (他ポジションの損失・エクイティ減少) により総エクスポージャー上限・証拠金を維持できなくなった指値を**約定前に決定論的に自動取消**する。取消結果不明は `cancel_unknown` → 新規発注停止 (§12) に接続
- **kill switch の HWM 定義**: HWM は**入出金調整済みエクイティ** (unrealized 込み) で計算する (入金は HWM に加算、出金は減算 — 入金が利益扱い、出金がドローダウン扱いになるのを防ぐ)。snapshot 更新は原子的に行い、再起動時は保存済み HWM を復元する。**発火はラッチ** (条件が戻っても自動解除しない): 解除は人間の明示操作 (対話シェルの専用コマンド) のみ。kill switch の発火動作は**新規停止のみ**で、既存ポジションはクローズせず SL/TP 監視を継続する
- 日次損失の日初エクイティも入出金調整を同様に適用する
- kill switch・日次損失の判定に使う account_snapshots が欠損・陳腐化している場合は **fail closed** (新規停止)

### Executor と取引モードの承認ゲート

- Phase 1 は学習モード (ペーパー取引) のみ (前身の PositionManager 簡素版 + SQLite `orders` 状態機械)。学習モードでは risk gate 通過後に自動でペーパー発注する
- **ペーパー約定規則**: 現在値のスナップショットではなく **1 分足の high/low で到達判定**する (ポーリング間の水準通過を見逃さないため)。同一バーで SL と TP の両方に到達した場合は**保守的に SL 約定**とする。指値エントリーと SL/TP が同一バー内で成立し得る場合など、OHLC から順序を判定できないときも**最悪結果 (SL 約定) を採用**する。約定価格は spread を考慮し、ギャップ時の SL はギャップ後の価格で約定させる (実勢の滑りを再現し、成績評価の歪みを防ぐ)
- MT5 の発注系接続は Phase 3。取引モードへの切替は config ではなく **`main.py mode trading` (人間の明示操作)** でのみ行う
- **取引モードの手動承認ゲート (Phase 3、初期状態)**: risk gate を通過した open intent は `approval_requests` (kind=live_trade) に登録され、**人間の承認 (Discord ボタン or CLI) が下りるまで発注しない**。expires_at (指値は指値期限、market は 15 分) を過ぎると自動 expired となり発注しない。close / cancel は資金保護方向の操作なので承認不要で即時実行する
- **承認時の再検証 (TOCTOU 対策)**: 承認までの間に価格・残高・kill switch 状態が変わり得るため、承認 POST は単なる status 更新ではなく、サービス内の排他区間で ①pending・未失効の compare-and-set ②mode / autopilot / kill switch / 口座・ポジション状態の再取得 ③**最新 bid/ask で Risk Gate 全ルール + position sizing を再実行** — を行い、再検証に失敗した場合は `invalidated` として理由を記録する (approved にしない)
- **broker 送信は DB トランザクションと原子化できない**前提で 2 段階に分ける: 再検証合格後、①**送信前に client_order_id (冪等キー) を永続化** (`submitting`) ②broker へ 1 回だけ送信。応答タイムアウト・結果不明・DB 保存失敗は**再送せず `submit_unknown`** とし、注文履歴・建玉の照合 (reconcile) で解決する。reconcile は再起動時に加えて**稼働中も定期実行** (毎分) し、**未解決の submit_unknown がある間は同一 intent の再送と新規発注を停止**する
- **保護注文の確認 (SL/TP 添付失敗対策)**: broker が注文本体を受理しても SL/TP 設定だけ失敗・不明になり得る (MT5 は注文と建玉が別 ID)。約定後は注文・建玉・SL/TP を照合し、**保護の確認が取れるまで `open` にしない** (`protection_pending`)。SL 設定の失敗・欠落は即時再設定をリトライし、リトライ失敗時は**決定論的に緊急クローズ + 通知** — SL なしの実ポジションを存在させない
- **自動発注への段階移行 (最終目標)**: 手動承認での成績実績を確認した上で、対話シェルの `autopilot on` (確認プロンプト付き、**API には載せない**) で承認ゲートを外し、risk gate 通過後に自動発注する。`autopilot off` でいつでも手動承認に戻せる。切替は activity ログに記録し Discord に必ず通知する。**config 編集では切替できない** (状態は `data/state/` 管理、§3)。自動発注時も risk gate・kill switch・日次損失上限は全件通過必須

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
- **ユーザーによる追加コマンド**: `news add <url> [--name]` (§8)。fetcher (feed/web) を自動判定し、機械検証 (URL 到達性・parse 成功・重複) を実行して登録する。`added_by=user` は自分の意思による追加のため**人間承認は不要** (機械検証のみで enabled)。agent 追加は従来通り approval_requests を通す

### tech plugin 機構

- 配置: **1 plugin = 1 フォルダ** `plugins/tech/<name>/`。構成は 3 ファイル:
  - `indicator.py` — 実装 (純関数)
  - `config.yaml` — テクニカル分析パラメータの既定値 (期間・閾値等)。**ユーザーがコードを触らずにパラメータ調整できる**ようにするため
  - `test_indicator.py` — テスト同梱必須
- インターフェースは意図的に極小、かつ**純関数に限定** (I/O・外部アクセス禁止): `Indicator.compute(df, params) -> dict`。`params` は plugin_loader が `config.yaml` を読み込んで渡す。qwen3.6 クラスの実装力でも品質が安定し、テストが決定論的になる粒度にする
- `tools/plugin_loader.py` が起動時にフォルダを discover し、**pytest 合格 + 承認済み** (approval_requests で approved) のもののみレジストリに登録。承認は indicator.py + config.yaml の**内容ハッシュに対して**行う
- config.yaml の再承認免除は**人間がローカルで明示的に編集した場合のみ**。**agent による config.yaml 変更はコード変更と同様に approval 対象**とし、戦略採用ゲート (下記) を通す — パラメータ変更は戦略結果を直接変えるため、承認迂回経路にしない
- **サンドボックス実行**: 「純関数・I/O 禁止」は規約だけでは強制できない (import 時の任意コード実行を pytest では防げない) ため、plugin は**サービスプロセスに直接 import せずサブプロセスで実行**し、入力 (OHLCV DataFrame) と出力 (JSON) だけを IPC で渡す。ロード時に **AST 検査 + import allowlist** (numpy / pandas / math 等の計算系のみ) で禁止 import を拒否する。サブプロセスには**最小限の環境変数のみ渡し (秘密情報・broker 資格情報は渡さない)**、作業ディレクトリを限定し、CPU 時間・メモリ・プロセス数を resource limit で制限、タイムアウト・出力サイズ制限を課す。これらは**到達を最小化する多層防御であり完全な隔離の保証ではない** — だからこそ plugin の採用には人間承認を必須とする
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

### 品質ゲート (loop 側で強制) — コード品質と戦略品質を分離

**コード品質ゲート**:
- テストが 1 件でも落ちる変更は PR / approval_request にしない。分析レポート (`reports/improve-YYYY-MM-DD.md`) だけ残す
- main への直 push は不可 (branch protection + ツール実装で二重に防ぐ)
- **採用は必ず人間承認** (コア改善 = PR、plugin = approval_requests)

**戦略採用ゲート** (risk gate パラメータ・戦略に影響する提案が対象。pytest 合格だけでは戦略の良し悪しは判定できないため):
- バックテスト必須 (手数料・spread 込み)。**最低取引数 (初期 30) 未満の標本による変更提案は不可** — 「観察のみ」としてバックログに残す
- 評価は out-of-sample (期間分割) で行い、既存構成 (baseline) との比較値を approval_request / PR に添付する
- 週次/日次で変更を繰り返す性質上、少数トレードへの過学習を防ぐことを人間レビューの観点として明記する

## 7. 承認ゲートと操作 REST API

### approval_requests (SQLite)

人間承認が必要な事象を一元管理する汎用テーブル:

```
id, kind (tech_plugin | news_source | live_trade), payload_json,
status (pending | approved | rejected | expired | invalidated),
reason, decided_by, decided_at, message_id, expires_at, created_at
```

- `kind=tech_plugin`: 改善ループが plugin を書いたら発行。payload は plugin パス・要約・テスト結果。承認されるまでロードされない
- `kind=news_source`: ニュースソース追加 (**agent 追加時のみ発行**。user 追加は機械検証成功後に直接 enabled — §6)。payload はソース情報 + 機械検証結果 (URL 到達性・parse 成功・取得サンプル)。承認されるまで `news_sources.enabled` にならない
- `kind=live_trade` (Phase 3): 取引モード (手動承認時) の open intent。payload は TradeIntent 全体。expires_at 超過で自動 expired。autopilot on の間は発行されない (§5)
- `status=invalidated`: live_trade の承認時再検証 (§5) に失敗した場合の終端状態 (理由を reason に記録)
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
| `POST /ask` | 臨時 Mission の実行依頼 (**回答専用スキーマ — TradeIntent は生成不可、§5**) | client.py |
| `POST /policy` | 方針書への追記 | client.py |
| `POST /backlog` | 改善アイデアの投入 | client.py |
| `POST /improve` | 改善 loop の即時手動起動 (実行は常にサービスプロセス内) | client.py |
| `POST /news` | news ソース追加 (fetcher 自動判定 + 機械検証、§6) | client.py |
| `GET /models` | 利用可能モデル一覧 (llama-swap `/v1/models` + claude、番号付き) | client.py |
| `POST /model` | LLM runner / ローカルモデルの切替 (client 側で番号→モデル名を解決して送る。資金と無関係のため API 可) | client.py |

- `X-API-Key` 認証 (finance 方式踏襲)。ただし**キーは 2 段に分離**する: **operator キー** (閲覧・ask・policy・backlog・news・model・tech_plugin / news_source の承認) と **approver キー** (live_trade の承認/却下のみ)。単一キーの漏洩で実発注の承認まで通ることを防ぐ。`decided_by` はリクエスト本文でなく**認証主体から生成**する (本文の自己申告を信用しない)
- API の bind は**デフォルト localhost のみ**。外部公開する場合は reverse proxy + TLS を前提とする (ドキュメントに明記)
- **autopilot 中の追加制限**: trading + autopilot on の間は **API の変更系操作を全面拒否**する — 許可するのは読み取り系 (`GET *`)・`reject` (安全方向の却下)・`ask` (回答専用) のみ。`POST /policy` / `/model` / `/news` / `/improve` / `/backlog` / approve は**ホスト上の対話シェルのみ**。operator キー漏洩時に判断入力・戦略構成の変更 (方針・モデル・ニュースソースの RAG 混入・plugin 承認) で次周期から人間承認なしの実発注を誘導する経路を、個別列挙でなく原則として閉じるため
- **載せない一線**: 発注操作・risk gate 等の資金関連設定の変更・モード切替 (`mode`)・自動発注切替 (`autopilot`)・サービス停止。金を動かす経路と重大操作はホスト上の明示操作のみ
- Mission 実行 (`/ask`) も含め、**Mission を実行するのは常にサービスプロセスだけ**。排他制御はプロセス内の Mission スロットで完結する (クロスプロセスロックは不要)

## 8. エントリポイントと操作体系 (main モデル)

エントリは **main.py (サービス本体)** と **client.py (稼働中サービスの操作クライアント)** の 2 つ。前身 finance と同じ運用感を踏襲しつつ、旧 client.py の問題 (ログのストリーミング表示が入力に割り込む) を「**ログ表示の pull 型コマンド化**」で解消する。

### main.py — 対話シェル付きサービス

```
uv run main.py                        # サービス起動 + スプラッシュ + コマンド受付 (TTY のとき)
uv run main.py --daemon               # systemd 用: コマンド受付なし (非 TTY 時は自動でこちら)
uv run main.py init                   # 設定ウィザード (唯一のウィザード。サービスは起動しない)
uv run main.py mode learning|trading  # 稼働モード切替 (Phase 3、サービス停止中の人間の明示操作。API には載せない)
```

- 起動時**スプラッシュ**: mode (学習/取引)・発注方式 (手動/自動)・対象ペア・runner/モデル・risk gate 現行値・承認待ち件数・未約定指値・直近成績サマリを 1 画面表示。**項目構成は運用しながら調整する** (初期実装は最小でよい)
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
| `activity [n] [カテゴリ]` | **activity ログ**の直近 n 行 (カテゴリ絞り込み可、§13) |
| `ask "..."` | 臨時 Mission (実行は常にサービスプロセス内の排他スロット) |
| `approve <id>` / `reject <id> [理由]` | 承認操作 |
| `policy add "..."` | 方針書へ追記 |
| `improve` | 改善 loop の即時手動起動 (実行は常にサービスプロセス内の排他スロット) |
| `improve add "..."` / `backlog` | 改善アイデア投入 / バックログ一覧 |
| `news add <url> [--name]` / `news list` | ニュース取得対象の追加 (機械検証つき、§6) / 一覧 |
| `model` | 利用可能モデルの一覧表示 (llama-swap `/v1/models` から動的取得 + claude)。**各モデルに番号を割り振り**、両 loop の現在選択に印を付けて表示 |
| `model <trade\|improve> <番号>` | 一覧の**番号で切替** (モデル名の手入力は不要。番号→モデル名の解決は表示時の一覧で行い、内部にはモデル名で保存する) |
| `autopilot on\|off` | 取引モードの自動発注切替 (Phase 3、確認プロンプト付き。**対話シェルのみ**、API には載せない) |
| `stop` | graceful shutdown (**main.py 対話シェルのみ**。API には載せない) |

### client.py — 稼働中サービスの操作クライアント

- 操作 API への薄い HTTP クライアント (`X-API-Key`)。`uv run client.py status` のようにワンショット実行し、即終了する
- **--daemon 運用中の手元操作と、cron・シェルスクリプトからの連携**はこちらが担う (finance で実証済みのパターン)
- サービス停止中は使えない。停止中に必要な操作は main.py サブコマンド (init / mode) と、ログファイルの直接閲覧 (`tail logs/activity.log` — txt ベースなのでそのまま読める) でカバーする

### 初期起動フロー (ウィザードは init だけ)

1. **`uv run main.py init`** — 設定ウィザード (settings.yaml 生成、DB 初期化、稼働モードを学習で初期化、llama-swap / Discord / 価格ソースの接続確認)。価格ソースはデフォルト yfinance で、MT5 ブリッジ / Twelve Data は任意の追加設定 (未設定ならスキップ、§5)。完了までサービス起動を拒否。systemd unit 化はユーザーが明示的に行う (ドキュメントのみ提供)
2. **`uv run main.py`** — サービス開始。組み込みデフォルト実装 (基本指標 + 基本ニュースソース) でそのまま稼働できる

初期構築 (ニュースソース拡充・指標追加) は専用ウィザードを持たず、**通常チャネルで行う**: `news add` で取得対象を足す、`improve add "..."` でアイデアを投入して `improve` を手動起動する (runner は config / `model` コマンドで claude / local を選択、ClaudeRunner はサブスク認証で利用可能)。成果は通常の承認フローに乗る。

### ユーザー入力チャネル

| チャネル | 用途 | 消化タイミング |
|---|---|---|
| `policy add "..."` → `policy/directives.md` | 永続的な方針 (例: 「USDJPY は当面見送り」) | 取引/改善の全 Mission に毎回**末尾 4000 文字**を注入 (§5 と同一) |
| `ask "..."` | ワンショットの質問・指示 | 臨時 Mission を即時実行、その 1 回だけ注入 |
| `improve add "..."` | 改善アイデアの投入 | 次回の改善 loop がバックログから選択 (`improve` で即時起動も可) |
| `news add <url>` | ニュース取得対象の追加 | 機械検証後に即 enabled (user 追加は承認不要、§6) |
| Discord ボタン / `approve` | tech plugin・news ソース (agent 追加)・実発注の承認 | approval_requests の決定 |

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
├── main.py                    # サービス本体エントリ (対話シェル / --daemon / init / mode, §8)
├── client.py                  # 操作クライアントエントリ (操作 API 経由のワンショット実行, §8)
├── config/
│   ├── settings.yaml.example  # コミット (新規キーは必ず両方同期)
│   └── settings.yaml          # gitignore
├── policy/
│   └── directives.md          # ユーザー方針 (タイムスタンプ付き追記)
├── plugins/                   # gitignore — LLM 生成、ユーザーごとに異なる
│   └── tech/<name>/           #   1 plugin = 1 フォルダ: indicator.py (純関数) + config.yaml (パラメータ) + test_indicator.py (§6)
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
│   │   ├── scheduler.py       # 毎時起動・排他スロット・指値期限切れ取消・day 強制クローズ・クローズ中はニュース収集のみ
│   │   ├── risk_gate.py       # 全ルール (テーブルテスト対象)
│   │   ├── executor.py        # broker 抽象 + 発注経路 + 取引モードの承認ゲート/autopilot 分岐
│   │   ├── paper_broker.py    # ペーパー約定 (指値判定含む)
│   │   ├── market_hours.py    # (移植)
│   │   └── notifier.py        # Discord webhook (移植)
│   ├── store/                 # ── ストレージ層 ──
│   │   ├── db.py              # SQLite 接続 + 11 テーブルスキーマ
│   │   ├── orders.py / intents.py / missions.py / reflections.py / snapshots.py  # orders / trade_intents / missions / reflections / account_snapshots
│   │   ├── backlog.py / improve_runs.py / econ_events.py / approvals.py / news_sources.py  # improvement_backlog / improvement_runs / econ_events / approval_requests / news_sources
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
│   ├── api/                   # ── 操作 API (FastAPI, §7。発注・資金設定・mode/autopilot は載せない) ──
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
- 秘密情報: `.env` (`DISCORD_WEBHOOK_URL`, `TWELVEDATA_API_KEY`, `AFX_OPERATOR_KEY`, `AFX_APPROVER_KEY` など — API キー 2 段分離は §7)
- **稼働モード・発注方式 (autopilot) は settings.yaml に置かず `data/state/` に保存** (§3 — config 編集では実資金運用・自動発注に切り替わらない構造的担保)

### SQLite スキーマ (`data/agentic.db`、前身 18 テーブル → 11 テーブルに再設計)

| テーブル | 内容 |
|---|---|
| `ohlcv` | 価格データ (symbol, interval, bar_time, OHLCV)。前身と同形 |
| `missions` | 全 Mission 実行記録 (loop 種別, runner, status, output_json, transcript_json) |
| `trade_intents` | LLM の全出力 + risk gate 判定 (accepted / rejected + 却下理由) |
| `orders` | ポジション/指値の状態機械 (下記)。主要カラム: intent_id (FK), approval_id (FK, 取引モード手動承認時), **client_order_id** (送信前に永続化する冪等キー), entry_type, horizon (day/swing), **quantity / filled_quantity / remaining_quantity / avg_fill_price** (部分約定対応), SL/TP, requested_price / close_price, fees_swap, **broker_order_id / broker_position_id** (MT5 では注文と建玉が別 ID になり得る), broker_synced_at (reconcile 最終照合時刻), realized_pnl, close_reason, created_at / updated_at / filled_at / closed_at |
| `reflections` | トレード振り返り (order_id 主キー。前身から簡素化) |
| `account_snapshots` | 残高・エクイティ推移 (kill switch 判定と成績レポートの根拠) |
| `improvement_backlog` | 改善アイデア (source: user/agent/research, status: open/selected/done/rejected) |
| `improvement_runs` | 改善実施記録 (選択 backlog, PR URL / approval_request ID / レポートパス) |
| `econ_events` | 経済指標カレンダー (前身 econ_event_store 移植) |
| `approval_requests` | 人間承認の一元管理 (§7: kind = tech_plugin / news_source / live_trade) |
| `news_sources` | ニュース取得先リスト (§6: name, fetcher, url, enabled, added_by) |

**orders の状態遷移** (approval のライフサイクルと broker のライフサイクルを混同しない):

```
approval_pending → submitting → submitted ─┬ (market) ──────────→ protection_pending → open
       │               │                   └ (指値) pending_fill ──┘   │
       │               │                        ├→ expired              └→ (保護確認失敗) 緊急クローズ → closing
       │               │                        └→ cancelling → cancelled / cancel_unknown
       │               ├→ rejected (broker 拒否)
       │               └→ submit_unknown (送信結果不明)
       └→ rejected / expired / invalidated

open → closing → closed
         └→ close_unknown (クローズ結果不明 — closed 扱いにしない)
```

- 学習モード・autopilot では approval_pending をスキップして submitting から始まる
- **`protection_pending`**: 約定後、注文・建玉・SL/TP の照合確認が取れるまで `open` にしない (§5)。SL 再設定リトライ失敗は決定論的に緊急クローズ + 通知
- **部分約定**: filled_quantity / remaining_quantity を保存。残数量は取消し、約定分は保護確認の上 open として扱う
- **クローズ・取消の失敗/結果不明** (`close_unknown` / `cancel_unknown`) はシステム上 closed / cancelled にせず、再試行 + reconcile + 通知で解決するまで保持する
- 各遷移の主体は決定論的コア。broker 送信は client_order_id (冪等キー) を**送信前に永続化**した上で 1 回だけ。**reconcile は再起動時 + 稼働中の定期実行 (毎分)** で broker 照会し、**未解決の `*_unknown` がある間は新規発注を停止**する
- 「価格が来たら入る」予約 = `pending_fill` (期限付き指値)。旧 `trade_plans` に相当するテーブルは**意図的に持たない** (IFD 計画型の廃止に伴う): LLM の判断内容 = `trade_intents`、予約 = `orders.pending_fill`、実発注の承認待ち = `approval_requests` (kind=live_trade)

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
| **activity ログ** | カテゴリ別イベント (下表)。「システムが何をしたか」の構造化 1 行記録 (ts, category, event, summary, ref_id) | `logs/activity.log` | ユーザー。`activity` コマンド (対話シェル / client.py)・`tail -f logs/activity.log`・Discord 通知・`?afx status` はこちらだけを読む |

activity のカテゴリ (処理の流れ「収集 → 分析 → 統合判断 → 執行」に対応させる。運用しながら精査・追加してよい):

| カテゴリ | 内容 |
|---|---|
| `NEWS` | ニュース収集・要約・RAG 格納 |
| `TECH` | テクニカル指標の計算・tech plugin 実行 |
| `AGGREGATE` | 取引判断 loop の統合判断 (毎時の判断結果・hold 理由・ask の回答) |
| `TRADE` | 発注・約定・クローズ・期限切れ取消・risk gate 却下 |
| `IMPROVE` | 改善 loop の活動 (発見・リサーチ・実装・PR/approval 発行) |
| `APPROVAL` | 承認要求の発行・承認/却下/期限切れ |
| `SYSTEM` | 起動・停止・モード切替・autopilot 切替・runner/モデル切替 |

### エラーハンドリング

- LocalRunner: max_turns / timeout / JSON 修復不能 → MissionResult.status に記録し、その周期は「判断なし」として終了。ログ + Discord 通知。llama-swap TTL デッドロック時も timeout_sec で必ず抜ける
- ClaudeRunner: SDK エラー・レート制限・クレジット枯渇 → 同上 (local への自動フォールバックはしない。挙動を予測可能に保つ)
- 承認ゲート: bot 停止時も main.py 対話シェル / client.py の `approve` で承認可能。live_trade の承認待ちは expires_at で必ず決着する (無限待ちなし)
- 全 MissionResult (transcript 含む) を SQLite `missions` に保存し、後から「なぜこの判断をしたか」を追跡可能にする

## 14. テスト戦略

- TDD (failing test → 実装 → green)
- **FakeRunner**: 定型 MissionResult を返す AgentRunner 実装。loop 制御・risk gate・executor・policy 注入・ask 差し込みを LLM なしで全件テスト
- LocalRunner のループ制御は httpx モック (tool_calls 応答系列) でテスト
- risk gate は全ルール × 境界値のテーブルテスト (limit 乖離・期限上限・SL/TP 方向整合・NaN/Infinity 拒否含む)
- position sizing は境界値 (最小/最大 lot・step 丸め・算出不能時の fail closed) をテーブルテスト
- **ask Mission の出力から intent が執行されないこと** (origin 検証) をテスト
- live_trade の**承認時再検証** (価格変動で invalidated になるケース・二重承認拒否) をテスト
- orders 状態機械の異常系をテスト: `submit_unknown` / `cancel_unknown` / `close_unknown` の reconcile 解決、未解決中の新規発注停止、`protection_pending` で SL 設定失敗 → 緊急クローズ、部分約定の残数量取消
- kill switch の HWM 入出金調整・発火ラッチ (自動解除しないこと)・人間の明示リセットをテスト
- 未約定指値のリスク予約 (エクスポージャー算入・維持不能時の自動取消) をテスト
- 取引モードで MT5 quote 不健全時に新規実発注が fail closed になること、autopilot 中の変更系 API 拒否をテスト
- モード遷移ガード (中間状態・working order 残存時の learning 切替拒否) と市場クローズ移行時の実指値全取消 (取消と約定の競合含む) をテスト
- データ健全性検証 (鮮度・連続性・異常値) と全ソース不健全時の fail closed をテスト
- 指値のペーパー約定判定・期限切れ取消・approval expires・day ポジションの強制クローズ・市場クローズ中の処理停止 (ニュース収集のみ継続) はクロックモックでテスト
- 稼働モード・autopilot の状態遷移は「config 編集では切り替わらない」「明示コマンドのみ」を含めてテスト
- plugin_loader は「未承認はロードされない」「テスト不合格はロードされない」を含めてテスト
- news_sources の機械検証 (URL 到達性・parse 成功・重複) は fetcher モックでテスト
- activity ログはカテゴリ・イベント形式の書き出しをテスト (技術ログと混ざらないこと)
- CI (GitHub Actions): pytest を PR 必須チェックにする (改善 loop の品質ゲートの土台)

## 15. 段階導入

- **Phase 1**: 決定論的コア + LocalRunner + 取引判断 loop (学習モード = ペーパー、ハイブリッド発注、horizon) + 価格取得 (デフォルト yfinance、設定時は MT5 / TD を優先) + main.py (スプラッシュ + 対話シェル + init 起動ガード + stop) + ログ 2 軸。ツールは get_ohlcv / get_indicators / search_news / get_positions の最小セット (組み込み実装 + news_sources 初期データのみ)
- **Phase 2**: ClaudeRunner (Agent SDK) + 戦略改善 loop (バックログ + Web リサーチ) + tech plugin 機構 + news_sources 承認フロー + 操作 API + client.py + `news add` / `model` コマンド + Discord 承認 (discord_bot 側 cog 含む) + policy チャネル
- **Phase 3**: MT5 の発注系接続 + 資金保護系の本格接続 + 取引モード切替 (`mode trading`、人間の明示操作のみ) + 手動承認ゲート (live_trade) + `autopilot` (自動発注への段階移行)

## 16. 非スコープ (YAGNI)

- 汎用 REST API サーバー (§7 の操作 API のみ例外として持つ。発注操作・資金関連設定の変更・mode / autopilot・サービス停止のエンドポイントは作らない)
- 対話シェルへのログのストリーミング表示 (旧 client.py の混線の原因。ログ表示は pull 型コマンドと tail で行う)
- 改善の自動採用 (完全自動マージ / 無承認 plugin ロード)
- local ⇔ claude の自動フォールバック / エスカレーション
- IFD 的な条件監視付き予約注文 (前身の構造的限界の原因。指値 + 期限で代替)
- マルチアセット対応 (FX 2 ペアから始める)
