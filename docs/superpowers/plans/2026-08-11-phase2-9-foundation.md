# Phase 2 プラン 9: 前提整備 実装プラン (設計書 = `2026-08-11-phase2-9-foundation-design.md` 準拠)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** プラン 8 完了時に積み上がった確定済み設計 spec 3 本 (gather deadline / ctx 超過診断 / ohlcv キャッシュ窓) と、spec 小改訂束から落ちた実装項目・独立起票・可観測性の欠落を返済し、改善ループ (プラン 10) が乗る基盤を固める。**新機能は増やさない。**

**Architecture:** 18 task を 5 束に分ける。束 A (ctx 超過診断) は `MissionResult.reason` の契約を土台に検知→運搬→出口→init 可視化を積む。束 C (ohlcv) は窓計算ヘルパを作り、**先に `ohlcv` を `ohlcv_cache` / `ohlcv_history` へ分割してから** `_cached_bars` をキャッシュ専用 API に対して実装する。束 B (gather deadline) は `Clock` と独立した monotonic を注入し、`to_account_rate` の各脚まで deadline を伝播させる。束 D (スキーマ・承認) と束 E (起票返済・可観測性) は独立。**決定論的コアの判定ロジックは全 task で不変。**

**Tech Stack:** Python 3.12 / uv / pytest / sqlite3 (WAL, `PRAGMA foreign_keys=ON`) / httpx (LocalRunner) / pandas (resample) / threading (`core_lock`)

## Global Constraints

- **Anthropic API (従量課金) は使用不可**。Claude 利用は `claude -p` (サブスク認証) のみ。本プランは `ClaudeRunner` を実装しない (プラン 10)
- **発注・SL 変更・クローズ・資金保護は LLM に委ねない。決定論的コードで強制する。** `core/risk_gate.py` / `core/paper_broker.py` / `core/transitions.py` は本プランで **diff ゼロ**
- **`core/executor.py` の変更は 2 種のみ**: ①Task 6/7 の deadline 追加 ②Task 17 の `set_gate_result` 引数追加。**どちらも却下条件・受理条件を変えない**
- **drawdown kill switch は config で無効化不可** — 本プランはこの経路 (`state.update(kill_switch_latched=True)`) に触れない
- 秘密情報は `.env` のみ。`config/settings.yaml` は gitignore、**新キー追加時は `config/settings.yaml.example` と両方を同期する**
- パッケージ管理 uv (`uv sync` / `uv run pytest` / `uv add`)。**TDD (failing test → 実装 → green) を各 step で徹底する**
- 既存テストが 1 本も壊れない (**プラン 9 開始時点 1726 passed / 1 deselected**)
- **`core_lock` を保持したまま外部 I/O (notifier / ネットワーク / サブプロセス) を実行しない。** さらに **scheduler スレッド上で外部 I/O を同期実行しない** (設計書 D3' — lock を外しても同一スレッドなら次の tick が止まり、watchdog はスレッド生存しか見ないので検出できない)
- **migration は空 DB と既存 DB の双方で冪等**であること (Task 13 / 15 / 16 / 17 / 19)

## プラン規約 (マルチエージェント SDD — CLAUDE.md「実装体制」節に準拠。プラン 8 の規約を継承)

- 全体指揮 + 実装監督: opus (main セッション)。実装担当: haiku (機械的 task) / codex (重量 task)。変異検証: haiku 並列 fan-out
- 大 task はテスト転写と実装転写を並列執筆し、統合 + red/green 実行は 1 レーン直列。小 task は丸ごと 1 agent
- 依存の浅い task 束は worktree 並列。**ユーザーへの節目確認は task 単位ではなく束単位**
- **変異テストの前後で `__pycache__` を必ず削除する**。手順: `find . -name __pycache__ -type d -not -path "./.venv/*" -exec rm -rf {} +`。**受入条件の最終 `uv run pytest -q` も pyc 削除後に実行する**
  - 理由: Python は `.pyc` の再利用可否を「元ソースの mtime + サイズ」だけで判定する。**同バイト数の書き換え** (`>=`↔`<=` 等) を**同一秒内に** revert すると `.pyc` が更新されず、以降のテストが変異したまま走る。`git diff` はクリーンなので**差分検査では検出できない**
- **変異を注入したら、必ず該当行を `grep -n` / `sed -n` で表示し、意図した改変が入ったことを目視確認してから pytest を実行する** — 実装者・レビュアー・**指揮者のすべてに適用**
- **変異は 2 方向に広げる**: ①**同じ防御を複数の壊し方で** (呼び出しを消す / 引数を消す / 値を None にする / 条件を反転する) ②**防御の適用範囲全体に** (防御が N 箇所に適用されるなら N 箇所すべてに当てる)
- **各 step の変異リストは「下限」であって天井ではない**。実装者は「この task が守ろうとしている性質」ごとに、それを外して red になるかを自分で確かめ、リストに無い変異を追加したら**報告する**
- **1 つの検査目的につき 1 テスト。** 先に落ちる assert が後続を短絡させ、後続の検査点が無検証のまま残る (プラン 8 Task 19/20 で 2 度実測)。ただし**前提条件の assert は同居してよい** — 見分け方は「その assert が落ちたとき『別の production 欠陥を見つけた』なら検査目的、『テスト自身の fixture/DB が汚れている』なら前提条件」
- **変異は 1 つずつ独立に当て、殺した固有のテスト名を記録した mutation ledger を成果物とする。** 「何本落ちたか」ではなく「狙った検査点のテストが落ちたか」を見る
- `pytest.raises(match=...)` はエラー文言固有の部分文字列に絞る (plugin 名/パス/repr 由来の偽陽性が過去 3 度発生)
- **レビューの段構成** (プラン 8 の裁定を継承): 段 0 = 指揮者の変異スイープ (レビュー**前**) → 1 周目 = codex + **ローカル 3 本** (並列・Claude 枠ゼロ) → 2 周目 = `/code-review high` + ローカル 3 本 → 3 周目 = ブリーフ付き sonnet (2 周目で must-fix が出たときのみ)
  - **有償 2 本 (`/code-review` と sonnet) は並列にしない**。無償 (codex・ローカル) は並列でよい
  - **段 0 スイープと外部レビューを同じ worktree で並列に走らせない** (変異を注入する主体は同時に 1 つだけ)
  - 1 周目・2 周目のレビューと修正は指揮者がユーザーの許可を得ずに実施してよい。**3 周目の要否はユーザーが判断する**
- **ローカル LLM 3 本 = `qwen3-coder-30b-a3b-instruct` + `kwaipilot-kat-coder-v2.5-dev` + `muse-glimmer-30b-low`** (2026-08-11 ユーザー裁定で 2 本 → 3 本)。**取る穴の型が違うので 3 本とも回す** — KAT は「呼び出し元を検証していない」型の構造的な読み、muse-glimmer は「境界値が退化して変異が生きる」型の数値的な読み、qwen は重複が多いが誤検出が少ない。コストゼロなので**指揮者の判断で黙って外してはならない**。直接 API (`http://127.0.0.1:8080/v1/chat/completions`) へ**逐次**投げる (llama-swap は 1 本しかロードしない)。材料は 4〜6k トークン・**全文形式** (diff 不可)
  - **`muse-glimmer-30b` (high) は使わない** — 6.7k の材料で 8,000 トークン全部を thinking に使い**本文 0 バイト**で終わる実測。**low のみ**
  - **muse-glimmer にだけ設定が違う**: `temperature`/`top_p`/`reasoning_effort` を**送らない** (サンプリングはサーバ側の unsloth 推奨値、思考量はエンドポイント側の `reasoning_strength`)
  - **KAT の `[Critical]` ラベルは無視**し、「この行を消してもテストが通る」形の列挙だけを実測対象に拾う
  - **指摘はそのまま採用しない。裏取りは必ず指揮者が変異または実データで行う**
- **重大度が割れたら指揮者が再判定する**。codex と sonnet の重大度評価は系統的にずれる
- **指揮者検証はほどほどに** — 続ける: 現物照合・機械照合・全体テスト・プランの変異リストの裏取り・レビュアーの誤検出の反証。控える: 自主変異の網羅的掃討 (2〜3 件の抜き取りに留める)
- レジャー: `.superpowers/sdd/2026-08-11-phase2-9-foundation/progress.md`

## 着手前に実装者が読むもの (再記述しない — 設計の正はこれら)

| 文書 | 何の正か |
|---|---|
| `docs/superpowers/specs/2026-08-11-phase2-9-foundation-design.md` | **本プランの設計書**。D1〜D8 の裁定と変異リスト |
| `docs/superpowers/specs/2026-08-11-gather-deadline-design.md` | 束 B の正。**「確定仕様」節のみ**が正 (初稿本文は誤りを含む履歴) |
| `docs/superpowers/specs/2026-08-10-context-overflow-diagnosis-design.md` (改訂 3) | 束 A の正。検査点 21・受入条件 9 項目 |
| `docs/superpowers/specs/2026-08-10-ohlcv-cache-fallback-design.md` (**改訂 5**) | 束 C の正。**改訂 5 の節が本文に優先する** (ohlcv 分割に伴う差分・検査点 17) |
| `docs/superpowers/specs/2026-07-25-agentic-fx-design.md` (改訂 17) | 本体設計書。§12 スキーマ・§7 承認・§5 Risk Gate |

## 着手前の既知事実 (指揮者が実コードで確認済み — 2026-08-11)

**これらを再調査しない。ただし着手時に現物と食い違ったら報告すること** (プラン記述側の欠陥を疑う):

| 事実 | 場所 | 含意 |
|---|---|---|
| `LIVE_SOURCES = {"yfinance","twelvedata","mt5-live"}` が**既に存在**し、`upsert_bars` が検証している | `store/ohlcv.py:21,65-69` | **D2 の「書き分けを API で強制」は半分実装済み**。Task 16 は「対称の allowlist を `import_bars` に足す + テーブル分離」 |
| `import_bars` の source 検証は**非空チェックのみ** | `store/ohlcv.py:217-218` | 履歴側 allowlist が無い (Task 16 で追加) |
| `load_bars` は**既に `since` / `until` を持つ** | `store/ohlcv.py:93-95` | spec ③ の「`since` 付き `load_bars`」に**新規引数は不要**。呼び出し側が渡していないだけ |
| `KNOWN_OHLCV_SOURCES` を `service.py:_validate_startup` が producer_source typo 検出に使う | `store/ohlcv.py:28` | Task 16 の分割時に整合を保つこと |
| `ANALYSIS_SOURCE = "dukascopy"` / `_EVAL_SOURCE = "dukascopy"` | `backtest/analysis.py:57` / `plugin/approval.py:86` | 分析・採用ゲートは**履歴のみ**を読む (分割の境界が割れている根拠) |
| `set_gate_result` の呼び出しは **21 箇所** (`core/executor.py` + `loops/trade_loop.py:318`)、**うち 4 箇所が `accepted=True`** (指揮者の初版は 23 と誤記 — 定義行とコメント言及を含めていた。束 E 執筆時に発見) | — | Task 17 は全 call site を機械的に列挙すること |
| `intents.insert(conn, mission_id, payload: dict, now)` は **`TradeIntent` ではなく任意の dict** を受ける | `store/intents.py:8` | Task 17 の `action` は**必須引数で渡す** (payload から再抽出しない) |
| maintenance hook は**毎 tick 呼ばれ、tick 全体が `core_lock` 内** | `service.py:665-671` | Task 16 の prune は lock 内可 (DB のみ)、**通知は不可** |
| commit-post は `core_lock` 非保持で **mission supervisor スレッド**、通知の定位置 | `loops/trade_loop.py:357-364` | Task 17 の評価と通知はここに置く |
| `conn_supervisor` は lock 外の**読み取り専用**接続 (プラン 8 新設) | `service.py` | Task 17 の判定 SQL はこれを使う |
| `_finest_native_base` は「導出できる**最も粗い**ネイティブ足」を選ぶ (関数名に反する) | `datafeed/price_provider.py:301` | Task 8/16 の窓計算・保持期間下限の根拠 |
| 実 DB は **116K・`ohlcv` 0 行** | `data/agentic.db` | Task 16 の移行コストは実質ゼロ (ただし migration は非空 DB でもテストする) |

## File Structure

**新規作成**:

| ファイル | 責務 |
|---|---|
| `src/agentic_fx/datafeed/cache_window.py` | spec ③ の窓計算 (`live_window_days`) と要求 interval 境界への floor (`1d` を含む)。純関数・DB 非依存 (Task 8) |
| `src/agentic_fx/store/alert_state.py` | 通知抑制の永続 KV (`get` / `set` の 2 関数のみ。既知 key 1 個) (Task 17) |
| `src/agentic_fx/store/reflection_attempts.py` | reflection の試行回数台帳 (`bump` / `clear` / `attempts_of`) (Task 15) |
| `tests/datafeed/test_cache_window.py` ほか各 task のテスト | — |

**主な変更**:

| ファイル | 変更 | task |
|---|---|---|
| `src/agentic_fx/runners/base.py` | `MissionResult.reason` 追加 + `AgentRunner` docstring 規範 | 1 |
| `src/agentic_fx/runners/local_runner.py` | HTTP error envelope 解析・`safe_text` 安全化 | 2 |
| `src/agentic_fx/mission_worker.py` / `runners/worker_runner.py` | `result` frame に `reason` を載せ / 読む | 3 |
| `src/agentic_fx/loops/trade_loop.py` | `reason` を activity + 通知へ / **Task 17 の評価を commit-post に追加** | 4, 17 |
| `src/agentic_fx/loops/reflection_cycle.py` | `reflection_mission_failed` 新設 / **再試行ポリシー** | 4, 15 |
| `src/agentic_fx/service.py` | `_check_llama_swap` の n_ctx 可視化 / 起動時の保持期間検証 | 5, 16 |
| `src/agentic_fx/core/executor.py` | deadline 検査点 / `set_gate_result` 引数追加 (**判定ロジック不変**) | 6, 7, 17 |
| `src/agentic_fx/datafeed/price_provider.py` | monotonic 注入と脚伝播 / `_cached_bars` の窓適用 | 7, 9, 10 |
| `src/agentic_fx/store/ohlcv.py` | **`ohlcv_cache` / `ohlcv_history` への分割** + prune + import 側 allowlist | 16 |
| `src/agentic_fx/store/db.py` | DDL + migration (signals FK / trade_intents 列 / 新テーブル / ohlcv 分割 / improvement_runs) | 13, 15, 16, 17, 19 |
| `src/agentic_fx/store/intents.py` | `insert` に `action` 必須引数 / `set_gate_result` に `reject_category` 必須引数 | 17 |
| `src/agentic_fx/tools/plugin_loader.py` | 最新決定優先 | 12 |
| `src/agentic_fx/store/improve_runs.py` | `finish()` から `pr_url` 引数を落とす (**本表の初版で漏れていた** — 束 D 執筆時に発見。`improve_runs.py:16,19,21`) | 19 |
| `src/agentic_fx/config.py` + `config/settings.yaml.example` | `reflection.max_attempts` / `datafeed.cache_retention_days` / `alert.consecutive_gate_reject` | 15, 16, 17 |

## Task 一覧・依存・Interfaces

**実行グラフ**: `A / C / D は worktree 並列` → `B は C の後` → `E は A・C・D・B すべての後`。**束 C の内部は `8 → 16 → 9 → 10 → 11` の直列**。

| 束 | # | task | 依存 |
|---|---|---|---|
| A | 1 | `MissionResult.reason` 契約 + `AgentRunner` docstring 規範 | — |
| A | 2 | ctx 超過の検知 (envelope 解析・安全化) | 1 |
| A | 3 | `reason` の worker 運搬 | 1 |
| A | 4 | trade / reflection 出口 | 1, 2 |
| A | 5 | init の `n_ctx` 可視化 | — |
| C | 8 | 窓計算ヘルパ + floor | — |
| C | 16 | **`ohlcv` 2 テーブル分割 + prune + 保持期間検証** | 8 |
| C | 9 | `lookback_days` 配線 | 8, 16 |
| C | 10 | `_cached_bars` 窓適用 | 8, 9, 16 |
| C | 11 | 本番連鎖 E2E pin + 実データ実測 | 10 |
| B | 6 | monotonic 注入 + gather deadline (OPEN/CLOSE 両方) | C 完了後 |
| B | 7 | deadline の脚伝播 (`to_account_rate`) | 6 |
| D | 12 | approval 最新決定優先 | — |
| D | 13 | `signals.claimed_by_mission_id` FK migration | — |
| D | 19 | `improvement_runs` の PR 列 migration | — |
| E | 15 | reflection の再試行ポリシー | 4 |
| E | 17 | `gate_rejected` の可観測性 | B 完了後 (`executor.py` 競合) |
| E | 18 | `llama_swap.timeout_sec` 実測 | — |

### 束をまたぐ Interfaces (実装者が隣の task の名前と型を知るための唯一の情報源)

```python
# Task 1 が produces (束 A の全 task と Task 15 が consumes)
@dataclass
class MissionResult:
    status: Literal["completed", "failed", "timeout", "max_turns"]
    output: dict[str, Any] | None
    transcript: list[dict[str, Any]] = field(default_factory=list)
    reason: str | None = None          # ← 追加。既定 None で既存呼び出しは無変更

# Task 8 が produces (Task 16 と Task 10 が consumes)
# src/agentic_fx/datafeed/cache_window.py
def live_window_days(source: str, interval: str, lookback_days: int) -> int: ...
def floor_to_interval(ts: datetime, interval: str) -> datetime: ...   # 1d は UTC 00:00

# Task 16 が produces (Task 10 が consumes)
# src/agentic_fx/store/ohlcv.py
IMPORT_SOURCES: frozenset[str]                    # {"dukascopy", "mt5"}
def upsert_cache_bars(conn, bars: list[Bar], *, source: str) -> int: ...
def load_cache_bars(conn, symbol: str, interval: str, *, source: str,
                    since: datetime | None = None,
                    until: datetime | None = None) -> list[Bar]: ...
def prune_cache(conn, *, cutoff: datetime, limit: int) -> int: ...   # 削除行数を返す
def import_history_bars(conn, rows: list[tuple], *, source: str) -> ImportResult: ...
def load_history_bars(conn, symbol: str, interval: str, *, source: str,
                      since: datetime | None = None,
                      until: datetime | None = None) -> list[Bar]: ...
def load_history_spread(conn, symbol: str, interval: str,
                        bar_time_iso: str, *, source: str) -> float | None: ...

# Task 17 が produces (呼び出し側 = executor.py / trade_loop.py の全 call site)
# src/agentic_fx/store/intents.py
def insert(conn, mission_id: int, payload: dict, now: datetime, *,
           action: str) -> int: ...                       # ← action は必須
def set_gate_result(conn, intent_id: int, *, accepted: bool,
                    reject_reason: str | None,
                    reject_category: str | None) -> None: ...
# reject_category: accepted=True なら必ず None /
#                  accepted=False なら "risk_gate" | "origin" | "mission" | "execution"

# Task 15 が produces
# src/agentic_fx/store/reflection_attempts.py
def bump(conn, order_id: int, *, now: datetime, reason: str | None) -> int: ...  # 新 attempts
def clear(conn, order_id: int) -> None: ...
def attempts_of(conn, order_id: int) -> int: ...

# Task 17 が produces
# src/agentic_fx/store/alert_state.py
def get(conn, key: str) -> str | None: ...
def set(conn, key: str, value: str, *, now: datetime) -> None: ...
GATE_REJECT_STREAK_KEY = "gate_reject.last_notified_streak_id"
```

---

<!-- 各 task の詳細 step は束ごとに追記する -->


---

## プラン 9 束 A 詳細 step: ctx 超過の診断 (Task 1〜5)

**親文書**: `docs/superpowers/plans/2026-08-11-phase2-9-foundation.md` (骨格 — Global Constraints / プラン規約 / Interfaces / 既知事実は本書では再記述しない)
**設計の正**: `docs/superpowers/specs/2026-08-10-context-overflow-diagnosis-design.md` (改訂 3) — 検査点 21 個・受入条件 9 項目

本書は束 A (Task 1〜5) の実装 step のみを扱う。束 B〜E は別書。

### 束内の実行順序

Task 1 (契約) → { Task 2 (検知), Task 3 (運搬), Task 5 (init 可視化) は Task 1 の後なら並列可 } → Task 4 (出口。Task 1 と Task 2 に依存 — reason が既に安全化済みである前提で activity/通知に足すだけで、trade_loop/reflection_cycle 側で再安全化しない)。

検査点 21 個の Task 割り当て:

| 検査点 | 内容 | Task |
|---|---|---|
| 1〜9 | LocalRunner の検知・安全化 | Task 2 |
| 10〜12 | worker 経由の運搬 | Task 3 |
| 13〜16 | trade/reflection の出口 | Task 4 |
| 17〜21 | init の可視化 | Task 5 |

---

### Task 1: `MissionResult.reason` 契約 + `AgentRunner` docstring 規範

**Files:**
- Modify: `src/agentic_fx/runners/base.py:38-47` (`MissionResult` dataclass と `AgentRunner` クラス)
- Test: `tests/runners/test_base.py` (末尾に追記)

**Interfaces:**
- Consumes: なし (依存なし)
- Produces (束 A の Task 2/3/4 が使う):
  ```python
  @dataclass
  class MissionResult:
      status: Literal["completed", "failed", "timeout", "max_turns"]
      output: dict[str, Any] | None
      transcript: list[dict[str, Any]] = field(default_factory=list)
      reason: str | None = None   # 既定 None。キーワード引数 `reason=` で設定する
  ```
  `AgentRunner.__doc__` に reason の規範 (Task 2 の実装者と将来の `ClaudeRunner` 実装者向け) を書く。

#### 現状 (`src/agentic_fx/runners/base.py:38-47`)

```python
@dataclass
class MissionResult:
    status: Literal["completed", "failed", "timeout", "max_turns"]
    output: dict[str, Any] | None
    transcript: list[dict[str, Any]] = field(default_factory=list)


class AgentRunner(ABC):
    @abstractmethod
    def run(self, mission: Mission) -> MissionResult: ...
```

- [ ] **Step 1: 失敗するテストを書く**

`tests/runners/test_base.py` の末尾に追記する:

```python
def test_mission_result_reason_defaults_to_none_via_keywords():
    """Task 1: reason は既定 None (キーワード構築)。"""
    r = MissionResult(status="completed", output={"a": 1}, transcript=[])
    assert r.reason is None


def test_mission_result_reason_defaults_to_none_via_existing_positional_call():
    """Task 1: 既存の 3 positional 引数の呼び出しパターン (プラン 8 以前
    からの全呼び出し site — 本文中で確認済み) が無変更のまま動作し、
    reason は既定 None になる。この形が壊れると reason を必須化する
    変異を見逃す。"""
    r = MissionResult("failed", None, [])
    assert r.reason is None


def test_mission_result_reason_can_be_set_via_keyword():
    """Task 1: reason はキーワード引数で明示設定できる。"""
    r = MissionResult(status="failed", output=None, transcript=[],
                      reason="context exceeded: prompt 1 tokens > n_ctx 2")
    assert r.reason == "context exceeded: prompt 1 tokens > n_ctx 2"


def test_agent_runner_docstring_states_reason_contract():
    """Task 1: AgentRunner の docstring に reason の規範が書かれている
    (設計: 2026-08-10-context-overflow-diagnosis-design.md §4.3)。"""
    doc = AgentRunner.__doc__ or ""
    assert "reason" in doc
    assert "外部応答の本文を生で" in doc
```

- [ ] **Step 2: 失敗を確認**

```bash
uv run pytest tests/runners/test_base.py -v
```

期待される失敗 (実測で確認済み — 実装前の現物で検証した):

```
$ uv run python -c "
from agentic_fx.runners.base import MissionResult, AgentRunner
r = MissionResult('failed', None, [])
print(r.reason)
"
AttributeError: 'MissionResult' object has no attribute 'reason'
```

- `test_mission_result_reason_defaults_to_none_via_keywords`: `AttributeError: 'MissionResult' object has no attribute 'reason'`
- `test_mission_result_reason_defaults_to_none_via_existing_positional_call`: 同上
- `test_mission_result_reason_can_be_set_via_keyword`: `TypeError: MissionResult.__init__() got an unexpected keyword argument 'reason'`
- `test_agent_runner_docstring_states_reason_contract`: `AssertionError: assert 'reason' in ''` (`AgentRunner.__doc__` が現状 `None`。実測で確認済み)

- [ ] **Step 3: 最小実装**

`src/agentic_fx/runners/base.py:38-47` を以下に置き換える:

```python
@dataclass
class MissionResult:
    status: Literal["completed", "failed", "timeout", "max_turns"]
    output: dict[str, Any] | None
    transcript: list[dict[str, Any]] = field(default_factory=list)
    # runner が診断できた失敗理由 (安全化済み・単一行・上限内)。既定 None —
    # 既存呼び出しは無変更 (設計書 2026-08-10-context-overflow-diagnosis-
    # design.md §4.2)。「なぜ失敗したか」の軸であり、`status` (「どの
    # 終端状態か」の軸) とは独立 — 新しい status 値は作らない (同 §4.1)。
    reason: str | None = None


class AgentRunner(ABC):
    """Mission 実行を抽象化する runner インターフェース。

    **reason の規範** (設計書 docs/superpowers/specs/2026-08-10-context-
    overflow-diagnosis-design.md §4.3): runner は `failed`/`timeout`/
    `max_turns` を返すとき、可能な限り安全化済みの `reason` を設定する。
    外部応答の本文を生で入れない — activity ログ・Discord 通知・worker
    の result frame をそのまま経由しうるため、秘密や長大なペイロードを
    漏らしてはならない。

    **現在の適用範囲**: 本規範を満たすのは `LocalRunner` (HTTP failure
    から解釈できた reason — プラン 9 Task 2) のみ。`WorkerRunner` が
    親側で生成する失敗 (起動 timeout・protocol error・EOF) と
    `ClaudeRunner` (未実装、プラン 10 スコープ) の理由付けはこの規範の
    対象外 — 予測実装しない (同 §4.3)。プラン 10 で `ClaudeRunner` を
    実装する task は、この docstring の規範を満たす契約テストを
    ブロッキングチェックリストに含めること。
    """

    @abstractmethod
    def run(self, mission: Mission) -> MissionResult: ...
```

- [ ] **Step 4: 成功を確認**

```bash
uv run pytest tests/runners/test_base.py -v
```

全件 green を確認する (`test_mission_result_reason_defaults_to_none_via_keywords` /
`test_mission_result_reason_defaults_to_none_via_existing_positional_call` /
`test_mission_result_reason_can_be_set_via_keyword` /
`test_agent_runner_docstring_states_reason_contract` を含む全体)。

- [ ] **Step 5: 変異テスト**

```bash
find . -name __pycache__ -type d -not -path "./.venv/*" -exec rm -rf {} +
```

| # | 変異 | 殺すテスト |
|---|---|---|
| M1 | `reason` の既定値を `None` から `"unknown"` に変える | `test_mission_result_reason_defaults_to_none_via_keywords` |
| M2 | `reason` から `= None` を外し必須引数にする (positional 4 番目が必須になる) | `test_mission_result_reason_defaults_to_none_via_existing_positional_call` (`TypeError: missing 1 required positional argument`) |
| M3 | `AgentRunner` の docstring から「外部応答の本文を生で」の一文を削除する | `test_agent_runner_docstring_states_reason_contract` |

各変異を注入したら `grep -n "reason" src/agentic_fx/runners/base.py` で改変を目視確認してから対象テストのみ実行し red を確認、revert して green に戻す。

- [ ] **Step 6: コミット**

```bash
git add src/agentic_fx/runners/base.py tests/runners/test_base.py
git commit -m "$(cat <<'EOF'
feat: MissionResult.reason 契約 + AgentRunner docstring 規範 (プラン9 Task1)

ctx 超過診断 (設計書 2026-08-10-context-overflow-diagnosis-design.md)
の土台。reason は既定 None で既存呼び出しは無変更。status の4終端契約
は変えず、「なぜ失敗したか」を別軸として運ぶ。

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: ctx 超過の検知 (LocalRunner の HTTP error envelope 解析)

**Files:**
- Modify: `src/agentic_fx/runners/local_runner.py:22` (import に `safe_text` を追加)、`:28-32` (定数追加)、`:82-85` (`_finish` に `reason` 引数追加)、`:100-103` (`except httpx.HTTPError` ブロック)
- Create/Test: `tests/runners/test_local_runner_context_overflow.py` (新規)

**Interfaces:**
- Consumes: `MissionResult(status, output, transcript, reason=...)` (Task 1)。`safe_text(text: str) -> str` / `safe_error_text(e: BaseException) -> str` (`src/agentic_fx/_safe_error.py` — 既存、変更なし)
- Produces: `LocalRunner.run()` が HTTP failure 時に `MissionResult.reason` を設定する。他 task から呼ばれる新規公開関数は無い (`_reason_from_http_error` 等はモジュール private)。Task 3/4 が信頼する契約: **`LocalRunner` が設定する `reason` は既に単一行・長さ上限内・秘密除去済み** — 下流 (worker 運搬・trade/reflection 出口) は再安全化しない。

#### 現状 (`src/agentic_fx/runners/local_runner.py`)

```python
22: from agentic_fx._safe_error import safe_error_text
...
28: _MAX_REPAIR_RETRIES = 2
...
32: _MAX_TOOL_CALLS_PER_TURN = 16
...
82:         def _finish(status: str) -> MissionResult:
83:             """Terminal return with timeout priority (F4)."""
84:             return MissionResult("timeout" if timed_out() else status, None,
85:                                  messages)
...
100:             except httpx.HTTPError as e:
101:                 _log.warning("llama-swap request failed: %s",
102:                              safe_error_text(e))
103:                 return _finish("failed")
```

- [ ] **Step 1: 失敗するテストを書く**

`tests/runners/test_local_runner_context_overflow.py` を新規作成する:

```python
"""LocalRunner の ctx 超過検知・HTTP error envelope 解析 (プラン9 Task2)。

設計: docs/superpowers/specs/2026-08-10-context-overflow-diagnosis-design.md
検査点 1〜9 (§7 の表) + §4.1 の型検証要件 (表に無い追加検査)。
"""
import json

import httpx

from agentic_fx.runners.base import Mission
from agentic_fx.runners.local_runner import LocalRunner
from agentic_fx.tools.registry import ToolRegistry

SCHEMA = {"type": "object", "properties": {"action": {"type": "string"}},
          "required": ["action"]}

_CTX_ENVELOPE = {"error": {
    "code": 400,
    "message": "request (90010 tokens) exceeds the available context "
               "size (65536 tokens), try increasing it",
    "type": "exceed_context_size_error",
    "n_prompt_tokens": 90010, "n_ctx": 65536}}


def _mission(**over):
    d = dict(prompt="p", tools=[], output_schema=SCHEMA, max_turns=8,
             timeout_sec=30)
    d.update(over)
    return Mission(**d)


def _runner_for_json_body(status: int, body: dict, *,
                          model="qwen3.6-35b-a3b_Q4") -> LocalRunner:
    def handler(request):  # noqa: ANN001
        return httpx.Response(status, json=body)
    return LocalRunner(base_url="http://test/v1", model=model,
                       registry=ToolRegistry(),
                       transport=httpx.MockTransport(handler))


def _runner_for_raw_body(status: int, body: bytes, *,
                         model="m") -> LocalRunner:
    def handler(request):  # noqa: ANN001
        return httpx.Response(status, content=body)
    return LocalRunner(base_url="http://test/v1", model=model,
                       registry=ToolRegistry(),
                       transport=httpx.MockTransport(handler))


# ---- 検査点 1: error.type によるコンテキスト判定 ---------------------------

def test_ctx_overflow_detected_by_error_type():
    """type=exceed_context_size_error なら reason に ctx 専用文言が入る。"""
    r = _runner_for_json_body(400, _CTX_ENVELOPE).run(_mission())
    assert "context exceeded" in r.reason


def test_other_error_type_not_treated_as_ctx_overflow():
    """type がそれ以外なら、n_prompt_tokens/n_ctx 相当の値が同居していても
    ctx 専用文言にしない (「type 判定を無視して常に ctx 文言にする」変異の
    キラー — CP1 の陽性テストだけでは常時 ctx 文言化する変異を見逃す)。"""
    envelope = {"error": {"type": "invalid_request_error",
                          "message": "bad request",
                          "n_prompt_tokens": 90010, "n_ctx": 65536}}
    r = _runner_for_json_body(400, envelope).run(_mission())
    assert "context exceeded" not in r.reason


# ---- 検査点 2〜4: reason の構成要素 -----------------------------------------

def test_ctx_overflow_reason_includes_prompt_tokens():
    r = _runner_for_json_body(400, _CTX_ENVELOPE).run(_mission())
    assert "90010" in r.reason


def test_ctx_overflow_reason_includes_n_ctx():
    r = _runner_for_json_body(400, _CTX_ENVELOPE).run(_mission())
    assert "65536" in r.reason


def test_ctx_overflow_reason_includes_model():
    r = _runner_for_json_body(400, _CTX_ENVELOPE,
                              model="qwen3.6-35b-a3b_Q4").run(_mission())
    assert "qwen3.6-35b-a3b_Q4" in r.reason


# ---- 検査点 5: safe_text による安全化・単一行化・長さ上限 -------------------

def test_reason_is_single_line():
    envelope = {"error": {"type": "other",
                          "message": "line one\nline two\ttabbed"}}
    r = _runner_for_json_body(400, envelope).run(_mission())
    assert "\n" not in r.reason
    assert "\t" not in r.reason


def test_reason_collapses_printable_horizontal_whitespace():
    """M5a: isprintable() だけでは除去できない空白を split/join が畳む。"""
    envelope = {"error": {"type": "other", "message": "alpha  beta"}}
    r = _runner_for_json_body(400, envelope).run(_mission())
    assert r.reason == "alpha beta"


def test_reason_strips_non_whitespace_nonprintable_character():
    """M5b: split/join だけでは除去できない NUL を isprintable が落とす。"""
    envelope = {"error": {"type": "other", "message": "alpha\x00beta"}}
    r = _runner_for_json_body(400, envelope).run(_mission())
    # ⚠️ `_normalize_reason` は非印字文字を**削除ではなく空白 1 文字へ置換**する
    # (`ch if ch.isprintable() else " "`)。3 周目レビューで「削除」を前提にした
    # 期待値 "alphabeta" になっており、**正しい実装に対しても永久に red** だった。
    # 指揮者が実測: _normalize_reason("alpha\x00beta") == "alpha beta"
    assert r.reason == "alpha beta"


def test_reason_length_is_capped():
    envelope = {"error": {"type": "other", "message": "x" * 10_000}}
    r = _runner_for_json_body(400, envelope).run(_mission())
    assert len(r.reason) <= 550


def test_reason_uses_safe_text_to_strip_urls():
    envelope = {"error": {"type": "other",
                          "message": "upstream call to "
                                     "http://internal.example/v1?apikey=SECRET123 failed"}}
    r = _runner_for_json_body(400, envelope).run(_mission())
    assert "SECRET123" not in r.reason
    assert "http://internal.example" not in r.reason


# ---- 検査点 6: status は "failed" のまま ------------------------------------

def test_ctx_overflow_status_is_still_failed():
    r = _runner_for_json_body(400, _CTX_ENVELOPE).run(_mission())
    assert r.status == "failed"


# ---- 検査点 7: 非 400 の同一 envelope --------------------------------------

def test_non_400_status_with_same_envelope_is_also_parsed():
    """413 (Payload Too Large) でも同じ envelope なら診断が拾える —
    status code で分岐してはならない (codex I4)。"""
    r = _runner_for_json_body(413, _CTX_ENVELOPE).run(_mission())
    assert "context exceeded" in r.reason


# ---- 検査点 8: 本文が JSON でない / キー欠落 → 例外を出さず汎用文言 --------

def test_non_json_body_falls_back_to_generic_reason():
    r = _runner_for_raw_body(400, b"not json at all").run(_mission())
    assert r.status == "failed"
    assert "HTTP 400" in r.reason


def test_missing_error_key_falls_back_to_generic_reason():
    """JSON としては妥当だが `error` キーが空 — 例外を出さず汎用文言に
    降格する (CP8 の「キー欠落」側)。"""
    r = _runner_for_json_body(400, {"error": {}}).run(_mission())
    assert "HTTP 400" in r.reason


# ---- 検査点 9: 本文が上限超過 → 汎用文言に退避 ------------------------------

def test_oversized_body_falls_back_to_generic_reason():
    huge = json.dumps({"error": {"type": "exceed_context_size_error",
                                 "message": "x" * 200_000,
                                 "n_prompt_tokens": 1, "n_ctx": 2}}).encode()
    r = _runner_for_raw_body(400, huge).run(_mission())
    assert "HTTP 400" in r.reason
    assert "context exceeded" not in r.reason


# ---- 受入条件 5 の補強: 応答本文そのものは transcript に残らない -----------

def test_response_body_not_stored_in_transcript():
    """⚠️ 旧版の 2 本目は `assert "90010" not in serialized or "90010" in
    (r.reason or "")` という **恒真な assert** だった (2 周目のローカル LLM
    レビューで KAT が検出・指揮者が裏取り)。ctx 超過の `reason` には設計上
    必ず `90010` が入る (`test_ctx_overflow_reason_includes_prompt_tokens`
    がそれを要求している) ため **`or` の右辺が常に真**で、左辺が偽でも
    assert は通る = **構造的に失敗し得ない**。
    なお M15 (`messages` に応答本文を append する変異) 自体は 1 本目の
    `exceed_context_size_error` の assert が殺すので「検出不能」ではないが、
    **1 本目を弱めたり別の envelope に差し替えたりした瞬間に無防備になる**。
    `reason` 側の検査は別テストの担当なので、ここでは transcript だけを
    厳密に見る (1 検査目的 1 テスト)。"""
    r = _runner_for_json_body(400, _CTX_ENVELOPE).run(_mission())
    serialized = json.dumps(r.transcript)
    assert "exceed_context_size_error" not in serialized   # エラー種別が漏れない
    assert "90010" not in serialized                       # 本文由来の数値も漏れない


# ---- 追加検査 (spec 21 項目には無いが §4.1 の型検証要件): -----------------
# 「コンテキスト専用文言に使う値は str/bool を除く正整数に型を絞る。
#  不正な型なら汎用の error.message に降格する」

def test_ctx_prompt_tokens_wrong_type_downgrades_to_message():
    envelope = {"error": {"type": "exceed_context_size_error",
                          "message": "context is full, please retry",
                          "n_prompt_tokens": "90010",  # str — 不正
                          "n_ctx": 65536}}
    r = _runner_for_json_body(400, envelope).run(_mission())
    assert "context exceeded" not in r.reason
    assert "context is full, please retry" in r.reason


def test_ctx_n_ctx_bool_downgrades_to_message():
    envelope = {"error": {"type": "exceed_context_size_error",
                          "message": "ctx bool edge case",
                          "n_prompt_tokens": 1,
                          "n_ctx": True}}  # bool — 正整数として扱わない
    r = _runner_for_json_body(400, envelope).run(_mission())
    assert "context exceeded" not in r.reason
    assert "ctx bool edge case" in r.reason
```

- [ ] **Step 2: 失敗を確認**

```bash
uv run pytest tests/runners/test_local_runner_context_overflow.py -v
```

期待される失敗: 全 16 件が red。実測で確認した代表例 (Task 1 実装済み・Task 2 未実装の状態):

```
$ uv run python -c "
import httpx
from agentic_fx.runners.base import Mission
from agentic_fx.runners.local_runner import LocalRunner
from agentic_fx.tools.registry import ToolRegistry
env = {'error': {'type':'exceed_context_size_error','message':'x',
                  'n_prompt_tokens':90010,'n_ctx':65536}}
def handler(request):
    return httpx.Response(400, json=env)
runner = LocalRunner(base_url='http://test/v1', model='m',
                     registry=ToolRegistry(), transport=httpx.MockTransport(handler))
m = Mission(prompt='p', tools=[], output_schema={'type':'object'}, max_turns=8, timeout_sec=30)
r = runner.run(m)
print(r.status, repr(r.reason))
"
failed None
```
→ `test_ctx_overflow_detected_by_error_type`: `TypeError: argument of type 'NoneType' is not iterable` (`"context exceeded" in None`)。他の `reason` 依存テストも同型で fail。

- [ ] **Step 3: 最小実装**

`src/agentic_fx/runners/local_runner.py:22` の import 行を置き換える:

```python
from agentic_fx._safe_error import safe_error_text, safe_text
```

`:28-32` の定数群の直後 (`_MAX_TOOL_CALLS_PER_TURN = 16` の後) に追加する:

```python
_CTX_EXCEEDED_TYPE = "exceed_context_size_error"
# 実測の envelope は ~200B。65KB 超は異常 (例: リバースプロキシが返す
# HTML エラーページ) として解析せず汎用文言に退避する (設計書 §4.1)。
_MAX_ERROR_BODY_BYTES = 65536
# activity 一行・Discord 通知・worker result frame を肥大化させない上限
# (設計書 §4.1 「文字数の上限を定める」)。
_MAX_REASON_CHARS = 500


def _is_positive_int(v: object) -> bool:
    """bool を除く正整数か (設計書 §4.1: 「str/bool を除く正整数」)。"""
    return isinstance(v, int) and not isinstance(v, bool) and v > 0


def _normalize_reason(text: str) -> str:
    """`reason` を activity/通知向けに正規化する: `safe_text` で URL・
    秘密を伏字にし、改行・制御文字を単一行へ畳み、文字数上限で切り詰める
    (設計書 §4.1 — 安全化・単一行化・長さ上限)。"""
    text = safe_text(text)
    text = "".join(ch if ch.isprintable() else " " for ch in text)
    text = " ".join(text.split())
    if len(text) > _MAX_REASON_CHARS:
        text = text[:_MAX_REASON_CHARS] + "…(truncated)"
    return text


def _reason_from_http_error(e: httpx.HTTPError, model: str) -> str:
    """HTTPError から `reason` を組み立てる (設計書 §4.1)。

    **status code で分岐しない** — 413/422/500 でも同じ JSON error
    envelope を同じロジックで解析する (codex I4)。解析できなければ
    (JSON でない・上限超過・形状不正・型不正) 例外を出さず、従来どおりの
    汎用文言 (`safe_error_text(e)`) に退避する。**レスポンス本文そのもの
    はここで一度読むだけで、呼び出し元 (`messages`/transcript) には
    一切渡さない。**
    """
    if not isinstance(e, httpx.HTTPStatusError):
        return _normalize_reason(safe_error_text(e))
    body = e.response.content
    if len(body) > _MAX_ERROR_BODY_BYTES:
        return _normalize_reason(safe_error_text(e))
    try:
        payload = json.loads(body)
    except ValueError:
        return _normalize_reason(safe_error_text(e))
    if not isinstance(payload, dict):
        return _normalize_reason(safe_error_text(e))
    err = payload.get("error")
    if not isinstance(err, dict):
        return _normalize_reason(safe_error_text(e))
    if err.get("type") == _CTX_EXCEEDED_TYPE:
        n_prompt = err.get("n_prompt_tokens")
        n_ctx = err.get("n_ctx")
        if _is_positive_int(n_prompt) and _is_positive_int(n_ctx):
            return _normalize_reason(
                f"context exceeded: prompt {n_prompt} tokens > "
                f"n_ctx {n_ctx} (model={model})")
    message = err.get("message")
    if isinstance(message, str) and message:
        return _normalize_reason(message)
    return _normalize_reason(safe_error_text(e))
```

`:82-85` の `_finish` を置き換える:

```python
        def _finish(status: str, *, reason: str | None = None) -> MissionResult:
            """Terminal return with timeout priority (F4)."""
            return MissionResult("timeout" if timed_out() else status, None,
                                 messages, reason=reason)
```

`:100-103` の `except httpx.HTTPError` ブロックを置き換える:

```python
            except httpx.HTTPError as e:
                reason = _reason_from_http_error(e, self._model)
                _log.warning("llama-swap request failed: %s",
                             safe_error_text(e))
                return _finish("failed", reason=reason)
```

- [ ] **Step 4: 成功を確認**

```bash
uv run pytest tests/runners/test_local_runner_context_overflow.py -v
uv run pytest tests/runners/ -v
```

新規 16 件すべて green、かつ `tests/runners/test_local_runner_errors.py` (既存 12 件、`_finish` のシグネチャ変更を経由する) が壊れていないことを確認する。

- [ ] **Step 5: 変異テスト**

```bash
find . -name __pycache__ -type d -not -path "./.venv/*" -exec rm -rf {} +
```

| # | 変異 | 殺すテスト |
|---|---|---|
| M1 | `err.get("type") == _CTX_EXCEEDED_TYPE` の判定を削除し常に ctx 文言を返す | `test_other_error_type_not_treated_as_ctx_overflow` |
| M2 | `n_prompt_tokens` の f-string 埋め込みを削除 (`prompt X tokens` の `X` を空にする) | `test_ctx_overflow_reason_includes_prompt_tokens` |
| M3 | `n_ctx` の f-string 埋め込みを削除 | `test_ctx_overflow_reason_includes_n_ctx` |
| M4 | `model=` の f-string 埋め込みを削除 | `test_ctx_overflow_reason_includes_model` |
| M5a | `_normalize_reason` の空白畳み込み (`" ".join(text.split())`) を削除 | `test_reason_collapses_printable_horizontal_whitespace` |
| M5b | `_normalize_reason` の `isprintable()` フィルタを削除 | `test_reason_strips_non_whitespace_nonprintable_character` |
| M6 | `_normalize_reason` の長さ上限判定 (`if len(text) > _MAX_REASON_CHARS`) を削除 | `test_reason_length_is_capped` |
| M7 | `_normalize_reason` から `safe_text(text)` 呼び出しを削除 | `test_reason_uses_safe_text_to_strip_urls` |
| M8 | `_finish` 内の `status` を固定文字列 `"context_exceeded"` に差し替える | `test_ctx_overflow_status_is_still_failed` |
| M9 | `_reason_from_http_error` の先頭に `if e.response.status_code != 400: return _normalize_reason(safe_error_text(e))` を追加する (400 限定化) | `test_non_400_status_with_same_envelope_is_also_parsed` |
| M10 | `json.loads(body)` を `try/except` で囲むのをやめる (無防備化) | `test_non_json_body_falls_back_to_generic_reason` |
| M11 | `err.get("message")` の `isinstance(message, str) and message` 判定を `True` に固定 (空/非文字列でも通す) | `test_missing_error_key_falls_back_to_generic_reason` |
| M12 | `_MAX_ERROR_BODY_BYTES` のサイズ判定 (`if len(body) > _MAX_ERROR_BODY_BYTES`) を削除 | `test_oversized_body_falls_back_to_generic_reason` |
| M13 | `_is_positive_int` から `not isinstance(v, bool)` を削除 (bool を正整数として通す) | `test_ctx_n_ctx_bool_downgrades_to_message` |
| M14 | `_is_positive_int` から `isinstance(v, int)` チェックを削除し `v > 0` だけにする (文字列 `"90010" > 0` は TypeError で落ちるため別の壊し方: `_is_positive_int` を `lambda v: True` に差し替える) | `test_ctx_prompt_tokens_wrong_type_downgrades_to_message` |
| M15 | `run()` の `except httpx.HTTPError` で `messages` に応答本文を append する (transcript 汚染を注入) | `test_response_body_not_stored_in_transcript` |

各変異を注入したら `grep -n "<変更箇所>" src/agentic_fx/runners/local_runner.py` で改変を目視確認してから対象テストのみ実行し red を確認、revert して green に戻す。

- [ ] **Step 6: コミット**

```bash
git add src/agentic_fx/runners/local_runner.py tests/runners/test_local_runner_context_overflow.py
git commit -m "$(cat <<'EOF'
feat: LocalRunner に HTTP error envelope 解析を追加しctx超過を診断 (プラン9 Task2)

status code では分岐せず error.type=exceed_context_size_error を判定
(413/422/500 でも同一 envelope を拾う)。reason は safe_text で安全化
した上で単一行・500 文字上限に正規化し、応答本文そのものは transcript
にもログにも残さない。パース不能・形状不正・型不正・上限超過は例外を
出さず従来どおりの汎用文言へ退避する。

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: `reason` の worker 運搬

**Files:**
- Modify: `src/agentic_fx/mission_worker.py:419-421` (improve profile の result frame)、`:491-493` (trade profile の result frame)
- Modify: `src/agentic_fx/runners/worker_runner.py:249-255` (`kind == "result"` の分岐)
- Test: `tests/test_mission_worker_protocol.py` (末尾に追記)、`tests/runners/test_worker_runner.py` (末尾に追記)

**Interfaces:**
- Consumes: `MissionResult.reason` (Task 1)。子プロセス内では `LocalRunner.run(mission)` の戻り値 (Task 2 の実装が使われるが、Task 3 のテストは `_FakeLocalRunner` 差し替えで検証するため Task 2 の実装完了を待たずに書ける)
- Produces: `mission_worker.py` の `result` フレームが `{"type": "result", "status": ..., "output": ..., "reason": ...}` の形になる。`WorkerRunner.run(mission) -> MissionResult` が子から届いた `reason` を `.reason` に載せる。他 task はこれ以上のシグネチャに依存しない (`WorkerRunner.run` の型は変わらない — 既存の `AgentRunner.run(mission: Mission) -> MissionResult`)。

#### 現状 (`src/agentic_fx/mission_worker.py:415-426` — improve profile)

```python
415:             _send_frame(protocol_out, out_seq, {"type": "ready", "ok": True})
416:             ready_sent = True
417:             try:
418:                 result = runner.run(mission)
419:                 _send_frame(protocol_out, out_seq, {
420:                     "type": "result",
421:                     "status": result.status, "output": result.output})
422:             except Exception as exc:  # noqa: BLE001
423:                 _send_frame(protocol_out, out_seq, {
424:                     "type": "result", "status": "failed", "output": None,
425:                     "error": f"{type(exc).__name__}: {exc}"})
426:             return
```

#### 現状 (`src/agentic_fx/mission_worker.py:486-497` — trade profile)

```python
486:         _send_frame(protocol_out, out_seq, {"type": "ready", "ok": True})
487:         ready_sent = True
488:
489:         try:
490:             result = runner.run(mission)
491:             _send_frame(protocol_out, out_seq, {
492:                 "type": "result",
493:                 "status": result.status, "output": result.output})
494:         except Exception as exc:  # noqa: BLE001 — 必ず result を送る
495:             _send_frame(protocol_out, out_seq, {
496:                 "type": "result", "status": "failed", "output": None,
497:                 "error": f"{type(exc).__name__}: {exc}"})
```

#### 現状 (`src/agentic_fx/runners/worker_runner.py:249-255`)

```python
249:             if kind == "result":
250:                 status = payload["status"]
251:                 output = payload.get("output")
252:             else:  # eof / protocol_error / error — すべて failed に正規化
253:                 status = "failed"
254:                 output = None
255:             return MissionResult(status, output, transcript)
```

- [ ] **Step 1: 失敗するテストを書く**

`tests/test_mission_worker_protocol.py` の末尾に追記する (既存の `_drive_main`/`_FakeLocalRunner` 由来のシーム、`_handshake_settings` を再利用する):

```python
class _FakeLocalRunnerWithReason:
    """Task 3 (CP10) 用の `LocalRunner` 差し替え。reason 付き failed
    `MissionResult` を返す。`_FakeLocalRunner` と違い `on_message` は
    呼ばない (event フレームは本テストの対象外)。"""

    instances: list["_FakeLocalRunnerWithReason"] = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        _FakeLocalRunnerWithReason.instances.append(self)

    def run(self, mission):
        from agentic_fx.runners.base import MissionResult
        return MissionResult(
            status="failed", output=None,
            reason="context exceeded: prompt 90010 tokens > "
                   "n_ctx 65536 (model=m)")


def test_main_puts_reason_in_result_frame_when_runner_sets_it(
        monkeypatch, tmp_path):
    """Task 3 / CP10: LocalRunner が MissionResult.reason を設定したら、
    子は result フレームにそれを載せる (worker_profile=trade 経路)。"""
    frames, _, _ = _drive_main(monkeypatch, tmp_path,
                               runner_cls=_FakeLocalRunnerWithReason)
    result_frame = frames[-1]
    assert result_frame["type"] == "result"
    assert result_frame["status"] == "failed"
    assert result_frame["reason"] == (
        "context exceeded: prompt 90010 tokens > n_ctx 65536 (model=m)")


def test_main_puts_reason_in_result_frame_for_improve_profile(
        monkeypatch, tmp_path):
    """Task 3 / CP10 の improve profile 側 (別コードパス — mission_worker.py
    の improve 分岐は trade 分岐と独立した result frame 構築コードを持つ)。"""
    # Landlock はプロセス生涯に不可逆。既存テストと同じ seam で bootstrap を
    # 止め、pytest プロセスを sandbox 化しない。
    # 命名は同ファイル冒頭の既存 import (`from agentic_fx import mission_worker`)
    # と既存テストに合わせる (新規テストだけ別名を持ち込まない)。
    monkeypatch.setattr(mission_worker, "_bootstrap_improve_profile",
                        lambda *args, **kwargs: None)

    def settings_mutator(settings_dict):
        pass  # improve は既定 settings のまま (backend=local)

    frames, _, _ = _drive_main(
        monkeypatch, tmp_path,
        handshake_overrides={"worker_profile": "improve",
                             "db_path": None, "plugins_dir": None},
        settings_mutator=settings_mutator,
        runner_cls=_FakeLocalRunnerWithReason)
    result_frame = frames[-1]
    assert result_frame["type"] == "result"
    assert result_frame["reason"] == (
        "context exceeded: prompt 90010 tokens > n_ctx 65536 (model=m)")
```

`tests/runners/test_worker_runner.py` の末尾に追記する (既存の `_root`/`_rag`/`_mission`/`SETTINGS` を再利用する):

```python
def test_worker_runner_reads_reason_from_result_frame(tmp_path, monkeypatch):
    """Task 3 / CP11: 子が result フレームに載せた reason が
    MissionResult.reason まで届く。"""
    r, w = os.pipe()
    r2, w2 = os.pipe()

    def child_thread_fn():
        child_in = os.fdopen(r2, "rb")
        child_out = os.fdopen(w, "wb")
        handshake = json.loads(child_in.readline())
        assert handshake["type"] == "handshake"
        write_frame(child_out, {"type": "ready", "seq": 1, "ok": True})
        write_frame(child_out, {"type": "result", "seq": 2,
                                "status": "failed", "output": None,
                                "reason": "context exceeded: prompt 1 "
                                          "tokens > n_ctx 2 (model=m)"})
        child_out.close()

    t = threading.Thread(target=child_thread_fn, daemon=True)

    class FakeProc:
        pid = os.getpid()
        stdin = os.fdopen(w2, "wb")
        stdout = os.fdopen(r, "rb")
        returncode = None

        def poll(self):
            return None

        def wait(self, timeout=None):
            return -9

    fake_proc = FakeProc()

    import agentic_fx.runners.worker_runner as wr_mod
    monkeypatch.setattr(wr_mod.subprocess, "Popen", lambda *a, **k: fake_proc)
    monkeypatch.setattr(wr_mod.os, "killpg", lambda pid, sig: None)

    root = _root(tmp_path)
    clock = FixedClock(datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc))
    runner = WorkerRunner(root=root, settings=SETTINGS, clock=clock,
                          rag=_rag(tmp_path))
    t.start()
    result = runner.run(_mission())
    t.join(timeout=2.0)

    assert result.status == "failed"
    assert result.reason == (
        "context exceeded: prompt 1 tokens > n_ctx 2 (model=m)")


def test_worker_runner_tolerates_unknown_keys_in_result_frame(
        tmp_path, monkeypatch):
    """Task 3 / CP12 (codex M2): result フレームに reason に加えて未知
    キーが混ざっても reader は落ちず、reason を含む既知キーだけを使って
    MissionResult を組み立てる (将来のフレーム拡張に対する前方互換の
    回帰固定 — `mission_protocol.read_frame` は dict であることと seq
    しか検証しないことを実コードで確認済み)。"""
    r, w = os.pipe()
    r2, w2 = os.pipe()

    def child_thread_fn():
        child_in = os.fdopen(r2, "rb")
        child_out = os.fdopen(w, "wb")
        handshake = json.loads(child_in.readline())
        assert handshake["type"] == "handshake"
        write_frame(child_out, {"type": "ready", "seq": 1, "ok": True})
        write_frame(child_out, {"type": "result", "seq": 2,
                                "status": "completed", "output": {"x": 1},
                                "reason": "some reason",
                                "future_field_not_yet_defined": "ignore me"})
        child_out.close()

    t = threading.Thread(target=child_thread_fn, daemon=True)

    class FakeProc:
        pid = os.getpid()
        stdin = os.fdopen(w2, "wb")
        stdout = os.fdopen(r, "rb")
        returncode = None

        def poll(self):
            return None

        def wait(self, timeout=None):
            return -9

    fake_proc = FakeProc()

    import agentic_fx.runners.worker_runner as wr_mod
    monkeypatch.setattr(wr_mod.subprocess, "Popen", lambda *a, **k: fake_proc)
    monkeypatch.setattr(wr_mod.os, "killpg", lambda pid, sig: None)

    root = _root(tmp_path)
    clock = FixedClock(datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc))
    runner = WorkerRunner(root=root, settings=SETTINGS, clock=clock,
                          rag=_rag(tmp_path))
    t.start()
    result = runner.run(_mission())
    t.join(timeout=2.0)

    assert result.status == "completed"
    assert result.output == {"x": 1}
    assert result.reason == "some reason"
```

- [ ] **Step 2: 失敗を確認**

```bash
uv run pytest tests/test_mission_worker_protocol.py -k "reason" -v
uv run pytest tests/runners/test_worker_runner.py -k "reason or unknown_keys" -v
```

期待される失敗:

- `test_main_puts_reason_in_result_frame_when_runner_sets_it`: `KeyError: 'reason'` (result frame dict に `"reason"` キーが無い)
- `test_main_puts_reason_in_result_frame_for_improve_profile`: 同上
- `test_worker_runner_reads_reason_from_result_frame`: `AssertionError: assert None == 'context exceeded: prompt 1 tokens > n_ctx 2 (model=m)'`
- `test_worker_runner_tolerates_unknown_keys_in_result_frame`: `AssertionError: assert None == 'some reason'`

- [ ] **Step 3: 最小実装**

`src/agentic_fx/mission_worker.py:419-421` (improve profile) を置き換える:

```python
                _send_frame(protocol_out, out_seq, {
                    "type": "result",
                    "status": result.status, "output": result.output,
                    "reason": result.reason})
```

`src/agentic_fx/mission_worker.py:491-493` (trade profile) を置き換える:

```python
            _send_frame(protocol_out, out_seq, {
                "type": "result",
                "status": result.status, "output": result.output,
                "reason": result.reason})
```

(いずれも `except Exception as exc:` 側の result frame は変更しない — 子自身の未捕捉例外は Task 1 §4.3 が範囲外と定めた「WorkerRunner が親側で生成する失敗」と同種の、runner 経由でない失敗のため。)

`src/agentic_fx/runners/worker_runner.py:249-255` を置き換える:

```python
            if kind == "result":
                status = payload["status"]
                output = payload.get("output")
                reason = payload.get("reason")
            else:  # eof / protocol_error / error — すべて failed に正規化
                status = "failed"
                output = None
                reason = None
            return MissionResult(status, output, transcript, reason=reason)
```

- [ ] **Step 4: 成功を確認**

```bash
uv run pytest tests/test_mission_worker_protocol.py -v
uv run pytest tests/runners/test_worker_runner.py -v
```

新規 4 件を含め全件 green。特に `test_worker_runner_completes_mission_via_pipes` (reason を送らない既存の子スクリプト) が `payload.get("reason")` で `None` に落ちて壊れないことを確認する。

- [ ] **Step 5: 変異テスト**

```bash
find . -name __pycache__ -type d -not -path "./.venv/*" -exec rm -rf {} +
```

| # | 変異 | 殺すテスト |
|---|---|---|
| M1 | `mission_worker.py` trade profile の result frame から `"reason": result.reason` を削除する | `test_main_puts_reason_in_result_frame_when_runner_sets_it` |
| M2 | `mission_worker.py` improve profile の result frame から `"reason": result.reason` を削除する | `test_main_puts_reason_in_result_frame_for_improve_profile` |
| M3 | `worker_runner.py` の `reason = payload.get("reason")` を `reason = None` に固定する | `test_worker_runner_reads_reason_from_result_frame` |
| M4 | `worker_runner.py` の `if kind == "result":` 節の前に、`payload` のキーを既知集合 (`{"type","seq","status","output"}`) だけに絞る厳格化コード (未知キーがあれば `ProtocolError`) を追加する | `test_worker_runner_tolerates_unknown_keys_in_result_frame` |

各変異を注入したら `grep -n "reason" src/agentic_fx/mission_worker.py src/agentic_fx/runners/worker_runner.py` で改変を目視確認してから対象テストのみ実行し red を確認、revert して green に戻す。

- [ ] **Step 6: コミット**

```bash
git add src/agentic_fx/mission_worker.py src/agentic_fx/runners/worker_runner.py \
       tests/test_mission_worker_protocol.py tests/runners/test_worker_runner.py
git commit -m "$(cat <<'EOF'
feat: worker 経由でも MissionResult.reason を親まで運ぶ (プラン9 Task3)

LocalRunner は WorkerRunner 経由では子プロセス内で走るため、
result フレームに reason を載せないと親に届かない。mission_protocol
は frame が dict であることと seq しか検証しないため、キー追加は
既存 reader に対して非破壊 (未知キー耐性を回帰テストで固定)。

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 4: trade / reflection 出口

**Files:**
- Modify: `src/agentic_fx/loops/trade_loop.py:214-217` (`mission_failed` の activity + 通知)
- Modify: `src/agentic_fx/loops/reflection_cycle.py:158-175` (`reflection_mission_failed` の新設)
- Test: `tests/loops/test_trade_loop.py` (末尾に追記)、`tests/loops/test_reflection_cycle.py` (末尾に追記)

**Interfaces:**
- Consumes: `MissionResult.reason` (Task 1)。**Task 2 が保証する「reason は既に安全化・単一行化・上限内」を信頼し、trade_loop/reflection_cycle は reason を再安全化しない** (依存: Task 1, 2 — 骨格の依存表と一致)。テストは `FakeRunner` が返す `MissionResult(..., reason=...)` を直接使うため、Task 3 (worker 運搬) の完了は待たない。
- Produces: 新しい activity event 名 `"reflection_mission_failed"` (`Category.AGGREGATE`、`ref_id=str(order_id)`)。本 task 以外のどの task もこの event 名の生成主体にはならない。

**受入条件 9 (reflection の無制限再試行・starvation・DB 増大を独立課題として起票する) の扱い**: 「起票」はこのリポジトリの慣行 (spec/plan 文書への明記) に従う。設計書 §4.5「同時に判明した、より重い問題 (codex I3)」の節が既にその起票そのものである — 本 task で新たに書く文書は無い。本 task が担うのは「現挙動を回帰テストで固定する」側 (検査点 16) のみで、Step 1 の `test_reflection_current_retry_behavior_is_pinned` がそれに当たる。

#### 現状 (`src/agentic_fx/loops/trade_loop.py:209-218`)

```python
209:             if result.status != "completed":
210:                 with self._core_lock:
211:                     finalize_mission(self.conn, self.activity, self.clock,
212:                                      mid, result)
213:                 finalized = True
214:                 self.activity.write(Category.AGGREGATE, "mission_failed",
215:                                     f"runner status={result.status}",
216:                                     ref_id=str(mid))
217:                 self.notifier.send(f"[agentic-fx] 判断 Mission 失敗: {result.status}")
218:                 return None
```

#### 現状 (`src/agentic_fx/loops/reflection_cycle.py:158-175`)

```python
158:             with self._core_lock:
159:                 finalize_ok = finalize_mission(self.conn, self.activity,
160:                                                self.clock, mid, result)
161:             # **(レビュー 1 周目 F1 の副作用への対処)** `finalized` は
162:             # 「finalize_mission を試みたか」を表す (TradeLoop と同じ意味 —
163:             # 外側 finally の二重 finalize 抑止用)。`finalize_ok` (戻り値)
164:             # とは別物 — `finalize_mission` が `False` (書込み自体が例外で
165:             # 失敗) を返しても、それは「試みて失敗した」のであって「試みて
166:             # いない」ではない。ここで区別しないと、外側 finally が
167:             # 「まだ finalize していない」と誤認して `missions.finish` を
168:             # 二重に呼んでしまう (実測で発見・修正)。
169:             finalized = True
170:
171:             if result.status != "completed" or not finalize_ok:
172:                 # finish 失敗時は監査未確定 (missions 行が running のまま) なので
173:                 # reflection も保存しない。SQLite マーカー (reflections 行) が
174:                 # 無いので次周期の run_pending が同じ order を再試行する
175:                 return False
```

- [ ] **Step 1: 失敗するテストを書く**

`tests/loops/test_trade_loop.py` の末尾に追記する (既存の `_loop` ヘルパー・`MagicMock` import を再利用する):

```python
def test_mission_failed_activity_includes_reason_when_present(tmp_path):
    """Task 4 / CP13: reason があれば mission_failed activity 本文に
    含まれる。"""
    conn, loop, _, tp = _loop(tmp_path, [MissionResult(
        "failed", None, [], reason="context exceeded: prompt 90010 tokens "
                                    "> n_ctx 65536 (model=m)")])
    assert loop.run_once() is None
    act = (tp / "a.log").read_text(encoding="utf-8")
    assert "context exceeded: prompt 90010 tokens > n_ctx 65536" in act


def test_mission_failed_notification_includes_reason_when_present(tmp_path):
    """Task 4 / CP14: reason があれば通知本文にも含まれる。"""
    conn, loop, _, tp = _loop(tmp_path, [MissionResult(
        "failed", None, [], reason="context exceeded: prompt 1 tokens "
                                    "> n_ctx 2 (model=m)")])
    loop.notifier.send = MagicMock()
    assert loop.run_once() is None
    sent = loop.notifier.send.call_args[0][0]
    assert "context exceeded: prompt 1 tokens > n_ctx 2" in sent
```

`tests/loops/test_reflection_cycle.py` の末尾に追記する (既存の `_cycle`/`_closed_order` ヘルパーを再利用する):

```python
def test_reflection_mission_failed_activity_written_with_reason(tmp_path):
    """Task 4 / CP15: reflection Mission 失敗時に reflection_mission_failed
    activity が reason 付きで書かれる (通知は出さない — cyc は Notifier を
    持たないため、通知しないことは構造的に保証されている)。"""
    conn, rag, cyc = _cycle(tmp_path, [MissionResult(
        "failed", None, [], reason="context exceeded: prompt 1 tokens "
                                    "> n_ctx 2 (model=m)")])
    oid = _closed_order(conn)
    assert cyc.run_pending() == 0
    act = (tmp_path / "a.log").read_text(encoding="utf-8")
    assert "reflection_mission_failed" in act
    assert "context exceeded: prompt 1 tokens > n_ctx 2" in act
    assert f"order_id={oid}" in act


def test_reflection_current_retry_behavior_is_pinned(tmp_path):
    """Task 4 / CP16 (回帰固定): 同一 order で 2 回 run_pending を呼んでも
    starvation せず**毎回同じ order が選ばれ続ける** — missions 行が 2 本
    (どちらも failed)・reflection_mission_failed activity が 2 本・
    reflections 行は 0 本のまま (無制限再試行の現挙動。修正は設計書 §4.5
    codex I3 で既に独立課題として起票済み・本 task では直さない)。"""
    conn, rag, cyc = _cycle(tmp_path, [
        MissionResult("failed", None, [], reason="boom1"),
        MissionResult("failed", None, [], reason="boom2"),
    ])
    oid = _closed_order(conn)
    assert cyc.run_pending() == 0
    assert cyc.run_pending() == 0

    failed_missions = conn.execute(
        "SELECT COUNT(*) c FROM missions WHERE status='failed'"
    ).fetchone()["c"]
    assert failed_missions == 2

    act = (tmp_path / "a.log").read_text(encoding="utf-8")
    assert act.count("reflection_mission_failed") == 2

    assert reflections.get(conn, oid) is None
```

- [ ] **Step 2: 失敗を確認**

```bash
uv run pytest tests/loops/test_trade_loop.py -k "reason" -v
uv run pytest tests/loops/test_reflection_cycle.py -k "reflection_mission_failed or retry_behavior_is_pinned" -v
```

期待される失敗:

- `test_mission_failed_activity_includes_reason_when_present`: `AssertionError: assert 'context exceeded: prompt 90010 tokens > n_ctx 65536' in '...'` (activity 行に `runner status=failed` はあるが reason が付いていない)
- `test_mission_failed_notification_includes_reason_when_present`: 同型の `AssertionError`
- `test_reflection_mission_failed_activity_written_with_reason`: `AssertionError: assert 'reflection_mission_failed' in ''` (activity ファイルに何も書かれていない — 現状 reflection 失敗は無記録)
- `test_reflection_current_retry_behavior_is_pinned`: この 1 本は現挙動をそのまま検査しているため、`reflection_mission_failed` の書込みが未実装だと `act.count("reflection_mission_failed") == 2` の部分だけ `AssertionError: assert 0 == 2` で red になる (missions 行数・reflections 行の部分は既に現挙動どおり green — 「テスト全体が red」であって「全 assert が red」ではないことに注意)

- [ ] **Step 3: 最小実装**

`src/agentic_fx/loops/trade_loop.py:214-217` を置き換える:

```python
                self.activity.write(
                    Category.AGGREGATE, "mission_failed",
                    f"runner status={result.status}"
                    + (f" — {result.reason}" if result.reason else ""),
                    ref_id=str(mid))
                self.notifier.send(
                    f"[agentic-fx] 判断 Mission 失敗: {result.status}"
                    + (f" — {result.reason}" if result.reason else ""))
```

`src/agentic_fx/loops/reflection_cycle.py:169` (`finalized = True` の直後) と `:171` (`if result.status != "completed" or not finalize_ok:`) の間に挿入する:

```python
            finalized = True

            if result.status != "completed":
                # プラン 9 Task 4: reflection Mission の失敗を可視化する。
                # 通知は出さない (資金に直結しないため activity で足りる —
                # 設計書 2026-08-10-context-overflow-diagnosis-design.md
                # §4.5)。
                try:
                    self.activity.write(
                        Category.AGGREGATE, "reflection_mission_failed",
                        f"order_id={row['id']} mission_id={mid} "
                        f"status={result.status}"
                        + (f" — {result.reason}" if result.reason else ""),
                        ref_id=str(row["id"]))
                except Exception:  # noqa: BLE001 — 記録の失敗で reflection 経路を止めない
                    _log.exception(
                        "failed to record reflection_mission_failed for #%s",
                        row["id"])

            if result.status != "completed" or not finalize_ok:
```

(`if result.status != "completed" or not finalize_ok:` 以降の本体はそのまま。)

- [ ] **Step 4: 成功を確認**

```bash
uv run pytest tests/loops/test_trade_loop.py -v
uv run pytest tests/loops/test_reflection_cycle.py -v
```

全件 green。特に既存の `test_runner_failure_recorded` (trade, reason=None) と `test_runner_failure_skips_for_retry` (reflection, reason=None) が壊れていないこと (reason が None のとき文言に `— None` が混入しないこと) を確認する。

- [ ] **Step 5: 変異テスト**

```bash
find . -name __pycache__ -type d -not -path "./.venv/*" -exec rm -rf {} +
```

| # | 変異 | 殺すテスト |
|---|---|---|
| M1 | `trade_loop.py` の activity.write から `+ (f" — {result.reason}" ...)` を削除する | `test_mission_failed_activity_includes_reason_when_present` |
| M2 | `trade_loop.py` の notifier.send から同上を削除する | `test_mission_failed_notification_includes_reason_when_present` |
| M3 | `reflection_cycle.py` に挿入した `if result.status != "completed": ... self.activity.write(...)` ブロック全体を削除する | `test_reflection_mission_failed_activity_written_with_reason` |
| M4 | `reflection_cycle.py` の `_reflect_one` 冒頭で、`run_pending` が呼ぶ SQL に対して「直近に failed だった order を除外する」フィルタを追加する (無言の再試行制限の一例。実装イメージ: `self._core_lock` ブロック内の SELECT に `AND o.id NOT IN (SELECT ref_id FROM ... WHERE ...)` 相当を足す、または `self` に `_recently_failed: set[int]` を持たせて `run_pending` 側でスキップする) | `test_reflection_current_retry_behavior_is_pinned` (2 回目の `run_pending()` が同じ order を選ばなくなり `failed_missions == 2` が崩れる) |

各変異を注入したら `grep -n "reason\|reflection_mission_failed" src/agentic_fx/loops/trade_loop.py src/agentic_fx/loops/reflection_cycle.py` で改変を目視確認してから対象テストのみ実行し red を確認、revert して green に戻す。

- [ ] **Step 6: コミット**

```bash
git add src/agentic_fx/loops/trade_loop.py src/agentic_fx/loops/reflection_cycle.py \
       tests/loops/test_trade_loop.py tests/loops/test_reflection_cycle.py
git commit -m "$(cat <<'EOF'
feat: trade/reflection の Mission 失敗出口に reason を載せる (プラン9 Task4)

trade loop は既存の mission_failed activity/通知に reason を足すだけ。
reflection loop は失敗を一切記録していなかったため
reflection_mission_failed activity を新設 (通知は出さない — 資金に
直結しないため)。無制限再試行・starvation の現挙動はそのまま (修正は
別課題 — 設計書 §4.5 codex I3 で起票済み)、回帰テストで固定した。

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 5: init の `n_ctx` 可視化

**Files:**
- Modify: `src/agentic_fx/service.py:73-110` (`_check_llama_swap` を置き換え、`_model_load_order`/`_fetch_model_ctx` を新設)
- Modify: `tests/test_service_app.py:711-722` (`_RunnerStub`/`_StubSettings` に `improve` を追加)、末尾に新規テスト追記

**Interfaces:**
- Consumes: なし (依存なし。`settings.runner.trade.model` / `settings.runner.improve.model` / `settings.llama_swap.base_url` は既存の config surface)
- Produces: なし (束 A の葉 task。`n_ctx` は保存しない — §3 のドリフト回避)

**制約の再確認 (spec §4.4 より)**:
- **限定 except のみ** (`httpx.RequestError` / `httpx.HTTPStatusError` / `ValueError` / `TypeError` / `KeyError`)。`except Exception` は使わない
- 取得した `n_ctx` は **bool を除く正整数**のみ表示する
- **既存 6 テスト (`test_check_llama_swap_*`, `tests/test_service_app.py:744-815`) を 1 本も壊さない** — `_StubSettings` は `trade.model == improve.model` (同一) なので、既存 6 テストは「同じモデル」分岐を通り続ける

#### 現状 (`src/agentic_fx/service.py:73-110`)

```python
73: def _check_llama_swap(settings) -> None:
74:     """llama-swap 接続確認 (上書き 2)。一覧取得不能 / モデル不在 / cold-load
75:     smoke 失敗の 3 種を区別して警告する。init から呼ばれる (失敗は警告のみ —
76:     取引判断 Mission は実行時に fail closed で保護される)。"""
77:     import httpx
78:     base = settings.llama_swap.base_url
79:     model = settings.runner.trade.model
80:
81:     try:
82:         r = httpx.get(f"{base}/models", timeout=5)
83:         r.raise_for_status()
84:         ids = [m.get("id") for m in r.json().get("data", [])
85:                if isinstance(m, dict)]
86:     except (httpx.RequestError, httpx.HTTPStatusError,
87:             ValueError, TypeError) as e:
88:         print(f"警告: llama-swap のモデル一覧を取得できません ({e})。"
89:               "取引判断 Mission は失敗として記録されます。")
90:         return
91:
92:     if model not in ids:
93:         print(f"警告: モデル '{model}' が llama-swap の /models に存在しません。"
94:               f"alias 設定を確認してください (存在: {ids})")
95:         return
96:
97:     try:
98:         # cold-load smoke: TTL unload 後の初回 Mission がロード時間で
99:         # timeout しないよう、1 トークン生成でロードを促す
100:         r = httpx.post(f"{base}/chat/completions",
101:                        json={"model": model, "max_tokens": 1,
102:                              "messages": [{"role": "user", "content": "ping"}]},
103:                        timeout=120)
104:         r.raise_for_status()
105:     except (httpx.RequestError, httpx.HTTPStatusError) as e:
106:         print(f"警告: モデル '{model}' の cold-load smoke に失敗しました ({e})。"
107:               "初回 Mission が timeout する可能性があります。")
108:         return
109:
110:     print(f"llama-swap OK (model '{model}' loaded)")
```

#### 現状 (`tests/test_service_app.py:707-722`)

```python
707: class _LlamaSwapStub:
708:     base_url = "http://localhost:8080/v1"
709:
710:
711: class _RunnerChoiceStub:
712:     model = "qwen3.6-35b"
713:
714:
715: class _RunnerStub:
716:     trade = _RunnerChoiceStub()
717:
718:
719: class _StubSettings:
720:     llama_swap = _LlamaSwapStub()
721:     runner = _RunnerStub()
```

- [ ] **Step 1: 失敗するテストを書く**

まず `tests/test_service_app.py:715-717` の `_RunnerStub` に `improve` を追加する (既存 6 テストは `trade.model == improve.model` になり挙動が変わらないことを Step 4 で確認する):

```python
class _RunnerStub:
    trade = _RunnerChoiceStub()
    improve = _RunnerChoiceStub()  # 既定は trade と同一モデル (既存 6 テストの前提を変えない)
```

次に、異なるモデルのケース用に `_StubSettings` の直後へ追加する:

```python
class _RunnerChoiceStubTrade:
    model = "trade-m"


class _RunnerChoiceStubImprove:
    model = "improve-m"


class _RunnerStubDiff:
    trade = _RunnerChoiceStubTrade()
    improve = _RunnerChoiceStubImprove()


class _StubSettingsDiff:
    llama_swap = _LlamaSwapStub()
    runner = _RunnerStubDiff()
```

そして `tests/test_service_app.py` の `_check_llama_swap` テスト群 (既存 6 テストの直後) に追記する:

```python
# ---- プラン9 Task5: n_ctx 可視化 (CP17〜21) ---------------------------------

def test_check_llama_swap_same_model_calls_props_once(capsys):
    """CP17: trade == improve なら /props は 1 回だけ呼ばれる。"""
    calls = {"props": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "qwen3.6-35b"}]})
        if path == "/props":
            calls["props"] += 1
            return httpx.Response(200, json={
                "default_generation_settings": {"n_ctx": 65536}})
        return httpx.Response(200, json={})

    client = _mock_client(handler)
    with patch("httpx.get", client.get), patch("httpx.post", client.post):
        _check_llama_swap(_StubSettings())
    assert calls["props"] == 1
    out = capsys.readouterr().out
    assert "ctx 65536" in out


def test_check_llama_swap_different_models_improve_first_trade_last(capsys):
    """CP18: trade != improve なら improve の /props が先 (表示のみ)、
    trade は存在確認→smoke→/props の順で**最後**に処理される。"""
    call_order: list[tuple[str, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "trade-m"}]})
        if path == "/props":
            model = request.url.params.get("model")
            call_order.append(("props", model))
            return httpx.Response(200, json={
                "default_generation_settings": {"n_ctx": 4096}})
        if path.endswith("/chat/completions"):
            call_order.append(("smoke", None))
            return httpx.Response(200, json={})
        return httpx.Response(200, json={})

    client = _mock_client(handler)
    with patch("httpx.get", client.get), patch("httpx.post", client.post):
        _check_llama_swap(_StubSettingsDiff())

    kinds = [c[0] for c in call_order]
    assert kinds == ["props", "smoke", "props"]
    models_called = [c[1] for c in call_order if c[0] == "props"]
    assert models_called == ["improve-m", "trade-m"]


def test_check_llama_swap_props_http_failure_does_not_fail_init(capsys):
    """CP19: /props の HTTP 失敗でも init は成功する (表示だけ省略)。"""
    calls = {"props": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "qwen3.6-35b"}]})
        if path == "/props":
            calls["props"] += 1
            return httpx.Response(500)
        return httpx.Response(200, json={})

    client = _mock_client(handler)
    with patch("httpx.get", client.get), patch("httpx.post", client.post):
        _check_llama_swap(_StubSettings())  # 例外を出さない = init は成功
    assert calls["props"] >= 1  # /props に実際に到達したことを確認
    out = capsys.readouterr().out
    assert "llama-swap OK" in out
    assert "ctx" not in out


def test_check_llama_swap_props_ctx_wrong_type_is_not_displayed(capsys):
    """CP20 (str): n_ctx が文字列なら表示しない。init は成功する。"""
    calls = {"props": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "qwen3.6-35b"}]})
        if path == "/props":
            calls["props"] += 1
            return httpx.Response(200, json={
                "default_generation_settings": {"n_ctx": "65536"}})
        return httpx.Response(200, json={})

    client = _mock_client(handler)
    with patch("httpx.get", client.get), patch("httpx.post", client.post):
        _check_llama_swap(_StubSettings())
    assert calls["props"] >= 1
    out = capsys.readouterr().out
    assert "llama-swap OK" in out
    assert "ctx" not in out


def test_check_llama_swap_props_ctx_missing_is_not_displayed(capsys):
    """CP20 (欠落): n_ctx キーが無ければ表示しない。init は成功する。"""
    calls = {"props": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "qwen3.6-35b"}]})
        if path == "/props":
            calls["props"] += 1
            return httpx.Response(200, json={"default_generation_settings": {}})
        return httpx.Response(200, json={})

    client = _mock_client(handler)
    with patch("httpx.get", client.get), patch("httpx.post", client.post):
        _check_llama_swap(_StubSettings())
    assert calls["props"] >= 1
    out = capsys.readouterr().out
    assert "llama-swap OK" in out
    assert "ctx" not in out


def test_check_llama_swap_props_ctx_bool_is_not_displayed(capsys):
    """CP20 (bool): n_ctx が bool なら表示しない (「bool を除く正整数」)。"""
    calls = {"props": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "qwen3.6-35b"}]})
        if path == "/props":
            calls["props"] += 1
            return httpx.Response(200, json={
                "default_generation_settings": {"n_ctx": True}})
        return httpx.Response(200, json={})

    client = _mock_client(handler)
    with patch("httpx.get", client.get), patch("httpx.post", client.post):
        _check_llama_swap(_StubSettings())
    assert calls["props"] >= 1
    out = capsys.readouterr().out
    assert "llama-swap OK" in out
    assert "ctx" not in out


def test_check_llama_swap_unexpected_exception_in_props_is_not_swallowed():
    """CP21: RequestError/HTTPStatusError/ValueError/TypeError/KeyError の
    いずれでもない想定外例外は握らず、_check_llama_swap を通じて呼び出し
    元まで伝播する (init は落ちる)。"""
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "qwen3.6-35b"}]})
        if path == "/props":
            raise RuntimeError("unexpected boom")
        return httpx.Response(200, json={})

    client = _mock_client(handler)
    with patch("httpx.get", client.get), patch("httpx.post", client.post), \
         pytest.raises(RuntimeError, match="unexpected boom"):
        _check_llama_swap(_StubSettings())
```

- [ ] **Step 2: 失敗を確認**

```bash
uv run pytest tests/test_service_app.py -k "check_llama_swap" -v
```

期待される失敗 (実測で確認済み — 現行 `_check_llama_swap` は `/props` を一度も呼ばない):

- `test_check_llama_swap_same_model_calls_props_once`: `AssertionError: assert 0 == 1`
- `test_check_llama_swap_different_models_improve_first_trade_last`: `AssertionError: assert [] == ['props', 'smoke', 'props']`
- `test_check_llama_swap_props_http_failure_does_not_fail_init`: `AssertionError: assert 0 >= 1`
- `test_check_llama_swap_props_ctx_wrong_type_is_not_displayed` / `_missing_is_not_displayed` / `_bool_is_not_displayed`: 同型 `AssertionError: assert 0 >= 1`
- `test_check_llama_swap_unexpected_exception_in_props_is_not_swallowed`: `Failed: DID NOT RAISE <class 'RuntimeError'>` (現行コードは `/props` に到達しないため例外が発生しない)

既存 6 テスト (`test_check_llama_swap_ok` 等) は `_RunnerStub` への `improve` 追加だけでは壊れないことを確認する:

```bash
uv run pytest tests/test_service_app.py -k "check_llama_swap_ok or model_missing or list_connect_error or list_http_error or smoke_http_error or smoke_timeout" -v
```

→ 全 6 件 green のまま (現行 `_check_llama_swap` は `settings.runner.improve` を一切参照しないため)。

- [ ] **Step 3: 最小実装**

> **追補 (2 周目レビュー)**: 以下の指示は着手時点の記述であり、**現行実装とは
> 意図的に異なる**。`_fetch_model_ctx` には後から**必須引数 `timeout: float`**
> が加わり、`timeout=5` のリテラルは `_COLD_LOAD_TIMEOUT = 120` /
> `_PROPS_TIMEOUT_HOT = 5` に分離された。`/props` はモデルをロードさせるため、
> smoke より前に呼ぶ improve 側は必ず cold になり、5 秒では届かない
> (実機 35B 実測 cold 13.86s / hot 0.0005s)。3 周目レビューで cold load 本体で
> ある smoke 自身も同じ予算に束ねた。詳細は §③ と実装のコメントを参照。

`src/agentic_fx/service.py:73-110` を以下に置き換える:

```python
def _model_load_order(settings) -> list[str]:
    """trade/improve の重複除去済み順序付きリスト。異なる場合は
    improve→trade (trade を最後に置くのは意図的 — 設計書 §4.4: llama-swap
    の常駐数/VRAM/TTL 次第では後発ロードが先発を unload しうるため、
    init 終了時に取引判断で使うモデルを hot な状態で終わらせる)。"""
    trade = settings.runner.trade.model
    improve = settings.runner.improve.model
    return [trade] if trade == improve else [improve, trade]


def _fetch_model_ctx(base: str, model: str) -> int | None:
    """`GET /props?model=<model>` から `default_generation_settings.n_ctx`
    を取得する。**例外境界を限定する** (codex I2) — 取得・形状のいずれかの
    失敗でも None を返し、想定外例外は伝播させて init を落とす。"""
    import httpx
    try:
        api_root = base.rstrip("/")
        if api_root.endswith("/v1"):
            api_root = api_root[:-3]
        r = httpx.get(f"{api_root}/props", params={"model": model}, timeout=5)
        r.raise_for_status()
        n_ctx = r.json()["default_generation_settings"]["n_ctx"]
    except (httpx.RequestError, httpx.HTTPStatusError, ValueError,
            TypeError, KeyError):
        return None
    if not isinstance(n_ctx, int) or isinstance(n_ctx, bool) or n_ctx <= 0:
        return None
    return n_ctx


def _check_llama_swap(settings) -> None:
    """llama-swap 接続確認 (上書き 2、プラン 9 Task 5)。一覧取得不能 /
    モデル不在 / cold-load smoke 失敗の 3 種を区別して警告する。init から
    呼ばれる (失敗は警告のみ — 取引判断 Mission は実行時に fail closed で
    保護される)。

    **対象モデルは重複除去した順序付きリスト** (`_model_load_order`)。
    trade == improve なら存在確認→smoke→/props を各 1 回。異なるなら
    improve の /props を先に (表示のみ)、trade は従来どおり最後に
    存在確認→smoke→/props (設計書 §4.4 — trade を最後に置くのは意図的。
    improve が trade と異なる場合、init に cold load 1 回分の時間が
    増えることを既知コストとして許容する)。`n_ctx` は表示のみで
    保存しない (§3 のドリフト回避 — llama-swap 側の --ctx-size 変更で
    陳腐化するため)。
    """
    import httpx
    base = settings.llama_swap.base_url
    trade_model = settings.runner.trade.model
    improve_model = settings.runner.improve.model

    try:
        r = httpx.get(f"{base}/models", timeout=5)
        r.raise_for_status()
        ids = [m.get("id") for m in r.json().get("data", [])
               if isinstance(m, dict)]
    except (httpx.RequestError, httpx.HTTPStatusError,
            ValueError, TypeError) as e:
        print(f"警告: llama-swap のモデル一覧を取得できません ({e})。"
              "取引判断 Mission は失敗として記録されます。")
        return

    if trade_model not in ids:
        print(f"警告: モデル '{trade_model}' が llama-swap の /models に存在しません。"
              f"alias 設定を確認してください (存在: {ids})")
        return

    order = _model_load_order(settings)
    if len(order) == 2:
        improve_ctx = _fetch_model_ctx(base, order[0])
        if improve_ctx is not None:
            print(f"improve model '{order[0]}' ctx {improve_ctx}")

    try:
        # cold-load smoke: TTL unload 後の初回 Mission がロード時間で
        # timeout しないよう、1 トークン生成でロードを促す
        r = httpx.post(f"{base}/chat/completions",
                       json={"model": trade_model, "max_tokens": 1,
                             "messages": [{"role": "user", "content": "ping"}]},
                       timeout=120)
        r.raise_for_status()
    except (httpx.RequestError, httpx.HTTPStatusError) as e:
        print(f"警告: モデル '{trade_model}' の cold-load smoke に失敗しました ({e})。"
              "初回 Mission が timeout する可能性があります。")
        return

    trade_ctx = _fetch_model_ctx(base, trade_model)
    if trade_ctx is not None:
        print(f"llama-swap OK (model '{trade_model}' loaded, ctx {trade_ctx})")
    else:
        print(f"llama-swap OK (model '{trade_model}' loaded)")
```

- [ ] **Step 4: 成功を確認**

```bash
uv run pytest tests/test_service_app.py -k "check_llama_swap" -v
```

新規 7 件 + 既存 6 件、計 13 件すべて green。既存 6 件の出力文言 (`"OK" in out` / `"存在しません" in out` 等) が変わっていないことを確認する。

- [ ] **Step 5: 変異テスト**

```bash
find . -name __pycache__ -type d -not -path "./.venv/*" -exec rm -rf {} +
```

| # | 変異 | 殺すテスト |
|---|---|---|
| M1 | `_model_load_order` の `return [trade] if trade == improve else [improve, trade]` を常に `[improve, trade]` にする (重複除去を削除) | `test_check_llama_swap_same_model_calls_props_once` |
| M2 | `_model_load_order` の戻り値の順序を `[trade, improve]` に入れ替える | `test_check_llama_swap_different_models_improve_first_trade_last` |
| M3 | `_fetch_model_ctx` の `except (...)` から `httpx.HTTPStatusError` を外す (例外境界を広げる) | `test_check_llama_swap_props_http_failure_does_not_fail_init` (500 応答が `r.raise_for_status()` で例外化し `_check_llama_swap` まで伝播、テストが例外で落ちる) |
| M4 | `_fetch_model_ctx` の型検証 (`if not isinstance(n_ctx, int) or ...`) を削除し常に `n_ctx` を返す | `test_check_llama_swap_props_ctx_wrong_type_is_not_displayed` / `_missing_is_not_displayed` (`KeyError` 由来で None になるので実際には `_missing` は既存 except 節で守られる。型検証削除の主対象は wrong_type/bool) |
| M5 | `isinstance(n_ctx, bool)` の除外を削除する | `test_check_llama_swap_props_ctx_bool_is_not_displayed` |
| M6 | `_fetch_model_ctx` の `except (...)` を `except Exception:` に広げる | `test_check_llama_swap_unexpected_exception_in_props_is_not_swallowed` |
| M7 | `/v1` を除かず `f"{base}/props"` に戻す | 全 handler が `request.url.path == "/props"` を要求するため props call-count が 0 となり、`test_check_llama_swap_same_model_calls_props_once` ほかが red |

各変異を注入したら `grep -n "_model_load_order\|_fetch_model_ctx" src/agentic_fx/service.py` で改変を目視確認してから対象テストのみ実行し red を確認、revert して green に戻す。

- [ ] **Step 6: コミット**

```bash
git add src/agentic_fx/service.py tests/test_service_app.py
git commit -m "$(cat <<'EOF'
feat: init の llama-swap 接続確認に n_ctx 可視化を追加 (プラン9 Task5)

trade モデルしか見ていなかった _check_llama_swap を improve も含めた
重複除去リストに拡張。異なるモデルなら improve→trade の順で処理し、
trade を最後に置く (llama-swap の VRAM/TTL 次第で後発ロードが先発を
unload しうるため、init 終了時に取引判断モデルを hot にする)。
/props は OpenAI 互換 `/v1` ではなく llama-swap root から取得する。
取得失敗・形状不正は警告止まりで init を成功させ、想定外
例外だけは握らず落とす。n_ctx は表示のみで保存しない。

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
EOF
)"
```

---

### 束 A 自己レビュー

#### ① 検査点 21 個・受入条件 9 項目の対応表

**検査点 (spec §7 の表、全 21 個)**:

| # | 内容 | Task | Step | テスト名 |
|---|---|---|---|---|
| 1 | error.type によるコンテキスト判定 | 2 | 1/3 | `test_ctx_overflow_detected_by_error_type` (陽性) / `test_other_error_type_not_treated_as_ctx_overflow` (陰性・変異キラー) |
| 2 | reason に n_prompt_tokens が入る | 2 | 1/3 | `test_ctx_overflow_reason_includes_prompt_tokens` |
| 3 | reason に n_ctx が入る | 2 | 1/3 | `test_ctx_overflow_reason_includes_n_ctx` |
| 4 | reason に model が入る | 2 | 1/3 | `test_ctx_overflow_reason_includes_model` |
| 5 | safe_text による安全化・単一行化・長さ上限 | 2 | 1/3 | `test_reason_is_single_line` / `test_reason_length_is_capped` / `test_reason_uses_safe_text_to_strip_urls` |
| 6 | 超過でも status は "failed" のまま | 2 | 1/3 | `test_ctx_overflow_status_is_still_failed` |
| 7 | 非 400 (413/422) の同一 envelope も解析される | 2 | 1/3 | `test_non_400_status_with_same_envelope_is_also_parsed` |
| 8 | 本文が JSON でない/キー欠落 → 例外を出さず汎用文言 | 2 | 1/3 | `test_non_json_body_falls_back_to_generic_reason` / `test_missing_error_key_falls_back_to_generic_reason` |
| 9 | 本文が上限超過 → 汎用文言に退避 | 2 | 1/3 | `test_oversized_body_falls_back_to_generic_reason` |
| 10 | 子が result frame に reason を載せる | 3 | 1/3 | `test_main_puts_reason_in_result_frame_when_runner_sets_it` (trade) / `test_main_puts_reason_in_result_frame_for_improve_profile` (improve) |
| 11 | 親が frame の reason を MissionResult に載せる | 3 | 1/3 | `test_worker_runner_reads_reason_from_result_frame` |
| 12 | 未知キーを含む result frame でも read/dispatch できる | 3 | 1/3 | `test_worker_runner_tolerates_unknown_keys_in_result_frame` |
| 13 | trade の mission_failed activity に reason が載る | 4 | 1/3 | `test_mission_failed_activity_includes_reason_when_present` |
| 14 | trade の通知に reason が載る | 4 | 1/3 | `test_mission_failed_notification_includes_reason_when_present` |
| 15 | reflection_mission_failed activity が書かれ reason が載る | 4 | 1/3 | `test_reflection_mission_failed_activity_written_with_reason` |
| 16 | reflection の現挙動の固定 (2 回 run_pending → missions 2 本・failure activity 2 本・reflections 0 本) | 4 | 1/3 | `test_reflection_current_retry_behavior_is_pinned` |
| 17 | init: trade.model == improve.model なら /props は 1 回だけ | 5 | 1/3 | `test_check_llama_swap_same_model_calls_props_once` |
| 18 | init: 異なるなら improve→trade の順で trade が最後 | 5 | 1/3 | `test_check_llama_swap_different_models_improve_first_trade_last` |
| 19 | init: /props の HTTP 失敗でも init は成功 | 5 | 1/3 | `test_check_llama_swap_props_http_failure_does_not_fail_init` |
| 20 | init: /props の形状不正 (str/欠落/bool) でも init は成功し表示しない | 5 | 1/3 | `test_check_llama_swap_props_ctx_wrong_type_is_not_displayed` / `_missing_is_not_displayed` / `_bool_is_not_displayed` |
| 21 | init: 想定外例外は握らず init を落とす | 5 | 1/3 | `test_check_llama_swap_unexpected_exception_in_props_is_not_swallowed` |

**受入条件 (spec §8、全 9 項目)**:

| # | 内容 | 対応 |
|---|---|---|
| 1 | trade loop: 運用者が activity と通知だけで「超過だった」と分かる | Task 2 (検知・reason 構成) + Task 4 (出口)。検査点 1〜6, 13, 14 が実測 |
| 2 | reflection loop: reflection_mission_failed activity が残る (通知は出さない) | Task 4。検査点 15。「通知を出さない」は `ReflectionCycle` が `Notifier` を保持しない構造で担保 (`_cycle` フィクスチャに notifier 引数が無い) |
| 3 | 表示される n_prompt_tokens/n_ctx がサーバーの応答そのまま | Task 2。検査点 2, 3 (再計算せず `err.get(...)` をそのまま埋め込む実装で担保) |
| 4 | 非 400 の status でも同一 envelope なら診断が拾える | Task 2。検査点 7 |
| 5 | reason は単一行・上限内・安全化済みであり、応答本文そのものはどこにも保存されない | Task 2。検査点 5, 8, 9 + `test_response_body_not_stored_in_transcript` (補強) |
| 6 | worker profile (子プロセス実行) でも reason が親に届く | Task 3。検査点 10, 11, 12 |
| 7 | init が実際の n_ctx を表示し、取得に失敗しても init は成功する。想定外例外は握らない | Task 5。検査点 17〜21 |
| 8 | 既存テストが 1 本も壊れない | 各 Task の Step 4 で個別確認。特に Task 2 (`test_local_runner_errors.py` 12 件)・Task 3 (`test_worker_runner.py` 既存分)・Task 4 (`test_trade_loop.py`/`test_reflection_cycle.py` 既存分)・Task 5 (`test_service_app.py` 既存 6 件) を明記した |
| 9 | reflection の無制限再試行・starvation・missions 行の増大が独立課題として起票されている (本 task では直さないが、現挙動が回帰テストで固定されている) | 「起票」は設計書 §4.5 (codex I3 の節) が既に満たしている (本書での追加文書化は無い、Task 4 の Interfaces 節に明記)。「回帰テストで固定」は Task 4 検査点 16 (`test_reflection_current_retry_behavior_is_pinned`) が担う |

#### ② プレースホルダの不在確認

本書内を再走査し、「TBD」「適切に」「同様に」「以下同様」「(省略)」に類する記述が無いことを確認した。Task 1〜5 はそれぞれ Files/Interfaces/Step 1〜6 を単独で完結させて書いており、他 Task を参照する箇所は「Task N の a/b (依存関係)」のような固有名詞つき参照のみで、内容の省略はない。

#### ③ Task 間の型・関数名の一致確認

- `MissionResult.reason: str | None = None` (Task 1 が produce) — Task 2 (`_finish(..., reason=...)` → `MissionResult(..., reason=reason)`)、Task 3 (`result.reason` を読んで frame に載せる／frame から読んで `MissionResult(..., reason=reason)` に渡す)、Task 4 (`result.reason` を activity/通知の文字列に埋め込む) のすべてで同一のキーワード引数名 `reason` として一貫させた
- `AgentRunner.__doc__` の規範文言 (Task 1) は Task 2 の実装方針 (単一行・上限・安全化・生本文を入れない) と文言レベルで一致させた
- `mission_worker.py` の result frame キー `"reason"` (Task 3 produce) と `worker_runner.py` の `payload.get("reason")` (Task 3 consume) は同一プロセス内の Task 3 で閉じており、他 Task には露出しない
- Task 4 が読む `result.reason` は Task 2/3 が設定する値と同じ型 (`str | None`) で、Task 4 側は追加の変換をしない (Interfaces 節に明記済み)
- Task 5 は他 Task と型を共有しない (独立 leaf task)。`_model_load_order(settings) -> list[str]` と `_fetch_model_ctx(base: str, model: str, timeout: float) -> int | None` は `service.py` 内で完結する private 関数として定義した
  - **`timeout` は 2 周目レビューで追加した必須引数** (3 周目レビューで本文との乖離を検出し追記)。`/props` はモデルをロードさせるため、呼び出し側が cold を踏むか hot を踏むかで必要な予算が 4 桁違う (実機 35B で cold 13.86s / hot 0.0005s)。既定値を置くと第 3 の呼び出し点が黙って cold に 5 秒を割り当てて同じ欠陥が再発するため、**既定値を置かない**

#### ④ カバーできなかった項目 (正直に列挙)

- spec §4.3 が求める「プラン 9 の ClaudeRunner task の blocking チェックリストに契約テストを追加する」は、**本プランには ClaudeRunner を実装する task が存在しない** (親骨格の Global Constraints: 「本プランは ClaudeRunner を実装しない (プラン 10)」)。したがって追加すべきチェックリスト自体が本プラン内に無く、Task 1 の docstring 末尾にプラン 10 実装者への申し送り文を書くにとどめた (「プラン 10 で ClaudeRunner を実装する task は、この docstring の規範を満たす契約テストをブロッキングチェックリストに含めること」)。プラン 10 の計画時に実際のチェックリスト項目として反映される必要がある — 本書では完結できない
- Task 4 の CP16 テストで固定した「現挙動」(無制限再試行・starvation) 自体の是正は、spec 自身が「本 task では直さない」と明記した範囲外の作業であり、意図的に未実装のまま残した (検査点 16 として回帰固定のみ実施)


---

## プラン 9 束 B 詳細 step: snapshot gather の deadline (Task 6〜7)

**親文書**: `docs/superpowers/plans/2026-08-11-phase2-9-foundation.md` (骨格 — Global Constraints / プラン規約 / Interfaces / 既知事実は本書では再記述しない)
**設計の正**: `docs/superpowers/specs/2026-08-11-gather-deadline-design.md` の**冒頭「確定仕様」節のみ**。同文書の「以下は履歴 (初稿本文)」以降は誤り (中核の論証が崩れている) を含む経緯であり、実装の根拠にしない。

本書は束 B (Task 6〜7) の実装 step のみを扱う。束 A/C/D/E は別書。

### 束内の実行順序・前提

- **束 B は束 C 完了後に着手する** (親骨格の実行グラフ)。理由: Task 7 が触る `src/agentic_fx/datafeed/price_provider.py` の `to_account_rate` (:411-464) は、束 C の Task 9/10 が触る `_cached_bars`/`get_bars`/`_chain` (:139-247) と**同一ファイル**にあるため、並行 worktree で進めると diff 競合が起きる。関数自体は disjoint (行範囲が重ならない) だが、ファイル単位の worktree マージでは無条件に安全とは言えない。詳細は本書末尾「束 B 自己レビュー」③。
- Task 6 → Task 7 の順で直列に進める (Task 7 は Task 6 が作る `Executor.monotonic_fn`/`_make_deadline_checker`/gather 内のローカル変数 `check` を再利用する)。
- **Task 17 (`E` 束) は「束 B 完了後」に依存する** — `core/executor.py` の同一ファイル競合を避けるため。Task 17 実装者への申し送り: 本書 Task 6/7 が `open_from_snapshot`/`close_from_snapshot` の commit-core 鮮度検証ロジックを 1 文字も変えていないこと、`trade_loop.py` の `snapshot_error` 経路 (commit-pre の `except Exception`) がそのまま deadline 例外も飲み込むため Task 17 の `reject_category` は既存の execution 系分類 (`"execution"`) がそのまま適用できることを確認済み (新しい分類軸は不要)。

---

### Task 6: monotonic 注入 + gather deadline (OPEN/CLOSE 両方)

**Files:**
- Modify: `src/agentic_fx/core/executor.py:1-9` (import に `time` を追加)、`:143-174` (`Executor.__init__` — `monotonic_fn` 注入点)、`:522-547` (`gather_open_snapshot`)、`:724-739` (`gather_close_snapshot`)。新設: `Executor._make_deadline_checker` (`gather_open_snapshot` の直前に配置)
- Modify: `tests/loops/test_trade_loop.py:1-15` (import に `time` を追加)、`:33-51` (`_loop` ヘルパに `monotonic_fn` 引数を追加)
- Create: `tests/core/test_executor_gather_deadline.py` (Task 6 の全テスト。Task 7 でこのファイルに追記する)
- Modify: `tests/loops/test_trade_loop_phases.py` (末尾に出口ピンの統合テストを追記)
- Test 実行対象 (既存回帰確認): `tests/core/test_executor_snapshot.py`、`tests/loops/test_trade_loop.py`、`tests/loops/test_trade_loop_phases.py`、`tests/test_e2e_worker_isolation.py`

**Interfaces:**
- Consumes: `agentic_fx.datafeed.health.DataUnhealthy` (既存例外型。新しい配管は作らない — `trade_loop.py` の commit-pre `except Exception` (:248, :267) がそのまま捕捉する)。`settings.worker.snapshot_max_age_sec` (既存 config キー、既定 10.0 — 新キーは作らない)
- Produces (Task 7 が consume):
  ```python
  # src/agentic_fx/core/executor.py — Executor
  def __init__(self, *, ..., monotonic_fn: Callable[[], float] = time.monotonic,
               ...) -> None: ...
  # self.monotonic_fn: Callable[[], float]  既定 time.monotonic。
  # 経過測定専用 — self.clock は captured_at にのみ使う。

  def _make_deadline_checker(self, budget: float) -> Callable[[str], None]:
      """呼ぶたびに現在の self.monotonic_fn() を測り、生成時からの経過が
      budget を超えていたら DataUnhealthy を送出する checker を返す。
      比較は `>` (等号は受理側)。"""
  ```
  `gather_open_snapshot`/`gather_close_snapshot` のシグネチャ・戻り値型は**不変**。内部にローカル変数 `check = self._make_deadline_checker(budget)` を持つ (Task 7 がこの `check` を `cycle_rate_fn`/`resolve_close_rate` へ渡す配線を追加する)。

#### 現状 (`src/agentic_fx/core/executor.py:143-174` — `Executor.__init__`)

```python
    def __init__(self, *, conn: sqlite3.Connection, broker: PaperBroker,
                 settings: Settings, state_store: StateStore,
                 activity: ActivityLog, notifier: Notifier, clock: Clock,
                 quote_fn: Callable[[str], Quote],
                 spec_fn: Callable[[str], InstrumentSpec],
                 rate_fn: Callable[[str, str, datetime], ConversionRate],
                 ) -> None:
        self.conn = conn
        self.broker = broker
        self.settings = settings
        self.state = state_store
        self.activity = activity
        self.notifier = notifier
        self.clock = clock
        self.quote_fn = quote_fn
        self.spec_fn = spec_fn
        # 通貨 1 単位 = 口座通貨いくらか (設計書 §5)。quote_fn/spec_fn と同じ
        # 注入点の作法 (PriceProvider.to_account_rate を呼び出し側が
        # account_currency/max_skew_min を束縛して渡す想定)。
        self.rate_fn = rate_fn
        # 以下も既存 __init__ の一部であり、置換時に必ず温存する。省略不可。
        self._last_good_rate: dict[tuple[str, str], ConversionRate] = {}
        self._deferred_notifications: list[str] | None = None
```

#### 現状 (`src/agentic_fx/core/executor.py:522-547` — `gather_open_snapshot`)

```python
    def gather_open_snapshot(self, intent: TradeIntent, *,
                             exposure_pairs: list[str]) -> ExecutionSnapshot:
        """commit-pre 相専用 (設計書 §3.1) — **core_lock を保持しない状態
        で呼ぶこと**。Risk Gate 評価に要る全外部取得 (quote + 全 exposure
        pair の instrument spec + 全 exposure 通貨の換算レート) を 1 回で
        完了させ、timestamp 付きスナップショットにする。

        `exposure_pairs` は呼び出し元 (commit-pre 相) が `conn_supervisor`
        (lock 外の読取専用接続) から読んだ既存 exposure の pair 一覧。
        """
        now = self.clock.now()
        quote = self.quote_fn(intent.pair)
        spec = self.spec_fn(intent.pair)
        cycle_rate = self.cycle_rate_fn(now)
        specs_by_pair: dict = {intent.pair: spec}
        currencies: set = {spec.quote_currency, spec.base_currency}
        for pair in exposure_pairs:
            pair_spec = self.spec_fn(pair)
            specs_by_pair[pair] = pair_spec
            currencies.add(pair_spec.quote_currency)
            currencies.add(pair_spec.base_currency)
        # A3: rates の構築順を安定化 (PYTHONHASHSEED 依存を避ける)
        rates = {ccy: cycle_rate(ccy) for ccy in sorted(currencies)}
        return ExecutionSnapshot(quote=quote, spec=spec,
                                 specs_by_pair=specs_by_pair, rates=rates,
                                 captured_at=now)
```

#### 現状 (`src/agentic_fx/core/executor.py:724-739` — `gather_close_snapshot`)

```python
    def gather_close_snapshot(self, row: dict) -> CloseSnapshot:
        """commit-pre 相専用 (裁定書 F-1 / CR-2 / P8-01) — **core_lock
        非保持で呼ぶこと**。CLOSE 実行に要る quote (成行価格) +
        instrument spec + 換算レートを 1 回で取得し timestamp 付き
        スナップショットにする。`row` は呼び出し元 (Task 15 の commit-pre
        相) が `conn_supervisor` (lock 外の読取専用接続) から読んだ現在の
        order 行 (`pair`/`direction` を参照するだけ)。"""
        now = self.clock.now()
        pair = row["pair"]
        quote = self.quote_fn(pair)
        price = quote.bid if row["direction"] == "long" else quote.ask
        spec = self.spec_fn(pair)
        rate, degraded = self.resolve_close_rate(spec.quote_currency, now)
        return CloseSnapshot(order_id=row["id"], pair=pair, price=price,
                             spec=spec, rate=rate,
                             rate_degraded=degraded, captured_at=now)
```

- [ ] **Step 1: `tests/core/test_executor_gather_deadline.py` を新規作成し、`_make_deadline_checker` の失敗するテストを書く**

```python
"""プラン9 束B Task 6/7: gather deadline (executor.py)。
設計の正: docs/superpowers/specs/2026-08-11-gather-deadline-design.md の
「確定仕様」節。検査点ごとのテーブル駆動 + 全 stub の呼び出し列を assert
する (境界は直前・等号・直後の 3 点)。"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from agentic_fx.activity import ActivityLog
from agentic_fx.config import load_settings
from agentic_fx.core.accounting import record_snapshot
from agentic_fx.core.contracts import (
    ConversionRate, FixedClock, InstrumentSpec, Origin, OrderStatus as S,
    Quote, TradeIntent,
)
from agentic_fx.core.executor import Executor
from agentic_fx.core.notifier import Notifier
from agentic_fx.core.paper_broker import PaperBroker
from agentic_fx.datafeed.health import DataUnhealthy
from agentic_fx.store import orders
from agentic_fx.store.db import connect, init_db
from agentic_fx.store.state import StateStore

NOW = datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc)
SETTINGS = load_settings(
    Path(__file__).resolve().parents[2] / "config" / "settings.yaml.example")
BUDGET = SETTINGS.worker.snapshot_max_age_sec  # 10.0 (既定 — 新キーは作らない)

SPECS = {
    "USDJPY": InstrumentSpec(symbol="USDJPY", pip_size=0.01, min_lot=0.01,
                             max_lot=50.0, lot_step=0.01, contract_size=100_000,
                             base_currency="USD", quote_currency="JPY"),
    "EURUSD": InstrumentSpec(symbol="EURUSD", pip_size=0.01, min_lot=0.01,
                             max_lot=50.0, lot_step=0.01, contract_size=100_000,
                             base_currency="EUR", quote_currency="USD"),
    "GBPUSD": InstrumentSpec(symbol="GBPUSD", pip_size=0.01, min_lot=0.01,
                             max_lot=50.0, lot_step=0.01, contract_size=100_000,
                             base_currency="GBP", quote_currency="USD"),
}
QUOTE = Quote("USDJPY", 148.49, 148.51, NOW, "test")


class _Mono:
    """テスト用可変 monotonic フェイク。缶詰の呼び出し順シーケンスにしない
    (呼び出し回数が実装の一部を変えるだけで壊れるため) — leg スタブ自身が
    `t` を進める。"""

    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def _make_executor(tmp_path, *, monotonic_fn, quote_fn, spec_fn,
                   rate_fn) -> Executor:
    tmp_path.mkdir(parents=True, exist_ok=True)
    conn = connect(tmp_path / "t.db")
    init_db(conn)
    record_snapshot(conn, now=NOW, balance=1_000_000, equity=1_000_000)
    return Executor(
        conn=conn, broker=PaperBroker(conn, SETTINGS, FixedClock(NOW)),
        settings=SETTINGS, state_store=StateStore(tmp_path / "state.json"),
        activity=ActivityLog(tmp_path / "activity.log"),
        notifier=Notifier(enabled=False, webhook_url=None),
        clock=FixedClock(NOW), monotonic_fn=monotonic_fn,
        quote_fn=quote_fn, spec_fn=spec_fn, rate_fn=rate_fn)


def _recording_stubs(mono: _Mono, leg_costs: dict[str, float]):
    """quote_fn/spec_fn/rate_fn の記録型スタブ一式。各呼び出しは `calls` に
    追記し、`leg_costs` (呼び出しキー→消費秒数) ぶんだけ `mono.t` を進めて
    から返る (= その脚の取得に要した時間を模す)。"""
    calls: list[str] = []

    def quote_fn(pair: str) -> Quote:
        calls.append(f"quote:{pair}")
        mono.t += leg_costs.get(f"quote:{pair}", 0.0)
        return QUOTE

    def spec_fn(pair: str) -> InstrumentSpec:
        calls.append(f"spec:{pair}")
        mono.t += leg_costs.get(f"spec:{pair}", 0.0)
        return SPECS[pair]

    def rate_fn(ccy: str, account_ccy: str, now, **_ignored) -> ConversionRate:
        # **_ignored: Task 7 が rate_fn に keyword-only `deadline_check` を
        # 追加する (cycle_rate_fn/resolve_close_rate 経由)。Task 6 の
        # 呼び出しは kwarg を渡さないためここでは常に空。
        calls.append(f"rate:{ccy}")
        mono.t += leg_costs.get(f"rate:{ccy}", 0.0)
        return ConversionRate(1.0, ccy, account_ccy, (now,))

    return calls, quote_fn, spec_fn, rate_fn


def _open_intent(pair: str = "USDJPY") -> TradeIntent:
    return TradeIntent.from_llm_dict(
        {"action": "open", "pair": pair, "direction": "long",
         "entry_type": "market", "horizon": "day", "limit_price": None,
         "expires_in": None, "stop_loss": 148.00, "take_profit": 149.60,
         "reasoning": "t"}, origin=Origin.SCHEDULER)


def _insert_open_order(conn, pair: str = "USDJPY", direction: str = "long") -> dict:
    oid = orders.insert(
        conn, pair=pair, direction=direction, entry_type="market",
        horizon="day", status=S.OPEN, now=NOW,
        quantity=0.1, remaining_quantity=0.0, requested_price=148.20,
        avg_fill_price=148.20, filled_quantity=0.1, stop_loss=147.80,
        take_profit=149.00, filled_at=NOW.isoformat())
    return orders.get(conn, oid)


# ---- Task 6: _make_deadline_checker (低レベル) -----------------------------

def test_deadline_checker_accepts_exact_budget_boundary(tmp_path):
    """比較は `>` (等号は受理側) — 予算ちょうど消費しても発火しない。"""
    mono = _Mono()
    ex = _make_executor(tmp_path, monotonic_fn=mono,
                        quote_fn=lambda p: QUOTE, spec_fn=lambda p: SPECS[p],
                        rate_fn=lambda c, a, n, **k: ConversionRate(1.0, c, a, (n,)))
    check = ex._make_deadline_checker(BUDGET)
    mono.t = BUDGET  # ちょうど予算を使い切った
    check("leg")  # raise しないこと


def test_deadline_checker_raises_just_over_budget(tmp_path):
    mono = _Mono()
    ex = _make_executor(tmp_path, monotonic_fn=mono,
                        quote_fn=lambda p: QUOTE, spec_fn=lambda p: SPECS[p],
                        rate_fn=lambda c, a, n, **k: ConversionRate(1.0, c, a, (n,)))
    check = ex._make_deadline_checker(BUDGET)
    mono.t = BUDGET + 0.1
    with pytest.raises(DataUnhealthy, match="deadline exceeded"):
        check("leg:x")
```

- [ ] **Step 2: テストを実行し、`_make_deadline_checker`/`monotonic_fn` が存在せず落ちることを確認する**

Run: `uv run pytest tests/core/test_executor_gather_deadline.py -v`
Expected: `test_deadline_checker_accepts_exact_budget_boundary`/`test_deadline_checker_raises_just_over_budget` の 2 件が `TypeError: Executor.__init__() got an unexpected keyword argument 'monotonic_fn'` (または `AttributeError: 'Executor' object has no attribute '_make_deadline_checker'`) で FAIL する。

- [ ] **Step 3: `monotonic_fn` 注入点と `_make_deadline_checker` を実装する**

`src/agentic_fx/core/executor.py:1-9` の import に `time` を追加する:

```python
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable
```

`Executor.__init__` (:143-174) を書き換える (`clock: Clock,` の直後に `monotonic_fn` を追加。他の代入行は無変更):

```python
    def __init__(self, *, conn: sqlite3.Connection, broker: PaperBroker,
                 settings: Settings, state_store: StateStore,
                 activity: ActivityLog, notifier: Notifier, clock: Clock,
                 monotonic_fn: Callable[[], float] = time.monotonic,
                 quote_fn: Callable[[str], Quote],
                 spec_fn: Callable[[str], InstrumentSpec],
                 rate_fn: Callable[[str, str, datetime], ConversionRate],
                 ) -> None:
        self.conn = conn
        self.broker = broker
        self.settings = settings
        self.state = state_store
        self.activity = activity
        self.notifier = notifier
        self.clock = clock
        # Task 6 (プラン9 束B): gather deadline の経過測定専用。
        # `self.clock` は captured_at にのみ使う — FixedClock/ReplayClock
        # は進まないため clock 基準の deadline は永久に到来しない
        # (spec 改稿 C1)。
        self.monotonic_fn = monotonic_fn
        self.quote_fn = quote_fn
        self.spec_fn = spec_fn
        # 通貨 1 単位 = 口座通貨いくらか (設計書 §5)。quote_fn/spec_fn と同じ
        # 注入点の作法 (PriceProvider.to_account_rate を呼び出し側が
        # account_currency/max_skew_min を束縛して渡す想定)。
        self.rate_fn = rate_fn
        # CLOSE の fail-soft と commit-post 通知 drain が依存する既存状態。
        # __init__ 置換で落としてはならない。
        self._last_good_rate: dict[tuple[str, str], ConversionRate] = {}
        self._deferred_notifications: list[str] | None = None
```

`gather_open_snapshot` の直前 (:521 の空行の後、`def gather_open_snapshot` の前) に新設する:

```python
    def _make_deadline_checker(self, budget: float) -> Callable[[str], None]:
        """1 回の gather (`gather_open_snapshot`/`gather_close_snapshot`)
        専用の deadline checker を作る (Task 6, プラン9 束B — 設計:
        `docs/superpowers/specs/2026-08-11-gather-deadline-design.md`
        確定仕様 #2/#3)。

        - 予算は呼び出し元が渡す (`settings.worker.snapshot_max_age_sec`
          を流用。新 config キーは作らない)。
        - 経過は `self.monotonic_fn()` (既定 `time.monotonic`) で測る。
          `self.clock` は `captured_at` にのみ使う — `FixedClock`/
          `ReplayClock` は進まないため clock 基準の deadline は永久に
          到来しない (改稿 C1)。
        - 比較は `>` (等号は受理側 — commit-core の
          `age_sec > max_snapshot_age_sec` (`open_from_snapshot`/
          `close_from_snapshot`) と揃える)。
        - 戻り値の `check(leg)` は次の脚を呼ぶ**前**に呼ぶこと。予算超過
          なら `DataUnhealthy` を送出する。文言は commit-core の
          `"execution/close snapshot is stale"` と区別できるよう
          "deadline exceeded" を使う (trade_loop.py の commit-pre が
          既存の `except Exception` で捕捉する — 新しい配管は作らない)。
        - Task 7 でこの checker を `cycle_rate_fn`/`resolve_close_rate`
          経由で `PriceProvider.to_account_rate` の `for spec in legs`
          まで伝播させる (改稿 C2)。実行中の 1 脚は中断できない —
          deadline が保証するのは「次の脚に進まないこと」のみ (確定
          仕様 #9)。
        """
        start = self.monotonic_fn()

        def check(leg: str) -> None:
            elapsed = self.monotonic_fn() - start
            if elapsed > budget:
                raise DataUnhealthy(
                    f"snapshot gather deadline exceeded: {elapsed:.1f}s "
                    f"elapsed (budget {budget:.1f}s) — aborting before {leg}")
        return check
```

- [ ] **Step 4: テストを実行し green を確認する**

Run: `uv run pytest tests/core/test_executor_gather_deadline.py -v`
Expected: 2 件とも PASS。

- [ ] **Step 5: `gather_open_snapshot` の OPEN 側テーブル駆動テストを追記する**

`tests/core/test_executor_gather_deadline.py` の末尾に追記する:

```python
# ---- Task 6: gather_open_snapshot (OPEN 側) --------------------------------

def test_gather_open_snapshot_completes_when_within_budget(tmp_path):
    """非損失性のピン (確定仕様テスト観点 1): 全脚が予算内なら deadline は
    発火せず、全 stub が呼ばれて snapshot が完成する。"""
    mono = _Mono()
    calls, quote_fn, spec_fn, rate_fn = _recording_stubs(mono, leg_costs={})
    ex = _make_executor(tmp_path, monotonic_fn=mono, quote_fn=quote_fn,
                        spec_fn=spec_fn, rate_fn=rate_fn)
    intent = _open_intent("USDJPY")

    snapshot = ex.gather_open_snapshot(intent, exposure_pairs=[])

    assert calls == ["quote:USDJPY", "spec:USDJPY", "rate:JPY", "rate:USD"]
    assert snapshot.specs_by_pair == {"USDJPY": SPECS["USDJPY"]}
    assert set(snapshot.rates) == {"JPY", "USD"}


def test_gather_open_snapshot_boundary_exact_budget_still_succeeds(tmp_path):
    """境界 (等号): quote_fn の取得に予算ちょうど掛かっても、比較は `>`
    なので次の脚 (spec_fn) は打ち切られず全体が完成する
    (`>=` へ変異すると red になる)。"""
    mono = _Mono()
    calls, quote_fn, spec_fn, rate_fn = _recording_stubs(
        mono, leg_costs={"quote:USDJPY": BUDGET})
    ex = _make_executor(tmp_path, monotonic_fn=mono, quote_fn=quote_fn,
                        spec_fn=spec_fn, rate_fn=rate_fn)
    intent = _open_intent("USDJPY")

    snapshot = ex.gather_open_snapshot(intent, exposure_pairs=[])

    assert calls == ["quote:USDJPY", "spec:USDJPY", "rate:JPY", "rate:USD"]
    assert snapshot is not None


def test_gather_open_snapshot_aborts_before_spec_of_intent_pair(tmp_path):
    """打ち切りのピン (OPEN-1, 検査点「直後」): quote_fn だけで予算超過
    → spec_fn(intent.pair) が呼ばれない。"""
    mono = _Mono()
    calls, quote_fn, spec_fn, rate_fn = _recording_stubs(
        mono, leg_costs={"quote:USDJPY": BUDGET + 0.1})
    ex = _make_executor(tmp_path, monotonic_fn=mono, quote_fn=quote_fn,
                        spec_fn=spec_fn, rate_fn=rate_fn)
    intent = _open_intent("USDJPY")

    with pytest.raises(DataUnhealthy, match="deadline exceeded"):
        ex.gather_open_snapshot(intent, exposure_pairs=[])

    assert calls == ["quote:USDJPY"]


def test_gather_open_snapshot_aborts_before_second_exposure_pair_spec(tmp_path):
    """打ち切りのピン (OPEN-2): exposure_pairs 2 件で、1 件目 (EURUSD) の
    spec_fn 取得で予算超過 → 2 件目 (GBPUSD) の spec_fn が呼ばれない。
    1 件だけだと「ループの前で 1 回だけ検査する」誤実装でも green に
    なってしまうため 2 件必須 (advisor 指摘)。"""
    mono = _Mono()
    calls, quote_fn, spec_fn, rate_fn = _recording_stubs(
        mono, leg_costs={"spec:EURUSD": BUDGET + 0.1})
    ex = _make_executor(tmp_path, monotonic_fn=mono, quote_fn=quote_fn,
                        spec_fn=spec_fn, rate_fn=rate_fn)
    intent = _open_intent("USDJPY")

    with pytest.raises(DataUnhealthy, match="deadline exceeded"):
        ex.gather_open_snapshot(intent, exposure_pairs=["EURUSD", "GBPUSD"])

    assert calls == ["quote:USDJPY", "spec:USDJPY", "spec:EURUSD"]


def test_gather_open_snapshot_aborts_before_second_currency_rate(tmp_path):
    """打ち切りのピン (OPEN-3): 通貨 2 件 (JPY/USD, USDJPY 単体の
    quote/base から自然に出る) で、1 件目 (JPY) の rate_fn 取得で予算超過
    → 2 件目 (USD) の rate_fn が呼ばれない。"""
    mono = _Mono()
    calls, quote_fn, spec_fn, rate_fn = _recording_stubs(
        mono, leg_costs={"rate:JPY": BUDGET + 0.1})
    ex = _make_executor(tmp_path, monotonic_fn=mono, quote_fn=quote_fn,
                        spec_fn=spec_fn, rate_fn=rate_fn)
    intent = _open_intent("USDJPY")

    with pytest.raises(DataUnhealthy, match="deadline exceeded"):
        ex.gather_open_snapshot(intent, exposure_pairs=[])

    assert calls == ["quote:USDJPY", "spec:USDJPY", "rate:JPY"]


def test_gather_open_snapshot_deadline_independent_of_fixed_clock(tmp_path):
    """論理時計からの独立のピン (確定仕様テスト観点 3): `clock` は
    FixedClock (captured_at は常に NOW で不変) のまま、monotonic フェイク
    だけを進めて deadline が発火することを確認する。経過測定が誤って
    `self.clock` に戻る変異が入ると、captured_at が動かないため elapsed
    は恒等的に 0 になり、このテストは raise を観測できず red になる。"""
    mono = _Mono()
    calls, quote_fn, spec_fn, rate_fn = _recording_stubs(
        mono, leg_costs={"quote:USDJPY": BUDGET + 0.1})
    ex = _make_executor(tmp_path, monotonic_fn=mono, quote_fn=quote_fn,
                        spec_fn=spec_fn, rate_fn=rate_fn)
    intent = _open_intent("USDJPY")

    with pytest.raises(DataUnhealthy):
        ex.gather_open_snapshot(intent, exposure_pairs=[])

    # clock 自体は 1 秒たりとも進んでいない (captured_at 由来の deadline
    # なら絶対に発火しないはずの状況で発火したことの反証材料)
    assert ex.clock.now() == NOW
```

- [ ] **Step 6: テストを実行し、OPEN-1/2/3 のキル用テストが FAIL することを確認する**

Run: `uv run pytest tests/core/test_executor_gather_deadline.py -v`
Expected: `test_gather_open_snapshot_completes_when_within_budget` と `test_gather_open_snapshot_boundary_exact_budget_still_succeeds` の 2 件は既存の `gather_open_snapshot` (deadline 検査なし) でも偶然 PASS する (非損失性・境界は「発火しない」ことを確認するテストであり、まだ検査自体が無くても矛盾しないため — 回帰ピンとして機能する)。残り 4 件 (`..._aborts_before_spec_of_intent_pair` / `..._aborts_before_second_exposure_pair_spec` / `..._aborts_before_second_currency_rate` / `..._deadline_independent_of_fixed_clock`) は `pytest.raises` の中で例外が発生せず `DID NOT RAISE` で FAIL する。

- [ ] **Step 7: `gather_open_snapshot` に deadline 検査点を実装する**

`gather_open_snapshot` (:522-547) を次で置き換える:

```python
    def gather_open_snapshot(self, intent: TradeIntent, *,
                             exposure_pairs: list[str]) -> ExecutionSnapshot:
        """commit-pre 相専用 (設計書 §3.1) — **core_lock を保持しない状態
        で呼ぶこと**。Risk Gate 評価に要る全外部取得 (quote + 全 exposure
        pair の instrument spec + 全 exposure 通貨の換算レート) を 1 回で
        完了させ、timestamp 付きスナップショットにする。

        `exposure_pairs` は呼び出し元 (commit-pre 相) が `conn_supervisor`
        (lock 外の読取専用接続) から読んだ既存 exposure の pair 一覧。

        **Task 6 (プラン9 束B): gather deadline。** 予算は
        `settings.worker.snapshot_max_age_sec` を流用 (新 config キーは
        作らない)。経過は `self.monotonic_fn()` で測る (`self.clock` は
        `captured_at` にのみ使う)。比較は `>` (等号は受理側 — commit-core
        の `age_sec > max_snapshot_age_sec` と揃える)。予算超過は
        `DataUnhealthy` (次の脚を呼ぶ前に打ち切る — 実行中の 1 脚は
        中断できない)。
        """
        now = self.clock.now()
        budget = self.settings.worker.snapshot_max_age_sec
        check = self._make_deadline_checker(budget)
        quote = self.quote_fn(intent.pair)
        check(f"spec:{intent.pair}")
        spec = self.spec_fn(intent.pair)
        cycle_rate = self.cycle_rate_fn(now)
        specs_by_pair: dict = {intent.pair: spec}
        currencies: set = {spec.quote_currency, spec.base_currency}
        for pair in exposure_pairs:
            check(f"spec:{pair}")
            pair_spec = self.spec_fn(pair)
            specs_by_pair[pair] = pair_spec
            currencies.add(pair_spec.quote_currency)
            currencies.add(pair_spec.base_currency)
        # A3: rates の構築順を安定化 (PYTHONHASHSEED 依存を避ける)。dict
        # comprehension だった箇所を for ループに変える (Task 6: 通貨
        # ごとの deadline 検査を挟むため — ソート順を含め挙動は従前と同一)。
        rates: dict = {}
        for ccy in sorted(currencies):
            check(f"rate:{ccy}")
            rates[ccy] = cycle_rate(ccy)
        return ExecutionSnapshot(quote=quote, spec=spec,
                                 specs_by_pair=specs_by_pair, rates=rates,
                                 captured_at=now)
```

- [ ] **Step 8: テストを実行し OPEN 側 6 件が全件 green になることを確認する**

Run: `uv run pytest tests/core/test_executor_gather_deadline.py -v`
Expected: 全件 PASS (Step 1〜4 の 2 件 + Step 5 の 6 件 = 8 件)。

- [ ] **Step 9: `gather_close_snapshot` の CLOSE 側テーブル駆動テストを追記する**

`tests/core/test_executor_gather_deadline.py` の末尾に追記する:

```python
# ---- Task 6: gather_close_snapshot (CLOSE 側 — OPEN と対称) -----------------

def test_gather_close_snapshot_completes_when_within_budget(tmp_path):
    mono = _Mono()
    calls, quote_fn, spec_fn, rate_fn = _recording_stubs(mono, leg_costs={})
    ex = _make_executor(tmp_path, monotonic_fn=mono, quote_fn=quote_fn,
                        spec_fn=spec_fn, rate_fn=rate_fn)
    row = _insert_open_order(ex.conn, pair="USDJPY")

    snapshot = ex.gather_close_snapshot(row)

    assert calls == ["quote:USDJPY", "spec:USDJPY", "rate:JPY"]
    assert snapshot.price == QUOTE.bid


def test_gather_close_snapshot_boundary_exact_budget_still_succeeds(tmp_path):
    mono = _Mono()
    calls, quote_fn, spec_fn, rate_fn = _recording_stubs(
        mono, leg_costs={"quote:USDJPY": BUDGET})
    ex = _make_executor(tmp_path, monotonic_fn=mono, quote_fn=quote_fn,
                        spec_fn=spec_fn, rate_fn=rate_fn)
    row = _insert_open_order(ex.conn, pair="USDJPY")

    snapshot = ex.gather_close_snapshot(row)

    assert calls == ["quote:USDJPY", "spec:USDJPY", "rate:JPY"]
    assert snapshot is not None


def test_gather_close_snapshot_aborts_before_spec(tmp_path):
    """CLOSE 対称のピン (CLOSE-1, 確定仕様テスト観点 5): quote_fn で予算
    超過 → spec_fn(pair) が呼ばれない。初稿の「CLOSE は打ち切らない」
    (非対称) は改稿 I1 で誤りと判明し撤回された — ここは初稿を反転させる。
    """
    mono = _Mono()
    calls, quote_fn, spec_fn, rate_fn = _recording_stubs(
        mono, leg_costs={"quote:USDJPY": BUDGET + 0.1})
    ex = _make_executor(tmp_path, monotonic_fn=mono, quote_fn=quote_fn,
                        spec_fn=spec_fn, rate_fn=rate_fn)
    row = _insert_open_order(ex.conn, pair="USDJPY")

    with pytest.raises(DataUnhealthy, match="deadline exceeded"):
        ex.gather_close_snapshot(row)

    assert calls == ["quote:USDJPY"]


def test_gather_close_snapshot_aborts_before_resolve_close_rate(tmp_path):
    """CLOSE 対称のピン (CLOSE-2): spec_fn で予算超過 →
    resolve_close_rate 経由の rate_fn が呼ばれない。"""
    mono = _Mono()
    calls, quote_fn, spec_fn, rate_fn = _recording_stubs(
        mono, leg_costs={"spec:USDJPY": BUDGET + 0.1})
    ex = _make_executor(tmp_path, monotonic_fn=mono, quote_fn=quote_fn,
                        spec_fn=spec_fn, rate_fn=rate_fn)
    row = _insert_open_order(ex.conn, pair="USDJPY")

    with pytest.raises(DataUnhealthy, match="deadline exceeded"):
        ex.gather_close_snapshot(row)

    assert calls == ["quote:USDJPY", "spec:USDJPY"]
```

- [ ] **Step 10: テストを実行し CLOSE-1/CLOSE-2 のキル用テストが FAIL することを確認する**

Run: `uv run pytest tests/core/test_executor_gather_deadline.py -v`
Expected: `test_gather_close_snapshot_completes_when_within_budget`/`..._boundary_exact_budget_still_succeeds` の 2 件は PASS (回帰ピンとして機能)。`..._aborts_before_spec`/`..._aborts_before_resolve_close_rate` の 2 件は `DID NOT RAISE` で FAIL する。

- [ ] **Step 11: `gather_close_snapshot` に deadline 検査点を実装する**

`gather_close_snapshot` (:724-739) を次で置き換える:

```python
    def gather_close_snapshot(self, row: dict) -> CloseSnapshot:
        """commit-pre 相専用 (裁定書 F-1 / CR-2 / P8-01) — **core_lock
        非保持で呼ぶこと**。CLOSE 実行に要る quote (成行価格) +
        instrument spec + 換算レートを 1 回で取得し timestamp 付き
        スナップショットにする。`row` は呼び出し元 (Task 15 の commit-pre
        相) が `conn_supervisor` (lock 外の読取専用接続) から読んだ現在の
        order 行 (`pair`/`direction` を参照するだけ)。

        **Task 6 (プラン9 束B): gather deadline。** OPEN と対称に予算を
        適用する — 初稿の「CLOSE には入れない」は誤りとして撤回された
        (改稿 I1): `close_from_snapshot` も同じ予算で stale を拒否する
        (`age_sec > max_snapshot_age_sec`) ため、待って得られるのは古い
        snapshot と拒否であって close ではない。早く失敗を確定して次の
        再試行機会に戻る方が資金保護に有利。
        """
        now = self.clock.now()
        budget = self.settings.worker.snapshot_max_age_sec
        check = self._make_deadline_checker(budget)
        pair = row["pair"]
        quote = self.quote_fn(pair)
        price = quote.bid if row["direction"] == "long" else quote.ask
        check(f"spec:{pair}")
        spec = self.spec_fn(pair)
        check(f"rate:{spec.quote_currency}")
        rate, degraded = self.resolve_close_rate(spec.quote_currency, now)
        return CloseSnapshot(order_id=row["id"], pair=pair, price=price,
                             spec=spec, rate=rate,
                             rate_degraded=degraded, captured_at=now)
```

- [ ] **Step 12: テストを実行し CLOSE 側 4 件が全件 green になり、既存 gather 系テストが壊れていないことを確認する**

Run: `uv run pytest tests/core/test_executor_gather_deadline.py tests/core/test_executor_snapshot.py -v`
Expected: 新規 12 件 (OPEN 8 + CLOSE 4) + `test_executor_snapshot.py` 既存 20 件が全件 PASS。既存テストは `monotonic_fn` を渡さない (既定 `time.monotonic`) ため実測経過は常にミリ秒未満 — 既定予算 10.0s を超えず deadline は発火しない。

- [ ] **Step 13: `tests/loops/test_trade_loop.py` の `_loop` ヘルパに `monotonic_fn` 注入を追加する**

`tests/loops/test_trade_loop.py:1-15` の import に `time` を追加する:

```python
import threading
import time
from datetime import datetime, timezone
```

`_loop` (:33-51) を書き換える (シグネチャに `monotonic_fn=None` を追加し、`Executor(...)` 呼び出しへ渡す):

```python
def _loop(tmp_path, results, healthy=True, monotonic_fn=None):
    conn = connect(tmp_path / "t.db")
    init_db(conn)
    record_snapshot(conn, now=NOW, balance=1_000_000, equity=1_000_000)
    clock = FixedClock(NOW)
    broker = PaperBroker(conn, SETTINGS, clock)
    def rate_fn(ccy: str, account_ccy: str, now) -> ConversionRate:
        return ConversionRate(value=1.0, from_ccy=ccy, to_ccy=account_ccy,
                              leg_ts=(now,))

    executor = Executor(
        conn=conn, broker=broker, settings=SETTINGS,
        state_store=StateStore(tmp_path / "s.json"),
        activity=ActivityLog(tmp_path / "a.log"),
        notifier=Notifier(enabled=False, webhook_url=None),
        clock=clock, monotonic_fn=monotonic_fn or time.monotonic,
        quote_fn=lambda p: QUOTE, spec_fn=lambda p: SPEC,
        rate_fn=rate_fn)
```

(`rate_fn` の本体・以降の `provider = MagicMock()` 以下は無変更 — `rate_fn` は Task 7 の Step 11 で `**_ignored` を追加する。)

- [ ] **Step 14: テストを実行し、`_loop` を使う既存テストが壊れていないことを確認する**

Run: `uv run pytest tests/loops/test_trade_loop.py tests/loops/test_trade_loop_phases.py -q`
Expected: 既存全件 PASS (`monotonic_fn=None` は `time.monotonic` にフォールバックするだけで挙動不変)。

- [ ] **Step 15: 出口のピン (commit-pre への統合確認) を `tests/loops/test_trade_loop_phases.py` に追記する**

`tests/loops/test_trade_loop_phases.py` の末尾に追記する:

```python
def test_commit_pre_gather_deadline_produces_reason_distinct_from_stale(tmp_path):
    """出口のピン (確定仕様テスト観点 4): gather_open_snapshot の deadline
    超過は commit-pre の既存 `except Exception` (trade_loop.py:248) に
    そのまま乗り、trade_intents に理由が残る。文言は commit-core の
    "execution snapshot is stale" (executor.py の open_from_snapshot)
    とは異なることを確認する — ログを読む人が「ハングで打ち切った」のか
    「取得はできたが古かった」のかを区別できる必要がある (確定仕様 #8)。

    ここでは境界の厳密さではなく「配線が実際に効いているか」だけを見る
    粗い統合テストなので、monotonic フェイクは呼び出し回数に依存しない
    増分方式にする (厳密な境界テストは test_executor_gather_deadline.py
    側が担う)。
    """
    class _AlwaysLateMono:
        def __init__(self) -> None:
            self.n = 0

        def __call__(self) -> float:
            self.n += 1
            return self.n * 1000.0

    conn, loop, runner, tp = _loop(tmp_path, [MissionResult(
        "completed",
        {"action": "open", "pair": "USDJPY", "direction": "long",
         "entry_type": "market", "horizon": "day", "limit_price": None,
         "expires_in": None, "stop_loss": 147.80, "take_profit": 149.00,
         "reasoning": "x"}, [])], monotonic_fn=_AlwaysLateMono())

    out = loop.run_once("cron")

    assert out is not None
    assert out["result"] == "rejected"
    iid_row = conn.execute(
        "SELECT gate_result, reject_reason FROM trade_intents "
        "ORDER BY id DESC LIMIT 1").fetchone()
    assert iid_row["gate_result"] == "rejected"
    reason = iid_row["reject_reason"]
    assert "deadline exceeded" in reason
    assert "stale" not in reason
```

- [ ] **Step 16: テストを実行し green を確認する**

Run: `uv run pytest tests/loops/test_trade_loop_phases.py -v -k gather_deadline`
Expected: PASS。(このテストは Step 7/11 実装済みの配線をそのまま確認する統合ピンであり、実装コードの新規追加は伴わない — TDD の red/green サイクルではなく回帰確認として一度で green になる。)

- [ ] **Step 17: 変異テスト**

```bash
find . -name __pycache__ -type d -not -path "./.venv/*" -exec rm -rf {} +
```

| # | 変異 | 殺すテスト |
|---|---|---|
| M1 | OPEN-1 の検査 (`check(f"spec:{intent.pair}")`) を削除 | `test_gather_open_snapshot_aborts_before_spec_of_intent_pair` |
| M2 | OPEN-2 の検査 (exposure ループ内の `check(f"spec:{pair}")`) を削除 | `test_gather_open_snapshot_aborts_before_second_exposure_pair_spec` |
| M3 | OPEN-3 の検査 (rates ループ内の `check(f"rate:{ccy}")`) を削除 | `test_gather_open_snapshot_aborts_before_second_currency_rate` |
| M4 | CLOSE-1 の検査 (`check(f"spec:{pair}")`) を削除 | `test_gather_close_snapshot_aborts_before_spec` |
| M5 | CLOSE-2 の検査 (`check(f"rate:{spec.quote_currency}")`) を削除 | `test_gather_close_snapshot_aborts_before_resolve_close_rate` |
| M6 | `_make_deadline_checker` の `budget` 引数を無視し独立の大きな定数 (例: `999.0`) に差し替える | `test_gather_open_snapshot_aborts_before_spec_of_intent_pair`・`test_gather_close_snapshot_aborts_before_spec` 他、超過系の全テスト |
| M7 | `_make_deadline_checker` の `elapsed = self.monotonic_fn() - start` を `elapsed = 0.0` にする（型を壊さず deadline を無効化する意味的変異） | `test_gather_open_snapshot_deadline_independent_of_fixed_clock` (`DataUnhealthy` が上がらず red。`datetime - datetime` と float の比較による `TypeError` だけで殺す変異にはしない) |
| M8 | 比較を `elapsed > budget` から `elapsed >= budget` に変える | `test_gather_open_snapshot_boundary_exact_budget_still_succeeds`・`test_gather_close_snapshot_boundary_exact_budget_still_succeeds` |
| M9 | `DataUnhealthy` を送出せず `return None` にする (checker が握りつぶす) | `test_gather_open_snapshot_aborts_before_spec_of_intent_pair`・`test_deadline_checker_raises_just_over_budget` |

各変異を注入したら `grep -n "_make_deadline_checker\|check(f\"" src/agentic_fx/core/executor.py` で改変を目視確認してから対象テストのみ実行し red を確認、revert して green に戻す。revert 後も `find . -name __pycache__ -type d -not -path "./.venv/*" -exec rm -rf {} +` を実行する。

- [ ] **Step 18: 全体テストを実行し、既存 1726 件が壊れていないことを確認する**

```bash
find . -name __pycache__ -type d -not -path "./.venv/*" -exec rm -rf {} +
uv run pytest -q
```

Expected: 既存件数 + Task 6 の新規 13 件 (checker 2 + OPEN 6 + CLOSE 4 + 出口ピン 1) が全件 PASS、0 failed。

- [ ] **Step 19: コミット**

```bash
git add src/agentic_fx/core/executor.py tests/core/test_executor_gather_deadline.py \
        tests/loops/test_trade_loop.py tests/loops/test_trade_loop_phases.py
git commit -m "$(cat <<'EOF'
feat: gather deadline (monotonic 注入 + OPEN/CLOSE 両方) を追加 (プラン9 Task6)

一次ソースがハングすると gather が予算超過後も外部 I/O を続け、
commit-core の鮮度検証が必ず拒否する「健全に見える gate_rejected を
記録しながら一度も取引しないシステム」になる (実測: 20.5秒)。
gather_open_snapshot/gather_close_snapshot の各脚の間に
time.monotonic ベースの deadline 検査を挟み、予算超過なら次の脚に
進む前に DataUnhealthy で打ち切る。captured_at は FixedClock を使う
既存位置のまま変えない (deadline は monotonic 専用、Clock とは独立)。
CLOSE 側も同じ予算で対称に打ち切る — close_from_snapshot は同じ予算で
stale を拒否するため、待っても close はできない (設計:
docs/superpowers/specs/2026-08-11-gather-deadline-design.md 確定仕様)。

Risk Gate / commit-core の判定ロジックは 1 文字も変えていない。
EOF
)"
```

---

### Task 7: deadline の脚伝播 (`PriceProvider.to_account_rate`)

**Files:**
- Modify: `src/agentic_fx/datafeed/price_provider.py:411-464` (`to_account_rate` に `deadline_check` パラメータ追加)
- Modify: `src/agentic_fx/core/executor.py:229-322` (`cycle_rate_fn` シグネチャ + 呼び出し 1 行)、`:324-339` (`resolve_close_rate` シグネチャ + 呼び出し 1 行)、`gather_open_snapshot`/`gather_close_snapshot` の `cycle_rate_fn(now)`/`resolve_close_rate(...)` 呼び出し箇所各 1 行
- Modify: `src/agentic_fx/service.py:447-450` (`rate_fn` クロージャが `deadline_check` を転送)。**親骨格の File Structure 表に無い追加変更** — production 配線を実際に通すために必須 (本書末尾「束 B 自己レビュー」③に理由を記載)
- Modify: `tests/datafeed/test_price_provider.py` (`to_account_rate` セクションの末尾に追記)
- Modify: `tests/core/test_executor_snapshot.py:51-57` (`_rate_fn`)、`:507-511` (`always_fail_rate_fn`) — `**_ignored`/`deadline_check=None` 許容の 1 行追加
- Modify: `tests/loops/test_trade_loop.py:39-41` (`_loop` 内の `rate_fn`) — 同上
- Modify: `tests/core/test_executor_gather_deadline.py` (Task 6 で作成したファイルに Task 7 のテストを追記)
- Test 実行対象 (既存回帰確認): `tests/datafeed/test_price_provider.py`、`tests/core/test_executor_snapshot.py`、`tests/test_e2e_worker_isolation.py`、`tests/loops/test_trade_loop.py`、`tests/loops/test_trade_loop_phases.py`、`tests/test_e2e_phase1.py`、`tests/test_e2e_plugin_signal.py`、`tests/test_service_app.py`、`tests/test_wiring.py`

**Interfaces:**
- Consumes: Task 6 の `Executor.monotonic_fn`/`_make_deadline_checker`/gather 内のローカル変数 `check` (`Callable[[str], None]`)
- Produces:
  ```python
  # src/agentic_fx/datafeed/price_provider.py — PriceProvider
  def to_account_rate(self, ccy: str, account_ccy: str, *,
                      reference_ts: datetime, max_skew_min: float,
                      deadline_check: Callable[[str], None] | None = None,
                      ) -> ConversionRate: ...
  # deadline_check は USD クロス (`for spec in legs`) の各脚の前でのみ
  # 呼ぶ。直接/逆ペア (1 脚) では呼ばない。None (既定) は no-op。
  # leg 名は実シンボルまで含める: f"rate:{ccy}:{spec[0]}" (例:
  # "rate:EUR:EURUSD")。

  # src/agentic_fx/core/executor.py — Executor
  def cycle_rate_fn(self, now, *,
                    deadline_check: Callable[[str], None] | None = None
                    ) -> Callable[[str], ConversionRate]: ...
  def resolve_close_rate(self, ccy: str, now, *,
                         deadline_check: Callable[[str], None] | None = None
                         ) -> tuple[ConversionRate | None, bool]: ...
  # 両者とも deadline_check が None なら self.rate_fn を 3 引数のまま
  # 呼ぶ (既存の 3 引数 rate_fn 実装 — backtest/runner.py、大半の
  # scheduler/executor テストの stub — を一切変えない)。deadline_check
  # が非 None のときだけ keyword-only 引数として rate_fn へ転送する。
  ```
- `rate_fn: Callable[[str, str, datetime], ConversionRate]` の呼び出し規約が拡張される: **gather 経由の呼び出しのみ** `deadline_check=` keyword を追加で受け取る。`rate_fn` 実装は `**_ignored` (または明示 `deadline_check=None`) で無害に許容できる。

#### 現状 (`src/agentic_fx/datafeed/price_provider.py:411-464` — `to_account_rate`)

```python
    def to_account_rate(self, ccy: str, account_ccy: str, *,
                        reference_ts: datetime,
                        max_skew_min: float) -> ConversionRate:
        """通貨 1 単位 = 口座通貨いくらか (設計書 §5「口座通貨と換算」)。
        ...(既存 docstring — 変更なし、Step 3 で 1 段落だけ追記する)...
        """
        if ccy == account_ccy:
            return ConversionRate(1.0, ccy, account_ccy, (reference_ts,))
        direct = self._rate_symbol(ccy, account_ccy)
        if direct is not None:
            rate, ts = self._rate_of(direct)
            result = ConversionRate(rate, ccy, account_ccy, (ts,))
        else:
            # ③ USD 経由のクロス (例: EUR→JPY = EURUSD(ask) × USDJPY(ask))
            legs: list[tuple[str, bool]] = []
            for base, quote in ((ccy, "USD"), ("USD", account_ccy)):
                if base == quote:
                    continue            # 片脚が USD 同士なら換算不要
                spec = self._rate_symbol(base, quote)
                if spec is None:
                    raise DataUnhealthy(
                        f"cannot convert {ccy}->{account_ccy}: no symbol for "
                        f"{base}/{quote} in VENDOR_SYMBOLS")
                legs.append(spec)
            if not legs:
                # ccy == account_ccy は先に返しているため到達しない
                raise DataUnhealthy(
                    f"cannot convert {ccy}->{account_ccy}: no route")
            rate = 1.0
            leg_ts: list[datetime] = []
            for spec in legs:
                r, ts = self._rate_of(spec)
                rate *= r
                leg_ts.append(ts)
            result = ConversionRate(rate, ccy, account_ccy, tuple(leg_ts))
        validate_conversion_skew(result, reference_ts=reference_ts,
                                 max_skew_min=max_skew_min)
        return result
```

- [ ] **Step 1: `to_account_rate` の `deadline_check` の失敗するテストを `tests/datafeed/test_price_provider.py` に追記する**

「to_account_rate」セクション末尾 (`test_to_account_rate_cross_leg_skew_exceeded_raises` の直後、`test_readonly_provider_skips_bar_cache_write` の直前) に追記する:

```python
def test_to_account_rate_deadline_check_called_before_each_cross_leg(tmp_path):
    """PROV-1 (確定仕様 #5): USD クロスの `for spec in legs` の各脚の前で
    `deadline_check` が呼ばれ、そこで拒否されると 2 脚目の `_rate_of`
    (=quote 取得) が実行されない。"""
    _, p = _provider(tmp_path)
    checked: list[str] = []

    def deadline_check(leg: str) -> None:
        checked.append(leg)
        if leg == "rate:EUR:USDJPY":
            raise DataUnhealthy(f"gather deadline exceeded before {leg}")

    with patch("agentic_fx.datafeed.price_provider.sources.yf_quote",
               side_effect=_quote_map(EURUSD=(1.08, 1.09),
                                      USDJPY=(148.0, 149.0))) as yq:
        with pytest.raises(DataUnhealthy, match="gather deadline exceeded"):
            _rate(p, "EUR", "JPY", deadline_check=deadline_check)

    assert checked == ["rate:EUR:EURUSD", "rate:EUR:USDJPY"]
    # 2 脚目で打ち切ったので quote 取得は 1 回 (EURUSD) だけ
    assert yq.call_count == 1


def test_to_account_rate_deadline_check_not_called_for_direct_pair(tmp_path):
    """スコープ確認: 直接/逆ペア (1 脚) は `for spec in legs` を通らない
    ため `deadline_check` は呼ばれない (確定仕様 #5 のスコープは USD
    クロスの脚間のみ)。"""
    _, p = _provider(tmp_path)
    checked: list[str] = []
    with patch("agentic_fx.datafeed.price_provider.sources.yf_quote",
               side_effect=_quote_map(USDJPY=(148.0, 149.0))):
        _rate(p, "USD", "JPY", deadline_check=checked.append)
    assert checked == []


def test_to_account_rate_deadline_check_default_none_is_backward_compatible(
        tmp_path):
    """`deadline_check` 省略時 (既定 None) は素通り — 既存の全呼び出し元
    (直接/逆ペア/クロスいずれも) の挙動を変えないことの pin。"""
    _, p = _provider(tmp_path)
    with patch("agentic_fx.datafeed.price_provider.sources.yf_quote",
               side_effect=_quote_map(EURUSD=(1.08, 1.09),
                                      USDJPY=(148.0, 149.0))):
        rate = _rate(p, "EUR", "JPY")  # deadline_check を渡さない
    assert rate.value == pytest.approx(1.09 * 149.0)
```

- [ ] **Step 2: テストを実行し失敗を確認する**

Run: `uv run pytest tests/datafeed/test_price_provider.py -v -k deadline_check`
Expected: `test_to_account_rate_deadline_check_called_before_each_cross_leg` と `test_to_account_rate_deadline_check_not_called_for_direct_pair` の 2 件が `TypeError: PriceProvider.to_account_rate() got an unexpected keyword argument 'deadline_check'` で FAIL する。`..._default_none_is_backward_compatible` は `deadline_check` を渡さないため既に PASS する (回帰ピンとして機能)。

- [ ] **Step 3: `to_account_rate` に `deadline_check` を実装する**

`to_account_rate` (:411-464) を次で置き換える:

```python
    def to_account_rate(self, ccy: str, account_ccy: str, *,
                        reference_ts: datetime,
                        max_skew_min: float,
                        deadline_check: Callable[[str], None] | None = None,
                        ) -> ConversionRate:
        """通貨 1 単位 = 口座通貨いくらか (設計書 §5「口座通貨と換算」)。

        quote/base のどちらの通貨にも使う汎用関数 (旧
        `quote_to_account_rate` — 互換 alias は作らない。呼び出し側を
        全て更新する)。①直接ペア → ②逆ペア (逆数) → ③USD 経由のクロス の
        順に解決し、**保守側** (`_rate_of` 参照) を使う。

        `reference_ts`/`max_skew_min`: この換算が使われる判断 (gate 評価・
        予約再検証サイクル) の基準時刻と許容skew。各脚の鮮度は quote 取得時
        (`get_quote` → `validate_quote`) に検証済みだが、①クロス脚同士の
        時刻差 ②この判断全体とのスナップショット時刻差 は
        `validate_conversion_skew` で追加検証する (codex 指摘 D-I2)。

        いずれも解決不可、または skew 超過なら DataUnhealthy。呼び出し側
        (sizing / risk_gate / executor / scheduler) はこれを fail closed
        (sizing は SizingError、gate/executor は却下、予約再検証は当該ペアの
        pending_fill 取消) に変換する — 推測値でのサイジングは無音の過大
        建玉になるため、ここで握りつぶさない。

        `deadline_check` (Task 7, プラン9 束B): `Executor.gather_*_snapshot`
        の snapshot 予算を USD クロスの各脚まで伝播させる checker。
        `None` (既定・通常の呼び出し元) では no-op。`for spec in legs`
        の**各脚の前**で呼ぶ — 直接/逆ペア (1 脚) では呼ばない
        (スコープは USD クロスの脚間のみ — 改稿 C2)。leg 名は実シンボル
        まで含める (例: `"rate:EUR:EURUSD"`)。`cycle_rate(ccy)` 1 回の
        内部で最大 2 本のネットワーク脚が走るため、gather 直下の検査
        だけでは 1 通貨のクロスで予算 × 2 を払ってしまう
        (改稿 C2 の実測根拠)。
        """
        if ccy == account_ccy:
            return ConversionRate(1.0, ccy, account_ccy, (reference_ts,))
        direct = self._rate_symbol(ccy, account_ccy)
        if direct is not None:
            rate, ts = self._rate_of(direct)
            result = ConversionRate(rate, ccy, account_ccy, (ts,))
        else:
            # ③ USD 経由のクロス (例: EUR→JPY = EURUSD(ask) × USDJPY(ask))
            legs: list[tuple[str, bool]] = []
            for base, quote in ((ccy, "USD"), ("USD", account_ccy)):
                if base == quote:
                    continue            # 片脚が USD 同士なら換算不要
                spec = self._rate_symbol(base, quote)
                if spec is None:
                    raise DataUnhealthy(
                        f"cannot convert {ccy}->{account_ccy}: no symbol for "
                        f"{base}/{quote} in VENDOR_SYMBOLS")
                legs.append(spec)
            if not legs:
                # ccy == account_ccy は先に返しているため到達しない
                raise DataUnhealthy(
                    f"cannot convert {ccy}->{account_ccy}: no route")
            rate = 1.0
            leg_ts: list[datetime] = []
            for spec in legs:
                if deadline_check is not None:
                    deadline_check(f"rate:{ccy}:{spec[0]}")
                r, ts = self._rate_of(spec)
                rate *= r
                leg_ts.append(ts)
            result = ConversionRate(rate, ccy, account_ccy, tuple(leg_ts))
        validate_conversion_skew(result, reference_ts=reference_ts,
                                 max_skew_min=max_skew_min)
        return result
```

- [ ] **Step 4: テストを実行し新規 3 件が green になり、既存の to_account_rate テスト (13 件) が壊れていないことを確認する**

Run: `uv run pytest tests/datafeed/test_price_provider.py -v -k "to_account_rate"`
Expected: 既存 13 件 + 新規 3 件 = 16 件全件 PASS。

- [ ] **Step 5: Executor レベルの伝播テストを `tests/core/test_executor_gather_deadline.py` に追記する**

ファイル末尾に追記する:

```python
# ---- Task 7: 脚伝播 (Executor → rate_fn への deadline_check 配線) ----------

def test_cycle_rate_fn_propagates_deadline_check_to_rate_fn(tmp_path):
    """Task 7: `cycle_rate_fn` が `deadline_check` を `rate_fn` へ転送する
    こと (`to_account_rate` 自体の脚検査は price_provider 側のテストが
    担うので、ここでは「渡ったこと」だけを見る configuration test)。"""
    mono = _Mono()
    received: list[object] = []

    def rate_fn(ccy, account_ccy, now, *, deadline_check=None):
        received.append(deadline_check)
        return ConversionRate(1.0, ccy, account_ccy, (now,))

    ex = _make_executor(tmp_path, monotonic_fn=mono,
                        quote_fn=lambda p: QUOTE, spec_fn=lambda p: SPECS[p],
                        rate_fn=rate_fn)
    intent = _open_intent("USDJPY")

    ex.gather_open_snapshot(intent, exposure_pairs=[])

    assert len(received) == 2  # JPY, USD の 2 通貨
    assert all(cb is not None for cb in received)
    # 1 回の gather = 1 つの deadline (通貨ごとに別オブジェクトを渡さない)
    assert len(set(id(cb) for cb in received)) == 1


def test_resolve_close_rate_propagates_deadline_check_to_rate_fn(tmp_path):
    """Task 7: `gather_close_snapshot` → `resolve_close_rate` →
    `rate_fn` への転送 (CLOSE 側)。"""
    mono = _Mono()
    received: list[object] = []

    def rate_fn(ccy, account_ccy, now, *, deadline_check=None):
        received.append(deadline_check)
        return ConversionRate(1.0, ccy, account_ccy, (now,))

    ex = _make_executor(tmp_path, monotonic_fn=mono,
                        quote_fn=lambda p: QUOTE, spec_fn=lambda p: SPECS[p],
                        rate_fn=rate_fn)
    row = _insert_open_order(ex.conn, pair="USDJPY")

    ex.gather_close_snapshot(row)

    assert len(received) == 1
    assert received[0] is not None


def test_gather_close_snapshot_absorbs_cross_leg_deadline_into_degraded_rate(
        tmp_path):
    """CLOSE の吸収契約の pin (advisor 指摘): 実際の `PriceProvider.
    to_account_rate` を `rate_fn` に束縛し、EUR→JPY の USD クロス
    (2 脚) の 1 脚目取得後に deadline を超過させる。`resolve_close_rate`
    は例外を握りつぶし degraded フォールバックにする既存契約 (設計書
    §5「クローズはレート欠損でも妨げない」) があるため、
    `gather_close_snapshot` 自体は例外を投げずに完了する。ただし 2 脚目
    (USDJPY) の quote は一度も取得されていないこと (=打ち切りが実際に
    効いていること) まで確認する — 「degraded になった」だけでは
    (rate_fn が別の理由で単純に失敗しても同じ結果になるため) 偶然の
    一致と区別できない。

    本番の `_SPECS` (USDJPY/EURUSD のみ) には quote_currency=EUR を持つ
    銘柄が無い (account_currency=JPY からは常に直接ペアで解決できる) ため、
    このテストだけ架空の EURJPY spec を注入して EUR→JPY クロスを踏ませる。
    """
    mono = _Mono()
    conn = connect(tmp_path / "t.db")
    init_db(conn)
    record_snapshot(conn, now=NOW, balance=1_000_000, equity=1_000_000)
    from agentic_fx.datafeed.price_provider import PriceProvider
    provider = PriceProvider(conn, SETTINGS, FixedClock(NOW))

    def rate_fn(ccy, account_ccy, now, **kw):
        return provider.to_account_rate(
            ccy, account_ccy, reference_ts=now,
            max_skew_min=SETTINGS.datafeed.conversion_skew_max_min, **kw)

    eurjpy_spec = InstrumentSpec(
        symbol="EURJPY", pip_size=0.01, min_lot=0.01, max_lot=50.0,
        lot_step=0.01, contract_size=100_000,
        base_currency="EUR", quote_currency="EUR")

    def quote_fn(pair):
        return Quote("EURJPY", 160.0, 160.2, NOW, "test")

    def spec_fn(pair):
        return eurjpy_spec

    ex = Executor(
        conn=conn, broker=PaperBroker(conn, SETTINGS, FixedClock(NOW)),
        settings=SETTINGS, state_store=StateStore(tmp_path / "state.json"),
        activity=ActivityLog(tmp_path / "activity.log"),
        notifier=Notifier(enabled=False, webhook_url=None),
        clock=FixedClock(NOW), monotonic_fn=mono,
        quote_fn=quote_fn, spec_fn=spec_fn, rate_fn=rate_fn)
    row = _insert_open_order(ex.conn, pair="EURJPY")

    def eurusd_quote(pair):
        assert pair == "EURUSD"
        mono.t = BUDGET + 0.1  # 1 脚目取得に予算を使い切ったことを模す
        return Quote("EURUSD", 1.08, 1.09, NOW, "yfinance")

    with patch("agentic_fx.datafeed.price_provider.sources.yf_quote",
               side_effect=eurusd_quote) as yq:
        snapshot = ex.gather_close_snapshot(row)

    assert snapshot.rate_degraded is True
    assert snapshot.rate is None
    # 2 脚目 (USDJPY) は一度も取得されていない (打ち切りが効いている)
    assert yq.call_count == 1
    assert yq.call_args.args[0] == "EURUSD"
```

この CLOSE 統合テストの識別力は末尾 2 assert にある。catch-all は stub の
`AssertionError` も `DataUnhealthy` に変えるため、`rate_degraded/rate` だけでは
伝播有無を区別できない。必ず `call_count == 1` と第 1 脚名を残す。また現行の
CLOSE 契約では cross-leg deadline も catch-all に吸収され、外部には `stale`
相当の degraded として見える。これは診断性上の既知制約であり、本 task では
fail-soft 契約を変えない。

このテストは `unittest.mock.patch` を使うため、ファイル冒頭の import に追記する:

```python
from unittest.mock import patch
```

- [ ] **Step 6: テストを実行し 3 件が FAIL することを確認する**

Run: `uv run pytest tests/core/test_executor_gather_deadline.py -v -k "propagates_deadline_check or absorbs_cross_leg"`
Expected: 3 件とも FAIL する。
- `test_cycle_rate_fn_propagates_deadline_check_to_rate_fn`: `assert all(cb is not None for cb in received)` が `received == [None, None]` で AssertionError。
- `test_resolve_close_rate_propagates_deadline_check_to_rate_fn`: `received == [None]` となり AssertionError。
- `test_gather_close_snapshot_absorbs_cross_leg_deadline_into_degraded_rate`: `rate_degraded/rate` は catch-all のため変更前後とも同値になり得る。未転送なら第 2 脚まで呼ばれて `yq.call_count == 2`、正しく転送すれば第 1 脚後に止まり `== 1` なので、call-count assert で FAIL する。

- [ ] **Step 7: `cycle_rate_fn` に `deadline_check` を実装し `gather_open_snapshot` から配線する**

`cycle_rate_fn` (:229-233 のシグネチャと :294-297 の `if ccy not in cache:` ブロック冒頭) を書き換える。シグネチャを次に変える:

```python
    def cycle_rate_fn(self, now, *,
                      deadline_check: Callable[[str], None] | None = None
                      ) -> Callable[[str], ConversionRate]:
```

既存 docstring の末尾 (fix round 3 の段落の後) に 1 段落追記する:

```python
        Task 7 (プラン9 束B): `deadline_check` を渡すと `self.rate_fn` へ
        そのまま転送する — `rate_fn` が `PriceProvider.to_account_rate` に
        束縛されている場合、USD クロスの `for spec in legs` の各脚の前で
        呼ばれる (改稿 C2)。`None` (既定・gather 以外の全呼び出し元
        `_open` 等) では `rate_fn` に kwarg 自体を渡さない — 3 引数のみを
        期待する既存の `rate_fn` 実装 (backtest/runner.py・scheduler 系
        テストの多数の stub) を壊さないため。
        """
```

`fn(ccy)` 内の呼び出し行 (:296-297) を書き換える:

```python
        def fn(ccy: str) -> ConversionRate:
            nonlocal span_min, span_max
            if ccy not in cache:
                if deadline_check is not None:
                    rate = self.rate_fn(ccy, account_ccy, now,
                                        deadline_check=deadline_check)
                else:
                    rate = self.rate_fn(ccy, account_ccy, now)
```

(以降 `self._last_good_rate[(ccy, account_ccy)] = rate` から関数末尾までは 1 文字も変えない。)

`gather_open_snapshot` の `cycle_rate = self.cycle_rate_fn(now)` の行を次に変える:

```python
        cycle_rate = self.cycle_rate_fn(now, deadline_check=check)
```

- [ ] **Step 8: `resolve_close_rate` に `deadline_check` を実装し `gather_close_snapshot` から配線する**

`resolve_close_rate` (:324-339) を次で置き換える:

```python
    def resolve_close_rate(self, ccy: str, now, *,
                           deadline_check: Callable[[str], None] | None = None
                           ) -> tuple[ConversionRate | None, bool]:
        """クローズ専用のレート解決。現在レートが取れなければ最後に健全性
        検証を通ったレートへ degraded フォールバックする (設計書 §5:
        クローズはレート欠損でも妨げない)。戻り値は (rate, degraded)。
        rate が None なのは、一度も健全なレートを観測できていない場合のみ
        (プロセス起動直後の初回クローズ等) — この場合 realized_pnl は
        未確定のまま残し、次回の定期同期で解消する。

        `deadline_check` (Task 7, プラン9 束B): `cycle_rate_fn` と同じ
        転送規約 — `None` (既定) なら `rate_fn` を 3 引数のまま呼ぶ。
        **`deadline_check` が USD クロスの 2 脚目の前で `DataUnhealthy`
        を送出しても、この関数自体の fail-soft 契約は変えない** —
        直下の `except Exception` がそれを吸収し degraded フォールバック
        にする。CLOSE はレート欠損でも妨げない、という既存方針の帰結
        であり、意図的な挙動 (2 脚目相当の外部 I/O だけを打ち切り、
        CLOSE 自体は失敗させない)。
        """
        account_ccy = self.settings.account_currency
        try:
            if deadline_check is not None:
                rate = self.rate_fn(ccy, account_ccy, now,
                                    deadline_check=deadline_check)
            else:
                rate = self.rate_fn(ccy, account_ccy, now)
            self._last_good_rate[(ccy, account_ccy)] = rate
            return rate, False
        except Exception:  # noqa: BLE001 — クローズを止めない (設計書 §5)
            return self._last_good_rate.get((ccy, account_ccy)), True
```

`gather_close_snapshot` の `rate, degraded = self.resolve_close_rate(spec.quote_currency, now)` の行を次に変える:

```python
        rate, degraded = self.resolve_close_rate(
            spec.quote_currency, now, deadline_check=check)
```

- [ ] **Step 9: テストを実行し Step 5 の 3 件が green になり、`test_executor_gather_deadline.py` 全体が壊れていないことを確認する**

Run: `uv run pytest tests/core/test_executor_gather_deadline.py -v`
Expected: Task 6 の 13 件 + Task 7 の 3 件 = 16 件全件 PASS。

- [ ] **Step 10: `tests/core/test_executor_snapshot.py` の `rate_fn` stub を kwarg 許容に更新する**

`_rate_fn` (:51-57) を次で置き換える:

```python
def _rate_fn(ccy, account_ccy, now, **_ignored):
    """JPY 恒等 / USD・EUR → JPY のみ供給する最小スタブ。**_ignored:
    Task 7 (プラン9 束B) が gather 経由の呼び出しで rate_fn へ
    deadline_check kwarg を渡すため、この既存 stub は無害に許容する。"""
    if ccy == account_ccy:
        return ConversionRate(1.0, ccy, account_ccy, (now,))
    if account_ccy == "JPY" and ccy in ("USD", "EUR"):
        return ConversionRate(QUOTE.ask, ccy, "JPY", (now,))
    raise DataUnhealthy(f"no rate for {ccy}->{account_ccy}")
```

`always_fail_rate_fn` (関数定義は `test_close_order_from_snapshot_records_degraded_rate` 内、:507-511 付近) を次で置き換える:

```python
    def always_fail_rate_fn(ccy, account_ccy, now, **_ignored):
        # 最初の 1 回だけ失敗させる (resolve_close_rate がフォールバック)。
        # **_ignored: Task 7 の deadline_check kwarg を無害に許容する。
        calls.append(f"rate_fn:{ccy}")
        raise DataUnhealthy(f"rate unavailable for {ccy}")
```

Run: `uv run pytest tests/core/test_executor_snapshot.py tests/test_e2e_worker_isolation.py -q`
Expected: 既存全件 PASS (`test_e2e_worker_isolation.py` は `test_executor_snapshot.py` の `_make_executor`/`_rate_fn` を import して使うため、同じ更新で両方カバーされる)。

- [ ] **Step 11: `tests/loops/test_trade_loop.py` の `_loop` 内 `rate_fn` を kwarg 許容に更新する**

`_loop` (:39-41) の `rate_fn` を次で置き換える:

```python
    def rate_fn(ccy: str, account_ccy: str, now, **_ignored) -> ConversionRate:
        # **_ignored: Task 7 (プラン9 束B) の deadline_check kwarg を
        # 無害に許容する (このテスト用 stub は skew/クロスを模さない
        # ため kwarg 自体は使わない)。
        return ConversionRate(value=1.0, from_ccy=ccy, to_ccy=account_ccy,
                              leg_ts=(now,))
```

Run: `uv run pytest tests/loops/test_trade_loop.py tests/loops/test_trade_loop_phases.py -q`
Expected: 既存全件 + Task 6 の Step 15 で追加した出口ピン 1 件が全件 PASS。

- [ ] **Step 12: production 配線 (`service.py`) を更新する**

`src/agentic_fx/service.py` の import に `Callable` を追加する (既存 import ブロックの適切な位置、例えば `from datetime import datetime, timezone` の直前など型 import 群の近くに 1 行追加):

```python
from collections.abc import Callable
```

`rate_fn` クロージャ (:447-450) を次で置き換える:

```python
        def rate_fn(ccy: str, account_ccy: str, now: datetime, *,
                    deadline_check: Callable[[str], None] | None = None):
            return provider.to_account_rate(
                ccy, account_ccy, reference_ts=now,
                max_skew_min=settings.datafeed.conversion_skew_max_min,
                deadline_check=deadline_check)
```

Run: `uv run pytest tests/test_e2e_phase1.py tests/test_e2e_worker_isolation.py tests/test_e2e_plugin_signal.py tests/test_service_app.py tests/test_wiring.py -q`
Expected: 既存全件 PASS。これらのテストは `build_app`/`Scheduler` 経由で `service.py` の `rate_fn` を間接的に組み立てるが、`deadline_check` を渡さない経路 (Scheduler・backtest) では `deadline_check=None` が渡り `to_account_rate` 側で no-op になるため、production 配線の変更は既存 e2e テストの挙動を変えない。

- [ ] **Step 13: 変異テスト**

```bash
find . -name __pycache__ -type d -not -path "./.venv/*" -exec rm -rf {} +
```

| # | 変異 | 殺すテスト |
|---|---|---|
| M10 | `to_account_rate` の `for spec in legs` 内の `deadline_check(...)` 呼び出しを削除する (PROV-1) | `test_to_account_rate_deadline_check_called_before_each_cross_leg` |
| M11 | `deadline_check` を直接/逆ペア (1 脚) の分岐にも呼ぶよう追加する (スコープ逸脱) | `test_to_account_rate_deadline_check_not_called_for_direct_pair` |
| M12 | `cycle_rate_fn` の `fn(ccy)` 内で `deadline_check` を `self.rate_fn` へ転送しない (常に 3 引数呼び出しに戻す) | `test_cycle_rate_fn_propagates_deadline_check_to_rate_fn` |
| M13 | `resolve_close_rate` の `deadline_check` 転送を削除する (常に 3 引数呼び出しに戻す) | `test_resolve_close_rate_propagates_deadline_check_to_rate_fn`・`test_gather_close_snapshot_absorbs_cross_leg_deadline_into_degraded_rate` |
| M14 | `gather_open_snapshot` の `cycle_rate_fn(now, deadline_check=check)` を `cycle_rate_fn(now)` に戻す (配線切断) | `test_cycle_rate_fn_propagates_deadline_check_to_rate_fn` |
| M15 | `gather_close_snapshot` の `resolve_close_rate(..., deadline_check=check)` を `deadline_check` 無しに戻す (配線切断) | `test_resolve_close_rate_propagates_deadline_check_to_rate_fn`・`test_gather_close_snapshot_absorbs_cross_leg_deadline_into_degraded_rate` |

各変異を注入したら `grep -n "deadline_check" src/agentic_fx/datafeed/price_provider.py src/agentic_fx/core/executor.py` で改変を目視確認してから対象テストのみ実行し red を確認、revert して green に戻す。revert 後も `find . -name __pycache__ -type d -not -path "./.venv/*" -exec rm -rf {} +` を実行する。

- [ ] **Step 14: 全体テストを実行する**

```bash
find . -name __pycache__ -type d -not -path "./.venv/*" -exec rm -rf {} +
uv run pytest -q
```

Expected: 束 B 開始前の既存件数 + Task 6 の 13 件 + Task 7 の 6 件 (price_provider 3 + executor 3) = 全件 PASS、0 failed。

- [ ] **Step 15: コミット**

```bash
git add src/agentic_fx/datafeed/price_provider.py src/agentic_fx/core/executor.py \
        src/agentic_fx/service.py tests/datafeed/test_price_provider.py \
        tests/core/test_executor_snapshot.py tests/core/test_executor_gather_deadline.py \
        tests/loops/test_trade_loop.py
git commit -m "$(cat <<'EOF'
feat: gather deadline を to_account_rate の USD クロス脚まで伝播 (プラン9 Task7)

cycle_rate(ccy) 1 回の内部で最大2本のネットワーク脚が走るため、
Task 6 の gather 直下の検査だけでは EUR→JPY 等の1通貨のクロスで
予算×2 を払ってしまう (実測根拠は設計書
docs/superpowers/specs/2026-08-11-gather-deadline-design.md 改稿 C2)。
Executor.cycle_rate_fn/resolve_close_rate に deadline_check を追加し、
PriceProvider.to_account_rate の for spec in legs の各脚の前まで
伝播させる。deadline_check=None (既定) は完全な no-op — 3引数のみを
期待する既存 rate_fn 実装 (backtest/scheduler 系の大半の stub) は
無変更のまま動く。CLOSE 側は resolve_close_rate の既存 fail-soft
契約 (except Exception → degraded フォールバック) がそのまま
deadline 例外も吸収する — CLOSE を失敗させず、2脚目相当の外部 I/O
だけを打ち切る。

Risk Gate / commit-core の判定ロジックは 1 文字も変えていない。
EOF
)"
```

---

### 束 B 自己レビュー

#### ① spec 確定仕様のテスト 6 観点・変異 7 種の対応表

**テスト 6 観点** (確定仕様「テスト」節):

| # | 観点 | Task | Step | テスト名 |
|---|---|---|---|---|
| 1 | 非損失性のピン | 6 | 5, 9 | `test_gather_open_snapshot_completes_when_within_budget` / `test_gather_close_snapshot_completes_when_within_budget` (境界の等号ケースを含めれば `..._boundary_exact_budget_still_succeeds` 2 件も同じ観点の追加事例) |
| 2 | 打ち切りのピン (検査点ごと) | 6, 7 | 5, 9 (executor 側 5 検査点), 1 (price_provider 側 1 検査点) | `test_gather_open_snapshot_aborts_before_spec_of_intent_pair` (OPEN-1) / `..._aborts_before_second_exposure_pair_spec` (OPEN-2) / `..._aborts_before_second_currency_rate` (OPEN-3) / `test_gather_close_snapshot_aborts_before_spec` (CLOSE-1) / `..._aborts_before_resolve_close_rate` (CLOSE-2) / `test_to_account_rate_deadline_check_called_before_each_cross_leg` (PROV-1、Task 7) |
| 3 | 論理時計からの独立 | 6 | 5 | `test_gather_open_snapshot_deadline_independent_of_fixed_clock` |
| 4 | 出口のピン | 6 | 15 | `test_commit_pre_gather_deadline_produces_reason_distinct_from_stale` |
| 5 | CLOSE 対称のピン | 6 | 9 | `test_gather_close_snapshot_aborts_before_spec` + `..._aborts_before_resolve_close_rate` (観点 2 の CLOSE-1/CLOSE-2 と同一テストが兼務 — CLOSE 側だけを独立に集めた観点であり、新たに別テストを立てる必要はない) |
| 6 | 脚伝播のピン | 7 | 1, 5 | `test_to_account_rate_deadline_check_called_before_each_cross_leg` (price_provider 側の直接 pin) + `test_cycle_rate_fn_propagates_deadline_check_to_rate_fn` / `test_resolve_close_rate_propagates_deadline_check_to_rate_fn` (Executor 側の配線確認) + `test_gather_close_snapshot_absorbs_cross_leg_deadline_into_degraded_rate` (統合確認) |

**変異 7 種** (確定仕様「変異」節):

| # | spec の記述 | 本書での対応 | Step |
|---|---|---|---|
| 1 | 各検査点を 1 つずつ削除する | M1〜M5 (executor.py の 5 検査点個別) + M10 (price_provider.py の PROV-1) の**計 6 サイト**に分解した (advisor 指摘: spec の「各検査点」は executor 側 5 箇所 + price_provider 側 1 箇所の計 6 箇所であり、まとめて 1 変異にしていない) | Task6 Step17 (M1-M5) / Task7 Step13 (M10) |
| 2 | 予算を `snapshot_max_age_sec` から独立の大きな定数に差し替える | M6 | Task6 Step17 |
| 3 | 経過測定を `monotonic` から `clock` に戻す | M7 | Task6 Step17 |
| 4 | 比較を `>` から `>=` に変える | M8 | Task6 Step17 |
| 5 | `DataUnhealthy` を送出せず `None` を返す | M9 | Task6 Step17 |
| 6 | CLOSE 側の deadline を削除する | **M4 (CLOSE-1 削除) + M5 (CLOSE-2 削除) に部分集合として包含される** — spec 自身が項目 1 と項目 6 を別々に列挙しているが、実装上「CLOSE 側の deadline」は CLOSE-1/CLOSE-2 の 2 検査点そのものであり、別の変異ではなく項目 1 の CLOSE 側 2 サイトと同一。重複計上しないことを明記する (advisor 指摘) | Task6 Step17 (M4, M5) |
| 7 | `to_account_rate` への伝播を削除する | **M10 (price_provider.py の PROV-1 削除) と同一**。加えて Executor 側の配線切断 (M12〜M15) も「伝播を削除する」の亜種として独立に用意した (price_provider 単体が正しくても Executor が配線を通さなければ本番では無効になるため) | Task7 Step13 (M10, M12-M15) |

#### ② プレースホルダの不在確認

本書を再走査し、「TBD」「適切に」「同様に」「以下同様」「(省略)」に類する記述が無いことを確認した。Task 6/Task 7 とも Files/Interfaces/現状/Step 1〜N (テストコード全文・実装コード全文・grep/pytest/git コマンド全文) を単独で完結させて書いており、他 Task を参照する箇所は「Task 6 の `_make_deadline_checker`」のような固有名詞つき参照のみで、内容の省略はない。CLOSE 側テスト (Step 9) は OPEN 側 (Step 5) と構造が対称だが、コードは省略せず全文を再掲した。

#### ③ `price_provider.py` / `executor.py` を触る他 task (9/10/17) との競合ポイント

- **`price_provider.py`**: Task 7 が触るのは `to_account_rate` (:411-464) のみ。束 C の Task 9/10 が触るのは `get_bars`/`_cached_bars`/`_chain`/`_derive`/`_base_candidates`/`_finest_native_base` (:112-351、`_rate_symbol`/`_rate_of`/`to_account_rate` より前の bars 関連メソッド群) — 行範囲は完全に disjoint (409 行目より前が束 C、411 行目以降が束 B)。**それでも同一ファイルなので、束 C の task がこのファイルへ新しいメソッドを `to_account_rate` の直前・直後に挿入すると行番号がずれる** — 本書の「Modify: :411-464」という行指定は束 C 完了後の状態を前提にしており、束 C の実装者が `_SPECS` 辞書や import 文を変更した場合は再確認が必要。親骨格が「束 B は束 C の後」と定めているため、着手直前に `grep -n "def to_account_rate" src/agentic_fx/datafeed/price_provider.py` で実際の行番号を再確認すること。
- **`executor.py`**: Task 6/7 が触るのは `Executor.__init__`・`cycle_rate_fn`・`resolve_close_rate`・`gather_open_snapshot`・`gather_close_snapshot`・新設 `_make_deadline_checker`。Task 17 (束 E) が触るのは `set_gate_result` の呼び出し箇所全 21 (親骨格の既知事実表。**初版の 23 は指揮者の誤記で訂正済み**) — `intents_store.set_gate_result(self.conn, iid, accepted=..., reject_reason=...)` の呼び出し引数に `reject_category` を追加する変更であり、本書が触るメソッドの内部にもこの呼び出しが複数箇所ある (`gather_open_snapshot` 自体には無いが、`_evaluate_and_execute_open`・`close_intent`・`cancel_intent` 等、Task 6/7 が触らない箇所に多数ある)。親骨格が「Task 17 は束 B 完了後」と定めており、本書の変更が確定した行番号を前提に Task 17 が `set_gate_result` の呼び出し箇所を機械的に列挙すればよい。**Task 6/7 は `set_gate_result` の呼び出し自体 (引数の個数・意味) を一切変えていない** — deadline 超過時の拒否経路 (`trade_loop.py` の `snapshot_error` 分岐) は既存の `set_gate_result(self.conn, iid, accepted=False, reject_reason=reasons[0])` をそのまま使う (Task 17 が `reject_category="execution"` を付与する対象にそのまま乗る)。
- **`service.py`**: 親骨格の File Structure 表では Task 5/16 のみが `service.py` を触ると記載されているが、Task 7 は `rate_fn` クロージャ (:447-450) に 1 パラメータを追加する変更が**必須**だった (これを飛ばすと `deadline_check` は Executor/PriceProvider の両方で実装済みでも production では一度も渡らない「単体は緑だが誰からも呼ばれない」状態になる — CLAUDE.md 由来のメモリ「配線そのものを検証する」に照らして見送れない)。Task 5/16 の実装者は `_check_llama_swap`/起動時保持期間検証で `service.py` の別セクションを触るため、行範囲は重ならない見込みだが、実行順序 (束 A/C/D は束 B と並列、束 B は束 C 完了後) の都合上 **Task 5 と Task 7 が同時に `service.py` を編集する可能性がある** — マージ時に diff 競合が起きたら機械的な行結合で解決できる程度の小さな変更 (1 関数のシグネチャ + 1 import 行) であることを申し送る。

#### ④ 初稿本文の誤り (CLOSE 非対称・非損失性恒真) を混入させていないことの確認

- **CLOSE 非対称**: 本書は Task 6 Step 9/11 で `gather_close_snapshot` に OPEN と対称の 2 検査点 (CLOSE-1/CLOSE-2) を実装しており、「CLOSE には入れない」という初稿の記述は一切採用していない。`test_gather_close_snapshot_aborts_before_spec`/`..._aborts_before_resolve_close_rate` の docstring に「初稿の『CLOSE は打ち切らない』(非対称) は改稿 I1 で誤りと判明し撤回された」と明記し、反転させたテストであることを明示した。
- **非損失性が恒真**: 本書は `_make_deadline_checker` の docstring および Task 6 の「現状」節の前後で、「完全な非損失」という主張を一度も使っていない。テスト名も `test_gather_open_snapshot_completes_when_within_budget` (「予算内なら完成する」という限定的な主張) であり、「非損失性が恒真に成立する」という初稿の論証を再掲・引用していない。経過測定は `self.monotonic_fn()` (実測 wall-clock 相当) を使い、`self.clock` (`FixedClock`/`ReplayClock` は進まない) には一切依存させていない — これは改稿 C1 の裁定をそのまま実装した結果であり、初稿の「`clock.now() - captured_at` は必ず gather 所要時間以上」という誤った前提を踏襲していない。
- **最悪 = 予算 + 1 脚**: 本書は Task 7 で `to_account_rate` の `for spec in legs` まで伝播させており、初稿が見落としていた「`cycle_rate(ccy)` 1 回で最大 2 本のネットワーク脚」の問題 (改稿 C2) に対処済み。`_make_deadline_checker` の docstring には「実行中の 1 脚は中断できない」という限界を明記しつつ、「予算 + 1 脚」という数値そのもの (改稿前の過小評価) は記述していない。

#### ⑤ カバーできなかった項目 (正直に列挙)

- **実ネットワーク環境での実測** (spec が「解く問題」で述べる実測 20.5 秒のハングが、本実装で実際に約 10 秒に短縮されることの本番相当の実測) は本書のスコープ外。本書のテストはすべて `monotonic_fn`/`quote_fn`/`spec_fn`/`rate_fn` を注入したユニット/統合テストであり、`llama-swap` や実際の yfinance/MT5 タイムアウトを使った E2E 実測は行っていない。親骨格の Task 20 相当 (旧 spec が「Task 20 の範囲を明確に超える」と述べていた E2E + 受入条件検証) は本プランのどの task にも明示的に割り当てられていないため、必要であれば別途起票が要る。
- **`to_account_rate` の直接/逆ペア (1 脚) 分岐への `deadline_check` 拡大**: 確定仕様は「USD クロスの `for spec in legs` の各脚の前」に伝播範囲を限定しており、本書もそれに従って直接/逆ペアには検査を入れていない (`test_to_account_rate_deadline_check_not_called_for_direct_pair` がこの非対称をピンしている)。1 脚のみのペアでも `_rate_of` → `get_quote` が単独でハングしうる余地は残るが、これは gather 側の OPEN-3/CLOSE-2 検査点 (`cycle_rate(ccy)` 呼び出し全体を包む検査) がカバーする範囲であり、確定仕様の設計判断をそのまま踏襲した。
- **`service.py` の `rate_fn` クロージャ変更に対する専用ユニットテスト**は追加していない (Step 12 は既存の e2e/wiring テストの回帰確認のみ)。`service.py` はこのプロジェクトで直接ユニットテストされる薄い配線層であり (既存の `test_service_app.py` も `build_app` 経由の統合テストが主体)、本書もその慣行に倣ったが、`rate_fn` クロージャが `deadline_check=None` を明示的に転送することだけを確認する軽量な専用テストは無く、必要なら Task 17 着手前に追加を検討する余地がある。


---

## Phase 2 プラン 9 束 C 実装プラン: ohlcv の窓計算・テーブル分割・キャッシュ経路

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**親プラン**: `docs/superpowers/plans/2026-08-11-phase2-9-foundation.md` の束 C (Task 8 → 16 → 9 → 10 → 11)。**本ファイルは束 C の詳細 step のみを持つ。Global Constraints・プラン規約・レビュー段構成は親プランのものがそのまま適用される (再記述しない)。**

**Goal:** キャッシュ経路 (`_cached_bars`) に、ライブ経路と同じ規則の読み込み窓を持たせ、`ohlcv` を `ohlcv_cache` (ライブ逐次キャッシュ・保持ポリシー対象) / `ohlcv_history` (バックテスト・削除しない) の 2 テーブルへ構造的に分割し、最終フォールバック `1m → 1h` を `build_app` の実配線で pin する。

**Architecture:** 5 task を **8 → 16 → 9 → 10 → 11 の直列**で実装する (この順序が実行順序)。Task 8 は窓計算・floor の純関数を新規ファイルに作る (DB 非依存)。Task 16 は `ohlcv` を 2 テーブルへ分割し、書き込み関数ごとに受理 source を allowlist で強制し、保持ポリシー (prune) と起動時の保持期間検証を追加する — **窓計算 (Task 8) より後・`_cached_bars` の書き換え (Task 9/10) より前に置く**、これは分割後の API・テーブル名を前提に Task 9/10 のテスト・実装を書くためで、逆順だと旧テーブル前提のコードを後から作り直すことになる (spec ③ 改訂 5)。Task 9 は `lookback_days` を `_cached_bars` まで配線するだけ (窓計算はまだ適用しない — 配線漏れの検出可能性を保つため独立させる)。Task 10 が実際に窓と floor を `_cached_bars` に適用する。Task 11 は `build_app` の実 factory を使った E2E pin と実データ実測。

**Tech Stack:** Python 3.12 / uv / pytest / sqlite3 (WAL, `PRAGMA foreign_keys=ON`) / pandas (resample)

### 参照文書 (再記述しない — 設計の正はこれら)

| 文書 | 何の正か |
|---|---|
| `docs/superpowers/plans/2026-08-11-phase2-9-foundation.md` | 親プラン。Global Constraints・プラン規約・Interfaces 節 (本束の新 API シグネチャ) |
| `docs/superpowers/specs/2026-08-10-ohlcv-cache-fallback-design.md` (**改訂 5 が本文に優先**) | 束 C の設計の正。検査点 16 個 (本文表) + 改訂 5 の 17 番 |
| `docs/superpowers/specs/2026-08-11-phase2-9-foundation-design.md` D2 | ohlcv 分割・保持ポリシー・保持期間検証の設計・変異リスト |
| `docs/superpowers/specs/2026-07-25-agentic-fx-design.md` §12 | `ohlcv_cache`/`ohlcv_history` のスキーマ定義 |

### 本束の中で確認した既知事実 (実装前に再調査しない)

| 事実 | 場所 | 含意 |
|---|---|---|
| `DatafeedSettings.intervals` の既定は `["1m","1h"]` だが `config/settings.yaml.example` の実値は `[1m, 5m, 15m, 1h, 4h]` | `config.py:93-94` / `settings.yaml.example:44` | 保持期間検証はこの実値 (最大要求 20 日) で起動が通ることを確認する |
| `price_provider.py` の `_base_candidates`/`_finest_native_base`/`DERIVE_ONLY_INTERVALS` は**純粋関数**で `self` を実質使わない | `price_provider.py:33,285-311` | Task 8 は重複実装として `cache_window.py` に同等ロジックを持ち、Task 10 で `PriceProvider` 側を委譲に置き換えて重複を解消する (DRY はタスク境界をまたいで解消してよい — 親プランの File 表は Task 8 が `price_provider.py` を触らないことを前提にしている) |
| `store/ohlcv.py` の `upsert_bars`/`load_bars`/`import_bars`/`load_spread` の呼び出し箇所は `price_provider.py` (4 箇所) と `backtest/importer.py`・`backtest/mt5_import.py` (各 1 箇所) のみ。生 SQL で `ohlcv` を直接叩く箇所は `backtest/replay.py`・`backtest/timeframes.py`・`backtest/holdout.py`・`backtest/cli.py`・`backtest/mt5_import.py` (`compare_sources`) の 5 箇所 | 実コード grep で確認済み (2026-08-11) | Task 16 の書き換え対象を漏れなく列挙済み。詳細は本ファイル末尾の自己レビュー②表 |
| `Scheduler._run_hooks` は `on_signal_maintenance` と同型の「毎 tick 呼ぶ optional フック」パターンを既に持つ | `core/scheduler.py:222-224` | Task 16 の prune 配線は同じパターン (`on_cache_maintenance`) を追加する。既存の `on_signal_maintenance`/`_run_data_hook` は変更しない |
| `_process_exits` の末尾で `settings.pairs` 全件について `bars_fn(pair)` を呼ぶ processed-bar marking が**account の有無・注文の有無に関わらず必ず走る** | `core/scheduler.py:969-977` | Task 11 の E2E pin が「空の注文 DB でも 1m が書かれる」ことの根拠 |
| `build_app` は `quote_fn`/`spec_fn`/`bars_fn`/`provider` を一切注入しなければ、内部で書き込み可能な `PriceProvider(conn_core, settings, clock)` を構築し `bars_fn = provider.latest_1m_bar` を `Scheduler` に渡す | `service.py:399-445,589-597` | Task 11 の pin はこれらの注入点を一切使わない (「本物の配線」を証明するため) |

---

### Task 8: 窓計算ヘルパ + floor (`cache_window.py`)

**対応する spec ③ 検査点**: 2 (native 窓 = lookback_days), 3 (derive 窓 = lookback_days×ratio), 4 の一部 (native 分岐は base 探索そのものを行わない — 統合的な負例は Task 10), 6/7/8/9 の基盤となる `floor_to_interval` の単体挙動, 9 (1d floor が UTC 00:00)。

**設計判断 (このファイル内でのみ確定する事項)**:
- `cache_window.py` は **DB 非依存の純関数モジュール**。`sqlite3.Connection` も `PriceProvider` インスタンスも受け取らない。
- `DERIVE_ONLY_INTERVALS` / `_base_candidates` 相当 / `_finest_native_base` 相当のロジックを**このファイルに新規実装する** (`price_provider.py` の既存実装から重複して書く — 親プランの File 表で Task 8 は `price_provider.py` を触らないため)。**重複は一時的**であり、Task 10 が `PriceProvider` 側をこのモジュールへの委譲に置き換えて解消する (Task 10 の Step 1 で明示)。
- `finest_native_base` は `interval` を導出できるネイティブ足が無ければ `agentic_fx.datafeed.health.DataUnhealthy` を送出する — 呼び出し元 (Task 10 の `_cached_bars`) が既存の `except Exception` パターンでそのまま拾えるようにするため。`health.py` は DB 非依存の純粋な検証モジュールなので、ここからの import は循環を生まない (`health.py` は `price_provider.py`/`cache_window.py` のどちらも import しない)。
- `floor_to_interval` は UTC epoch (`1970-01-01T00:00:00+00:00`) を錨に `((ts - epoch) // width) * width` で計算する。epoch は UTC 深夜 (00:00) なので、この式は **`1d` も含めた全 interval で「UTC 日境界」と一致する** (`datafeed/bars.py:79-102` の `resample(origin="epoch")` と同一の錨、`backtest/timeframes.py:floor_to_bucket` の docstring が同じ事実を明記している — ただし `timeframes.py` は `backtest` 層でありここから import しない。同じ数式を独立に実装する)。

**Files:**
- Create: `src/agentic_fx/datafeed/cache_window.py`
- Test: `tests/datafeed/test_cache_window.py`

**Interfaces:**
- Consumes: `agentic_fx.datafeed.sources.INTERVAL_MIN` (dict[str,float])・`agentic_fx.datafeed.sources.NATIVE_INTERVALS` (dict[str,frozenset[str]])・`agentic_fx.datafeed.health.DataUnhealthy`
- Produces (Task 16 の保持期間検証・Task 10 の `_cached_bars` が consume):
  ```python
  DERIVE_ONLY_INTERVALS: frozenset[str]                          # {"4h", "1d"}
  def base_candidates(interval: str) -> list[str]: ...           # 粗い順
  def finest_native_base(source: str, interval: str) -> str: ... # DataUnhealthy を送出しうる
  def live_window_days(source: str, interval: str, lookback_days: int) -> int: ...
  def floor_to_interval(ts: datetime, interval: str) -> datetime: ...  # naive は ValueError
  ```

- [ ] **Step 1: `tests/datafeed/test_cache_window.py` を新規作成し、native/derive の窓計算の失敗するテストを書く**

```python
from datetime import datetime, timedelta, timezone

import pytest

from agentic_fx.datafeed import cache_window
from agentic_fx.datafeed.health import DataUnhealthy

UTC = timezone.utc


def test_native_interval_window_equals_lookback_days():
    """spec テスト 2: native 要求 (1h/yfinance) の窓は lookback_days そのもの。"""
    assert cache_window.live_window_days("yfinance", "1h", 5) == 5


def test_native_branch_does_not_consult_finest_native_base(monkeypatch):
    """native 分岐は base 探索を一切行わない (spec テスト 4 の基盤: 窓が
    「キャッシュ側の base」に依存しないことの前提は、まず「ライブ側の
    native 判定だけで窓が決まり、base 探索コードパスに触れない」こと)。
    finest_native_base が呼ばれたら AssertionError で検出する。"""
    def _boom(source, interval):
        raise AssertionError("finest_native_base must not be called for a "
                              "native, non-derive-only interval")
    monkeypatch.setattr(cache_window, "finest_native_base", _boom)
    assert cache_window.live_window_days("yfinance", "1h", 5) == 5


def test_derive_interval_window_multiplies_by_ratio():
    """spec テスト 3: 導出要求 (4h/yfinance) の窓は lookback_days × ratio。
    yfinance の 4h は DERIVE_ONLY のため常に導出。base=1h (yfinance の
    ネイティブ) → ratio = 240/60 = 4 → 5 * 4 = 20。"""
    assert cache_window.live_window_days("yfinance", "4h", 5) == 20


def test_derive_ratio_uses_finest_native_base_of_requested_source():
    """mt5 は 4h をネイティブに持つが DERIVE_ONLY 強制のため同じ導出分岐を
    通る。mt5 の base も 1h (mt5 は 1h をネイティブに持つ) なので ratio は
    同じ 4 になる — ソースが違っても導出規則自体は同一であることの確認。"""
    assert cache_window.live_window_days("mt5", "4h", 5) == 20


def test_window_raises_data_unhealthy_when_source_cannot_derive(monkeypatch):
    """source が interval を提供も導出もできなければ DataUnhealthy
    (finest_native_base の契約 — Task 10 の except Exception がそのまま
    拾える形)。実在ソースには常にネイティブ足があるため、合成の空
    NATIVE_INTERVALS を持つ架空 source で再現する。"""
    from agentic_fx.datafeed import sources
    monkeypatch.setitem(sources.NATIVE_INTERVALS, "toy", frozenset())
    with pytest.raises(DataUnhealthy, match="toy"):
        cache_window.live_window_days("toy", "5m", 5)


def test_base_candidates_orders_coarse_to_fine():
    assert cache_window.base_candidates("1h") == ["30m", "15m", "5m", "1m"]


def test_base_candidates_excludes_derive_only_intervals():
    """4h/1d は base 候補にならない (ブローカー格子の裏口混入を防ぐ設計、
    price_provider.py:285-293 と同じ規則)。"""
    assert "4h" not in cache_window.base_candidates("1d")


def test_finest_native_base_picks_coarsest_native_divisor():
    """yfinance で 4h を導出する base は 1h (yfinance にネイティブ)。"""
    assert cache_window.finest_native_base("yfinance", "4h") == "1h"


def test_floor_to_interval_rejects_naive_datetime():
    with pytest.raises(ValueError, match="naive"):
        cache_window.floor_to_interval(datetime(2026, 7, 22, 13, 45),
                                       "1h")


def test_floor_to_interval_1h_floors_down_within_hour():
    ts = datetime(2026, 7, 22, 13, 45, 30, tzinfo=UTC)
    assert cache_window.floor_to_interval(ts, "1h") == \
        datetime(2026, 7, 22, 13, 0, tzinfo=UTC)


def test_floor_to_interval_1d_floors_to_utc_midnight():
    """spec テスト 9: 1d の floor が UTC 00:00 になる。"""
    ts = datetime(2026, 7, 22, 23, 59, 59, tzinfo=UTC)
    assert cache_window.floor_to_interval(ts, "1d") == \
        datetime(2026, 7, 22, 0, 0, tzinfo=UTC)


def test_floor_to_interval_1d_across_month_boundary():
    ts = datetime(2026, 8, 1, 3, 0, tzinfo=UTC)
    assert cache_window.floor_to_interval(ts, "1d") == \
        datetime(2026, 8, 1, 0, 0, tzinfo=UTC)


def test_floor_to_interval_is_idempotent_on_boundary():
    ts = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)
    once = cache_window.floor_to_interval(ts, "1h")
    twice = cache_window.floor_to_interval(once, "1h")
    assert once == twice == ts


def test_floor_to_interval_non_utc_offset_normalizes_to_utc():
    jst = timezone(timedelta(hours=9))
    ts = datetime(2026, 7, 22, 21, 30, tzinfo=jst)  # == 12:30 UTC
    assert cache_window.floor_to_interval(ts, "1h") == \
        datetime(2026, 7, 22, 12, 0, tzinfo=UTC)
```

- [ ] **Step 2: テストを実行し、`cache_window` が存在せず ImportError で落ちることを確認する**

Run: `uv run pytest tests/datafeed/test_cache_window.py -v`
Expected: 全件 FAIL/ERROR (`ModuleNotFoundError: No module named 'agentic_fx.datafeed.cache_window'`)

- [ ] **Step 3: `src/agentic_fx/datafeed/cache_window.py` を実装する**

```python
"""キャッシュ読み込み窓の計算 + 要求 interval への floor。

DB 非依存の純関数モジュール — `sqlite3.Connection` も `PriceProvider`
インスタンスも受け取らない。spec:
docs/superpowers/specs/2026-08-10-ohlcv-cache-fallback-design.md §3.1/3.2
(改訂 5 が本文に優先するが、本ファイルが実装する窓計算・floor の規則
自体は改訂 5 でも不変)。

`_base_candidates`/`_finest_native_base`/`DERIVE_ONLY_INTERVALS` は
`price_provider.py` の既存実装 (Task 8 時点) と同じロジックをここに
新規実装している (一時的な重複 — Task 10 で `PriceProvider` 側をこの
モジュールへの委譲に置き換えて解消する)。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from agentic_fx.datafeed import sources
from agentic_fx.datafeed.health import DataUnhealthy

# 境界がソース依存の足 (price_provider.py:33 と同じ定義)。ネイティブに
# 持っていても常に細かい足から導出する — ブローカー格子の裏口混入を防ぐ。
DERIVE_ONLY_INTERVALS = frozenset({"4h", "1d"})

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def base_candidates(interval: str) -> list[str]:
    """interval を導出できる base 足を、粗い順に返す (ソース非依存)。

    interval_min の約数でなければ境界が合わないため候補から外す。
    DERIVE_ONLY_INTERVALS は base にしない (price_provider.py:285-293 と
    同じ規則 — 1d を「ネイティブ 4h から導出」にするとブローカー格子が
    裏口から入る)。
    """
    want = sources.INTERVAL_MIN[interval]
    cands = [i for i, m in sources.INTERVAL_MIN.items()
             if i not in DERIVE_ONLY_INTERVALS and m < want and want % m == 0]
    return sorted(cands, key=lambda i: sources.INTERVAL_MIN[i], reverse=True)


def finest_native_base(source: str, interval: str) -> str:
    """interval を導出できる、最も粗いネイティブ足を選ぶ。

    (関数名は `finest` だが実際に選ぶのは「最も粗い」足 — price_provider.py
    の既存実装と同じ命名の由来をそのまま引き継ぐ。挙動は docstring の
    とおり。) 提供も導出もできなければ `DataUnhealthy`。
    """
    native = sources.NATIVE_INTERVALS[source]
    for base in base_candidates(interval):
        if base in native:
            return base
    raise DataUnhealthy(f"{source} cannot provide or derive {interval}")


def live_window_days(source: str, interval: str, lookback_days: int) -> int:
    """spec §3.1: キャッシュの読み込み窓を、ライブ経路が同じ要求に対して
    取る期間に揃える。

    窓は要求 (source, interval, lookback_days) だけで決まり、キャッシュ側で
    どの base を使うか (実際に何がキャッシュされているか) には一切依存
    しない — この関数はキャッシュの中身を読まない (DB 非依存)。
    """
    if (interval in sources.NATIVE_INTERVALS[source]
            and interval not in DERIVE_ONLY_INTERVALS):
        return lookback_days
    base = finest_native_base(source, interval)
    ratio = sources.INTERVAL_MIN[interval] / sources.INTERVAL_MIN[base]
    return int(lookback_days * ratio)


def floor_to_interval(ts: datetime, interval: str) -> datetime:
    """UTC epoch 錨のバケット開始時刻へ切り下げる。

    naive は ValueError (fail closed — プロジェクト全体の規律)。epoch
    (1970-01-01T00:00Z) は UTC 深夜なので、この式は 1d も含め全 interval
    で「UTC 日境界」と一致する (`datafeed/bars.py:79-102` の
    `resample(origin="epoch")` と同一の錨)。
    """
    if ts.tzinfo is None:
        raise ValueError(
            "floor_to_interval: naive datetime is rejected "
            "(tz-aware UTC required)")
    ts_utc = ts.astimezone(timezone.utc)
    width = timedelta(minutes=sources.INTERVAL_MIN[interval])
    return _EPOCH + ((ts_utc - _EPOCH) // width) * width
```

- [ ] **Step 4: テストを実行し全件 PASS を確認する**

Run: `uv run pytest tests/datafeed/test_cache_window.py -v`
Expected: 15 passed

- [ ] **Step 5: 全体テストを実行し既存テストが壊れていないことを確認する**

```bash
find . -name __pycache__ -type d -not -path "./.venv/*" -exec rm -rf {} +
uv run pytest -q
```
Expected: 既存件数 + 15 件が passed (この時点では `cache_window.py` はどこからも呼ばれていないため、他モジュールの挙動は不変)

- [ ] **Step 6: 変異を当てて対応するテストが落ちることを確認する (mutation ledger)**

`find . -name __pycache__ -type d -not -path "./.venv/*" -exec rm -rf {} +` を変異の前後で必ず実行する。

| # | 変異 | 対象行 | 殺すテスト |
|---|---|---|---|
| M8-1 | native 分岐で `lookback_days` に無関係な ratio を掛ける (`return lookback_days` → `return lookback_days * 2`) | `live_window_days` の native 分岐 | `test_native_interval_window_equals_lookback_days` |
| M8-2 | 導出分岐で ratio を落とす (`lookback_days * ratio` → `lookback_days`) | `live_window_days` の derive 分岐 | `test_derive_interval_window_multiplies_by_ratio` |
| M8-3 | `base_candidates` から `DERIVE_ONLY_INTERVALS` 除外を外す | `base_candidates` | `test_base_candidates_excludes_derive_only_intervals` |
| M8-4 | `finest_native_base` のループ順を反転 (`sorted(..., reverse=True)` → `reverse=False`) | `base_candidates` | `test_finest_native_base_picks_coarsest_native_divisor` (最も粗い足ではなく最も細かい足が選ばれ 1h ではなく 1m が返る) |
| ~~M8-5~~ | ~~`floor_to_interval` で `astimezone(timezone.utc)` を消す~~ **この変異は等価であり ledger の誤り** (2026-08-12 実測・1 周目 codex 指摘)。`ts - _EPOCH` は aware 同士なので絶対時刻で差を取り、その結果を UTC の `_EPOCH` に足すため**戻り値が変わらない**。実測でフルスイート 1827 passed のまま生存する。**代替**: `ts.astimezone(timezone.utc)` → `ts.replace(tzinfo=timezone.utc)` (オフセットを換算せず付け替えるので JST テストが red になる) | `floor_to_interval` | 代替変異なら `test_floor_to_interval_non_utc_offset_normalizes_to_utc` |
| M8-6 | `floor_to_interval` の naive チェックを消す | `floor_to_interval` | `test_floor_to_interval_rejects_naive_datetime` |
| M8-7 | `_EPOCH` を `1970-01-01T00:00:01` (1 秒ずらす) にする | `_EPOCH` | `test_floor_to_interval_1d_floors_to_utc_midnight` (00:00 ではなく 23:59:59 のバケットが返る) |

各変異を個別に注入し、`grep -n` で改変箇所を目視確認してから該当テストを実行し FAIL を確認、revert して green に戻す。

- [ ] **Step 7: コミット**

```bash
git add src/agentic_fx/datafeed/cache_window.py tests/datafeed/test_cache_window.py
git commit -m "$(cat <<'EOF'
feat: ohlcv キャッシュ窓計算 + floor ヘルパを追加 (プラン9 Task8)

_cached_bars がライブ経路と同じ規則の読み込み窓を持てるよう、
live_window_days/floor_to_interval を DB 非依存の純関数として新設する。
配線は Task 10 で行う (本 task はヘルパ単体のみ)。
EOF
)"
```

---

### Task 16: `ohlcv` の `ohlcv_cache`/`ohlcv_history` 分割 + prune + 保持期間検証

**本プラン最大の task。** spec ③ の設計 (窓計算・floor・source 単位隔離) はここでは変えない — 変わるのは永続化の境界だけ (spec ③ 改訂 5)。

**対応する spec ③ 検査点**: 改訂 5 の 17 番 (履歴テーブルにキャッシュ経路が到達しない — ただし E2E としての実装は Task 11 に置く。本 task は「境界が API で強制されている」ことをストア層の単体テストで担保する)。**設計書 D2 の変異リスト全項目**もここで扱う。

**設計判断 (このファイル内でのみ確定する事項)**:

1. **API はテーブルを引数で選ばせない** — キャッシュ書き込み (`upsert_cache_bars`)・キャッシュ読み (`load_cache_bars`)・履歴書き込み (`import_history_bars`)・履歴読み (`load_history_bars`/`load_history_spread`) を別関数にし、各関数が受理する `source` を allowlist で検証する。**読み側にも allowlist を検証させる** (spec ③ の書き込み側検証だけでなく、読み側でも `source` が正しい集合に属すことを強制する — これが「人間 CLI にライブ source を渡したら fail closed」を構造的に実現する。CLI 引数の `choices=` 制限と二重の防御になる)。
2. `price_provider.py` の呼び出し 4 箇所 (`upsert_bars` ×2、`load_bars` ×1 — 正確には 3 箇所、後述) は**このtask 内で新 API 名に付け替える**。親プランの File 表は `price_provider.py` を Task 7/9/10 の変更対象として挙げているが、Task 16 で `store/ohlcv.py` の関数名そのものを変更する以上、唯一の本番呼び出し元である `price_provider.py` を同時に更新しない限り import が壊れ「8→16→9→10→11 の各 task 完了時点で green を保つ」(プラン規約) を満たせない。**この Step は呼び出し名の機械的な付け替えのみ** (引数・挙動は無変更) — 窓・floor の適用ロジックは Task 9/10 の担当のまま変えない。
3. **DB migration**: 既存の `_migrate_ohlcv_v2` (v1→v2 単一テーブル移行) は無変更で残す。新設の `_migrate_ohlcv_split` を **v2 単一テーブル → `ohlcv_cache`/`ohlcv_history`** の変換として追加し、`init_db` が「`ohlcv` が (v1 として) 残っていれば `_migrate_ohlcv_v2` → 続けて `_migrate_ohlcv_split`」の順で両方呼ぶようにする。**未知 source は履歴側へ** (fail-safe — 削除されないテーブルへ寄せる)。
4. **prune**: `DELETE FROM ohlcv_cache WHERE bar_time < cutoff` のみ (source 絞り不要 — テーブルが分かれているため)。有界バッチ (`LIMIT`) + 毎 maintenance 実行で「追いつくまで」削除する。
5. **保持期間検証**: `service.py:_validate_startup` に追加する。構成済み `datafeed.intervals` × 3 既知チェーン source (`enabled` に関わらず — config だけを見て toggle できる将来の変更に対しても安全側) × 既定 `lookback_days=5` の `live_window_days` 最大値が `datafeed.cache_retention_days` を超えれば起動拒否。
6. **CLI**: `backtest/cli.py` の `history coverage` / `backtest run` / `analyze corr` の `--source` に `choices=sorted(ohlcv.IMPORT_SOURCES)` を追加し、ライブ source 名を渡すと argparse が `SystemExit(2)` で拒否する (`history import` は既に `choices=["dukascopy","mt5"]` で制限済み)。

**Files:**
- Modify: `src/agentic_fx/store/db.py`
- Modify: `src/agentic_fx/store/ohlcv.py`
- Modify: `src/agentic_fx/config.py`
- Modify: `config/settings.yaml.example`
- Modify: `src/agentic_fx/core/scheduler.py`
- Modify: `src/agentic_fx/service.py`
- Modify: `src/agentic_fx/datafeed/price_provider.py` (call-site rename のみ — Step 9)
- Modify: `src/agentic_fx/backtest/importer.py`
- Modify: `src/agentic_fx/backtest/mt5_import.py`
- Modify: `src/agentic_fx/backtest/replay.py`
- Modify: `src/agentic_fx/backtest/timeframes.py`
- Modify: `src/agentic_fx/backtest/holdout.py`
- Modify: `src/agentic_fx/backtest/cli.py`
- Test: `tests/store/test_ohlcv.py` (全面書き換え)
- Test: `tests/store/test_db.py` (該当 10 テストを分割後の終端状態に合わせて書き換え)
- Test: `tests/datafeed/test_price_provider.py` (機械的リネームのみ)
- Test: `tests/store/test_records.py` (機械的リネームのみ)
- Test: `tests/tools/test_mission_registry.py` (機械的リネームのみ)
- Test: `tests/backtest/test_*.py` (機械的リネーム — sed で一括)
- Test: `tests/core/test_scheduler.py` (新規: `on_cache_maintenance` 配線)
- Test: `tests/test_service_app.py` (新規: 保持期間検証・prune 配線)

**Interfaces:**
- Consumes: Task 8 の `cache_window.live_window_days`
- Produces (Task 9/10/11 が consume — 親プラン Interfaces 節と同一):
  ```python
  # src/agentic_fx/store/ohlcv.py
  LIVE_SOURCES: frozenset[str]                      # {"yfinance","twelvedata","mt5-live"} (既存維持)
  IMPORT_SOURCES: frozenset[str]                    # {"dukascopy", "mt5"}
  KNOWN_OHLCV_SOURCES: frozenset[str]               # LIVE_SOURCES | IMPORT_SOURCES (既存の値と同一)
  def upsert_cache_bars(conn, bars: list[Bar], *, source: str) -> int: ...
  def load_cache_bars(conn, symbol: str, interval: str, *, source: str,
                      since: datetime | None = None,
                      until: datetime | None = None) -> list[Bar]: ...
  def prune_cache(conn, *, cutoff: datetime, limit: int) -> int: ...   # 削除行数を返す
  def import_history_bars(conn, rows: list[tuple], *, source: str) -> ImportResult: ...
  def load_history_bars(conn, symbol: str, interval: str, *, source: str,
                        since: datetime | None = None,
                        until: datetime | None = None) -> list[Bar]: ...
  def load_history_spread(conn, symbol: str, interval: str,
                          bar_time_iso: str, *, source: str) -> float | None: ...
  ```

#### Step 群 A: `store/ohlcv.py` の新 API を先にテストで固定する (red)

- [ ] **Step 1: `tests/store/test_ohlcv.py` を新 API 前提で全面書き換えする**

```python
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from agentic_fx.core.contracts import Bar
from agentic_fx.store import ohlcv
from agentic_fx.store.db import connect, init_db

NOW_ISO = "2026-07-22T12:00:00+00:00"
ROW = ("USDJPY", "1m", NOW_ISO, 148.0, 148.2, 147.9, 148.1, 100.0, 0.012)


def _conn(tmp_path):
    conn = connect(tmp_path / "t.db")
    init_db(conn)
    return conn


# ---- IMPORT_SOURCES / LIVE_SOURCES の対称性 --------------------------------

def test_import_sources_and_live_sources_are_disjoint():
    """D2 設計の中核: mt5 (履歴) と mt5-live (キャッシュ) が同じ集合に
    絶対に入らないこと。"""
    assert ohlcv.IMPORT_SOURCES.isdisjoint(ohlcv.LIVE_SOURCES)
    assert ohlcv.IMPORT_SOURCES == frozenset({"dukascopy", "mt5"})
    assert ohlcv.LIVE_SOURCES == frozenset({"yfinance", "twelvedata", "mt5-live"})


def test_known_ohlcv_sources_is_the_union():
    assert ohlcv.KNOWN_OHLCV_SOURCES == ohlcv.LIVE_SOURCES | ohlcv.IMPORT_SOURCES


# ---- import_history_bars (旧 import_bars) ----------------------------------

def test_import_history_bars_inserts_and_is_idempotent(tmp_path):
    conn = _conn(tmp_path)
    r1 = ohlcv.import_history_bars(conn, [ROW], source="dukascopy")
    r2 = ohlcv.import_history_bars(conn, [ROW], source="dukascopy")
    assert (r1.inserted, r1.unchanged, r1.conflicted) == (1, 0, 0)
    assert (r2.inserted, r2.unchanged, r2.conflicted) == (0, 1, 0)


def test_import_history_bars_writes_to_ohlcv_history_table(tmp_path):
    conn = _conn(tmp_path)
    ohlcv.import_history_bars(conn, [ROW], source="dukascopy")
    assert conn.execute("SELECT COUNT(*) FROM ohlcv_history").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM ohlcv_cache").fetchone()[0] == 0


def test_import_history_bars_rejects_live_source(tmp_path):
    """D2 変異リスト: 履歴インポートにライブ source 名を渡しても通って
    しまうと、キャッシュ由来行が履歴テーブルに紛れ込む。"""
    conn = _conn(tmp_path)
    with pytest.raises(ValueError, match="IMPORT_SOURCES"):
        ohlcv.import_history_bars(conn, [ROW], source="yfinance")
    with pytest.raises(ValueError, match="IMPORT_SOURCES"):
        ohlcv.import_history_bars(conn, [ROW], source="mt5-live")
    assert conn.execute("SELECT COUNT(*) FROM ohlcv_history").fetchone()[0] == 0


def test_import_history_bars_never_mutates_existing(tmp_path):
    """既存行と値が異なる入力は棄却 — spec §6「既存行は不変」。"""
    conn = _conn(tmp_path)
    ohlcv.import_history_bars(conn, [ROW], source="dukascopy")
    tampered = ROW[:6] + (148.15,) + ROW[7:]
    r = ohlcv.import_history_bars(conn, [tampered], source="dukascopy")
    assert r.conflicted == 1
    assert conn.execute(
        "SELECT close FROM ohlcv_history").fetchone()[0] == 148.1


def test_import_history_bars_spread_null_vs_value_conflicts(tmp_path):
    conn = _conn(tmp_path)
    no_spread = ROW[:8] + (None,)
    ohlcv.import_history_bars(conn, [no_spread], source="dukascopy")
    r_same = ohlcv.import_history_bars(conn, [no_spread], source="dukascopy")
    assert (r_same.inserted, r_same.unchanged, r_same.conflicted) == (0, 1, 0)
    with_spread = ROW  # spread=0.012
    r_conflict = ohlcv.import_history_bars(conn, [with_spread], source="dukascopy")
    assert r_conflict.conflicted == 1
    assert conn.execute(
        "SELECT spread FROM ohlcv_history").fetchone()[0] is None


def test_import_history_bars_float_tolerance_within_1e9_is_unchanged(tmp_path):
    conn = _conn(tmp_path)
    ohlcv.import_history_bars(conn, [ROW], source="dukascopy")
    almost_same = ROW[:3] + (ROW[3] + 1e-10,) + ROW[4:]
    r = ohlcv.import_history_bars(conn, [almost_same], source="dukascopy")
    assert (r.inserted, r.unchanged, r.conflicted) == (0, 1, 0)


def test_import_history_bars_conflicted_logs_warning(tmp_path, caplog):
    conn = _conn(tmp_path)
    ohlcv.import_history_bars(conn, [ROW], source="dukascopy")
    tampered = ROW[:6] + (148.15,) + ROW[7:]
    with caplog.at_level("WARNING"):
        ohlcv.import_history_bars(conn, [tampered], source="dukascopy")
    assert any("conflict" in r.message.lower() for r in caplog.records)


def test_import_history_bars_rejects_naive_bar_time(tmp_path):
    conn = _conn(tmp_path)
    naive = ROW[:2] + ("2026-07-22T12:00:00",) + ROW[3:]
    with pytest.raises(ValueError, match="aware"):
        ohlcv.import_history_bars(conn, [naive], source="dukascopy")
    assert conn.execute("SELECT COUNT(*) FROM ohlcv_history").fetchone()[0] == 0


def test_import_history_bars_normalizes_bar_time_representation(tmp_path):
    conn = _conn(tmp_path)
    ohlcv.import_history_bars(conn, [ROW], source="dukascopy")
    same_instant_different_repr = (
        ROW[0], ROW[1], "2026-07-22T21:00:00+09:00") + ROW[3:]
    r = ohlcv.import_history_bars(conn, [same_instant_different_repr],
                                  source="dukascopy")
    assert r.unchanged == 1
    assert conn.execute("SELECT COUNT(*) FROM ohlcv_history").fetchone()[0] == 1


def test_import_history_bars_rejects_ohlc_ordering_violation(tmp_path):
    conn = _conn(tmp_path)
    broken = ROW[:3] + (999.0,) + ROW[4:]
    with pytest.raises(ValueError, match="low<="):
        ohlcv.import_history_bars(conn, [broken], source="dukascopy")
    assert conn.execute("SELECT COUNT(*) FROM ohlcv_history").fetchone()[0] == 0


def test_import_history_bars_rejects_non_finite_values(tmp_path):
    conn = _conn(tmp_path)
    broken = ROW[:3] + (float("nan"),) + ROW[4:]
    with pytest.raises(ValueError, match="finite"):
        ohlcv.import_history_bars(conn, [broken], source="dukascopy")


def test_import_history_bars_rejects_empty_source(tmp_path):
    conn = _conn(tmp_path)
    with pytest.raises(ValueError, match="IMPORT_SOURCES"):
        ohlcv.import_history_bars(conn, [ROW], source="")


def test_import_history_bars_batch_rejects_whole_input_on_late_bad_row(tmp_path):
    conn = _conn(tmp_path)
    good = ROW
    bad = ROW[:3] + (999.0,) + ROW[4:]
    with pytest.raises(ValueError):
        ohlcv.import_history_bars(conn, [good, bad], source="dukascopy")
    assert conn.execute("SELECT COUNT(*) FROM ohlcv_history").fetchone()[0] == 0


def test_import_history_bars_rolls_back_on_mid_batch_write_failure(tmp_path):
    from agentic_fx.store import ohlcv as ohlcv_module

    conn = _conn(tmp_path)
    row2 = ("USDJPY", "5m", NOW_ISO, 1.0, 2.0, 0.5, 1.5, 10.0, None)

    class _FailingConn:
        def __init__(self, real):
            self._real = real
            self._insert_count = 0

        def execute(self, sql, *args, **kwargs):
            if str(sql).strip().startswith(
                    "INSERT OR IGNORE INTO ohlcv_history "):
                self._insert_count += 1
                if self._insert_count == 2:
                    raise sqlite3.OperationalError("injected failure")
            return self._real.execute(sql, *args, **kwargs)

        def __getattr__(self, name):
            return getattr(self._real, name)

    with pytest.raises(sqlite3.OperationalError):
        ohlcv_module.import_history_bars(_FailingConn(conn), [ROW, row2],
                                         source="dukascopy")

    assert conn.execute("SELECT COUNT(*) FROM ohlcv_history").fetchone()[0] == 0

    conn.execute(
        "INSERT OR IGNORE INTO ohlcv_history (symbol, interval, bar_time, "
        "open, high, low, close, volume, source, spread) "
        "VALUES ('EURUSD','1h','2026-07-22T13:00:00+00:00',"
        "1,2,0.5,1.5,10,'dukascopy',NULL)")
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM ohlcv_history").fetchone()[0] == 1
    assert conn.execute(
        "SELECT symbol FROM ohlcv_history").fetchone()[0] == "EURUSD"


# ---- load_history_bars (旧 load_bars、source=履歴専用) ---------------------

def test_load_history_bars_filters_by_source(tmp_path):
    conn = _conn(tmp_path)
    ohlcv.import_history_bars(conn, [ROW], source="dukascopy")
    ohlcv.import_history_bars(
        conn, [("USDJPY", "1m", NOW_ISO, 1, 2, 0.5, 1.5, 0, None)],
        source="mt5")
    assert len(ohlcv.load_history_bars(
        conn, "USDJPY", "1m", source="dukascopy")) == 1
    assert ohlcv.load_history_bars(
        conn, "USDJPY", "1m", source="dukascopy")[0].open == 148.0


def test_load_history_bars_rejects_live_source(tmp_path):
    """人間 CLI がライブ source を渡したら fail closed — 構造的にストア層で
    強制する (CLI の argparse choices= はこれを補強する第二の壁)。"""
    conn = _conn(tmp_path)
    with pytest.raises(ValueError, match="IMPORT_SOURCES"):
        ohlcv.load_history_bars(conn, "USDJPY", "1m", source="yfinance")


def test_load_history_bars_rejects_naive_since(tmp_path):
    conn = _conn(tmp_path)
    ohlcv.import_history_bars(conn, [ROW], source="dukascopy")
    with pytest.raises(ValueError, match="naive"):
        ohlcv.load_history_bars(conn, "USDJPY", "1m", source="dukascopy",
                                since=datetime(2026, 7, 22))


def test_load_history_bars_rejects_naive_until(tmp_path):
    conn = _conn(tmp_path)
    ohlcv.import_history_bars(conn, [ROW], source="dukascopy")
    with pytest.raises(ValueError, match="naive"):
        ohlcv.load_history_bars(conn, "USDJPY", "1m", source="dukascopy",
                                until=datetime(2026, 7, 22))


def test_load_history_bars_jst_since_matches_utc_instant(tmp_path):
    conn = _conn(tmp_path)
    ohlcv.import_history_bars(conn, [ROW], source="dukascopy")
    jst = timezone(timedelta(hours=9))
    since_jst = datetime.fromisoformat(NOW_ISO).astimezone(jst)
    bars_jst = ohlcv.load_history_bars(conn, "USDJPY", "1m", source="dukascopy",
                                       since=since_jst)
    bars_utc = ohlcv.load_history_bars(conn, "USDJPY", "1m", source="dukascopy",
                                       since=datetime.fromisoformat(NOW_ISO))
    assert len(bars_jst) == 1
    assert bars_jst == bars_utc


def test_load_history_spread_returns_value(tmp_path):
    conn = _conn(tmp_path)
    ohlcv.import_history_bars(conn, [ROW], source="dukascopy")
    assert ohlcv.load_history_spread(conn, "USDJPY", "1m", NOW_ISO,
                                     source="dukascopy") == 0.012


def test_load_history_spread_none_when_missing(tmp_path):
    conn = _conn(tmp_path)
    assert ohlcv.load_history_spread(conn, "USDJPY", "1m", NOW_ISO,
                                     source="dukascopy") is None


def test_load_history_spread_rejects_live_source(tmp_path):
    conn = _conn(tmp_path)
    with pytest.raises(ValueError, match="IMPORT_SOURCES"):
        ohlcv.load_history_spread(conn, "USDJPY", "1m", NOW_ISO,
                                  source="mt5-live")


def test_load_history_spread_distinguishes_intervals_sharing_bar_time(tmp_path):
    conn = _conn(tmp_path)
    row_1h = ("USDJPY", "1h", NOW_ISO, 148.0, 148.2, 147.9, 148.1, 100.0, 0.02)
    row_1m = ("USDJPY", "1m", NOW_ISO, 148.0, 148.2, 147.9, 148.1, 100.0, 0.01)
    ohlcv.import_history_bars(conn, [row_1h], source="dukascopy")
    ohlcv.import_history_bars(conn, [row_1m], source="dukascopy")
    assert ohlcv.load_history_spread(conn, "USDJPY", "1h", NOW_ISO,
                                     source="dukascopy") == 0.02
    assert ohlcv.load_history_spread(conn, "USDJPY", "1m", NOW_ISO,
                                     source="dukascopy") == 0.01


# ---- upsert_cache_bars / load_cache_bars (旧 upsert_bars/load_bars) --------

def test_upsert_cache_bars_writes_to_ohlcv_cache_table(tmp_path):
    conn = _conn(tmp_path)
    b = Bar("USDJPY", "1m", datetime.fromisoformat(NOW_ISO), 1, 2, 0.5, 1.5, 0)
    ohlcv.upsert_cache_bars(conn, [b], source="yfinance")
    assert conn.execute("SELECT COUNT(*) FROM ohlcv_cache").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM ohlcv_history").fetchone()[0] == 0


def test_upsert_cache_bars_still_overwrites_live_cache(tmp_path):
    """live キャッシュ (yfinance) は形成中バー更新のため上書き — 現行維持。"""
    conn = _conn(tmp_path)
    b = Bar("USDJPY", "1m", datetime.fromisoformat(NOW_ISO), 1, 2, 0.5, 1.5, 0)
    ohlcv.upsert_cache_bars(conn, [b], source="yfinance")
    ohlcv.upsert_cache_bars(
        conn, [Bar("USDJPY", "1m", b.ts, 1, 3, 0.5, 2.5, 9)], source="yfinance")
    row = conn.execute("SELECT high, source FROM ohlcv_cache").fetchone()
    assert row["high"] == 3 and row["source"] == "yfinance"


def test_upsert_cache_bars_normalizes_bar_time_to_utc(tmp_path):
    conn = _conn(tmp_path)
    jst_ts = datetime.fromisoformat("2026-07-22T21:00:00+09:00")  # == NOW_ISO
    b = Bar("USDJPY", "1m", jst_ts, 1, 2, 0.5, 1.5, 0)
    ohlcv.upsert_cache_bars(conn, [b], source="yfinance")
    row = conn.execute("SELECT bar_time FROM ohlcv_cache").fetchone()
    assert row["bar_time"] == NOW_ISO
    loaded = ohlcv.load_cache_bars(conn, "USDJPY", "1m", source="yfinance")
    assert len(loaded) == 1 and loaded[0].ts == datetime.fromisoformat(NOW_ISO)


def test_upsert_cache_bars_different_sources_coexist(tmp_path):
    conn = _conn(tmp_path)
    b = Bar("USDJPY", "1m", datetime.fromisoformat(NOW_ISO), 1, 2, 0.5, 1.5, 0)
    ohlcv.upsert_cache_bars(conn, [b], source="yfinance")
    ohlcv.upsert_cache_bars(conn, [b], source="mt5-live")
    assert conn.execute("SELECT COUNT(*) FROM ohlcv_cache").fetchone()[0] == 2


def test_upsert_cache_bars_rejects_non_live_source(tmp_path):
    """live/import 境界を API 契約で強制する。"""
    conn = _conn(tmp_path)
    b = Bar("USDJPY", "1m", datetime.fromisoformat(NOW_ISO), 1, 2, 0.5, 1.5, 0)
    with pytest.raises(ValueError, match="LIVE_SOURCES"):
        ohlcv.upsert_cache_bars(conn, [b], source="dukascopy")
    with pytest.raises(ValueError, match="LIVE_SOURCES"):
        ohlcv.upsert_cache_bars(conn, [b], source="mt5")
    assert conn.execute("SELECT COUNT(*) FROM ohlcv_cache").fetchone()[0] == 0


def test_upsert_cache_bars_rejects_naive_bar_time(tmp_path):
    conn = _conn(tmp_path)
    naive_bar = Bar("USDJPY", "1m", datetime(2026, 7, 22, 12, 0),
                    1, 2, 0.5, 1.5, 0)
    with pytest.raises(ValueError, match="naive"):
        ohlcv.upsert_cache_bars(conn, [naive_bar], source="yfinance")
    assert conn.execute("SELECT COUNT(*) FROM ohlcv_cache").fetchone()[0] == 0


def test_load_cache_bars_rejects_history_source(tmp_path):
    conn = _conn(tmp_path)
    with pytest.raises(ValueError, match="LIVE_SOURCES"):
        ohlcv.load_cache_bars(conn, "USDJPY", "1m", source="dukascopy")


def test_load_cache_bars_rejects_naive_since(tmp_path):
    conn = _conn(tmp_path)
    b = Bar("USDJPY", "1m", datetime.fromisoformat(NOW_ISO), 1, 2, 0.5, 1.5, 0)
    ohlcv.upsert_cache_bars(conn, [b], source="yfinance")
    with pytest.raises(ValueError, match="naive"):
        ohlcv.load_cache_bars(conn, "USDJPY", "1m", source="yfinance",
                              since=datetime(2026, 7, 22))


def test_ohlcv_cache_table_has_no_spread_column(tmp_path):
    """設計書 §12: ohlcv_cache は spread 列を持たない (ライブ経路は埋め
    ないため常に NULL だった不整合そのものを消す)。"""
    conn = _conn(tmp_path)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(ohlcv_cache)")}
    assert "spread" not in cols


# ---- prune_cache ------------------------------------------------------------

def _cache_row(conn, bar_time_iso: str, source="yfinance"):
    b = Bar("USDJPY", "1m", datetime.fromisoformat(bar_time_iso),
           1, 2, 0.5, 1.5, 0)
    ohlcv.upsert_cache_bars(conn, [b], source=source)


def test_prune_cache_deletes_rows_older_than_cutoff(tmp_path):
    conn = _conn(tmp_path)
    _cache_row(conn, "2026-07-01T00:00:00+00:00")
    _cache_row(conn, "2026-07-22T00:00:00+00:00")
    cutoff = datetime(2026, 7, 15, tzinfo=timezone.utc)
    n = ohlcv.prune_cache(conn, cutoff=cutoff, limit=1000)
    assert n == 1
    assert conn.execute("SELECT COUNT(*) FROM ohlcv_cache").fetchone()[0] == 1
    remaining = conn.execute("SELECT bar_time FROM ohlcv_cache").fetchone()[0]
    assert remaining == "2026-07-22T00:00:00+00:00"


def test_prune_cache_never_touches_ohlcv_history(tmp_path):
    """D2 最重要変異の対偶: prune はどんな cutoff/limit でも履歴に到達
    できない (テーブルが分かれているため)。"""
    conn = _conn(tmp_path)
    ohlcv.import_history_bars(conn, [ROW], source="dukascopy")  # 2026-07-22
    far_future = datetime(2099, 1, 1, tzinfo=timezone.utc)
    ohlcv.prune_cache(conn, cutoff=far_future, limit=1000)
    assert conn.execute("SELECT COUNT(*) FROM ohlcv_history").fetchone()[0] == 1


def test_prune_cache_respects_limit(tmp_path):
    conn = _conn(tmp_path)
    for i in range(5):
        _cache_row(conn, f"2026-07-0{i+1}T00:00:00+00:00")
    cutoff = datetime(2026, 7, 22, tzinfo=timezone.utc)
    n = ohlcv.prune_cache(conn, cutoff=cutoff, limit=2)
    assert n == 2
    assert conn.execute("SELECT COUNT(*) FROM ohlcv_cache").fetchone()[0] == 3


def test_prune_cache_repeated_calls_converge(tmp_path):
    """日次投入量が 1 batch を超えても複数 maintenance/日で追いつく。"""
    conn = _conn(tmp_path)
    cutoff = datetime(2026, 7, 22, tzinfo=timezone.utc)
    batch_limit = 3
    daily_ingest = 7
    assert daily_ingest > batch_limit
    for day in range(3):
        for i in range(daily_ingest):
            _cache_row(conn, f"2026-06-{day * daily_ingest + i + 1:02d}T00:00:00+00:00")
        # scheduler は日次一回ではなく maintenance ごとに呼ぶ。3 回なら
        # capacity=9 > daily_ingest=7 となり、その日の backlog が消える。
        for _ in range(3):
            ohlcv.prune_cache(conn, cutoff=cutoff, limit=batch_limit)
        # 各日の終わりに backlog が消えていること (これが収束の本体)
        assert conn.execute("SELECT COUNT(*) FROM ohlcv_cache").fetchone()[0] == 0


def test_prune_cache_rejects_naive_cutoff(tmp_path):
    conn = _conn(tmp_path)
    with pytest.raises(ValueError, match="naive"):
        ohlcv.prune_cache(conn, cutoff=datetime(2026, 7, 22), limit=10)


def test_prune_cache_rejects_non_positive_limit(tmp_path):
    conn = _conn(tmp_path)
    with pytest.raises(ValueError, match="limit"):
        ohlcv.prune_cache(conn, cutoff=datetime(2026, 7, 22, tzinfo=timezone.utc),
                          limit=0)
```

- [ ] **Step 2: テストを実行し、新関数が存在せず AttributeError で落ちることを確認する**

Run: `uv run pytest tests/store/test_ohlcv.py -v`
Expected: 新 API を直接要求するテストは FAIL/ERROR
(`AttributeError: ... import_history_bars` 等)。入力検証など変更前から成立する
回帰 pin は green でよく、「全件 FAIL」は要求しない。

#### Step 群 B: `store/db.py` の DDL + migration

- [ ] **Step 3: `src/agentic_fx/store/db.py` の DDL を `ohlcv_cache`/`ohlcv_history` の 2 テーブルへ差し替える**

`_OHLCV_V2_DDL` は既存 `_migrate_ohlcv_v2` が参照するため**モジュール定数として
残す**。`_SCHEMA` の連結からだけ外し、以下の 2 定数を追加して先頭部分を
`_OHLCV_CACHE_DDL + _OHLCV_HISTORY_DDL` に置き換える:

```python
_OHLCV_CACHE_DDL = """
CREATE TABLE IF NOT EXISTS ohlcv_cache (
  symbol TEXT NOT NULL, interval TEXT NOT NULL, bar_time TEXT NOT NULL,
  open REAL NOT NULL, high REAL NOT NULL, low REAL NOT NULL,
  close REAL NOT NULL, volume REAL NOT NULL DEFAULT 0,
  source TEXT NOT NULL,
  PRIMARY KEY (symbol, interval, bar_time, source)
);
"""

_OHLCV_HISTORY_DDL = """
CREATE TABLE IF NOT EXISTS ohlcv_history (
  symbol TEXT NOT NULL, interval TEXT NOT NULL, bar_time TEXT NOT NULL,
  open REAL NOT NULL, high REAL NOT NULL, low REAL NOT NULL,
  close REAL NOT NULL, volume REAL NOT NULL DEFAULT 0,
  source TEXT NOT NULL, spread REAL,
  PRIMARY KEY (symbol, interval, bar_time, source)
);
"""

_SCHEMA = _OHLCV_CACHE_DDL + _OHLCV_HISTORY_DDL + """
CREATE TABLE IF NOT EXISTS missions (
...  -- (以下、既存の missions 以降は無変更)
```

`TABLE_NAMES` を更新する:

```python
TABLE_NAMES = frozenset({
    "ohlcv_cache", "ohlcv_history", "missions", "trade_intents", "orders",
    "reflections", "account_snapshots", "improvement_backlog",
    "improvement_runs", "econ_events", "approval_requests", "news_sources",
    "backtest_runs", "analysis_runs", "signals",
})
```

- [ ] **Step 4: `_backup_before_migration` の suffix を引数化する (既存の v1→v2 移行と挙動互換)**

```python
def _backup_before_migration(conn: sqlite3.Connection, suffix: str) -> None:
    """移行が必要と判定された時だけ、移行前スナップショットを取る。
    `suffix` は移行の種類ごとに別ファイルにするための識別子
    (Task 16: v1→v2 移行と v2→split 移行を別バックアップにする)。"""
    main = next((r for r in conn.execute("PRAGMA database_list")
                if r["name"] == "main"), None)
    db_file = main["file"] if main is not None else ""
    if not db_file:
        return
    src_path = Path(db_file)
    bak_path = src_path.with_name(src_path.name + suffix)
    if bak_path.exists():
        return
    bak_conn = sqlite3.connect(bak_path)
    try:
        conn.backup(bak_conn)
    finally:
        bak_conn.close()
    _log.warning("ohlcv 移行前のバックアップを作成しました: %s", bak_path)
```

`_migrate_ohlcv_v2` 内の唯一の呼び出し箇所を更新する:

```python
    _backup_before_migration(conn, ".bak-ohlcv-v2")
```

(この 1 行だけの変更 — `_migrate_ohlcv_v2` の残りのロジックは無変更)

- [ ] **Step 5: `_migrate_ohlcv_split` を新設する**

```python
def _migrate_ohlcv_split(conn: sqlite3.Connection) -> None:
    """v2 単一テーブル `ohlcv` (source 列あり) を `ohlcv_cache`/
    `ohlcv_history` へ分割する (プラン 9 Task 16。設計書 §12「キャッシュと
    履歴をテーブルで分ける」)。

    source が LIVE_SOURCES ならキャッシュへ、それ以外 (IMPORT_SOURCES を
    含む) は履歴へ — **未知 source は履歴側へ隔離する** (fail-safe。履歴
    側は削除しないため、判断を誤っても失われない。設計書 §12 移行方針)。

    `_migrate_ohlcv_v2` と同型の再開安全パターン: BEGIN IMMEDIATE →
    存在検査 (無ければ既に完了 済み — 何もせず抜ける) → INSERT OR IGNORE →
    値一致検証 (F3 相当 — 無視された行が「同じ値だから」か「破損/想定外
    書き込みと衝突したから」かを区別する) → DROP → COMMIT。失敗時 ROLLBACK。

    `LIVE_SOURCES` は store/ohlcv.py にある (関数内 import — db.py と
    ohlcv.py の間に将来循環 import が生じても壊れないようにする防御的
    パターン。config.py の INTERVAL_MIN 関数内 import と同じ流儀)。
    """
    from agentic_fx.store.ohlcv import LIVE_SOURCES

    _backup_before_migration(conn, ".bak-ohlcv-split")
    conn.execute("BEGIN IMMEDIATE")
    try:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='ohlcv'").fetchone() is not None
        if not exists:
            conn.commit()
            return
        rows = conn.execute(
            "SELECT symbol, interval, bar_time, open, high, low, close, "
            "volume, source, spread FROM ohlcv").fetchall()
        for r in rows:
            if r["source"] in LIVE_SOURCES:
                conn.execute(
                    "INSERT OR IGNORE INTO ohlcv_cache (symbol, interval, "
                    "bar_time, open, high, low, close, volume, source) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    (r["symbol"], r["interval"], r["bar_time"], r["open"],
                     r["high"], r["low"], r["close"], r["volume"],
                     r["source"]))
            else:
                conn.execute(
                    "INSERT OR IGNORE INTO ohlcv_history (symbol, interval, "
                    "bar_time, open, high, low, close, volume, source, "
                    "spread) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (r["symbol"], r["interval"], r["bar_time"], r["open"],
                     r["high"], r["low"], r["close"], r["volume"],
                     r["source"], r["spread"]))

        mismatches = []
        for r in rows:
            target = "ohlcv_cache" if r["source"] in LIVE_SOURCES \
                else "ohlcv_history"
            existing = conn.execute(
                f"SELECT open, high, low, close, volume FROM {target} "
                "WHERE symbol=? AND interval=? AND bar_time=? AND source=?",
                (r["symbol"], r["interval"], r["bar_time"],
                 r["source"])).fetchone()
            ok = (existing is not None
                  and _values_match(existing["open"], r["open"])
                  and _values_match(existing["high"], r["high"])
                  and _values_match(existing["low"], r["low"])
                  and _values_match(existing["close"], r["close"])
                  and _values_match(existing["volume"], r["volume"]))
            if not ok:
                mismatches.append(
                    f"{r['symbol']}/{r['interval']}/{r['bar_time']}/"
                    f"{r['source']}")
        if mismatches:
            raise RuntimeError(
                "ohlcv split migration: ohlcv と ohlcv_cache/ohlcv_history で"
                f"値が一致しない行があります ({mismatches[0]})。ohlcv は"
                "温存しました。data/agentic.db.bak-ohlcv-split (または手動"
                "バックアップ) からの復元と手動調査が必要です。")

        conn.execute("DROP TABLE ohlcv")
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
```

> **FK 再構築の警告:** この RENAME 先行形は `ohlcv` を参照する外部キーが
> 無いから成立する。参照される側を再構築すると SQLite が参照元 DDL を旧名へ
> 書き換えるため、必ず「`name_new` を CREATE → コピー →旧 `name` を DROP →
> `name_new` を `name` へ RENAME」の順序を使う。Task 17 はこの順序で実装する。

- [ ] **Step 6: `init_db` を新しい migration チェーンで書き換える**

```python
def _ohlcv_legacy_exists(conn: sqlite3.Connection) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' "
        "AND name='ohlcv'").fetchone() is not None


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)
    if _ohlcv_legacy_exists(conn):
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(ohlcv)")}
        v1_leftover = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='ohlcv_v1'").fetchone() is not None
        if "source" not in cols or v1_leftover:
            _migrate_ohlcv_v2(conn)     # v1 → v2 (既存、無変更)
        _migrate_ohlcv_split(conn)      # v2 → ohlcv_cache/ohlcv_history (新設)
    _ensure_column(conn, "missions", "trigger", "trigger TEXT")
    conn.commit()
```

**重要な順序の注記**: `_migrate_ohlcv_v2` 自体の内部再検査 (F2) は「`ohlcv` に `source` 列があり `ohlcv_v1` が無い」を「完全に v2 移行済み」とみなして何もせず抜けるが、それでも `ohlcv` テーブル自体は残る (v1→v2 は同じテーブル名を rebuild するだけ)。そのため `_migrate_ohlcv_v2` を呼んだかどうかに関わらず、`_ohlcv_legacy_exists` が真である限り必ず `_migrate_ohlcv_split` を呼ぶ (v2 の `ohlcv` が残っていれば分割する)。

- [ ] **Step 7: `tests/store/test_db.py` を新しい終端状態に合わせて書き換える**

`EXPECTED` 集合を更新する:

```python
EXPECTED = {
    "ohlcv_cache", "ohlcv_history", "missions", "trade_intents", "orders",
    "reflections", "account_snapshots", "improvement_backlog",
    "improvement_runs", "econ_events", "approval_requests", "news_sources",
    "backtest_runs", "analysis_runs", "signals",
}
```

`test_init_creates_all_14_tables` の関数名とコメントを実数 (15) に合わせる:

```python
def test_init_creates_all_15_tables(tmp_path):
    conn = connect(tmp_path / "agentic.db")
    init_db(conn)
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%'").fetchall()
    assert {r["name"] for r in rows} == EXPECTED
    assert TABLE_NAMES == frozenset(EXPECTED)
```

`test_init_db_migrates_legacy_ohlcv_to_v2` を「v1 → 分割後の `ohlcv_cache`」の終端状態を検査する内容に書き換える (関数名も変える):

```python
def test_init_db_migrates_legacy_v1_ohlcv_into_cache_table(tmp_path):
    """旧 v1 PK (symbol,interval,bar_time) の ohlcv が、v2 (source 込み PK)
    を経て ohlcv_cache (source='yfinance' 合成) へ収束する。"""
    conn = connect(tmp_path / "legacy.db")
    conn.execute(
        "CREATE TABLE ohlcv (symbol TEXT NOT NULL, interval TEXT NOT NULL, "
        "bar_time TEXT NOT NULL, open REAL NOT NULL, high REAL NOT NULL, "
        "low REAL NOT NULL, close REAL NOT NULL, volume REAL NOT NULL DEFAULT 0, "
        "PRIMARY KEY (symbol, interval, bar_time))")
    conn.execute("INSERT INTO ohlcv VALUES ('USDJPY','1m',"
                 "'2026-07-22T12:00:00+00:00',1,2,0.5,1.5,100)")
    conn.commit()

    init_db(conn)  # v1 → v2 → split が連鎖して走る

    names = {r["name"] for r in
             conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "ohlcv" not in names and "ohlcv_v1" not in names
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(ohlcv_cache)")}
    assert "source" in cols and "spread" not in cols
    row = conn.execute("SELECT * FROM ohlcv_cache").fetchone()
    assert row["source"] == "yfinance"
    assert row["open"] == 1
    assert row["high"] == 2
    assert row["low"] == 0.5
    assert row["close"] == 1.5
    assert row["volume"] == 100


def test_init_db_ohlcv_migration_is_idempotent(tmp_path):
    """新規 DB (v1 ohlcv が最初から無い) に init_db を複数回流しても
    split/v2 migration が何もしないこと。"""
    conn = connect(tmp_path / "v2.db")
    init_db(conn)
    init_db(conn)
    names = {r["name"] for r in
             conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "ohlcv" not in names and "ohlcv_v1" not in names
    assert "ohlcv_cache" in names and "ohlcv_history" in names
```

`test_ohlcv_migration_rolls_back_on_failure_and_resumes` は前半 (`_migrate_ohlcv_v2` を直接呼ぶ部分) は無変更。末尾の `init_db(conn)` 後のアサーションだけを次のように更新する:

```python
    # 再実行で成功する (v1 → v2 → split の連鎖が走り、ohlcv_cache に収束する)
    init_db(conn)
    names = {r["name"] for r in
             conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "ohlcv" not in names
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(ohlcv_cache)")}
    assert "source" in cols
    row = conn.execute("SELECT source FROM ohlcv_cache").fetchone()
    assert row["source"] == "yfinance"
```

`test_ohlcv_migration_resumes_from_stale_ohlcv_v1` の末尾 (`init_db(conn)` 以降) を更新する:

```python
    init_db(conn)  # ohlcv_v1 が残っていても再開して片付く (v2 → split も連鎖)

    names = {r["name"] for r in
             conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "ohlcv_v1" not in names and "ohlcv" not in names
    assert conn.execute("SELECT COUNT(*) FROM ohlcv_cache").fetchone()[0] == 1
    row = conn.execute("SELECT * FROM ohlcv_cache").fetchone()
    assert (row["open"], row["high"], row["low"], row["close"],
           row["volume"]) == (1, 2, 0.5, 1.5, 100)
```

`test_migrate_ohlcv_v2_is_noop_when_already_migrated_by_another_connection` の setup を、フレッシュ DB (init_db が `ohlcv` を作らなくなったため) ではなく `_legacy_v1_ddl()` による手作りの v1 テーブル + `_migrate_ohlcv_v2` 直接呼び出しに書き換える (`init_db` を経由しない — この関数はあくまで `_migrate_ohlcv_v2` 単体の再検査ロジックを検査する):

```python
def test_migrate_ohlcv_v2_is_noop_when_already_migrated_by_another_connection(
        tmp_path):
    """F2: BEGIN IMMEDIATE 取得後に「既に完全移行済み」を再検証し、redundant
    な rebuild を実行しないこと。"""
    from agentic_fx.store import db as db_module

    conn = connect(tmp_path / "race.db")
    conn.execute(_legacy_v1_ddl())
    conn.execute("INSERT INTO ohlcv VALUES ('USDJPY','1m',"
                 "'2026-07-22T12:00:00+00:00',1,2,0.5,1.5,100)")
    conn.commit()
    db_module._migrate_ohlcv_v2(conn)  # 1 回目の移行 (正常) — v2 単一テーブル止まり
    conn.execute("INSERT INTO ohlcv (symbol,interval,bar_time,open,high,low,"
                 "close,volume,source) VALUES ('USDJPY','1m',"
                 "'2026-07-22T12:00:00+00:00',1,2,0.5,1.5,0,'dukascopy')")
    conn.commit()

    class _SpyConn:
        def __init__(self, real):
            self._real = real
            self.rename_calls = 0

        def execute(self, sql, *args, **kwargs):
            if str(sql).strip().startswith("ALTER TABLE ohlcv RENAME"):
                self.rename_calls += 1
            return self._real.execute(sql, *args, **kwargs)

        def __getattr__(self, name):
            return getattr(self._real, name)

    spy = _SpyConn(conn)
    db_module._migrate_ohlcv_v2(spy)  # 2 回目 (別接続が先に済ませていた想定)

    assert spy.rename_calls == 0  # F2: 再検証で何もせず抜けている
    row = conn.execute(
        "SELECT open FROM ohlcv WHERE source='dukascopy'").fetchone()
    assert row is not None and row["open"] == 1
```

`test_migration_creates_backup_before_rebuild` は無変更 (v1→v2 のバックアップは split と無関係)。`test_migration_backup_not_overwritten_on_retry` は `_backup_before_migration` の呼び出しにサフィックス引数を足すだけ:

```python
    db_module._backup_before_migration(conn, ".bak-ohlcv-v2")
```

`test_migration_skips_backup_for_inmemory_db` の末尾を更新する (in-memory 接続でも split まで含めて安全にスキップされること):

```python
    init_db(conn)  # backup 対象パスが無いのでスキップされ、移行は成功する

    names = {r["name"] for r in
             conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "ohlcv" not in names
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(ohlcv_cache)")}
    assert "source" in cols
```

**新規追加テスト** (split migration 固有の検査 — 既存ファイルの末尾に追加する):

```python
def test_migrate_ohlcv_split_routes_unknown_source_to_history(tmp_path):
    """設計書 §12 移行方針: 未知 source は履歴側へ隔離する (fail-safe —
    履歴側は削除されないため、判断を誤っても失われない)。"""
    from agentic_fx.store import db as db_module

    conn = connect(tmp_path / "unknown.db")
    conn.executescript(db_v2_ddl_for_test())
    conn.executescript(db_module._OHLCV_CACHE_DDL +
                       db_module._OHLCV_HISTORY_DDL)
    conn.execute("INSERT INTO ohlcv VALUES ('USDJPY','1m',"
                 "'2026-07-22T12:00:00+00:00',1,2,0.5,1.5,100,"
                 "'some-future-vendor',NULL)")
    conn.commit()

    db_module._migrate_ohlcv_split(conn)

    assert conn.execute(
        "SELECT COUNT(*) FROM ohlcv_history WHERE source='some-future-vendor'"
    ).fetchone()[0] == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM ohlcv_cache WHERE source='some-future-vendor'"
    ).fetchone()[0] == 0


def test_migrate_ohlcv_split_routes_live_and_import_sources_correctly(tmp_path):
    from agentic_fx.store import db as db_module

    conn = connect(tmp_path / "mixed.db")
    conn.executescript(db_v2_ddl_for_test())
    conn.executescript(db_module._OHLCV_CACHE_DDL +
                       db_module._OHLCV_HISTORY_DDL)
    conn.execute("INSERT INTO ohlcv VALUES ('USDJPY','1m',"
                 "'2026-07-22T12:00:00+00:00',1,2,0.5,1.5,100,'yfinance',NULL)")
    conn.execute("INSERT INTO ohlcv VALUES ('USDJPY','1m',"
                 "'2026-07-22T12:01:00+00:00',1,2,0.5,1.5,100,'mt5-live',NULL)")
    conn.execute("INSERT INTO ohlcv VALUES ('USDJPY','1m',"
                 "'2026-07-22T12:02:00+00:00',1,2,0.5,1.5,100,'dukascopy',0.01)")
    conn.execute("INSERT INTO ohlcv VALUES ('USDJPY','1m',"
                 "'2026-07-22T12:03:00+00:00',1,2,0.5,1.5,100,'mt5',NULL)")
    conn.commit()

    db_module._migrate_ohlcv_split(conn)

    assert conn.execute(
        "SELECT COUNT(*) FROM ohlcv_cache").fetchone()[0] == 2
    assert conn.execute(
        "SELECT COUNT(*) FROM ohlcv_history").fetchone()[0] == 2
    assert conn.execute(
        "SELECT COUNT(*) FROM ohlcv_cache WHERE source IN ('dukascopy','mt5')"
    ).fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM ohlcv_history WHERE source IN "
        "('yfinance','mt5-live')").fetchone()[0] == 0


def test_migrate_ohlcv_split_is_noop_when_ohlcv_table_absent(tmp_path):
    """フレッシュ DB (init_db 直後) には `ohlcv` が存在しない — 直接呼んでも
    何もせず正常終了する。"""
    from agentic_fx.store import db as db_module

    conn = connect(tmp_path / "fresh.db")
    init_db(conn)
    db_module._migrate_ohlcv_split(conn)  # 例外なく即座に戻る
    assert conn.execute(
        "SELECT COUNT(*) FROM ohlcv_cache").fetchone()[0] == 0


def test_migrate_ohlcv_split_raises_on_value_conflict(tmp_path):
    """F3 相当: 分割先に既に食い違う値の行がある場合は例外で停止し、
    `ohlcv` を温存する (黙って破棄しない)。"""
    from agentic_fx.store import db as db_module

    conn = connect(tmp_path / "split_conflict.db")
    conn.executescript(db_v2_ddl_for_test())
    conn.execute("INSERT INTO ohlcv VALUES ('USDJPY','1m',"
                 "'2026-07-22T12:00:00+00:00',1,2,0.5,1.5,100,'yfinance',NULL)")
    conn.commit()
    conn.executescript(
        "CREATE TABLE IF NOT EXISTS ohlcv_cache ("
        "symbol TEXT NOT NULL, interval TEXT NOT NULL, bar_time TEXT NOT NULL, "
        "open REAL NOT NULL, high REAL NOT NULL, low REAL NOT NULL, "
        "close REAL NOT NULL, volume REAL NOT NULL DEFAULT 0, "
        "source TEXT NOT NULL, "
        "PRIMARY KEY (symbol, interval, bar_time, source));")
    conn.execute("INSERT INTO ohlcv_cache VALUES ('USDJPY','1m',"
                 "'2026-07-22T12:00:00+00:00',999,2,0.5,1.5,100,'yfinance')")
    conn.commit()

    with pytest.raises(RuntimeError, match="一致しない"):
        db_module._migrate_ohlcv_split(conn)

    names = {r["name"] for r in
             conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "ohlcv" in names  # 温存されている
```

- [ ] **Step 8: `store/db.py` + `test_db.py` のテストを実行し green を確認する**

```bash
find . -name __pycache__ -type d -not -path "./.venv/*" -exec rm -rf {} +
uv run pytest tests/store/test_db.py -v
```
Expected: 全件 PASS

#### Step 群 C: `store/ohlcv.py` の実装

- [ ] **Step 9: `src/agentic_fx/store/ohlcv.py` を全面書き換えする**

```python
from __future__ import annotations

import logging
import math
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

from agentic_fx.core.contracts import Bar

_log = logging.getLogger("agentic_fx.store.ohlcv")

_FLOAT_TOL = 1e-9

# ライブチェーンの永続化名のみ。"mt5" ではなく "mt5-live"
# (datafeed/price_provider.py の _STORAGE_SOURCE 参照)。
LIVE_SOURCES = frozenset({"yfinance", "twelvedata", "mt5-live"})

# 一括インポータの source 名のみ (backtest/importer.py / backtest/mt5_import.py)。
IMPORT_SOURCES = frozenset({"dukascopy", "mt5"})

# プラン 9 Task 16: KNOWN_OHLCV_SOURCES は両集合の union として定義する
# (以前は独立したリテラルだったが、LIVE_SOURCES/IMPORT_SOURCES を単一の
# 真実の源にする — service.py:_validate_startup の producer_source typo
# 検出が参照する)。
KNOWN_OHLCV_SOURCES = LIVE_SOURCES | IMPORT_SOURCES


def _iso_utc(ts: datetime) -> str:
    """aware datetime を UTC へ正規化してから isoformat する。naive は
    ValueError (fail closed)。"""
    if ts.tzinfo is None:
        raise ValueError(
            "_iso_utc: naive datetime is rejected (tz-aware UTC required)")
    return ts.astimezone(timezone.utc).isoformat()


def _require_aware_utc(dt: datetime, label: str) -> datetime:
    if dt.tzinfo is None:
        raise ValueError(f"{label} is naive; tz-aware UTC datetime required "
                          "(cannot safely assume UTC)")
    return dt.astimezone(timezone.utc)


# ---- キャッシュ (ohlcv_cache) — ライブ逐次専用 ------------------------------


def upsert_cache_bars(conn: sqlite3.Connection, bars: list[Bar], *,
                      source: str) -> int:
    """live キャッシュ用。形成中バーの更新があるため常に上書きする。

    `source` は `LIVE_SOURCES` のいずれかでなければならない — 一括インポート
    由来の source (例: "dukascopy") をここから書くと `ohlcv_history` の
    「既存行不変」契約を迂回できてしまうテーブル越境が生じるため
    (テーブルが分かれているので実際には書き込み自体が別テーブルへ向かうが、
    source の取り違えという設定ミスを早期に検出する)。
    """
    if source not in LIVE_SOURCES:
        raise ValueError(
            f"upsert_cache_bars: source={source!r} は LIVE_SOURCES "
            f"{sorted(LIVE_SOURCES)} に含まれません "
            "(一括インポート由来の source は import_history_bars を使うこと)")
    conn.executemany(
        "INSERT INTO ohlcv_cache (symbol, interval, bar_time, open, high, "
        "low, close, volume, source) VALUES (?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(symbol, interval, bar_time, source) DO UPDATE SET "
        "open=excluded.open, high=excluded.high, low=excluded.low, "
        "close=excluded.close, volume=excluded.volume",
        [(b.symbol, b.interval, _iso_utc(b.ts), b.open, b.high, b.low,
          b.close, b.volume, source) for b in bars])
    conn.commit()
    return len(bars)


def load_cache_bars(conn: sqlite3.Connection, symbol: str, interval: str, *,
                    source: str, since: datetime | None = None,
                    until: datetime | None = None) -> list[Bar]:
    if source not in LIVE_SOURCES:
        raise ValueError(
            f"load_cache_bars: source={source!r} は LIVE_SOURCES "
            f"{sorted(LIVE_SOURCES)} に含まれません")
    q = "SELECT * FROM ohlcv_cache WHERE symbol=? AND interval=? AND source=?"
    args: list = [symbol, interval, source]
    if since is not None:
        since_utc = _require_aware_utc(since, "since")
        q += " AND bar_time >= ?"
        args.append(since_utc.isoformat())
    if until is not None:
        until_utc = _require_aware_utc(until, "until")
        q += " AND bar_time <= ?"
        args.append(until_utc.isoformat())
    rows = conn.execute(q + " ORDER BY bar_time", args).fetchall()
    return [Bar(r["symbol"], r["interval"],
                datetime.fromisoformat(r["bar_time"]),
                r["open"], r["high"], r["low"], r["close"], r["volume"])
            for r in rows]


def prune_cache(conn: sqlite3.Connection, *, cutoff: datetime,
                limit: int) -> int:
    """`ohlcv_cache` の保持ポリシー。`bar_time < cutoff` の行を最大 `limit`
    件だけ削除する (有界バッチ — core_lock 保持窓を無制限に伸ばさないため)。

    `DELETE FROM ohlcv_cache` のみで完結する — source 絞りも「未知 source
    は消さない」fail-safe 分岐も**不要** (`ohlcv_history` はテーブルが
    違うため、この DELETE は原理的に到達できない)。呼び出し側 (maintenance
    hook) が毎 tick 呼ぶことで、有界バッチのまま日次増分に追いつく
    (設計書 D2「追いつくまで毎 maintenance」)。

    削除件数を返す (呼び出し側は使わなくてよいが、テスト・ログで有用)。
    """
    cutoff_utc = _require_aware_utc(cutoff, "cutoff")
    if limit < 1:
        raise ValueError(f"prune_cache: limit must be >= 1, got {limit}")
    cur = conn.execute(
        "DELETE FROM ohlcv_cache WHERE rowid IN ("
        "SELECT rowid FROM ohlcv_cache WHERE bar_time < ? LIMIT ?)",
        (cutoff_utc.isoformat(), limit))
    conn.commit()
    return cur.rowcount


# ---- 履歴 (ohlcv_history) — バックテスト・一括インポート専用 ----------------


@dataclass(frozen=True)
class ImportResult:
    inserted: int
    unchanged: int
    conflicted: int


def _close_enough(a: float | None, b: float | None) -> bool:
    if a is None or b is None:
        return a is None and b is None
    return abs(a - b) < _FLOAT_TOL


def _validate_and_normalize_row(idx: int, row: tuple) -> tuple:
    """投入前検証 (import_history_bars 用)。

    - 有限値であること (NaN/inf を弾く)
    - `low <= min(open, close) <= max(open, close) <= high`
    - `bar_time` は aware datetime として parse できること (UTC へ正規化
      してから isoformat した文字列に置き換えて返す)

    不正行はここで ValueError を送出する。呼び出し側は**全行をここで
    検証してから**書き込みを始める。
    """
    if len(row) != 9:
        raise ValueError(
            f"import_history_bars: rows[{idx}] has {len(row)} fields, "
            "expected 9 (symbol, interval, bar_time_iso, o, h, l, c, "
            "volume, spread)")
    symbol, interval, bar_time_iso, o, h, l, c, volume, spread = row  # noqa: E741
    if not symbol or not interval:
        raise ValueError(
            f"import_history_bars: rows[{idx}] has empty symbol/interval")
    try:
        dt = datetime.fromisoformat(bar_time_iso)
    except (TypeError, ValueError) as e:
        raise ValueError(
            f"import_history_bars: rows[{idx}] bar_time not parseable: "
            f"{bar_time_iso!r}") from e
    if dt.tzinfo is None:
        raise ValueError(
            f"import_history_bars: rows[{idx}] bar_time is naive (aware "
            f"datetime required): {bar_time_iso!r}")
    normalized_bar_time = dt.astimezone(timezone.utc).isoformat()
    for name, val in (("open", o), ("high", h), ("low", l), ("close", c),
                      ("volume", volume)):
        if not isinstance(val, (int, float)) or isinstance(val, bool) \
                or not math.isfinite(val):
            raise ValueError(
                f"import_history_bars: rows[{idx}] {name} is not a finite "
                f"number: {val!r}")
    if spread is not None:
        if not isinstance(spread, (int, float)) or isinstance(spread, bool) \
                or not math.isfinite(spread):
            raise ValueError(
                f"import_history_bars: rows[{idx}] spread is not a finite "
                f"number: {spread!r}")
    lo_bound, hi_bound = min(o, c), max(o, c)
    if not (l <= lo_bound <= hi_bound <= h):
        raise ValueError(
            f"import_history_bars: rows[{idx}] violates low<=min(open,close)"
            f"<=max(open,close)<=high: open={o} high={h} low={l} close={c}")
    return (symbol, interval, normalized_bar_time, o, h, l, c, volume, spread)


def import_history_bars(conn: sqlite3.Connection, rows: list[tuple], *,
                        source: str) -> ImportResult:
    """一括インポータ用。既存行は不変 — 同一キー同一値は無変更 (unchanged)、
    値が異なる既存行は入力をスキップして件数だけ計上する (conflicted)。

    rows: (symbol, interval, bar_time_iso, o, h, l, c, volume, spread|None)

    `source` は `IMPORT_SOURCES` のいずれかでなければならない — ライブ
    source 名 (例: "yfinance") をここから書くと `ohlcv_history` の
    「削除しない」保証をライブキャッシュの上書きセマンティクスで穢すため。

    バッチ原子性: (1) 書き込み前に全行を検証・正規化し、(2) 実際の書き込みは
    `SAVEPOINT` で明示的に囲み、例外時は `ROLLBACK TO SAVEPOINT` してから
    再送出する。
    """
    if source not in IMPORT_SOURCES:
        raise ValueError(
            f"import_history_bars: source={source!r} は IMPORT_SOURCES "
            f"{sorted(IMPORT_SOURCES)} に含まれません "
            "(ライブ由来の source は upsert_cache_bars を使うこと)")
    normalized_rows = [_validate_and_normalize_row(i, row)
                       for i, row in enumerate(rows)]

    inserted = 0
    unchanged = 0
    conflicted = 0
    conn.execute("SAVEPOINT import_history_bars")
    try:
        for (symbol, interval, bar_time_iso, o, h, l, c, volume,  # noqa: E741
             spread) in normalized_rows:
            cur = conn.execute(
                "INSERT OR IGNORE INTO ohlcv_history (symbol, interval, "
                "bar_time, open, high, low, close, volume, source, spread) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (symbol, interval, bar_time_iso, o, h, l, c, volume, source,
                 spread))
            if cur.rowcount == 1:
                inserted += 1
                continue
            existing = conn.execute(
                "SELECT open, high, low, close, volume, spread FROM "
                "ohlcv_history WHERE symbol=? AND interval=? AND bar_time=? "
                "AND source=?",
                (symbol, interval, bar_time_iso, source)).fetchone()
            same = (
                _close_enough(existing["open"], o)
                and _close_enough(existing["high"], h)
                and _close_enough(existing["low"], l)
                and _close_enough(existing["close"], c)
                and _close_enough(existing["volume"], volume)
                and _close_enough(existing["spread"], spread)
            )
            if same:
                unchanged += 1
            else:
                conflicted += 1
        conn.execute("RELEASE import_history_bars")
    except BaseException:
        conn.execute("ROLLBACK TO SAVEPOINT import_history_bars")
        conn.execute("RELEASE import_history_bars")
        raise
    conn.commit()
    if conflicted > 0:
        _log.warning(
            "import_history_bars: %d row(s) conflicted with existing data "
            "for source=%s (existing rows left unchanged)",
            conflicted, source)
    return ImportResult(inserted, unchanged, conflicted)


def load_history_bars(conn: sqlite3.Connection, symbol: str, interval: str,
                      *, source: str, since: datetime | None = None,
                      until: datetime | None = None) -> list[Bar]:
    if source not in IMPORT_SOURCES:
        raise ValueError(
            f"load_history_bars: source={source!r} は IMPORT_SOURCES "
            f"{sorted(IMPORT_SOURCES)} に含まれません "
            "(履歴はバックテスト・分析専用 — ライブ source は "
            "load_cache_bars を使うこと)")
    q = ("SELECT * FROM ohlcv_history WHERE symbol=? AND interval=? "
        "AND source=?")
    args: list = [symbol, interval, source]
    if since is not None:
        since_utc = _require_aware_utc(since, "since")
        q += " AND bar_time >= ?"
        args.append(since_utc.isoformat())
    if until is not None:
        until_utc = _require_aware_utc(until, "until")
        q += " AND bar_time <= ?"
        args.append(until_utc.isoformat())
    rows = conn.execute(q + " ORDER BY bar_time", args).fetchall()
    return [Bar(r["symbol"], r["interval"],
                datetime.fromisoformat(r["bar_time"]),
                r["open"], r["high"], r["low"], r["close"], r["volume"])
            for r in rows]


def load_history_spread(conn: sqlite3.Connection, symbol: str, interval: str,
                        bar_time_iso: str, *, source: str) -> float | None:
    if source not in IMPORT_SOURCES:
        raise ValueError(
            f"load_history_spread: source={source!r} は IMPORT_SOURCES "
            f"{sorted(IMPORT_SOURCES)} に含まれません")
    row = conn.execute(
        "SELECT spread FROM ohlcv_history WHERE symbol=? AND interval=? "
        "AND bar_time=? AND source=?",
        (symbol, interval, bar_time_iso, source)).fetchone()
    return row["spread"] if row is not None else None
```

- [ ] **Step 10: `tests/store/test_ohlcv.py` を実行し green を確認する**

```bash
find . -name __pycache__ -type d -not -path "./.venv/*" -exec rm -rf {} +
uv run pytest tests/store/test_ohlcv.py -v
```
Expected: 全件 PASS (44 件)

#### Step 群 D: 本番呼び出し元の機械的リネーム

- [ ] **Step 11: `src/agentic_fx/datafeed/price_provider.py` の呼び出し名だけを付け替える (挙動不変)**

3 箇所を変更する (引数・呼び出し位置は無変更):

`get_bars` 内 (native 分岐、旧 `:163`):
```python
                    if not self.readonly:
                        ohlcv.upsert_cache_bars(self.conn, bars,
                                                source=_storage_source(name))
```

`_cached_bars` 内 (旧 `:228`):
```python
                cached = ohlcv.load_cache_bars(self.conn, pair, src,
                                               source=storage_name)
```

`_derive` 内 (旧 `:342`):
```python
        if not self.readonly:
            ohlcv.upsert_cache_bars(self.conn, raw, source=_storage_source(source))
```

- [ ] **Step 12: `tests/datafeed/test_price_provider.py` を一括リネームする**

このファイルの `ohlcv.load_bars`/`ohlcv.upsert_bars` 呼び出しは全て live source (`yfinance`/`mt5-live`) を使っており、全件がキャッシュ API への単純改名で済む (曖昧さなし — 事前 grep で確認済み)。

> **プラン欠陥 #5 (2026-08-12 実行時に判明・修正済み)**: この「事前 grep で確認済み」は**誤り**。
> `tests/datafeed/test_price_provider.py:417` が `ohlcv.load_bars(..., source="mt5") == []` を
> assert しており、`mt5` は IMPORT_SOURCES 側の名前。単純改名すると新 API の読み側 allowlist が
> `ValueError` を投げ、Step 14 の期待「全件 PASS」に到達できない。
> **修正**: 分割前の保証「`mt5` では空が返る」より、分割後の「読み側が名前ごと拒否する」の方が
> 検査目的 (一括インポータ source の混入防止) に対して強い。assert を
> `with pytest.raises(ValueError): ohlcv.load_cache_bars(conn, "USDJPY", "1m", source="mt5")`
> へ置き換えた。

```bash
sed -i \
  -e 's/ohlcv\.load_bars(/ohlcv.load_cache_bars(/g' \
  -e 's/ohlcv\.upsert_bars(/ohlcv.upsert_cache_bars(/g' \
  tests/datafeed/test_price_provider.py
```

- [ ] **Step 13: `tests/store/test_records.py` と `tests/tools/test_mission_registry.py` の `load_bars`/`upsert_bars` をキャッシュ用名へリネームする**

両ファイルとも `source="yfinance"` のみを使用 (事前 grep で確認済み)。

```bash
sed -i \
  -e 's/ohlcv\.load_bars(/ohlcv.load_cache_bars(/g' \
  -e 's/ohlcv\.upsert_bars(/ohlcv.upsert_cache_bars(/g' \
  tests/store/test_records.py tests/tools/test_mission_registry.py
```

- [ ] **Step 14: 改名結果を目視確認し、リネーム対象のテストを実行する**

```bash
grep -n "ohlcv\.\(load_bars\|upsert_bars\)(" \
  tests/datafeed/test_price_provider.py tests/store/test_records.py \
  tests/tools/test_mission_registry.py
```
Expected: 出力なし (リネーム漏れが無い)

```bash
find . -name __pycache__ -type d -not -path "./.venv/*" -exec rm -rf {} +
uv run pytest tests/datafeed/test_price_provider.py tests/store/test_records.py \
  tests/tools/test_mission_registry.py -v
```
Expected: 全件 PASS (この時点で Task 8/9/10 の窓ロジックはまだ配線されていないため、`price_provider.py` の挙動自体は分割前と完全に同じ)

#### Step 群 E: backtest 層 (履歴専用) の付け替え

すべて `ohlcv_history` へ付け替える — backtest 層は履歴データしか読み書きしないため、曖昧さは無い (事前 grep で確認済み)。

> **プラン欠陥 #6 (2026-08-12 実行時に判明・修正済み — 本番の読み経路を壊す)**:
> この「backtest 層は履歴データしか読み書きしない」は**誤り**。
> `backtest/timeframes.py:load_resampled_frame` は backtest 専用ではなく、
> **本番のライブ経路と共有されたリーダ**である。呼び出し元は 3 系統:
> `backtest/analysis.py` (履歴)、`plugin/strategy_adapter.py` (`_EVAL_SOURCE="dukascopy"` = 履歴)、
> そして **`plugin/signal_producer.py:170` (`settings.plugin.producer_source` = 既定 `"yfinance"` = ライブ)**。
>
> SQL を `FROM ohlcv_history` に固定すると、本番の signal producer はライブデータの入った
> `ohlcv_cache` ではなく空の `ohlcv_history` を読む。**症状は例外ではない** —
> `signal_producer._evaluate_one` は plugin 単位で fail-open (WARNING → cursor を進めず次 tick
> 再試行) なので、**signal が永久に 1 本も出ないまま上位に失敗が伝わらない**。
> `service.py:_validate_startup` の `producer_source ∈ KNOWN_OHLCV_SOURCES` (union) 検証も
> ライブ source を通すのでここでは捕まらない。
>
> **修正**: `load_resampled_frame` が `source` からテーブルを**導出**する `_table()` を追加した
> (`LIVE_SOURCES` → `ohlcv_cache` / `IMPORT_SOURCES` → `ohlcv_history` / それ以外は `ValueError`)。
> 設計判断 #1「API はテーブルを引数で選ばせない」の不変条件は保たれる — #1 が禁じているのは
> **呼び出し側がテーブルを選ぶこと**であり、互いに素な allowlist からの一意導出はこれに当たらない。
> 両テーブルで SELECT する列は同一 (`spread` は履歴側のみだがこの SELECT は使わない) なので、
> `ohlcv.py` のようにリーダを 2 本へ割る必要はない。
> 併せて、ライブ source で seed していたフィクスチャ 3 本
> (`tests/plugin/test_signal_producer.py` / `tests/test_e2e_plugin_signal.py` /
> `tests/test_service_app.py`) を `upsert_cache_bars` へ振り直した。
> 観測点は `tests/backtest/test_timeframes.py` の 4 テスト (変異 X-1/X-2 で kill 済み)。

- [ ] **Step 15: `src/agentic_fx/backtest/importer.py` を付け替える**

```python
from agentic_fx.store.ohlcv import ImportResult, import_history_bars
```

`import_dukascopy` 内の呼び出し:
```python
        if rows:
            result = import_history_bars(conn, rows, source="dukascopy")
```

- [ ] **Step 16: `src/agentic_fx/backtest/mt5_import.py` を付け替える**

```python
from agentic_fx.store.ohlcv import ImportResult, import_history_bars
```

`import_mt5` 内:
```python
        if rows:
            result = import_history_bars(conn, rows, source="mt5")
```

`compare_sources` 内の raw SQL:
```python
    cur = conn.execute(
        "SELECT ta.close AS a_close, tb.close AS b_close "
        "FROM ohlcv_history ta JOIN ohlcv_history tb "
        "ON ta.symbol = tb.symbol AND ta.interval = tb.interval "
        "AND ta.bar_time = tb.bar_time "
        "WHERE ta.symbol = ? AND ta.interval = '1m' "
        "AND ta.source = ? AND tb.source = ?",
        (symbol, a, b))
```

- [ ] **Step 17: `src/agentic_fx/backtest/replay.py` の `BarFeed.__init__` の raw SQL を付け替える**

```python
        rows = conn.execute(
            "SELECT * FROM ohlcv_history WHERE symbol=? AND interval='1m' "
            "AND source=? AND bar_time>=? AND bar_time<=? ORDER BY bar_time",
            (symbol, source, start_utc.isoformat(), end_utc.isoformat())
        ).fetchall()
```

- [ ] **Step 18: `src/agentic_fx/backtest/timeframes.py` の `load_resampled_frame` の raw SQL を付け替える**

```python
    q = ("SELECT bar_time, open, high, low, close, volume FROM ohlcv_history "
         "WHERE symbol=? AND interval='1m' AND source=?")
```

- [ ] **Step 19: `src/agentic_fx/backtest/holdout.py` の `_oldest_bar_start` の raw SQL を付け替える**

```python
def _oldest_bar_start(history_conn: sqlite3.Connection, symbol: str,
                      source: str) -> datetime:
    row = history_conn.execute(
        "SELECT MIN(bar_time) FROM ohlcv_history WHERE symbol=? "
        "AND interval='1m' AND source=?", (symbol, source)).fetchone()
```

- [ ] **Step 20: `src/agentic_fx/backtest/cli.py` の `_empty_history_guard` の raw SQL を付け替え、ライブ source を fail closed にする**

`_empty_history_guard` 内:
```python
    n_bars = conn.execute(
        "SELECT COUNT(*) FROM ohlcv_history WHERE symbol=? AND interval='1m' "
        "AND source=? AND bar_time >= ? AND bar_time < ?",
        (args.symbol, args.source, args.from_.isoformat(),
         args.to.isoformat())).fetchone()[0]
```

`register_subparsers` 内、`cov`/`run`/`corr` の `--source` に `choices=` を追加する (`imp` は既に `choices=["dukascopy","mt5"]` で制限済みなので触らない)。ファイル冒頭の import に `from agentic_fx.store import ohlcv` を追加する:

```python
from agentic_fx.store import backtest_runs, ohlcv
```

```python
    cov = history_sub.add_parser("coverage", help="カバレッジレポート")
    cov.add_argument("--symbol", required=True)
    cov.add_argument("--timeframe", required=True)
    cov.add_argument("--source", required=True, choices=sorted(ohlcv.IMPORT_SOURCES))
    cov.add_argument("--from", dest="from_", type=_parse_date, required=True)
    cov.add_argument("--to", dest="to", type=_parse_date, required=True)
```

```python
    run.add_argument("--symbol", required=True)
    run.add_argument("--source", required=True, choices=sorted(ohlcv.IMPORT_SOURCES))
```

```python
    corr.add_argument("--a", required=True)
    corr.add_argument("--b", required=True)
    corr.add_argument("--timeframe", required=True)
    corr.add_argument("--source", required=True, choices=sorted(ohlcv.IMPORT_SOURCES))
```

- [ ] **Step 21: `tests/backtest/test_cli.py` に「ライブ source を渡すと fail closed」の失敗するテストを追加する**

ファイル末尾に追加する。argparse の `choices=` 検証は `parse_args()` 内、DB/settings に触れるより前に発火するため `ensure_initialized`/`import_dukascopy` 等の patch は不要 — 既存テストと同じ `monkeypatch.chdir(tmp_path)` + `_install_settings(tmp_path)` の型だけ揃える (`main`/`pytest` は既存 import をそのまま使う)。

```python
def test_backtest_run_rejects_live_source(tmp_path, monkeypatch):
    """人間 CLI にライブ source を渡すと argparse が fail closed する
    (設計書 D2)。SystemExit(2) は argparse の標準的な引数エラー終了コード。"""
    monkeypatch.chdir(tmp_path)
    _install_settings(tmp_path)
    with pytest.raises(SystemExit) as exc:
        main(["backtest", "run", "--symbol", "USDJPY", "--source", "yfinance",
             "--from", "2026-01-01", "--to", "2026-01-02",
             "--proposal-file", "dummy.jsonl"])
    assert exc.value.code == 2


def test_history_coverage_rejects_live_source(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _install_settings(tmp_path)
    with pytest.raises(SystemExit) as exc:
        main(["history", "coverage", "--symbol", "USDJPY",
             "--timeframe", "1h", "--source", "mt5-live",
             "--from", "2026-01-01", "--to", "2026-01-02"])
    assert exc.value.code == 2


def test_analyze_corr_rejects_live_source(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _install_settings(tmp_path)
    with pytest.raises(SystemExit) as exc:
        main(["analyze", "corr", "--a", "USDJPY", "--b", "EURUSD",
             "--timeframe", "1h", "--source", "twelvedata"])
    assert exc.value.code == 2
```

- [ ] **Step 22: backtest 層のソース側変更を実行前に目視確認する**

```bash
grep -n "FROM ohlcv\b\|INTO ohlcv\b\|import_bars(\|load_bars(\|load_spread(" \
  src/agentic_fx/backtest/*.py
```
Expected: `ohlcv_history`/`import_history_bars`/`load_history_bars`/`load_history_spread` のみが残り、旧名は 0 件

- [ ] **Step 23: grep で確定した全 call site を API の用途別にリネームする**

履歴側は既存 backtest 10 ファイルに加え、次を含む全 grep 結果を更新する:
`tests/test_e2e_plugin_signal.py`、`tests/test_service_app.py`、
`tests/plugin/test_approval.py`、`tests/plugin/test_signal_producer.py`、
`tests/plugin/test_strategy_adapter.py`。キャッシュ側で**本 Step が触るのは
`tests/test_e2e_worker_isolation.py` のみ**である — `tests/store/test_records.py` と
`tests/tools/test_mission_registry.py` は **Step 13 で既にリネーム済み**なので
ここでは対象外 (下の sed に含まれていないのは意図的)。import 文も call site と別に検索する。

```bash
FILES="tests/backtest/test_mt5_import.py tests/backtest/test_analysis.py \
  tests/backtest/test_holdout.py tests/backtest/test_e2e_backtest.py \
  tests/backtest/test_replay.py tests/backtest/test_runner.py \
  tests/backtest/test_timeframes.py tests/backtest/test_bench.py \
  tests/backtest/test_cli.py tests/backtest/test_importer.py"

sed -i \
  -e 's/import_bars/import_history_bars/g' \
  -e 's/load_bars/load_history_bars/g' \
  -e 's/load_spread/load_history_spread/g' \
  -e 's/FROM ohlcv/FROM ohlcv_history/g' \
  $FILES

# test_timeframes.py の唯一のキャッシュ側呼び出し (他 source との
# 混入防止テスト) だけは upsert_cache_bars へ個別に直す
sed -i 's/ohlcv\.upsert_bars(/ohlcv.upsert_cache_bars(/' \
  tests/backtest/test_timeframes.py

HISTORY_FIXTURES="tests/test_e2e_plugin_signal.py tests/test_service_app.py \
  tests/plugin/test_approval.py tests/plugin/test_signal_producer.py \
  tests/plugin/test_strategy_adapter.py"
sed -i 's/ohlcv_store\.import_bars(/ohlcv_store.import_history_bars(/g' \
  $HISTORY_FIXTURES

sed -i -e 's/ohlcv_store\.upsert_bars(/ohlcv_store.upsert_cache_bars(/g' \
  tests/test_e2e_worker_isolation.py

# `from ... import load_bars` も拾う。末尾 `(` を要求してはならない。
sed -i -e 's/import load_bars/import load_history_bars/' \
  -e 's/load_bars(/load_history_bars(/g' tests/backtest/test_importer.py
```

- [ ] **Step 24: リネーム漏れが無いことを確認し、backtest 層のテスト一式を実行する**

```bash
rg -n '\b(import_bars|load_bars|load_spread|upsert_bars)\b|FROM ohlcv\b' \
  src tests --glob '*.py'
```
Expected: 旧 API の定義・migration 用旧テーブル SQL・説明コメント以外の call
site/import は出力なし。出力を一行ずつ分類し、上記一覧に無い call site があれば
用途 (`LIVE_SOURCES` か `IMPORT_SOURCES`) を確認して同じ step で追加修正する。

```bash
find . -name __pycache__ -type d -not -path "./.venv/*" -exec rm -rf {} +
uv run pytest tests/backtest/ -v
```
Expected: 全件 PASS

#### Step 群 F: 保持期間の設定 + 起動時検証

- [ ] **Step 25: `config.py` の `DatafeedSettings` に `cache_retention_days` を追加する失敗するテストを書く**

`tests/test_config.py` は既存の `_with_datafeed(tmp_path, **overrides)` ヘルパ (settings.yaml.example の `datafeed:` ブロックを上書きして一時 YAML を作り `load_settings` に渡す) と `ConfigError` (`load_settings` が pydantic の `ValidationError` を wrap する例外) を持つ。同じ流儀で追記する:

```python
def test_cache_retention_days_defaults_to_30():
    s = load_settings(EXAMPLE)
    assert s.datafeed.cache_retention_days == 30


def test_cache_retention_days_must_be_positive():
    with pytest.raises(ConfigError, match="cache_retention_days"):
        load_settings(_with_datafeed(tmp_path, cache_retention_days=0))
```

2 つ目のテストは `tmp_path` fixture を引数に取る必要がある — 関数シグネチャを `def test_cache_retention_days_must_be_positive(tmp_path):` にする。

- [ ] **Step 26: テストを実行し、少なくとも一方が意図通り落ちることを確認する**

Run: `uv run pytest tests/test_config.py -k cache_retention_days -v`

Expected: `test_cache_retention_days_defaults_to_30` は **FAIL** (`AttributeError: 'DatafeedSettings' object has no attribute 'cache_retention_days'`)。`test_cache_retention_days_must_be_positive` は `DatafeedSettings` が `_Strict` (`extra="forbid"`) のため、フィールド未定義の時点でも「未知キー `cache_retention_days`」として `ConfigError` を送出し**先に PASS してしまう** (テスト自体は post-implementation でも正しく `gt=0` 違反を検査するので無効ではないが、この時点では「フィールドが無い」ことと「値が不正」ことを区別できていない — 正常な現象であり修正不要)。1 件が意図通り FAIL していることを確認できれば次へ進む。

- [ ] **Step 27: `config.py` の `DatafeedSettings` にフィールドを追加する**

```python
class DatafeedSettings(_Strict):
    yfinance: SourceToggle
    mt5: SourceToggle
    twelvedata: SourceToggle
    freshness_max_min: float = Field(gt=0)
    conversion_skew_max_min: float = Field(gt=0)
    intervals: list[str] = Field(default_factory=lambda: ["1m", "1h"],
                                 min_length=1)
    primary_intervals: list[str] = Field(default_factory=lambda: ["1h"],
                                         min_length=1)
    watch_symbols: list[str] = Field(default_factory=list)
    # プラン 9 Task 16: ohlcv_cache の保持期間 (日)。起動時検証
    # (service.py:_validate_startup) が構成済み intervals の要求日数と
    # 突き合わせる — ここでは正値検証のみ (interval との整合はキャッシュ
    # 窓計算 (cache_window) を import する必要があり、config.py を
    # datafeed 層に依存させない方針を保つため service.py 側に置く)。
    cache_retention_days: int = Field(default=30, gt=0)
```

- [ ] **Step 28: テストを実行し green を確認する**

Run: `uv run pytest tests/test_config.py -k cache_retention_days -v`
Expected: PASS

- [ ] **Step 29: `config/settings.yaml.example` に新キーを追記する**

`datafeed:` ブロックに 1 行追加する:

```yaml
datafeed:
  yfinance:   {enabled: true}
  mt5:        {enabled: false, bridge_url: "http://localhost:8812"}
  twelvedata: {enabled: false}
  freshness_max_min: 20       # これより古い quote/bar は不健全扱い
  conversion_skew_max_min: 5  # 換算レート (口座通貨換算) のクロス脚間・判断内スナップショットの時刻差許容。freshness_max_min より必ず小さいこと (同じ/大きいと skew 検証が発火しなくなる)
  intervals: [1m, 5m, 15m, 1h, 4h]   # 取得・保持する足 (1m は必須: ペーパー約定判定が依存)
  primary_intervals: [1h]            # 判断 Mission が依存する足 (healthcheck の対象)。増やすほど単一障害点も増える
  watch_symbols: []           # 取引不可・分析専用 (§6)。pair enum には入れない
  cache_retention_days: 30    # ohlcv_cache の保持期間 (日)。intervals の構成が要求する最小日数を下回ると起動拒否 (プラン9 Task16)
```

(`config/settings.yaml` は個人設定で gitignore — 開発者は各自 `.example` からコピーしているので、既存の個人 `settings.yaml` には手を触れない。新規に生成する場合のみ反映される。)

- [ ] **Step 30: `cache_retention_days` を config に追加した状態でコミット (中間コミット)**

```bash
find . -name __pycache__ -type d -not -path "./.venv/*" -exec rm -rf {} +
uv run pytest -q
git add src/agentic_fx/config.py config/settings.yaml.example tests/test_config.py
git commit -m "$(cat <<'EOF'
feat: datafeed.cache_retention_days 設定を追加 (プラン9 Task16)

ohlcv_cache の保持期間 (既定30日)。起動時の整合検証は次のコミットで
service.py に追加する (現時点では正値検証のみ)。
EOF
)"
```

- [ ] **Step 31: `service.py:_validate_startup` の保持期間検証を失敗するテストで固定する**

`tests/test_service_app.py` に追加する (既存の `_init`/`build_app` 呼び出しパターンを確認し合わせる — このファイルは束 C 冒頭の grep で `build_app` を使うテストが既にあることを確認済み):

```python
def test_build_app_rejects_cache_retention_below_interval_requirement(
        tmp_path):
    """設計書 D2: cache_retention_days が構成済み intervals の要求日数を
    下回ると起動拒否する (黙って clamp しない)。yfinance で 1d を intervals
    に足すと要求日数が 120 日に跳ね上がる (base=1h, ratio=24, lookback=5)
    ことを使って再現する。"""
    (tmp_path / "config").mkdir()
    src = open("config/settings.yaml.example", encoding="utf-8").read()
    src = src.replace(
        "intervals: [1m, 5m, 15m, 1h, 4h]",
        "intervals: [1m, 5m, 15m, 1h, 4h, 1d]")
    (tmp_path / "config" / "settings.yaml").write_text(src)
    with pytest.raises(RuntimeError, match="cache_retention_days"):
        with patch("agentic_fx.service.PriceProvider"), \
             patch("agentic_fx.service._check_llama_swap"):
            build_app(tmp_path)


def test_build_app_error_message_names_interval_and_required_days(tmp_path):
    (tmp_path / "config").mkdir()
    src = open("config/settings.yaml.example", encoding="utf-8").read()
    src = src.replace(
        "intervals: [1m, 5m, 15m, 1h, 4h]",
        "intervals: [1m, 5m, 15m, 1h, 4h, 1d]")
    (tmp_path / "config" / "settings.yaml").write_text(src)
    with pytest.raises(RuntimeError, match="1d") as exc:
        with patch("agentic_fx.service.PriceProvider"), \
             patch("agentic_fx.service._check_llama_swap"):
            build_app(tmp_path)
    assert "120" in str(exc.value)


def test_build_app_accepts_default_example_intervals_and_retention(tmp_path):
    """既定の settings.yaml.example (intervals 5 種、cache_retention_days=30)
    は起動を通る — 最大要求は 20 日 (4h: base=1h, ratio=4, lookback=5)。"""
    (tmp_path / "config").mkdir()
    src = open("config/settings.yaml.example", encoding="utf-8").read()
    (tmp_path / "config" / "settings.yaml").write_text(src)
    with patch("agentic_fx.service.PriceProvider") as pp, \
         patch("agentic_fx.service._check_llama_swap"):
        pp.return_value.healthcheck.return_value = "yfinance"
        app = build_app(tmp_path)
    app.close()
```

- [ ] **Step 32: テストを実行し失敗を確認する**

Run: `uv run pytest tests/test_service_app.py -k cache_retention -v`
Expected: `test_build_app_rejects_cache_retention_below_interval_requirement` と `test_build_app_error_message_names_interval_and_required_days` は **PASS してしまう可能性がある点に注意** — 検証がまだ無いので `RuntimeError` が上がらず `pytest.raises` が失敗 (`Failed: DID NOT RAISE`) するはずである。`test_build_app_accepts_default_example_intervals_and_retention` は既存経路で通るはずなので現時点で PASS していてよい (回帰確認用)。

Expected 実際の結果: 上 2 件が FAIL (`DID NOT RAISE <class 'RuntimeError'>`)

- [ ] **Step 33: `service.py` に `_validate_cache_retention` を実装し `_validate_startup` から呼ぶ**

ファイル冒頭の import に追加する:

```python
from agentic_fx.datafeed import cache_window, sources
```

`_validate_startup` の直前に新設する:

```python
# service.py:get_bars の既定引数 (datafeed/price_provider.py:get_bars) と
# 同じ値。ここが乖離すると保持期間検証が実際の要求と食い違う。
_DEFAULT_LOOKBACK_DAYS = 5


def _validate_cache_retention(settings) -> None:
    """起動時ガード: datafeed.cache_retention_days が構成済み intervals の
    live_window_days 最大値を下回っていないか (設計書 D2)。

    enabled に関わらず全既知チェーン source (mt5/twelvedata/yfinance) で
    検証する — config の enabled は運用中いつでも切り替わりうるため、
    「今 enabled な source だけ」を基準にすると、後から別 source を有効化
    した瞬間にキャッシュフォールバックが黙って壊れる余地を残す。
    """
    d = settings.datafeed
    for interval in d.intervals:
        for source in sources.NATIVE_INTERVALS:
            try:
                needed = cache_window.live_window_days(
                    source, interval, _DEFAULT_LOOKBACK_DAYS)
            except DataUnhealthy:
                continue  # この source は interval を提供も導出もできない
            if needed > d.cache_retention_days:
                raise RuntimeError(
                    f"datafeed.cache_retention_days={d.cache_retention_days} "
                    f"日は interval={interval!r} (source={source!r}) が要求 "
                    f"する {needed} 日を下回っています — "
                    "datafeed.cache_retention_days を "
                    f"{needed} 以上に増やすか、datafeed.intervals から "
                    f"{interval!r} を外してください")
```

`DataUnhealthy` の import を追加する:

```python
from agentic_fx.datafeed.health import DataUnhealthy
```

`_validate_startup` 本体の末尾に 1 行追加する:

```python
def _validate_startup(settings) -> None:
    ...  # 既存の pairs / schema / producer_source 検証 (無変更)
    if settings.plugin.producer_source not in ohlcv.KNOWN_OHLCV_SOURCES:
        raise RuntimeError(
            f"settings.plugin.producer_source={settings.plugin.producer_source!r} "
            f"is not a known source (known: {sorted(ohlcv.KNOWN_OHLCV_SOURCES)})")
    _validate_cache_retention(settings)
```

- [ ] **Step 34: テストを実行し green を確認する**

```bash
find . -name __pycache__ -type d -not -path "./.venv/*" -exec rm -rf {} +
uv run pytest tests/test_service_app.py -k "cache_retention or Cache_retention" -v
```
Expected: 3 件 PASS

- [ ] **Step 35: 全体テストを実行し既存の `build_app` を使う E2E テストが壊れていないことを確認する**

```bash
uv run pytest tests/test_e2e_phase1.py tests/test_e2e_plugin_signal.py \
  tests/test_e2e_worker_isolation.py tests/test_service_app.py -v
```
Expected: 全件 PASS (既定の `settings.yaml.example` は保持期間検証を通るため、既存 E2E は無影響)

#### Step 群 G: prune の maintenance 配線

**設計判断**: `Scheduler` に `on_signal_maintenance` と同型の新規 optional フック `on_cache_maintenance` を追加する。`on_signal_maintenance` を流用せず**専用フックにする** — signal maintenance と ohlcv prune は責務が異なり (前者は signal キューの状態機械、後者は DB 容量管理)、1 つの関数に混ぜると「signal maintenance の変更が意図せず prune の呼び出し頻度を変える」結合が生まれる。`core_lock` 保持は既存の `on_signal_maintenance` と同じ理由で許容される (DB のみの処理、通知はしない — 親プラン既知事実表)。

- [ ] **Step 36: `tests/core/test_scheduler_cache_maintenance.py` を新規作成し `on_cache_maintenance` 配線の失敗するテストを書く**

既存の `tests/core/test_scheduler_signal.py` と同じ流儀 (`Env`/`WED`/`SETTINGS` を `tests.core.test_scheduler` から再利用し、`Env` はコンストラクタで `on_cache_maintenance` を受けないので `env.sched.on_cache_maintenance = ...` を各テストで直接差し替える) を踏襲する。

```python
"""Scheduler の ohlcv_cache maintenance フックのテスト (プラン 9 Task 16)。

`Env`/`WED`/`SETTINGS` は `tests/core/test_scheduler.py` のものを再利用する
(既存の Env は on_cache_maintenance を持たないので、各テストで
`env.sched.on_cache_maintenance` を直接差し替える —
`tests/core/test_scheduler_signal.py` と同じ流儀)。
"""
from __future__ import annotations

from datetime import timedelta

from tests.core.test_scheduler import WED, Env


def test_tick_calls_on_cache_maintenance_every_tick_when_configured(tmp_path):
    """on_signal_maintenance と同型: 毎 tick 呼ばれる (間引きしない —
    prune は「追いつくまで毎 maintenance」実行する設計 D2)。"""
    env = Env(tmp_path)
    calls = []
    env.sched.on_cache_maintenance = lambda now: calls.append(now)
    env.sched.tick(WED)
    env.sched.tick(WED + timedelta(minutes=1))
    assert len(calls) == 2


def test_tick_skips_cache_maintenance_when_not_configured(tmp_path):
    """既定 None = 機能無効 — 未設定でも tick が例外なく完了すること。"""
    env = Env(tmp_path)
    env.sched.tick(WED)  # on_cache_maintenance 未設定のまま


def test_cache_maintenance_failure_does_not_stop_tick(tmp_path):
    """_run_data_hook と同じ fail-open 契約 — prune 失敗が資金保護
    (_process_exits) を止めてはならない。"""
    def _boom(now):
        raise RuntimeError("prune failed")
    env = Env(tmp_path)
    env.sched.on_cache_maintenance = _boom
    env.sched.tick(WED)  # 例外を送出せず完了すること
```

- [ ] **Step 37: テストを実行し `on_cache_maintenance` 属性が無く落ちることを確認する**

Run: `uv run pytest tests/core/test_scheduler_cache_maintenance.py -v`
Expected: 全件 FAIL (`AttributeError: 'Scheduler' object has no attribute 'on_cache_maintenance'`)

- [ ] **Step 38: `src/agentic_fx/core/scheduler.py` に `on_cache_maintenance` を追加する**

`__init__` のシグネチャに追加する (`on_signal_maintenance` の直後):

```python
                 on_signal_maintenance: Callable[[datetime], None] | None = None,
                 on_cache_maintenance: Callable[[datetime], None] | None = None,
                 signal_due_fn: Callable[[datetime], bool] | None = None,
```

本体に代入を追加する (`self.on_signal_maintenance = on_signal_maintenance` の直後):

```python
        self.on_signal_maintenance = on_signal_maintenance
        # プラン 9 Task 16: ohlcv_cache の保持ポリシー (prune) 用フック。
        # 既定 None = 機能無効 (既存テスト互換)。signal maintenance とは
        # 責務が異なるため専用フックにする (責務混在を避ける)。
        self.on_cache_maintenance = on_cache_maintenance
```

`_run_hooks` に追加する (`on_signal_maintenance` の呼び出しブロックの直後):

```python
        if self.on_signal_maintenance is not None:
            self._run_data_hook(
                "signal_maintenance", lambda: self.on_signal_maintenance(now))
        if self.on_cache_maintenance is not None:
            self._run_data_hook(
                "cache_maintenance", lambda: self.on_cache_maintenance(now))
```

- [ ] **Step 39: テストを実行し green を確認する**

```bash
find . -name __pycache__ -type d -not -path "./.venv/*" -exec rm -rf {} +
uv run pytest tests/core/test_scheduler_cache_maintenance.py tests/core/test_scheduler.py \
  tests/core/test_scheduler_signal.py tests/core/test_scheduler_tick_order.py -v
```
Expected: 全件 PASS (新規 3 件 + 既存 scheduler 系テストが無影響)

- [ ] **Step 40: `service.py` に prune 呼び出しの maintenance 実体を追加する失敗するテストを書く**

`tests/test_service_app.py` に追加する:

```python
def test_build_app_wires_cache_maintenance_hook(tmp_path):
    """build_app が Scheduler に on_cache_maintenance を配線すること
    (None のままではない)。"""
    (tmp_path / "config").mkdir()
    src = open("config/settings.yaml.example", encoding="utf-8").read()
    (tmp_path / "config" / "settings.yaml").write_text(src)
    with patch("agentic_fx.service.PriceProvider") as pp, \
         patch("agentic_fx.service._check_llama_swap"):
        pp.return_value.healthcheck.return_value = "yfinance"
        app = build_app(tmp_path)
    try:
        assert app.scheduler.on_cache_maintenance is not None
    finally:
        app.close()


def test_cache_maintenance_prunes_old_cache_rows(tmp_path):
    """配線された on_cache_maintenance が実際に ohlcv_cache を刈ること
    (cutoff = tick 時刻 - cache_retention_days)。"""
    from agentic_fx.core.contracts import Bar
    from agentic_fx.store import ohlcv

    (tmp_path / "config").mkdir()
    src = open("config/settings.yaml.example", encoding="utf-8").read()
    (tmp_path / "config" / "settings.yaml").write_text(src)
    with patch("agentic_fx.service.PriceProvider") as pp, \
         patch("agentic_fx.service._check_llama_swap"):
        pp.return_value.healthcheck.return_value = "yfinance"
        app = build_app(tmp_path, clock=FixedClock(NOW))
    try:
        # ⚠️ **境界の両側**を置く。旧版は 40 日前の 1 本だけを入れて「消えたこと」
        # しか見ておらず、**`cutoff` を retention 非依存の固定値にする変異が生存**
        # していた (ローカル LLM muse-glimmer が検出・指揮者が裏取り) — 例えば
        # `cutoff = NOW` (全削除) でもこのテストは PASS してしまう。
        # 残るべき 1 本を足して初めて「retention 由来の cutoff」を pin できる。
        old_bar = Bar("USDJPY", "1m", NOW - timedelta(days=40),      # 保持外 → 消える
                      148.0, 148.1, 147.9, 148.05, 10)
        keep_bar = Bar("USDJPY", "1m", NOW - timedelta(days=10),     # 保持内 → 残る
                       149.0, 149.1, 148.9, 149.05, 11)
        ohlcv.upsert_cache_bars(app.conn_core, [old_bar, keep_bar],
                                source="yfinance")

        app.scheduler.on_cache_maintenance(NOW)

        rows = ohlcv.load_cache_bars(
            app.conn_core, "USDJPY", "1m", source="yfinance",
            since=NOW - timedelta(days=41))
        # 「消えた」だけでなく「残った」も見る。全削除・無削除の双方を殺す。
        assert [b.ts for b in rows] == [NOW - timedelta(days=10)]
    finally:
        app.close()
```

(`NOW`/`FixedClock` は `tests/test_service_app.py` 冒頭の既存定数・import を使う — 無ければ `from agentic_fx.core.contracts import FixedClock` と `NOW = datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc)` をファイル冒頭に揃える。)

- [ ] **Step 41: テストを実行し失敗を確認する**

Run: `uv run pytest tests/test_service_app.py -k "cache_maintenance" -v`
Expected: `test_build_app_wires_cache_maintenance_hook` は FAIL (`AssertionError: assert None is not None`)。`test_cache_maintenance_prunes_old_cache_rows` は FAIL (`AttributeError: 'NoneType' object is not callable` — `on_cache_maintenance` が None のまま)

- [ ] **Step 42: `service.py` に prune の実配線を追加する**

ファイル冒頭付近、他の定数と同じ並びに追加する:

```python
# プラン 9 Task 16: 1 回の DELETE が SL/TP 監視 (_process_exits) の許容
# 遅延を超えないことを基準にした有界バッチ上限。実測は Task 16 Step 43
# のコメントを参照 (実装時に実測して更新すること)。
_OHLCV_PRUNE_BATCH_LIMIT = 5000
```

`build_app` 内、`on_signal_maintenance` 定義の直後に追加する:

```python
        def on_cache_maintenance(now: datetime) -> None:
            # プラン 9 Task 16: ohlcv_cache の保持ポリシー。有界バッチ
            # (_OHLCV_PRUNE_BATCH_LIMIT) × 毎 maintenance 実行で、初回の
            # 大量削除 (既存蓄積分) の lock 保持窓を抑えつつ、定常状態では
            # 1 回の呼び出しで日次増分に追いつく (設計書 D2)。
            cutoff = now - timedelta(days=settings.datafeed.cache_retention_days)
            ohlcv.prune_cache(conn_core, cutoff=cutoff,
                              limit=_OHLCV_PRUNE_BATCH_LIMIT)
```

`Scheduler(...)` の呼び出しに引数を追加する:

```python
        scheduler = Scheduler(conn=conn_core, executor=executor,
                              settings=settings, state_store=state,
                              activity=activity, bars_fn=bars_fn,
                              on_trade_mission=on_trade_mission,
                              on_news_cycle=collector.collect,
                              on_econ_cycle=econ.refresh,
                              on_signal_maintenance=on_signal_maintenance,
                              on_cache_maintenance=on_cache_maintenance,
                              signal_due_fn=signal_due_fn,
                              stop_event=stop_event)
```

`timedelta` の import が無ければ追加する (`from datetime import datetime, timedelta`)。`ohlcv` は既に `_validate_startup` で import 済み。

- [ ] **Step 43: 有界バッチ上限を実データで実測し、コメントへ書き込む**

以下を実行する (`uv run python` の対話的スクリプト。実 DB ではなく tmp DB を使う):

```bash
uv run python - <<'EOF'
import tempfile, time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agentic_fx.core.contracts import Bar
from agentic_fx.store import ohlcv
from agentic_fx.store.db import connect, init_db

with tempfile.TemporaryDirectory() as d:
    conn = connect(Path(d) / "bench.db")
    init_db(conn)
    now = datetime(2026, 7, 22, tzinfo=timezone.utc)
    bars = [Bar("USDJPY", "1m", now - timedelta(minutes=i), 148.0, 148.1,
               147.9, 148.05, 10) for i in range(5000)]
    ohlcv.upsert_cache_bars(conn, bars, source="yfinance")
    t0 = time.monotonic()
    n = ohlcv.prune_cache(conn, cutoff=now + timedelta(days=1), limit=5000)
    dt = time.monotonic() - t0
    print(f"deleted={n} elapsed={dt:.4f}s")
EOF
```

実測した `elapsed` の値を、`_OHLCV_PRUNE_BATCH_LIMIT` のコメントに書き足す (例: 「実測 2026-08-11: 5000 行の DELETE が X.XXX 秒 (ローカル SQLite、WAL)」)。SL/TP 監視の許容遅延 (`_BAR_FRESHNESS = timedelta(minutes=5)` — `scheduler.py:26`) に対して十分小さい (数百 ms 以下) ことを確認する。**実測値が閾値に対して大きすぎる場合は `_OHLCV_PRUNE_BATCH_LIMIT` を実測に基づいて下げ、テストを再実行すること。**

- [ ] **Step 44: テストを実行し green を確認する**

```bash
find . -name __pycache__ -type d -not -path "./.venv/*" -exec rm -rf {} +
uv run pytest tests/test_service_app.py -v
```
Expected: 全件 PASS

#### Step 群 H: 変異検証 + コミット

- [ ] **Step 45: 全体テストを実行し既存件数が壊れていないことを確認する**

```bash
find . -name __pycache__ -type d -not -path "./.venv/*" -exec rm -rf {} +
uv run pytest -q
```
Expected: 全件 PASS (親プランの既存件数 1726 + Task 8/16 で追加した件数)

- [ ] **Step 46: 変異を当てて対応するテストが落ちることを確認する (mutation ledger — D2 の変異リスト全項目 + spec ③ 改訂 5 検査点 17 の構造的裏付け)**

`find . -name __pycache__ -type d -not -path "./.venv/*" -exec rm -rf {} +` を各変異の前後で必ず実行する。各変異を注入したら `grep -n` で改変箇所を目視確認してから該当テストを実行する。

| # | 変異 | 対象行 | 殺すテスト |
|---|---|---|---|
| M16-1 | **【最重要】`prune_cache` の対象テーブルを `ohlcv_cache` から `ohlcv_history` に差し替える** | `store/ohlcv.py:prune_cache` の SQL | `test_prune_cache_never_touches_ohlcv_history` |
| M16-2 | `upsert_cache_bars` の source 検証 (`LIVE_SOURCES` allowlist) を削除する | `store/ohlcv.py:upsert_cache_bars` | `test_upsert_cache_bars_rejects_non_live_source` |
| M16-3 | `load_cache_bars` の source 検証を削除する | `store/ohlcv.py:load_cache_bars` | `test_load_cache_bars_rejects_history_source` |
| M16-4 | `import_history_bars` の source 検証を削除する | `store/ohlcv.py:import_history_bars` | `test_import_history_bars_rejects_live_source` |
| M16-5 | `load_history_bars` の source 検証を削除する | `store/ohlcv.py:load_history_bars` | `test_load_history_bars_rejects_live_source` |
| M16-6 | `load_history_spread` の source 検証を削除する | `store/ohlcv.py:load_history_spread` | `test_load_history_spread_rejects_live_source` |
| M16-7 | `_migrate_ohlcv_split` の振り分け条件を反転する (`in LIVE_SOURCES` → `not in LIVE_SOURCES`) → 未知/履歴 source がキャッシュへ、ライブ source が履歴へ入れ替わる | `store/db.py:_migrate_ohlcv_split` | `test_migrate_ohlcv_split_routes_live_and_import_sources_correctly` と `test_migrate_ohlcv_split_routes_unknown_source_to_history` の両方 |
| M16-8 | `init_db` が `_migrate_ohlcv_split` を呼ぶ行を削除する | `store/db.py:init_db` | `test_init_db_migrates_legacy_v1_ohlcv_into_cache_table` (`ohlcv_cache` が空のまま失敗する) |
| M16-9 | `prune_cache` の `LIMIT` を外す (無制限 DELETE) | `store/ohlcv.py:prune_cache` の SQL | `test_prune_cache_respects_limit` |
| M16-10 | `prune_cache` の `limit` 非正値検証を削除する | `store/ohlcv.py:prune_cache` | `test_prune_cache_rejects_non_positive_limit` |
| M16-11 | `_validate_cache_retention` の呼び出しを `_validate_startup` から削除する | `service.py:_validate_startup` | `test_build_app_rejects_cache_retention_below_interval_requirement` |
| M16-12 | `_validate_cache_retention` の比較演算子を反転する (`needed > d.cache_retention_days` → `<`) | `service.py:_validate_cache_retention` | `test_build_app_rejects_cache_retention_below_interval_requirement` (今度は逆に、正常設定側で誤って RuntimeError が出て `test_build_app_accepts_default_example_intervals_and_retention` が落ちる) |
| M16-13 | `Scheduler.__init__`/`_run_hooks` から `on_cache_maintenance` の呼び出しを削除する (配線を切る) | `core/scheduler.py:_run_hooks` | `test_tick_calls_on_cache_maintenance_every_tick_when_configured` |
| M16-14 | `build_app` の `Scheduler(...)` から `on_cache_maintenance=on_cache_maintenance` 引数を削除する | `service.py:build_app` | `test_build_app_wires_cache_maintenance_hook` と `test_cache_maintenance_prunes_old_cache_rows` |
| M16-15 | `on_cache_maintenance` の `cutoff` 計算から `settings.datafeed.cache_retention_days` を外し固定値にする | `service.py:on_cache_maintenance` | **実測 (2026-08-12): 記載の `test_cache_maintenance_prunes_old_cache_rows` では生存した。** 固定値として自然に選ぶ `30` が `settings.yaml.example` の既定と一致し**等価変異**になるため (40 日/10 日の境界両側を置いても 30 日と 30 日は同じ挙動)。既定と異なる保持期間 (45 日) を明示し、30 日固定なら消える 35 日前のバーが残ることを見る `test_cache_maintenance_cutoff_follows_configured_retention_days` を追加して kill した |
| M16-16 | `backtest/cli.py` の `cov`/`run`/`corr` から `choices=sorted(ohlcv.IMPORT_SOURCES)` を削除する | `backtest/cli.py:register_subparsers` | **実測 (2026-08-12): 記載の 3 本では生存した。** `choices` が無くても後段の初期化ガードが同じ `SystemExit(2)` を返すため、exit code だけを見る assert では**拒否した主体を区別できない**。3 本に `assert "invalid choice" in capsys.readouterr().err` を足して kill した (`test_backtest_run_rejects_live_source` / `test_history_coverage_rejects_live_source` / `test_analyze_corr_rejects_live_source`) |
| X-1 | (指揮者の自主変異) `timeframes._table` の `LIVE_SOURCES` 分岐が `ohlcv_history` を返すようにする | `backtest/timeframes.py:_table` | `test_live_source_reads_the_cache_table` |
| X-2 | (指揮者の自主変異) `timeframes._table` の末尾 `raise` の手前で `ohlcv_history` を返す (fail closed を既定値へ落とす) | `backtest/timeframes.py:_table` | `test_unknown_source_is_rejected` |
| M16-17 | `_ohlcv_legacy_exists` を常に `False` を返すようにする (migration チェーンを丸ごと無効化) | `store/db.py:_ohlcv_legacy_exists` | `test_init_db_migrates_legacy_v1_ohlcv_into_cache_table` |

**変異は 1 つずつ独立に当て、殺した固有のテスト名を記録する。**

- [ ] **Step 47: 全体テストを再度実行し green に戻っていることを確認する**

```bash
find . -name __pycache__ -type d -not -path "./.venv/*" -exec rm -rf {} +
uv run pytest -q
```
Expected: 全件 PASS

- [ ] **Step 48: コミット**

```bash
git add src/agentic_fx/store/db.py src/agentic_fx/store/ohlcv.py \
  src/agentic_fx/config.py config/settings.yaml.example \
  src/agentic_fx/core/scheduler.py src/agentic_fx/service.py \
  src/agentic_fx/datafeed/price_provider.py \
  src/agentic_fx/backtest/importer.py src/agentic_fx/backtest/mt5_import.py \
  src/agentic_fx/backtest/replay.py src/agentic_fx/backtest/timeframes.py \
  src/agentic_fx/backtest/holdout.py src/agentic_fx/backtest/cli.py \
  tests/store/test_ohlcv.py tests/store/test_db.py \
  tests/datafeed/test_price_provider.py tests/store/test_records.py \
  tests/tools/test_mission_registry.py tests/backtest/ \
  tests/core/test_scheduler.py tests/core/test_scheduler_cache_maintenance.py \
  tests/test_config.py tests/test_service_app.py
git commit -m "$(cat <<'EOF'
feat: ohlcv を ohlcv_cache/ohlcv_history へ分割 + 保持ポリシー (プラン9 Task16)

キャッシュ (ライブ逐次・削除対象) と履歴 (バックテスト・削除しない) を
テーブルで構造的に分離する。書き込み/読み込み関数ごとに受理 source を
allowlist で強制し、呼び出し側がテーブルを引数で選ぶ余地を無くす。
prune (有界バッチ・毎 maintenance) と起動時の保持期間検証を追加し、
mt5/mt5-live の 1 文字違いで履歴を無音破壊する経路を構造で防ぐ。

既存 DB は v1→v2→split の migration チェーンで収束する
(未知 source は履歴側へ隔離、fail-safe)。
EOF
)"
```

---

### Task 9: `lookback_days` を `_cached_bars` まで配線する

**対応する spec ③ 検査点**: 15 (非既定の `lookback_days` が `get_bars` から `load_cache_bars(since=...)` まで届く)。

**設計判断 (このファイル内でのみ確定する事項)**:
- 本 task は**配線のみ**を検査する。`since` の計算式は `now - timedelta(days=lookback_days)` という**素朴な (native/derive の区別も floor も無い) 暫定実装**にとどめる — 正しい比・floor の適用は Task 10 の責務。この分離は spec ③ 自身が要求している (「helper の単体テストだけでは配線欠落を検出できない」ので配線と窓計算のロジックを別々に検査可能にする)。
- `_cached_bars` のシグネチャに `lookback_days: int` を**既定値なしの必須引数**として追加する。呼び出し元 `get_bars` はローカル変数 `lookback_days` (自身の引数) をそのまま渡す。
- テストは `ohlcv.load_cache_bars` をスパイし、**`since` が `None` でないこと**と**`lookback_days` を変えると `since` も変わること** (`lookback_days=20` の `since` が `lookback_days=1` の `since` より過去になる) の両方を確認する。後者が「helper 内で既定値を推測する」退行 (spec ③ の必須変異) を検出する — 前者だけだと固定値埋め込みでも通ってしまう。

**Files:**
- Modify: `src/agentic_fx/datafeed/price_provider.py`
- Test: `tests/datafeed/test_price_provider.py`

**Interfaces:**
- Consumes: Task 16 の `ohlcv.load_cache_bars(conn, symbol, interval, *, source, since=None, until=None)`
- Produces: `_cached_bars` の新シグネチャ `_cached_bars(self, pair, interval, now, errors, lookback_days)` (Task 10 が引き続き変更する — Task 10 は `since` の計算式だけを差し替え、シグネチャは維持する)

- [ ] **Step 1: `tests/datafeed/test_price_provider.py` に失敗するテストを追加する**

ファイル末尾に追加する (`_provider`/`_fresh_bars`/`ohlcv`/`patch`/`pytest`/`DataUnhealthy` は既存 import をそのまま使う):

```python
def test_cached_bars_since_is_not_none_and_varies_with_lookback_days(
        tmp_path, monkeypatch):
    """spec ③ テスト 15: 非既定の lookback_days が get_bars から
    load_cache_bars(since=...) まで届く。helper 側で既定値を推測する退行は
    「lookback_days を変えても since が変わらない」形で現れるため、
    2 回の呼び出しを比較して単調性を確認する (固定値埋め込みも検出する)。
    """
    conn, p = _provider(tmp_path)
    captured_since = []
    orig = ohlcv.load_cache_bars

    def _spy(conn_, symbol, interval, *, source, since=None, until=None):
        captured_since.append(since)
        return orig(conn_, symbol, interval, source=source, since=since,
                    until=until)

    monkeypatch.setattr(ohlcv, "load_cache_bars", _spy)
    with patch("agentic_fx.datafeed.price_provider.sources.yf_bars",
               side_effect=OSError("down")):
        with pytest.raises(DataUnhealthy):
            p.get_bars("USDJPY", "1m", 1)
        since_for_1 = captured_since[-1]
        with pytest.raises(DataUnhealthy):
            p.get_bars("USDJPY", "1m", 20)
        since_for_20 = captured_since[-1]

    assert since_for_1 is not None
    assert since_for_20 is not None
    assert since_for_20 < since_for_1
```

- [ ] **Step 2: テストを実行し `_cached_bars` が `lookback_days` を受け取らず落ちることを確認する**

Run: `uv run pytest tests/datafeed/test_price_provider.py -k cached_bars_since -v`
Expected: FAIL — `captured_since` の要素が全て `None` のままなので `assert since_for_1 is not None` で AssertionError (現行 `_cached_bars` は `load_cache_bars` を `since` なしで呼ぶため)

- [ ] **Step 3: `price_provider.py` の `_cached_bars` に `lookback_days` を配線する**

`get_bars` の呼び出し (旧 `:182`) を更新する:

```python
        got = self._cached_bars(pair, interval, now, errors, lookback_days)
```

`_cached_bars` のシグネチャと候補ループ内の呼び出しを更新する (旧 `:192-247`)。**既存 docstring の全段落 (F1/F6/DERIVE_ONLY 残骸の根拠) と旧 `:235` の fail closed コメントはそのまま残し**、docstring 末尾に下記転写ブロック中の `lookback_days` に言及する段落を**追記**する。コード側の変更は ①シグネチャに `lookback_days: int` を追加 ②`since = now - timedelta(days=lookback_days)` を `d = ...` の直後に挿入 ③`load_cache_bars(...)` に `since=since` を追加 — の 3 箇所のみ。

```python
    def _cached_bars(self, pair: str, interval: str, now: datetime,
                     errors: list[str], lookback_days: int
                     ) -> tuple[list[Bar], str] | None:
        """キャッシュから interval の足を作る。健全性検証を通らなければ None。

        `lookback_days` は必須引数 (既定値で誤魔化さない — spec ③: helper
        側で推測すると `latest_1m_bar` の lookback_days=1 と通常呼び出しの
        既定 5 を区別できなくなる)。

        本 task (プラン 9 Task 9) 時点では `since` の計算は素朴な
        `now - timedelta(days=lookback_days)` — native/derive の区別・
        floor 適用は Task 10 が実装する。配線そのものが正しいことを
        独立に検査するための中間状態。
        """
        d = self.settings.datafeed
        since = now - timedelta(days=lookback_days)
        candidates = [i for i in [interval, *self._base_candidates(interval)]
                      if i not in DERIVE_ONLY_INTERVALS]
        live_sources = [name for name, _ in
                        self._chain(pair, kind="bars", interval=interval)]
        for name in live_sources:
            storage_name = _storage_source(name)
            for src in candidates:
                cached = ohlcv.load_cache_bars(self.conn, pair, src,
                                               source=storage_name,
                                               since=since)
                if not cached:
                    continue
                label = ("cache" if src == interval else f"cache({src})")
                label = f"{label}[{storage_name}]"
                try:
                    validate_bars(cached, now, d.freshness_max_min,
                                  sources.INTERVAL_MIN[src])
                    if src == interval:
                        return cached, "cache"
                    derived = self._resample(cached, pair, interval)
                    validate_bars(derived, now, d.freshness_max_min,
                                  sources.INTERVAL_MIN[interval])
                except Exception as e:  # noqa: BLE001
                    errors.append(f"{label}: {_safe_error_text(e)}")
                    continue
                return derived, f"cache({src}→{interval} derived)"
        return None
```

`timedelta` の import をファイル冒頭に追加する (現行は `from datetime import datetime` のみ):

```python
from datetime import datetime, timedelta
```

- [ ] **Step 4: テストを実行し green を確認する**

```bash
find . -name __pycache__ -type d -not -path "./.venv/*" -exec rm -rf {} +
uv run pytest tests/datafeed/test_price_provider.py -v
```
Expected: 全件 PASS (キャッシュ経路を踏む既存フィクスチャの最長スパンは `_fresh_bars(interval="1h", n=100)` = 100h で、既定 `lookback_days=5` = 120h に対し余裕 20h。`_fresh_bars(interval="4h", n=30)` = 120h ちょうどの残骸フィクスチャは `DERIVE_ONLY_INTERVALS` により candidates から外れ読まれない。よって `since = now - N日` で排除される行は存在しない)

- [ ] **Step 5: 全体テストを実行する**

```bash
uv run pytest -q
```
Expected: 全件 PASS

- [ ] **Step 6: 変異を当てて対応するテストが落ちることを確認する**

`find . -name __pycache__ -type d -not -path "./.venv/*" -exec rm -rf {} +` を前後で実行する。

| # | 変異 | 対象行 | 殺すテスト |
|---|---|---|---|
| M9-1 | `_cached_bars` 内で `lookback_days` を無視し固定値 (例: `5`) を使う | `_cached_bars` の `since` 計算 | `test_cached_bars_since_is_not_none_and_varies_with_lookback_days` |
| M9-2 | `load_cache_bars` 呼び出しから `since=since` 引数を削除する | `_cached_bars` の `ohlcv.load_cache_bars(...)` 呼び出し | `test_cached_bars_since_is_not_none_and_varies_with_lookback_days` |
| M9-3 | `get_bars` の `_cached_bars` 呼び出しから `lookback_days` 引数を削除する (シグネチャ側は必須のままなので `TypeError` になり、既存キャッシュ系テストが軒並み落ちる — 独立した検査点ではないが配線が壊れたことの直接的な証拠) | `get_bars` の `self._cached_bars(...)` 呼び出し | `test_get_bars_falls_back_to_cache` ほか既存キャッシュ系テスト全件 |

- [ ] **Step 7: 全体テストを再実行し green に戻っていることを確認する**

```bash
find . -name __pycache__ -type d -not -path "./.venv/*" -exec rm -rf {} +
uv run pytest -q
```
Expected: 全件 PASS

- [ ] **Step 8: コミット**

```bash
git add src/agentic_fx/datafeed/price_provider.py tests/datafeed/test_price_provider.py
git commit -m "$(cat <<'EOF'
feat: lookback_days を _cached_bars まで配線する (プラン9 Task9)

get_bars から _cached_bars へ lookback_days を必須引数で通し、
load_cache_bars(since=...) まで到達させる。窓計算 (native/derive の
区別・floor 適用) は Task 10 で実装する — 本 task は配線のみ。
EOF
)"
```

> 注記 (2026-08-14 着手前検証): Step 4 の旧根拠 (「_fresh_bars が保証」) は事実誤りだったため差し替え済み。Task 10 で窓を `live_window_days` に変える際はこの実測値 (最長フィクスチャ 100h) を前提にすること。

---

### Task 10: `_cached_bars` に窓計算 + floor を適用する

**対応する spec ③ 検査点**: 1 (`since` 付きで `load_cache_bars` を呼ぶ — Task 9 で配線済みだが正しい値になるのは本 task から), 4 (窓はキャッシュ側の base に依存しない), 5 (窓より古い行が結果に入らない), 6 (直読候補は floor しない), 7 (導出候補は要求 interval 境界へ floor), 8 (クエリ切断由来の部分バケットが先頭に出ない — 7 とは別の観測点), 13 (蓄積が大きくても読み込み量が窓ぶんに収まる), 16 (先順位 source の窓計算失敗が後順位 source を道連れにしない)。

**設計判断 (このファイル内でのみ確定する事項)**:
- **DRY 解消 (Task 8 が残した一時的重複の解消)**: `price_provider.py` の `_base_candidates`/`_finest_native_base` を `cache_window.base_candidates`/`cache_window.finest_native_base` への委譲に置き換える。モジュールレベルの `DERIVE_ONLY_INTERVALS` も `cache_window.DERIVE_ONLY_INTERVALS` を re-export する形に変える (値は不変、定義元を一本化するだけ)。これは**挙動を変えない純粋なリファクタ**なので、新規の失敗するテストは書かず、既存の derive 系テスト (`test_get_bars_derives_4h_from_1h_on_yfinance` 等) を回帰確認に使う。
- **窓計算は source 単位の `try` の中に置く** (spec ③ 必須事項)。現行構造は「source を外側・interval 候補を内側」の 2 重ループになっており、窓計算 (`cache_window.live_window_days` + `floor_to_interval`) は**外側ループの先頭、内側ループより前・`try` の中**に置く。`cache_window.finest_native_base` が送出しうる `DataUnhealthy` をここで拾い、`errors` に記録して次の source へ進む (`continue`) — ライブ経路の per-source try (`get_bars` 本体) と対称の隔離。
- **直読候補と導出候補で `since` を分ける**: `window_start = now - timedelta(days=window_days)` (floor しない) と `derive_since = cache_window.floor_to_interval(window_start, interval)` (要求 `interval` の境界へ floor — base の interval ではない) を source 単位で 1 回だけ計算し、候補ループ内で `since = window_start if src == interval else derive_since` を選ぶ。

**Files:**
- Modify: `src/agentic_fx/datafeed/price_provider.py`
- Test: `tests/datafeed/test_price_provider.py`

**Interfaces:**
- Consumes: Task 8 の `cache_window.live_window_days`/`cache_window.floor_to_interval`/`cache_window.base_candidates`/`cache_window.finest_native_base`/`cache_window.DERIVE_ONLY_INTERVALS`
- Produces: `_cached_bars` の最終形 (シグネチャは Task 9 と同一 — 中身のみ変わる。Task 11 が `get_bars`/`_cached_bars` の通しの挙動を E2E で pin する)

- [ ] **Step 1: DRY リファクタ — `_base_candidates`/`_finest_native_base`/`DERIVE_ONLY_INTERVALS` を `cache_window` への委譲に置き換える**

ファイル冒頭の import に追加する:

```python
from agentic_fx.datafeed import cache_window
```

`DERIVE_ONLY_INTERVALS` のローカル定義 (旧 `:33`) を削除し、`cache_window` のものを re-export する:

```python
# 定義元は datafeed/cache_window.py (Task 8/10)。ここでは re-export のみ —
# 本ファイル内の DERIVE_ONLY_INTERVALS という裸名参照 (複数箇所) を変えずに
# 定義元を一本化するため。
DERIVE_ONLY_INTERVALS = cache_window.DERIVE_ONLY_INTERVALS
```

`_base_candidates`/`_finest_native_base` メソッドの本体を委譲に置き換える (docstring は要点のみ残す):

```python
    def _base_candidates(self, interval: str) -> list[str]:
        """interval を導出できる base 足を、粗い順に返す。
        実装は cache_window.base_candidates に委譲する (Task 10 — Task 8 が
        一時的に重複させていたロジックの定義元を一本化)。"""
        return cache_window.base_candidates(interval)

    def _finest_native_base(self, source: str, interval: str) -> str:
        """interval を導出できる、最も粗いネイティブ足を選ぶ。
        実装は cache_window.finest_native_base に委譲する (Task 10)。"""
        return cache_window.finest_native_base(source, interval)
```

- [ ] **Step 2: 既存の derive 系テストを実行し、リファクタで挙動が変わっていないことを確認する**

```bash
find . -name __pycache__ -type d -not -path "./.venv/*" -exec rm -rf {} +
uv run pytest tests/datafeed/test_price_provider.py -v
```
Expected: 全件 PASS (完全な回帰確認 — 1 件でも落ちたら委譲の実装ミス)

- [ ] **Step 3: `tests/datafeed/test_price_provider.py` に窓計算・floor の失敗するテストを追加する**

ファイル末尾に追加する:

```python
def test_window_independent_of_cache_base_used_for_derivation(tmp_path, monkeypatch):
    """spec ③ テスト 4: 窓は「キャッシュ側の base」に依存しない — 1m から
    1h を作っても窓は 5 日 (キャッシュ base の比を誤って使う退行なら
    60 倍の 300 日になる)。"""
    conn, p = _provider(tmp_path)
    captured = {}
    orig = ohlcv.load_cache_bars

    def _spy(conn_, symbol, interval, *, source, since=None, until=None):
        captured.setdefault(interval, since)
        return orig(conn_, symbol, interval, source=source, since=since,
                    until=until)

    monkeypatch.setattr(ohlcv, "load_cache_bars", _spy)
    p._cached_bars("USDJPY", "1h", NOW, [], 5)

    since_1m = captured["1m"]
    assert NOW - since_1m <= timedelta(days=5, hours=1)  # floor の余白込み
    assert NOW - since_1m > timedelta(days=4)


def test_rows_older_than_window_are_excluded(tmp_path):
    """spec ③ テスト 5: 窓より古い行が結果に入らない。"""
    conn, p = _provider(tmp_path)
    old_bar = Bar("USDJPY", "1h", NOW - timedelta(days=10), 148, 148.1,
                 147.9, 148.05, 10)
    fresh_bar = Bar("USDJPY", "1h", NOW - timedelta(hours=1), 148, 148.1,
                    147.9, 148.05, 10)
    ohlcv.upsert_cache_bars(conn, [old_bar, fresh_bar], source="yfinance")

    result = p._cached_bars("USDJPY", "1h", NOW, [], 5)

    assert result is not None
    cached, origin = result
    assert origin == "cache"
    assert len(cached) == 1
    assert cached[0].ts == fresh_bar.ts


def test_direct_read_candidate_since_is_not_floored(tmp_path, monkeypatch):
    """spec ③ テスト 6: 直読候補 (src == interval) は window_start を
    そのまま渡す (floor すると要求より広く返してしまう)。"""
    conn, p = _provider(tmp_path)
    now = datetime(2026, 7, 22, 0, 47, 0, tzinfo=timezone.utc)
    captured = {}
    orig = ohlcv.load_cache_bars

    def _spy(conn_, symbol, interval, *, source, since=None, until=None):
        captured.setdefault(interval, since)
        return orig(conn_, symbol, interval, source=source, since=since,
                    until=until)

    monkeypatch.setattr(ohlcv, "load_cache_bars", _spy)
    p._cached_bars("USDJPY", "1h", now, [], 0)  # lookback_days=0 → window_start=now

    assert captured["1h"] == now  # 未 floor


def test_derive_candidate_since_is_floored_to_requested_interval(
        tmp_path, monkeypatch):
    """spec ③ テスト 7: 導出候補 (src != interval) は要求 interval の
    境界へ floor される — floor の基準は要求 interval であって base の
    interval ではない (1h を作るなら 1h 境界)。"""
    conn, p = _provider(tmp_path)
    now = datetime(2026, 7, 22, 0, 47, 0, tzinfo=timezone.utc)
    captured = {}
    orig = ohlcv.load_cache_bars

    def _spy(conn_, symbol, interval, *, source, since=None, until=None):
        captured.setdefault(interval, since)
        return orig(conn_, symbol, interval, source=source, since=since,
                    until=until)

    monkeypatch.setattr(ohlcv, "load_cache_bars", _spy)
    p._cached_bars("USDJPY", "1h", now, [], 0)

    assert captured["1m"] == datetime(2026, 7, 22, 0, 0, 0, tzinfo=timezone.utc)


def test_query_truncation_does_not_leave_partial_leading_bucket(tmp_path):
    """spec ③ テスト 8 (テスト 7 とは別の観測点): floor されたクエリ境界の
    ため、導出バケットの先頭が「クエリ切断由来の部分バケット」にならない
    こと。open を分単位で単調増加させ、floor が無ければバケットの open が
    境界時刻の行ではなく now 直前の 1 行だけを反映してしまうことを検出
    する。"""
    conn, p = _provider(tmp_path)
    now = datetime(2026, 7, 22, 0, 47, 0, tzinfo=timezone.utc)
    boundary = datetime(2026, 7, 22, 0, 0, 0, tzinfo=timezone.utc)
    bars = [Bar("USDJPY", "1m", boundary + timedelta(minutes=i),
               148.0 + i * 0.01, 148.1 + i * 0.01, 147.9 + i * 0.01,
               148.05 + i * 0.01, 10)
           for i in range(48)]  # 00:00 (境界) 〜 00:47 (= now)
    ohlcv.upsert_cache_bars(conn, bars, source="yfinance")

    result = p._cached_bars("USDJPY", "1h", now, [], 0)  # lookback_days=0

    assert result is not None
    derived, origin = result
    assert origin == "cache(1m→1h derived)"
    assert len(derived) == 1
    assert derived[0].open == pytest.approx(148.00)  # 境界行 (i=0) の open


def test_read_volume_bounded_by_window_regardless_of_accumulation(tmp_path):
    """spec ③ テスト 13: DB の蓄積量に関係なく、読み込み件数が窓ぶんに
    収まる。"""
    conn, p = _provider(tmp_path)
    bars = [Bar("USDJPY", "1h", NOW - timedelta(hours=i), 148, 148.1, 147.9,
               148.05, 10) for i in range(30 * 24)]  # 30 日分
    ohlcv.upsert_cache_bars(conn, bars, source="yfinance")

    result = p._cached_bars("USDJPY", "1h", NOW, [], 5)  # 窓は 5 日 (native)

    assert result is not None
    cached, origin = result
    assert origin == "cache"
    assert len(cached) <= 5 * 24 + 1  # 窓 5 日ぶん (端数の余裕を許容)


def test_prior_source_window_failure_does_not_block_next_source(
        tmp_path, monkeypatch):
    """spec ③ テスト 16: 先順位 source が導出不能でも、後順位 source の
    健全なキャッシュに進む。窓計算は source 単位の try の中に置くこと —
    try の外に置くと先順位 source の失敗が後順位のキャッシュ試行を殺す。"""
    conn, p = _provider(tmp_path, mt5=True)  # mt5 有効 → チェーン先頭
    # mt5 の能力を落とし、5m を提供も導出もできないようにする
    monkeypatch.setitem(sources.NATIVE_INTERVALS, "mt5", frozenset())
    fresh = _fresh_bars(interval="5m", n=30)
    ohlcv.upsert_cache_bars(conn, fresh, source="yfinance")

    result = p._cached_bars("USDJPY", "5m", NOW, [], 5)

    assert result is not None
    cached, origin = result
    assert origin == "cache"


def test_cache_window_is_widened_by_derive_ratio(tmp_path, monkeypatch):
    """spec ③ テスト 4 の裏面 (配線の検査): 導出が要る interval では窓が
    ライブ経路と同じ ratio 倍に広がる。cache_window.live_window_days を
    呼ばず lookback_days をそのまま使う退行は、単体 (test_cache_window)
    が緑のまま素通りするのでここで殺す。"""
    conn, p = _provider(tmp_path)
    captured = {}
    orig = ohlcv.load_cache_bars

    def _spy(conn_, symbol, interval, *, source, since=None, until=None):
        captured.setdefault(interval, since)
        return orig(conn_, symbol, interval, source=source, since=since,
                    until=until)

    monkeypatch.setattr(ohlcv, "load_cache_bars", _spy)
    p._cached_bars("USDJPY", "4h", NOW, [], 5)   # 4h は DERIVE_ONLY → base 1h, ratio 4
    span = NOW - captured["1h"]
    assert timedelta(days=20) <= span < timedelta(days=20, hours=4)
```

- [ ] **Step 4: テストを実行し、窓計算未適用のため落ちることを確認する**

```bash
find . -name __pycache__ -type d -not -path "./.venv/*" -exec rm -rf {} +
uv run pytest tests/datafeed/test_price_provider.py \
  -k "window_independent or rows_older_than_window or direct_read_candidate_since \
or derive_candidate_since or query_truncation or read_volume_bounded or \
prior_source_window_failure" -v
```
Expected: **2 件 FAIL** (`test_derive_candidate_since_is_floored_to_requested_interval` / `test_query_truncation_does_not_leave_partial_leading_bucket`)。残る 5 件は Task 9 の素朴な `since` でも成立するため PASS のまま (本 task では退行ガードとして機能する)。全件 FAIL にはならない。訂正 2 の追加テスト (`test_cache_window_is_widened_by_derive_ratio`) を含めると red は 3 件。(2026-08-14 着手前検証の probe 実測)

- [ ] **Step 5: `_cached_bars` を書き換える**

```python
    def _cached_bars(self, pair: str, interval: str, now: datetime,
                     errors: list[str], lookback_days: int
                     ) -> tuple[list[Bar], str] | None:
        """キャッシュから interval の足を作る。健全性検証を通らなければ None。

        窓 (spec ③ §3.1): source ごとに `cache_window.live_window_days` で
        取得日数を求め、`window_start = now - N日` を直読候補 (floor しない)、
        `floor_to_interval(window_start, interval)` を導出候補 (要求
        interval の境界へ floor) に使う。窓計算はキャッシュの中身を見ない
        (DB 非依存) ので「キャッシュ側の base に依存しない」性質が構造的に
        保たれる。

        **窓計算は source 単位の try の中に置く** — `finest_native_base` は
        提供も導出もできない source に対して DataUnhealthy を送出する。
        ここを try の外に置くと、先順位 source が導出不能というだけで
        後順位 source の健全なキャッシュへ進めなくなる (spec ③ テスト 16)。
        """
        d = self.settings.datafeed
        candidates = [i for i in [interval, *self._base_candidates(interval)]
                      if i not in DERIVE_ONLY_INTERVALS]
        live_sources = [name for name, _ in
                        self._chain(pair, kind="bars", interval=interval)]
        for name in live_sources:
            storage_name = _storage_source(name)
            try:
                window_days = cache_window.live_window_days(
                    name, interval, lookback_days)
                window_start = now - timedelta(days=window_days)
                derive_since = cache_window.floor_to_interval(
                    window_start, interval)
            except Exception as e:  # noqa: BLE001 — 次 source へ (テスト16)
                errors.append(f"{name}: {_safe_error_text(e)}")
                continue
            for src in candidates:
                since = window_start if src == interval else derive_since
                cached = ohlcv.load_cache_bars(self.conn, pair, src,
                                               source=storage_name,
                                               since=since)
                if not cached:
                    continue
                label = ("cache" if src == interval else f"cache({src})")
                label = f"{label}[{storage_name}]"
                try:
                    # キャッシュも健全性検証を通さない限り使わない (fail closed)
                    validate_bars(cached, now, d.freshness_max_min,
                                  sources.INTERVAL_MIN[src])
                    if src == interval:
                        return cached, "cache"
                    derived = self._resample(cached, pair, interval)
                    validate_bars(derived, now, d.freshness_max_min,
                                  sources.INTERVAL_MIN[interval])
                except Exception as e:  # noqa: BLE001
                    errors.append(f"{label}: {_safe_error_text(e)}")
                    continue
                return derived, f"cache({src}→{interval} derived)"
        return None
```

- [ ] **Step 6: テストを実行し green を確認する**

```bash
find . -name __pycache__ -type d -not -path "./.venv/*" -exec rm -rf {} +
uv run pytest tests/datafeed/test_price_provider.py -v
```
Expected: 全件 PASS (Task 8/9 分 + 本 task 8 件 + 既存回帰分)

- [ ] **Step 7: 全体テストを実行する**

```bash
uv run pytest -q
```
Expected: 全件 PASS

- [ ] **Step 8: 変異を当てて対応するテストが落ちることを確認する**

`find . -name __pycache__ -type d -not -path "./.venv/*" -exec rm -rf {} +` を前後で実行する。

| # | 変異 | 対象行 | 殺すテスト |
|---|---|---|---|
| M10-1 | `since = window_start if src == interval else derive_since` を `since = derive_since` (常に floor) に変える | `_cached_bars` の候補ループ | `test_direct_read_candidate_since_is_not_floored` |
| M10-2 | 同上を `since = window_start` (常に未 floor) に変える | 同上 | `test_derive_candidate_since_is_floored_to_requested_interval` と `test_query_truncation_does_not_leave_partial_leading_bucket` の両方 |
| M10-3 | 窓計算を candidates ループの内側へ移し、比を**キャッシュ base から**取る (`window_days = int(lookback_days * sources.INTERVAL_MIN[interval] / sources.INTERVAL_MIN[src])`) | `_cached_bars` | `test_window_independent_of_cache_base_used_for_derivation` (1m 経路で 300 日になり窓上限 5日+1h を超える) |
| M10-4 | `window_start`/`derive_since` の計算を **`for name` ループ内・`try` の外** へ出す (計算だけを try の直前に置く) | 同上 | `test_prior_source_window_failure_does_not_block_next_source` |
| M10-5 | `since=since` を `load_cache_bars` 呼び出しから削除する (窓の絞りを外す) | `_cached_bars` の `ohlcv.load_cache_bars(...)` 呼び出し | `test_rows_older_than_window_are_excluded` と `test_read_volume_bounded_by_window_regardless_of_accumulation` の両方 |
| M10-6 | `window_days = cache_window.live_window_days(name, interval, lookback_days)` を `window_days = lookback_days` に変える (Task 8 の窓計算を呼ばない退行) | `_cached_bars` | `test_cache_window_is_widened_by_derive_ratio` |

**M10-1〜M10-6 はいずれも「1 つの検査目的につき 1 テスト」の原則に沿って、それぞれ固有のテストが検出する。**

- [ ] **Step 9: 全体テストを再実行し green に戻っていることを確認する**

```bash
find . -name __pycache__ -type d -not -path "./.venv/*" -exec rm -rf {} +
uv run pytest -q
```
Expected: 全件 PASS

- [ ] **Step 10: コミット**

```bash
git add src/agentic_fx/datafeed/price_provider.py tests/datafeed/test_price_provider.py
git commit -m "$(cat <<'EOF'
feat: _cached_bars に窓計算 + floor を適用する (プラン9 Task10)

source 単位で cache_window.live_window_days/floor_to_interval を計算し、
直読候補は未floorのwindow_start、導出候補は要求intervalへfloorした
derive_sinceを使う。窓計算は source単位のtry内に置き、先順位source
の導出不能が後順位のキャッシュ試行を道連れにしないことを保証する。

price_provider.py の _base_candidates/_finest_native_base/
DERIVE_ONLY_INTERVALS は cache_window への委譲に統一 (Task 8 が
残した一時的重複を解消)。
EOF
)"
```

> 注記 (2026-08-14 着手前検証): 上記訂正 1〜4 は probe 実測済み (red 2+1 件 / M10-3 旧文面は survive / M10-6 は追加テストが無いと全 survive)。probe は scratchpad の test_task10_probe.py。

---


### Task 11: 本番連鎖 E2E pin + 実データ実測

**対応する spec ③ 検査点**: 10 (末尾の形成中バケットを残す)、11 (`build_app` から tick の書き込みを経た最終 `1m → 1h` フォールバック)、12 (tick の processed-bar marking が 1m を保存する配線)、14 (窓外の古い異常行を受理集合から外す契約変更)、17 (`ohlcv_history` をキャッシュ経路が拾わない負例)。検査点 1〜9・13・15・16 は Task 8/16/9/10 が独立に検査済みなので、本 task では重複させない。

**Files:**
- Create: `tests/datafeed/test_cache_fallback_e2e.py`
- Create: `scripts/measure_cache_validation_window.py`
- Create: `docs/superpowers/measurements/2026-08-11-cache-validation-real-data.md`
- Test: `tests/datafeed/test_cache_fallback_e2e.py`
- Modify: `src/agentic_fx/service.py`
- Modify: `src/agentic_fx/tools/mission_registry.py`

**Interfaces:**
- Consumes: Task 10 完了後の `PriceProvider.get_bars(self, pair: str, interval: str, lookback_days: int = 5) -> list[Bar]`、`PriceProvider.bars_origin(self, pair: str, interval: str) -> str | None`、`PriceProvider._cached_bars(self, pair: str, interval: str, now: datetime, errors: list[str], lookback_days: int) -> tuple[list[Bar], str] | None`
- Consumes: Task 16 の `ohlcv.upsert_cache_bars(conn, bars: list[Bar], *, source: str) -> int`、`ohlcv.load_cache_bars(conn, symbol: str, interval: str, *, source: str, since: datetime | None = None, until: datetime | None = None) -> list[Bar]`、`ohlcv.load_history_bars(conn, symbol: str, interval: str, *, source: str, since: datetime | None = None, until: datetime | None = None) -> list[Bar]`
- Consumes: `build_app(root: Path, *, runner: AgentRunner | None = None, clock: Clock | None = None, quote_fn=None, spec_fn=None, bars_fn=None, embedding_fn=None, provider: PriceProvider | None = None, stop_event: threading.Event | None = None) -> App` と `App.close(*, busy_resources: frozenset[str] = frozenset()) -> list[str]`
- Produces: 本番 factory の配線を固定する `tests/datafeed/test_cache_fallback_e2e.py`、実データ計測 CLI `scripts/measure_cache_validation_window.py --db PATH --symbol SYMBOL --history-source SOURCE --window-source LIVE_SOURCE --interval INTERVAL --lookback-days N --output PATH`、実測記録 `docs/superpowers/measurements/2026-08-11-cache-validation-real-data.md`

- [ ] **Step 1: 失敗するテストを書く**

`tests/datafeed/test_cache_fallback_e2e.py` を次の内容で新規作成する。`_init_root` は実 `run_init` が作る設定・policy・DB を使い、`_build_real_app` は実 `build_app` factory に `FakeRunner` と `FakeEmbedding` だけを注入する。order は一件も seed しない。`OPEN_NOW` は水曜 12:00 UTC で、`run_init` が作成した最新 account snapshot と同時刻以上になる。

```python
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from agentic_fx.core.contracts import Bar, FixedClock
from agentic_fx.runners.fake_runner import FakeRunner
from agentic_fx.service import build_app, run_init
from agentic_fx.store import ohlcv
from tests.store.test_rag import FakeEmbedding


OPEN_NOW = datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc)
PAIR = "USDJPY"
LIVE_SOURCE = "yfinance"


def _one_minute_bars(now: datetime = OPEN_NOW, n: int = 181) -> list[Bar]:
    start = now - timedelta(minutes=n - 1)
    return [
        Bar(PAIR, "1m", start + timedelta(minutes=i),
            148.0 + i * 0.0001, 148.1 + i * 0.0001,
            147.9 + i * 0.0001, 148.05 + i * 0.0001, 100.0)
        for i in range(n)
    ]


def _init_root(root) -> None:
    (root / "config").mkdir()
    example = open("config/settings.yaml.example", encoding="utf-8").read()
    (root / "config" / "settings.yaml.example").write_text(
        example, encoding="utf-8")
    with patch("agentic_fx.service.PriceProvider") as provider_cls, \
         patch("agentic_fx.service._check_llama_swap"):
        provider_cls.return_value.healthcheck.return_value = LIVE_SOURCE
        run_init(root)


def _build_real_app(root):
    _init_root(root)
    return build_app(
        root,
        runner=FakeRunner([]),
        clock=FixedClock(OPEN_NOW),
        embedding_fn=FakeEmbedding(),
    )


def _only_one_minute(pair: str, interval: str,
                     lookback_days: int) -> list[Bar]:
    assert pair == PAIR
    if interval != "1m":
        raise OSError(f"fixture intentionally has no live {interval}")
    # 毎回新しい list を返す。呼び出し回数に依存して枯れない。
    return list(_one_minute_bars())


def _all_live_fetchers_down(pair: str, interval: str,
                            lookback_days: int) -> list[Bar]:
    raise OSError(f"all live fetchers down: {pair} {interval}")


def test_tick_processed_bar_marking_persists_one_minute_without_orders(tmp_path):
    """spec ③ テスト 12: 無注文でも settings.pairs の marking が 1m を書く。"""
    app = _build_real_app(tmp_path)
    try:
        assert app.conn_core.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0
        with patch("agentic_fx.datafeed.price_provider.sources.yf_bars",
                   side_effect=_only_one_minute):
            app.scheduler.tick(OPEN_NOW)
        rows = ohlcv.load_cache_bars(
            app.conn_core, PAIR, "1m", source=LIVE_SOURCE)
        assert rows
        assert app.conn_core.execute(
            "SELECT COUNT(*) FROM ohlcv_cache WHERE symbol=? AND interval='1m' "
            "AND source=?", (PAIR, LIVE_SOURCE)).fetchone()[0] == len(rows)
    finally:
        app.close()


def test_build_app_tick_then_final_one_minute_to_one_hour_fallback(tmp_path):
    """spec ③ テスト 11: factory→無注文 tick→live 全滅→最終導出を pin。"""
    app = _build_real_app(tmp_path)
    try:
        assert app.conn_core.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0
        with patch("agentic_fx.datafeed.price_provider.sources.yf_bars",
                   side_effect=_only_one_minute):
            app.scheduler.tick(OPEN_NOW)

        one_hour_count = app.conn_core.execute(
            "SELECT COUNT(*) FROM ohlcv_cache WHERE symbol=? AND interval='1h' "
            "AND source=?", (PAIR, LIVE_SOURCE)).fetchone()[0]
        assert one_hour_count == 0  # 前提条件: 同 source の 1h cache は空

        one_minute_count = app.conn_core.execute(
            "SELECT COUNT(*) FROM ohlcv_cache WHERE symbol=? AND interval='1m' "
            "AND source=?", (PAIR, LIVE_SOURCE)).fetchone()[0]
        if one_minute_count == 0:
            pytest.fail(
                "setup failure: tick did not persist yfinance 1m cache; "
                "test_tick_processed_bar_marking_persists_one_minute_without_orders "
                "owns this production inspection point")

        with patch("agentic_fx.datafeed.price_provider.sources.yf_bars",
                   side_effect=_all_live_fetchers_down):
            bars = app.provider.get_bars(PAIR, "1h")

        assert bars
        assert app.provider.bars_origin(PAIR, "1h") == "cache(1m→1h derived)"
    finally:
        app.close()


def test_cache_fallback_keeps_trailing_in_progress_bucket(tmp_path):
    """spec ③ テスト 10: 12:00〜12:47 の形成中 1h bucket を落とさない。"""
    app = _build_real_app(tmp_path)
    try:
        boundary = OPEN_NOW.replace(minute=0)
        bars = [
            Bar(PAIR, "1m", boundary + timedelta(minutes=i),
                148.0, 148.1, 147.9, 148.05, 100.0)
            for i in range(48)
        ]
        ohlcv.upsert_cache_bars(app.conn_core, bars, source=LIVE_SOURCE)
        result = app.provider._cached_bars(PAIR, "1h", boundary + timedelta(minutes=47),
                                           [], 0)
        assert result is not None
        derived, origin = result
        assert origin == "cache(1m→1h derived)"
        assert [bar.ts for bar in derived] == [boundary]
    finally:
        app.close()


def test_window_excludes_old_abnormal_bar_and_changes_acceptance(tmp_path):
    """spec ③ テスト 14: 窓外の古い spike は fallback 全体を落とさない。"""
    app = _build_real_app(tmp_path)
    try:
        old = Bar(PAIR, "1h", OPEN_NOW - timedelta(days=10),
                  100.0, 100.0, 100.0, 100.0, 100.0)
        old_spike = Bar(PAIR, "1h", OPEN_NOW - timedelta(days=10) + timedelta(hours=1),
                        150.0, 150.0, 150.0, 150.0, 100.0)
        recent = [
            Bar(PAIR, "1h", OPEN_NOW - timedelta(hours=i),
                148.0, 148.1, 147.9, 148.05, 100.0)
            for i in range(24, -1, -1)
        ]
        ohlcv.upsert_cache_bars(
            app.conn_core, [old, old_spike, *recent], source=LIVE_SOURCE)
        with patch("agentic_fx.datafeed.price_provider.sources.yf_bars",
                   side_effect=_all_live_fetchers_down):
            got = app.provider.get_bars(PAIR, "1h", lookback_days=5)
        assert got
        assert all(bar.ts >= OPEN_NOW - timedelta(days=5) for bar in got)
        assert app.provider.bars_origin(PAIR, "1h") == "cache"
    finally:
        app.close()


def test_cache_fallback_ignores_matching_history_rows(tmp_path):
    """改訂 5 テスト 17: 同一キーの history 行を cache loader は読まない。"""
    app = _build_real_app(tmp_path)
    try:
        one_minute = _one_minute_bars()
        ohlcv.upsert_cache_bars(app.conn_core, one_minute, source=LIVE_SOURCE)
        history_ts = one_minute[-1].ts.replace(minute=0)
        # API allowlist ではなく raw SQL で同 source まで一致させる。
        # テーブル名だけを history へ向ける変異がこの行を直読するようにするため。
        app.conn_core.execute(
            "INSERT INTO ohlcv_history "
            "(symbol,interval,bar_time,open,high,low,close,volume,spread,source) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (PAIR, "1h", history_ts.isoformat(), 999.0, 999.0, 999.0,
             999.0, 1.0, None, LIVE_SOURCE))
        app.conn_core.commit()
        assert app.conn_core.execute(
            "SELECT COUNT(*) FROM ohlcv_cache WHERE symbol=? AND interval='1h' "
            "AND source=?", (PAIR, LIVE_SOURCE)).fetchone()[0] == 0
        assert app.conn_core.execute(
            "SELECT COUNT(*) FROM ohlcv_history WHERE symbol=? AND interval='1h' "
            "AND source=?", (PAIR, LIVE_SOURCE)).fetchone()[0] == 1

        with patch("agentic_fx.datafeed.price_provider.sources.yf_bars",
                   side_effect=_all_live_fetchers_down):
            got = app.provider.get_bars(PAIR, "1h")
        assert got
        assert app.provider.bars_origin(PAIR, "1h") == "cache(1m→1h derived)"
        assert all(bar.close != 999.0 for bar in got)
    finally:
        app.close()
```

- [ ] **Step 2: 失敗を確認**

```bash
find . -name __pycache__ -type d -not -path "./.venv/*" -exec rm -rf {} +
uv run pytest tests/datafeed/test_cache_fallback_e2e.py -v
```

Expected: `test_tick_processed_bar_marking_persists_one_minute_without_orders` と `test_build_app_tick_then_final_one_minute_to_one_hour_fallback` は Task 16 適用前なら `AttributeError: module 'agentic_fx.store.ohlcv' has no attribute 'load_cache_bars'`、Task 16 適用後かつ Task 10 未適用なら `_cached_bars()` の `lookback_days` シグネチャまたは未絞り挙動の相違で FAIL。`test_cache_fallback_keeps_trailing_in_progress_bucket`、`test_window_excludes_old_abnormal_bar_and_changes_acceptance`、`test_cache_fallback_ignores_matching_history_rows` の少なくとも一件が、Task 10/16 の最終形が未適用なら期待した origin・行集合・テーブル境界のいずれかで FAIL する。

- [ ] **Step 3: E2E に必要な本番実装を確認し、テスト目的ごとの最小修正を実装**

Task 10 の `_cached_bars` 最終形と Task 16 の二テーブル API が正しく実装済みなら、検査点 10/11/12/14/17 のための production ロジック追加は行わない。失敗した検査点だけを次の所有箇所で直す。

```text
検査点10: src/agentic_fx/datafeed/price_provider.py の _resample 呼び出し後に末尾を drop しない
検査点11: src/agentic_fx/datafeed/price_provider.py の _base_candidates から 1m を除外しない
検査点12: src/agentic_fx/core/scheduler.py の _process_exits 末尾で settings.pairs 全件に bars_fn を呼ぶ
検査点14: src/agentic_fx/datafeed/price_provider.py から load_cache_bars(..., since=since) を呼ぶ
検査点17: src/agentic_fx/datafeed/price_provider.py は load_cache_bars だけを呼び、load_history_bars を呼ばない
```

`src/agentic_fx/core/risk_gate.py`、`src/agentic_fx/core/paper_broker.py`、`src/agentic_fx/core/transitions.py` は編集しない。scheduler の変更が必要になった場合も processed-bar marking の既存配線の復元だけに限定し、注文・SL/TP 判定ロジックは変更しない。

- [ ] **Step 4: E2E テストを green にする**

```bash
find . -name __pycache__ -type d -not -path "./.venv/*" -exec rm -rf {} +
uv run pytest tests/datafeed/test_cache_fallback_e2e.py -v
```

Expected: 5 passed。非同期 Mission supervisor は start しておらず観測対象外であり、全テストが `finally` で `app.close()` を完了する。

- [ ] **Step 5: 実データ計測スクリプトを書く**

`scripts/measure_cache_validation_window.py` を次の内容で作成する。履歴 DB の実バーをそのまま読み、全期間と `live_window_days` で切った期間の双方について `validate_bars` の合否・中央値時間・ピークメモリ・行数を測る。隣接 bar 間に 48 時間以上の差が一つも無ければ、週末ギャップを含む実データという前提を満たさないため終了コード 2 で拒否する。

```python
from __future__ import annotations

import argparse
import json
import statistics
import time
import tracemalloc
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agentic_fx.datafeed.cache_window import live_window_days
from agentic_fx.datafeed.health import validate_bars
from agentic_fx.datafeed.sources import INTERVAL_MIN
from agentic_fx.store import ohlcv
from agentic_fx.store.db import connect


def _measure(bars, now: datetime, freshness_max_min: int,
             interval_min: int, repeats: int) -> dict:
    elapsed = []
    peaks = []
    outcomes = []
    errors = []
    for _ in range(repeats):
        tracemalloc.start()
        started = time.perf_counter()
        try:
            validate_bars(bars, now, freshness_max_min, interval_min)
            outcomes.append("PASS")
            errors.append(None)
        except Exception as exc:  # 計測対象の合否を記録して次の反復へ進む
            outcomes.append("FAIL")
            errors.append(f"{type(exc).__name__}: {exc}")
        elapsed.append(time.perf_counter() - started)
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        peaks.append(peak)
    return {
        "rows": len(bars),
        "outcome": outcomes[0],
        "error": errors[0],
        "median_seconds": statistics.median(elapsed),
        "peak_bytes_max": max(peaks),
        "repeats": repeats,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--history-source", required=True)
    parser.add_argument("--window-source", required=True,
                        choices=("yfinance", "twelvedata", "mt5"))
    parser.add_argument("--interval", default="1m")
    parser.add_argument("--lookback-days", type=int, default=5)
    parser.add_argument("--freshness-max-min", type=int, default=20)
    parser.add_argument("--repeats", type=int, default=7)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    conn = connect(args.db)
    try:
        all_bars = ohlcv.load_history_bars(
            conn, args.symbol, args.interval, source=args.history_source)
    finally:
        conn.close()
    if len(all_bars) < 2:
        raise SystemExit("実測不能: ohlcv_history の実バーが 2 行未満")

    weekend_gaps = sum(
        1 for left, right in zip(all_bars, all_bars[1:])
        if right.ts - left.ts >= timedelta(hours=48))
    if weekend_gaps == 0:
        raise SystemExit("実測不能: 48時間以上の週末ギャップを含まない")

    now = all_bars[-1].ts + timedelta(minutes=INTERVAL_MIN[args.interval])
    # history の producer 名 (dukascopy/mt5) と、本番要求窓を決める live source
    # (yfinance/twelvedata/mt5) は別契約なので混同しない。
    days = live_window_days(args.window_source, args.interval,
                            args.lookback_days)
    since = now - timedelta(days=days)
    windowed = [bar for bar in all_bars if bar.ts >= since]
    result = {
        "db": str(args.db),
        "symbol": args.symbol,
        "history_source": args.history_source,
        "window_source": args.window_source,
        "interval": args.interval,
        "first_ts": all_bars[0].ts.isoformat(),
        "last_ts": all_bars[-1].ts.isoformat(),
        "weekend_gaps_ge_48h": weekend_gaps,
        "window_days": days,
        "before": _measure(all_bars, now, args.freshness_max_min,
                           INTERVAL_MIN[args.interval], args.repeats),
        "after": _measure(windowed, now, args.freshness_max_min,
                          INTERVAL_MIN[args.interval], args.repeats),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n",
                           encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 6: 実データで窓縮小の前後を測る**

実装 worktree の `data/agentic.db` に週末を跨ぐ履歴が無い場合は、運用で保持している Dukascopy/MT5 履歴 DB のコピーを `--db` に指定する。実データを repo に commit しない。

```bash
uv run python scripts/measure_cache_validation_window.py \
  --db data/agentic.db \
  --symbol USDJPY \
  --history-source dukascopy \
  --window-source yfinance \
  --interval 1m \
  --lookback-days 5 \
  --repeats 7 \
  --output /tmp/cache-validation-real-data.json
```

Expected: exit 0。JSON の `weekend_gaps_ge_48h` が 1 以上で、`before` と `after` の `outcome`、`error`、`rows`、`median_seconds`、`peak_bytes_max` が埋まる。`after.rows < before.rows` かつ `after.median_seconds < before.median_seconds` を実測値で確認する。合否が変わらない実データでも値を改変せず記録し、古い異常行を含む実データで `before=FAIL` / `after=PASS` になった場合もそのまま記録する。§1.4 の 180 日連続合成値 5.76 秒 / 161.9 MB を転記して実測値の代用にしない。

- [ ] **Step 7: 実測結果を記録する**

`docs/superpowers/measurements/2026-08-11-cache-validation-real-data.md` を作成し、`/tmp/cache-validation-real-data.json` の値を全て具体値へ転記する。次の見出しと表を使い、角括弧の説明語は実測値で置換してから commit する。

```markdown
# cache validation window 実データ実測 (2026-08-11)

- DB: 実測に使った DB の識別子 (秘密情報を含まない basename)
- symbol/source/interval: 実際の値
- 期間: first_ts から last_ts
- 48 時間以上の週末ギャップ数: 実際の整数
- 反復数: 7
- 実行コマンド: 秘密情報を除いた Step 6 のコマンド

| 条件 | 行数 | validate_bars 合否 | 失敗理由 | 中央値秒 | peak bytes |
|---|---:|---|---|---:|---:|
| 窓縮小前 (全期間) | 実測行数 | PASS または FAIL | 無ければ `なし` | 実測秒 | 実測 bytes |
| 窓縮小後 (要求窓) | 実測行数 | PASS または FAIL | 無ければ `なし` | 実測秒 | 実測 bytes |

## 結論

窓外のデータを検査対象から外したことで生じた合否の変化、または合否が同じだった事実を記す。
行数・所要時間・peak memory の差を具体値で記し、連続合成バーの旧値 5.76 秒 / 161.9 MB とは分ける。
```

- [ ] **Step 8: コメントへ二段構えの機構を書く**

`src/agentic_fx/service.py` の healthcheck provider 周辺コメントと `src/agentic_fx/tools/mission_registry.py` の readonly provider コメントを、それぞれ次の機構が明記される文章へ更新する。

```python
# scheduler tick の processed-bar marking が書き込み可能 provider 経由で
# 1m cache を継続的に温める。1h は live 1h が検証を通れば直接保存され、
# 通らない場合は保存済み 1m から cache(1m→1h derived) として復元される。
# readonly Mission provider はこの二段構えの書き手ではない。
```

- [ ] **Step 9: Task 11 の対象テストと全体テストを実行する**

```bash
find . -name __pycache__ -type d -not -path "./.venv/*" -exec rm -rf {} +
uv run pytest tests/datafeed/test_cache_fallback_e2e.py \
  tests/datafeed/test_cache_window.py \
  tests/datafeed/test_price_provider.py -v
uv run pytest -q
```

Expected: 対象テスト全件 PASS、全体テスト全件 PASS、開始時点の 1726 passed / 1 deselected から意図した新規テスト件数以外の減少なし。

- [ ] **Step 10: 短絡補償を確認する**

テスト 11 が `setup failure: tick did not persist yfinance 1m cache` で abort した場合に未実行となる検査点を、次の独立テストが殺すことを単独実行で確認する。

| テスト 11 で短絡される後段 | 独立に殺すテスト | 実行コマンド |
|---|---|---|
| processed-bar marking が 1m を保存する | `test_tick_processed_bar_marking_persists_one_minute_without_orders` | `uv run pytest tests/datafeed/test_cache_fallback_e2e.py::test_tick_processed_bar_marking_persists_one_minute_without_orders -v` |
| `_base_candidates` が 1m を候補に含め、1m から 1h を導出する | `test_window_independent_of_cache_base_used_for_derivation` | `uv run pytest tests/datafeed/test_price_provider.py::test_window_independent_of_cache_base_used_for_derivation -v` |
| 最終導出の resample がクエリ切断由来の部分先頭を作らない | `test_query_truncation_does_not_leave_partial_leading_bucket` | `uv run pytest tests/datafeed/test_price_provider.py::test_query_truncation_does_not_leave_partial_leading_bucket -v` |
| 最終導出が形成中の末尾 bucket を残す | `test_cache_fallback_keeps_trailing_in_progress_bucket` | `uv run pytest tests/datafeed/test_cache_fallback_e2e.py::test_cache_fallback_keeps_trailing_in_progress_bucket -v` |
| キャッシュ loader が履歴テーブルを読まない | `test_cache_fallback_ignores_matching_history_rows` | `uv run pytest tests/datafeed/test_cache_fallback_e2e.py::test_cache_fallback_ignores_matching_history_rows -v` |

- [ ] **Step 11: 変異テスト**

各変異を一つずつ当て、該当行を `grep -n` または `sed -n` で目視し、前後で `__pycache__` を削除する。各行は一つの固有テスト名を主 killer とする。

| # | 変異 | red になる固有テスト名 |
|---|---|---|
| M11-1 | `_resample` 後に `derived = derived[:-1]` を入れて形成中末尾を落とす | `test_cache_fallback_keeps_trailing_in_progress_bucket` |
| M11-2 | `_base_candidates("1h")` の結果から `1m` を除外する | `test_build_app_tick_then_final_one_minute_to_one_hour_fallback` |
| M11-3 | `build_app` の Scheduler 配線を `bars_fn=lambda pair: None` に差し替える | `test_tick_processed_bar_marking_persists_one_minute_without_orders` |
| M11-4 | `_process_exits` 末尾の `for pair in self.settings.pairs` 走査を削除する | `test_tick_processed_bar_marking_persists_one_minute_without_orders` |
| M11-5 | `load_cache_bars(..., since=since)` から `since` を削除して全期間を検証へ戻す | `test_window_excludes_old_abnormal_bar_and_changes_acceptance` |
| M11-6 | `_cached_bars` の読み口 `ohlcv.load_cache_bars` を同じ引数で `ohlcv_history` を読む実装へ向ける | `test_cache_fallback_ignores_matching_history_rows` |
| M11-7 | E2E の `build_app(...)` をローカルな `PriceProvider` + `Scheduler` 組立てへ置換する変異はテスト変異なので許容しない。production の `build_app` から `bars_fn=provider.latest_1m_bar` の束縛を削除する | `test_tick_processed_bar_marking_persists_one_minute_without_orders` |

変異を全て戻した後、次を実行する。

```bash
find . -name __pycache__ -type d -not -path "./.venv/*" -exec rm -rf {} +
uv run pytest tests/datafeed/test_cache_fallback_e2e.py -v
uv run pytest -q
```

Expected: 5 passed、全体テスト全件 PASS。

- [ ] **Step 12: コミット**

```bash
git add tests/datafeed/test_cache_fallback_e2e.py \
  scripts/measure_cache_validation_window.py \
  docs/superpowers/measurements/2026-08-11-cache-validation-real-data.md \
  src/agentic_fx/service.py \
  src/agentic_fx/tools/mission_registry.py
git commit -m "test: 本番 OHLCV 最終フォールバックを E2E で pin する (プラン9 Task11)"
```

---

### 束 C 自己レビュー

#### spec ③ 検査点 17 個の対応

| # | Task / Step | 独立テスト名 |
|---:|---|---|
| 1 | Task 9 Step 1、Task 10 Step 3 | `test_cached_bars_since_is_not_none_and_varies_with_lookback_days` |
| 2 | Task 8 Step 1 | `test_native_interval_window_equals_lookback_days` |
| 3 | Task 8 Step 1 | `test_derive_interval_window_multiplies_by_ratio` |
| 4 | Task 10 Step 3 | `test_window_independent_of_cache_base_used_for_derivation` |
| 4 の裏面 (配線の検査) | Task 10 Step 3 | `test_cache_window_is_widened_by_derive_ratio` — 導出 interval で窓が ratio 倍に広がることを `_cached_bars` 経由で検査 (Task 10 訂正 2 で追加) |
| 5 | Task 10 Step 3 | `test_rows_older_than_window_are_excluded` |
| 6 | Task 10 Step 3 | `test_direct_read_candidate_since_is_not_floored` |
| 7 | Task 10 Step 3 | `test_derive_candidate_since_is_floored_to_requested_interval` |
| 8 | Task 10 Step 3 | `test_query_truncation_does_not_leave_partial_leading_bucket` |
| 9 | Task 8 Step 1 | `test_floor_to_interval_1d_floors_to_utc_midnight`、`test_floor_to_interval_1d_across_month_boundary` |
| 10 | Task 11 Step 1 | `test_cache_fallback_keeps_trailing_in_progress_bucket` |
| 11 | Task 11 Step 1 | `test_build_app_tick_then_final_one_minute_to_one_hour_fallback` |
| 12 | Task 11 Step 1 | `test_tick_processed_bar_marking_persists_one_minute_without_orders` |
| 13 | Task 10 Step 3 | `test_read_volume_bounded_by_window_regardless_of_accumulation` |
| 14 | Task 11 Step 1 | `test_window_excludes_old_abnormal_bar_and_changes_acceptance` |
| 15 | Task 9 Step 1 | `test_cached_bars_since_is_not_none_and_varies_with_lookback_days` |
| 16 | Task 10 Step 3 | `test_prior_source_window_failure_does_not_block_next_source` |
| 17 | Task 11 Step 1 | `test_cache_fallback_ignores_matching_history_rows` |

#### spec ③ 受入条件 7 項目の対応

| 受入条件 | Task / Step | テスト名または成果物 |
|---|---|---|
| 1. キャッシュ窓がライブ経路と一致 | Task 8 Step 1、Task 10 Step 3 | `test_native_interval_window_equals_lookback_days`、`test_derive_interval_window_multiplies_by_ratio`、`test_rows_older_than_window_are_excluded` |
| 2. 窓がキャッシュ base に依存しない | Task 10 Step 3 | `test_window_independent_of_cache_base_used_for_derivation` |
| 2.5 呼び出しごとの `lookback_days` が loader の `since` まで届く | Task 9 Step 1 | `test_cached_bars_since_is_not_none_and_varies_with_lookback_days` |
| 2.6 先順位 source の窓計算失敗を source 単位で隔離 | Task 10 Step 3 | `test_prior_source_window_failure_does_not_block_next_source` |
| 3. クエリ切断由来の部分先頭 bucket を防ぐ | Task 8 Step 1、Task 10 Step 3 | `test_floor_to_interval_1h_floors_down_within_hour`、`test_query_truncation_does_not_leave_partial_leading_bucket` |
| 4. 蓄積量に依存せず読み込み量・所要を窓内へ制限 | Task 10 Step 3、Task 11 Steps 5〜7 | `test_read_volume_bounded_by_window_regardless_of_accumulation`、`2026-08-11-cache-validation-real-data.md` |
| 5. `build_app` から tick 書込を経た最終 `1m → 1h` pin | Task 11 Steps 1・4・10 | `test_build_app_tick_then_final_one_minute_to_one_hour_fallback`、`test_tick_processed_bar_marking_persists_one_minute_without_orders` |
| 6. §6 の契約変更を実データで実測・記録 | Task 11 Steps 5〜7 | `test_window_excludes_old_abnormal_bar_and_changes_acceptance`、`scripts/measure_cache_validation_window.py`、`docs/superpowers/measurements/2026-08-11-cache-validation-real-data.md` |
| 7. 既存テストが壊れない | Task 8 Step 5、Task 16 Steps 35・45・47、Task 9 Steps 5・7、Task 10 Steps 7・9、Task 11 Steps 9・11 | `uv run pytest -q` 全件 |

#### 設計書 D2 の変異リスト対応

| D2 変異 | Task / Step | red になるテスト名 |
|---|---|---|
| prune 対象を `ohlcv_cache` から `ohlcv_history` へ差し替える | Task 16 Steps 1・46 | `test_prune_cache_never_touches_ohlcv_history` |
| cache writer が履歴 source `dukascopy` を受理する | Task 16 Steps 1・46 | `test_upsert_cache_bars_rejects_non_live_source` |
| history importer が live source を受理する | Task 16 Steps 1・46 | `test_import_history_bars_rejects_live_source` |
| migration が未知 source を cache 側へ振り分ける | Task 16 Steps 7・46 | `test_migrate_ohlcv_split_routes_unknown_source_to_history` |
| 保持期間の設定検証を削除する | Task 16 Steps 31・46 | `test_build_app_rejects_cache_retention_below_interval_requirement` |
| prune のバッチ `LIMIT` を外す | Task 16 Steps 1・46 | `test_prune_cache_respects_limit` |
| maintenance の呼び出しを削除する | Task 16 Steps 36・46 | `test_tick_calls_on_cache_maintenance_every_tick_when_configured` |
| 人間 CLI が live source を受理する | Task 16 Steps 21・46 | `test_backtest_run_rejects_live_source`、`test_history_coverage_rejects_live_source`、`test_analyze_corr_rejects_live_source` |

#### `ohlcv` 直接 SQL の全箇所と付け替え先

次の 7 ファイルを `rg -n -i '(FROM|INTO|JOIN|UPDATE|DELETE FROM)[[:space:]]+ohlcv\\b|\\bohlcv\\b'` で確認した。コメント・docstring だけの言及も区別した。

| ファイル / 現行箇所 | 現行 SQL または言及 | Task / Step | 付け替え先 |
|---|---|---|---|
| `src/agentic_fx/backtest/replay.py:85` | `SELECT * FROM ohlcv ... interval='1m'` | Task 16 Step 17 | `ohlcv_history` |
| `src/agentic_fx/backtest/timeframes.py:130` | `SELECT ... FROM ohlcv` | Task 16 Step 18 | `ohlcv_history`。同ファイル :1/:4/:74/:102 の docstring/comment も `ohlcv_history` と記述する |
| `src/agentic_fx/backtest/holdout.py:88` | `SELECT MIN(bar_time) FROM ohlcv` | Task 16 Step 19 | `ohlcv_history`。:96 の comment も `ohlcv_history` と記述する |
| `src/agentic_fx/backtest/mt5_import.py:174` | `FROM ohlcv ta JOIN ohlcv tb` | Task 16 Step 16 | 両 alias とも `ohlcv_history`。:3 の bridge API path `/ohlcv/` は HTTP endpoint 名なので変更しない |
| `src/agentic_fx/backtest/cli.py:247` | `SELECT COUNT(*) FROM ohlcv` | Task 16 Step 20 | `ohlcv_history` |
| `src/agentic_fx/backtest/analysis.py:69/:99/:317` | SQL は無く docstring/comment の `ohlcv` 言及のみ | 付け替え対象なし | 実データ読み口は Task 16 Step 18 で `ohlcv_history` へ付け替える `timeframes.load_resampled_frame` 経由 |
| `src/agentic_fx/store/ohlcv.py:71/:96/:125` | live upsert/load/spread SQL | Task 16 Step 9 | upsert/load は `ohlcv_cache`。cache に spread は無いため live spread 読み口は廃止 |
| `src/agentic_fx/store/ohlcv.py:230/:241` | history import と conflict 検査 SQL | Task 16 Step 9 | `ohlcv_history` |

#### プレースホルダ確認

追記後に次を実行し、終了コード 1 (該当なし) を確認する。禁止語を説明するために本文へその語自体を書くと scan が自己検出するため、コマンドは文字列を分割して検索する。

```bash
rg -n "TB""D|適切""に|同様""に|以下""同様|\\(省略\\)|Task [0-9]+ と同じ" \
  .superpowers/sdd/2026-08-11-phase2-9-foundation/plan-bundle-C.md
```

Task 16 Step 13 の見出しに禁止された曖昧語が既存 2836 行内に一件ある。既存部分を書き換えない厳守事項のため、本追記では修正不能であり、下記「カバーできなかった項目」に明記する。本追記範囲 (追記開始行以降) だけを `tail -n +2837` で同じ scan に掛けた結果は該当なしであることを確認する。

#### `store/db.py` を触る Task 13 / 15 / 17 / 19 との競合ポイント

| 競合 task | 同じファイルの変更点 | Task 16 との統合規則 |
|---|---|---|
| Task 13 | `signals.claimed_by_mission_id` FK migration と壊れた claim の修復 | `_migrate_ohlcv_split` を Task 13 migration の前後どちらへ置いても各 migration が独立冪等になるようにする。`init_db` の migration chain 編集を行単位で再適用し、Task 13 の FK repair と `foreign_key_check` を落とさない |
| Task 15 | `reflection_attempts` DDL と migration | `_create_latest_schema` の table 集合と `init_db` chain が衝突する。15 table という Task 16 時点の件数 assert は最終統合時に Task 15 の新 table を加えた期待値へ更新し、`reflection_attempts` の DDL を消さない |
| Task 17 | `trade_intents` 列 migration と `alert_state` DDL | `trade_intents` の `action` / `reject_category` migration、`alert_state` 作成、`ohlcv` 分割 migration をそれぞれ独立関数のまま一度ずつ chain に残す。Task 17 後は table 件数テストを最終 schema に合わせる |
| Task 19 | `improvement_runs` の PR 列 migration | Task 19 の `improvement_runs` rebuild/ALTER と Task 16 の旧 `ohlcv` rename/drop が同じ transaction helper・backup suffix に触れる。backup suffix を migration ごとに一意にし、片方の「既に終端 schema」判定が他方を skip させない |

#### カバーできなかった項目

| 項目 | 状態と理由 |
|---|---|
| 既存 2836 行全体の禁止語ゼロ | カバー不能。Task 16 Step 13 の見出しに既存の曖昧語が一件あるが、「既存部分を書き換えない」という今回の指示が優先する。本追記には使用しない |
| 実データの具体的な測定値 | plan 執筆時点では Task 11 未実行であり、週末を含む運用履歴 DB も入力されていない。Task 11 Steps 5〜7 に、実行時に値を測って記録し、合否が期待と違っても隠さない手順を設けた |


---

## プラン 9 束 D 実装計画: スキーマと承認 (Task 12 / 13 / 19)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> 本書は親プラン `docs/superpowers/plans/2026-08-11-phase2-9-foundation.md` の束 D の詳細 step である。**Global Constraints / プラン規約 / Interfaces / 着手前の既知事実は親プランのものをそのまま適用する** (ここでは転記しない — 転記すると必ず食い違う。プロジェクトの既定方針)。設計の正は `docs/superpowers/specs/2026-08-11-phase2-9-foundation-design.md` の **D4 (Task 12) / D5 (Task 13) / D7 (Task 19)**。

**Goal:** approval の最新決定優先 (Task 12)・signals の FK 整合性 (Task 13)・improvement_runs の死んだ列除去 (Task 19) の 3 件を、TDD・冪等 migration・変異耐性を満たして実装する。

**Architecture:** Task 12 は `tools/plugin_loader.py` の 1 関数のみを変更 (DDL に触れない)。Task 13 と Task 19 は `store/db.py` に「rebuild 用 DDL 定数 + migration 関数 + `init_db()` からの呼び出し」を追加する、既存の `_migrate_ohlcv_v2` と同型のパターンに従う。**Task 13 → Task 19 の順に `store/db.py` を直列編集する** (下記「束 D 内の実装順序」参照 — 両者が同じ関数 (`init_db`) の同じ挿入点と、同じ定数ブロックの近傍に触れるため、真の並列 worktree では機械的な merge 衝突を起こす)。

**Tech Stack:** Python 3.12 / uv / pytest / sqlite3 (`PRAGMA foreign_keys`, table rebuild)

---

### 束 D 内の実装順序 (厳守)

Task 12 は `store/db.py` に一切触れないため、Task 13/19 のどちらとも並列でよい。**Task 13 と Task 19 は同一ファイル `src/agentic_fx/store/db.py` の隣接領域 (定数ブロック直後・`init_db()` の末尾) に追記する**ため、以下の順序で直列に実装すること:

1. **Task 12** (独立、いつ実施してもよい)
2. **Task 13** (`_SIGNALS_V2_DDL` 定数 + `_migrate_signals_fk` + `init_db()` への配線)
3. **Task 19** (`_IMPROVEMENT_RUNS_V2_DDL` 定数 + `_migrate_improvement_runs_v2` + `init_db()` への配線) — **Task 13 が `store/db.py` に加えた変更が既に存在する前提で書かれている**

worktree を分けて並列実施する場合は、Task 19 の worktree を Task 13 マージ後に作り直すか、Task 19 実装者が Task 13 の diff を先に取り込んでから着手すること。

---

### Task 12: approval の最新決定優先

**設計の正**: 設計書 D4 (`2026-08-11-phase2-9-foundation-design.md:226-238`)、本体設計書 §7 (`2026-07-25-agentic-fx-design.md:563`)

**Files:**
- Modify: `src/agentic_fx/tools/plugin_loader.py:37-101`
- Test: `tests/tools/test_plugin_loader.py`

**Interfaces:**
- Consumes: `agentic_fx.store.approvals.create` / `agentic_fx.store.approvals.decide` / `agentic_fx.store.approvals.expire_due` (既存、変更なし)
- Produces: `plugin_loader.approved_plugins(conn, plugins_dir) -> list[PluginMeta]` (公開シグネチャ不変。内部の `_approved_hashes_by_name` の**意味論のみ**変更 — 「ever approved」→「最新決定が approved」)

#### 現状の欠陥 (再確認)

`src/agentic_fx/tools/plugin_loader.py:70-101` の `_approved_hashes_by_name` は `WHERE kind=? AND status='approved'` で承認済みハッシュ集合を作るため、**後から reject しても取り消せない**。

- [ ] **Step 1: 失敗するテストを書く (`tests/tools/test_plugin_loader.py`)**

まず import 行に `json` を追加する。現在の import ブロック (ファイル冒頭 15-32 行目) を以下に置き換える:

```python
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pandas as pd
import pytest

from agentic_fx.core.contracts import Bar
from agentic_fx.datafeed.sources import INTERVAL_MIN
from agentic_fx.plugin.loader import PluginMeta
from agentic_fx.plugin.sandbox import SandboxError
from agentic_fx.store import approvals
from agentic_fx.store.db import connect, init_db
from agentic_fx.tools import market_tools, plugin_loader
from agentic_fx.tools.registry import ToolRegistry
```

(既存の `import logging` 位置に `import json` を追加しただけで、他の import は変更しない。)

次に、ファイル末尾に以下のテスト群を追記する (既存の `_approve` ヘルパのすぐ後ろに置くとよい — `_approve` 自体は変更しない):

```python
# ===========================================================================
# Task 12 (プラン9 束D、設計書 D4): approval の最新決定優先
# ===========================================================================

def _decide(conn, name: str, content_hash: str, *, status: str,
           now: datetime) -> int:
    """`_approve` の一般化版 (status を選べる)。既存の `_approve` は
    approved 固定のヘルパとして残す (既存テストの呼び出しを変えない)。"""
    aid = approvals.create(conn, "plugin",
                           {"name": name, "content_hash": content_hash}, now)
    approvals.decide(conn, aid, status=status, decided_by="shell", now=now)
    return aid


def test_approved_plugins_reject_after_approve_revokes(tmp_path):
    """D4: 後から reject すれば承認は取り消される。これは現行バグ
    (status='approved' 集合方式は取り消しが効かない) の回帰ピン ——
    このテストが無いと退行しても検出できない。"""
    plugins_dir = tmp_path / "plugins"
    plugins_dir.mkdir()
    d = _write_plugin(plugins_dir, "flip_ind")
    conn = _conn(tmp_path)
    from agentic_fx.plugin.loader import content_hash as ch
    h = ch(d)
    _decide(conn, "flip_ind", h, status="approved", now=NOW)
    _decide(conn, "flip_ind", h, status="rejected",
           now=NOW + timedelta(minutes=1))

    metas = plugin_loader.approved_plugins(conn, plugins_dir)

    assert metas == []


def test_approved_plugins_approve_after_reject_readmits(tmp_path):
    """D4: 後から re-approve すれば再承認される。"""
    plugins_dir = tmp_path / "plugins"
    plugins_dir.mkdir()
    d = _write_plugin(plugins_dir, "flip_back_ind")
    conn = _conn(tmp_path)
    from agentic_fx.plugin.loader import content_hash as ch
    h = ch(d)
    _decide(conn, "flip_back_ind", h, status="rejected", now=NOW)
    _decide(conn, "flip_back_ind", h, status="approved",
           now=NOW + timedelta(minutes=1))

    metas = plugin_loader.approved_plugins(conn, plugins_dir)

    assert len(metas) == 1 and metas[0].name == "flip_back_ind"


def test_approved_plugins_expired_after_approve_does_not_revoke(tmp_path):
    """D4 が必須とする肯定検査: expired は決定として数えない。承認後に
    **別の** 承認要求 (同じ name/content_hash) が発行され、それが期限切れ
    で expired になっても、先の承認は取り消され *ない* こと。
    `decide()` は expired を書けず (approved/rejected のみ)、
    `expire_due()` は pending にしか触れないため、この経路だけが
    「本物の expired 行」を作れる (raw SQL に頼らない)。"""
    plugins_dir = tmp_path / "plugins"
    plugins_dir.mkdir()
    d = _write_plugin(plugins_dir, "expire_noop_ind")
    conn = _conn(tmp_path)
    from agentic_fx.plugin.loader import content_hash as ch
    h = ch(d)
    _decide(conn, "expire_noop_ind", h, status="approved", now=NOW)
    later = NOW + timedelta(minutes=1)
    approvals.create(conn, "plugin",
                     {"name": "expire_noop_ind", "content_hash": h}, later,
                     expires_at=later + timedelta(minutes=15))
    n = approvals.expire_due(conn, later + timedelta(minutes=16))
    assert n == 1  # 前提: 2 件目の要求が確かに expired になった

    metas = plugin_loader.approved_plugins(conn, plugins_dir)

    assert len(metas) == 1 and metas[0].name == "expire_noop_ind"


def test_approved_plugins_invalidated_after_approve_does_not_revoke(tmp_path):
    """D4: invalidated も決定として数えない。現行コードに kind=plugin へ
    invalidated を書く経路が無い (Phase 3 の live_trade 専用、設計書 §7)
    ため、DB を直接操作して再現する。"""
    plugins_dir = tmp_path / "plugins"
    plugins_dir.mkdir()
    d = _write_plugin(plugins_dir, "invalidated_noop_ind")
    conn = _conn(tmp_path)
    from agentic_fx.plugin.loader import content_hash as ch
    h = ch(d)
    _decide(conn, "invalidated_noop_ind", h, status="approved", now=NOW)
    later = NOW + timedelta(minutes=1)
    conn.execute(
        "INSERT INTO approval_requests (kind, payload_json, status, "
        "decided_by, decided_at, created_at) VALUES "
        "('plugin', ?, 'invalidated', 'system', ?, ?)",
        (json.dumps({"name": "invalidated_noop_ind", "content_hash": h}),
         later.isoformat(), later.isoformat()))
    conn.commit()

    metas = plugin_loader.approved_plugins(conn, plugins_dir)

    assert len(metas) == 1 and metas[0].name == "invalidated_noop_ind"


def test_approved_plugins_excludes_approved_row_with_null_decided_at(
        tmp_path):
    """`decided_at IS NOT NULL` の絞り込みのピン — status='approved' でも
    decided_at が NULL (DB 破損・移行漏れ等の想定外行) は決定として
    数えない。"""
    plugins_dir = tmp_path / "plugins"
    plugins_dir.mkdir()
    d = _write_plugin(plugins_dir, "null_decided_ind")
    conn = _conn(tmp_path)
    from agentic_fx.plugin.loader import content_hash as ch
    h = ch(d)
    conn.execute(
        "INSERT INTO approval_requests (kind, payload_json, status, "
        "created_at) VALUES ('plugin', ?, 'approved', ?)",
        (json.dumps({"name": "null_decided_ind", "content_hash": h}),
         NOW.isoformat()))
    conn.commit()

    metas = plugin_loader.approved_plugins(conn, plugins_dir)

    assert metas == []


def test_approved_plugins_orders_by_decided_at_not_insertion_order(
        tmp_path):
    """`ORDER BY decided_at, id` を落とす変異の killer。id (=挿入順) は
    reject → approve の順だが、decided_at は逆 (approve が先・reject が
    後) にする。decided_at 基準の実装なら最終決定は reject (除外)。もし
    ORDER BY が抜けて SQL の物理走査順 (= 挿入順 = id 昇順) にフォール
    バックすれば最終決定は approve (含まれる) ため、観測結果でどちらの
    ロジックかを判別できる。"""
    plugins_dir = tmp_path / "plugins"
    plugins_dir.mkdir()
    d = _write_plugin(plugins_dir, "chronology_ind")
    conn = _conn(tmp_path)
    from agentic_fx.plugin.loader import content_hash as ch
    h = ch(d)
    later = NOW + timedelta(hours=1)
    # id=1 (先に INSERT) が reject だが decided_at は「後」(later)。
    _decide(conn, "chronology_ind", h, status="rejected", now=later)
    # id=2 (後に INSERT) が approve だが decided_at は「先」(NOW)。
    _decide(conn, "chronology_ind", h, status="approved", now=NOW)

    metas = plugin_loader.approved_plugins(conn, plugins_dir)

    # decided_at 基準の正しい時系列は approve(NOW) → reject(later) なので
    # 最終決定は reject = 除外。
    assert metas == []


def test_approved_plugins_same_decided_at_ties_break_by_higher_id(tmp_path):
    """同時刻決定のタイブレークは id 最大、という契約のピン。

    **注記 (mutation ledger に転記すること)**: SQLite はインデックス無しの
    単純スキャンで rowid (=id) 昇順を返す実装になっているため、
    `ORDER BY decided_at, id` から `, id` を削る変異は、本テストの構成
    (物理走査順 = id 昇順 = 意図したタイブレーク勝者の順) では実行結果を
    変えない可能性が高い (equivalent mutant の疑い)。それでも**契約の
    ピンとして意味がある** (将来 SQL 実行計画が変わっても仕様どおりに
    振る舞うことを保証する) ため削除しない。実測して mutation ledger に
    生死どちらでも記録すること。"""
    plugins_dir = tmp_path / "plugins"
    plugins_dir.mkdir()
    d = _write_plugin(plugins_dir, "tie_ind")
    conn = _conn(tmp_path)
    from agentic_fx.plugin.loader import content_hash as ch
    h = ch(d)
    _decide(conn, "tie_ind", h, status="approved", now=NOW)  # id 小
    _decide(conn, "tie_ind", h, status="rejected", now=NOW)  # id 大・同時刻

    metas = plugin_loader.approved_plugins(conn, plugins_dir)

    assert metas == []  # id が大きい reject が勝つ
```

- [ ] **Step 2: テストを実行し、期待どおりに落ちることを確認する**

Run: `uv run pytest tests/tools/test_plugin_loader.py -k "Task 12 or reject_after_approve or null_decided_at or orders_by_decided_at or same_decided_at" -v`

（pytest の `-k` は docstring ではなく関数名でマッチするため、実際には以下を使う）

Run: `uv run pytest tests/tools/test_plugin_loader.py::test_approved_plugins_reject_after_approve_revokes tests/tools/test_plugin_loader.py::test_approved_plugins_excludes_approved_row_with_null_decided_at tests/tools/test_plugin_loader.py::test_approved_plugins_orders_by_decided_at_not_insertion_order tests/tools/test_plugin_loader.py::test_approved_plugins_same_decided_at_ties_break_by_higher_id -v`

Expected: **4 件 FAIL** (現行コードは `status='approved'` の存在だけを見るため、reject/decided_at NULL/時系列の逆転を無視して plugin を admit してしまう)。

`test_approved_plugins_approve_after_reject_readmits` / `test_approved_plugins_expired_after_approve_does_not_revoke` / `test_approved_plugins_invalidated_after_approve_does_not_revoke` は現行コードでも**たまたま green** になる (現行コードは reject/expired/invalidated 行を一切見ないため、approved 行が 1 つでもあれば admit してしまい、これらのテストの期待値 (「approve 済みなら admit」) と偶然一致する)。これは正常であり、実装後の回帰防止ピンとして機能する。

- [ ] **Step 3: `_approved_hashes_by_name` を実装する (`src/agentic_fx/tools/plugin_loader.py`)**

まず `approved_plugins()` の docstring (37-48 行目) 中の以下の段落を更新する:

```python
    同名 plugin に対して承認済み行が複数存在する場合、**現在のハッシュに
    一致する行が 1 つでもあれば**承認とみなす (「どれが最新の承認か」は
    判定しない — 一致という事実だけで十分)。
    """
```

を以下に置き換える:

```python
    同一 (name, content_hash) に対して決定 (approved/rejected) が複数
    存在する場合、**最新の決定** (`decided_at` 最大、同時刻は `id` 最大)
    が有効になる (設計書 §7 / D4、プラン9 Task 12)。後から reject すれば
    承認は取り消され、後から re-approve すれば再承認される。`expired` /
    `invalidated` は決定として数えない (新しい要求の失効が古い承認を
    取り消すのは誤り)。
    """
```

次に `_approved_hashes_by_name` 関数本体 (70-101 行目) を丸ごと置き換える:

```python
def _approved_hashes_by_name(conn: sqlite3.Connection) -> dict[str, set[str]]:
    """(name, content_hash) ごとの**最新決定**が approved のものだけを
    admit する (設計書 §7 / D4、プラン9 Task 12)。

    `status IN ('approved','rejected')` かつ `decided_at IS NOT NULL` を
    `ORDER BY decided_at, id` で読み、同じ (name, content_hash) を持つ行を
    順に上書きする — 最後に残った決定が最新の決定になる。同時刻は id が
    タイブレーク (SQL の第二ソートキー)。

    **`expired` / `invalidated` は決定として数えない** — WHERE 句で最初
    から除外する。どちらも「決定されないまま終端した要求」であり、新しい
    要求の失効が古い承認を取り消すのは誤り (D4)。

    既存の防御 (payload が JSON でない / dict でない / name・content_hash
    が str でない行の warning + skip) はそのまま維持する。
    """
    rows = conn.execute(
        "SELECT id, payload_json, status FROM approval_requests "
        "WHERE kind=? AND status IN ('approved','rejected') "
        "AND decided_at IS NOT NULL "
        "ORDER BY decided_at, id", (APPROVAL_KIND,)).fetchall()
    latest_status: dict[tuple[str, str], str] = {}
    for row in rows:
        try:
            payload = json.loads(row["payload_json"])
        except (json.JSONDecodeError, TypeError) as exc:
            # DB 破損 (手動編集・移行漏れ等) の観測性のため warning を残す
            # (approved_plugins() 自体は fail closed で継続 — この行は
            # ただ「承認情報として使えない」だけとして扱う)。
            _log.warning(
                "approval_requests id=%s (kind=%s): payload_json が JSON と"
                "して解釈できません (%s) — skipping this row",
                row["id"], APPROVAL_KIND, exc)
            continue
        if not isinstance(payload, dict):
            _log.warning(
                "approval_requests id=%s (kind=%s): payload が dict では"
                "ありません (got %s) — skipping this row",
                row["id"], APPROVAL_KIND, type(payload).__name__)
            continue
        name, content_hash = payload.get("name"), payload.get("content_hash")
        if isinstance(name, str) and isinstance(content_hash, str):
            latest_status[(name, content_hash)] = row["status"]
        else:
            _log.warning(
                "approval_requests id=%s (kind=%s): payload に有効な "
                "name/content_hash (str) がありません — skipping this row",
                row["id"], APPROVAL_KIND)

    out: dict[str, set[str]] = {}
    for (name, content_hash), status in latest_status.items():
        if status == "approved":
            out.setdefault(name, set()).add(content_hash)
    return out
```

`approved_plugins()` 本体 (49-67 行目) は無変更 — `_approved_hashes_by_name` の戻り値の型 (`dict[str, set[str]]`) が変わらないため、呼び出し側は無修正でよい。

- [ ] **Step 4: 全テストを実行し、green を確認する**

Run: `uv run pytest tests/tools/test_plugin_loader.py -v`

Expected: PASS (既存の全テスト + Step 1 で追加した 7 テストすべて)

- [ ] **Step 5: コミット**

```bash
git add src/agentic_fx/tools/plugin_loader.py tests/tools/test_plugin_loader.py
git commit -m "$(cat <<'EOF'
feat: approval の最新決定優先で plugin 承認取り消しを可能にする

status='approved' の存在だけを見ていたため、後から reject しても承認が
取り消せなかった (設計書 §7 裁定)。(name, content_hash) ごとに
decided_at, id 順で最新決定を求め、それが approved のものだけ admit する
よう改める。expired/invalidated は決定として数えない。

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

#### Task 12 変異ノート

`uv run pytest tests/tools/test_plugin_loader.py -q` を実行する前後で `find . -name __pycache__ -type d -not -path "./.venv/*" -exec rm -rf {} +` を必ず実行すること (プラン規約)。各変異は 1 つずつ独立に当て、意図した行を `grep -n` で確認してから実行する。

| # | 変異 (設計書 D4 の変異リスト由来) | 殺すテスト |
|---|---|---|
| 1 | `status IN ('approved','rejected')` を `status='approved'` に戻す (reject-after-approve のバグ復活) | `test_approved_plugins_reject_after_approve_revokes` |
| 2 | `latest_status[(name, content_hash)] = row["status"]` の代入を `if status=="approved": ...` のような早期絞り込みに変え、reject 行を辞書に反映しなくする | `test_approved_plugins_reject_after_approve_revokes` (reject 側の反映が消えると承認取り消しが効かなくなる) |
| 3 | `expired-after-approve` を取り消してしまう (`status IN (...)` に `'expired'` を混ぜる、または WHERE から `decided_at IS NOT NULL` を落として実質的に expired 行を拾ってしまう経路を作る) | `test_approved_plugins_expired_after_approve_does_not_revoke` |
| 4 | `invalidated` を決定に混ぜる | `test_approved_plugins_invalidated_after_approve_does_not_revoke` |
| 5 | `ORDER BY decided_at, id` の `decided_at, ` を削る (`ORDER BY id` になる) | `test_approved_plugins_orders_by_decided_at_not_insertion_order` |
| 6 | `ORDER BY decided_at, id` の `, id` を削る (`ORDER BY decided_at` になる) | `test_approved_plugins_same_decided_at_ties_break_by_higher_id` — **equivalent mutant の可能性あり (上記テスト docstring 参照)。実測して ledger に生死を記録すること** |
| 7 (追加) | `AND decided_at IS NOT NULL` を削る | `test_approved_plugins_excludes_approved_row_with_null_decided_at` |
| 8 (追加) | `out.setdefault(name, set()).add(content_hash)` を条件反転する (`status != "approved"` で admit してしまう) | `test_approved_plugins_reject_after_approve_revokes` と `test_approved_plugins_approve_after_reject_readmits` の両方が同時に反転して落ちる (approve/reject の判定が丸ごと逆転するため) |

**#7・#8 は設計書 D4 の変異リストに無い追加分。** #7 は「decided_at IS NOT NULL」という D4 本文の条件が変異リストに反映されていなかったための補完、#8 は「防御を複数の壊し方で当てる」というプラン規約 (呼び出しを消す/条件を反転する) に基づく追加。

---

### Task 13: `signals.claimed_by_mission_id` の FK migration

**設計の正**: 設計書 D5 (`2026-08-11-phase2-9-foundation-design.md:240-254`)、本体設計書 §12 (`2026-07-25-agentic-fx-design.md:808`)

**Files:**
- Modify: `src/agentic_fx/store/db.py` (DDL 定数の追加・`_SCHEMA` の書き換え・`_migrate_signals_fk` の新設・`init_db()` への配線)
- Test: `tests/store/test_db.py` (migration の新規テスト群)
- **既存テストの修復 (実装ではなく前提修理 — 下記 Step 1.5 で行う)**:
  - `tests/store/test_signals.py`
  - `tests/loops/test_trade_loop_phases.py`
  - `tests/test_service_app.py`

**Interfaces:**
- Consumes: `agentic_fx.store.missions.start(conn, loop, runner, model, now, trigger=None) -> int` (既存、プラン7で導入済み)
- Produces: `db_module._migrate_signals_fk(conn: sqlite3.Connection) -> None` (private — `init_db()` 内部専用。テストからは `from agentic_fx.store import db as db_module` で直接呼べる)
- 変更しないもの: `signals.py` の公開 API (`add`/`claim_oldest`/`consume`/`requeue`/`reclaim_expired`/`pending_exists`/`recent`) は**シグネチャも実装も無変更**。FK は DB 層の制約であり、既に正しい mission id を渡している呼び出し側 (`trade_loop.py`、`missions.recover_interrupted`) は無改修で動く。

#### 重大な前提修理: 3 つの既存テストファイルがダミーの mission_id を使っている

`signals.claim_oldest(conn, mission_id=N, ...)` は `claimed_by_mission_id` 列に `N` をそのまま書き込む。FK を追加すると、**`missions` テーブルに実在しない id を書き込む呼び出しは `sqlite3.IntegrityError` になる**。調査の結果、以下の 3 ファイルがダミー整数 (`mission_id=1`, `mission_id=2`, ..., `mission_id=999`) を使って `claim_oldest` を呼んでおり、そのままでは FK 追加後に軒並み失敗する:

1. `tests/store/test_signals.py` — ほぼ全テストが `mission_id=1/2/3/4` を使用 (実在する `missions` 行が無い)。加えて 279-296 行目付近に生 SQL `claimed_by_mission_id=1` の直接書き込みが 1 箇所ある
2. `tests/loops/test_trade_loop_phases.py:179,183` — `test_requeue_signal_does_not_send_notification_itself` が `mission_id=1` を使用
3. `tests/test_service_app.py:617` — `test_f1c_startup_reclaim_recovers_claimed_signal` が `mission_id=999` を使用

**除外の根拠 (この 3 ファイル以外を触らなくてよい理由)**: `claimed_by_mission_id` への書き込みが起きるのは `claim_oldest()` の `UPDATE ... SET claimed_by_mission_id=:mission_id` 経路と、生 SQL で直接この列に代入する箇所だけである。`grep -rn "claimed_by_mission_id\s*=\s*[0-9]" tests/ src/` で全リポジトリを検索し、生 SQL 代入は上記 1. の 1 箇所のみと確認済み。`claim_oldest` の呼び出し箇所は `grep -rln "claim_oldest" tests/` で 6 ファイルに絞り込み済みで、うち:
- `tests/store/test_missions_cas.py` は既に `missions.start()` で作った実在 id (`mid` 変数) を使っており対象外
- `tests/loops/test_trade_loop_signal.py` と `tests/tools/test_signal_tools.py` は `claim_oldest` を直接リテラル id で呼ぶ箇所が無い (前者は `claim_oldest` 自体を patch/mock している) ため対象外

以下の`test_signals.py`内の呼び出しは、`claimed_by_mission_id` へ**書き込まない**ため対象外とし変更しない (根拠を併記):
- `signals.consume(conn, ..., mission_id=999, now=NOW)` (mismatch を確かめるテスト) — `consume()` の SQL は `WHERE ... AND claimed_by_mission_id=?` で比較にしか使わず、SET句に無い
- `signals.claim_oldest(conn, mission_id=1, now=NAIVE_NOW, ...)` (naive now を拒否させるテスト) — `_require_aware(now, ...)` が SQL 実行前に例外を送出するため、UPDATE 文自体が実行されない

- [ ] **Step 1: `store/db.py` の migration テストを書く (`tests/store/test_db.py`)**

ファイル末尾に以下を追記する:

```python
def _legacy_signals_ddl() -> str:
    """FK 追加前 (Task 13 以前) の signals DDL。claimed_by_mission_id に
    REFERENCES が無い。"""
    return (
        "CREATE TABLE signals ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "plugin TEXT NOT NULL, content_hash TEXT NOT NULL,"
        "pair TEXT NOT NULL, timeframe TEXT NOT NULL, bar_ts TEXT NOT NULL,"
        "kind TEXT NOT NULL, payload_json TEXT NOT NULL,"
        "status TEXT NOT NULL DEFAULT 'pending'"
        "  CHECK(status IN ('pending','claimed','consumed','abandoned')),"
        "claimed_by_mission_id INTEGER, claimed_at TEXT,"
        "requeue_count INTEGER NOT NULL DEFAULT 0,"
        "created_at TEXT NOT NULL,"
        "UNIQUE(plugin, content_hash, pair, timeframe, bar_ts))")


def _legacy_signals_and_missions_conn(tmp_path):
    """レガシー (FK 無し) signals + 標準スキーマの missions 等を持つ接続を
    返す (Task 13 の repair 単体テスト用)。`_SCHEMA` は signals を
    `CREATE TABLE IF NOT EXISTS` で作るため、先に手動でレガシー signals を
    作っておけば `executescript(_SCHEMA)` はそれを温存したまま missions 等
    他テーブルだけを作る。"""
    from agentic_fx.store import db as db_module

    conn = connect(tmp_path / "legacy.db")
    conn.execute(_legacy_signals_ddl())
    conn.commit()
    conn.executescript(db_module._SCHEMA)
    return conn


def _insert_legacy_signal(conn, *, content_hash, status,
                          claimed_by_mission_id, claimed_at):
    conn.execute(
        "INSERT INTO signals (plugin, content_hash, pair, timeframe, "
        "bar_ts, kind, payload_json, status, claimed_by_mission_id, "
        "claimed_at, requeue_count, created_at) VALUES "
        "('p', ?, 'USDJPY', '1h', '2026-08-03T12:00:00+00:00', 'signal', "
        "'{}', ?, ?, ?, 0, '2026-08-03T11:00:00+00:00')",
        (content_hash, status, claimed_by_mission_id, claimed_at))
    conn.commit()


# --- 受入条件: FK 自体 ------------------------------------------------

def test_init_db_fresh_signals_table_has_missions_fk(tmp_path):
    conn = connect(tmp_path / "fresh.db")
    init_db(conn)
    fks = conn.execute("PRAGMA foreign_key_list(signals)").fetchall()
    assert any(fk["table"] == "missions"
              and fk["from"] == "claimed_by_mission_id" for fk in fks)


def test_signals_fk_rejects_nonexistent_mission_id_after_migration(tmp_path):
    conn = connect(tmp_path / "fresh.db")
    init_db(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO signals (plugin, content_hash, pair, timeframe, "
            "bar_ts, kind, payload_json, status, claimed_by_mission_id, "
            "claimed_at, requeue_count, created_at) VALUES "
            "('p','h','USDJPY','1h','2026-08-03T12:00:00+00:00','signal',"
            "'{}','claimed', 12345, '2026-08-03T12:00:00+00:00', 0, "
            "'2026-08-03T11:00:00+00:00')")


# --- 受入条件: legacy DB からの移行 (エンドツーエンド) -------------------

def test_init_db_migrates_legacy_signals_to_v2(tmp_path):
    """旧 (FK 無し) signals を持つ DB に init_db を流すと FK 付きへ
    再構築され、宙吊り行 (missions 行が無い claimed) が修復されること。"""
    conn = connect(tmp_path / "legacy.db")
    conn.execute(_legacy_signals_ddl())
    conn.execute(
        "INSERT INTO signals (plugin, content_hash, pair, timeframe, "
        "bar_ts, kind, payload_json, status, claimed_by_mission_id, "
        "claimed_at, requeue_count, created_at) VALUES "
        "('p','h','USDJPY','1h','2026-08-03T12:00:00+00:00','signal','{}',"
        "'claimed', 999, '2026-08-03T11:30:00+00:00', 0, "
        "'2026-08-03T11:00:00+00:00')")
    conn.commit()

    init_db(conn)  # missions は空のまま作られるので 999 は宙吊り

    row = conn.execute(
        "SELECT status, claimed_by_mission_id, claimed_at "
        "FROM signals").fetchone()
    assert row["status"] == "pending"
    assert row["claimed_by_mission_id"] is None
    assert row["claimed_at"] is None
    fks = conn.execute("PRAGMA foreign_key_list(signals)").fetchall()
    assert any(fk["table"] == "missions" for fk in fks)


def test_signals_migration_is_idempotent(tmp_path):
    conn = connect(tmp_path / "legacy.db")
    conn.execute(_legacy_signals_ddl())
    conn.execute(
        "INSERT INTO signals (plugin, content_hash, pair, timeframe, "
        "bar_ts, kind, payload_json, status, requeue_count, created_at) "
        "VALUES ('p','h','USDJPY','1h','2026-08-03T12:00:00+00:00','signal',"
        "'{}','pending', 0, '2026-08-03T11:00:00+00:00')")
    conn.commit()

    init_db(conn)
    init_db(conn)  # 2 回目でも例外なし

    names = {r["name"] for r in
             conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "signals_v1" not in names
    assert conn.execute("SELECT COUNT(*) c FROM signals").fetchone()["c"] == 1


# --- 受入条件: status ごとの修復規則 (D5、1 検査目的 1 テスト) -----------

def test_migrate_signals_fk_repairs_dangling_claimed_row(tmp_path):
    """D5: status='claimed' かつ参照先 missions 行が無い → pending に戻し
    claimed_by_mission_id/claimed_at を NULL にする。"""
    from agentic_fx.store import db as db_module

    conn = _legacy_signals_and_missions_conn(tmp_path)
    _insert_legacy_signal(conn, content_hash="dangling_claimed",
                          status="claimed", claimed_by_mission_id=999,
                          claimed_at="2026-08-03T11:30:00+00:00")

    db_module._migrate_signals_fk(conn)

    row = conn.execute(
        "SELECT status, claimed_by_mission_id, claimed_at FROM signals "
        "WHERE content_hash='dangling_claimed'").fetchone()
    assert row["status"] == "pending"
    assert row["claimed_by_mission_id"] is None
    assert row["claimed_at"] is None


def test_migrate_signals_fk_leaves_claimed_row_with_valid_mission_untouched(
        tmp_path):
    """claimed かつ参照先 missions 行が実在する場合は無変更 — repair が
    dangling 行だけに当たり、有効な claim の監査情報を壊さないことの
    ピン (D5 の変異リストには無いが、"claimed の修復を落とす" の逆方向の
    検査として必要)。"""
    from agentic_fx.store import db as db_module
    from agentic_fx.store import missions

    conn = _legacy_signals_and_missions_conn(tmp_path)
    now = datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc)
    mid = missions.start(conn, "trade", "local", "m", now)
    _insert_legacy_signal(conn, content_hash="valid_claimed",
                          status="claimed", claimed_by_mission_id=mid,
                          claimed_at="2026-08-03T11:30:00+00:00")

    db_module._migrate_signals_fk(conn)

    row = conn.execute(
        "SELECT status, claimed_by_mission_id, claimed_at FROM signals "
        "WHERE content_hash='valid_claimed'").fetchone()
    assert row["status"] == "claimed"
    assert row["claimed_by_mission_id"] == mid
    assert row["claimed_at"] == "2026-08-03T11:30:00+00:00"


def test_migrate_signals_fk_repairs_dangling_consumed_row_without_reviving(
        tmp_path):
    """D5: status='consumed' かつ dangling → claimed_by_mission_id のみ
    NULL、status は 'consumed' のまま (終端状態を蘇らせない)。"""
    from agentic_fx.store import db as db_module

    conn = _legacy_signals_and_missions_conn(tmp_path)
    _insert_legacy_signal(conn, content_hash="dangling_consumed",
                          status="consumed", claimed_by_mission_id=999,
                          claimed_at="2026-08-03T11:30:00+00:00")

    db_module._migrate_signals_fk(conn)

    row = conn.execute(
        "SELECT status, claimed_by_mission_id FROM signals "
        "WHERE content_hash='dangling_consumed'").fetchone()
    assert row["status"] == "consumed"  # pending に戻さない
    assert row["claimed_by_mission_id"] is None


def test_migrate_signals_fk_repairs_dangling_abandoned_row_without_reviving(
        tmp_path):
    """D5: status='abandoned' かつ dangling → claimed_by_mission_id のみ
    NULL、status は 'abandoned' のまま。"""
    from agentic_fx.store import db as db_module

    conn = _legacy_signals_and_missions_conn(tmp_path)
    _insert_legacy_signal(conn, content_hash="dangling_abandoned",
                          status="abandoned", claimed_by_mission_id=999,
                          claimed_at="2026-08-03T11:30:00+00:00")

    db_module._migrate_signals_fk(conn)

    row = conn.execute(
        "SELECT status, claimed_by_mission_id FROM signals "
        "WHERE content_hash='dangling_abandoned'").fetchone()
    assert row["status"] == "abandoned"
    assert row["claimed_by_mission_id"] is None


def test_migrate_signals_fk_leaves_pending_row_unchanged(tmp_path):
    """D5: status='pending' (claimed_by_mission_id は元々 NULL) → 無変更。"""
    from agentic_fx.store import db as db_module

    conn = _legacy_signals_and_missions_conn(tmp_path)
    _insert_legacy_signal(conn, content_hash="already_pending",
                          status="pending", claimed_by_mission_id=None,
                          claimed_at=None)

    db_module._migrate_signals_fk(conn)

    row = conn.execute(
        "SELECT status, claimed_by_mission_id, claimed_at FROM signals "
        "WHERE content_hash='already_pending'").fetchone()
    assert row["status"] == "pending"
    assert row["claimed_by_mission_id"] is None
    assert row["claimed_at"] is None


def test_migrate_signals_fk_leaves_foreign_key_check_clean(tmp_path):
    """移行後は PRAGMA foreign_key_check(signals) が空であること (D5 の
    受入条件の直接ピン)。"""
    from agentic_fx.store import db as db_module

    conn = _legacy_signals_and_missions_conn(tmp_path)
    _insert_legacy_signal(conn, content_hash="dangling",
                          status="claimed", claimed_by_mission_id=999,
                          claimed_at="2026-08-03T11:30:00+00:00")

    db_module._migrate_signals_fk(conn)

    violations = conn.execute("PRAGMA foreign_key_check(signals)").fetchall()
    assert violations == []


# --- 受入条件: UNIQUE / CHECK / id / sqlite_sequence の温存 ------------

def test_signals_migration_preserves_unique_constraint(tmp_path):
    conn = connect(tmp_path / "legacy.db")
    conn.execute(_legacy_signals_ddl())
    conn.execute(
        "INSERT INTO signals (plugin, content_hash, pair, timeframe, "
        "bar_ts, kind, payload_json, status, requeue_count, created_at) "
        "VALUES ('p','h','USDJPY','1h','2026-08-03T12:00:00+00:00','signal',"
        "'{}','pending', 0, '2026-08-03T11:00:00+00:00')")
    conn.commit()

    init_db(conn)

    from agentic_fx.store import signals as signals_module
    dup = signals_module.add(
        conn, plugin="p", content_hash="h", pair="USDJPY", timeframe="1h",
        bar_ts="2026-08-03T12:00:00+00:00", kind="signal", payload={"x": 2},
        now=datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc))
    assert dup is None  # UNIQUE(plugin,content_hash,pair,timeframe,bar_ts)


def test_signals_migration_preserves_status_check(tmp_path):
    conn = connect(tmp_path / "legacy.db")
    conn.execute(_legacy_signals_ddl())
    conn.commit()

    init_db(conn)

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO signals (plugin, content_hash, pair, timeframe, "
            "bar_ts, kind, payload_json, status, requeue_count, created_at) "
            "VALUES ('p','h','USDJPY','1h','2026-08-03T12:00:00+00:00',"
            "'signal','{}','bogus_status', 0, '2026-08-03T11:00:00+00:00')")


def test_signals_migration_preserves_ids_and_autoincrement_sequence(
        tmp_path):
    conn = connect(tmp_path / "legacy.db")
    conn.execute(_legacy_signals_ddl())
    conn.execute(
        "INSERT INTO signals (id, plugin, content_hash, pair, timeframe, "
        "bar_ts, kind, payload_json, status, claimed_by_mission_id, "
        "claimed_at, requeue_count, created_at) VALUES "
        "(7,'p','h','USDJPY','1h','2026-08-03T12:00:00+00:00','signal','{}',"
        "'pending', NULL, NULL, 0, '2026-08-03T11:00:00+00:00')")
    conn.commit()

    init_db(conn)

    row = conn.execute("SELECT id FROM signals").fetchone()
    assert row["id"] == 7  # id は書き換えられない

    from agentic_fx.store import signals as signals_module
    new_id = signals_module.add(
        conn, plugin="p2", content_hash="h2", pair="USDJPY", timeframe="1h",
        bar_ts="2026-08-03T13:00:00+00:00", kind="signal", payload={},
        now=datetime(2026, 8, 3, 13, 0, tzinfo=timezone.utc))
    assert new_id > 7  # AUTOINCREMENT シーケンスが id=7 を踏まえて続く


# --- PRAGMA foreign_keys の復元 (BEGIN 前後のトグル順序のピン) ---------

def test_migrate_signals_fk_restores_foreign_keys_pragma_after_failure(
        tmp_path):
    """SQLite はトランザクション開始後の `PRAGMA foreign_keys` 変更を
    無視する (実測確認済み)。migration は BEGIN の**外側**で OFF にし、
    成功・失敗を問わず ON に戻さねばならない — 戻し忘れると以降の
    プロセス全体で FK 保護が無効になる。INSERT を意図的に失敗させ、
    例外後も `PRAGMA foreign_keys` が 1 (ON) であることを確認する。"""
    from agentic_fx.store import db as db_module

    conn = _legacy_signals_and_missions_conn(tmp_path)
    _insert_legacy_signal(conn, content_hash="x", status="pending",
                          claimed_by_mission_id=None, claimed_at=None)

    class _FailingConn:
        def __init__(self, real):
            self._real = real

        def execute(self, sql, *args, **kwargs):
            if str(sql).strip().startswith("INSERT OR IGNORE INTO signals "):
                raise sqlite3.OperationalError("injected failure")
            return self._real.execute(sql, *args, **kwargs)

        def __getattr__(self, name):
            return getattr(self._real, name)

    with pytest.raises(sqlite3.OperationalError):
        db_module._migrate_signals_fk(_FailingConn(conn))

    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_migrate_signals_fk_disables_foreign_keys_before_begin(tmp_path):
    """順序そのものを観測し、BEGIN 後へ OFF を移す変異を殺す。"""
    from agentic_fx.store import db as db_module
    real = _legacy_signals_and_missions_conn(tmp_path)

    class _OrderConn:
        def __init__(self, conn):
            self._conn, self.events = conn, []
        def execute(self, sql, *args, **kwargs):
            normalized = " ".join(str(sql).split()).upper()
            if normalized in {"PRAGMA FOREIGN_KEYS=OFF", "BEGIN IMMEDIATE"}:
                self.events.append(normalized)
            return self._conn.execute(sql, *args, **kwargs)
        def __getattr__(self, name):
            return getattr(self._conn, name)

    observed = _OrderConn(real)
    db_module._migrate_signals_fk(observed)
    assert observed.events.index("PRAGMA FOREIGN_KEYS=OFF") < \
           observed.events.index("BEGIN IMMEDIATE")
```

- [ ] **Step 1.5: `datetime`/`timezone` の import を `test_db.py` に追加する**

`test_db.py` は現状トップレベルで `datetime` を import していない (各テストがローカル import している)。Step 1 で追加したテストの一部 (`test_signals_migration_preserves_unique_constraint` など) はトップレベルの `datetime`/`timezone` を使うため、ファイル冒頭の import ブロックを以下に置き換える:

```python
import sqlite3
from datetime import datetime, timezone

import pytest

from agentic_fx.store.db import TABLE_NAMES, connect, connect_readonly, init_db
```

- [ ] **Step 2: テストを実行し、期待どおりに落ちることを確認する**

Run: `uv run pytest tests/store/test_db.py -k signals -v`

Expected: 新 migration を直接要求するテストは FAIL。既存 schema/fixture の
回帰 pin など変更前から成立するテストは green でよく、「全件 FAIL」は要求しない。

- [ ] **Step 3: 既存の 3 ファイルの前提を修理する (まだ FK は追加しない — この時点ではスイート全体が green のままであること)**

**3-a. `tests/store/test_signals.py` を全文置き換える。**

```python
"""signals ストアのテスト (プラン 7 Task 7)。

signals は 4 状態 (pending/claimed/consumed/abandoned) のキュー。§5 の
「3 段階」は正常系遷移 (pending → claimed → consumed) の呼称で、abandoned
は requeue 上限超過 / 鮮度切れの終端状態。

コントローラ解決 (task-7-brief.md 添付) の骨子:
- 格納値は isoformat ('T' 区切り)。SQLite の datetime() は 'T' 区切りを
  受理するが返り値はスペース区切りなので、鮮度・lease 判定は両辺を
  datetime() で正規化して比較する ('T' vs ' ' 文字列比較の罠)。
- claim_oldest の freshness_bars=None は鮮度条件そのものを WHERE から
  外す (ゲート無効)。
- CASE 式 (timeframe→分) は PLUGIN_TIMEFRAMES と同期必須 — 同期テストを
  必ず置く。
- requeue の上限判定は「現在の requeue_count >= max_requeue なら
  abandoned (増分なし)、未満なら pending + requeue_count+1」
  (controller 解決 #6 の逐語)。reclaim_expired も同じ判定を通す。

**Task 13 (プラン9 束D、設計書 D5)**: claimed_by_mission_id に
missions(id) への FK が付いたため、claim_oldest に渡す mission_id は
実在する missions 行でなければならない。以前はダミー整数リテラル
(1, 2, 3, ...) を使っていたが、`_mid(conn)` で実在する missions 行を
作ってその id を渡す。consume() の mismatch 比較や、naive now を拒否する
テストのように SQL の UPDATE が実行されない (=書き込みが起きない) 箇所は
FK の影響を受けないため変更していない。
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

import pytest

from agentic_fx.backtest.timeframes import PLUGIN_TIMEFRAMES
from agentic_fx.store import missions, signals
from agentic_fx.store.db import connect, init_db

NOW = datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc)


def _conn(tmp_path):
    conn = connect(tmp_path / "t.db")
    init_db(conn)
    return conn


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _add(conn, *, bar_ts: datetime, content_hash="h1", pair="USDJPY",
         timeframe="1h", kind="signal", payload=None, now=NOW):
    return signals.add(
        conn, plugin="p.py", content_hash=content_hash, pair=pair,
        timeframe=timeframe, bar_ts=_iso(bar_ts), kind=kind,
        payload=payload or {"x": 1}, now=now)


def _mid(conn) -> int:
    """テスト用に実在する missions 行を 1 つ作り、その id を返す
    (Task 13: claim_oldest が書き込む mission id は missions テーブルに
    実在しないと sqlite3.IntegrityError になる)。"""
    return missions.start(conn, "trade", "local", "m", NOW)


# ---------------------------------------------------------------------
# ① 重複キー 2 回目 None
# ---------------------------------------------------------------------
def test_add_duplicate_key_returns_none_on_second_insert(tmp_path):
    conn = _conn(tmp_path)
    bar_ts = NOW
    first = _add(conn, bar_ts=bar_ts)
    second = _add(conn, bar_ts=bar_ts)
    assert isinstance(first, int)
    assert second is None
    rows = conn.execute("SELECT COUNT(*) c FROM signals").fetchone()
    assert rows["c"] == 1


# ---------------------------------------------------------------------
# ② content_hash 違いは別行
# ---------------------------------------------------------------------
def test_add_different_content_hash_is_separate_row(tmp_path):
    conn = _conn(tmp_path)
    bar_ts = NOW
    first = _add(conn, bar_ts=bar_ts, content_hash="h1")
    second = _add(conn, bar_ts=bar_ts, content_hash="h2")
    assert isinstance(first, int) and isinstance(second, int)
    assert first != second
    rows = conn.execute("SELECT COUNT(*) c FROM signals").fetchone()
    assert rows["c"] == 2


# ---------------------------------------------------------------------
# ③ claim_oldest が bar_ts 最古
# ---------------------------------------------------------------------
def test_claim_oldest_picks_oldest_bar_ts_even_if_inserted_later(tmp_path):
    """新しい bar_ts を先に INSERT する (id ASC だけでは正しい行を選べない
    ことを保証する — ORDER BY datetime(bar_ts) が効いているかの killer)。"""
    conn = _conn(tmp_path)
    newer_id = _add(conn, bar_ts=datetime(2026, 8, 3, 11, 0, tzinfo=timezone.utc),
                    content_hash="newer")
    older_id = _add(conn, bar_ts=datetime(2026, 8, 3, 9, 0, tzinfo=timezone.utc),
                    content_hash="older")
    assert newer_id < older_id  # id 順は逆 (先に入れた方が古い bar_ts)

    m1 = _mid(conn)
    claimed = signals.claim_oldest(conn, mission_id=m1, now=NOW,
                                    freshness_bars=None)
    assert claimed is not None
    assert claimed["id"] == older_id
    assert claimed["status"] == "claimed"
    assert claimed["claimed_by_mission_id"] == m1


# ---------------------------------------------------------------------
# ④ 2 連続 claim は別行
# ---------------------------------------------------------------------
def test_two_consecutive_claims_return_different_rows(tmp_path):
    conn = _conn(tmp_path)
    id1 = _add(conn, bar_ts=datetime(2026, 8, 3, 9, 0, tzinfo=timezone.utc),
              content_hash="a")
    id2 = _add(conn, bar_ts=datetime(2026, 8, 3, 10, 0, tzinfo=timezone.utc),
              content_hash="b")
    c1 = signals.claim_oldest(conn, mission_id=_mid(conn), now=NOW,
                              freshness_bars=None)
    c2 = signals.claim_oldest(conn, mission_id=_mid(conn), now=NOW,
                              freshness_bars=None)
    assert {c1["id"], c2["id"]} == {id1, id2}
    assert c1["id"] != c2["id"]
    c3 = signals.claim_oldest(conn, mission_id=_mid(conn), now=NOW,
                              freshness_bars=None)
    assert c3 is None  # もう pending は無い


# ---------------------------------------------------------------------
# ⑤ consume の mission_id 不一致拒否
# ---------------------------------------------------------------------
def test_consume_rejects_mismatched_mission_id(tmp_path):
    conn = _conn(tmp_path)
    _add(conn, bar_ts=NOW)
    claimed = signals.claim_oldest(conn, mission_id=_mid(conn), now=NOW,
                                    freshness_bars=None)
    with pytest.raises(ValueError, match="not claimed"):
        # 999 は claimed_by_mission_id へ書き込まれない (WHERE の比較にしか
        # 使われない) ため実在の missions 行である必要は無い。
        signals.consume(conn, claimed["id"], mission_id=999, now=NOW)
    row = conn.execute("SELECT status FROM signals WHERE id=?",
                       (claimed["id"],)).fetchone()
    assert row["status"] == "claimed"  # 拒否されたので状態不変


def test_consume_succeeds_with_matching_mission_id(tmp_path):
    conn = _conn(tmp_path)
    _add(conn, bar_ts=NOW)
    m1 = _mid(conn)
    claimed = signals.claim_oldest(conn, mission_id=m1, now=NOW,
                                    freshness_bars=None)
    signals.consume(conn, claimed["id"], mission_id=m1, now=NOW)
    row = conn.execute("SELECT status FROM signals WHERE id=?",
                       (claimed["id"],)).fetchone()
    assert row["status"] == "consumed"


def test_consume_rejects_non_claimed_status(tmp_path):
    """pending のまま consume しようとすると拒否 (fail closed)。"""
    conn = _conn(tmp_path)
    sid = _add(conn, bar_ts=NOW)
    with pytest.raises(ValueError, match="not claimed"):
        signals.consume(conn, sid, mission_id=1, now=NOW)


# ---------------------------------------------------------------------
# ⑥ requeue 上限超過 abandoned (境界を厳密にピン: >= max_requeue で
#    abandoned、増分なし。未満は pending + requeue_count+1)
# ---------------------------------------------------------------------
def test_requeue_boundary_table(tmp_path):
    conn = _conn(tmp_path)
    max_requeue = 2
    sid = _add(conn, bar_ts=NOW)

    signals.claim_oldest(conn, mission_id=_mid(conn), now=NOW,
                         freshness_bars=None)
    status = signals.requeue(conn, sid, now=NOW, max_requeue=max_requeue)
    assert status == "pending"
    row = conn.execute("SELECT requeue_count FROM signals WHERE id=?",
                       (sid,)).fetchone()
    assert row["requeue_count"] == 1

    signals.claim_oldest(conn, mission_id=_mid(conn), now=NOW,
                         freshness_bars=None)
    status = signals.requeue(conn, sid, now=NOW, max_requeue=max_requeue)
    assert status == "pending"
    row = conn.execute("SELECT requeue_count FROM signals WHERE id=?",
                       (sid,)).fetchone()
    assert row["requeue_count"] == 2

    signals.claim_oldest(conn, mission_id=_mid(conn), now=NOW,
                         freshness_bars=None)
    status = signals.requeue(conn, sid, now=NOW, max_requeue=max_requeue)
    assert status == "abandoned"  # requeue_count(2) >= max_requeue(2)
    row = conn.execute(
        "SELECT status, requeue_count, claimed_by_mission_id, claimed_at "
        "FROM signals WHERE id=?", (sid,)).fetchone()
    assert row["status"] == "abandoned"
    assert row["requeue_count"] == 2  # abandoned 分岐では増分しない
    assert row["claimed_by_mission_id"] is None
    assert row["claimed_at"] is None


def test_requeue_rejects_non_claimed_status(tmp_path):
    conn = _conn(tmp_path)
    sid = _add(conn, bar_ts=NOW)
    with pytest.raises(ValueError, match="is not claimed"):
        signals.requeue(conn, sid, now=NOW, max_requeue=2)


# ---------------------------------------------------------------------
# ⑦ reclaim_expired: 期限内は不変・期限切れは requeue_count 増・
#    上限超過 abandoned。lease 境界 <= を厳密にピン。
# ---------------------------------------------------------------------
def test_reclaim_expired_boundary_and_transitions(tmp_path):
    conn = _conn(tmp_path)
    lease_min = 15
    max_requeue = 2

    # 期限内 (claimed_at == now - 14min, cutoff は now - 15min): 不変
    within_id = _add(conn, bar_ts=NOW, content_hash="within")
    signals.claim_oldest(conn, mission_id=_mid(conn), now=NOW,
                         freshness_bars=None)
    within_claimed_at = NOW - timedelta(minutes=14)
    conn.execute("UPDATE signals SET claimed_at=? WHERE id=?",
                (within_claimed_at.isoformat(), within_id))

    # ちょうど境界 (claimed_at == now - 15min): <= なので回収対象
    boundary_id = _add(conn, bar_ts=NOW, content_hash="boundary")
    signals.claim_oldest(conn, mission_id=_mid(conn), now=NOW,
                         freshness_bars=None)
    boundary_claimed_at = NOW - timedelta(minutes=15)
    conn.execute("UPDATE signals SET claimed_at=? WHERE id=?",
                (boundary_claimed_at.isoformat(), boundary_id))

    # 期限切れ・requeue_count=1 で上限未満 → pending + count=2
    expired_id = _add(conn, bar_ts=NOW, content_hash="expired")
    signals.claim_oldest(conn, mission_id=_mid(conn), now=NOW,
                         freshness_bars=None)
    conn.execute(
        "UPDATE signals SET claimed_at=?, requeue_count=1 WHERE id=?",
        ((NOW - timedelta(hours=2)).isoformat(), expired_id))

    # 期限切れ・requeue_count=2 (>= max_requeue) → abandoned・増分なし
    over_id = _add(conn, bar_ts=NOW, content_hash="over")
    signals.claim_oldest(conn, mission_id=_mid(conn), now=NOW,
                         freshness_bars=None)
    conn.execute(
        "UPDATE signals SET claimed_at=?, requeue_count=2 WHERE id=?",
        ((NOW - timedelta(hours=2)).isoformat(), over_id))
    conn.commit()

    result = signals.reclaim_expired(conn, now=NOW, lease_min=lease_min,
                                     max_requeue=max_requeue)
    assert sorted(result) == sorted(["pending", "pending", "abandoned"])

    def _row(sid):
        return dict(conn.execute(
            "SELECT status, requeue_count, claimed_by_mission_id, claimed_at "
            "FROM signals WHERE id=?", (sid,)).fetchone())

    within = _row(within_id)
    assert within["status"] == "claimed"  # 不変
    assert within["requeue_count"] == 0

    boundary = _row(boundary_id)
    assert boundary["status"] == "pending"  # 境界含む (<=)
    assert boundary["requeue_count"] == 1

    expired = _row(expired_id)
    assert expired["status"] == "pending"
    assert expired["requeue_count"] == 2
    assert expired["claimed_by_mission_id"] is None

    over = _row(over_id)
    assert over["status"] == "abandoned"
    assert over["requeue_count"] == 2  # 増分なし
    assert over["claimed_by_mission_id"] is None


# ---------------------------------------------------------------------
# ⑧ abandoned は claim 対象外
# ---------------------------------------------------------------------
def test_abandoned_is_not_claimable(tmp_path):
    conn = _conn(tmp_path)
    sid = _add(conn, bar_ts=NOW)
    signals.claim_oldest(conn, mission_id=_mid(conn), now=NOW,
                         freshness_bars=None)
    signals.requeue(conn, sid, now=NOW, max_requeue=0)  # 即 abandoned
    row = conn.execute("SELECT status FROM signals WHERE id=?",
                       (sid,)).fetchone()
    assert row["status"] == "abandoned"
    claimed = signals.claim_oldest(conn, mission_id=_mid(conn), now=NOW,
                                   freshness_bars=None)
    assert claimed is None


# ---------------------------------------------------------------------
# ⑨ expire_stale: freshness 超過の pending が abandoned・以内は残る・
#    claimed には触れない
# ---------------------------------------------------------------------
def test_expire_stale_only_touches_stale_pending(tmp_path):
    conn = _conn(tmp_path)
    freshness_bars = 2  # 1h × 2 = 120min cutoff

    stale_id = _add(conn, bar_ts=datetime(2026, 8, 3, 9, 0, tzinfo=timezone.utc),
                    content_hash="stale")  # 3h 前 -> stale
    fresh_id = _add(conn, bar_ts=datetime(2026, 8, 3, 11, 0, tzinfo=timezone.utc),
                    content_hash="fresh")  # 1h 前 -> fresh
    claimed_stale_id = _add(
        conn, bar_ts=datetime(2026, 8, 3, 9, 0, tzinfo=timezone.utc),
        content_hash="claimed_stale")  # stale だが claimed 済み → 触れない
    # claim_oldest は bar_ts 最古優先で stale_id (同じ bar_ts だが id が
    # 若い) を選んでしまうため、対象行を直接 claimed にする (このテストは
    # claim_oldest の選択ロジックではなく expire_stale の対象範囲を見る)。
    # Task 13: claimed_by_mission_id には実在する mission id が要る。
    m1 = _mid(conn)
    conn.execute("UPDATE signals SET status='claimed', "
                "claimed_by_mission_id=?, claimed_at=? WHERE id=?",
                (m1, NOW.isoformat(), claimed_stale_id))
    conn.commit()
    row = conn.execute("SELECT status FROM signals WHERE id=?",
                       (claimed_stale_id,)).fetchone()
    assert row["status"] == "claimed"

    count = signals.expire_stale(conn, now=NOW, freshness_bars=freshness_bars)
    assert count == 1

    assert conn.execute("SELECT status FROM signals WHERE id=?",
                        (stale_id,)).fetchone()["status"] == "abandoned"
    assert conn.execute("SELECT status FROM signals WHERE id=?",
                        (fresh_id,)).fetchone()["status"] == "pending"
    assert conn.execute("SELECT status FROM signals WHERE id=?",
                        (claimed_stale_id,)).fetchone()["status"] == "claimed"


# ---------------------------------------------------------------------
# ⑩ 1h/4h/1d 混在の pending で timeframe 別 cutoff が SQL 内で正しく効く
#    (codex R3 C1 killer)
# ---------------------------------------------------------------------
def test_expire_stale_mixed_timeframes_cutoff(tmp_path):
    conn = _conn(tmp_path)
    freshness_bars = 2
    # 1h の 3 バー前 (3h 前) = stale (cutoff 2*60=120min)
    h1_id = _add(conn, bar_ts=datetime(2026, 8, 3, 9, 0, tzinfo=timezone.utc),
                content_hash="h1", timeframe="1h")
    # 4h の 3 バー前 相当 (12h 前と見せかけて実際は 3h 前 = fresh。
    # cutoff は 2*240=480min=8h なので 3h 前は fresh)
    h4_id = _add(conn, bar_ts=datetime(2026, 8, 3, 9, 0, tzinfo=timezone.utc),
                content_hash="h4", timeframe="4h")
    # 1d の 3 時間前 = fresh (cutoff 2*1440=2880min=48h)
    d1_id = _add(conn, bar_ts=datetime(2026, 8, 3, 9, 0, tzinfo=timezone.utc),
                content_hash="d1", timeframe="1d")

    count = signals.expire_stale(conn, now=NOW, freshness_bars=freshness_bars)
    assert count == 1  # 1h の行だけ stale

    assert conn.execute("SELECT status FROM signals WHERE id=?",
                        (h1_id,)).fetchone()["status"] == "abandoned"
    assert conn.execute("SELECT status FROM signals WHERE id=?",
                        (h4_id,)).fetchone()["status"] == "pending"
    assert conn.execute("SELECT status FROM signals WHERE id=?",
                        (d1_id,)).fetchone()["status"] == "pending"


# ---------------------------------------------------------------------
# ⑪ expire_stale を呼ばずに claim_oldest だけ呼んでも stale 行は claim
#    されない (claim 側の鮮度条件ピン)
# ---------------------------------------------------------------------
def test_claim_oldest_skips_stale_row_without_expire_stale(tmp_path):
    conn = _conn(tmp_path)
    freshness_bars = 2
    # stale (bar_ts が古い方) と fresh (新しい方) の 2 行。bar_ts 最古優先
    # だけを見ると stale の方が選ばれてしまう — 鮮度ゲートが効いているかの
    # killer。
    stale_id = _add(conn, bar_ts=datetime(2026, 8, 3, 9, 0, tzinfo=timezone.utc),
                    content_hash="stale")  # 3h 前 -> stale (1h tf, cutoff 2h)
    fresh_id = _add(conn, bar_ts=datetime(2026, 8, 3, 11, 30,
                                          tzinfo=timezone.utc),
                    content_hash="fresh")  # 30min 前 -> fresh

    claimed = signals.claim_oldest(conn, mission_id=_mid(conn), now=NOW,
                                   freshness_bars=freshness_bars)
    assert claimed is not None
    assert claimed["id"] == fresh_id  # stale はスキップされ fresh が選ばれる

    # stale 行はまだ pending のまま (claim も expire もされていない)
    row = conn.execute("SELECT status FROM signals WHERE id=?",
                       (stale_id,)).fetchone()
    assert row["status"] == "pending"


# ---------------------------------------------------------------------
# claim 経路でも timeframe 別 cutoff が効くことをピン (⑩ は expire_stale
# 経由、⑪ は 1h のみでの claim 経路だったため、claim が「行ごとの
# timeframe」で cutoff を計算していることを直接見るテストが無かった —
# advisor 指摘)。
# ---------------------------------------------------------------------
def test_claim_oldest_computes_cutoff_per_row_timeframe(tmp_path):
    conn = _conn(tmp_path)
    freshness_bars = 2
    # 1h, bar_ts が古い方 (3h 前) -> stale (cutoff 2*60=120min)。bar_ts が
    # 最古なので鮮度ゲートが無ければこちらが選ばれてしまう。
    stale_1h_id = _add(
        conn, bar_ts=datetime(2026, 8, 3, 9, 0, tzinfo=timezone.utc),
        content_hash="stale_1h", timeframe="1h")
    # 4h, 同じく 3h 前だが cutoff は 2*240=480min=8h なので fresh。
    fresh_4h_id = _add(
        conn, bar_ts=datetime(2026, 8, 3, 9, 0, tzinfo=timezone.utc),
        content_hash="fresh_4h", timeframe="4h")

    claimed = signals.claim_oldest(conn, mission_id=_mid(conn), now=NOW,
                                   freshness_bars=freshness_bars)
    assert claimed is not None
    assert claimed["id"] == fresh_4h_id  # stale な 1h 行はスキップされる

    row = conn.execute("SELECT status FROM signals WHERE id=?",
                       (stale_1h_id,)).fetchone()
    assert row["status"] == "pending"  # stale 行はまだ触れられていない


# ---------------------------------------------------------------------
# claim_oldest の freshness_bars=None はゲート無効 (stale でも claim 対象)
# ---------------------------------------------------------------------
def test_claim_oldest_freshness_none_disables_gate(tmp_path):
    conn = _conn(tmp_path)
    stale_id = _add(conn, bar_ts=datetime(2026, 8, 1, 0, 0, tzinfo=timezone.utc))
    claimed = signals.claim_oldest(conn, mission_id=_mid(conn), now=NOW,
                                   freshness_bars=None)
    assert claimed is not None
    assert claimed["id"] == stale_id


# ---------------------------------------------------------------------
# CASE 式 (_TF_MINUTES_CASE) の PLUGIN_TIMEFRAMES との同期テスト
# ---------------------------------------------------------------------
def test_tf_minutes_case_synced_with_plugin_timeframes():
    case_tfs = set(re.findall(r"WHEN '([^']+)'", signals._TF_MINUTES_CASE))
    assert case_tfs == set(PLUGIN_TIMEFRAMES)


# ---------------------------------------------------------------------
# add(): timeframe は PLUGIN_TIMEFRAMES 限定 (CASE が NULL を返す組み合わせ
# を db に入れさせない — 入ってしまうと鮮度判定不能な不死身 pending になる)
# ---------------------------------------------------------------------
def test_add_rejects_timeframe_outside_plugin_timeframes(tmp_path):
    conn = _conn(tmp_path)
    with pytest.raises(ValueError, match="timeframe"):
        signals.add(conn, plugin="p.py", content_hash="h", pair="USDJPY",
                    timeframe="5m", bar_ts=_iso(NOW), kind="signal",
                    payload={}, now=NOW)


# ---------------------------------------------------------------------
# pending_exists / recent
# ---------------------------------------------------------------------
def test_pending_exists(tmp_path):
    conn = _conn(tmp_path)
    assert signals.pending_exists(conn) is False
    _add(conn, bar_ts=NOW)
    assert signals.pending_exists(conn) is True


def test_recent_filters_by_pair_and_bar_ts_since(tmp_path):
    """recent() は bar_ts を鮮度軸に使う (claim/freshness/staleness と同じ
    時間軸)。created_at (格納時刻) が新しくても bar_ts が古ければ「recent」
    には含めない — 取引 loop に古いバー由来のシグナルを「最近」として
    見せない (advisor 判断)。"""
    conn = _conn(tmp_path)
    old_bar = _add(conn, bar_ts=datetime(2026, 8, 1, 0, 0, tzinfo=timezone.utc),
                   content_hash="old", pair="USDJPY", now=NOW)
    new_bar = _add(conn, bar_ts=datetime(2026, 8, 3, 11, 0, tzinfo=timezone.utc),
                   content_hash="new", pair="USDJPY", now=NOW)
    other_pair = _add(conn, bar_ts=datetime(2026, 8, 3, 11, 0,
                                            tzinfo=timezone.utc),
                      content_hash="other", pair="EURUSD", now=NOW)

    since = datetime(2026, 8, 2, 0, 0, tzinfo=timezone.utc)
    rows = signals.recent(conn, "USDJPY", since=since)
    ids = {r["id"] for r in rows}
    assert ids == {new_bar}
    assert old_bar not in ids
    assert other_pair not in ids


# ---------------------------------------------------------------------
# fix round 1 F1 (codex/sonnet 一致 Important): add() が bar_ts を検証
# せず、不正値が不死身 pending になる。
# ---------------------------------------------------------------------
def test_add_rejects_unparseable_bar_ts(tmp_path):
    conn = _conn(tmp_path)
    with pytest.raises(ValueError, match="bar_ts is not a valid"):
        signals.add(conn, plugin="p.py", content_hash="h", pair="USDJPY",
                    timeframe="1h", bar_ts="not-a-date", kind="signal",
                    payload={}, now=NOW)
    assert conn.execute(
        "SELECT COUNT(*) c FROM signals").fetchone()["c"] == 0


def test_add_rejects_naive_bar_ts(tmp_path):
    """F2: naive な bar_ts は SQLite が黙って UTC 扱いし、時差分ズレて
    鮮度・lease 判定が誤動作する — F1 と同じ関数で拒否する。"""
    conn = _conn(tmp_path)
    naive_bar_ts = datetime(2026, 8, 3, 12, 0).isoformat()  # tz 無し
    with pytest.raises(ValueError, match="bar_ts is naive"):
        signals.add(conn, plugin="p.py", content_hash="h", pair="USDJPY",
                    timeframe="1h", bar_ts=naive_bar_ts, kind="signal",
                    payload={}, now=NOW)
    assert conn.execute(
        "SELECT COUNT(*) c FROM signals").fetchone()["c"] == 0


# ---------------------------------------------------------------------
# fix round 1 F2 (codex Important): naive now/since が SQLite に黙って
# UTC として解釈され、鮮度/lease 判定が時差分ズレる。SQL 内で日時比較を
# 行う公開関数の入口ですべて拒否することをピンする。
#
# Task 13 注記: 以下の claim_oldest(mission_id=1, now=NAIVE_NOW, ...) は
# _require_aware(now, ...) が UPDATE 文の実行前に例外を送出するため、
# claimed_by_mission_id への書き込みは一切発生しない。よってダミー整数 1
# のままで FK の影響を受けず、変更不要。
# ---------------------------------------------------------------------
NAIVE_NOW = datetime(2026, 8, 3, 12, 0)  # tz 無し


def test_naive_now_rejected_across_sql_comparison_functions(tmp_path):
    conn = _conn(tmp_path)

    with pytest.raises(ValueError, match="now is naive"):
        signals.add(conn, plugin="p.py", content_hash="h", pair="USDJPY",
                    timeframe="1h", bar_ts=_iso(NOW), kind="signal",
                    payload={}, now=NAIVE_NOW)
    with pytest.raises(ValueError, match="now is naive"):
        signals.claim_oldest(conn, mission_id=1, now=NAIVE_NOW,
                             freshness_bars=None)
    with pytest.raises(ValueError, match="now is naive"):
        signals.expire_stale(conn, now=NAIVE_NOW, freshness_bars=2)
    with pytest.raises(ValueError, match="now is naive"):
        signals.reclaim_expired(conn, now=NAIVE_NOW, lease_min=15,
                                max_requeue=2)
    with pytest.raises(ValueError, match="since is naive"):
        signals.recent(conn, "USDJPY", since=NAIVE_NOW)

    # どの呼び出しも DB に副作用を残していない (fail closed)
    assert conn.execute(
        "SELECT COUNT(*) c FROM signals").fetchone()["c"] == 0


# ---------------------------------------------------------------------
# fix round 1 F3 (sonnet 変異生存): reclaim_expired の status='claimed'
# フィルタを外す変異が既存テスト全 green で生存した。consume() は
# claimed_at をクリアしない設計 (監査情報として残す) のため、consumed 行
# の claimed_at を backdate しても reclaim_expired が触れないことを直接
# ピンする (consume() の動作は変更しない)。
# ---------------------------------------------------------------------
def test_reclaim_expired_does_not_touch_consumed_rows(tmp_path):
    conn = _conn(tmp_path)
    sid = _add(conn, bar_ts=NOW)
    m1 = _mid(conn)
    signals.claim_oldest(conn, mission_id=m1, now=NOW, freshness_bars=None)
    signals.consume(conn, sid, mission_id=m1, now=NOW)

    old_claimed_at = NOW - timedelta(hours=5)  # lease (15min) を大きく超過
    conn.execute("UPDATE signals SET claimed_at=? WHERE id=?",
                (old_claimed_at.isoformat(), sid))
    conn.commit()

    result = signals.reclaim_expired(conn, now=NOW, lease_min=15,
                                     max_requeue=2)
    assert result == []  # consumed 行は対象外なので何も回収されない

    row = conn.execute(
        "SELECT status, claimed_by_mission_id, requeue_count "
        "FROM signals WHERE id=?", (sid,)).fetchone()
    assert row["status"] == "consumed"  # pending に戻っていない
    assert row["claimed_by_mission_id"] == m1  # 監査情報は残る (仕様どおり)
    assert row["requeue_count"] == 0


# ---------------------------------------------------------------------
# fix round 2 F5 (再レビュー変異生存): _validate_bar_ts の層 3 (SQLite
# 受理性チェック) を削除する変異が既存 24 テスト全 green で生存した。
# "+0000" (コロン無しオフセット) は Python fromisoformat は tz-aware
# として受理する (層1・層2 を通過) が、SQLite の datetime() は NULL を
# 返す — 層 3 だけが防ぐ実在の入力。
# ---------------------------------------------------------------------
def test_add_rejects_bar_ts_python_parseable_but_sqlite_null(tmp_path):
    conn = _conn(tmp_path)
    bar_ts = "2026-08-03T12:00:00+0000"  # tz-aware だが SQLite が NULL を返す形式
    with pytest.raises(ValueError, match="not accepted by SQLite"):
        signals.add(conn, plugin="p.py", content_hash="h", pair="USDJPY",
                    timeframe="1h", bar_ts=bar_ts, kind="signal",
                    payload={}, now=NOW)
    assert conn.execute(
        "SELECT COUNT(*) c FROM signals").fetchone()["c"] == 0
```

**3-b. `tests/loops/test_trade_loop_phases.py` を編集する。**

import 行 (11 行目) を:

```python
from agentic_fx.store import orders, signals
```

から以下に変更する:

```python
from agentic_fx.store import missions, orders, signals
```

`test_requeue_signal_does_not_send_notification_itself` の本体を以下に置き換える (既存の docstring は変更しない):

```python
def test_requeue_signal_does_not_send_notification_itself(tmp_path):
    """codex A1 (レビュー 1 周目・最重要): `_requeue_signal` 自体は通知を
    送らず、文面を返すだけであることを直接確認する。Notifier.send は
    urlopen(timeout=10) の同期実行なので、lock 保持中に呼ぶと SL/TP 監視が
    最大 10 秒止まる (`_requeue_signal` は core_lock 保持中に呼ばれる)。"""
    conn, loop, runner, tp = _loop(tmp_path, [])
    recording = _RecordingNotifier()
    loop.notifier = recording

    max_requeue = SETTINGS.plugin.signal_requeue_max
    sid = signals.add(
        conn, plugin="sig1", content_hash="h1", pair="USDJPY",
        timeframe="1h", bar_ts=NOW.isoformat(), kind="signal",
        payload={"direction": "long", "strength": 0.7, "rationale": "up"},
        now=NOW)
    # Task 13: claimed_by_mission_id に missions(id) への FK が付くため、
    # 実在する mission 行が必要 (以前はダミー整数 1 だった)。
    mid = missions.start(conn, "trade", "local", "m", NOW)
    for _ in range(max_requeue):
        claimed = signals.claim_oldest(conn, mission_id=mid, now=NOW,
                                       freshness_bars=None)
        assert claimed is not None, "claim できなかった (テスト前提が崩れている)"
        signals.requeue(conn, sid, now=NOW, max_requeue=max_requeue)
    claimed = signals.claim_oldest(conn, mission_id=mid, now=NOW,
                                   freshness_bars=None)
    assert claimed is not None

    msg = loop._requeue_signal(claimed)

    assert msg is not None and "abandoned" in msg
    assert recording.sent == [], (
        "_requeue_signal 自体は通知を送ってはならない (呼び出し元が"
        "lock 解放後に送る)")
```

**3-c. `tests/test_service_app.py` を編集する。**

`test_f1c_startup_reclaim_recovers_claimed_signal` の本体を以下に置き換える:

```python
def test_f1c_startup_reclaim_recovers_claimed_signal(tmp_path):
    """F1(c): 停止時に claimed のまま残った signal 行が、次の build_app
    (= 次回起動) 直後、tick を待たずに pending へ回収されること。"""
    from agentic_fx.store import missions as missions_module
    from agentic_fx.store import signals as signals_store

    _init(tmp_path)
    app1 = build_app(tmp_path, runner=FakeRunner([]), clock=FixedClock(NOW),
                     embedding_fn=FakeEmbedding())
    sid = signals_store.add(
        app1.conn_core, plugin="sig1", content_hash="h1", pair="USDJPY",
        timeframe="1h", bar_ts=(NOW - timedelta(hours=1)).isoformat(),
        kind="signal", payload={"direction": "long"}, now=NOW)
    # Task 13: claimed_by_mission_id に missions(id) への FK が付くため、
    # 実在する mission 行が必要 (以前はダミー整数 999 だった)。
    mid = missions_module.start(app1.conn_core, "trade", "local", "m", NOW)
    lease_min = app1.settings.plugin.signal_lease_min
    old = NOW - timedelta(minutes=lease_min + 5)
    claimed = signals_store.claim_oldest(app1.conn_core, mission_id=mid,
                                         now=old, freshness_bars=None)
    assert claimed is not None and claimed["id"] == sid  # 前提

    # 起動時 recover_interrupted が claim を先に戻さないよう mission を終端化。
    # これにより次回起動で pending 化する唯一の主体が lease 回収になる。
    # ⚠️ `missions.finish` は 6 引数必須 (conn, mission_id, status,
    #    output, transcript, now)。既定値は無い (`store/missions.py:23-24`)。
    #    4 引数だと NOW が output に束縛され TypeError で落ちる (3 周目レビュー)。
    missions_module.finish(app1.conn_core, mid, "completed", None, [], NOW)

    # FC-2 (プラン8): instance_lock (flock) は App の全寿命で保持される
    # ため、同一 root への 2 回目の build_app は 1 回目の instance_lock を
    # 解放してからでないと InstanceAlreadyRunning になる。「再起動」を
    # 模す以上、1 回目のプロセスが終了して lock を手放したことも模す
    # 必要がある (App.close() への instance_lock 配線は Task 19)。
    app1.instance_lock.close()

    # 「再起動」を模して同じ DB に対しもう一度 build_app する
    app2 = build_app(tmp_path, runner=FakeRunner([]), clock=FixedClock(NOW),
                     embedding_fn=FakeEmbedding())

    row = app2.conn_core.execute(
        "SELECT status FROM signals WHERE id=?", (sid,)).fetchone()
    assert row["status"] == "pending"  # 起動時 reclaim が回収した
```

（このファイル中のコメント「App.close() への instance_lock 配線は Task 19」は**プラン8時代の別の Task 19 を指す古い注記**であり、本プラン9の Task 19 とは無関係。変更しない。）

- [ ] **Step 4: 修理した 3 ファイルを対象にスイートを実行し、まだ全 green であることを確認する (FK はまだ追加していないので、ここで壊れているとしたら前提修理自体のミス)**

Run: `uv run pytest tests/store/test_signals.py tests/loops/test_trade_loop_phases.py tests/test_service_app.py -q`

Expected: PASS (全件)

- [ ] **Step 5: FK を `_SCHEMA` に追加する (`src/agentic_fx/store/db.py`)**

`_OHLCV_V2_DDL` の定義ブロック (11-21 行目) を以下に置き換える (`_SIGNALS_V2_DDL` を新設し、`_SCHEMA` の先頭連結を維持する):

```python
_OHLCV_V2_DDL = """
CREATE TABLE IF NOT EXISTS ohlcv (
  symbol TEXT NOT NULL, interval TEXT NOT NULL, bar_time TEXT NOT NULL,
  open REAL NOT NULL, high REAL NOT NULL, low REAL NOT NULL,
  close REAL NOT NULL, volume REAL NOT NULL DEFAULT 0,
  source TEXT NOT NULL DEFAULT 'yfinance', spread REAL,
  PRIMARY KEY (symbol, interval, bar_time, source)
);
"""

# Task 13 (プラン9 束D、設計書 D5): signals.claimed_by_mission_id に
# missions(id) への FK を追加する。既存 DB では table rebuild が要るため
# (SQLite は ADD CONSTRAINT を持たない)、DDL を _migrate_signals_fk からも
# 再利用できるよう定数として切り出す (_OHLCV_V2_DDL と同型)。
_SIGNALS_V2_DDL = """
CREATE TABLE IF NOT EXISTS signals (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  plugin TEXT NOT NULL, content_hash TEXT NOT NULL,
  pair TEXT NOT NULL, timeframe TEXT NOT NULL, bar_ts TEXT NOT NULL,
  kind TEXT NOT NULL, payload_json TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending'
    CHECK(status IN ('pending','claimed','consumed','abandoned')),
  claimed_by_mission_id INTEGER REFERENCES missions(id), claimed_at TEXT,
  requeue_count INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  UNIQUE(plugin, content_hash, pair, timeframe, bar_ts)
);
"""

_SCHEMA = _OHLCV_V2_DDL + """
```

次に、`_SCHEMA` 末尾の旧 `signals` インライン定義 (131-143 行目) を、新定数への連結に置き換える:

```python
CREATE TABLE IF NOT EXISTS signals (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  plugin TEXT NOT NULL, content_hash TEXT NOT NULL,
  pair TEXT NOT NULL, timeframe TEXT NOT NULL, bar_ts TEXT NOT NULL,
  kind TEXT NOT NULL, payload_json TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending'
    CHECK(status IN ('pending','claimed','consumed','abandoned')),
  claimed_by_mission_id INTEGER, claimed_at TEXT,
  requeue_count INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  UNIQUE(plugin, content_hash, pair, timeframe, bar_ts)
);
"""
```

を以下に置き換える:

```python
""" + _SIGNALS_V2_DDL
```

（`TABLE_NAMES` (145-150 行目) は変更しない — `signals` という名前自体は変わらないため。）

- [ ] **Step 6: `_migrate_signals_fk` を実装し `init_db()` に配線する (`src/agentic_fx/store/db.py`)**

`init_db()` 関数全体 (ファイル末尾、Step 5 適用後もこの関数自体は無変更のまま存在する) を以下に置き換える (直前に `_migrate_signals_fk` を新設し、`init_db` 内で呼び出す):

```python
def _migrate_signals_fk(conn: sqlite3.Connection) -> None:
    """signals.claimed_by_mission_id に missions(id) への FK を追加する
    table rebuild (設計書 §12 裁定 2026-08-11 / D5、プラン9 Task 13)。

    SQLite は既存テーブルへの ADD CONSTRAINT を持たないため rebuild が要る。
    移行前に残りうる宙吊り行 (参照先 missions 行が既に無い
    claimed_by_mission_id) を status ごとに修復してからコピーする —
    修復無しでコピーすると FK 違反になる。

    status ごとの修復規則 (D5):
    - claimed かつ宙吊り: status='pending' + claimed_by_mission_id=NULL +
      claimed_at=NULL (lease 回収と同じ扱いに戻す)
    - consumed/abandoned かつ宙吊り: claimed_by_mission_id=NULL のみ
      (**終端状態を蘇らせない** — status は変えない)
    - pending: 元々 claimed_by_mission_id は NULL のため対象外

    **PRAGMA foreign_keys は BEGIN の外側でトグルする**。SQLite は
    トランザクション開始後の `PRAGMA foreign_keys` 変更を無視する (実測
    確認済み — BEGIN 後に発行した OFF は次の PRAGMA 読み出しでも ON の
    ままになる)。OFF にした後は成功・失敗を問わず finally で必ず ON に
    戻す — 戻し忘れると以降のプロセス全体で FK 保護が無効になる。

    修復後は `PRAGMA foreign_key_check(signals)` が空であることを検査して
    からコミットする (D5 逐語)。空でなければ repair ロジックの不備であり、
    黙って進めず例外にする。

    **FK 再構築の警告:** `signals` は現時点で参照元が無いため RENAME 先行が
    動くにすぎない。参照される側を再構築するときは「新名 CREATE → コピー →
    旧表 DROP → 新表を本来名へ RENAME」の順序を使う (Task 17 を参照)。
    """
    fk_present = any(
        fk["table"] == "missions" and fk["from"] == "claimed_by_mission_id"
        for fk in conn.execute("PRAGMA foreign_key_list(signals)"))
    v1_exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' "
        "AND name='signals_v1'").fetchone() is not None
    if fk_present and not v1_exists:
        return  # 既に移行済み

    conn.execute("PRAGMA foreign_keys=OFF")
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            # F2 型の再検査: ロック取得までの間に別接続が移行を完了させて
            # いないか確認する。
            fk_present2 = any(
                fk["table"] == "missions"
                and fk["from"] == "claimed_by_mission_id"
                for fk in conn.execute("PRAGMA foreign_key_list(signals)"))
            v1_exists2 = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='signals_v1'").fetchone() is not None
            if fk_present2 and not v1_exists2:
                conn.commit()
                return

            if not v1_exists2:
                conn.execute("ALTER TABLE signals RENAME TO signals_v1")
            conn.execute(_SIGNALS_V2_DDL)

            conn.execute(
                "INSERT OR IGNORE INTO signals "
                "(id, plugin, content_hash, pair, timeframe, bar_ts, kind, "
                "payload_json, status, claimed_by_mission_id, claimed_at, "
                "requeue_count, created_at) "
                "SELECT id, plugin, content_hash, pair, timeframe, bar_ts, "
                "kind, payload_json, "
                "CASE WHEN status='claimed' AND claimed_by_mission_id "
                "IS NOT NULL AND claimed_by_mission_id NOT IN "
                "(SELECT id FROM missions) THEN 'pending' ELSE status END, "
                "CASE WHEN claimed_by_mission_id IS NOT NULL "
                "AND claimed_by_mission_id NOT IN (SELECT id FROM missions) "
                "THEN NULL ELSE claimed_by_mission_id END, "
                "CASE WHEN status='claimed' AND claimed_by_mission_id "
                "IS NOT NULL AND claimed_by_mission_id NOT IN "
                "(SELECT id FROM missions) THEN NULL ELSE claimed_at END, "
                "requeue_count, created_at FROM signals_v1")

            copied = conn.execute(
                "SELECT COUNT(*) c FROM signals").fetchone()["c"]
            original = conn.execute(
                "SELECT COUNT(*) c FROM signals_v1").fetchone()["c"]
            if copied != original:
                raise RuntimeError(
                    "signals migration: 行数が一致しません "
                    f"(signals_v1={original}, signals={copied})。"
                    "OR IGNORE が想定外の重複と衝突した可能性があります。")

            violations = conn.execute(
                "PRAGMA foreign_key_check(signals)").fetchall()
            if violations:
                raise RuntimeError(
                    "signals migration: 修復後も FK 違反が残っています "
                    f"({len(violations)} 行): "
                    f"{[dict(v) for v in violations]}")

            conn.execute("DROP TABLE signals_v1")
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
    finally:
        conn.execute("PRAGMA foreign_keys=ON")


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(ohlcv)")}
    v1_leftover = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' "
        "AND name='ohlcv_v1'").fetchone() is not None
    if "source" not in cols or v1_leftover:
        _migrate_ohlcv_v2(conn)
    _ensure_column(conn, "missions", "trigger", "trigger TEXT")
    _migrate_signals_fk(conn)
    conn.commit()
```

- [ ] **Step 7: `find . -name __pycache__ ... -exec rm -rf {} +` を実行してから、束 D 関連 + 全体テストを実行する**

Run:
```bash
find . -name __pycache__ -type d -not -path "./.venv/*" -exec rm -rf {} +
uv run pytest tests/store/test_db.py tests/store/test_signals.py tests/store/test_missions_cas.py tests/loops/test_trade_loop_phases.py tests/test_service_app.py -q
```

Expected: PASS (全件)

- [ ] **Step 8: `uv run pytest -q` で全スイートを実行し、1726 件から減っていないことを確認する**

Run: `find . -name __pycache__ -type d -not -path "./.venv/*" -exec rm -rf {} + && uv run pytest -q`

Expected: 既存の全テスト PASS (Task 13 追加分・修理分を含め件数が増加、減少なし)

- [ ] **Step 9: コミット**

```bash
git add src/agentic_fx/store/db.py tests/store/test_db.py tests/store/test_signals.py tests/loops/test_trade_loop_phases.py tests/test_service_app.py
git commit -m "$(cat <<'EOF'
feat: signals.claimed_by_mission_id に missions(id) への FK を追加する

存在しない mission id での claim という配線不整合を書き込み時に検出
できるようにする (設計書 §12 裁定、D5)。既存 DB の宙吊り行は status ごと
に異なる修復を当ててから rebuild する: claimed は pending へ戻す
(lease 回収と同じ扱い)、consumed/abandoned は claimed_by_mission_id の
みクリアして終端状態は蘇らせない。

FK 追加に伴い、ダミーの mission_id リテラルで claim_oldest を呼んでいた
3 つの既存テストファイル (test_signals.py / test_trade_loop_phases.py /
test_service_app.py) を実在する missions 行を使う形に修理した。

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

#### Task 13 変異ノート

`uv run pytest tests/store/test_db.py -q` の前後で `__pycache__` を必ず削除する。各変異は 1 つずつ独立に当て、意図した行を `grep -n` で確認してから実行する。

| # | 変異 (設計書 D5 の変異リスト由来) | 殺すテスト |
|---|---|---|
| 1 | FK を付けない (`_SIGNALS_V2_DDL` から `REFERENCES missions(id)` を削る) | `test_init_db_fresh_signals_table_has_missions_fk`・`test_signals_fk_rejects_nonexistent_mission_id_after_migration` |
| 2 | `claimed` の修復を落とす (INSERT..SELECT の CASE 式から claimed 分岐を削り、dangling 行をそのままコピーする) | `test_migrate_signals_fk_repairs_dangling_claimed_row` (repair されない値で assert が落ちる、または `foreign_key_check` が violations を検出して RuntimeError になり、いずれにせよテストが red になる) |
| 3 | `consumed`/`abandoned` を pending に戻す (CASE の ELSE を 'pending' にする等) | `test_migrate_signals_fk_repairs_dangling_consumed_row_without_reviving`・`test_migrate_signals_fk_repairs_dangling_abandoned_row_without_reviving` |
| 4 | `foreign_key_check` を削除する (violations チェックのブロックを消す) | **❌ 殺せない — killer テストは存在しない (codex 1 周目 Critical 3)。** `test_migrate_signals_fk_leaves_foreign_key_check_clean` は「移行後に violations が空である」という**契約**のピンであって、**検査コードの存在**のピンではない。#2 の repair が正しく効いていれば violations は元々空なので、検査ブロックを丸ごと消しても**どのテストも red にならない**。**mutation ledger には「既存テストでは殺せない」と明記し、削除しないことをレビューで明示的に確認する**。どうしても動的に殺したいなら「repair 後・検査前に violation を注入するテスト専用 seam」を足す必要があるが、**最終防波堤のためだけに production へ seam を足す判断は実装者に委ねる** (足すなら理由を報告すること) |
| 5 (追加) | `PRAGMA foreign_keys=OFF` を `BEGIN IMMEDIATE` の後に移動する | `test_migrate_signals_fk_disables_foreign_keys_before_begin` が実行順を直接観測して red。repair SQL はコピー時に dangling FK を NULL 化するため、repair 系だけではこの順序を pin できない |
| 6 (追加) | `finally: conn.execute("PRAGMA foreign_keys=ON")` を削除する | `test_migrate_signals_fk_restores_foreign_keys_pragma_after_failure` |
| 7 (追加) | INSERT..SELECT の列リストから `id` を落とす (AUTOINCREMENT に任せて renumber してしまう) | `test_signals_migration_preserves_ids_and_autoincrement_sequence` |
| 8 (追加) | `_SIGNALS_V2_DDL` から `UNIQUE(...)` を落とす | `test_signals_migration_preserves_unique_constraint` |
| 9 (追加) | `_SIGNALS_V2_DDL` から `CHECK(status IN (...))` を落とす | `test_signals_migration_preserves_status_check` |

**#5〜9 は設計書 D5 の変異リストに無い追加分。** #5 はアドバイザーレビューで発見した実装上の landmine (`PRAGMA foreign_keys` はトランザクション内で無視される) の直接ピン、#6 は「失敗時に FK 保護を無効なまま残さない」という新規の防御、#7〜9 は「UNIQUE 制約と既存インデックスを保つこと」という親プランの受入条件の直接ピン (D5 の変異リストには挙がっていないが親プランの受入条件に明記されている)。

---

### Task 19: `improvement_runs` の PR 列 migration

**設計の正**: 設計書 D7 (`2026-08-11-phase2-9-foundation-design.md:334-342`)、本体設計書 §12 (`2026-07-25-agentic-fx-design.md:802`)

**前提**: `store/db.py` は Task 13 の変更 (`_SIGNALS_V2_DDL` 定数・`_migrate_signals_fk`・`init_db()` への配線) が既に適用されている状態から着手する (「束 D 内の実装順序」節参照)。

**Files:**
- Modify: `src/agentic_fx/store/db.py` (`_IMPROVEMENT_RUNS_V2_DDL` 定数の追加・`_SCHEMA` の書き換え・`_migrate_improvement_runs_v2` の新設・`init_db()` への配線)
- Modify: `src/agentic_fx/store/improve_runs.py` (`finish()` から `pr_url` 引数を削除)
- Test: `tests/store/test_db.py` (migration の新規テスト群)
- Test: `tests/store/test_backlog.py` (`finish()` シグネチャ変更のピン)

**Interfaces:**
- Produces: `db_module._migrate_improvement_runs_v2(conn: sqlite3.Connection) -> None` (private)
- 変更後の公開シグネチャ: `improve_runs.finish(conn, run_id, *, result, now, approval_id=None, report_path=None) -> None` (**`pr_url` 引数を削除** — 親プラン `2026-08-11-phase2-9-foundation.md` のファイル表は `store/db.py` のみを Task 19 の対象として記載しているが、`store/improve_runs.py` の `finish()` は `pr_url` 列に書き込んでおり、列を落とすと SQL がそのままでは動かなくなる。**これはプラン記述側の欠落であり、本 task の対象に含める** (CLAUDE.md 「欠陥はプラン記述側にある」原則)

`tests/store/test_backlog.py:17` の既存呼び出し `improve_runs.finish(c, rid, result="report", now=NOW, report_path="reports/improve-2026-07-26.md")` は `pr_url` を渡していないため、この変更で**壊れない**。

- [ ] **Step 1: `store/db.py` の migration テストを書く (`tests/store/test_db.py`)**

ファイル末尾に以下を追記する:

```python
def _legacy_improvement_runs_ddl() -> str:
    """pr_url 列を持つ旧 (Task 19 以前) improvement_runs DDL。"""
    return (
        "CREATE TABLE improvement_runs ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "backlog_id INTEGER REFERENCES improvement_backlog(id),"
        "result TEXT,"
        "pr_url TEXT, approval_id INTEGER, report_path TEXT,"
        "started_at TEXT NOT NULL, finished_at TEXT)")


def test_init_db_migrates_legacy_improvement_runs_drops_pr_url(tmp_path):
    """旧 PR 経路の名残 (pr_url 列) が落ち、result の値域が CHECK で
    固定される (D7)。"""
    conn = connect(tmp_path / "legacy.db")
    conn.execute(_legacy_improvement_runs_ddl())
    conn.execute(
        "INSERT INTO improvement_runs (result, report_path, started_at, "
        "finished_at) VALUES ('report', 'reports/x.md', "
        "'2026-08-11T09:00:00+00:00', '2026-08-11T09:05:00+00:00')")
    conn.commit()

    init_db(conn)

    cols = {r["name"] for r in
           conn.execute("PRAGMA table_info(improvement_runs)")}
    assert "pr_url" not in cols
    row = conn.execute(
        "SELECT result, report_path FROM improvement_runs").fetchone()
    assert row["result"] == "report"
    assert row["report_path"] == "reports/x.md"


def test_improvement_runs_migration_is_idempotent(tmp_path):
    conn = connect(tmp_path / "legacy.db")
    conn.execute(_legacy_improvement_runs_ddl())
    conn.execute(
        "INSERT INTO improvement_runs (result, started_at) VALUES "
        "('report', '2026-08-11T09:00:00+00:00')")
    conn.commit()

    init_db(conn)
    init_db(conn)  # 2 回目でも例外なし

    names = {r["name"] for r in
             conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "improvement_runs_v1" not in names
    assert conn.execute(
        "SELECT COUNT(*) c FROM improvement_runs").fetchone()["c"] == 1


def test_improvement_runs_migration_aborts_on_legacy_pr_result_with_null_pr_url(
        tmp_path):
    """codex I6: result='pr', pr_url=NULL は現スキーマで合法 (result に
    CHECK が無いため) — pr_url だけ見るガードはこの行を見逃す。result 側
    の検査が要ることの直接ピン。"""
    conn = connect(tmp_path / "legacy.db")
    conn.execute(_legacy_improvement_runs_ddl())
    conn.execute(
        "INSERT INTO improvement_runs (result, pr_url, started_at) "
        "VALUES ('pr', NULL, '2026-08-11T09:00:00+00:00')")
    conn.commit()

    with pytest.raises(RuntimeError, match="旧 PR 経路"):
        init_db(conn)

    # 中断時は旧テーブルのまま (pr_url 列が残っている) — 部分適用しない
    cols = {r["name"] for r in
           conn.execute("PRAGMA table_info(improvement_runs)")}
    assert "pr_url" in cols


def test_improvement_runs_migration_aborts_on_nonnull_pr_url_with_other_result(
        tmp_path):
    """result が 'pr' でなくても pr_url が非NULLなら中断する
    (result='pr' OR pr_url IS NOT NULL の OR のもう半分)。"""
    conn = connect(tmp_path / "legacy.db")
    conn.execute(_legacy_improvement_runs_ddl())
    conn.execute(
        "INSERT INTO improvement_runs (result, pr_url, started_at) "
        "VALUES ('report', 'https://example/pr/1', "
        "'2026-08-11T09:00:00+00:00')")
    conn.commit()

    with pytest.raises(RuntimeError, match="旧 PR 経路"):
        init_db(conn)


def test_improvement_runs_migration_error_names_offending_row_ids(tmp_path):
    """「黙って捨てない」は人間が該当行を見つけられて初めて意味を持つ —
    エラーメッセージに対象 id を含めることを直接ピンする。"""
    conn = connect(tmp_path / "legacy.db")
    conn.execute(_legacy_improvement_runs_ddl())
    conn.execute(
        "INSERT INTO improvement_runs (result, started_at) VALUES "
        "('pr', '2026-08-11T09:00:00+00:00')")
    conn.commit()
    bad_id = conn.execute(
        "SELECT id FROM improvement_runs").fetchone()["id"]

    with pytest.raises(RuntimeError, match=f"id={bad_id}"):
        init_db(conn)


def test_improvement_runs_check_rejects_invalid_result_after_migration(
        tmp_path):
    conn = connect(tmp_path / "fresh.db")
    init_db(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO improvement_runs (result, started_at) VALUES "
            "('pr', '2026-08-11T09:00:00+00:00')")


def test_improvement_runs_check_allows_null_result_for_unfinished_run(
        tmp_path):
    """CHECK(result IN (...)) は NULL を拒否しない (SQLite の CHECK は
    NULL に対して常に通過する) — start() 直後 (未 finish) の行が新スキーマ
    でも作れることのピン。NOT NULL や `result IS NOT NULL` を書き足す
    誤実装だけがこれを壊す。"""
    from agentic_fx.store import improve_runs

    conn = connect(tmp_path / "fresh.db")
    init_db(conn)
    rid = improve_runs.start(conn, None, datetime(2026, 8, 11, 9, 0,
                                                   tzinfo=timezone.utc))
    row = conn.execute(
        "SELECT result FROM improvement_runs WHERE id=?", (rid,)).fetchone()
    assert row["result"] is None
```

- [ ] **Step 2: テストを実行し、期待どおりに落ちることを確認する**

Run: `uv run pytest tests/store/test_db.py -k improvement_runs -v`

Expected: 新制約・guard を直接要求するテストは FAIL。変更前から成立する保存系・
冪等性の回帰 pin は green でよく、「全件 FAIL」は要求しない。

- [ ] **Step 3: `improve_runs.finish()` から `pr_url` を削除するテストを書く (`tests/store/test_backlog.py`)**

`tests/store/test_backlog.py` の import に `import pytest` を追加し、ファイル末尾に
以下を追記する:

```python
def test_finish_no_longer_accepts_pr_url(tmp_path):
    """Task 19: pr_url は improvement_runs から落ちたため finish() の
    引数からも外れている (渡すと TypeError)。"""
    c = connect(tmp_path / "t.db")
    init_db(c)
    bid = backlog.add(c, "x", "user", NOW)
    rid = improve_runs.start(c, bid, NOW)
    with pytest.raises(TypeError):
        improve_runs.finish(c, rid, result="report", now=NOW,
                            pr_url="https://example/pr/1")
```

- [ ] **Step 4: テストを実行し、落ちることを確認する**

Run: `uv run pytest tests/store/test_backlog.py::test_finish_no_longer_accepts_pr_url -v`

Expected: FAIL (現行の `finish()` は `pr_url` を受理してしまうため、`pytest.raises(TypeError)` の中で例外が起きず `Failed: DID NOT RAISE`)

- [ ] **Step 5: `_IMPROVEMENT_RUNS_V2_DDL` 定数を追加し `_SCHEMA` を書き換える (`src/agentic_fx/store/db.py`)**

> **FK 再構築の警告:** `improvement_runs` は現時点で参照元が無いので RENAME
> 先行が成立する。参照される側では「新名 CREATE → コピー →旧表 DROP →
> 新表を本来名へ RENAME」を使う。Task 17 の `trade_intents` が実例である。

（Task 13 の Step 5 で新設された）`_SIGNALS_V2_DDL` の定義ブロックの直後、`_SCHEMA = _OHLCV_V2_DDL + """` の直前に以下を挿入する:

```python
_SIGNALS_V2_DDL = """
CREATE TABLE IF NOT EXISTS signals (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  plugin TEXT NOT NULL, content_hash TEXT NOT NULL,
  pair TEXT NOT NULL, timeframe TEXT NOT NULL, bar_ts TEXT NOT NULL,
  kind TEXT NOT NULL, payload_json TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending'
    CHECK(status IN ('pending','claimed','consumed','abandoned')),
  claimed_by_mission_id INTEGER REFERENCES missions(id), claimed_at TEXT,
  requeue_count INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  UNIQUE(plugin, content_hash, pair, timeframe, bar_ts)
);
"""

# Task 19 (プラン9 束D、設計書 D7): improvement_runs から pr_url を落とし
# result を approval|report に限定する (旧 PR 経路の廃止、設計書改訂17)。
# 理由は _SIGNALS_V2_DDL と同じ — rebuild からも再利用するため定数化する。
_IMPROVEMENT_RUNS_V2_DDL = """
CREATE TABLE IF NOT EXISTS improvement_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  backlog_id INTEGER REFERENCES improvement_backlog(id),
  result TEXT CHECK (result IN ('approval','report')),
  approval_id INTEGER, report_path TEXT,
  started_at TEXT NOT NULL, finished_at TEXT
);
"""

_SCHEMA = _OHLCV_V2_DDL + """
```

（上記コードブロックの `_SIGNALS_V2_DDL` 部分は Task 13 で既に存在する — この Step では `_IMPROVEMENT_RUNS_V2_DDL` の追加と、その前後の文脈確認のために全体を示している。実際の Edit は `_SIGNALS_V2_DDL` ブロックの直後に `_IMPROVEMENT_RUNS_V2_DDL` を挿入するだけでよい。）

次に `_SCHEMA` 中の旧 `improvement_runs` インライン定義を置き換える。現在の該当箇所:

```python
CREATE TABLE IF NOT EXISTS improvement_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  backlog_id INTEGER REFERENCES improvement_backlog(id),
  result TEXT,                   -- pr | approval | report
  pr_url TEXT, approval_id INTEGER, report_path TEXT,
  started_at TEXT NOT NULL, finished_at TEXT
);
```

を以下に置き換える:

```python
""" + _IMPROVEMENT_RUNS_V2_DDL + """
```

- [ ] **Step 6: `_migrate_improvement_runs_v2` を実装し `init_db()` に配線する (`src/agentic_fx/store/db.py`)**

`init_db()` 関数全体 (Task 13 の Step 6 で `_migrate_signals_fk` 呼び出しが既に入っている状態) を以下に置き換える (直前に `_migrate_improvement_runs_v2` を新設し、`init_db` 内で呼び出す):

```python
def _migrate_improvement_runs_v2(conn: sqlite3.Connection) -> None:
    """improvement_runs から pr_url 列を落とし、result に
    CHECK (approval|report) を追加する table rebuild (設計書 §12 / D7、
    プラン9 Task 19)。

    旧 PR 経路の廃止 (設計書改訂17) により result='pr' / pr_url は死んだ
    概念になった。**移行前に result='pr' OR pr_url IS NOT NULL の行が
    1 件でもあれば中断してエラーにする** — 現スキーマは result に CHECK が
    無いため `result='pr', pr_url=NULL` が合法であり、pr_url だけを見る
    ガードはこの行を見逃して不正値を新契約に持ち込む (codex I6)。
    """
    cols = {r["name"] for r in
           conn.execute("PRAGMA table_info(improvement_runs)")}
    v1_exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' "
        "AND name='improvement_runs_v1'").fetchone() is not None
    if "pr_url" not in cols and not v1_exists:
        return  # 既に移行済み

    conn.execute("BEGIN IMMEDIATE")
    try:
        cols2 = {r["name"] for r in
                conn.execute("PRAGMA table_info(improvement_runs)")}
        v1_exists2 = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='improvement_runs_v1'").fetchone() is not None
        if "pr_url" not in cols2 and not v1_exists2:
            conn.commit()
            return  # 別接続が先に移行済み

        # 移行対象データの検査は RENAME より前 — 中断時に旧テーブルへ
        # 一切触れない (黙って捨てず、対象行の id を名指しして中断する)。
        bad_rows = conn.execute(
            "SELECT id FROM improvement_runs "
            "WHERE result='pr' OR pr_url IS NOT NULL").fetchall()
        if bad_rows:
            ids = ", ".join(str(r["id"]) for r in bad_rows)
            raise RuntimeError(
                "improvement_runs migration: 旧 PR 経路の行が残っています "
                f"(result='pr' または pr_url が非NULL、id={ids})。新スキーマ"
                "はこの値域を持てません。手動で調査・退避してから再実行して"
                "ください。")

        if not v1_exists2:
            conn.execute(
                "ALTER TABLE improvement_runs RENAME TO improvement_runs_v1")
        conn.execute(_IMPROVEMENT_RUNS_V2_DDL)
        conn.execute(
            "INSERT OR IGNORE INTO improvement_runs "
            "(id, backlog_id, result, approval_id, report_path, "
            "started_at, finished_at) "
            "SELECT id, backlog_id, result, approval_id, report_path, "
            "started_at, finished_at FROM improvement_runs_v1")
        conn.execute("DROP TABLE improvement_runs_v1")
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(ohlcv)")}
    v1_leftover = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' "
        "AND name='ohlcv_v1'").fetchone() is not None
    if "source" not in cols or v1_leftover:
        _migrate_ohlcv_v2(conn)
    _ensure_column(conn, "missions", "trigger", "trigger TEXT")
    _migrate_signals_fk(conn)
    _migrate_improvement_runs_v2(conn)
    conn.commit()
```

- [ ] **Step 7: `improve_runs.py` から `pr_url` を削除する (`src/agentic_fx/store/improve_runs.py`)**

`finish()` 関数全体を以下に置き換える (`start()` は無変更):

```python
def finish(conn: sqlite3.Connection, run_id: int, *, result: str, now: datetime,
           approval_id: int | None = None,
           report_path: str | None = None) -> None:
    conn.execute(
        "UPDATE improvement_runs SET result=?, approval_id=?, "
        "report_path=?, finished_at=? WHERE id=?",
        (result, approval_id, report_path, now.isoformat(), run_id))
    conn.commit()
```

- [ ] **Step 8: `__pycache__` を削除してから、Task 19 関連 + 全体テストを実行する**

Run:
```bash
find . -name __pycache__ -type d -not -path "./.venv/*" -exec rm -rf {} +
uv run pytest tests/store/test_db.py tests/store/test_backlog.py -q
```

Expected: PASS (全件)

- [ ] **Step 9: `uv run pytest -q` で全スイートを実行し、1726 件から減っていないことを確認する**

Run: `find . -name __pycache__ -type d -not -path "./.venv/*" -exec rm -rf {} + && uv run pytest -q`

Expected: 既存の全テスト PASS (Task 19 追加分を含め件数が増加、減少なし)

- [ ] **Step 10: コミット**

```bash
git add src/agentic_fx/store/db.py src/agentic_fx/store/improve_runs.py tests/store/test_db.py tests/store/test_backlog.py
git commit -m "$(cat <<'EOF'
feat: improvement_runs から死んだ PR 経路 (pr_url/result='pr') を除去する

設計書改訂17で PR 経路が廃止済み。result の値域を CHECK
(approval|report) で固定し、pr_url 列を落とす。移行前に result='pr' OR
pr_url IS NOT NULL の行が残っていれば (result のみ 'pr' で pr_url が NULL
のケースを含め) 中断してエラーにし、想定外の値を新スキーマへ黙って
持ち込まない。improve_runs.finish() から pr_url 引数を削除する
(store/db.py のみを対象としていた親プランのファイル表の欠落を補う)。

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

#### Task 19 変異ノート

`uv run pytest tests/store/test_db.py -q` の前後で `__pycache__` を必ず削除する。各変異は 1 つずつ独立に当て、意図した行を `grep -n` で確認してから実行する。

| # | 変異 (設計書 D7 の防御・受入条件由来) | 殺すテスト |
|---|---|---|
| 1 | ガードの `WHERE result='pr' OR pr_url IS NOT NULL` から `result='pr' OR` を削り `pr_url IS NOT NULL` だけにする (codex I6 が指摘した「pr_url だけ見る」誤実装への回帰) | `test_improvement_runs_migration_aborts_on_legacy_pr_result_with_null_pr_url` |
| 2 | 同ガードから `OR pr_url IS NOT NULL` を削る | `test_improvement_runs_migration_aborts_on_nonnull_pr_url_with_other_result` |
| 3 | ガードのチェック自体 (bad_rows ブロック) を削除する | `test_improvement_runs_migration_aborts_on_legacy_pr_result_with_null_pr_url`・`test_improvement_runs_migration_aborts_on_nonnull_pr_url_with_other_result` の両方 |
| 4 | `_IMPROVEMENT_RUNS_V2_DDL` から `CHECK (result IN (...))` を削除する | `test_improvement_runs_check_rejects_invalid_result_after_migration` |
| 5 | `_IMPROVEMENT_RUNS_V2_DDL` の `result` 列に `NOT NULL` を付ける、または CHECK を `result IS NOT NULL AND result IN (...)` にする | `test_improvement_runs_check_allows_null_result_for_unfinished_run` |
| 6 | `finish()` から `pr_url` を削除する変更そのものを行わない (仕上げ忘れ) | `test_finish_no_longer_accepts_pr_url` |
| 7 (追加) | ガードのエラーメッセージから対象行 id の埋め込みを削る | `test_improvement_runs_migration_error_names_offending_row_ids` |

**#7 は設計書 D7 の変異リストに無い追加分。** advisor レビューの「エラーは人間が該当行を見つけられて初めて意味を持つ」という指摘 (「黙って捨てない」を字面ではなく実効的に満たすための追加) に基づく。

---

### 束 D 自己レビュー

#### ① 設計書 D4/D5/D7 の変異リスト対応表

**D4 (Task 12)** — 設計書 `2026-08-11-phase2-9-foundation-design.md:238` の変異リスト 5 項目:

| 設計書の変異 | 対応 Step | 対応テスト |
|---|---|---|
| reject-after-approve で取り消されること | Task 12 Step 1 | `test_approved_plugins_reject_after_approve_revokes` |
| approve-after-reject で再承認されること | Task 12 Step 1 | `test_approved_plugins_approve_after_reject_readmits` |
| expired-after-approve で取り消され *ない* こと | Task 12 Step 1 | `test_approved_plugins_expired_after_approve_does_not_revoke` (CLAUDE.md 指示で必須指定された肯定検査) |
| `ORDER BY` を落とす | Task 12 Step 1 | `test_approved_plugins_orders_by_decided_at_not_insertion_order` |
| `id` の同時刻タイブレークを落とす | Task 12 Step 1 | `test_approved_plugins_same_decided_at_ties_break_by_higher_id` (**equivalent mutant の疑いを明記済み — 実測して ledger に生死を記録すること**) |

追加 2 件 (Task 12 変異ノート #7・#8): `decided_at IS NOT NULL` フィルタ / admit 条件の反転。

**D5 (Task 13)** — 設計書 `2026-08-11-phase2-9-foundation-design.md:254` の変異リスト 4 項目:

| 設計書の変異 | 対応 Step | 対応テスト |
|---|---|---|
| FK を付けない | Task 13 Step 1 | `test_init_db_fresh_signals_table_has_missions_fk`・`test_signals_fk_rejects_nonexistent_mission_id_after_migration` |
| `claimed` の修復を落とす | Task 13 Step 1 | `test_migrate_signals_fk_repairs_dangling_claimed_row` |
| `consumed` を pending に戻す | Task 13 Step 1 | `test_migrate_signals_fk_repairs_dangling_consumed_row_without_reviving`・`test_migrate_signals_fk_repairs_dangling_abandoned_row_without_reviving` (設計書は consumed のみ明記だが abandoned も同じ修復規則のため対で用意した) |
| `foreign_key_check` を削除 | Task 13 Step 1 | `test_migrate_signals_fk_leaves_foreign_key_check_clean` (ただし単独では殺せない可能性がある旨を変異ノート #4 に明記) |

追加 5 件 (Task 13 変異ノート #5〜9): `PRAGMA foreign_keys` トグル順序の landmine / finally での復元 / id 保存 / UNIQUE 保存 / CHECK 保存。

**D7 (Task 19)** — 設計書 `2026-08-11-phase2-9-foundation-design.md:342` は明示的な「変異」リストの節を持たず、防御と受入条件が本文に埋め込まれている。それらすべてを Task 19 変異ノートに転記した:

| 設計書の防御・受入条件 | 対応 Step | 対応テスト |
|---|---|---|
| `result='pr' OR pr_url IS NOT NULL` のガード (`pr_url` だけでは見逃す、codex I6) | Task 19 Step 1 | `test_improvement_runs_migration_aborts_on_legacy_pr_result_with_null_pr_url`・`test_improvement_runs_migration_aborts_on_nonnull_pr_url_with_other_result` |
| 新テーブルに `CHECK (result IN ('approval','report'))` | Task 19 Step 1 | `test_improvement_runs_check_rejects_invalid_result_after_migration` |

追加 3 件 (Task 19 変異ノート #5・#6・#7): NULL result の許容 (未完了 run) / `finish()` の `pr_url` 削除自体 / エラーメッセージの id 埋め込み。

#### ② プレースホルダの確認

本書中の全コードブロック (テスト・実装・git コマンド) を目視で再確認し、以下のいずれも含まれないことを確認した: `TBD` / `TODO` / `実装する` (説明のみで中身が無い形) / `Task N と同様` / `(省略)` / `以下同様`。Task 19 の Step 5 で「（上記コードブロックの `_SIGNALS_V2_DDL` 部分は Task 13 で既に存在する — ...）」という注記を置いたのは省略ではなく、**Task 13 の diff が既に適用済みという前提を明示するための文脈情報**であり、挿入すべき実コード (`_IMPROVEMENT_RUNS_V2_DDL` の全文) はその直前に完全に書かれている。

#### ③ `store/db.py` を触る他 task (16/15/17) との競合ポイント

親プランの「task 間のファイル競合」表 (`2026-08-11-phase2-9-foundation.md:405`) が既に一般論として指摘している「`store/db.py` は Task 13/15/16/17/19 が集中する」を、束 D の視点で具体化する:

1. **`TABLE_NAMES` (db.py:145-150) と `tests/store/test_db.py::EXPECTED`**: 束 D (Task 12/13/19) は**どのテーブル名も追加・削除しない** (signals・improvement_runs とも既存テーブルの列変更のみ)。したがってこの 2 箇所は束 D の変更では触らない。一方 **Task 16 (束 C) は `ohlcv` を削除し `ohlcv_cache`/`ohlcv_history` を追加**、**Task 15/17 (束 E) は `reflection_attempts`/`alert_state` を新設**するため、両方とも `TABLE_NAMES` と `EXPECTED` を書き換える。**束 D の実装者は `TABLE_NAMES` に一切触れないことで、この 2 箇所での他束との衝突を構造的に避けている** — レビュー時にここが変更されていたら束 D のスコープ逸脱として指摘すること。

2. **`_SCHEMA` 文字列内の挿入位置**: 束 D は `_OHLCV_V2_DDL` ブロックの直後 (Task 13: `_SIGNALS_V2_DDL`、Task 19: `_IMPROVEMENT_RUNS_V2_DDL`) に定数を追加し、`_SCHEMA` 本体では improvement_runs (中間) と signals (末尾) のインライン定義をそれぞれの定数への連結に置き換える。**Task 16 は `_OHLCV_V2_DDL` 自体を書き換える** (ohlcv → ohlcv_cache/ohlcv_history への分割) ため、束 D の定数群を挿入する「`_OHLCV_V2_DDL` の直後」という基準点そのものが Task 16 のマージ後には別内容になっている可能性が高い。**worktree 並列で束 C (Task 16) と束 D を同時に走らせた場合、`_SCHEMA` 定義部分でのマージ時に手動リベースが必要になる** (親プランの実行グラフは A/C/D を並列可としているため、これは実際に起こり得る)。

3. **`init_db()` 関数末尾への呼び出し追加**: 束 D は `_ensure_column(conn, "missions", "trigger", "trigger TEXT")` の直後に `_migrate_signals_fk(conn)` → `_migrate_improvement_runs_v2(conn)` の順で追記する。**Task 15/17/16 も同じ「`_ensure_column` の後、`conn.commit()` の前」という末尾領域に自分の migration 呼び出しを追記する可能性が高い** (この領域が「新規 migration 呼び出しの追記点」として最も衝突コストが低いことは Task 13 の設計時点で既に意図している — `_OHLCV_V2_DDL` ブロックの中間に割り込むより安全)。**実際の統合順序は、親プランが指定する束の合流順序 (`A/C/D → B → E`) に従う**: 束 D が先に `init_db()` 末尾へ 2 行追記した状態を、束 E (Task 15/17) が取り込んでからさらに追記する形になり、逆順にはならない。束 C (Task 16) は独立した `_migrate_ohlcv_v2` を書き換えるため `init_db()` 末尾への追記合戦にはならない見込みだが、**マージ時に `init_db()` 本体を必ず目視確認すること** (どの migration 呼び出しが何回・どの順で並んでいるか)。

4. **Task 13 と Task 19 自身の直列化**: 本書冒頭「束 D 内の実装順序」で明記したとおり、Task 13 → Task 19 の順で `store/db.py` を直列編集する。この順序を守らずに真の並列 worktree で実施した場合、`_OHLCV_V2_DDL` 直後の定数追加位置と `init_db()` 末尾の呼び出し追加位置の両方で機械的な git 衝突が起こる (どちらも同じ 1〜2 行の挿入点を取り合う形になる)。

#### カバーできなかった項目 (正直な申告)

- **Task 13 変異ノート #4 (`foreign_key_check` の削除)**: 設計書 D5 が明示する変異だが、repair ロジック (#2・#3 の変異) が正しく機能している限り `foreign_key_check` の結果は元々空になるため、**「検査コードの削除」そのものを直接殺すテストを用意できなかった**。この検査は「将来 repair ロジックに未知のバグが入ったときの最終防波堤」であり、変異テストで直接その価値を証明する方法が無い (repair が正しい限り検査対象が常に空集合になるため)。実装者はこの点を mutation ledger に率直に記録し、削除しないことをレビューで明示的に確認してもらうこと。
- **Task 19 のガード実装順序 (RENAME の前に検査を置くこと)**: advisor の指摘を反映しテストは書いたが (`test_improvement_runs_migration_aborts_on_legacy_pr_result_with_null_pr_url` が「中断時に `pr_url` 列が残っている」ことまで確認している)、「ガードが RENAME の前に実行されている」ことをコード的に強制する変異テスト (例: ガードを RENAME の後に移動する変異) は個別に用意していない。上記テストの「pr_url 列が残っている」assert が実質的にこの順序を検査しているため独立テストは追加しなかったが、実装者はこの assert が実際に red/green で機能することを mutation sweep で確認すること。


---

## プラン 9 束 E 実装計画: 起票返済と可観測性 (Task 15 / 17 / 18)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> 本書は親プラン `docs/superpowers/plans/2026-08-11-phase2-9-foundation.md` の束 E の詳細 step である。Global Constraints / プラン規約 / Interfaces / 着手前の既知事実は親プランをそのまま適用し、ここでは再記述しない。設計の正は `docs/superpowers/specs/2026-08-11-phase2-9-foundation-design.md` の D1 / D3 / D3' / D8。

**Goal:** reflection の無制限再試行を有限化し、取引 Mission の commit-post で連続 gate rejection を一度だけ通知し、`llama_swap.timeout_sec` を cold/warm 実測で決める。

**Architecture:** Task 15 は失敗台帳を SQLite に分離し、抽出時の上限除外・成功時 clear・人間による再キューを一つの契約にする。Task 17 は intent 記録に構造化列を追加し、読み取り専用 `conn_supervisor` で区間を評価、lock 外通知後に `core_lock` 下の `conn_core` でラッチを更新する。Task 18 はコード設計ではなく環境付き計測記録を成果物にする。

**Tech Stack:** Python 3.12 / uv / pytest / sqlite3 (WAL・table rebuild) / threading / httpx (`llama-swap` OpenAI-compatible API)

### 束内の実行順序

Task 15 → Task 17 → Task 18 の順で実施する。Task 15 と Task 17 はともに `store/db.py` / `config.py` / `settings.yaml.example` を触るため直列にし、Task 17 は束 B の `executor.py` 変更を取り込んだ後に着手する。Task 18 は Task 5 の `/props` 可視化が入った実運用環境で行う。

---

### Task 15: reflection 再試行ポリシー

**Files:**
- Create: `src/agentic_fx/store/reflection_attempts.py`
- Modify: `src/agentic_fx/store/db.py:11-21,31-60,145-150,375-384`
- Modify: `src/agentic_fx/loops/reflection_cycle.py:41,65-88,90-215`
- Modify: `src/agentic_fx/config.py:133-140,253-273`
- Modify: `config/settings.yaml.example:48-54`
- Modify: `src/agentic_fx/commands.py:12,16-24,41-84`
- Test: `tests/store/test_db.py`
- Create/Test: `tests/store/test_reflection_attempts.py`
- Modify/Test: `tests/loops/test_reflection_cycle.py`（束 A Task 4 が追加する `test_reflection_current_retry_behavior_is_pinned` を名指しで置換）
- Modify/Test: `tests/test_commands.py`
- Modify/Test: `tests/test_config.py`

**Interfaces:**
- Consumes: `MissionResult.reason: str | None`（Task 1/2）、`ReflectionCycle._reflect_one(row: dict) -> bool`、`Commands.dispatch(line: str) -> str`
- Produces:
  ```python
  def bump(conn: sqlite3.Connection, order_id: int, *,
           now: datetime, reason: str | None) -> int: ...
  def clear(conn: sqlite3.Connection, order_id: int) -> None: ...
  def attempts_of(conn: sqlite3.Connection, order_id: int) -> int: ...

  class ReflectionSettings(_Strict):
      max_attempts: int = Field(ge=1, default=2)
  ```

- [ ] **Step 1: 失敗するテストを書く**

`tests/store/test_db.py` に空 DB・既存 DB・二重初期化を別テストで追加する。

```python
def test_init_db_fresh_creates_reflection_attempts(tmp_path):
    conn = connect(tmp_path / "fresh.db")
    init_db(conn)
    cols = {r["name"] for r in conn.execute(
        "PRAGMA table_info(reflection_attempts)")}
    assert cols == {"order_id", "attempts", "last_attempt_at", "last_reason"}


def test_init_db_existing_database_adds_reflection_attempts(tmp_path):
    conn = connect(tmp_path / "legacy.db")
    conn.executescript(
        "CREATE TABLE missions (id INTEGER PRIMARY KEY, loop TEXT NOT NULL, "
        "runner TEXT NOT NULL, model TEXT NOT NULL, status TEXT NOT NULL, "
        "output_json TEXT, transcript_json TEXT, started_at TEXT NOT NULL, "
        "finished_at TEXT);"
        "CREATE TABLE trade_intents (id INTEGER PRIMARY KEY, mission_id INTEGER "
        "NOT NULL, payload_json TEXT NOT NULL, gate_result TEXT, reject_reason "
        "TEXT, created_at TEXT NOT NULL);"
        "CREATE TABLE orders (id INTEGER PRIMARY KEY, intent_id INTEGER, "
        "pair TEXT NOT NULL, direction TEXT NOT NULL, entry_type TEXT NOT NULL, "
        "horizon TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL, "
        "updated_at TEXT NOT NULL);"
    )
    init_db(conn)
    assert conn.execute("SELECT COUNT(*) FROM reflection_attempts").fetchone()[0] == 0


def test_reflection_attempts_migration_is_idempotent(tmp_path):
    conn = connect(tmp_path / "existing.db")
    init_db(conn)
    init_db(conn)
    assert conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' "
        "AND name='reflection_attempts'").fetchone()[0] == 1
```

`tests/store/test_reflection_attempts.py` を作成する。

```python
from datetime import datetime, timezone

from agentic_fx.store import reflection_attempts
from agentic_fx.store.db import connect, init_db

NOW = datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc)


def _conn(tmp_path):
    c = connect(tmp_path / "a.db")
    init_db(c)
    return c


def test_bump_returns_incremented_attempt_count(tmp_path):
    c = _conn(tmp_path)
    c.execute("INSERT INTO orders (pair,direction,entry_type,horizon,status,"
              "created_at,updated_at) VALUES ('USDJPY','long','market','day',"
              "'closed',?,?)", (NOW.isoformat(), NOW.isoformat()))
    oid = c.execute("SELECT id FROM orders").fetchone()[0]
    assert reflection_attempts.bump(c, oid, now=NOW, reason="boom") == 1
    assert reflection_attempts.bump(c, oid, now=NOW, reason="boom2") == 2


def test_clear_deletes_attempt_row(tmp_path):
    c = _conn(tmp_path)
    c.execute("INSERT INTO orders (pair,direction,entry_type,horizon,status,"
              "created_at,updated_at) VALUES ('USDJPY','long','market','day',"
              "'closed',?,?)", (NOW.isoformat(), NOW.isoformat()))
    oid = c.execute("SELECT id FROM orders").fetchone()[0]
    reflection_attempts.bump(c, oid, now=NOW, reason=None)
    reflection_attempts.clear(c, oid)
    assert reflection_attempts.attempts_of(c, oid) == 0
```

`tests/loops/test_reflection_cycle.py` の束 A テスト `test_reflection_current_retry_behavior_is_pinned` を削除せず、次の名前と内容へ置換する。

```python
def test_reflection_retries_to_limit_then_stops(tmp_path):
    conn, rag, cyc = _cycle(tmp_path, [
        MissionResult("failed", None, [], reason="boom1"),
        MissionResult("failed", None, [], reason="boom2"),
        MissionResult("failed", None, [], reason="must-not-run"),
    ])
    oid = _closed_order(conn)
    assert cyc.run_pending() == 0
    assert cyc.run_pending() == 0
    assert cyc.run_pending() == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM missions WHERE loop='reflection' "
        "AND status='failed'").fetchone()[0] == 2
    assert conn.execute(
        "SELECT attempts FROM reflection_attempts WHERE order_id=?", (oid,)
    ).fetchone()[0] == 2


def test_reflection_abandoned_activity_written_once_at_limit(tmp_path):
    conn, rag, cyc = _cycle(tmp_path, [
        MissionResult("failed", None, [], reason="one"),
        MissionResult("failed", None, [], reason="two"),
    ])
    _closed_order(conn)
    cyc.run_pending()
    cyc.run_pending()
    cyc.run_pending()
    text = (tmp_path / "a.log").read_text(encoding="utf-8")
    assert text.count("reflection_abandoned") == 1


def test_failed_old_order_does_not_starve_later_order(tmp_path):
    conn, rag, cyc = _cycle(tmp_path, [
        MissionResult("failed", None, [], reason="one"),
        MissionResult("failed", None, [], reason="two"),
        MissionResult("completed", {"content": "later"}, []),
    ])
    first = _closed_order(conn)
    second = _closed_order(conn)
    cyc.run_pending(max_items=1)
    cyc.run_pending(max_items=1)
    assert cyc.run_pending(max_items=1) == 1
    assert reflections.get(conn, first) is None
    assert reflections.get(conn, second)["content"] == "later"


def test_success_clears_prior_attempt_row(tmp_path):
    conn, rag, cyc = _cycle(tmp_path, [
        MissionResult("completed", {"content": "ok"}, [])])
    oid = _closed_order(conn)
    from agentic_fx.store import reflection_attempts
    reflection_attempts.bump(conn, oid, now=NOW, reason="old")
    assert cyc.run_pending() == 1
    assert reflection_attempts.attempts_of(conn, oid) == 0


def test_rag_failure_consumes_attempt_and_stops_at_limit(tmp_path):
    from unittest.mock import Mock
    from agentic_fx.store import reflection_attempts
    conn, rag, cyc = _cycle(tmp_path, [
        MissionResult("completed", {"content": "ok"}, []),
        MissionResult("completed", {"content": "ok"}, [])])
    oid = _closed_order(conn)
    rag.add_reflection = Mock(side_effect=RuntimeError("chroma down"))
    assert cyc.run_pending() == 0
    assert cyc.run_pending() == 0
    assert reflection_attempts.attempts_of(conn, oid) == 2
    before = conn.execute("SELECT COUNT(*) FROM missions").fetchone()[0]
    assert cyc.run_pending() == 0
    assert conn.execute("SELECT COUNT(*) FROM missions").fetchone()[0] == before
```

`tests/test_commands.py` と `tests/test_config.py` に追加する。

```python
def test_reflect_retry_deletes_attempt_row(tmp_path):
    conn, _state, _activity, cmd = _commands(tmp_path)
    now = cmd.clock.now()
    conn.execute("INSERT INTO orders (pair,direction,entry_type,horizon,status,"
                 "created_at,updated_at) VALUES ('USDJPY','long','market','day',"
                 "'closed',?,?)", (now.isoformat(), now.isoformat()))
    oid = conn.execute("SELECT id FROM orders").fetchone()[0]
    from agentic_fx.store import reflection_attempts
    reflection_attempts.bump(conn, oid, now=now, reason="boom")
    assert cmd.dispatch(f"reflect retry {oid}") == f"order #{oid} を reflection 再試行対象へ戻しました"
    assert reflection_attempts.attempts_of(conn, oid) == 0


def test_reflection_and_alert_defaults_from_example():
    s = load_settings(EXAMPLE)
    assert s.reflection.max_attempts == 2
    assert s.alert.consecutive_gate_reject == 10
```

- [ ] **Step 2: 失敗を確認**

Run:

```bash
uv run pytest tests/store/test_db.py -k reflection_attempts -v
uv run pytest tests/store/test_reflection_attempts.py -v
uv run pytest tests/loops/test_reflection_cycle.py -k "retries_to_limit or abandoned_activity or starve or success_clears" -v
uv run pytest tests/test_commands.py::test_reflect_retry_deletes_attempt_row tests/test_config.py::test_reflection_and_alert_defaults_from_example -v
```

Expected: `no such table: reflection_attempts`、`ModuleNotFoundError: agentic_fx.store.reflection_attempts`、旧回帰ピンでは 3 本目の mission が作られて `assert 3 == 2`、command は help を返し、config は `Settings` に `reflection` / `alert` が無く FAIL。

- [ ] **Step 3: 最小実装**

最初に `tests/store/test_db.py` の全表集合を Task 15 完了時点へ更新する。Task 16
完了時点の 15 表集合にこの task の 1 表を足すので、変更は次のとおりである。

```python
EXPECTED = {
    "ohlcv_cache", "ohlcv_history", "missions", "trade_intents", "orders",
    "reflections", "account_snapshots", "improvement_backlog",
    "improvement_runs", "econ_events", "approval_requests", "news_sources",
    "backtest_runs", "analysis_runs", "signals", "reflection_attempts",
}


def test_init_creates_all_16_tables(tmp_path):
    conn = connect(tmp_path / "agentic.db")
    init_db(conn)
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%'").fetchall()
    assert {r["name"] for r in rows} == EXPECTED
    assert TABLE_NAMES == frozenset(EXPECTED)
```

`EXPECTED` は省略記号をソースへ書かず、Task 16 が作った 15 個の文字列をすべて
温存して末尾へ `"reflection_attempts"` を追加する。この変更後、Task 15 単独の
Step 4 で `TABLE_NAMES == frozenset(EXPECTED)` と実 DB の 16 表が一致する。

`store/db.py` の `_SCHEMA` に orders より後で次を追加し、`TABLE_NAMES` に `reflection_attempts` を加える。`CREATE TABLE IF NOT EXISTS` なので空 DB・既存 DB・二重実行で同じ経路を使う。

```sql
CREATE TABLE IF NOT EXISTS reflection_attempts (
  order_id INTEGER PRIMARY KEY REFERENCES orders(id),
  attempts INTEGER NOT NULL DEFAULT 0,
  last_attempt_at TEXT NOT NULL,
  last_reason TEXT
);
```

`src/agentic_fx/store/reflection_attempts.py` を作成する。

```python
from __future__ import annotations

import sqlite3
from datetime import datetime


def bump(conn: sqlite3.Connection, order_id: int, *, now: datetime,
         reason: str | None) -> int:
    row = conn.execute(
        "INSERT INTO reflection_attempts "
        "(order_id, attempts, last_attempt_at, last_reason) VALUES (?,1,?,?) "
        "ON CONFLICT(order_id) DO UPDATE SET attempts=attempts+1, "
        "last_attempt_at=excluded.last_attempt_at, "
        "last_reason=excluded.last_reason RETURNING attempts",
        (order_id, now.isoformat(), reason)).fetchone()
    conn.commit()
    return int(row["attempts"])


def clear(conn: sqlite3.Connection, order_id: int) -> None:
    conn.execute("DELETE FROM reflection_attempts WHERE order_id=?", (order_id,))
    conn.commit()


def attempts_of(conn: sqlite3.Connection, order_id: int) -> int:
    row = conn.execute(
        "SELECT attempts FROM reflection_attempts WHERE order_id=?", (order_id,)
    ).fetchone()
    return int(row["attempts"]) if row else 0
```

`config.py` に次を足し `Settings` に default factory で載せる。example へ `reflection: {max_attempts: 2}` と、Task 17 用の `alert: {consecutive_gate_reject: 10}` を同時に追加する。

```python
class ReflectionSettings(_Strict):
    max_attempts: int = Field(ge=1, default=2)


class AlertSettings(_Strict):
    consecutive_gate_reject: int = Field(ge=1, default=10)

# Settings fields
reflection: ReflectionSettings = Field(default_factory=ReflectionSettings)
alert: AlertSettings = Field(default_factory=AlertSettings)
```

`reflection_cycle.py` は import に `reflection_attempts` を加え、抽出を named parameter 化する。

```python
rows = self.conn.execute(
    "SELECT o.* FROM orders o "
    "LEFT JOIN reflections r ON r.order_id=o.id "
    "LEFT JOIN reflection_attempts a ON a.order_id=o.id "
    "WHERE o.status='closed' AND r.order_id IS NULL "
    "AND (a.attempts IS NULL OR a.attempts < :max) "
    "ORDER BY o.id LIMIT :limit",
    {"max": self.settings.reflection.max_attempts, "limit": max_items},
).fetchall()
```

`_reflect_one` の Mission 失敗分岐では `core_lock` 内で bump し、到達時だけ activity を書く。`finalize_ok=False` は mission 監査書込み失敗なので試行回数を消費せず、既存の再試行契約を維持する。

```python
if result.status != "completed":
    with self._core_lock:
        attempts = reflection_attempts.bump(
            self.conn, row["id"], now=self.clock.now(), reason=result.reason)
    if attempts == self.settings.reflection.max_attempts:
        try:
            self.activity.write(
                Category.AGGREGATE, "reflection_abandoned",
                f"order_id={row['id']} attempts={attempts}",
                ref_id=str(row["id"]))
        except Exception:  # noqa: BLE001
            _log.exception("failed to record reflection_abandoned for #%s", row["id"])
    return False
if not finalize_ok:
    return False
```

`rag.add_reflection(...)` の既存 `except Exception` も「completed だから無料」に
せず、activity `reflection_rag_failed` の記録後、return 前に次を実行する:

```python
with self._core_lock:
    attempts = reflection_attempts.bump(
        self.conn, row["id"], now=self.clock.now(),
        reason="rag.add_reflection failed")
if attempts == self.settings.reflection.max_attempts:
    try:
        self.activity.write(
            Category.AGGREGATE, "reflection_abandoned",
            f"order_id={row['id']} attempts={attempts}",
            ref_id=str(row["id"]))
    except Exception:  # noqa: BLE001
        _log.exception("failed to record reflection_abandoned for #%s", row["id"])
return False
```

成功の completion marker 保存直後、同じ `core_lock` 内で `clear` する。

```python
with self._core_lock:
    reflections.save(self.conn, row["id"], content, now)
    reflection_attempts.clear(self.conn, row["id"])
```

`commands.py` は `reflection_attempts` を import し、help と dispatch に追加する。

```python
if cmd == "reflect" and len(args) == 2 and args[0] == "retry":
    order_id = int(args[1])
    if orders.get(self.conn, order_id) is None:
        raise ValueError(f"order #{order_id} does not exist")
    reflection_attempts.clear(self.conn, order_id)
    self.activity.write(Category.SYSTEM, "reflection_requeued",
                        f"order_id={order_id} via shell", ref_id=str(order_id))
    return f"order #{order_id} を reflection 再試行対象へ戻しました"
```

- [ ] **Step 4: 成功を確認**

```bash
uv run pytest tests/store/test_db.py tests/store/test_reflection_attempts.py tests/loops/test_reflection_cycle.py tests/test_commands.py tests/test_config.py -q
```

Expected: 全件 PASS。`test_reflection_retries_to_limit_then_stops` は missions=2、`test_reflection_abandoned_activity_written_once_at_limit` は通知 seam を一切持たず activity 1 行、成功行は attempts=0。

- [ ] **Step 5: 変異テスト**

```bash
find . -name __pycache__ -type d -not -path "./.venv/*" -exec rm -rf {} +
```

| 変異 | red になる固有テスト |
|---|---|
| SQL の attempts 上限条件を削除 | `test_reflection_retries_to_limit_then_stops` |
| `bump()` の `attempts+1` を `attempts` にする | `test_bump_returns_incremented_attempt_count` |
| `reflection_abandoned` write を削除 | `test_reflection_abandoned_activity_written_once_at_limit` |
| `LEFT JOIN reflection_attempts` または join 条件を削除 | `test_failed_old_order_does_not_starve_later_order` |
| `rag.add_reflection` 例外経路の `bump()` を削除 | `test_rag_failure_consumes_attempt_and_stops_at_limit` |
| `:max` を `999` にする | `test_reflection_retries_to_limit_then_stops` |
| 成功時 `clear()` を削除 | `test_success_clears_prior_attempt_row` |
| shell の `clear()` を削除 | `test_reflect_retry_deletes_attempt_row` |

各変異は 1 件ずつ当て、`grep -n "reflection_attempts\|reflection_abandoned\|attempts <"` で改変を表示後、表の単独テストを実行する。revert 後も同じ単独テストが green であることを確認し ledger に記録する。

- [ ] **Step 6: コミット**

```bash
git add src/agentic_fx/store/reflection_attempts.py src/agentic_fx/store/db.py \
  src/agentic_fx/loops/reflection_cycle.py src/agentic_fx/config.py \
  config/settings.yaml.example src/agentic_fx/commands.py \
  tests/store/test_db.py tests/store/test_reflection_attempts.py \
  tests/loops/test_reflection_cycle.py tests/test_commands.py tests/test_config.py
git commit -m "feat: reflection 再試行を上限付き台帳で有限化する (plan9 task15)"
```

---

### Task 17: `gate_rejected` の可観測性

**Files:**
- Create: `src/agentic_fx/store/alert_state.py`
- Modify: `src/agentic_fx/store/db.py`（Task 13/16/19/15 適用後の DDL 定数群と `init_db()` 末尾）
- Modify: `src/agentic_fx/store/intents.py:8-23`
- Modify: `src/agentic_fx/core/executor.py:343-870`（`insert` 1 箇所、`set_gate_result` 現物 20 箇所のみ。判定ロジック不変）
- Modify: `src/agentic_fx/loops/trade_loop.py:49-69,298-371`（現物 `set_gate_result` 1 箇所 + commit-post 評価）
- Modify: `src/agentic_fx/config.py`（Task 15 が追加した `AlertSettings` を消費）
- Modify: `config/settings.yaml.example`（Task 15 が追加した `alert` を消費）
- Test: `tests/store/test_db.py`
- Modify/Test: `tests/store/test_intents.py`
- Create/Test: `tests/store/test_alert_state.py`
- Create/Test: `tests/loops/test_gate_reject_alert.py`
- Create/Test: `tests/core/test_executor_category.py`
- Modify/Test: `tests/core/test_executor.py`, `tests/core/test_executor_snapshot.py`, `tests/loops/test_trade_loop_phases.py`

**Interfaces:**
- Consumes:
  ```python
  def insert(conn, mission_id: int, payload: dict, now: datetime, *,
             action: str) -> int: ...
  def set_gate_result(conn, intent_id: int, *, accepted: bool,
                      reject_reason: str | None,
                      reject_category: str | None) -> None: ...
  ```
- Produces:
  ```python
  GATE_REJECT_STREAK_KEY = "gate_reject.last_notified_streak_id"
  def get(conn: sqlite3.Connection, key: str) -> str | None: ...
  def set(conn: sqlite3.Connection, key: str, value: str, *,
          now: datetime) -> None: ...
  # src/agentic_fx/loops/trade_loop.py
  def gate_reject_streak(
      conn: sqlite3.Connection,
  ) -> tuple[int, int, str | None]: ...
  ```
  tuple は `(streak_id, rejected_count, dominant_category)`。SQL ①②③を順に実行し、②はカテゴリで絞らない。配置は `TradeLoop` の commit-post 専用集計であり store の CRUD ではないため、`src/agentic_fx/loops/trade_loop.py` の module-level 関数に確定する。テストは必ず `from agentic_fx.loops.trade_loop import gate_reject_streak` と import し、裸の未定義名を使わない。

#### 全 `set_gate_result` call site の現物一覧

2026-08-11 の現 worktree を `rg -n 'intents_store\.set_gate_result\(' src/agentic_fx/core/executor.py src/agentic_fx/loops/trade_loop.py` した結果は **21 箇所（accepted 4 / rejected 17）**であり、**指揮者が現物照合して 21 が正と確定した** (親骨格初版の「23 箇所」は定義行とコメント言及を含む誤記で訂正済み)。束 B 合流後の Step 2 で再計数し、**21 以外なら束 B の変更が call site を増減させた可能性があるので指揮者へ報告する** (存在しない call site を作らない)。

| 現在行 | accepted | category | 理由 |
|---|---:|---|---|
| `executor.py:369` | False | `origin` | scheduler 以外の origin |
| `executor.py:379` | False | `mission` | loop が trade でない |
| `executor.py:410` | False | `risk_gate` | fresh account snapshot 無し |
| `executor.py:427` | False | `risk_gate` | conversion rate 不健全 |
| `executor.py:452` | False | `risk_gate` | `evaluate()` が却下 |
| `executor.py:464` | True | `None` | open gate 受理 |
| `executor.py:565` | False | `execution` | open snapshot stale |
| `executor.py:574` | False | `risk_gate` | commit-core account snapshot 無し |
| `executor.py:588` | False | `execution` | exposure snapshot coverage |
| `executor.py:609` | False | `execution` | pair coverage |
| `executor.py:622` | False | `execution` | currency coverage |
| `executor.py:686` | False | `execution` | close 対象が open でない（legacy close path） |
| `executor.py:692` | True | `None` | legacy close 受理 |
| `executor.py:789` | False | `execution` | close 対象が open でない |
| `executor.py:800` | False | `execution` | close snapshot 無し |
| `executor.py:812` | False | `execution` | close snapshot stale |
| `executor.py:818` | True | `None` | close 受理 |
| `executor.py:825` | False | `execution` | close snapshot coverage |
| `executor.py:864` | False | `execution` | cancel 対象が pending_fill でない |
| `executor.py:870` | True | `None` | cancel 受理 |
| `trade_loop.py:318` | False | `execution` | commit-pre snapshot 取得失敗 |

上表を最終 DB 行で観測できるのは **20 site** である。`executor.py:818` は正常 close
なら `accepted/NULL` が残るので観測できるが、直後の
`close_order_from_snapshot()` が `SnapshotCoverageError` を投げる経路では
`executor.py:825` が同じ行を `rejected/execution` に上書きする。したがって
818 の category 変異を「coverage error 後の最終 `trade_intents.reject_category`」で
殺すことはできない。818 は正常 close 経路、825 は coverage error 経路で別々に
検査する。これ以外の到達不能 site は現物テスト fixture との照合では無い。

- [ ] **Step 1: 失敗するテストを書く**

`tests/store/test_db.py` に fresh / legacy / idempotent / CHECK を 1 目的 1 テストで追加する。legacy helper は旧 `trade_intents` DDL を完全に作る。

```python
def _legacy_trade_intents_ddl():
    return ("CREATE TABLE trade_intents (id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "mission_id INTEGER NOT NULL REFERENCES missions(id),"
            "payload_json TEXT NOT NULL,gate_result TEXT,reject_reason TEXT,"
            "created_at TEXT NOT NULL)")


def test_init_db_fresh_trade_intents_has_observability_columns(tmp_path):
    c = connect(tmp_path / "fresh.db")
    init_db(c)
    cols = {r["name"] for r in c.execute("PRAGMA table_info(trade_intents)")}
    assert {"action", "reject_category"} <= cols


def test_trade_intents_migration_accepts_legacy_rejected_row_with_null_action(tmp_path):
    c = connect(tmp_path / "legacy.db")
    c.executescript("CREATE TABLE missions (id INTEGER PRIMARY KEY, loop TEXT NOT NULL,"
                    "runner TEXT NOT NULL,model TEXT NOT NULL,status TEXT NOT NULL,"
                    "started_at TEXT NOT NULL);" + _legacy_trade_intents_ddl())
    c.execute("INSERT INTO missions VALUES (1,'trade','local','m','completed','x')")
    c.execute("INSERT INTO trade_intents (mission_id,payload_json,gate_result,"
              "reject_reason,created_at) VALUES (1,'{}','rejected','legacy','x')")
    c.commit()
    init_db(c)
    row = c.execute("SELECT action,reject_category FROM trade_intents").fetchone()
    assert row["action"] is None and row["reject_category"] is None


def test_trade_intents_migration_is_idempotent(tmp_path):
    """⚠️ 旧版は空 DB で `init_db` を 2 回呼び `COUNT(*) == 0` を見るだけで、
    **migration が実際に行われたか / 2 回目が既存行を壊さないかを一切検証して
    いなかった** (空の新規 DB は何をしても 0 件。ローカル LLM 2 本が一致して
    指摘し指揮者が裏取りした)。legacy 行を 1 件持つ DB で 2 回流し、**行が
    生き残り列も揃っている**ことを見る形に変える。"""
    c = connect(tmp_path / "db.sqlite")
    c.executescript("CREATE TABLE missions (id INTEGER PRIMARY KEY, loop TEXT NOT NULL,"
                    "runner TEXT NOT NULL,model TEXT NOT NULL,status TEXT NOT NULL,"
                    "started_at TEXT NOT NULL);" + _legacy_trade_intents_ddl())
    c.execute("INSERT INTO missions VALUES (1,'trade','local','m','completed','x')")
    c.execute("INSERT INTO trade_intents (mission_id,payload_json,gate_result,"
              "reject_reason,created_at) VALUES (1,'{}','rejected','legacy','x')")
    c.commit()
    init_db(c)
    init_db(c)                      # 2 回目が壊さないことが本題
    rows = c.execute("SELECT action,reject_category,reject_reason "
                     "FROM trade_intents").fetchall()
    assert len(rows) == 1                              # 行が重複も消失もしない
    assert rows[0]["reject_reason"] == "legacy"        # 中身が保たれている
    assert rows[0]["action"] is None                   # legacy は NULL のまま
    cols = {r["name"] for r in c.execute("PRAGMA table_info(trade_intents)")}
    assert {"action", "reject_category"} <= cols       # 列が消えていない


def test_trade_intents_migration_keeps_orders_fk_usable(tmp_path):
    """参照される側の rebuild 後も orders FK は trade_intents を指す。"""
    c = connect(tmp_path / "legacy.db")
    c.executescript("CREATE TABLE missions (id INTEGER PRIMARY KEY, loop TEXT NOT NULL,"
                    "runner TEXT NOT NULL,model TEXT NOT NULL,status TEXT NOT NULL,"
                    "started_at TEXT NOT NULL);" + _legacy_trade_intents_ddl() +
                    "CREATE TABLE orders (id INTEGER PRIMARY KEY, intent_id INTEGER "
                    "REFERENCES trade_intents(id));")
    c.execute("INSERT INTO missions VALUES (1,'trade','local','m','completed','x')")
    c.execute("INSERT INTO trade_intents (id,mission_id,payload_json,created_at) "
              "VALUES (7,1,'{}','x')")
    c.commit()
    init_db(c)
    fk = c.execute("PRAGMA foreign_key_list(orders)").fetchone()
    assert fk["table"] == "trade_intents"
    c.execute("INSERT INTO orders (intent_id) VALUES (7)")


def test_trade_intents_check_rejects_invalid_gate_category_pair(tmp_path):
    c = connect(tmp_path / "db.sqlite")
    init_db(c)
    c.execute("INSERT INTO missions (id,loop,runner,model,status,started_at) "
              "VALUES (999,'trade','local','m','completed','x')")
    with pytest.raises(sqlite3.IntegrityError):
        c.execute("INSERT INTO trade_intents (mission_id,payload_json,action,"
                  "gate_result,reject_category,created_at) VALUES "
                  "(999,'{}','open','accepted','risk_gate','x')")
```

`tests/store/test_intents.py` を更新する。

```python
def test_insert_requires_explicit_action_and_persists_it(tmp_path):
    c = connect(tmp_path / "t.db"); init_db(c)
    mid = missions.start(c, "trade", "local", "m", NOW)
    iid = intents.insert(c, mid, {"opaque": True}, NOW, action="open")
    assert intents.get(c, iid)["action"] == "open"


def test_set_gate_result_requires_category_and_persists_it(tmp_path):
    c = connect(tmp_path / "t.db"); init_db(c)
    mid = missions.start(c, "trade", "local", "m", NOW)
    iid = intents.insert(c, mid, {}, NOW, action="open")
    intents.set_gate_result(c, iid, accepted=False,
                            reject_reason="RR", reject_category="risk_gate")
    assert intents.get(c, iid)["reject_category"] == "risk_gate"
```

必須 keyword 化で壊れる既存呼び出しは、次の grep で得た **全 3 件**を同じ
Step 1 で先に直す（production の `executor.py:355` は Step 3 で直す）。

```bash
rg -n --glob '*.py' '(intents_store|intents)\.(insert|set_gate_result)\(' tests src
```

```diff
# tests/store/test_intents.py
-iid = intents.insert(c, mid, {"action": "hold", "reasoning": "様子見"}, NOW)
-intents.set_gate_result(c, iid, accepted=False, reject_reason="RR below 1.5")
+iid = intents.insert(c, mid, {"action": "hold", "reasoning": "様子見"},
+                     NOW, action="hold")
+intents.set_gate_result(c, iid, accepted=False, reject_reason="RR below 1.5",
+                        reject_category="mission")

# tests/core/test_executor_snapshot.py
-return intents_store.insert(conn, mid, _intent_payload(intent), NOW)
+return intents_store.insert(conn, mid, _intent_payload(intent), NOW,
+                            action=intent.action.value)
```

既存テスト内の `insert` は上の 2 件、`set_gate_result` は上の 1 件だけである。
Step 3 では production の残る 1 件を次のとおり直す。

```diff
 iid = intents_store.insert(self.conn, mission_id,
-                           _intent_payload(intent), now)
+                           _intent_payload(intent), now,
+                           action=intent.action.value)
```

`tests/store/test_alert_state.py` を作成する。

```python
import pytest
from datetime import datetime, timezone
from agentic_fx.store import alert_state
from agentic_fx.store.db import connect, init_db

NOW = datetime(2026, 8, 11, tzinfo=timezone.utc)


def test_alert_state_roundtrip_single_known_key(tmp_path):
    c = connect(tmp_path / "a.db"); init_db(c)
    assert alert_state.get(c, alert_state.GATE_REJECT_STREAK_KEY) is None
    alert_state.set(c, alert_state.GATE_REJECT_STREAK_KEY, "7", now=NOW)
    assert alert_state.get(c, alert_state.GATE_REJECT_STREAK_KEY) == "7"


def test_alert_state_rejects_unknown_key(tmp_path):
    c = connect(tmp_path / "a.db"); init_db(c)
    with pytest.raises(ValueError, match="unknown alert_state key"):
        alert_state.set(c, "anything", "1", now=NOW)
```

`tests/loops/test_gate_reject_alert.py` は DB fixture で SQL 意味論と commit-post を分離して書く。ファイル先頭と全 helper は次の実コードにする（既存 `tests/loops/test_trade_loop.py::_loop` と production の `build_app` / `_scheduler_tick_once` をそのまま通す）。

```python
from pathlib import Path
from shutil import copyfile
from unittest.mock import patch

import pytest

from agentic_fx.core.contracts import FixedClock
from agentic_fx.loops.trade_loop import gate_reject_streak
from agentic_fx.runners.base import MissionResult
from agentic_fx.runners.fake_runner import FakeRunner
from agentic_fx.service import _scheduler_tick_once, build_app, run_init
from agentic_fx.store import alert_state, intents, missions
from agentic_fx.store.db import connect, init_db
from tests.loops.test_trade_loop import NOW, _loop
from tests.store.test_rag import FakeEmbedding


class _RecordingNotifier:
    def __init__(self):
        self.sent = []
        self.fail = False

    def send(self, text):
        if self.fail:
            raise RuntimeError("notify failed")
        self.sent.append(text)


class _ExecuteFails:
    def __init__(self, message):
        self.message = message

    def execute(self, *args, **kwargs):
        raise AssertionError(self.message)


class _RejectTradeIntentReads:
    """alert_state の get/set は通し、判定 SQL を conn_core で読む変異だけ殺す。"""
    def __init__(self, real):
        self.real = real
    def execute(self, sql, *args, **kwargs):
        if "FROM trade_intents" in str(sql):
            raise AssertionError("trade_intents must use conn_supervisor")
        return self.real.execute(sql, *args, **kwargs)
    def __getattr__(self, name):
        return getattr(self.real, name)


def _conn(tmp_path):
    c = connect(tmp_path / "alerts.db")
    init_db(c)
    return c


def _intent(c, action, gate_result, reject_category):
    mid = missions.start(c, "trade", "local", "test", NOW)
    iid = intents.insert(c, mid, {"action": action}, NOW, action=action)
    intents.set_gate_result(
        c, iid, accepted=gate_result == "accepted",
        reject_reason=None if gate_result == "accepted" else "test rejection",
        reject_category=reject_category)
    return iid


def _settings_with_threshold(settings, threshold):
    return settings.model_copy(update={
        "alert": settings.alert.model_copy(update={
            "consecutive_gate_reject": threshold})})


def _completed_hold_loop(tmp_path, threshold=10):
    conn, loop, runner, tp = _loop(tmp_path, [MissionResult(
        "completed", {"action": "hold", "reasoning": "test"}, [])])
    loop.settings = _settings_with_threshold(loop.settings, threshold)
    loop.executor.settings = loop.settings
    loop.notifier = _RecordingNotifier()
    return conn, loop, runner, tp


def _loop_with_threshold(tmp_path, threshold):
    conn, loop, _, _ = _completed_hold_loop(tmp_path, threshold)
    notifier = loop.notifier
    return loop, conn, loop._conn_supervisor, notifier


def _app_with_threshold(tmp_path, threshold):
    root = tmp_path / "app"
    (root / "config").mkdir(parents=True)
    copyfile(Path(__file__).resolve().parents[2] /
             "config" / "settings.yaml.example",
             root / "config" / "settings.yaml.example")
    with patch("agentic_fx.service.PriceProvider") as provider, \
         patch("agentic_fx.service._check_llama_swap"):
        provider.return_value.healthcheck.return_value = "test"
        run_init(root)
        app = build_app(root, runner=FakeRunner([]), clock=FixedClock(NOW),
                        embedding_fn=FakeEmbedding())
    app.settings = _settings_with_threshold(app.settings, threshold)
    app.trade_loop.settings = app.settings
    app.executor.settings = app.settings
    app.notifier = _RecordingNotifier()
    app.trade_loop.notifier = app.notifier
    return app


def _inject_alert_failure(loop, stage, monkeypatch):
    if stage == "evaluate":
        monkeypatch.setattr(
            "agentic_fx.loops.trade_loop.gate_reject_streak",
            lambda conn: (_ for _ in ()).throw(RuntimeError("evaluate")))
    elif stage == "notify":
        loop.notifier.fail = True
    else:
        monkeypatch.setattr(
            alert_state, "set",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("state_update")))
```

```python
def test_streak_id_is_previous_accepted_open_id(tmp_path):
    """⚠️ 旧版は accepted を **1 行**しか作らず `(accepted, 2, "risk_gate")` を
    見ていたため、2 つの穴があった (ローカル LLM muse-glimmer が検出し、
    指揮者が実 SQLite で裏取り):

    ① accepted が 1 行だと `MAX(id)` と `MIN(id)` が同値になり、**`MAX`→`MIN`
       変異が生き残る**。この変異は致命的で、`MIN` だと streak_id が最初の
       accepted に固定され続け、**accepted が入っても連続が切れなくなる**
       (= 取引できているのに通知が鳴り続ける)。accepted を **2 行**作って
       初めて区別できる (実測: MAX=3→count 2 / MIN=1→count 3)。
    ② `risk_gate` 1 件・`execution` 1 件は**同数タイ**であり、SQL ③ の
       `ORDER BY c DESC LIMIT 1` の勝者は SQLite 任せで**非決定的**。
       支配的カテゴリを assert するなら**差を付ける**こと。
    """
    c = _conn(tmp_path)
    _intent(c, "open", "accepted", None)                 # 古い accepted (id=1)
    _intent(c, "open", "rejected", "risk_gate")          # これは数えてはいけない
    accepted = _intent(c, "open", "accepted", None)      # 最新 accepted (id=3)
    _intent(c, "open", "rejected", "risk_gate")
    _intent(c, "open", "rejected", "risk_gate")
    _intent(c, "open", "rejected", "execution")          # risk_gate 2 : execution 1
    assert gate_reject_streak(c) == (accepted, 3, "risk_gate")


def test_dominant_category_is_the_majority_not_a_tie(tmp_path):
    """支配的カテゴリが**多数派**で決まることを単独で pin する
    (上のテストと検査目的が別。タイでの非決定性を持ち込まないため件数に差を付ける)。"""
    c = _conn(tmp_path)
    _intent(c, "open", "rejected", "risk_gate")
    _intent(c, "open", "rejected", "execution")
    _intent(c, "open", "rejected", "execution")
    assert gate_reject_streak(c)[2] == "execution"


def test_streak_count_does_not_filter_reject_category(tmp_path):
    c = _conn(tmp_path)
    _intent(c, "open", "rejected", "risk_gate")
    _intent(c, "open", "rejected", "execution")
    assert gate_reject_streak(c)[1] == 2


def test_close_rejection_is_not_counted(tmp_path):
    c = _conn(tmp_path)
    _intent(c, "close", "rejected", "execution")
    assert gate_reject_streak(c)[1] == 0


def test_threshold_notifies_once_per_accepted_streak(tmp_path):
    loop, core, ro, notifier = _loop_with_threshold(tmp_path, threshold=2)
    _intent(core, "open", "rejected", "risk_gate")
    _intent(core, "open", "rejected", "execution")
    loop._notify_gate_reject_streak()
    loop._notify_gate_reject_streak()
    assert len(notifier.sent) == 1
    assert "2 件連続" in notifier.sent[0]


def test_new_accepted_open_rearms_notification(tmp_path):
    loop, core, ro, notifier = _loop_with_threshold(tmp_path, threshold=1)
    _intent(core, "open", "rejected", "risk_gate")
    loop._notify_gate_reject_streak()
    _intent(core, "open", "accepted", None)
    _intent(core, "open", "rejected", "execution")
    loop._notify_gate_reject_streak()
    assert len(notifier.sent) == 2


def test_notification_failure_does_not_update_streak_id(tmp_path):
    loop, core, ro, notifier = _loop_with_threshold(tmp_path, threshold=1)
    _intent(core, "open", "rejected", "risk_gate")
    notifier.fail = True
    loop._notify_gate_reject_streak()
    assert alert_state.get(core, alert_state.GATE_REJECT_STREAK_KEY) is None


def test_gate_alert_reads_only_conn_supervisor(tmp_path, monkeypatch):
    loop, core, ro, notifier = _loop_with_threshold(tmp_path, threshold=1)
    _intent(core, "open", "rejected", "risk_gate")
    monkeypatch.setattr(loop, "conn", _RejectTradeIntentReads(core))
    loop._notify_gate_reject_streak()
    assert notifier.sent


# ⚠️ 旧版はここに test_gate_alert_notifier_is_never_called_on_scheduler_thread
# を置いていたが、**vacuous (どんな実装でも PASS する) だった** — ローカル LLM
# レビュー (KAT) が検出し指揮者が裏取りした。旧版は
# `threading.Thread(target=loop._notify_gate_reject_streak)` で**メソッドを直接**
# 別スレッドから呼んでいたため、notifier 内の `threading.get_ident()` は決して
# テスト実行スレッドの ident と一致せず、`pytest.fail` ガードは**構造的に発火
# 不能**だった。しかも殺すべき変異は「**呼び出し元**を scheduler へ移す」ことで
# あり、実際の呼び出し元を一切通らないこのテストでは変異が生存する。
#
# 正しい pin は「**scheduler tick 経路を実際に走らせて notifier が呼ばれない**」
# ことと「**commit-post 経路では呼ばれる**」ことの対を見ることである。

def test_scheduler_tick_never_notifies_gate_reject_streak(tmp_path):
    """呼び出し元の pin (本命)。閾値を超える却下が既にある状態で scheduler の
    tick を実運用と同じ経路で回し、**通知が 1 件も出ない**ことを見る。
    `_notify_gate_reject_streak` を maintenance / tick へ移す変異はここで死ぬ。"""
    app = _app_with_threshold(tmp_path, threshold=1)
    _intent(app.conn_core, "open", "rejected", "risk_gate")
    _scheduler_tick_once(app)          # 実配線 (service.py) をそのまま使う
    assert app.notifier.sent == []


def test_commit_post_notifies_gate_reject_streak(tmp_path):
    """対になる肯定側の pin。commit-post 経路では実際に通知が出ることを見る
    (上のテストだけだと「どこからも呼ばない」実装でも green になるため)。"""
    conn, loop, runner, tp = _completed_hold_loop(tmp_path, threshold=1)
    _intent(conn, "open", "rejected", "risk_gate")
    loop.run_once()
    assert len(loop.notifier.sent) == 1


@pytest.mark.parametrize("stage", ["evaluate", "notify", "state_update"])
def test_commit_post_alert_exception_never_changes_finalized_mission_to_failed(
        tmp_path, stage, monkeypatch):
    conn, loop, runner, tp = _completed_hold_loop(tmp_path, threshold=1)
    _intent(conn, "open", "rejected", "risk_gate")
    _inject_alert_failure(loop, stage, monkeypatch)
    assert loop.run_once() == {"result": "hold", "order_id": None, "reasons": []}
    assert conn.execute("SELECT status FROM missions ORDER BY id DESC LIMIT 1").fetchone()[0] == "completed"
```

同じ Step 1 で `tests/core/test_executor_category.py` を新設し、21 site を次の
1 本の table-driven test で管理する。`route` は同ファイル内の callable で、
各 callable はコメントに示した既存 fixture と public/commit-core API を呼び、
戻り値を `(conn, intent_id)` に統一する。

```python
@pytest.mark.parametrize(
    ("site", "route", "expected"),
    [
        ("executor.py:369", route_origin_non_scheduler, "origin"),
        ("executor.py:379", route_non_trade_mission, "mission"),
        ("executor.py:410", route_legacy_open_no_account, "risk_gate"),
        ("executor.py:427", route_legacy_open_bad_conversion, "risk_gate"),
        ("executor.py:452", route_open_risk_gate_reject, "risk_gate"),
        ("executor.py:464", route_open_accepted, None),
        ("executor.py:565", route_open_snapshot_stale, "execution"),
        ("executor.py:574", route_open_snapshot_no_account, "risk_gate"),
        ("executor.py:588", route_open_exposure_not_covered, "execution"),
        ("executor.py:609", route_open_pair_not_covered, "execution"),
        ("executor.py:622", route_open_currency_not_covered, "execution"),
        ("executor.py:686", route_legacy_close_not_open, "execution"),
        ("executor.py:692", route_legacy_close_accepted, None),
        ("executor.py:789", route_close_snapshot_not_open, "execution"),
        ("executor.py:800", route_close_snapshot_missing, "execution"),
        ("executor.py:812", route_close_snapshot_stale, "execution"),
        ("executor.py:818", route_close_snapshot_accepted, None),
        ("executor.py:825", route_close_snapshot_mismatch, "execution"),
        ("executor.py:864", route_cancel_not_pending, "execution"),
        ("executor.py:870", route_cancel_pending, None),
        ("trade_loop.py:318", route_commit_pre_snapshot_failure, "execution"),
    ],
    ids=lambda value: value if isinstance(value, str) else None,
)
def test_every_set_gate_result_site_persists_category(
        tmp_path, site, route, expected):
    conn, iid = route(tmp_path)
    row = conn.execute(
        "SELECT gate_result,reject_category FROM trade_intents WHERE id=?",
        (iid,),
    ).fetchone()
    assert row is not None, f"{site} did not persist its intent"
    assert row["reject_category"] == expected
    assert row["gate_result"] == ("accepted" if expected is None else "rejected")
```

各 route の入力と DB 前提は曖昧にせず次で固定する。実装時には表の「既存
fixture / 呼出し」をそのまま callable 本体へ移す（各 route は 5--15 行で、
最後に `SELECT MAX(id)` を読み `(conn, iid)` を返す）。

| route | intent / DB 前提 | 既存 fixture / 呼出し |
|---|---|---|
| `route_origin_non_scheduler` | OPEN, `Origin.ASK`; fresh account | `test_executor._setup`; `handle_intent` |
| `route_non_trade_mission` | OPEN, scheduler; mission loop=`ask` | `_setup`; `missions.start(...,"ask",...)`; `handle_intent` |
| `route_legacy_open_no_account` | OPEN; `DELETE account_snapshots` | `_setup`; `handle_intent` |
| `route_legacy_open_bad_conversion` | OPEN; fresh account; `rate_fn` raises `DataUnhealthy` | `_setup`; replace `cycle_rate_fn`; `handle_intent` |
| `route_open_risk_gate_reject` | OPEN, `take_profit=148.30` | `_setup`; `handle_intent` |
| `route_open_accepted` | valid OPEN | `_setup`; `handle_intent` |
| `route_open_snapshot_stale` | valid OPEN; captured_at=`NOW-999s` | `test_executor_snapshot._make_executor`; `open_from_snapshot(...,max_snapshot_age_sec=5)` |
| `route_open_snapshot_no_account` | valid snapshot then delete account | `_make_executor`; `open_from_snapshot` |
| `route_open_exposure_not_covered` | snapshot後に EURUSD OPEN を insert | `_make_executor`; `_insert_open_order`; `open_from_snapshot` |
| `route_open_pair_not_covered` | USDJPY intent + EURUSD snapshot | `_make_executor`; `open_from_snapshot` |
| `route_open_currency_not_covered` | USDJPY spec + empty rates | `_make_executor`; `ExecutionSnapshot`; `open_from_snapshot` |
| `route_legacy_close_not_open` | CLOSE order_id=999999 | `_setup`; `handle_intent` |
| `route_legacy_close_accepted` | valid OPEN order then CLOSE | `_setup`; `handle_intent` |
| `route_close_snapshot_not_open` | cancelled row + CLOSE | `_make_executor`; `close_from_snapshot` |
| `route_close_snapshot_missing` | OPEN row + snapshot=None | `_make_executor`; `close_from_snapshot` |
| `route_close_snapshot_stale` | OPEN row + old `CloseSnapshot` | `_make_executor`; `close_from_snapshot(...,max_snapshot_age_sec=5)` |
| `route_close_snapshot_accepted` | OPEN row + matching fresh `CloseSnapshot` | `_make_executor`; `close_from_snapshot` |
| `route_close_snapshot_mismatch` | OPEN row + different order_id の fresh `CloseSnapshot` | `_make_executor`; `close_from_snapshot` |
| `route_cancel_not_pending` | CANCEL order_id=999999 | `_setup`; `handle_intent` |
| `route_cancel_pending` | valid limit OPENで pending_fill を作り CANCEL | `_setup`; `handle_intent` |
| `route_commit_pre_snapshot_failure` | completed OPEN; `gather_open_snapshot` raises `DataUnhealthy` | `test_trade_loop._loop`; `run_once` |

`executor.py:818` と `executor.py:825` は前述の上書き関係があるため、818 の
route は matching snapshot で正常終了させ、825 の route だけ mismatch を渡す。
これにより各変異を別々に殺せる。commit-pre が正しく作った snapshot を同じ
intent に渡す production 配線だけでは 609/622/825 は到達しないが、既存の
commit-core API 防御テストと同じく壊れた境界入力を直接渡して到達させる。

- [ ] **Step 2: 失敗を確認し、call site 件数を再監査する**

```bash
uv run pytest tests/store/test_db.py -k "trade_intents or alert_state" -v
uv run pytest tests/store/test_intents.py tests/store/test_alert_state.py -v
uv run pytest tests/loops/test_gate_reject_alert.py -v
rg -n 'intents_store\.set_gate_result\(' src/agentic_fx/core/executor.py src/agentic_fx/loops/trade_loop.py
rg -n 'accepted=True' src/agentic_fx/core/executor.py src/agentic_fx/loops/trade_loop.py
```

Expected: 列/table/helper が無いため FAIL。束 B 合流後も **21 / accepted 4** が正常値。21 以外なら束 B が call site を増減した事実と新しい grep 全行を指揮者へ報告し、表と parametrize を現物に同期してから再開する。

- [ ] **Step 3: 最小実装**

`store/db.py` は `trade_intents` を定数 DDL + rebuild migration にする。新 DDL の CHECK は legacy の `action IS NULL` を先頭で除外する。

同時に `tests/store/test_db.py` の Task 15 完了時点 16 表集合へ
`"alert_state"` を追加し、関数名を次のように更新する。

```python
EXPECTED = {
    "ohlcv_cache", "ohlcv_history", "missions", "trade_intents", "orders",
    "reflections", "account_snapshots", "improvement_backlog",
    "improvement_runs", "econ_events", "approval_requests", "news_sources",
    "backtest_runs", "analysis_runs", "signals", "reflection_attempts",
    "alert_state",
}


def test_init_creates_all_17_tables(tmp_path):
    conn = connect(tmp_path / "agentic.db")
    init_db(conn)
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%'").fetchall()
    assert {r["name"] for r in rows} == EXPECTED
    assert TABLE_NAMES == frozenset(EXPECTED)
```

省略記号は説明用でありソースには書かない。Task 15 までの 16 文字列を温存して
17 表にするため、Task 17 の Step 4 がこの task 単独で green になる。

```sql
CREATE TABLE IF NOT EXISTS trade_intents (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  mission_id INTEGER NOT NULL REFERENCES missions(id),
  payload_json TEXT NOT NULL,
  action TEXT,
  gate_result TEXT,
  reject_reason TEXT,
  reject_category TEXT,
  created_at TEXT NOT NULL,
  CHECK (action IS NULL
         OR gate_result IS NULL
         OR (gate_result='accepted' AND reject_category IS NULL)
         OR (gate_result='rejected' AND reject_category IN
             ('risk_gate','origin','mission','execution')))
);
CREATE TABLE IF NOT EXISTS alert_state (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
```

`trade_intents` は `orders.intent_id` から参照されるため RENAME 先行は禁止。
SQLite は RENAME 時に `orders` の DDL まで旧名へ書き換える。次の順序をそのまま
実装する: 新名で作成 → コピー →旧表 DROP → 新表を本来名へ RENAME。

```python
_TRADE_INTENTS_NEW_DDL = """
CREATE TABLE trade_intents_new (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  mission_id INTEGER NOT NULL REFERENCES missions(id),
  payload_json TEXT NOT NULL,
  action TEXT, gate_result TEXT, reject_reason TEXT, reject_category TEXT,
  created_at TEXT NOT NULL,
  CHECK (action IS NULL OR gate_result IS NULL
         OR (gate_result='accepted' AND reject_category IS NULL)
         OR (gate_result='rejected' AND reject_category IN
             ('risk_gate','origin','mission','execution')))
);
"""

def _migrate_trade_intents_observability(conn: sqlite3.Connection) -> None:
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(trade_intents)")}
    new_exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' "
        "AND name='trade_intents_new'").fetchone() is not None
    if {"action", "reject_category"} <= cols and not new_exists:
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        cols = {r["name"] for r in conn.execute(
            "PRAGMA table_info(trade_intents)")}
        new_exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='trade_intents_new'").fetchone() is not None
        if {"action", "reject_category"} <= cols and not new_exists:
            conn.commit()
            return
        if new_exists:
            conn.execute("DROP TABLE trade_intents_new")
        old_count = conn.execute("SELECT COUNT(*) FROM trade_intents").fetchone()[0]
        # executescript は暗黙 commit し得るため migration transaction 内では使わない。
        conn.execute(_TRADE_INTENTS_NEW_DDL)
        conn.execute(
            "INSERT INTO trade_intents_new "
            "(id,mission_id,payload_json,action,gate_result,reject_reason,"
            "reject_category,created_at) "
            "SELECT id,mission_id,payload_json,NULL,gate_result,reject_reason,"
            "NULL,created_at FROM trade_intents")
        new_count = conn.execute(
            "SELECT COUNT(*) FROM trade_intents_new").fetchone()[0]
        if new_count != old_count:
            raise RuntimeError("trade_intents migration row count mismatch")
        conn.execute("DROP TABLE trade_intents")
        conn.execute("ALTER TABLE trade_intents_new RENAME TO trade_intents")
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
```

`init_db()` から Task 13/16/19/15 migration 後に呼び、`TABLE_NAMES` に
`alert_state` を足す。Task 13/16/19 の RENAME 先行は参照元が無い場合に限る
ことを各束の警告コメントでも明記する。

`store/intents.py` を更新する。

```python
def insert(conn: sqlite3.Connection, mission_id: int, payload: dict,
           now: datetime, *, action: str) -> int:
    cur = conn.execute(
        "INSERT INTO trade_intents "
        "(mission_id,payload_json,action,created_at) VALUES (?,?,?,?)",
        (mission_id, json.dumps(payload, ensure_ascii=False),
         action, now.isoformat()))
    conn.commit()
    return cur.lastrowid


def set_gate_result(conn: sqlite3.Connection, intent_id: int, *,
                    accepted: bool, reject_reason: str | None,
                    reject_category: str | None) -> None:
    conn.execute(
        "UPDATE trade_intents SET gate_result=?,reject_reason=?,"
        "reject_category=? WHERE id=?",
        ("accepted" if accepted else "rejected", reject_reason,
         reject_category, intent_id))
    conn.commit()
```

`executor.py:355` は payload から再抽出せず `action=intent.action.value` を渡す。上表の全 call site に `reject_category=` を足し、accepted 4 箇所は `None`。この変更以外の判定式・return・activity・order 操作は変更しない。

`store/alert_state.py` を作成する。

```python
from __future__ import annotations
import sqlite3
from datetime import datetime

GATE_REJECT_STREAK_KEY = "gate_reject.last_notified_streak_id"
_KNOWN_KEYS = frozenset({GATE_REJECT_STREAK_KEY})


def get(conn: sqlite3.Connection, key: str) -> str | None:
    if key not in _KNOWN_KEYS:
        raise ValueError(f"unknown alert_state key: {key}")
    row = conn.execute("SELECT value FROM alert_state WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


def set(conn: sqlite3.Connection, key: str, value: str, *, now: datetime) -> None:
    if key not in _KNOWN_KEYS:
        raise ValueError(f"unknown alert_state key: {key}")
    conn.execute(
        "INSERT INTO alert_state (key,value,updated_at) VALUES (?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value,"
        "updated_at=excluded.updated_at", (key, value, now.isoformat()))
    conn.commit()
```

`trade_loop.py` に private helper と commit-post 呼び出しを追加する。判定は必ず `_conn_supervisor`、通知は lock 非保持、更新だけ短い lock と `self.conn`。

```python
def gate_reject_streak(conn: sqlite3.Connection) -> tuple[int, int, str | None]:
    streak_id = int(conn.execute(
        "SELECT COALESCE(MAX(id),0) FROM trade_intents "
        "WHERE action='open' AND gate_result='accepted'").fetchone()[0])
    count = int(conn.execute(
        "SELECT COUNT(*) FROM trade_intents WHERE action='open' "
        "AND gate_result='rejected' AND id > ?", (streak_id,)).fetchone()[0])
    row = conn.execute(
        "SELECT reject_category,COUNT(*) c FROM trade_intents "
        "WHERE action='open' AND gate_result='rejected' AND id > ? "
        "GROUP BY reject_category ORDER BY c DESC,reject_category ASC LIMIT 1",
        (streak_id,)).fetchone()
    return streak_id, count, row["reject_category"] if row else None


def _notify_gate_reject_streak(self) -> None:
    try:
        streak_id, count, dominant = gate_reject_streak(self._conn_supervisor)
        if count < self.settings.alert.consecutive_gate_reject:
            return
        with self._core_lock:
            last = alert_state.get(
                self.conn, alert_state.GATE_REJECT_STREAK_KEY)
        if last == str(streak_id):
            return
        self.notifier.send(
            f"[agentic-fx] open intent が {count} 件連続で却下されています"
            f" (最多カテゴリ: {dominant or 'unknown'})")
        with self._core_lock:
            alert_state.set(
                self.conn, alert_state.GATE_REJECT_STREAK_KEY,
                str(streak_id), now=self.clock.now())
    except Exception:  # noqa: BLE001 — finalize 済み Mission を失敗に見せない
        _log.exception("gate rejection alert evaluation failed")
```

commit-post の deferred 通知 drain 後、`out is None` 判定より前に `self._notify_gate_reject_streak()` を置く。scheduler / maintenance / `_scheduler_tick_once` には一切追加しない。通知失敗は `send()` で helper の except へ飛ぶため `alert_state.set()` に到達しない。

- [ ] **Step 4: 成功を確認**

```bash
uv run pytest tests/store/test_db.py tests/store/test_intents.py tests/store/test_alert_state.py -q
uv run pytest tests/core/test_executor.py tests/core/test_executor_snapshot.py tests/loops/test_trade_loop.py tests/loops/test_trade_loop_phases.py tests/loops/test_gate_reject_alert.py -q
uv run pytest tests/core/test_executor_category.py -q
rg -n 'intents_store\.set_gate_result\(' src/agentic_fx/core/executor.py src/agentic_fx/loops/trade_loop.py
rg -n 'reject_category=' src/agentic_fx/core/executor.py src/agentic_fx/loops/trade_loop.py
```

Expected: 全件 PASS。2 つの grep の call site 件数が一致し accepted=True の全 4 箇所が `reject_category=None`。scheduler thread 検査は thread identity で green になり、lock 所有の有無だけを見ていない。

- [ ] **Step 5: 変異テスト**

```bash
find . -name __pycache__ -type d -not -path "./.venv/*" -exec rm -rf {} +
```

| 変異 | red になる固有テスト |
|---|---|
| `streak_id` を最新 rejected id に差し替える | `test_threshold_notifies_once_per_accepted_streak` |
| SQL ②へ `reject_category='risk_gate'` を足す | `test_streak_count_does_not_filter_reject_category` |
| `action='open'` を外す | `test_close_rejection_is_not_counted` |
| 通知成功後の `alert_state.set` を削除 | `test_threshold_notifies_once_per_accepted_streak` |
| 通知失敗を捕捉後も id を更新する | `test_notification_failure_does_not_update_streak_id` |
| 判定接続を `self.conn` (`conn_core`) にする | `test_gate_alert_reads_only_conn_supervisor` |
| `_notify_gate_reject_streak` を `_scheduler_tick_once` / maintenance から呼ぶ | **`test_scheduler_tick_never_notifies_gate_reject_streak`** (旧 `test_gate_alert_notifier_is_never_called_on_scheduler_thread` は vacuous で**この変異を殺せなかった** — ローカル LLM レビューで検出) |
| commit-post から `_notify_gate_reject_streak` の呼び出しを削除する (どこからも呼ばない) | **`test_commit_post_notifies_gate_reject_streak`** (上の否定側テストだけでは「呼ばない実装」が生存するため対で持つ) |
| migration を 2 回目に no-op でなく再実行し既存行を壊す | **`test_trade_intents_migration_is_idempotent`** (旧版は空 DB で `COUNT(*)==0` を見るだけで**この変異を殺せなかった**) |
| SQL ① の `MAX(id)` を `MIN(id)` に変える (**変異リストに無かった追加分**。`MIN` だと streak_id が最初の accepted に固定され、accepted が入っても連続が切れず鳴り続ける) | **`test_streak_id_is_previous_accepted_open_id`** (旧版は accepted 1 行で `MAX`/`MIN` が同値になり**生存していた** — ローカル LLM が検出) |
| SQL ③ の `ORDER BY c DESC` を `ASC` に変える (**追加分**) | **`test_dominant_category_is_the_majority_not_a_tie`** (件数に差を付けて非決定性を排したので殺せる) |
| commit-post helper の `except Exception` を外す（evaluate） | `test_commit_post_alert_exception_never_changes_finalized_mission_to_failed[evaluate]` |
| commit-post helper の `except Exception` を外す（notify） | `test_commit_post_alert_exception_never_changes_finalized_mission_to_failed[notify]` |
| commit-post helper の `except Exception` を外す（state update） | `test_commit_post_alert_exception_never_changes_finalized_mission_to_failed[state_update]` |
| `action IS NULL` を CHECK から外す | `test_trade_intents_migration_accepts_legacy_rejected_row_with_null_action` |

call site category 変異は Step 1 の
`test_every_set_gate_result_site_persists_category` の **21 行へ 1 箇所ずつ独立に**
当てる。accepted 4 箇所は `None -> "risk_gate"`、rejected 17 箇所は表の期待値を
別 category に変える。killer 名は全 site 共通の上記 parametrize test で、pytest
node id の `site`（例: `executor.py:369`）が 1:1 の識別子になる。818/825 は
matching/mismatch route を分け、上書き後の同じ行を誤って検査しない。

各変異で `grep -n -A2 -B1 'set_gate_result' <対象ファイル>` を表示し、固有テストだけが red になること、revert 後 green を ledger に記録する。

- [ ] **Step 6: コミット**

```bash
git add src/agentic_fx/store/db.py src/agentic_fx/store/intents.py \
  src/agentic_fx/store/alert_state.py src/agentic_fx/core/executor.py \
  src/agentic_fx/loops/trade_loop.py src/agentic_fx/config.py \
  config/settings.yaml.example tests/store/test_db.py tests/store/test_intents.py \
  tests/store/test_alert_state.py tests/loops/test_gate_reject_alert.py \
  tests/core/test_executor_category.py \
  tests/core/test_executor.py tests/core/test_executor_snapshot.py \
  tests/loops/test_trade_loop_phases.py
git commit -m "feat: gate rejection の連続区間を commit-post で通知する (plan9 task17)"
```

---

### Task 18: `llama_swap.timeout_sec` cold / warm 実測

**Files:**
- Create: `docs/operations/llama-swap-timeout-measurement-2026-08-11.md`
- Modify: `config/settings.yaml:33-36`（実測結果が既定 300 秒からの変更を要求する場合のみ）
- Modify: `config/settings.yaml.example:33-36`（同じ判断を example 既定へ採用する場合のみ）
- Test: `docs/operations/llama-swap-timeout-measurement-2026-08-11.md` の再現コマンドと生計測表

**Interfaces:**
- Consumes: `settings.runner.trade.model: str`、`settings.runner.improve.model: str`、`settings.llama_swap.base_url: str`、現在値 `settings.llama_swap.timeout_sec: float`、llama-swap の TTL / 同時ロード設定
- Produces: 環境付き cold/warm latency 表、採用 timeout と根拠。コードの関数シグネチャは変更しない

- [ ] **Step 1: 失敗する検証を書く**

計測前は成果物ファイルを作らず、必要な全見出しと数値行を要求する検証を実行する。これにより仮の timeout 値や空欄を成果物へ入れない。

```bash
test -f docs/operations/llama-swap-timeout-measurement-2026-08-11.md && \
rg -q '^## 環境$' docs/operations/llama-swap-timeout-measurement-2026-08-11.md && \
rg -q '^## 計測結果$' docs/operations/llama-swap-timeout-measurement-2026-08-11.md && \
rg -q '^## 判断$' docs/operations/llama-swap-timeout-measurement-2026-08-11.md && \
rg -q 'selected timeout_sec: [0-9]+(\.[0-9]+)?' docs/operations/llama-swap-timeout-measurement-2026-08-11.md
```

- [ ] **Step 2: 未計測であることを確認**

Expected: exit 1 (`test: ... No such file or directory`)。これが計測前の red であり、timeout 値を先に決めていない証拠になる。

- [ ] **Step 3: cold / warm を実測して記録する**

まず環境を記録する。

```bash
uv run python - <<'PY'
from pathlib import Path
from agentic_fx.config import load_settings
s = load_settings(Path('config/settings.yaml'))
print('base_url=', s.llama_swap.base_url)
print('trade_model=', s.runner.trade.model)
print('improve_model=', s.runner.improve.model)
print('current_timeout_sec=', s.llama_swap.timeout_sec)
PY
nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader
```

llama-swap の実起動設定から TTL と同時ロード数を確認し、プロセス引数または設定ファイルの該当行を記録へ転記する。TTL unload がログまたは `/models` / `/props` で確認できるまで待ち、単なる時間経過を cold と推測しない。

各モデル alias について、unload 確認直後に cold、その完了直後に warm を同じ payload で採る。

```bash
uv run python - <<'PY'
import json, os, time
from pathlib import Path
import httpx
from agentic_fx.config import load_settings
s = load_settings(Path('config/settings.yaml'))
url = s.llama_swap.base_url.rstrip('/') + '/chat/completions'
models = [("trade", s.runner.trade.model)]
if s.runner.improve.model != s.runner.trade.model:
    models.append(("improve", s.runner.improve.model))
for role, model in models:
    with open("/dev/tty", "r", encoding="utf-8") as tty_in:
        print(f"{model} の TTL unload をログで確認後 Enter: ",
              end="", flush=True)
        tty_in.readline()
    payload = {"model": model, "max_tokens": 1,
               "messages": [{"role": "user", "content": "ping"}]}
    for phase in ("cold", "warm"):
        started = time.monotonic()
        r = httpx.post(url, json=payload, timeout=None)
        elapsed = time.monotonic() - started
        print(json.dumps({"role": role, "phase": phase, "model": model,
                          "elapsed_sec": round(elapsed, 3),
                          "status": r.status_code}))
        r.raise_for_status()
PY
```

ヒアドキュメントで stdin は EOF になるため `input()` は呼ばない。対話入力だけ
上のコードのとおり `/dev/tty` から読む。

trade と improve が同一 alias なら 1 組だけ測り、表の improve 行へ「same as trade; no second cold load」と記録する。異なるならスクリプトが improve の TTL unload 確認を別に要求して **2 回目の cold**を測る。生 JSON 出力を計測文書へ貼り、環境、結果、判断の全欄を実値で作成する。

- [ ] **Step 4: 判断と設定反映を確認する**

成功した cold/warm の最大値を `W` とし、安全余裕 `max(30, W*0.25)` を足して秒単位で切り上げる。採用値が現行 300 秒以下なら設定変更を行わず「300 を維持」と記録する。300 秒を超える、または運用上過大で明確に短縮できるという実測根拠がある場合だけ `config/settings.yaml` を運用値として更新し、プロジェクト既定も変える判断なら `settings.yaml.example` も同じ値へ更新する。

```bash
! rg -q '未計測|未確定|仮値' docs/operations/llama-swap-timeout-measurement-2026-08-11.md
uv run python - <<'PY'
from pathlib import Path
from agentic_fx.config import load_settings
for p in (Path('config/settings.yaml'), Path('config/settings.yaml.example')):
    if p.exists():
        print(p, load_settings(p).llama_swap.timeout_sec)
PY
```

Expected: placeholder 検査 exit 0。文書の selected timeout と変更対象ファイルの値が一致し、既知 warm 31.16 秒との差を環境差として説明している。

- [ ] **Step 5: 変異テスト**

| 変異 | red になる検査 |
|---|---|
| cold 行を削除 | `rg -q 'cold after confirmed TTL unload' docs/operations/llama-swap-timeout-measurement-2026-08-11.md` |
| warm 行を削除 | `rg -q 'immediate warm' docs/operations/llama-swap-timeout-measurement-2026-08-11.md` |
| 異なる improve model の cold 値を削除 | `test -z "$IMPROVE_DIFF" || rg -q 'improve.*[0-9].* [0-9]' ...`（実 alias と値へ置換した実コマンドを ledger に保存） |
| VRAM / quantization / simultaneous load / TTL のいずれかを削除 | `rg -q 'GPU / VRAM:.*[0-9]'`、`rg -q 'quantization:.*[^ ]'`、`rg -q 'simultaneous loaded model limit:.*[0-9]'`、`rg -q 'TTL setting:.*[0-9]'` |
| selected timeout を計測最大値以下にする | 文書中の生 JSON から最大値を読み、`selected > max` を assert する `uv run python - <<'PY' ... PY` を ledger に保存 |

各文書変異は 1 件ずつ当て、該当 `rg` / Python 検査が red、revert 後 green になることを記録する。

- [ ] **Step 6: コミット**

```bash
git add docs/operations/llama-swap-timeout-measurement-2026-08-11.md
git add config/settings.yaml.example  # 実測判断で既定を変更した場合だけ
git commit -m "docs: llama-swap timeout の cold warm 実測を記録する (plan9 task18)"
```

`config/settings.yaml` は gitignore 対象の運用設定なので commit しない。変更した場合はコミット本文に「local operational setting updated」と記録する。

---

### 束 E 自己レビュー

#### ① D1 / D3 / D8 の変異対応表

| 設計項目 | Task / Step | killer test / 検査 |
|---|---|---|
| D1 上限判定削除 | 15 / 5 | `test_reflection_retries_to_limit_then_stops` |
| D1 attempts 加算削除 | 15 / 5 | `test_bump_returns_incremented_attempt_count` |
| D1 abandon activity 削除 | 15 / 5 | `test_reflection_abandoned_activity_written_once_at_limit` |
| D1 LEFT JOIN 条件削除 | 15 / 5 | `test_failed_old_order_does_not_starve_later_order` |
| D1 上限を大定数化 | 15 / 5 | `test_reflection_retries_to_limit_then_stops` |
| D1 成功 clear 削除 | 15 / 5 | `test_success_clears_prior_attempt_row` |
| D3 閾値判定削除 | 17 / 5 | `test_threshold_notifies_once_per_accepted_streak` |
| D3 streak_id を最新 id 化 | 17 / 5 | `test_threshold_notifies_once_per_accepted_streak` |
| D3 last id 更新削除 | 17 / 5 | `test_threshold_notifies_once_per_accepted_streak` |
| D3 action=open 削除 | 17 / 5 | `test_close_rejection_is_not_counted` |
| D3 category=risk_gate を SQL ②へ追加 | 17 / 5 | `test_streak_count_does_not_filter_reject_category` |
| D3 call site category 誤値（適用範囲全体） | 17 / 5 | site 群 **21** の category 単独テスト (下の parametrize 表で全件) |
| D3 通知失敗後も id 更新 | 17 / 5 | `test_notification_failure_does_not_update_streak_id` |
| D3 判定を conn_core 化 | 17 / 5 | `test_gate_alert_reads_only_conn_supervisor` |
| D3 scheduler thread で評価 | 17 / 5 | `test_scheduler_tick_never_notifies_gate_reject_streak` |
| D3 commit-post 例外捕捉削除 | 17 / 5 | `test_commit_post_alert_exception_never_changes_finalized_mission_to_failed[evaluate/notify/state_update]` |
| D8 cold / warm の片方欠落 | 18 / 5 | 計測文書の cold / warm heading 検査 |
| D8 異なる 2 モデルの second cold 欠落 | 18 / 5 | improve alias/value 検査 |
| D8 環境情報欠落 | 18 / 5 | VRAM / quantization / simultaneous load / TTL の個別 `rg` |
| D8 計測前の timeout 決め打ち | 18 / 1-4 | 成果物不在の red → 生 JSON → 判断の順序と `selected > observed max` 検査 |

#### ② プレースホルダ不在の確認

Task 15/17 の表数更新、旧 signature 呼出し、alert fixture、scheduler 実配線、
`gate_reject_streak` の import と配置は具体化した。Task 17 の category 検査は
21 行の parametrize に集約し、site id・到達条件・期待 category を 1:1 にした。
Task 18 は計測前に成果物自体を作らず、実測後に実値だけで作成するため仮の
timeout 値を置かない。

#### ③ 束 A Task 4 の回帰ピン書換え

`plan-bundle-A.md` Task 4 / Step 1 の正確なテスト名 **`test_reflection_current_retry_behavior_is_pinned`** を削除せず、Task 15 Step 1 で **`test_reflection_retries_to_limit_then_stops`** へ改名・内容置換する。旧期待「同一 order で 2 回 `run_pending` → failed missions 2 本（以後も無制限）」を、新期待「`max_attempts=2` までは failed missions 2 本、3 回目は新 mission なし、attempts=2、abandoned activity は一度」へ変える。

#### ④ 他 task との競合ポイント

| ファイル | 競合 | 統合規則 |
|---|---|---|
| `store/db.py` | Task 13/19 が rebuild helper と `init_db`、Task 16 が OHLCV 分割、Task 15/17 が新表・trade_intents rebuild | 束 D/C を先に取り込み、既存 migration 呼び出しを一つも落とさず Task 15 → 17 を末尾へ直列追加。Task 16=15 表、Task 15=16 表、Task 17=17 表として各 task 内で `TABLE_NAMES` と `tests/store/test_db.py::EXPECTED` を同期 |
| `executor.py` | Task 6/7 が deadline 配線、Task 17 が insert/set_gate_result 引数のみ | 束 B 後に grep を再実行。Task 17 は判定・return・risk gate 呼出しを変えず keyword 引数だけ追加 |
| `trade_loop.py` | Task 4 が reason 出口、Task 17 が snapshot reject category と commit-post alert | Task 4 の failure reason と deferred notification drain を温存。alert は 357-366 の commit-post 内、scheduler から非到達 |
| `config.py` / example | Task 16 が cache retention、Task 15/17 が reflection/alert | strict model の top-level fields と example sections を同じ commit 系列で同期 |

#### ⑤ カバーできなかった項目

- ~~親骨格の「23 箇所」と現物「21 箇所」の不一致~~ → **解消済み**。指揮者が現物照合し **21 が正**と確定 (23 は定義行とコメント言及を含む誤記) で、親骨格・設計書とも訂正済み。Task 17 Step 2 の再計数は「21 以外なら束 B が call site を増減させた」という**変化の検出**が目的であり、21 は正常値である。
- `executor.py:609` / `622` / `825` の defensive branch は production の正常な
  commit-pre→commit-core 配線では到達しない。Step 1 は既存 snapshot API テストと
  同じく壊れた境界入力を直接渡して call site を到達させる。`818` は `825` に
  上書きされ得るため matching snapshot の正常 close で単独観測する。この制約を
  外して「production 経路だけで全 21 site」を殺すことはできない。
- Task 18 の実測値・採用 timeout は実機の TTL unload と GPU 環境が無ければ計画段階では確定できない。これは D8 が明示する計測 task の本質であり、仮値は置かなかった。
- notifier の成功判定は現行 `Notifier.send()` が例外なしを成功とする契約に依存する。HTTP 2xx 以外を内部で握り潰す実装へ将来変わる場合は、戻り値契約を別 task で明示する必要がある。

