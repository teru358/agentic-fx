# Phase 1 プラン 5: trade loop + service/shell 統合 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 取引判断 loop・reflection・policy 注入・対話シェル・サービス配線を統合し、**Phase 1 完成 = 学習モードでのフル自走** (FakeRunner で LLM なし E2E 検証可能) を達成する。

**Architecture:** `loops/` に trade_loop / reflection_cycle / prompts (.md)、`policy.py`、`commands.py` + `shell.py`、`service.py` の本実装 (build_app 配線 + scheduler スレッド + graceful shutdown)。Mission 実行は**常にサービスプロセス内の単一 Mission スロット (threading.Lock)**。

**Tech Stack:** stdlib (threading, signal) / プラン 1〜4 の契約を消費

## Global Constraints (設計書より)

- **ask Mission は回答専用** — output_schema に TradeIntent を渡さない。TradeIntent を生成できるのは scheduler 起動の定期 Mission のみ (origin 検証は executor 済み) (設計書 §5)
- Mission プロンプト構成: ①固定システム指示 + policy 末尾 4000 文字 ②状態サマリ (決定論的生成) ③ワンショット (臨時時のみ) (設計書 §5)
- **fail closed**: 全価格ソース不健全なら Mission を実行しない (設計書 §5)
- hold を含む**全 Mission・全 intent・transcript を記録** (設計書 §12/§13)
- 対話シェルは静かに (ログを流さない)。ログは `log` / `activity` の pull 型 (設計書 §8)
- `--daemon` 時のみ技術ログを stderr にも出す (journald 経路、プラン 1 の引き継ぎ)
- `policy/directives.md` は読み込み + サイズ警告のみ (`policy add` コマンドは Phase 2) (設計書 §15)
- prompts はコード外の .md (設計書 §11)
- graceful shutdown: スケジューラ停止 → 実行中 Mission の完了待ち (タイムアウト付き) → 終了 (設計書 §8)
- kill switch ラッチの解除は人間の明示操作 — シェル `killswitch reset` (プラン 2 の引き継ぎ)
- コミットメッセージ末尾: `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`

**消費する契約** (変更禁止): プラン 2 `Executor.handle_intent / close_order`、`Scheduler(tick, on_trade_mission, on_news_cycle, bars_fn)`、プラン 3 `PriceProvider / NewsCollector / EconCalendar / Rag`、プラン 4 `Mission / MissionResult / AgentRunner / FakeRunner / LocalRunner / ToolRegistry / 各 build()`

---

### Task 1: DB 接続のスレッド対応 + prompts + policy.py

**Files:**
- Modify: `src/agentic_fx/store/db.py`
- Modify: `src/agentic_fx/store/missions.py`
- Create: `src/agentic_fx/loops/__init__.py`, `src/agentic_fx/loops/prompts/trade_mission.md`, `src/agentic_fx/loops/prompts/ask_mission.md`, `src/agentic_fx/loops/prompts/reflection.md`
- Create: `src/agentic_fx/policy.py`
- Test: `tests/test_policy.py`, `tests/store/test_db.py` (追記), `tests/store/test_missions.py` (追記)

**Interfaces:**
- Produces:
  - `db.connect(db_path, *, check_same_thread: bool = False)` — さらに `PRAGMA busy_timeout=5000` を設定
  - **`missions.trigger` 列 + migration + `missions.start(..., trigger=None)`** — **本 Task で実施する** (Task 3 の `TradeLoop.run_once(trigger)` が依存するため、ここで先に入れないと Task 3 のテストが `TypeError` になる)。詳細は末尾の「追記 (2026-07-27)」の変更 1 を参照
  - **スレッド × 接続の設計** (単一接続の同時使用はしない):
    - **conn_core**: scheduler スレッドの tick と Mission 実行 (ask 含む) 専用。**`core_lock` (threading.RLock) が tick 全体と ask を排他**するため、conn_core に同時アクセスするスレッドは常に 1 つ
    - **conn_shell**: シェル (Commands) 専用の**別接続**。approve/reject/status 等はこちら。WAL + busy_timeout により conn_core との並行書き込みは SQLite 側で直列化される
    - Mission スロット = core_lock (「Mission 実行は常にサービスプロセス内の単一スロット」の実装。設計書 §7)
  - `class Policy(path: Path)`: `.tail(chars: int = 4000) -> str` (ファイル無しは空文字)、`.size_warning(limit_chars: int = 16000) -> str | None` (超過時に警告文)
  - `load_prompt(name: str) -> str` — `loops/prompts/<name>.md` を読む (`loops/prompts.py` 内)

- [ ] **Step 1: 失敗するテストを書く**

`tests/test_policy.py`:

```python
from agentic_fx.loops.prompts_loader import load_prompt
from agentic_fx.policy import Policy


def test_tail_missing_file_empty(tmp_path):
    assert Policy(tmp_path / "directives.md").tail() == ""


def test_tail_returns_last_chars(tmp_path):
    p = tmp_path / "directives.md"
    p.write_text("A" * 5000, encoding="utf-8")
    assert len(Policy(p).tail(4000)) == 4000


def test_size_warning(tmp_path):
    p = tmp_path / "directives.md"
    p.write_text("A" * 20000, encoding="utf-8")
    assert Policy(p).size_warning() is not None
    p.write_text("short", encoding="utf-8")
    assert Policy(p).size_warning() is None


def test_prompts_exist():
    for name in ("trade_mission", "ask_mission", "reflection"):
        text = load_prompt(name)
        assert len(text) > 100
```

`tests/store/test_db.py` に追記:

```python
def test_connection_usable_across_threads(tmp_path):
    import threading
    conn = connect(tmp_path / "t.db")
    init_db(conn)
    errors = []

    def use():
        try:
            conn.execute("SELECT 1").fetchone()
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    t = threading.Thread(target=use)
    t.start()
    t.join()
    assert errors == []
```

- [ ] **Step 2: テストが失敗することを確認**

Run: `uv run pytest tests/test_policy.py tests/store/test_db.py -v`
Expected: 新規分 FAIL

- [ ] **Step 3: 実装**

`src/agentic_fx/store/db.py` の `connect` を変更:

```python
def connect(db_path: Path, *, check_same_thread: bool = False) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, check_same_thread=check_same_thread)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn
```

`src/agentic_fx/policy.py`:

```python
"""policy/directives.md — 末尾 4000 文字注入 + サイズ警告 (設計書 §8)。追記は Phase 2。"""
from __future__ import annotations

from pathlib import Path


class Policy:
    def __init__(self, path: Path) -> None:
        self._path = path

    def tail(self, chars: int = 4000) -> str:
        if not self._path.exists():
            return ""
        return self._path.read_text(encoding="utf-8")[-chars:]

    def size_warning(self, limit_chars: int = 16000) -> str | None:
        if self._path.exists() and \
                len(self._path.read_text(encoding="utf-8")) > limit_chars:
            return (f"警告: {self._path} が {limit_chars} 文字を超えています。"
                    "注入は末尾 4000 文字のみです。手動で整理してください。")
        return None
```

`src/agentic_fx/loops/prompts_loader.py`:

```python
from __future__ import annotations

from pathlib import Path

_DIR = Path(__file__).parent / "prompts"


def load_prompt(name: str) -> str:
    return (_DIR / f"{name}.md").read_text(encoding="utf-8")
```

`src/agentic_fx/loops/prompts/trade_mission.md`:

```markdown
あなたは FX 取引判断エージェントです。1 時間毎に呼び出され、現在の市場状況を
ツールで調査して、次のいずれか 1 つを JSON で出力します。

## 判断の原則
- 情報軸は 2 つ: ニュース (search_news) とテクニカル (get_indicators / get_ohlcv)。
  必ず両方を確認してから判断すること
- 過去の振り返り (get_recent_reflections / search_reflections) から同じ失敗を
  繰り返さないこと
- 経済指標発表 (get_econ_calendar) の直前は新規エントリーを避けること
- 確信が持てないときは hold を選ぶこと。hold も立派な判断であり記録される
- 現在ポジション・未約定指値は get_positions で確認し、order_id を使って
  close (裁量クローズ) / cancel (指値取消) を提案できる

## 出力形式 (JSON のみ。説明文を JSON の外に書かない)
{"action": "open", "pair": "USDJPY", "direction": "long|short",
 "entry_type": "market|limit", "horizon": "day|swing",
 "limit_price": 148.20, "expires_in": "4h",
 "stop_loss": 147.80, "take_profit": 149.00,
 "confidence": 0.0-1.0, "reasoning": "判断根拠"}
または
{"action": "close", "order_id": 12, "reasoning": "..."}
{"action": "cancel", "order_id": 12, "reasoning": "..."}
{"action": "hold", "reasoning": "..."}

## 制約 (システム側で強制される)
- open には stop_loss / take_profit / horizon が必須
- 数量はあなたは決めない (システムが決定論的に算出する)
- リスク管理ルール (RR・SL 距離・ポジション数・損失上限) に違反する提案は
  自動的に却下される。reasoning に却下理由が返るので次回の判断に活かすこと
- horizon=day は当日クローズ前に強制決済、swing はリスク半分で数量が計算される
```

`src/agentic_fx/loops/prompts/ask_mission.md`:

```markdown
あなたは FX トレーディングシステムのアナリストです。ユーザーの質問に、
ツールで現在の市場状況・ポジション・ニュースを調査して日本語で答えてください。

重要: あなたはこの Mission では取引を実行できません。発注・クローズ・取消の
提案は次回の定期判断で扱われます。永続的な方針はユーザーが policy に記録します。

## 出力形式 (JSON のみ)
{"answer": "回答本文"}
```

`src/agentic_fx/loops/prompts/reflection.md`:

```markdown
クローズされたトレードの振り返りを書いてください。以下を簡潔に (200-400 字):
- 何を根拠にエントリーしたか (記録された reasoning から)
- 結果とその要因 (SL/TP/裁量/強制のどれで閉じたか、想定と何が違ったか)
- 次回に活かす教訓 (同じ局面で何を変えるか)

## 出力形式 (JSON のみ)
{"content": "振り返り本文"}
```

- [ ] **Step 4: テストが通ることを確認**

Run: `uv run pytest tests/test_policy.py tests/store/test_db.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/agentic_fx/store/db.py src/agentic_fx/policy.py \
  src/agentic_fx/loops tests/test_policy.py tests/store/test_db.py
git commit -m "feat: policy 注入・prompts (.md)・DB スレッド対応"
```

---

### Task 2: 状態サマリ + Mission スキーマ (loops/summary.py)

**Files:**
- Create: `src/agentic_fx/loops/summary.py`
- Test: `tests/loops/__init__.py`, `tests/loops/test_summary.py`

**Interfaces:**
- Produces:
  - `TRADE_INTENT_SCHEMA: dict` — `{"type": "object", "properties": {"action": {"enum": ["open", "close", "cancel", "hold"]}, ...}, "required": ["action", "reasoning"]}` (構造の最終防衛は `TradeIntent.from_llm_dict` — スキーマは LLM への再出力誘導用に緩めでよい)
  - `ANSWER_SCHEMA: dict` — `{"type": "object", "properties": {"answer": {"type": "string"}}, "required": ["answer"]}`
  - `build_state_summary(conn, broker, econ, clock, starting_balance: float) -> str` — 設計書 §5 ②: 残高・エクイティ / **累計 P&L (= equity − starting_balance)**・日次 P&L / 現在ポジション + 未約定指値 (order_id・horizon 付き) / 直近 10 件のトレード 1 行要約 (ペア/方向/損益/クローズ理由) / 24h 以内の経済指標

- [ ] **Step 1: 失敗するテストを書く**

`tests/loops/test_summary.py`:

```python
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import jsonschema

from agentic_fx.config import load_settings
from agentic_fx.core.contracts import FixedClock, OrderStatus
from agentic_fx.core.paper_broker import PaperBroker
from agentic_fx.loops.summary import (
    ANSWER_SCHEMA, TRADE_INTENT_SCHEMA, build_state_summary,
)
from agentic_fx.store import orders
from agentic_fx.store.db import connect, init_db

NOW = datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc)
SETTINGS = load_settings(
    Path(__file__).resolve().parents[2] / "config" / "settings.yaml.example")


def test_schemas_validate():
    jsonschema.validate({"action": "hold", "reasoning": "wait"},
                        TRADE_INTENT_SCHEMA)
    jsonschema.validate({"answer": "回答"}, ANSWER_SCHEMA)
    try:
        jsonschema.validate({"action": "buy"}, TRADE_INTENT_SCHEMA)
        raise AssertionError("should fail")
    except jsonschema.ValidationError:
        pass


def test_summary_contains_positions_and_trades(tmp_path):
    conn = connect(tmp_path / "t.db")
    init_db(conn)
    orders.insert(conn, pair="USDJPY", direction="long", entry_type="limit",
                  horizon="swing", status=OrderStatus.PENDING_FILL, now=NOW,
                  quantity=0.1, requested_price=148.2, stop_loss=147.8,
                  take_profit=149.0)
    oid = orders.insert(conn, pair="EURUSD", direction="short",
                        entry_type="market", horizon="day",
                        status=OrderStatus.CLOSED, now=NOW, quantity=0.1,
                        realized_pnl=-1200.0, close_reason="sl")
    econ = MagicMock()
    econ.upcoming.return_value = [
        {"ts": "2026-07-22T19:30:00+00:00", "country": "USD",
         "name": "CPI", "importance": 3}]
    text = build_state_summary(conn, PaperBroker(conn, SETTINGS,
                                                 FixedClock(NOW)),
                               econ, FixedClock(NOW),
                               SETTINGS.paper.starting_balance)
    assert "USDJPY" in text and "swing" in text      # 未約定指値 + horizon
    assert f"#{oid}" in text or str(oid) in text      # order_id 提示
    assert "sl" in text and "-1,200" in text          # 直近トレード要約
    assert "CPI" in text                              # 経済指標
    assert "1,000,000" in text or "1000000" in text   # 残高
    assert "累計" in text and "-1,200" in text        # 累計 P&L (realized -1200)
```

- [ ] **Step 2: テストが失敗することを確認**

Run: `uv run pytest tests/loops/test_summary.py -v`
Expected: FAIL (ImportError)

- [ ] **Step 3: 実装**

`src/agentic_fx/loops/summary.py`:

```python
"""状態サマリ (常時注入) と Mission 出力スキーマ — 設計書 §5。決定論的コードが生成。"""
from __future__ import annotations

import sqlite3
from datetime import datetime

from agentic_fx.core.contracts import Clock, OrderStatus
from agentic_fx.core.paper_broker import PaperBroker
from agentic_fx.core.accounting import daily_start_equity
from agentic_fx.datafeed.econ_calendar import EconCalendar
from agentic_fx.store import orders

TRADE_INTENT_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "action": {"enum": ["open", "close", "cancel", "hold"]},
        "pair": {"type": "string"},
        "direction": {"enum": ["long", "short"]},
        "entry_type": {"enum": ["market", "limit"]},
        "horizon": {"enum": ["day", "swing"]},
        "order_id": {"type": "integer"},
        "limit_price": {"type": ["number", "null"]},
        "expires_in": {"type": ["string", "null"]},
        "stop_loss": {"type": ["number", "null"]},
        "take_profit": {"type": ["number", "null"]},
        "confidence": {"type": ["number", "null"]},
        "reasoning": {"type": "string"},
    },
    "required": ["action", "reasoning"],
}

ANSWER_SCHEMA: dict = {
    "type": "object",
    "properties": {"answer": {"type": "string"}},
    "required": ["answer"],
}

_ACTIVE = (OrderStatus.OPEN, OrderStatus.PENDING_FILL,
           OrderStatus.PROTECTION_PENDING)


def build_state_summary(conn: sqlite3.Connection, broker: PaperBroker,
                        econ: EconCalendar, clock: Clock,
                        starting_balance: float) -> str:
    now = clock.now()
    balance, equity = broker.equity()
    day_start = daily_start_equity(conn, now)
    daily_pnl = (equity - day_start) if day_start else 0.0
    total_pnl = equity - starting_balance

    lines = ["## 現在の状態 (システム生成)",
             f"- 時刻: {now.isoformat()}",
             f"- 残高: {balance:,.0f} / エクイティ: {equity:,.0f}",
             f"- 累計損益: {total_pnl:+,.0f} / 日次損益: {daily_pnl:+,.0f}"]

    active = orders.list_by_status(conn, *_ACTIVE)
    if active:
        lines.append("- ポジション/指値:")
        for r in active:
            entry = r["avg_fill_price"] or r["requested_price"]
            lines.append(
                f"  - #{r['id']} {r['pair']} {r['direction']} "
                f"{r['status']} {r['quantity']}lot @{entry} "
                f"SL={r['stop_loss']} TP={r['take_profit']} "
                f"horizon={r['horizon']}")
    else:
        lines.append("- ポジション/指値: なし")

    closed = conn.execute(
        "SELECT * FROM orders WHERE status='closed' "
        "ORDER BY closed_at DESC LIMIT 10").fetchall()
    if closed:
        lines.append("- 直近トレード:")
        for r in closed:
            lines.append(
                f"  - #{r['id']} {r['pair']} {r['direction']} "
                f"pnl={r['realized_pnl']:+,.0f} ({r['close_reason']})")

    events = econ.upcoming(hours=24)
    if events:
        lines.append("- 24h 以内の経済指標:")
        for e in events[:8]:
            stars = "★" * int(e["importance"] or 0)
            lines.append(f"  - {e['ts']} {e['country']} {e['name']} {stars}")
    return "\n".join(lines)
```

- [ ] **Step 4: テストが通ることを確認**

Run: `uv run pytest tests/loops/test_summary.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/agentic_fx/loops/summary.py tests/loops
git commit -m "feat: 状態サマリ生成 + Mission 出力スキーマ (intent/answer 分離)"
```

---

### Task 3: 取引判断 loop (loops/trade_loop.py)

**Files:**
- Create: `src/agentic_fx/loops/trade_loop.py`
- Test: `tests/loops/test_trade_loop.py`

**Interfaces:**
- Produces: `class TradeLoop`:
  - `__init__(self, *, conn, runner: AgentRunner, settings: Settings, executor: Executor, provider: PriceProvider, econ: EconCalendar, policy: Policy, activity: ActivityLog, notifier: Notifier, clock: Clock)`
  - `run_once(self, trigger: str = "cron") -> dict | None` — 取引判断 Mission (scheduler の `on_trade_mission` に差し込む)。`trigger` は起動理由で `missions.start` にそのまま渡す (**`missions.trigger` 列と `start(trigger=)` は Task 1 で追加済み**。未実施ならそちらを先に済ませること)。Phase 2 のシグナル起動に向けた接合点であり、背景は末尾の「追記 (2026-07-27)」にある:
    1. **fail closed**: `provider.healthcheck(settings.pairs[0])` が `DataUnhealthy` → activity SYSTEM `data_unhealthy` + notifier + **Mission を実行せず None**
    2. prompt = `load_prompt("trade_mission")` + policy.tail(4000) + `build_state_summary(...)`
    3. `Mission(tools=[get_ohlcv, get_indicators, search_news, get_econ_calendar, get_positions, get_account, get_recent_reflections, search_reflections], output_schema=TRADE_INTENT_SCHEMA, max_turns/timeout=settings.llama_swap)`
    4. `missions.start(loop="trade")` → `runner.run` を **try/except で包み、例外は `failed` の MissionResult に正規化** (AgentRunner 契約は無例外を保証しない) → **finally で必ず `missions.finish`** (status・output・transcript — 設計書 §13「全 MissionResult を保存」)。`finish` 自体の失敗は技術ログ exception
    5. runner 失敗 (completed 以外) → activity AGGREGATE `mission_failed` + notifier + None
    6. `TradeIntent.from_llm_dict(output, origin=Origin.SCHEDULER)` — `IntentParseError` → missions は completed のまま activity AGGREGATE `intent_parse_failed` + None
    7. `executor.handle_intent(intent, mid)` → activity AGGREGATE `decision` (result 要約) → 戻り値
  - `ask_once(self, question: str) -> str` — 臨時 Mission (シェル/API の ask):
    - prompt = `load_prompt("ask_mission")` + policy + サマリ + `\n## ユーザーの質問\n{question}`
    - `output_schema=ANSWER_SCHEMA` (**TradeIntent 不可 — 発注経路遮断**)、loop="ask" で記録
    - 成功時 answer 文字列、失敗時はエラーメッセージ文字列を返す (シェルにそのまま表示)

- [ ] **Step 1: 失敗するテストを書く**

`tests/loops/test_trade_loop.py`:

```python
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

from agentic_fx.activity import ActivityLog
from agentic_fx.config import load_settings
from agentic_fx.core.accounting import record_snapshot
from agentic_fx.core.contracts import FixedClock, InstrumentSpec, Quote
from agentic_fx.core.executor import Executor
from agentic_fx.core.notifier import Notifier
from agentic_fx.core.paper_broker import PaperBroker
from agentic_fx.datafeed.health import DataUnhealthy
from agentic_fx.loops.trade_loop import TradeLoop
from agentic_fx.policy import Policy
from agentic_fx.runners.base import MissionResult
from agentic_fx.runners.fake_runner import FakeRunner
from agentic_fx.store.db import connect, init_db
from agentic_fx.store.state import StateStore

NOW = datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc)
SETTINGS = load_settings(
    Path(__file__).resolve().parents[2] / "config" / "settings.yaml.example")
SPEC = InstrumentSpec("USDJPY", 0.01, 0.01, 50.0, 0.01, 100_000)
QUOTE = Quote("USDJPY", 148.49, 148.51, NOW, "test")


def _loop(tmp_path, results, healthy=True):
    conn = connect(tmp_path / "t.db")
    init_db(conn)
    record_snapshot(conn, now=NOW, balance=1_000_000, equity=1_000_000)
    clock = FixedClock(NOW)
    broker = PaperBroker(conn, SETTINGS, clock)
    executor = Executor(conn=conn, broker=broker, settings=SETTINGS,
                        state_store=StateStore(tmp_path / "s.json"),
                        activity=ActivityLog(tmp_path / "a.log"),
                        notifier=Notifier(enabled=False, webhook_url=None),
                        clock=clock,
                        quote_fn=lambda p: QUOTE, spec_fn=lambda p: SPEC)
    provider = MagicMock()
    if healthy:
        provider.healthcheck.return_value = "yfinance"
    else:
        provider.healthcheck.side_effect = DataUnhealthy("all down")
    econ = MagicMock()
    econ.upcoming.return_value = []
    runner = FakeRunner(results)
    policy_path = tmp_path / "directives.md"
    policy_path.write_text("USDJPY は月末は控えめに", encoding="utf-8")
    loop = TradeLoop(conn=conn, runner=runner, settings=SETTINGS,
                     executor=executor, provider=provider, econ=econ,
                     policy=Policy(policy_path),
                     activity=ActivityLog(tmp_path / "a.log"),
                     notifier=Notifier(enabled=False, webhook_url=None),
                     clock=clock)
    return conn, loop, runner, tmp_path


def test_hold_mission_recorded(tmp_path):
    conn, loop, runner, tp = _loop(tmp_path, [MissionResult(
        "completed", {"action": "hold", "reasoning": "様子見"}, [])])
    out = loop.run_once()
    assert out["result"] == "hold"
    m = conn.execute("SELECT * FROM missions").fetchone()
    assert m["loop"] == "trade" and m["status"] == "completed"
    assert conn.execute("SELECT COUNT(*) c FROM trade_intents").fetchone()["c"] == 1
    # prompt に policy とサマリが注入されている
    prompt = runner.missions[0].prompt
    assert "月末は控えめに" in prompt
    assert "現在の状態" in prompt


def test_fail_closed_on_unhealthy_data(tmp_path):
    conn, loop, runner, tp = _loop(tmp_path, [], healthy=False)
    assert loop.run_once() is None
    assert runner.missions == []  # Mission を実行していない
    act = (tp / "a.log").read_text(encoding="utf-8")
    assert "data_unhealthy" in act


def test_runner_failure_recorded(tmp_path):
    conn, loop, _, tp = _loop(tmp_path,
                              [MissionResult("timeout", None, [])])
    assert loop.run_once() is None
    m = conn.execute("SELECT * FROM missions").fetchone()
    assert m["status"] == "timeout"
    assert "mission_failed" in (tp / "a.log").read_text(encoding="utf-8")


def test_runner_exception_normalized_to_failed(tmp_path):
    conn, loop, _, _ = _loop(tmp_path, [])

    class Boom:
        def run(self, mission):
            raise RuntimeError("crash")

    loop.runner = Boom()
    assert loop.run_once() is None  # 例外が漏れない
    m = conn.execute("SELECT * FROM missions").fetchone()
    assert m["status"] == "failed"  # 必ず finish される


def test_unparsable_intent_recorded(tmp_path):
    conn, loop, _, tp = _loop(tmp_path, [MissionResult(
        "completed", {"action": "buy!"}, [])])
    assert loop.run_once() is None
    assert "intent_parse_failed" in (tp / "a.log").read_text(encoding="utf-8")


def test_open_intent_executes(tmp_path):
    conn, loop, _, _ = _loop(tmp_path, [MissionResult(
        "completed",
        {"action": "open", "pair": "USDJPY", "direction": "long",
         "entry_type": "limit", "horizon": "day", "limit_price": 148.20,
         "expires_in": "4h", "stop_loss": 147.80, "take_profit": 149.00,
         "reasoning": "test"}, [])])
    out = loop.run_once()
    assert out["result"] == "pending"


def test_ask_is_answer_only(tmp_path):
    conn, loop, runner, _ = _loop(tmp_path, [MissionResult(
        "completed", {"answer": "現在は様子見が妥当です"}, [])])
    ans = loop.ask_once("今どう見てる？")
    assert "様子見" in ans
    # ask Mission のスキーマは answer 専用 (TradeIntent 不可)
    schema = runner.missions[0].output_schema
    assert "answer" in schema["properties"]
    assert "action" not in schema["properties"]
    m = conn.execute("SELECT * FROM missions").fetchone()
    assert m["loop"] == "ask"
    # intent は生成されない
    assert conn.execute("SELECT COUNT(*) c FROM trade_intents").fetchone()["c"] == 0
```

- [ ] **Step 2: テストが失敗することを確認**

Run: `uv run pytest tests/loops/test_trade_loop.py -v`
Expected: FAIL (ImportError)

- [ ] **Step 3: 実装**

`src/agentic_fx/loops/trade_loop.py`:

```python
"""取引判断 loop — Mission 組み立て・実行・記録・executor 連携 (設計書 §5)。"""
from __future__ import annotations

import sqlite3

from agentic_fx.activity import ActivityLog, Category
from agentic_fx.config import Settings
from agentic_fx.core.contracts import Clock, IntentParseError, Origin, TradeIntent
from agentic_fx.core.executor import Executor
from agentic_fx.core.notifier import Notifier
from agentic_fx.datafeed.econ_calendar import EconCalendar
from agentic_fx.datafeed.health import DataUnhealthy
from agentic_fx.datafeed.price_provider import PriceProvider
from agentic_fx.loops.prompts_loader import load_prompt
from agentic_fx.loops.summary import (
    ANSWER_SCHEMA, TRADE_INTENT_SCHEMA, build_state_summary,
)
from agentic_fx.policy import Policy
from agentic_fx.runners.base import AgentRunner, Mission, MissionResult
from agentic_fx.store import missions

import logging

_log = logging.getLogger("agentic_fx.trade_loop")

_TRADE_TOOLS = ["get_ohlcv", "get_indicators", "search_news",
                "get_econ_calendar", "get_positions", "get_account",
                "get_recent_reflections", "search_reflections"]


class TradeLoop:
    def __init__(self, *, conn: sqlite3.Connection, runner: AgentRunner,
                 settings: Settings, executor: Executor,
                 provider: PriceProvider, econ: EconCalendar, policy: Policy,
                 activity: ActivityLog, notifier: Notifier,
                 clock: Clock) -> None:
        self.conn = conn
        self.runner = runner
        self.settings = settings
        self.executor = executor
        self.provider = provider
        self.econ = econ
        self.policy = policy
        self.activity = activity
        self.notifier = notifier
        self.clock = clock

    # ---- 取引判断 Mission -------------------------------------------------

    def run_once(self, trigger: str = "cron") -> dict | None:
        now = self.clock.now()
        try:
            self.provider.healthcheck(self.settings.pairs[0])
        except DataUnhealthy as e:
            self.activity.write(Category.SYSTEM, "data_unhealthy", str(e))
            self.notifier.send(f"[agentic-fx] データ不健全のため判断をスキップ: {e}")
            return None

        prompt = self._build_prompt(load_prompt("trade_mission"))
        mission = Mission(prompt=prompt, tools=_TRADE_TOOLS,
                          output_schema=TRADE_INTENT_SCHEMA,
                          max_turns=self.settings.llama_swap.max_turns,
                          timeout_sec=self.settings.llama_swap.timeout_sec)
        mid = missions.start(self.conn, "trade",
                             self.settings.runner.trade.backend,
                             self.settings.runner.trade.model, now,
                             trigger=trigger)   # 起動理由を監査列に記録
        result = self._run_recorded(mid, mission)
        if result.status != "completed":
            self.activity.write(Category.AGGREGATE, "mission_failed",
                                f"runner status={result.status}",
                                ref_id=str(mid))
            self.notifier.send(f"[agentic-fx] 判断 Mission 失敗: {result.status}")
            return None
        try:
            intent = TradeIntent.from_llm_dict(result.output,
                                               origin=Origin.SCHEDULER)
        except IntentParseError as e:
            self.activity.write(Category.AGGREGATE, "intent_parse_failed",
                                str(e), ref_id=str(mid))
            return None
        out = self.executor.handle_intent(intent, mid)
        self.activity.write(Category.AGGREGATE, "decision",
                            f"{intent.action.value} -> {out['result']}",
                            ref_id=str(mid))
        return out

    # ---- 臨時 Mission (回答専用) ----------------------------------------

    def ask_once(self, question: str) -> str:
        now = self.clock.now()
        prompt = self._build_prompt(load_prompt("ask_mission")) \
            + f"\n\n## ユーザーの質問\n{question}"
        mission = Mission(prompt=prompt, tools=_TRADE_TOOLS,
                          output_schema=ANSWER_SCHEMA,
                          max_turns=self.settings.llama_swap.max_turns,
                          timeout_sec=self.settings.llama_swap.timeout_sec)
        mid = missions.start(self.conn, "ask",
                             self.settings.runner.trade.backend,
                             self.settings.runner.trade.model, now)
        result = self._run_recorded(mid, mission)
        if result.status != "completed":
            return f"(Mission 失敗: {result.status})"
        self.activity.write(Category.AGGREGATE, "ask_answered",
                            question[:80], ref_id=str(mid))
        return result.output["answer"]

    # ---- internal -------------------------------------------------------

    def _run_recorded(self, mid: int, mission: Mission) -> MissionResult:
        """runner を実行し、例外を failed に正規化して必ず missions.finish する。"""
        result: MissionResult | None = None
        try:
            result = self.runner.run(mission)
        except Exception:  # noqa: BLE001 — runner 例外で周期を殺さない
            _log.exception("runner raised")
            result = MissionResult("failed", None, [])
        finally:
            try:
                missions.finish(self.conn, mid, result.status, result.output,
                                result.transcript, self.clock.now())
            except Exception:  # noqa: BLE001
                _log.exception("missions.finish failed for %s", mid)
        return result

    def _build_prompt(self, system: str) -> str:
        parts = [system]
        tail = self.policy.tail(4000)
        if tail:
            parts.append(f"## ユーザー方針 (policy)\n{tail}")
        parts.append(build_state_summary(
            self.conn, self.executor.broker, self.econ, self.clock,
            self.settings.paper.starting_balance))
        return "\n\n".join(parts)
```

- [ ] **Step 4: テストが通ることを確認**

Run: `uv run pytest tests/loops/test_trade_loop.py -v`
Expected: PASS (全 6 テスト)

- [ ] **Step 5: Commit**

```bash
git add src/agentic_fx/loops/trade_loop.py tests/loops/test_trade_loop.py
git commit -m "feat: 取引判断 loop (fail closed・全記録・ask 回答専用)"
```

---

### Task 4: reflection cycle (loops/reflection_cycle.py)

**Files:**
- Create: `src/agentic_fx/loops/reflection_cycle.py`
- Test: `tests/loops/test_reflection_cycle.py`

**Interfaces:**
- Produces: `class ReflectionCycle`:
  - `__init__(self, *, conn, runner: AgentRunner, rag: Rag, settings: Settings, activity: ActivityLog, clock: Clock)`
  - `run_pending(self) -> int` — **reflection 未作成の closed orders** を検索し、各件: `load_prompt("reflection")` + トレード詳細 (order row + 対応 intent の reasoning) で Mission (`output_schema={"content": string}`、loop="reflection" で記録) → **`rag.add_reflection` を先に、`reflections.save` を後に** 実行する (**SQLite + ChromaDB 二重保存** — 設計書 §12。順序が肝: SQLite 保存を完了マーカーとし、ChromaDB 失敗時は SQLite 行が残らないため次回 rag upsert (order_id 冪等) ごと再試行できる — 片側だけ恒久的に欠ける状態を作らない)。runner の失敗・例外はその件をスキップ (次回再試行、Mission 記録は try/finally で必ず finish)。作成件数を返す

- [ ] **Step 1: 失敗するテストを書く**

`tests/loops/test_reflection_cycle.py`:

```python
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

from agentic_fx.activity import ActivityLog
from agentic_fx.config import load_settings
from agentic_fx.core.contracts import FixedClock, OrderStatus
from agentic_fx.loops.reflection_cycle import ReflectionCycle
from agentic_fx.runners.base import MissionResult
from agentic_fx.runners.fake_runner import FakeRunner
from agentic_fx.store import orders, reflections
from agentic_fx.store.db import connect, init_db

NOW = datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc)
SETTINGS = load_settings(
    Path(__file__).resolve().parents[2] / "config" / "settings.yaml.example")


def _cycle(tmp_path, results):
    conn = connect(tmp_path / "t.db")
    init_db(conn)
    rag = MagicMock()
    cyc = ReflectionCycle(conn=conn, runner=FakeRunner(results), rag=rag,
                          settings=SETTINGS,
                          activity=ActivityLog(tmp_path / "a.log"),
                          clock=FixedClock(NOW))
    return conn, rag, cyc


def _closed_order(conn):
    return orders.insert(conn, pair="USDJPY", direction="long",
                         entry_type="market", horizon="day",
                         status=OrderStatus.CLOSED, now=NOW, quantity=0.1,
                         realized_pnl=-1500.0, close_reason="sl",
                         avg_fill_price=148.5, close_price=148.0)


def test_creates_reflection_for_closed(tmp_path):
    conn, rag, cyc = _cycle(tmp_path, [MissionResult(
        "completed", {"content": "SL 幅が狭すぎた"}, [])])
    oid = _closed_order(conn)
    assert cyc.run_pending() == 1
    assert reflections.get(conn, oid)["content"] == "SL 幅が狭すぎた"
    rag.add_reflection.assert_called_once_with(oid, "SL 幅が狭すぎた", "USDJPY")


def test_skips_already_reflected(tmp_path):
    conn, rag, cyc = _cycle(tmp_path, [MissionResult(
        "completed", {"content": "x"}, [])])
    oid = _closed_order(conn)
    reflections.save(conn, oid, "既存", NOW)
    assert cyc.run_pending() == 0
    rag.add_reflection.assert_not_called()


def test_runner_failure_skips_for_retry(tmp_path):
    conn, rag, cyc = _cycle(tmp_path, [MissionResult("timeout", None, [])])
    oid = _closed_order(conn)
    assert cyc.run_pending() == 0
    assert reflections.get(conn, oid) is None  # 次回再試行できる


def test_rag_failure_leaves_no_sqlite_row(tmp_path):
    # ChromaDB 失敗時に SQLite だけ残ると恒久的に片側欠けになる (#codex 指摘)
    conn, rag, cyc = _cycle(tmp_path, [MissionResult(
        "completed", {"content": "x"}, [])])
    rag.add_reflection.side_effect = RuntimeError("chroma down")
    oid = _closed_order(conn)
    assert cyc.run_pending() == 0
    assert reflections.get(conn, oid) is None  # 完了マーカーなし → 次回再試行
```

- [ ] **Step 2: テストが失敗することを確認**

Run: `uv run pytest tests/loops/test_reflection_cycle.py -v`
Expected: FAIL (ImportError)

- [ ] **Step 3: 実装**

`src/agentic_fx/loops/reflection_cycle.py`:

```python
"""reflection cycle — クローズ済みトレードの振り返り生成 (SQLite + Chroma 二重保存)。"""
from __future__ import annotations

import json
import logging
import sqlite3

_log = logging.getLogger("agentic_fx.reflection")

from agentic_fx.activity import ActivityLog, Category
from agentic_fx.config import Settings
from agentic_fx.core.contracts import Clock
from agentic_fx.loops.prompts_loader import load_prompt
from agentic_fx.runners.base import AgentRunner, Mission
from agentic_fx.store import missions, reflections
from agentic_fx.store.rag import Rag

_SCHEMA = {"type": "object",
           "properties": {"content": {"type": "string"}},
           "required": ["content"]}


class ReflectionCycle:
    def __init__(self, *, conn: sqlite3.Connection, runner: AgentRunner,
                 rag: Rag, settings: Settings, activity: ActivityLog,
                 clock: Clock) -> None:
        self.conn = conn
        self.runner = runner
        self.rag = rag
        self.settings = settings
        self.activity = activity
        self.clock = clock

    def run_pending(self) -> int:
        rows = self.conn.execute(
            "SELECT o.* FROM orders o LEFT JOIN reflections r "
            "ON r.order_id = o.id WHERE o.status='closed' "
            "AND r.order_id IS NULL ORDER BY o.id").fetchall()
        created = 0
        for row in rows:
            if self._reflect_one(dict(row)):
                created += 1
        return created

    def _reflect_one(self, row: dict) -> bool:
        now = self.clock.now()
        intent = None
        if row["intent_id"]:
            ir = self.conn.execute(
                "SELECT payload_json FROM trade_intents WHERE id=?",
                (row["intent_id"],)).fetchone()
            intent = json.loads(ir["payload_json"]) if ir else None
        prompt = (load_prompt("reflection") + "\n\n## トレード詳細\n"
                  + json.dumps({"order": {k: row[k] for k in (
                      "id", "pair", "direction", "horizon", "quantity",
                      "avg_fill_price", "close_price", "realized_pnl",
                      "close_reason")},
                      "entry_reasoning": (intent or {}).get("reasoning")},
                      ensure_ascii=False, indent=1))
        mission = Mission(prompt=prompt, tools=[], output_schema=_SCHEMA,
                          max_turns=2,
                          timeout_sec=self.settings.llama_swap.timeout_sec)
        mid = missions.start(self.conn, "reflection",
                             self.settings.runner.trade.backend,
                             self.settings.runner.trade.model, now)
        result = None
        try:
            result = self.runner.run(mission)
        except Exception:  # noqa: BLE001
            _log.exception("reflection runner raised")
            from agentic_fx.runners.base import MissionResult
            result = MissionResult("failed", None, [])
        finally:
            try:
                missions.finish(self.conn, mid, result.status, result.output,
                                result.transcript, self.clock.now())
            except Exception:  # noqa: BLE001
                _log.exception("missions.finish failed for %s", mid)
        if result.status != "completed":
            return False
        content = result.output["content"]
        # rag → SQLite の順 (SQLite 行が完了マーカー。rag 失敗時は次回丸ごと再試行)
        try:
            self.rag.add_reflection(row["id"], content, row["pair"])
        except Exception:  # noqa: BLE001
            _log.exception("rag.add_reflection failed for #%s — retry next run",
                           row["id"])
            return False
        reflections.save(self.conn, row["id"], content, now)
        self.activity.write(Category.AGGREGATE, "reflection_created",
                            f"#{row['id']} {row['pair']}",
                            ref_id=str(row["id"]))
        return True
```

- [ ] **Step 4: テストが通ることを確認**

Run: `uv run pytest tests/loops/test_reflection_cycle.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/agentic_fx/loops/reflection_cycle.py tests/loops/test_reflection_cycle.py
git commit -m "feat: reflection cycle (未作成のみ・二重保存・失敗は再試行)"
```

---

### Task 5: 操作コマンド (commands.py)

**Files:**
- Create: `src/agentic_fx/commands.py`
- Test: `tests/test_commands.py`

**Interfaces:**
- Produces: `class Commands` (main.py シェルで使用。Phase 2 で client.py が同じ定義を API 経由で再利用):
  - `__init__(self, *, conn, state_store: StateStore, broker: PaperBroker, trade_loop: TradeLoop, activity: ActivityLog, log_dir: Path, clock: Clock)`
  - `dispatch(self, line: str) -> str` — 1 行をパースして実行し、表示文字列を返す。未知コマンドは help 文字列
  - Phase 1 コマンド: `status` / `log [n]` / `activity [n] [カテゴリ]` / `ask <質問...>` / `approve <id>` / `reject <id> [理由...]` / `killswitch reset` / `help`
  - `status`: mode・autopilot・kill switch・残高/エクイティ・アクティブ orders 数・直近 mission
  - `log`: `logs/agentic.log` の末尾 n 行 (デフォルト 20)
  - `activity`: `ActivityLog.tail(n, category)`
  - `approve/reject`: `approvals.decide` (`AlreadyDecidedError` はメッセージ表示)
  - `killswitch reset`: `state_store.update(kill_switch_latched=False)` + activity SYSTEM 記録 (人間の明示解除 — 設計書 §5)
  - Phase 2 予定コマンド (`policy add` / `improve` / `news` / `model` / `mode` / `autopilot`) は「Phase 2 で追加」と help に明記

- [ ] **Step 1: 失敗するテストを書く**

`tests/test_commands.py`:

```python
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

from agentic_fx.activity import ActivityLog, Category
from agentic_fx.commands import Commands
from agentic_fx.config import load_settings
from agentic_fx.core.contracts import FixedClock
from agentic_fx.core.paper_broker import PaperBroker
from agentic_fx.store import approvals
from agentic_fx.store.db import connect, init_db
from agentic_fx.store.state import StateStore

NOW = datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc)
SETTINGS = load_settings(
    Path(__file__).resolve().parents[2] / "config" / "settings.yaml.example")


def _commands(tmp_path):
    conn = connect(tmp_path / "t.db")
    init_db(conn)
    state = StateStore(tmp_path / "s.json")
    activity = ActivityLog(tmp_path / "logs" / "activity.log")
    (tmp_path / "logs").mkdir(exist_ok=True)
    (tmp_path / "logs" / "agentic.log").write_text(
        "line1\nline2\nline3\n", encoding="utf-8")
    trade_loop = MagicMock()
    trade_loop.ask_once.return_value = "回答です"
    cmds = Commands(conn=conn, state_store=state,
                    broker=PaperBroker(conn, SETTINGS, FixedClock(NOW)),
                    trade_loop=trade_loop, activity=activity,
                    log_dir=tmp_path / "logs", clock=FixedClock(NOW))
    return conn, state, activity, cmds


def test_status(tmp_path):
    _, _, _, cmds = _commands(tmp_path)
    out = cmds.dispatch("status")
    assert "learning" in out and "1,000,000" in out


def test_log_tail(tmp_path):
    _, _, _, cmds = _commands(tmp_path)
    out = cmds.dispatch("log 2")
    assert "line2" in out and "line1" not in out


def test_activity_filter(tmp_path):
    _, _, activity, cmds = _commands(tmp_path)
    activity.write(Category.TRADE, "order_opened", "x")
    activity.write(Category.NEWS, "collected", "y")
    out = cmds.dispatch("activity 10 TRADE")
    assert "order_opened" in out and "collected" not in out


def test_ask_delegates(tmp_path):
    _, _, _, cmds = _commands(tmp_path)
    assert "回答です" in cmds.dispatch("ask 今の相場は？")


def test_approve_reject(tmp_path):
    conn, _, _, cmds = _commands(tmp_path)
    aid = approvals.create(conn, "tech_plugin", {}, NOW)
    assert "approved" in cmds.dispatch(f"approve {aid}")
    out = cmds.dispatch(f"reject {aid} 理由")  # 二重決定
    assert "決定済み" in out or "already" in out.lower()


def test_killswitch_reset(tmp_path):
    _, state, _, cmds = _commands(tmp_path)
    state.update(kill_switch_latched=True)
    out = cmds.dispatch("killswitch reset")
    assert state.load().kill_switch_latched is False
    assert "解除" in out


def test_unknown_shows_help(tmp_path):
    _, _, _, cmds = _commands(tmp_path)
    assert "status" in cmds.dispatch("nonsense")
```

- [ ] **Step 2: テストが失敗することを確認**

Run: `uv run pytest tests/test_commands.py -v`
Expected: FAIL (ImportError)

- [ ] **Step 3: 実装**

`src/agentic_fx/commands.py`:

```python
"""操作コマンド定義 — main.py シェルと (Phase 2) client.py で共有 (設計書 §8)。"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from agentic_fx.activity import ActivityLog, Category
from agentic_fx.core.contracts import Clock
from agentic_fx.core.paper_broker import PaperBroker
from agentic_fx.store import approvals, missions, orders
from agentic_fx.store.approvals import AlreadyDecidedError
from agentic_fx.store.state import StateStore

_HELP = """コマンド一覧:
  status                     残高・モード・kill switch・直近 mission
  log [n]                    技術ログの直近 n 行 (default 20)
  activity [n] [カテゴリ]     activity ログ (NEWS/TECH/AGGREGATE/TRADE/IMPROVE/APPROVAL/SYSTEM)
  ask <質問>                  臨時 Mission (回答専用 — 発注はしない)
  approve <id> / reject <id> [理由]   承認操作
  killswitch reset           kill switch ラッチの解除 (人間の明示操作)
  stop                       graceful shutdown (シェルのみ)
(Phase 2 で追加: policy add / improve / news / model / mode / autopilot)"""


class Commands:
    def __init__(self, *, conn: sqlite3.Connection, state_store: StateStore,
                 broker: PaperBroker, trade_loop, activity: ActivityLog,
                 log_dir: Path, clock: Clock) -> None:
        self.conn = conn
        self.state = state_store
        self.broker = broker
        self.trade_loop = trade_loop
        self.activity = activity
        self.log_dir = log_dir
        self.clock = clock

    def dispatch(self, line: str) -> str:
        parts = line.strip().split()
        if not parts:
            return ""
        cmd, args = parts[0], parts[1:]
        try:
            if cmd == "status":
                return self._status()
            if cmd == "log":
                return self._log(int(args[0]) if args else 20)
            if cmd == "activity":
                n = int(args[0]) if args else 20
                cat = Category(args[1].upper()) if len(args) > 1 else None
                return "\n".join(self.activity.tail(n, cat)) or "(なし)"
            if cmd == "ask":
                if not args:
                    return "usage: ask <質問>"
                return self.trade_loop.ask_once(" ".join(args))
            if cmd == "approve" and args:
                approvals.decide(self.conn, int(args[0]), status="approved",
                                 decided_by="shell", now=self.clock.now())
                self.activity.write(Category.APPROVAL, "approved",
                                    f"#{args[0]} via shell", ref_id=args[0])
                return f"approval #{args[0]} approved"
            if cmd == "reject" and args:
                reason = " ".join(args[1:]) or None
                approvals.decide(self.conn, int(args[0]), status="rejected",
                                 decided_by="shell", now=self.clock.now(),
                                 reason=reason)
                self.activity.write(Category.APPROVAL, "rejected",
                                    f"#{args[0]} via shell", ref_id=args[0])
                return f"approval #{args[0]} rejected"
            if cmd == "killswitch" and args and args[0] == "reset":
                self.state.update(kill_switch_latched=False)
                self.activity.write(Category.SYSTEM, "kill_switch_reset",
                                    "human explicit reset via shell")
                return "kill switch ラッチを解除しました"
        except AlreadyDecidedError:
            return "その approval は決定済みです"
        except (ValueError, KeyError) as e:
            return f"エラー: {e}\n{_HELP}"
        return _HELP

    def _status(self) -> str:
        s = self.state.load()
        balance, equity = self.broker.equity()
        active = orders.list_by_status(self.conn, "open", "pending_fill",
                                       "protection_pending")
        recent = missions.recent(self.conn, 1)
        last = (f"{recent[0]['loop']}:{recent[0]['status']} "
                f"({recent[0]['started_at']})") if recent else "なし"
        return (f"mode: {s.mode.value} / autopilot: "
                f"{'on' if s.autopilot else 'off'} / kill switch: "
                f"{'LATCHED' if s.kill_switch_latched else 'ok'}\n"
                f"残高: {balance:,.0f} / エクイティ: {equity:,.0f}\n"
                f"アクティブ orders: {len(active)}\n"
                f"直近 mission: {last}")

    def _log(self, n: int) -> str:
        path = self.log_dir / "agentic.log"
        if not path.exists():
            return "(ログなし)"
        return "\n".join(
            path.read_text(encoding="utf-8").splitlines()[-n:])
```

- [ ] **Step 4: テストが通ることを確認**

Run: `uv run pytest tests/test_commands.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/agentic_fx/commands.py tests/test_commands.py
git commit -m "feat: 操作コマンド (status/log/activity/ask/approve/killswitch reset)"
```

---

### Task 6: 対話シェル (shell.py) + logging daemon 拡張

**Files:**
- Create: `src/agentic_fx/shell.py`
- Modify: `src/agentic_fx/logging_setup.py`
- Test: `tests/test_shell.py`, `tests/test_logging_setup.py` (追記)

**Interfaces:**
- Produces:
  - `run_shell(commands: Commands, stop_event: threading.Event, *, input_fn=input, print_fn=print) -> None` — プロンプト `afx> `。`stop` / EOF / Ctrl-C で `stop_event.set()` して抜ける。その他は `commands.dispatch` の戻りを print。**ログを勝手に流さない** (print するのはコマンド結果のみ)
  - `setup_technical_logging(log_dir, level="INFO", *, daemon: bool = False)` — `daemon=True` のとき stderr StreamHandler を追加 (systemd → journald 経路。プラン 1 の契約)

- [ ] **Step 1: 失敗するテストを書く**

`tests/test_shell.py`:

```python
import threading
from unittest.mock import MagicMock

from agentic_fx.shell import run_shell


def _run(lines):
    cmds = MagicMock()
    cmds.dispatch.return_value = "OK"
    stop = threading.Event()
    it = iter(lines)
    outputs = []

    def fake_input(prompt=""):
        try:
            return next(it)
        except StopIteration:
            raise EOFError

    run_shell(cmds, stop, input_fn=fake_input, print_fn=outputs.append)
    return cmds, stop, outputs


def test_stop_sets_event():
    cmds, stop, _ = _run(["stop"])
    assert stop.is_set()
    cmds.dispatch.assert_not_called()


def test_eof_sets_event():
    _, stop, _ = _run([])
    assert stop.is_set()


def test_commands_dispatched_and_printed():
    cmds, _, outputs = _run(["status", "stop"])
    cmds.dispatch.assert_called_once_with("status")
    assert "OK" in outputs


def test_empty_line_ignored():
    cmds, _, _ = _run(["", "  ", "stop"])
    cmds.dispatch.assert_not_called()
```

`tests/test_logging_setup.py` に追記:

```python
def test_daemon_adds_stderr_handler(tmp_path):
    import logging
    logger = setup_technical_logging(tmp_path, daemon=True)
    kinds = [type(h).__name__ for h in logger.handlers]
    assert "RotatingFileHandler" in kinds
    assert "StreamHandler" in kinds
    # daemon=False に戻すと stderr handler は外れる
    logger2 = setup_technical_logging(tmp_path, daemon=False)
    assert [type(h).__name__ for h in logger2.handlers] == [
        "RotatingFileHandler"]
```

- [ ] **Step 2: テストが失敗することを確認**

Run: `uv run pytest tests/test_shell.py tests/test_logging_setup.py -v`
Expected: 新規分 FAIL

- [ ] **Step 3: 実装**

`src/agentic_fx/shell.py`:

```python
"""対話シェル — 静かなプロンプト。ログは pull 型コマンドのみ (設計書 §8)。"""
from __future__ import annotations

import threading

from agentic_fx.commands import Commands


def run_shell(commands: Commands, stop_event: threading.Event, *,
              input_fn=input, print_fn=print) -> None:
    while not stop_event.is_set():
        try:
            line = input_fn("afx> ").strip()
        except (EOFError, KeyboardInterrupt):
            stop_event.set()
            return
        if not line:
            continue
        if line == "stop":
            stop_event.set()
            return
        print_fn(commands.dispatch(line))
```

`src/agentic_fx/logging_setup.py` — `setup_technical_logging` を置換:

```python
import sys


def setup_technical_logging(log_dir: Path, level: str = "INFO", *,
                            daemon: bool = False) -> logging.Logger:
    log_dir.mkdir(parents=True, exist_ok=True)
    target = (log_dir / "agentic.log").resolve()
    logger = logging.getLogger("agentic_fx")
    logger.setLevel(level.upper())
    logger.propagate = False
    file_ok = False
    for h in list(logger.handlers):
        if isinstance(h, RotatingFileHandler) and Path(h.baseFilename) == target:
            file_ok = True
            continue
        logger.removeHandler(h)
        h.close()
    if not file_ok:
        handler = RotatingFileHandler(target, maxBytes=10 * 1024 * 1024,
                                      backupCount=5, encoding="utf-8")
        handler.setFormatter(logging.Formatter(_FORMAT))
        logger.addHandler(handler)
    if daemon:
        stream = logging.StreamHandler(sys.stderr)
        stream.setFormatter(logging.Formatter(_FORMAT))
        logger.addHandler(stream)
    return logger
```

注: daemon=False 再呼び出しで StreamHandler が外れるのは「RotatingFileHandler + 同一パス以外は除去」のロジックによる。

- [ ] **Step 4: テストが通ることを確認**

Run: `uv run pytest tests/test_shell.py tests/test_logging_setup.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/agentic_fx/shell.py src/agentic_fx/logging_setup.py \
  tests/test_shell.py tests/test_logging_setup.py
git commit -m "feat: 対話シェル + daemon 時の journald 経路 (stderr handler)"
```

---

### Task 7: サービス配線 (service.py 本実装 + entry 拡張)

**Files:**
- Modify: `src/agentic_fx/service.py`, `src/agentic_fx/entry.py`
- Test: `tests/test_service_app.py`

**Interfaces:**
- Produces:
  - `@dataclass App(conn_core, conn_shell, settings, state, activity, broker, executor, provider, econ, collector, rag, trade_loop, reflection, scheduler, commands, registry, core_lock: threading.RLock)`
  - `build_app(root: Path, *, runner: AgentRunner | None = None, clock: Clock | None = None, quote_fn=None, spec_fn=None, bars_fn=None) -> App` — 全部品を配線して返す。**quote_fn / spec_fn / bars_fn は E2E テストの注入点** (None なら provider の実装を使う — build 後の patch では bound 済みクロージャに届かないため注入で解決):
    - registry: market/news/account/reflection の build を register_all
    - runner が None なら `LocalRunner(base_url=settings.llama_swap.base_url, model=settings.runner.trade.model, registry=registry)`
    - **conn_core** (scheduler/Mission 用) と **conn_shell** (Commands 用) の 2 接続。core_lock (RLock) が conn_core の全使用 (tick + ask) を排他
    - `Scheduler(on_trade_mission=<trade_loop.run_once + reflection.run_pending>, on_news_cycle=collector.collect, bars_fn=...)` — tick 全体が core_lock 下で走るため on_trade_mission 内での二重ロックは不要 (RLock なので取っても安全)
  - `run_service(root: Path, *, daemon: bool = False) -> int` — ensure_initialized → build_app → policy サイズ警告表示 → **スプラッシュ表示** → scheduler スレッド起動 (60 秒毎に **core_lock を取って** `tick(now)`。**stop_event が立っていたら新しい tick を開始しない**) → TTY なら `run_shell`、daemon なら SIGTERM/SIGINT 待ち → **graceful shutdown**: stop_event → `th.join(timeout=30)` → **join 成功時のみ activity `service_stopped` = "graceful"、タイムアウト時は "shutdown_timeout (Mission 継続中の可能性)" を正直に記録** (終了確認なしに graceful と書かない)
  - `build_splash(app) -> str` — mode・発注方式・対象ペア・runner/モデル・risk gate 現行値・承認待ち件数・未約定指値・直近成績。**項目は最小でよい (運用しながら調整 — 設計書 §8)**
  - entry: `--daemon` フラグ追加。非 TTY (`not sys.stdin.isatty()`) は自動で daemon 扱い
  - init 拡張: llama-swap 接続確認 (`GET {base_url}/models`、失敗は警告のみ)

- [ ] **Step 1: 失敗するテストを書く**

`tests/test_service_app.py`:

```python
import threading
from datetime import datetime, timezone

from agentic_fx.core.contracts import FixedClock
from agentic_fx.runners.base import MissionResult
from agentic_fx.runners.fake_runner import FakeRunner
from agentic_fx.service import build_app, build_splash, run_init

NOW = datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc)


def _init(tmp_path):
    (tmp_path / "config").mkdir()
    src = open("config/settings.yaml.example", encoding="utf-8").read()
    (tmp_path / "config" / "settings.yaml.example").write_text(src)
    from unittest.mock import patch
    with patch("agentic_fx.service.PriceProvider") as pp:
        pp.return_value.healthcheck.return_value = "yfinance"
        run_init(tmp_path)


def test_build_app_wires_everything(tmp_path):
    _init(tmp_path)
    fake = FakeRunner([MissionResult("completed",
                                     {"action": "hold", "reasoning": "w"},
                                     [])])
    app = build_app(tmp_path, runner=fake, clock=FixedClock(NOW))
    assert app.trade_loop is not None
    assert app.scheduler is not None
    assert isinstance(app.core_lock, type(threading.RLock()))
    assert app.conn_core is not app.conn_shell  # スレッド別接続
    # ツールが登録されている
    for name in ("get_ohlcv", "search_news", "get_positions",
                 "get_recent_reflections"):
        assert name in app.registry.names()


def test_splash_contains_key_fields(tmp_path):
    _init(tmp_path)
    app = build_app(tmp_path, runner=FakeRunner([]), clock=FixedClock(NOW))
    splash = build_splash(app)
    assert "learning" in splash
    assert "USDJPY" in splash
    assert "qwen" in splash  # runner モデル名


def test_on_trade_mission_runs_loop_and_reflection(tmp_path):
    _init(tmp_path)
    fake = FakeRunner([MissionResult("completed",
                                     {"action": "hold", "reasoning": "w"},
                                     [])])
    app = build_app(tmp_path, runner=fake, clock=FixedClock(NOW))
    from unittest.mock import patch
    from agentic_fx.core.accounting import record_snapshot
    record_snapshot(app.conn_core, now=NOW, balance=1_000_000,
                    equity=1_000_000)
    with patch.object(app.provider, "healthcheck", return_value="yfinance"):
        app.scheduler.on_trade_mission("cron")
    assert len(fake.missions) >= 1  # trade mission が実行された
    rows = app.conn_core.execute("SELECT * FROM missions").fetchall()
    assert any(r["loop"] == "trade" for r in rows)


def test_tick_propagates_trigger_to_missions_row(tmp_path):
    """tick → on_trade_mission(reason) → run_once(trigger) → missions.trigger。

    この配線は wrapper が引数を捨てても各層の単体テストでは緑のままに
    なるため、tick 起点で通しで検証する (codex レビュー 1-4)。
    """
    _init(tmp_path)
    fake = FakeRunner([MissionResult("completed",
                                     {"action": "hold", "reasoning": "w"},
                                     [])])
    app = build_app(tmp_path, runner=fake, clock=FixedClock(NOW))
    from unittest.mock import patch
    from agentic_fx.core.accounting import record_snapshot
    record_snapshot(app.conn_core, now=NOW, balance=1_000_000,
                    equity=1_000_000)
    with patch.object(app.provider, "healthcheck", return_value="yfinance"):
        app.scheduler.tick(NOW)
    row = app.conn_core.execute(
        "SELECT trigger FROM missions WHERE loop='trade'").fetchone()
    assert row["trigger"] == "cron"
```

- [ ] **Step 2: テストが失敗することを確認**

Run: `uv run pytest tests/test_service_app.py -v`
Expected: FAIL

- [ ] **Step 3: 実装**

`src/agentic_fx/service.py` に追加 (既存 `ensure_initialized` / `run_init` は維持し、以下を追記。import は先頭にまとめる):

```python
from __future__ import annotations

import signal
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import os

from agentic_fx.commands import Commands
from agentic_fx.core.contracts import Clock
from agentic_fx.core.executor import Executor
from agentic_fx.core.notifier import Notifier
from agentic_fx.core.paper_broker import PaperBroker
from agentic_fx.core.scheduler import Scheduler
from agentic_fx.datafeed.econ_calendar import EconCalendar
from agentic_fx.datafeed.news_collector import NewsCollector
from agentic_fx.loops.reflection_cycle import ReflectionCycle
from agentic_fx.loops.trade_loop import TradeLoop
from agentic_fx.policy import Policy
from agentic_fx.runners.base import AgentRunner
from agentic_fx.runners.local_runner import LocalRunner
from agentic_fx.store import approvals, orders
from agentic_fx.store.rag import Rag
from agentic_fx.tools import (
    account_tools, market_tools, news_tools, reflection_tools,
)
from agentic_fx.tools.registry import ToolRegistry


class _SystemClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


@dataclass
class App:
    conn_core: object
    conn_shell: object
    settings: object
    state: object
    activity: object
    broker: object
    executor: object
    provider: object
    econ: object
    collector: object
    rag: object
    trade_loop: object
    reflection: object
    scheduler: object
    commands: object
    registry: object
    core_lock: threading.RLock


def build_app(root: Path, *, runner: AgentRunner | None = None,
              clock: Clock | None = None, quote_fn=None, spec_fn=None,
              bars_fn=None) -> App:
    clock = clock or _SystemClock()
    settings = load_settings(root / "config" / "settings.yaml")
    state = _state_store(root)
    activity = ActivityLog(root / "logs" / "activity.log")
    conn_core = connect(root / "data" / "agentic.db")
    init_db(conn_core)
    conn_shell = connect(root / "data" / "agentic.db")

    provider = PriceProvider(conn_core, settings, clock)
    quote_fn = quote_fn or provider.get_quote
    spec_fn = spec_fn or provider.spec
    bars_fn = bars_fn or provider.latest_1m_bar

    econ = EconCalendar(conn_core, activity, clock)
    rag = Rag(root / "data" / "rag")
    collector = NewsCollector(conn_core, rag, activity, clock)
    broker = PaperBroker(conn_core, settings, clock)
    notifier = Notifier(enabled=settings.discord.enabled,
                        webhook_url=os.environ.get("DISCORD_WEBHOOK_URL"))
    executor = Executor(conn=conn_core, broker=broker, settings=settings,
                        state_store=state, activity=activity,
                        notifier=notifier, clock=clock,
                        quote_fn=quote_fn, spec_fn=spec_fn)

    registry = ToolRegistry()
    registry.register_all(market_tools.build(provider, econ, settings))
    registry.register_all(news_tools.build(rag))
    registry.register_all(account_tools.build(conn_core, broker))
    registry.register_all(reflection_tools.build(conn_core, rag))

    if runner is None:
        runner = LocalRunner(base_url=settings.llama_swap.base_url,
                             model=settings.runner.trade.model,
                             registry=registry)

    policy = Policy(root / "policy" / "directives.md")
    trade_loop = TradeLoop(conn=conn_core, runner=runner, settings=settings,
                           executor=executor, provider=provider, econ=econ,
                           policy=policy, activity=activity,
                           notifier=notifier, clock=clock)
    reflection = ReflectionCycle(conn=conn_core, runner=runner, rag=rag,
                                 settings=settings, activity=activity,
                                 clock=clock)

    core_lock = threading.RLock()

    def on_trade_mission(trigger: str) -> None:
        # tick 全体が core_lock 下で走る (RLock のため再取得も安全)
        # trigger は scheduler._trade_mission_due() が返した起動理由。
        # ここで捨てると missions.trigger が常に既定値になり、監査列が
        # 死ぬ (末尾「追記 (2026-07-27)」参照)
        with core_lock:
            trade_loop.run_once(trigger)
            reflection.run_pending()

    scheduler = Scheduler(conn=conn_core, executor=executor,
                          settings=settings, state_store=state,
                          activity=activity, bars_fn=bars_fn,
                          on_trade_mission=on_trade_mission,
                          on_news_cycle=collector.collect)

    # Commands は conn_shell 束縛の broker を持つ (conn_core をシェルスレッドから触らない)
    shell_broker = PaperBroker(conn_shell, settings, clock)
    commands = Commands(conn=conn_shell, state_store=state,
                        broker=shell_broker,
                        trade_loop=_LockedAsk(trade_loop, core_lock),
                        activity=activity, log_dir=root / "logs", clock=clock)
    return App(conn_core=conn_core, conn_shell=conn_shell, settings=settings,
               state=state, activity=activity, broker=broker,
               executor=executor, provider=provider, econ=econ,
               collector=collector, rag=rag, trade_loop=trade_loop,
               reflection=reflection, scheduler=scheduler, commands=commands,
               registry=registry, core_lock=core_lock)


class _LockedAsk:
    """ask を Mission スロット (core_lock) 経由で実行する薄いラッパー。"""

    def __init__(self, trade_loop: TradeLoop, lock: threading.RLock) -> None:
        self._loop = trade_loop
        self._lock = lock

    def ask_once(self, question: str) -> str:
        with self._lock:
            return self._loop.ask_once(question)


def build_splash(app: App) -> str:
    s = app.state.load()
    balance, equity = app.commands.broker.equity()  # conn_shell 側
    pending = len(approvals.pending(app.conn_shell))
    limits = len(orders.list_by_status(app.conn_shell, "pending_fill"))
    # 項目構成は運用しながら調整 (設計書 §8) — 初期実装は最小
    return (
        "=== agentic-fx ===\n"
        f"mode: {s.mode.value} / autopilot: {'on' if s.autopilot else 'off'}"
        f" / kill switch: {'LATCHED' if s.kill_switch_latched else 'ok'}\n"
        f"pairs: {', '.join(app.settings.pairs)}\n"
        f"runner: {app.settings.runner.trade.backend}"
        f" ({app.settings.runner.trade.model})\n"
        f"risk: {app.settings.risk.risk_per_trade_pct}%/trade,"
        f" DD kill {app.settings.risk.drawdown_kill_pct}%,"
        f" daily {app.settings.risk.daily_loss_limit_pct}%\n"
        f"残高: {balance:,.0f} / 承認待ち: {pending} / 未約定指値: {limits}\n"
        "コマンドは help を参照。stop で終了。")


def run_service(root: Path, *, daemon: bool = False) -> int:
    ensure_initialized(root)
    settings = load_settings(root / "config" / "settings.yaml")
    setup_technical_logging(root / "logs", settings.logging.level,
                            daemon=daemon)
    app = build_app(root)

    warning = Policy(root / "policy" / "directives.md").size_warning()
    if warning:
        print(warning)
    print(build_splash(app))
    app.activity.write(Category.SYSTEM, "service_started",
                       f"daemon={daemon}")

    stop_event = threading.Event()

    def scheduler_thread() -> None:
        last = 0.0
        while not stop_event.is_set():
            if time.monotonic() - last >= 60:
                last = time.monotonic()
                if stop_event.is_set():
                    break  # 停止フェーズ: 新しい tick を開始しない
                try:
                    with app.core_lock:
                        app.scheduler.tick(datetime.now(timezone.utc))
                except Exception:  # noqa: BLE001
                    logging.getLogger("agentic_fx").exception("tick failed")
            stop_event.wait(1)

    th = threading.Thread(target=scheduler_thread, daemon=True)
    th.start()

    if daemon:
        signal.signal(signal.SIGTERM, lambda *_: stop_event.set())
        signal.signal(signal.SIGINT, lambda *_: stop_event.set())
        while not stop_event.is_set():
            stop_event.wait(1)
    else:
        from agentic_fx.shell import run_shell
        run_shell(app.commands, stop_event)

    # graceful shutdown: scheduler スレッドの終了を確認してから記録する
    # (tick は core_lock 下で走るため、join 完了 = 実行中 Mission も完了)
    th.join(timeout=30)
    if th.is_alive():
        app.activity.write(Category.SYSTEM, "service_stopped",
                           "shutdown_timeout (Mission 継続中の可能性)")
        print("警告: 停止タイムアウト。実行中の処理が残っている可能性があります。")
        return 1
    app.activity.write(Category.SYSTEM, "service_stopped", "graceful")
    print("停止しました。")
    return 0
```

`src/agentic_fx/entry.py` — 置換:

```python
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from agentic_fx import service


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="afx", description="agentic-fx")
    parser.add_argument("--daemon", action="store_true",
                        help="systemd 用 (コマンド受付なし)")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("init", help="初期設定 (非対話・冪等)")
    args = parser.parse_args(argv)

    root = Path.cwd()
    if args.command == "init":
        return service.run_init(root)
    daemon = args.daemon or not sys.stdin.isatty()
    return service.run_service(root, daemon=daemon)
```

`run_init` 末尾 (価格ソース確認の後) に llama-swap 確認を追加:

```python
    try:
        import httpx
        httpx.get(f"{settings.llama_swap.base_url}/models", timeout=5)
        print("llama-swap OK")
    except Exception as e:  # noqa: BLE001
        print(f"警告: llama-swap に接続できません ({e})。"
              "取引判断 Mission は失敗として記録されます。")
```

- [ ] **Step 4: テストが通ることを確認**

Run: `uv run pytest tests/test_service_app.py tests/test_init_and_guard.py -v`
Expected: PASS (init テストは llama-swap 警告分岐を含め成立 — 失敗するなら httpx を mock)

- [ ] **Step 5: Commit**

```bash
git add src/agentic_fx/service.py src/agentic_fx/entry.py tests/test_service_app.py
git commit -m "feat: サービス配線 (build_app・スプラッシュ・scheduler スレッド・graceful shutdown)"
```

---

### Task 8: E2E — FakeRunner フル自走 (Phase 1 完成条件)

**Files:**
- Test: `tests/test_e2e_phase1.py`

- [ ] **Step 1: E2E テストを書く**

`tests/test_e2e_phase1.py`:

```python
"""Phase 1 完成条件: 学習モードで scheduler tick → Mission → intent →
ペーパー発注 → 約定 → reflection まで LLM なし (FakeRunner) で自走する。"""
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from agentic_fx.core.contracts import Bar, FixedClock
from agentic_fx.runners.base import MissionResult
from agentic_fx.runners.fake_runner import FakeRunner
from agentic_fx.service import build_app, run_init

WED = datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc)

OPEN_INTENT = {"action": "open", "pair": "USDJPY", "direction": "long",
               "entry_type": "limit", "horizon": "day",
               "limit_price": 148.20, "expires_in": "6h",
               "stop_loss": 147.80, "take_profit": 149.00,
               "reasoning": "e2e"}


HOLD_INTENT = {"action": "hold", "reasoning": "様子見"}


def test_phase1_full_cycle(tmp_path):
    (tmp_path / "config").mkdir()
    src = open("config/settings.yaml.example", encoding="utf-8").read()
    (tmp_path / "config" / "settings.yaml.example").write_text(src)
    with patch("agentic_fx.service.PriceProvider") as pp:
        pp.return_value.healthcheck.return_value = "yfinance"
        run_init(tmp_path)

    # 実行順に合わせた結果列 (#codex 指摘: reflection は 2 周目の trade の後):
    # tick1: trade → OPEN / (closed なし、reflection Mission は走らない)
    # tick4: trade → HOLD → reflection → content
    fake = FakeRunner([
        MissionResult("completed", OPEN_INTENT, []),
        MissionResult("completed", HOLD_INTENT, []),
        MissionResult("completed", {"content": "振り返り"}, []),
    ])

    from agentic_fx.core.contracts import InstrumentSpec, Quote
    bars = {}
    quote_fn = lambda p: Quote(p, 148.49, 148.51, WED, "test")  # noqa: E731
    spec_fn = lambda p: InstrumentSpec(p, 0.01, 0.01, 50.0, 0.01,  # noqa: E731
                                       100_000)
    # build_app の注入点を使う (build 後の patch は bound クロージャに届かない)
    app = build_app(tmp_path, runner=fake, clock=FixedClock(WED),
                    quote_fn=quote_fn, spec_fn=spec_fn,
                    bars_fn=lambda p: bars.get(p))

    with patch.object(app.provider, "healthcheck", return_value="test"):
        # tick 1: 毎時 Mission → 指値発注
        app.scheduler.tick(WED)
        rows = app.conn_core.execute("SELECT * FROM orders").fetchall()
        assert len(rows) == 1 and rows[0]["status"] == "pending_fill"

        # tick 2: 約定バー → open
        bars["USDJPY"] = Bar("USDJPY", "1m", WED, 148.30, 148.35, 148.15,
                             148.25, 100)
        app.scheduler.tick(WED + timedelta(minutes=1))
        assert app.conn_core.execute(
            "SELECT status FROM orders").fetchone()["status"] == "open"

        # tick 3: TP バー (新しい ts — 同一バー再処理防止) → closed
        bars["USDJPY"] = Bar("USDJPY", "1m", WED + timedelta(minutes=1),
                             148.90, 149.10, 148.85, 149.05, 100)
        app.scheduler.tick(WED + timedelta(minutes=2))
        row = app.conn_core.execute("SELECT * FROM orders").fetchone()
        assert row["status"] == "closed" and row["realized_pnl"] > 0

        # tick 4 (1 時間後): 2 周目 trade (hold) → reflection 生成
        bars["USDJPY"] = Bar("USDJPY", "1m",
                             WED + timedelta(hours=1),
                             149.00, 149.05, 148.95, 149.00, 100)
        app.scheduler.tick(WED + timedelta(hours=1, minutes=1))
        refl = app.conn_core.execute("SELECT * FROM reflections").fetchall()
        assert len(refl) == 1

    # 監査痕跡: 全 Mission が意図した status で完了している (#codex 指摘)
    missions = app.conn_core.execute(
        "SELECT loop, status FROM missions ORDER BY id").fetchall()
    assert [(m["loop"], m["status"]) for m in missions] == [
        ("trade", "completed"), ("trade", "completed"),
        ("reflection", "completed")]
    act = (tmp_path / "logs" / "activity.log").read_text(encoding="utf-8")
    for ev in ("decision", "limit_placed", "limit_filled", "order_closed",
               "reflection_created"):
        assert ev in act
    assert "intent_parse_failed" not in act  # 途中の Mission 失敗を見逃さない
```

- [ ] **Step 2: テストを実行して green にする**

Run: `uv run pytest tests/test_e2e_phase1.py -v`
Expected: PASS (落ちる場合は配線を修正 — テストの意図を変えない)

- [ ] **Step 3: 全体テスト + セルフレビュー**

Run: `uv run pytest -v`
Expected: 全件 PASS

チェック (Phase 1 完成条件):
- 設計書 §15 Phase 1 の項目が全て揃ったか: 決定論的コア / LocalRunner / 取引判断 loop / 価格取得 (yfinance デフォルト) / main.py (スプラッシュ + シェル + init ガード + stop) / ログ 2 軸 / 最小ツールセット
- `uv run main.py init` → `uv run main.py` → `status` / `ask` / `stop` の手動確認
- ask から intent が執行されないこと (origin 検証テスト + ANSWER_SCHEMA)

- [ ] **Step 4: Commit**

```bash
git add tests/test_e2e_phase1.py
git commit -m "test: Phase 1 E2E (FakeRunner フル自走 — 完成条件)"
```

---

## Phase 2 プランへの引き継ぎ事項

- `Commands` に Phase 2 コマンド (policy add / improve / news / model / mode / autopilot) を追加し、client.py は操作 API 経由で同じ dispatch を呼ぶ
- 操作 API (FastAPI) は `App` を共有し、`_LockedAsk` と同じ core_lock を使う (Mission 実行は常にサービスプロセス内)。API 用にも専用 conn を開く (conn_core をイベントループから触らない)
- ClaudeRunner は `AgentRunner` 実装として追加し、`settings.runner.<loop>.backend == "claude"` のとき build_app が選択する
- 改善 loop は `improve` 用の registry サブセット (research_tools + 書き込み系) を別途組む — 取引判断 loop の読み取り専用 registry に書き込み系を混ぜない
- ChromaDB の `Rag` は改善 loop でもそのまま使う。plugin_loader は `get_indicators` の合成点 (market_tools.build に承認済み plugin の結果を追加) として実装する

---

## 追記 (2026-07-27): Phase 2 シグナル起動に向けた接合点 (Task 3 に含める)

設計書 §5「strategy シグナルによる Mission 起動」を受けた変更。**Phase 2 で `strategy` plugin のシグナルが取引判断 Mission を前倒し起動できる**ようにするため、Phase 1 の段階で 2 点だけ形を用意しておく。後から入れると `scheduler.tick()` の中核と DB migration に手が入るため、先に整えるほうが安い。

**シグナル検出・`signals` テーブル・`get_signals` ツールは Phase 2 であり、本プランには含めない。**

**実施タイミング (上から順に実装できるように)**:

| 変更 | 実施 Task | 理由 |
|---|---|---|
| 変更 1 (`missions.trigger` + migration + `start(trigger=)`) | **Task 1** | Task 3 の `run_once(trigger)` が依存する。後回しにすると Task 3 のテストが `TypeError` になる |
| 変更 2 (`_trade_mission_due` + `on_trade_mission(reason)`) | **Task 7** (サービス配線) | scheduler と `build_app` の両方に触るため、配線を組む Task で一度に行う |

### 変更 1: `missions.trigger` 列を追加する (migration 込み) — **Task 1 で実施**

`src/agentic_fx/store/db.py` の `missions` DDL に追加する:

```sql
CREATE TABLE IF NOT EXISTS missions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  loop TEXT NOT NULL,            -- trade | improve | ask | reflection
  runner TEXT NOT NULL, model TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'running',
  trigger TEXT,                  -- cron | signal:<plugin> (Phase 2)。loop='trade' 以外は NULL
  output_json TEXT, transcript_json TEXT,
  started_at TEXT NOT NULL, finished_at TEXT
);
```

**`trigger` は nullable にし、既定値を持たせない。** 全 loop 共通で `DEFAULT 'cron'` にすると、ask / improve / reflection まで「cron 起動」と記録され、「cron 起動の ask Mission」という無意味な行が生まれる。この列は**取引判断 Mission の起動理由**なので、`loop='trade'` のときだけ値を入れ、集計は必ず `WHERE loop='trade'` で絞る (設計書 §12)。

`src/agentic_fx/store/missions.py` の `start` に `trigger: str | None = None` を追加して INSERT に含める。既定が `None` なので既存の呼び出し (improve / ask / reflection) は無変更でよい。

#### migration が必須 (これを省くと既存 DB で必ず落ちる)

`init_db` は `CREATE TABLE IF NOT EXISTS` を使うため、**DDL に列を足しても既存の `data/agentic.db` には列が追加されない**。その状態で新しい `start()` が `INSERT ... trigger ...` を実行すると `sqlite3.OperationalError: table missions has no column named trigger` で落ちる。開発機には既に DB があるので、これは確実に踏む。

`src/agentic_fx/store/db.py` に列の追加処理を書き、`init_db` から呼ぶ:

```python
def _ensure_column(conn: sqlite3.Connection, table: str, column: str,
                   ddl: str) -> None:
    """存在しない列を追加する (SQLite は ADD COLUMN IF NOT EXISTS を持たない)。

    init_db は CREATE TABLE IF NOT EXISTS なので、既存 DB のテーブル定義は
    更新されない。列追加は PRAGMA で検査して ALTER する必要がある。
    """
    cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)
    _ensure_column(conn, "missions", "trigger", "trigger TEXT")
    conn.commit()
```

`ALTER TABLE ... ADD COLUMN trigger TEXT` は nullable かつ既定値なしなので、既存行はすべて `NULL` になる (改修前の Mission は起動理由が不明であり、`NULL` はその事実の正しい表現である)。

**migration のテストを書くこと** (現行 `tests/store/test_db.py` は空 DB 作成と同一スキーマでの再実行しか見ていない):

```python
def test_init_db_adds_trigger_column_to_legacy_missions_table(tmp_path):
    """旧スキーマの DB に init_db を流すと trigger 列が追加される。"""
    p = tmp_path / "legacy.db"
    conn = connect(p)
    # trigger 列を持たない旧 missions テーブルを手で作る
    conn.execute("CREATE TABLE missions ("
                 "id INTEGER PRIMARY KEY AUTOINCREMENT, loop TEXT NOT NULL, "
                 "runner TEXT NOT NULL, model TEXT NOT NULL, "
                 "status TEXT NOT NULL DEFAULT 'running', "
                 "output_json TEXT, transcript_json TEXT, "
                 "started_at TEXT NOT NULL, finished_at TEXT)")
    conn.execute("INSERT INTO missions (loop, runner, model, started_at) "
                 "VALUES ('trade','local','m','2026-07-22T12:00:00+00:00')")
    conn.commit()

    init_db(conn)   # ここで ALTER が走る

    cols = {r["name"] for r in conn.execute("PRAGMA table_info(missions)")}
    assert "trigger" in cols
    assert conn.execute("SELECT trigger FROM missions").fetchone()[0] is None
    # 追加後に新しい start() が通ること
    mid = missions.start(conn, "trade", "local", "m", NOW, trigger="cron")
    assert conn.execute("SELECT trigger FROM missions WHERE id=?",
                        (mid,)).fetchone()[0] == "cron"
```

**注意**: `TRIGGER` は SQLite のキーワード (`CREATE TRIGGER`) だが、列名としては引用符なしで使える。実測で確認済み:

```
$ python3 -c "import sqlite3; c=sqlite3.connect(':memory:'); \
  c.execute('CREATE TABLE t (id INTEGER PRIMARY KEY, trigger TEXT)'); \
  c.execute(\"INSERT INTO t (trigger) VALUES ('cron')\"); \
  c.execute('ALTER TABLE t ADD COLUMN trigger2 TEXT'); \
  print(c.execute('SELECT trigger FROM t').fetchall())"
[('cron',)]
```

INSERT では列名を明示すること (現行の `start` は既に明示しているので形は変わらない)。

### 変更 2: `scheduler.tick()` の Mission 起動を「起動理由を返す関数」にする — **Task 7 で実施**

現在 (`src/agentic_fx/core/scheduler.py`):

```python
if self._last_trade is None or now - self._last_trade >= timedelta(hours=1):
    self._last_trade = now
    self.on_trade_mission()
```

変更後:

```python
reason = self._trade_mission_due(now)
if reason is not None:
    self._last_trade = now
    self.on_trade_mission(reason)

# ---- internal ----
def _trade_mission_due(self, now: datetime) -> str | None:
    """取引判断 Mission の起動理由を返す。起動不要なら None。

    Phase 2 でシグナル起動 (`"signal:<plugin>"`) が加わる唯一の分岐点。
    """
    if self._last_trade is None or now - self._last_trade >= timedelta(hours=1):
        return "cron"
    return None
```

`on_trade_mission` の型は `Callable[[str], None]` になる。`TradeLoop.run_once(trigger)` をそこに差し込む。

#### シグネチャ変更に伴う既存の呼び出し側の修正 (漏らすと `TypeError`)

型が変わるので、`on_trade_mission` を渡している・呼んでいる箇所をすべて直す。**プランを上から順に実装すると Task 7 で初めて壊れるため、ここに一覧を置く**:

| ファイル | 現状 | 変更後 |
|---|---|---|
| `src/agentic_fx/core/scheduler.py` | `on_trade_mission: Callable[[], None]` | `Callable[[str], None]` |
| 本プラン Task 7 `build_app` | `def on_trade_mission() -> None:` → `trade_loop.run_once()` | `def on_trade_mission(trigger: str) -> None:` → `trade_loop.run_once(trigger)` (修正済み) |
| 本プラン Task 7 のテスト | `app.scheduler.on_trade_mission()` | `app.scheduler.on_trade_mission("cron")` (修正済み) |
| `tests/core/test_scheduler.py` の `Env._trade()` | 引数なしのスタブ | `def _trade(self, trigger): ...` (呼ばれた trigger を記録すると後段の検証に使える) |
| `tests/core/test_e2e_paper_cycle.py` | `on_trade_mission=lambda: None` | `on_trade_mission=lambda trigger: None` |

### テスト (Task 3 の Step に追加する)

**単体 2 本だけでは不足する。** `run_once` の既定値と `_trade_mission_due` を別々に検証しても、両者をつなぐ `build_app` の wrapper が `trigger` を捨てていた場合に検出できない — 今回もっとも壊れやすいのはその wrapper である。通しの伝播テスト (Task 7 の `test_tick_propagates_trigger_to_missions_row`) を必ず併せて書くこと。

```python
def test_mission_trigger_is_recorded_as_cron():
    """cron 起動の Mission は missions.trigger = 'cron' で記録される。"""
    # TradeLoop.run_once() を既定引数で実行し、missions 行の trigger を検証する


def test_signal_trigger_is_recorded_verbatim():
    """任意の起動理由文字列がそのまま記録される (Phase 2 の "signal:<plugin>" 用)。"""
    # TradeLoop.run_once("signal:demo") を実行し、trigger == "signal:demo" を検証


def test_non_trade_missions_have_null_trigger():
    """ask / reflection の Mission は trigger が NULL のままである。"""
    # TradeLoop.ask_once(...) 実行後、その missions 行の trigger が None であること


def test_trade_mission_due_returns_cron_then_none():
    """1 時間経過で "cron"、直後の tick では None を返す (二重起動しない)。"""
    # Scheduler._trade_mission_due を直接呼び、境界を検証する
```

`_trade_mission_due` が理由文字列を返すことをテストで固定しておくと、Phase 2 で分岐を足すときに既存挙動の回帰が検出できる。

### Phase 2 への申し送り (本プランでは実装しない)

設計書 改訂第 13 版 §5 で決めた以下は、シグナル起動を実装する際に必ず反映すること。Phase 1 では `_trade_mission_due` が `"cron" | None` を返すだけなので、まだ関係しない:

- シグナルは `pending / claimed / consumed / abandoned` の 4 状態。claim は原子的に行い、Mission 失敗時は `pending` へ戻す (再キュー上限あり)。lease 期限切れの `claimed` は回収する
- **cron の締切とシグナルのレート制限を別変数で持つ** (`_last_cron_mission` / `_last_signal_mission`)。シグナル起動で cron の締切を後ろへずらすと、シグナルが続く限り定期実行が事実上停止する
- レート制限は口座全体の単位・DB 永続。排他スロットは**非ブロッキング取得**にし、取れなければ起動を諦めてシグナルは `pending` に残す

---

## 追記 (2026-07-31): プラン 4 吸収事項の織り込み (16 項目)

出典: プラン 4 レジャー (`.superpowers/sdd/2026-07-26-phase1-4-runner-tools/progress.md`) 末尾「プラン 5 が吸収すべき事項」。プラン 4 の最終ブランチレビュー (fable)・codex 節目レビュー・fix wave 再レビューで「プラン 5 送り」と裁定された全項目を、本プランの各 Task に割り当てる。**本追記はプラン本文と同格の要求仕様であり、衝突時は本追記が優先する** (本文より新しい裁定のため)。

### 冒頭「消費する契約 (変更禁止)」への例外

本追記の実施のため、以下の**プラン 4 成果物の変更を許可する** (それ以外は引き続き変更禁止):

| ファイル | 許可する変更 | 該当項目 |
|---|---|---|
| `src/agentic_fx/runners/local_runner.py` | A-6 の分岐ガード追加 / `close()` 追加 / W6 コメント数値修正 | A-6, chore |
| `src/agentic_fx/tools/market_tools.py` | `_pair_param` の pair を enum 化 | A-4 |
| `src/agentic_fx/tools/reflection_tools.py` | `build(conn, rag, pairs)` — pairs 引数追加 + pair enum 化 | A-4 |
| `src/agentic_fx/tools/news_tools.py` | 射影の `.get()` 化 | chore |
| `src/agentic_fx/datafeed/bars.py` | `bars_to_df` に昇順ソート追加 | A-8 |
| `src/agentic_fx/store/reflections.py` | ORDER BY タイブレーク追加 | chore |

既存テストの**期待値を弱める変更は不可** (whitelist ピン: get_ohlcv / get_indicators の properties キー集合は `{pair, timeframe}` のまま — enum 化は値の制約追加でありキー集合を変えない)。

### Task 0 (新設): プラン 4 残穴 + chore 一括 — **Task 1 の前に実施**

小粒・機械的な修正のバンドル。1 dispatch で完結させる。

**Files:** Modify: `src/agentic_fx/runners/local_runner.py`, `src/agentic_fx/datafeed/bars.py`, `src/agentic_fx/store/reflections.py`, `src/agentic_fx/tools/news_tools.py` / Test: 各既存テストファイルに追記

- [ ] **0-1 (A-6): tool_calls + 非文字列 content 複合形のガード** — `local_runner.py` の tool_calls 分岐 (`continue` の前) で、`normalized_msg` の content が `None` でも `str` でもない場合に W1 と同じ `json.dumps(content, ensure_ascii=False, default=str)` で文字列化する (W1 は content-only 分岐にしか効かず、複合形は素の非文字列のまま履歴に残り、型に厳しい互換サーバで次 POST が 400 になる — fix wave 再レビューの DEFER-TO-PLAN-5 裁定分)。**リトライ計上はしない** (tool_calls 分岐の意味論を変えない — 文字列化のみ)。テスト: tool_calls + dict content の応答 → 2 回目のリクエスト body で当該 assistant message の content が文字列であり、Mission は completed
- [ ] **0-2 (chore): `LocalRunner.close()`** — `self._client.close()` を呼ぶメソッドを追加。テスト: close() が例外なく呼べることのみ。**所有権と終了経路は Task 7 で定義する** (A-1 実装の項を参照): `App` に `runner` と `owns_runner: bool` (build_app 内生成なら True、注入なら False) を追加し、shutdown で **`th.join(timeout=30)` が成功した場合のみ** `owns_runner and isinstance(runner, LocalRunner)` のとき close する。join タイムアウト時は close しない (使用中の client を別スレッドから閉じない)
- [ ] **0-3 (chore): W5 ensure_ascii 回帰テスト** — ツール引数の破損 JSON (日本語を含む) → tool message の content に生の UTF-8 が残る (\\uXXXX エスケープでない) ことをピンする (fix wave W5 の revert 検知)
- [ ] **0-4 (chore): W6 較正コメントの数値修正** — `tests/runners/` の `test_timeout_during_parse_retry` コメントを実測値に修正: **deadline=6.1、終端呼び出し n=11 t=6.6** (final-rereview.md の instrumented 実測。旧記載 deadline=5.5 / n=10 t=6.0 は誤り)。コメントのみの 1 行 diff
- [ ] **0-5 (A-8): bars 昇順の防御ソート + 契約テスト** — `bars_to_df` の返す DataFrame を ts 昇順にソートする (`get_ohlcv` の `.tail(100)` = 「直近 100 本」は昇順前提。yfinance は昇順だが、Phase 3 の MT5 bridge がこの契約を知らずに壊すのを防ぐ)。実装は DataFrame 構築後に `return df.sort_index(kind="stable")` (同一 ts は入力順維持 — 安定ソート指定)。テスト: ①シャッフル入力 → 昇順 ②降順入力 → 昇順 ③空入力で columns / UTC index が維持される ④get_ohlcv の最終要素が最大 ts
- [ ] **0-6 (chore): reflections の ORDER BY タイブレーク** — `recent_for_pair` / `recent` の `ORDER BY created_at DESC` に `, order_id DESC` (または `id DESC` — 実カラム名に合わせる) を追加。テスト: 同一 created_at の 2 行で決定的順序
- [ ] **0-7 (chore): search_news 射影の `.get()` 化** — `{title, body, source_name}` 射影を `r.get(...)` にする (rag 側のメタデータ欠損行でツールが execute の例外経路に落ちない)。テスト: source_name 欠損の hit → エラーでなく `None` 入りで返る
- [ ] **0-8 (A-4 前半): ツール schema の pair enum 化** — `market_tools._pair_param` の pair を `{"enum": list(settings.pairs)}` に (settings は既に build が受けている)。`reflection_tools.build(conn, rag, pairs: list[str])` に引数を追加し `get_recent_reflections` の pair を enum 化。**全呼び出し元を `rg -n "reflection_tools\.build" src tests` で洗い出して追随修正する** (現状 `tests/tools/test_tool_impls.py` に 3 箇所。本番配線は Task 7 — 上書き済みの `reflection_tools.build(conn_core, rag, settings.pairs)` を使う)。テスト: registry.execute で pairs 外の pair → validation エラー JSON (execute は無送出契約のまま)
- [ ] **Commit**: `fix: プラン 4 残穴 (複合形ガード・pair enum・昇順契約) + chore 一括`

### 割当表 (吸収 16 項目 → 実施 Task)

| # | 項目 | 実施 Task | 裁定 | 種別 |
|---|---|---|---|---|
| A-1 | mission deadline のブロッキング外強制 + ツール資源 cap | Task 3 / 4 / 7 (MissionWatch) | **代替実装** (強制 → 検知+通知。preemption は Phase 2) | codex 必須 |
| A-2 | /v1/models モデル ID 厳密検証 + cold-load smoke | Task 7 (init 上書き) | 実装 | codex 必須 |
| A-3 | never-raise サービス境界 (schema 事前検証・finalize・slot 解放・transcript 機微) | Task 2 / 3 / 5 / 7 に分散 | 実装 (schema 検証は build_app 起動時 + テスト) | codex 必須 |
| A-4 | pair を settings 由来 enum に | Task 0 (ツール schema) + Task 2 (intent schema) | 実装 | 最終レビュー M-1 |
| A-5 | wiring assert `set(mission.tools) <= set(registry.names())` | Task 7 | 実装 | 最終レビュー M-6 |
| A-6 | tool_calls + 非文字列 content 複合形ガード | Task 0 | 実装 | DEFER-TO-PLAN-5 |
| A-7 | wiring テストは本番ファクトリ import | Task 7 / 8 (レビュー制約) | 実装 (制約) | 繰り越し |
| A-8 | バー昇順の契約テスト | Task 0 | 実装 | 繰り越し |
| A-9 | EURUSD 解禁 hard precondition 2 件 | 非対象 | **明示見送り** (解禁前チェックリストとして記録) | 繰り越し |
| A-10 | snapshot 順序再設計 / ask 回答専用 | 前者は非対象、後者は本文 Task 3 で織り込み済み | 前者 **明示見送り** (バックテスト spec 時) / 後者 実装済み | 繰り越し |
| chore | タイブレーク / close() / W5 / W6 / .get() | Task 0 | 実装 | chore |
| chore | mypy 導入 | 非対象 | **明示見送り** (Phase 1 完了後の独立 chore) | chore |

### A-1: mission deadline の外部強制 — 裁定と実装 (Task 7)

**裁定: Phase 1 は「検知 + 通知」方式とし、preemption (強制中断) は Phase 2 のプロセス隔離まで送る。**

根拠: ①Python スレッドは外部から kill できない ②実行放棄 (thread abandon) は、ツール群が保持する conn_core を放棄スレッドと次 tick が同時使用する競合を作り、ハング以上の正しさ危険がある ③現実のハング源は llama-swap HTTP で、httpx per-phase timeout (残り時間渡し) + localhost + monit watchdog + プラン 3 ツール自前 timeout が既に押さえている ④埋まっていない穴は「15 分想定の Mission が 3 時間走っても誰も気付かない」という**可観測性**であり、これは watchdog で閉じる。

**注意 (縮退の明示)**: この裁定により「timeout_sec で必ず抜ける」は Phase 1 では**保証されない** (LocalRunner の各段 deadline 確認 + httpx per-phase timeout による近似のみ)。Phase 2 の preemption 実装 (プロセス隔離) の受入条件: worker process への Mission 隔離 / 子プロセス側 DB 接続の分離 / 親の壁時計監視と terminate→kill エスカレーション / kill 後の missions 行 timeout finalize / transcript の保存範囲 (kill 時点までの部分 transcript) の定義。

**ツール資源 cap**: 総ツール実行数は既に `max_turns × _MAX_TOOL_CALLS_PER_TURN(=16)` で上界がある (プラン 4 W3)。新規コードは追加しない。

**計測点 (codex レビュー指摘)**: watch の begin/end は複合コールバック (`on_trade_mission` = trade + reflection をまとめて実行) に置いては**いけない** — 計測対象が「run_once 全時間 + run_pending 全時間」になり、個別 Mission の timeout と一致しない (trade 250 秒正常完了 + reflection 150 秒実行中 = 合算 400 秒で、deadline 内の reflection を誤 breach する)。begin/end は**個々の `runner.run(mission)` を所有する層**に置く: `TradeLoop._run_recorded` (trade / ask を loop 引数で区別) と `ReflectionCycle._reflect_one` の runner 呼び出し部。

**実装**:

- **Task 3**: `src/agentic_fx/loops/mission_watch.py` に `MissionWatch` を新設:

```python
@dataclass(frozen=True)
class MissionWatchEntry:
    mission_id: int
    loop: str
    started: float
    timeout_sec: float
    notified: bool = False

class MissionWatch:
    def __init__(self, time_fn=time.monotonic) -> None:
        self._lock = threading.Lock()
        self._entry: MissionWatchEntry | None = None
        self._time = time_fn

    def begin(self, mission_id: int, loop: str, timeout_sec: float) -> None:
        with self._lock:
            if self._entry is not None:
                # 全 Mission は core_lock 下で直列のため通常起きない。
                # 起きても raise しない (begin の例外で missions.finish が
                # 飛ばされる方が害が大きい) — 警告して上書き
                _log.warning("mission watch slot occupied by #%s — overwriting",
                             self._entry.mission_id)
            self._entry = MissionWatchEntry(mission_id, loop, self._time(),
                                            timeout_sec)

    def end(self, mission_id: int) -> None:
        with self._lock:
            if self._entry is not None and self._entry.mission_id == mission_id:
                self._entry = None

    def mark_notified(self, mission_id: int) -> None: ...   # notified を True に

    def breached(self, grace_sec: float = 60.0) -> MissionWatchEntry | None:
        """timeout + grace 超過かつ未通知なら entry を返す。それ以外 None。"""
```

  `TradeLoop.__init__` / `ReflectionCycle.__init__` に `watch: MissionWatch | None = None` を追加 (None なら内部で新規生成 — 既存テストは無変更で通る)。`_run_recorded` (と ReflectionCycle の同型部) で `watch.begin(mid, loop, mission.timeout_sec)` を **try ブロックの内側**で呼び、finally で `watch.end(mid)` (begin が万一失敗しても finish が必ず走る順序にする)
- **Task 4**: ReflectionCycle の runner 呼び出しを同型で begin/end する (loop="reflection")
- **Task 7**: build_app が `MissionWatch` を 1 つ生成して TradeLoop / ReflectionCycle に注入し、`App.mission_watch` に持つ。run_service に watchdog スレッド (30 秒毎): `entry = app.mission_watch.breached(grace_sec=60)` が entry を返したら activity SYSTEM `mission_watchdog_breach` (mission_id / loop / 経過秒を含める) + notifier 通知し `mark_notified(entry.mission_id)` (**Mission あたり 1 回だけ**)。**missions 行には書かない** (行の finalize は `_run_recorded` の finally が唯一の所有者 — 二重終端を作らない)
- テスト (fake monotonic clock で、スレッドは使わない): ①timeout+grace 未満は breached() が None ②超過で entry ③mark_notified 後は None ④end 後は None ⑤別 Mission の begin で notified がリセットされる (新 entry) ⑥trade 終了 → reflection 開始のとき、trade 開始からの合算でなく reflection 開始からの経過で判定される。watchdog スレッド配線は E2E では検証しない (スリープ依存テストを作らない — Task 7 セルフレビューで確認)

### A-2: llama-swap init 検証の上書き (Task 7)

本文 Task 7 の「`GET {base_url}/models`、失敗は警告のみ」を以下に**置き換える** (警告のみの方針は維持 — init は助言であり、サービスはオフラインでも起動できる)。3 種の失敗 (一覧取得不能 / モデル不在 / smoke 失敗) を**構造で区別する** — 単一 try に潰すと POST の 4xx/5xx を「OK」と誤報する (codex レビュー Critical 1):

```python
def _check_llama_swap(settings) -> None:
    import httpx
    base = settings.llama_swap.base_url
    model = settings.runner.trade.model

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

    if model not in ids:
        print(f"警告: モデル '{model}' が llama-swap の /models に存在しません。"
              f"alias 設定を確認してください (存在: {ids})")
        return

    try:
        # cold-load smoke: TTL unload 後の初回 Mission がロード時間で
        # timeout しないよう、1 トークン生成でロードを促す
        r = httpx.post(f"{base}/chat/completions",
                       json={"model": model, "max_tokens": 1,
                             "messages": [{"role": "user", "content": "ping"}]},
                       timeout=120)
        r.raise_for_status()
    except (httpx.RequestError, httpx.HTTPStatusError) as e:
        print(f"警告: モデル '{model}' の cold-load smoke に失敗しました ({e})。"
              "初回 Mission が timeout する可能性があります。")
        return

    print(f"llama-swap OK (model '{model}' loaded)")
```

`run_init` はこの `_check_llama_swap(settings)` を呼ぶ。**既存の init テスト (`tests/test_init_and_guard.py` のオフライン系 3 本) には `_check_llama_swap` 自体を patch して適用する** — mock 漏れで `timeout=120` の実待ちが混入するのを防ぐ。テスト (httpx を mock): ①GET 成功・モデル在・POST 成功 → "OK" ②モデル不在 → 警告にモデル名 ③GET 接続例外 → 一覧取得警告 ④GET 500 → 同 ⑤POST 500 → smoke 警告 (「OK」を出さない) ⑥POST timeout → smoke 警告。

### A-3: never-raise サービス境界の分散割当

- **Task 2**: `TRADE_INTENT_SCHEMA` / `ANSWER_SCHEMA` / `trade_intent_schema(pairs)` の返り値に対し `jsonschema.Draft202012Validator.check_schema(...)` が通ることをテストでピンする
- **Task 7 (起動時検証)**: schema はテストだけでは足りない — `trade_intent_schema(pairs)` は**実行時設定から動的生成**され、誤設定 (例: `pairs: []` → `"enum": []` は JSON Schema として不正) はテストでは検出できない。`build_app` が**実際に使用する schema** (trade = `trade_intent_schema(settings.pairs)` / ask = `ANSWER_SCHEMA` / reflection = `_SCHEMA`) を `Draft202012Validator.check_schema` で起動時検証し、失敗は `RuntimeError` で即落とす (A-5 の wiring assert と同じ「配線ミスは起動時に殺す」方針)。あわせて `settings.pairs` が空でないことも assert。テスト: `pairs: []` 相当の settings で build_app が `RuntimeError`
- **Task 3 (never-raise 公開境界 — 本文 Task 3 の上書き)**: 本文の `_run_recorded` は `runner.run` しか包んでおらず、その外側に例外源が多数ある (healthcheck の非 DataUnhealthy 例外 / `_build_prompt` 内の OSError・SQLite 例外 / `missions.start` の sqlite3.Error / `from_llm_dict` の想定外 TypeError / `executor.handle_intent` / activity・notifier の書き込み失敗 / ask の `result.output["answer"]` の KeyError — codex レビュー Critical 2)。二層に分ける:
  1. `run_once` / `ask_once` は**実装を `_run_once_impl` / `_ask_once_impl` に退避し、公開側は全体を `try/except Exception`** で包む (BaseException は捕らえない)。捕捉時: 技術ログ exception + activity SYSTEM `mission_boundary_failed` + notifier — この 3 つの報告自体も個別 try/except で握る (`_safe_report_boundary_failure(loop)`) — し、run_once は None、ask_once は `"(Mission 失敗: internal_error)"` を返す
  2. `_run_recorded` の強化: runner の返り値が `MissionResult` でない場合も failed に正規化 / `missions.finish` 失敗は技術ログに加えて activity SYSTEM `mission_finalize_failed` を試行 (それも失敗したらログのみ) / MissionWatch の begin は try 内・end は finally (A-1 参照)
  3. ask の completed 出力検証: `result.output` が dict でない・`answer` が str でない場合は失敗扱いの文字列を返す (FakeRunner 注入や LLM の schema すり抜けで KeyError にしない)
  - テスト: healthcheck / build_state_summary / missions.start / runner / missions.finish / executor / activity / notifier のそれぞれに例外を注入し、公開境界 (`run_once` / `ask_once`) から**例外が漏れない**ことを 1 経路ずつ検証する
- **Task 5 / 7**: **transcript_json は機微データ** (プロンプト = policy 全文の末尾 + 口座状態 + ニュース本文を含む)。シェルコマンド (`status` / `log` / `activity`)・スプラッシュ・notifier 通知のいずれにも transcript を出さない。保存先は missions.transcript_json のみ。レビュー constraints に明記
- **Task 7**: core_lock の取得は **`with` 文限定** (手動 acquire/release 禁止 — 全終端経路での解放を構文で保証)。セルフレビューで `grep -n "core_lock.acquire\|core_lock.release" src/` がヒットしないこと。scheduler_thread の最外周 `except Exception` (本文どおり) はスレッド死亡防止であり Mission 単位の正規化の代替ではない — 両方必要

### A-4 後半: TradeIntent schema の pair enum 化 (Task 2)

本文 Task 2 の `TRADE_INTENT_SCHEMA` 定数はそのまま維持し (「pair enum なし」の基底形)、**`trade_intent_schema(pairs: list[str]) -> dict`** を追加する: `copy.deepcopy(TRADE_INTENT_SCHEMA)` に `properties.pair["enum"] = list(pairs)` を設定して返す。Task 3 の TradeLoop は `trade_intent_schema(self.settings.pairs)` を使う (**本文 Task 3 の `output_schema=TRADE_INTENT_SCHEMA` を上書き**)。

最終防衛は引き続き `TradeIntent.from_llm_dict` + executor — enum は LLM への誘導と早期拒否 (schema リトライで LLM に「pairs 外」を伝えられる) であり、防衛線の置き換えではない。

テスト (Task 2 に追加): `trade_intent_schema(["USDJPY"])` で `pair: "EURUSD"` の open が ValidationError / `TRADE_INTENT_SCHEMA` 自体は pair 自由のまま (基底形の回帰ピン)。

**Task 7 の本番配線上書き (0-8 のシグネチャ変更の追随 — 漏らすと build_app が TypeError)**: 本文 Task 7 の `registry.register_all(reflection_tools.build(conn_core, rag))` を **`reflection_tools.build(conn_core, rag, settings.pairs)`** に置き換える (market_tools は既に settings を受けるため無変更)。テスト (Task 7 に追加): `build_app` 後、`app.registry.openai_tools(["get_ohlcv"])` の pair enum が `app.settings.pairs` と一致すること (配線がテスト用の値でなく本番 settings を通っている検証)。

### A-5: wiring assert (Task 7)

`build_app` の registry 組み立て直後に検証する:

```python
    missing = set(_TRADE_TOOLS) - set(registry.names())
    if missing:
        raise RuntimeError(f"tools not registered: {sorted(missing)}")
```

(`_TRADE_TOOLS` は trade_loop から import。`openai_tools` は未知名を無音で落とすため、typo は起動時に殺す — 最終レビュー M-6。) テスト: registry 登録を 1 つ欠いた状態を作り `RuntimeError` を確認 (検証部を関数 `_assert_tools_registered(registry, names)` に切り出すとテストしやすい)。

### A-7: レビュー制約 (Task 7 / 8)

配線のテストは**本番ファクトリ (`build_app`) を import して使う**こと。テストローカルに部品を再組み立てして配線を再現する形 (旧 `_env` 再構築) は、本番配線の欠陥を検出できないため禁止。本文 Task 7/8 のテストは既にこの形 — 追加テストにも同じ制約を適用する。

### A-9: EURUSD 解禁前チェックリスト (非対象 — 記録のみ)

Phase 1 は `pairs: [USDJPY]` のまま。EURUSD (クォート通貨 ≠ 口座通貨) を settings.pairs に足す前に、以下 2 件を実装・検証すること:

1. pips→口座通貨換算 (為替レート取得) — settings.yaml.example のコメントに既記載
2. `realized_pnl` が NULL の行を含む会計集計 (daily_start_equity / 累計 P&L) の正しさ検証

pair enum 化 (A-4) により、settings に足さない限り LLM もツールも EURUSD に触れない — 解禁は settings 変更 + 上記 2 件が揃った時点の明示判断。

### A-10 / その他の非対象裁定

- **snapshot 順序依存の再設計**: バックテスト spec 追記 (プラン 5 完了後のタスク) で扱う。本プランでは触れない
- **mypy 導入 (chore)**: 見送り。8 タスク全部のレビューに型ノイズが乗り、導入自体が全ファイル横断変更になる。Phase 1 完了後に独立 chore として実施する
- **W1 による transcript 非逐語化** (再レビュー note): Task 4 の reflection は order 行 + intent reasoning を材料とし、**transcript を ground truth として使わない** (本文は既にその設計 — レビュー constraints に明記して固定する)
