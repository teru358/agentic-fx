# Phase 1 プラン 4: Runner + tools Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** AgentRunner 抽象 (Mission/MissionResult/FakeRunner/LocalRunner) とツールレジストリ + Phase 1 ツール群を、FakeRunner と httpx MockTransport だけで完全にテスト可能な形で構築する。

**Architecture:** `runners/` に AgentRunner 抽象と LocalRunner (llama-swap への自前 tool-calling loop)、`tools/` にレジストリ (OpenAI function calling スキーマ変換 + 素関数) とツール実装 (プラン 3 の datafeed/store API の薄いラッパー)。ClaudeRunner (Agent SDK) と MCP 変換は **Phase 2** — ただし AgentRunner 境界はここで固定する。

**Tech Stack:** httpx (MockTransport でテスト) / jsonschema / プラン 1〜3 の契約を消費

## Global Constraints (設計書より)

- LocalRunner: 応答に tool_calls があれば実行して継続、なければ最終出力を `output_schema` で検証して終了。`max_turns` / `timeout_sec` 超過で強制終了 (status に反映) — llama-swap TTL デッドロックへの防御線 (設計書 §4)
- JSON 修復: `<think>` 除去・コードフェンス剥がし。**修復不能リトライは上限 2 回、リトライも max_turns に算入、timeout_sec が常に優先** (設計書 §4)
- スキーマ検証失敗はエラー内容を LLM に返して再出力 (上限 2 回、同上)
- ツールは 1 箇所 (`tools/`) に定義し、①OpenAI スキーマ ②(Phase 2) MCP ③pytest から素関数、の 3 形態 (設計書 §4)。本プランは ①③ を実装し、② の変換口だけ確保する
- 取引判断 loop のツールは**すべて読み取り専用** (設計書 §5)
- Phase 1 の最小ツールセット: `get_ohlcv / get_indicators / search_news / get_positions` (設計書 §15)。`get_econ_calendar / get_account / reflection 系` は**前倒し実装** (状態サマリ・振り返りが Phase 1 の学習モードに必要なため — スペック §5 のツール一覧に対応)
- Anthropic API (従量課金) は使わない。本プランに Claude 関連コードは登場しない
- コミットメッセージ末尾: `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`

**プラン 1〜3 から消費する契約** (変更禁止): `Settings.llama_swap (base_url/timeout_sec/max_turns)`、`Settings.runner (trade/improve: backend/model)`、`PriceProvider.get_bars/spec`、`compute_indicators/bars_to_df/resample`、`Rag.search_news/search_reflections`、`EconCalendar.upcoming`、`PaperBroker.equity`、`orders.list_by_status`、`reflections.recent`

---

### Task 1: AgentRunner 抽象 + FakeRunner (runners/base.py, fake_runner.py)

**Files:**
- Create: `src/agentic_fx/runners/__init__.py`, `src/agentic_fx/runners/base.py`, `src/agentic_fx/runners/fake_runner.py`
- Test: `tests/runners/__init__.py`, `tests/runners/test_base.py`

**Interfaces:**
- Produces (設計書 §4 の形をそのまま固定):
  - `@dataclass Mission(prompt: str, tools: list[str], output_schema: dict, max_turns: int, timeout_sec: float)`
  - `@dataclass MissionResult(status: Literal["completed", "failed", "timeout", "max_turns"], output: dict | None, transcript: list[dict])`
  - `class AgentRunner(ABC)`: `run(self, mission: Mission) -> MissionResult`
  - `class FakeRunner(AgentRunner)`: `__init__(self, results: list[MissionResult])` — run のたびに先頭から返す。`self.missions: list[Mission]` に受信 Mission を記録 (loop テストの検証点)。結果が尽きたら最後の結果を繰り返す

- [ ] **Step 1: 依存を追加**

```bash
uv add jsonschema
```

- [ ] **Step 2: 失敗するテストを書く**

`tests/runners/test_base.py`:

```python
from agentic_fx.runners.base import AgentRunner, Mission, MissionResult
from agentic_fx.runners.fake_runner import FakeRunner


def _mission(prompt="do"):
    return Mission(prompt=prompt, tools=[], output_schema={"type": "object"},
                   max_turns=4, timeout_sec=30)


def test_fake_runner_returns_scripted_results():
    r1 = MissionResult(status="completed", output={"a": 1}, transcript=[])
    r2 = MissionResult(status="failed", output=None, transcript=[])
    fake = FakeRunner([r1, r2])
    assert fake.run(_mission("m1")).output == {"a": 1}
    assert fake.run(_mission("m2")).status == "failed"
    assert fake.run(_mission("m3")).status == "failed"  # 尽きたら最後を繰り返す
    assert [m.prompt for m in fake.missions] == ["m1", "m2", "m3"]


def test_fake_runner_is_agent_runner():
    assert isinstance(FakeRunner([]), AgentRunner)
```

- [ ] **Step 3: テストが失敗することを確認**

Run: `uv run pytest tests/runners/test_base.py -v`
Expected: FAIL (ImportError)

- [ ] **Step 4: 実装**

`src/agentic_fx/runners/base.py`:

```python
"""AgentRunner 抽象 — 設計書 §4 の Mission/MissionResult をそのまま固定。"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Literal


@dataclass
class Mission:
    prompt: str
    tools: list[str]
    output_schema: dict
    max_turns: int
    timeout_sec: float


@dataclass
class MissionResult:
    status: Literal["completed", "failed", "timeout", "max_turns"]
    output: dict | None
    transcript: list[dict] = field(default_factory=list)


class AgentRunner(ABC):
    @abstractmethod
    def run(self, mission: Mission) -> MissionResult: ...
```

`src/agentic_fx/runners/fake_runner.py`:

```python
"""テスト用 Runner — 定型 MissionResult を返し、受信 Mission を記録する。"""
from __future__ import annotations

from agentic_fx.runners.base import AgentRunner, Mission, MissionResult


class FakeRunner(AgentRunner):
    def __init__(self, results: list[MissionResult]) -> None:
        self._results = list(results)
        self._i = 0
        self.missions: list[Mission] = []

    def run(self, mission: Mission) -> MissionResult:
        self.missions.append(mission)
        if not self._results:
            return MissionResult(status="failed", output=None, transcript=[])
        result = self._results[min(self._i, len(self._results) - 1)]
        self._i += 1
        return result
```

- [ ] **Step 5: テストが通ることを確認**

Run: `uv run pytest tests/runners/test_base.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml uv.lock src/agentic_fx/runners tests/runners
git commit -m "feat: AgentRunner 抽象 + FakeRunner"
```

---

### Task 2: JSON 修復 (runners/response_parser.py)

**Files:**
- Create: `src/agentic_fx/runners/response_parser.py`
- Test: `tests/runners/test_response_parser.py`

**Interfaces:**
- Produces:
  - `ParseError(ValueError)`
  - `parse_json_output(text: str) -> dict` — ①`<think>...</think>` 除去 ②コードフェンス (```json ... ```) 剥がし ③テキスト中の最初のバランスした `{...}` を抽出して json.loads。すべて失敗なら `ParseError`

- [ ] **Step 1: 失敗するテストを書く**

`tests/runners/test_response_parser.py`:

```python
import pytest

from agentic_fx.runners.response_parser import ParseError, parse_json_output


def test_plain_json():
    assert parse_json_output('{"action": "hold"}') == {"action": "hold"}


def test_think_tag_stripped():
    text = "<think>色々考えた</think>\n{\"action\": \"hold\"}"
    assert parse_json_output(text)["action"] == "hold"


def test_code_fence_stripped():
    text = "```json\n{\"action\": \"hold\"}\n```"
    assert parse_json_output(text)["action"] == "hold"


def test_json_embedded_in_prose():
    text = "判断は以下の通りです。 {\"action\": \"hold\", \"reasoning\": \"a{b}\"} 以上。"
    assert parse_json_output(text)["reasoning"] == "a{b}"


def test_nested_braces():
    text = 'result: {"a": {"b": 1}, "c": 2}'
    assert parse_json_output(text) == {"a": {"b": 1}, "c": 2}


def test_unparsable_raises():
    with pytest.raises(ParseError):
        parse_json_output("no json here")


def test_broken_json_raises():
    with pytest.raises(ParseError):
        parse_json_output('{"action": "hold"')
```

- [ ] **Step 2: テストが失敗することを確認**

Run: `uv run pytest tests/runners/test_response_parser.py -v`
Expected: FAIL (ImportError)

- [ ] **Step 3: 実装**

`src/agentic_fx/runners/response_parser.py`:

```python
"""JSON 修復 (前身 response_parser の移植): think 除去・フェンス剥がし・波括弧抽出。"""
from __future__ import annotations

import json
import re


class ParseError(ValueError):
    pass


_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)
_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def _first_balanced_object(text: str) -> str | None:
    start = text.find("{")
    while start != -1:
        depth = 0
        in_str = False
        esc = False
        for i in range(start, len(text)):
            ch = text[i]
            if esc:
                esc = False
                continue
            if ch == "\\":
                esc = True
            elif ch == '"':
                in_str = not in_str
            elif not in_str:
                if ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        return text[start:i + 1]
        start = text.find("{", start + 1)
    return None


def parse_json_output(text: str) -> dict:
    cleaned = _THINK_RE.sub("", text)
    fence = _FENCE_RE.search(cleaned)
    if fence:
        cleaned = fence.group(1)
    for candidate in (cleaned.strip(), _first_balanced_object(cleaned)):
        if not candidate:
            continue
        try:
            obj = json.loads(candidate)
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            continue
    raise ParseError(f"no parsable JSON object in: {text[:200]!r}")
```

- [ ] **Step 4: テストが通ることを確認**

Run: `uv run pytest tests/runners/test_response_parser.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/agentic_fx/runners/response_parser.py tests/runners/test_response_parser.py
git commit -m "feat: JSON 修復 (think 除去・フェンス剥がし・バランス抽出)"
```

---

### Task 3: ツールレジストリ (tools/registry.py)

**Files:**
- Create: `src/agentic_fx/tools/__init__.py`, `src/agentic_fx/tools/registry.py`
- Test: `tests/tools/__init__.py`, `tests/tools/test_registry.py`

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True) ToolDef(name: str, description: str, parameters: dict, func: Callable[..., object])` — parameters は JSON Schema (`{"type": "object", "properties": {...}, "required": [...]}`)
  - `class ToolRegistry`:
    - `register(self, tool: ToolDef) -> None` (重複名は ValueError)
    - `register_all(self, tools: list[ToolDef]) -> None`
    - `names(self) -> list[str]`
    - `openai_tools(self, allowed: list[str]) -> list[dict]` — OpenAI function calling 形式 (`{"type": "function", "function": {name, description, parameters}}`)。allowed に無い名前は無視
    - `execute(self, name: str, arguments: dict, allowed: list[str]) -> str` — func を kwargs で呼び、結果を `json.dumps(ensure_ascii=False, default=str)`。**未登録・非許可・実行例外はエラー文字列を返す** (LLM に返して継続させる — loop を止めない)
    - `func(self, name: str) -> Callable` — pytest から素関数を直接叩く第 3 形態
  - (Phase 2 の MCP 変換はこのクラスにメソッド追加で対応する — 契約として注記)

- [ ] **Step 1: 失敗するテストを書く**

`tests/tools/test_registry.py`:

```python
import json

import pytest

from agentic_fx.tools.registry import ToolDef, ToolRegistry


def _echo_tool():
    return ToolDef(
        name="echo", description="echo back",
        parameters={"type": "object",
                    "properties": {"msg": {"type": "string"}},
                    "required": ["msg"]},
        func=lambda msg: {"echoed": msg})


def test_register_and_names():
    reg = ToolRegistry()
    reg.register(_echo_tool())
    assert reg.names() == ["echo"]
    with pytest.raises(ValueError):
        reg.register(_echo_tool())


def test_openai_tools_filters_allowed():
    reg = ToolRegistry()
    reg.register(_echo_tool())
    reg.register(ToolDef("other", "d", {"type": "object", "properties": {}},
                         lambda: 1))
    schemas = reg.openai_tools(["echo"])
    assert len(schemas) == 1
    assert schemas[0]["function"]["name"] == "echo"
    assert schemas[0]["type"] == "function"


def test_execute_returns_json():
    reg = ToolRegistry()
    reg.register(_echo_tool())
    out = reg.execute("echo", {"msg": "hi"}, allowed=["echo"])
    assert json.loads(out) == {"echoed": "hi"}


def test_execute_not_allowed_returns_error_string():
    reg = ToolRegistry()
    reg.register(_echo_tool())
    out = reg.execute("echo", {"msg": "hi"}, allowed=[])
    assert "error" in out and "not allowed" in out


def test_execute_exception_returns_error_string():
    reg = ToolRegistry()
    reg.register(ToolDef("boom", "d", {"type": "object", "properties": {}},
                         func=lambda: 1 / 0))
    out = reg.execute("boom", {}, allowed=["boom"])
    assert "error" in out


def test_raw_func_access():
    reg = ToolRegistry()
    reg.register(_echo_tool())
    assert reg.func("echo")(msg="direct") == {"echoed": "direct"}
```

- [ ] **Step 2: テストが失敗することを確認**

Run: `uv run pytest tests/tools/test_registry.py -v`
Expected: FAIL (ImportError)

- [ ] **Step 3: 実装**

`src/agentic_fx/tools/registry.py`:

```python
"""ツールレジストリ — 1 定義から OpenAI スキーマ / 素関数の 2 形態 (MCP は Phase 2 で追加)。"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Callable

_log = logging.getLogger("agentic_fx.tools")


@dataclass(frozen=True, slots=True)
class ToolDef:
    name: str
    description: str
    parameters: dict
    func: Callable[..., object]


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolDef] = {}

    def register(self, tool: ToolDef) -> None:
        if tool.name in self._tools:
            raise ValueError(f"duplicate tool: {tool.name}")
        self._tools[tool.name] = tool

    def register_all(self, tools: list[ToolDef]) -> None:
        for t in tools:
            self.register(t)

    def names(self) -> list[str]:
        return list(self._tools)

    def openai_tools(self, allowed: list[str]) -> list[dict]:
        return [{"type": "function",
                 "function": {"name": t.name, "description": t.description,
                              "parameters": t.parameters}}
                for n, t in self._tools.items() if n in allowed]

    def execute(self, name: str, arguments: dict, allowed: list[str]) -> str:
        if name not in allowed or name not in self._tools:
            return json.dumps({"error": f"tool {name!r} not allowed"})
        try:
            result = self._tools[name].func(**arguments)
            return json.dumps(result, ensure_ascii=False, default=str)
        except Exception as e:  # noqa: BLE001 — LLM に返して継続
            _log.warning("tool %s failed: %s", name, e)
            return json.dumps({"error": f"{type(e).__name__}: {e}"})

    def func(self, name: str) -> Callable[..., object]:
        return self._tools[name].func
```

- [ ] **Step 4: テストが通ることを確認**

Run: `uv run pytest tests/tools/test_registry.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/agentic_fx/tools tests/tools
git commit -m "feat: ツールレジストリ (OpenAI スキーマ変換・許可フィルタ・素関数アクセス)"
```

---

### Task 4: ツール実装 (market / news / account / reflection)

**Files:**
- Create: `src/agentic_fx/tools/market_tools.py`, `src/agentic_fx/tools/news_tools.py`, `src/agentic_fx/tools/account_tools.py`, `src/agentic_fx/tools/reflection_tools.py`
- Test: `tests/tools/test_tool_impls.py`

**Interfaces:**
- Produces (各 module に `build(...) -> list[ToolDef]` ファクトリ。依存はクロージャで注入 — LLM 層はレジストリ経由でしか触れない):
  - `market_tools.build(provider: PriceProvider, econ: EconCalendar) -> list[ToolDef]`:
    - `get_ohlcv(pair, timeframe)` — 直近 100 本を `[{ts, open, high, low, close}]`
    - `get_indicators(pair, timeframe)` — `compute_indicators` の dict + `mtf_4h` キー (timeframe が 1h のとき resample("4h") で追補)
    - `get_econ_calendar(days)` — `EconCalendar.upcoming(hours=days*24)`
  - `news_tools.build(rag: Rag) -> list[ToolDef]`: `search_news(query)` — 上位 5 件
  - `account_tools.build(conn, broker: PaperBroker) -> list[ToolDef]`:
    - `get_positions()` — open / pending_fill / protection_pending の orders を `[{order_id, pair, direction, status, quantity, entry, stop_loss, take_profit, horizon}]`
    - `get_account()` — `{balance, equity}`
  - `reflection_tools.build(conn, rag: Rag) -> list[ToolDef]`: `get_recent_reflections(n)` / `search_reflections(query)`
- **全ツール読み取り専用** (書き込み系依存を一切受け取らない)

- [ ] **Step 1: 失敗するテストを書く**

`tests/tools/test_tool_impls.py`:

```python
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

from agentic_fx.config import load_settings
from agentic_fx.core.contracts import Bar, FixedClock, OrderStatus
from agentic_fx.core.paper_broker import PaperBroker
from agentic_fx.store import orders, reflections
from agentic_fx.store.db import connect, init_db
from agentic_fx.tools import (
    account_tools, market_tools, news_tools, reflection_tools,
)
from agentic_fx.tools.registry import ToolRegistry

NOW = datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc)
SETTINGS = load_settings(
    Path(__file__).resolve().parents[2] / "config" / "settings.yaml.example")


def _bars(n=120):
    return [Bar("USDJPY", "1h", NOW - timedelta(hours=n - i),
                148.0, 148.2, 147.8, 148.1, 100) for i in range(n)]


def test_market_tools():
    provider = MagicMock()
    provider.get_bars.return_value = _bars()
    econ = MagicMock()
    econ.upcoming.return_value = [{"name": "CPI"}]
    reg = ToolRegistry()
    reg.register_all(market_tools.build(provider, econ))
    ohlcv = reg.func("get_ohlcv")(pair="USDJPY", timeframe="1h")
    assert len(ohlcv) == 100  # 直近 100 本に制限
    assert set(ohlcv[0]) == {"ts", "open", "high", "low", "close"}
    ind = reg.func("get_indicators")(pair="USDJPY", timeframe="1h")
    assert "rsi_14" in ind and "mtf_4h" in ind
    assert reg.func("get_econ_calendar")(days=1) == [{"name": "CPI"}]


def test_news_and_reflection_tools(tmp_path):
    conn = connect(tmp_path / "t.db")
    init_db(conn)
    rag = MagicMock()
    rag.search_news.return_value = [{"title": "t"}]
    rag.search_reflections.return_value = [{"content": "c"}]
    reg = ToolRegistry()
    reg.register_all(news_tools.build(rag))
    reg.register_all(reflection_tools.build(conn, rag))
    assert reg.func("search_news")(query="usd")[0]["title"] == "t"
    assert reg.func("search_reflections")(query="q")[0]["content"] == "c"
    oid = orders.insert(conn, pair="USDJPY", direction="long",
                        entry_type="market", horizon="day",
                        status=OrderStatus.CLOSED, now=NOW)
    reflections.save(conn, oid, "振り返り本文", NOW)
    recent = reg.func("get_recent_reflections")(n=5)
    assert recent[0]["content"] == "振り返り本文"


def test_account_tools(tmp_path):
    conn = connect(tmp_path / "t.db")
    init_db(conn)
    orders.insert(conn, pair="USDJPY", direction="long", entry_type="limit",
                  horizon="day", status=OrderStatus.PENDING_FILL, now=NOW,
                  quantity=0.1, requested_price=148.2, stop_loss=147.8,
                  take_profit=149.0)
    reg = ToolRegistry()
    reg.register_all(account_tools.build(
        conn, PaperBroker(conn, SETTINGS, FixedClock(NOW))))
    pos = reg.func("get_positions")()
    assert pos[0]["order_id"] > 0
    assert pos[0]["status"] == "pending_fill"
    acct = reg.func("get_account")()
    assert acct["balance"] == 1_000_000


def test_all_tools_have_schemas():
    provider, econ, rag = MagicMock(), MagicMock(), MagicMock()
    tools = market_tools.build(provider, econ) + news_tools.build(rag)
    for t in tools:
        assert t.parameters["type"] == "object"
        assert t.description
```

- [ ] **Step 2: テストが失敗することを確認**

Run: `uv run pytest tests/tools/test_tool_impls.py -v`
Expected: FAIL (ImportError)

- [ ] **Step 3: 実装**

`src/agentic_fx/tools/market_tools.py`:

```python
"""market 系ツール — datafeed の薄い読み取り専用ラッパー。"""
from __future__ import annotations

from agentic_fx.datafeed.econ_calendar import EconCalendar
from agentic_fx.datafeed.indicators import bars_to_df, compute_indicators, resample
from agentic_fx.datafeed.price_provider import PriceProvider
from agentic_fx.tools.registry import ToolDef

_PAIR_PARAM = {"pair": {"type": "string", "description": "e.g. USDJPY"},
               "timeframe": {"type": "string",
                             "enum": ["5m", "15m", "1h", "4h", "1d"]}}


def build(provider: PriceProvider, econ: EconCalendar) -> list[ToolDef]:
    def get_ohlcv(pair: str, timeframe: str) -> list[dict]:
        bars = provider.get_bars(pair, timeframe)[-100:]
        return [{"ts": b.ts.isoformat(), "open": b.open, "high": b.high,
                 "low": b.low, "close": b.close} for b in bars]

    def get_indicators(pair: str, timeframe: str) -> dict:
        df = bars_to_df(provider.get_bars(pair, timeframe))
        out = compute_indicators(df)
        if timeframe == "1h":
            out["mtf_4h"] = compute_indicators(resample(df, "4h"))
        return out

    def get_econ_calendar(days: int = 1) -> list[dict]:
        return econ.upcoming(hours=days * 24)

    return [
        ToolDef("get_ohlcv", "OHLCV 価格データ (直近 100 本)",
                {"type": "object", "properties": _PAIR_PARAM,
                 "required": ["pair", "timeframe"]}, get_ohlcv),
        ToolDef("get_indicators",
                "テクニカル指標 (SMA/EMA/RSI/ATR/MACD/BB、1h には mtf_4h 付き)",
                {"type": "object", "properties": _PAIR_PARAM,
                 "required": ["pair", "timeframe"]}, get_indicators),
        ToolDef("get_econ_calendar", "経済指標カレンダー (今後 N 日)",
                {"type": "object",
                 "properties": {"days": {"type": "integer", "minimum": 1,
                                         "maximum": 7}},
                 "required": []}, get_econ_calendar),
    ]
```

`src/agentic_fx/tools/news_tools.py`:

```python
from __future__ import annotations

from agentic_fx.store.rag import Rag
from agentic_fx.tools.registry import ToolDef


def build(rag: Rag) -> list[ToolDef]:
    def search_news(query: str) -> list[dict]:
        return rag.search_news(query, n=5)

    return [ToolDef("search_news", "収集済みニュースの意味検索 (上位 5 件)",
                    {"type": "object",
                     "properties": {"query": {"type": "string"}},
                     "required": ["query"]}, search_news)]
```

`src/agentic_fx/tools/account_tools.py`:

```python
from __future__ import annotations

import sqlite3

from agentic_fx.core.contracts import OrderStatus
from agentic_fx.core.paper_broker import PaperBroker
from agentic_fx.store import orders
from agentic_fx.tools.registry import ToolDef

_ACTIVE = (OrderStatus.OPEN, OrderStatus.PENDING_FILL,
           OrderStatus.PROTECTION_PENDING)


def build(conn: sqlite3.Connection, broker: PaperBroker) -> list[ToolDef]:
    def get_positions() -> list[dict]:
        return [{"order_id": r["id"], "pair": r["pair"],
                 "direction": r["direction"], "status": r["status"],
                 "quantity": r["quantity"],
                 "entry": r["avg_fill_price"] or r["requested_price"],
                 "stop_loss": r["stop_loss"], "take_profit": r["take_profit"],
                 "horizon": r["horizon"]}
                for r in orders.list_by_status(conn, *_ACTIVE)]

    def get_account() -> dict:
        balance, equity = broker.equity()
        return {"balance": balance, "equity": equity}

    return [
        ToolDef("get_positions", "現在ポジション・未約定指値 (order_id 付き)",
                {"type": "object", "properties": {}, "required": []},
                get_positions),
        ToolDef("get_account", "口座残高・エクイティ",
                {"type": "object", "properties": {}, "required": []},
                get_account),
    ]
```

`src/agentic_fx/tools/reflection_tools.py`:

```python
from __future__ import annotations

import sqlite3

from agentic_fx.store import reflections
from agentic_fx.store.rag import Rag
from agentic_fx.tools.registry import ToolDef


def build(conn: sqlite3.Connection, rag: Rag) -> list[ToolDef]:
    def get_recent_reflections(n: int = 5) -> list[dict]:
        return reflections.recent(conn, n)

    def search_reflections(query: str) -> list[dict]:
        return rag.search_reflections(query, n=5)

    return [
        ToolDef("get_recent_reflections", "直近のトレード振り返り",
                {"type": "object",
                 "properties": {"n": {"type": "integer", "minimum": 1,
                                      "maximum": 20}},
                 "required": []}, get_recent_reflections),
        ToolDef("search_reflections", "過去の振り返りの意味検索 (類似局面)",
                {"type": "object",
                 "properties": {"query": {"type": "string"}},
                 "required": ["query"]}, search_reflections),
    ]
```

- [ ] **Step 4: テストが通ることを確認**

Run: `uv run pytest tests/tools/test_tool_impls.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/agentic_fx/tools tests/tools/test_tool_impls.py
git commit -m "feat: Phase 1 ツール群 (market/news/account/reflection — 全て読み取り専用)"
```

---

### Task 5: LocalRunner ハッピーパス (runners/local_runner.py)

**Files:**
- Create: `src/agentic_fx/runners/local_runner.py`
- Test: `tests/runners/test_local_runner.py`

**Interfaces:**
- Produces: `class LocalRunner(AgentRunner)`:
  - `__init__(self, *, base_url: str, model: str, registry: ToolRegistry, transport: httpx.BaseTransport | None = None)` — transport はテスト注入用 (`httpx.MockTransport`)。実運用は None (通常の接続)
  - `run(self, mission: Mission) -> MissionResult`:
    1. `messages = [{"role": "user", "content": mission.prompt}]`
    2. ループ (最大 `mission.max_turns` ターン): `POST {base_url}/chat/completions` body = `{model, messages, tools: registry.openai_tools(mission.tools), tool_choice: "auto"}`。HTTP timeout は**残り時間** (`deadline - now`、最低 1 秒)
    3. 応答 message に `tool_calls` があれば各呼び出しを `registry.execute` して `{"role": "tool", "tool_call_id", "content"}` を追加し継続
    4. content のみなら `parse_json_output` → `jsonschema.validate(output, mission.output_schema)` → completed
    5. `ParseError` / `ValidationError` はエラー内容を user メッセージで返して再出力 (各上限 2 回、**リトライも max_turns に算入**)。超過で failed
    6. 全体 deadline 超過 → timeout / ターン上限 → max_turns。httpx のタイムアウト例外も timeout
    7. transcript は全 messages (assistant 応答・tool 結果含む)

- [ ] **Step 1: 失敗するテストを書く**

`tests/runners/test_local_runner.py`:

```python
import json

import httpx

from agentic_fx.runners.base import Mission
from agentic_fx.runners.local_runner import LocalRunner
from agentic_fx.tools.registry import ToolDef, ToolRegistry

SCHEMA = {"type": "object", "properties": {"action": {"type": "string"}},
          "required": ["action"]}


def _registry():
    reg = ToolRegistry()
    reg.register(ToolDef("get_price", "price",
                         {"type": "object", "properties": {}},
                         lambda: {"price": 148.5}))
    return reg


def _mission(**over):
    d = dict(prompt="判断せよ", tools=["get_price"], output_schema=SCHEMA,
             max_turns=6, timeout_sec=30)
    d.update(over)
    return Mission(**d)


def _resp(message):
    return httpx.Response(200, json={"choices": [{"message": message}]})


def _runner(script):
    """script: list of response messages returned in order."""
    calls = {"i": 0, "bodies": []}

    def handler(request):
        calls["bodies"].append(json.loads(request.content))
        msg = script[min(calls["i"], len(script) - 1)]
        calls["i"] += 1
        return _resp(msg)

    runner = LocalRunner(base_url="http://test/v1", model="qwen",
                         registry=_registry(),
                         transport=httpx.MockTransport(handler))
    return runner, calls


def test_direct_final_answer():
    runner, calls = _runner([{"role": "assistant",
                              "content": '{"action": "hold"}'}])
    r = runner.run(_mission())
    assert r.status == "completed"
    assert r.output == {"action": "hold"}
    assert calls["bodies"][0]["model"] == "qwen"
    assert calls["bodies"][0]["tools"][0]["function"]["name"] == "get_price"


def test_tool_call_loop():
    tool_call_msg = {"role": "assistant", "content": None, "tool_calls": [
        {"id": "c1", "type": "function",
         "function": {"name": "get_price", "arguments": "{}"}}]}
    final = {"role": "assistant", "content": '{"action": "open"}'}
    runner, calls = _runner([tool_call_msg, final])
    r = runner.run(_mission())
    assert r.status == "completed"
    # 2 回目のリクエストに tool 結果が渡っていること
    msgs = calls["bodies"][1]["messages"]
    assert msgs[-1]["role"] == "tool"
    assert json.loads(msgs[-1]["content"]) == {"price": 148.5}
    # transcript に全メッセージが残る
    assert any(m.get("role") == "tool" for m in r.transcript)


def test_think_tag_repaired():
    runner, _ = _runner([{"role": "assistant",
                          "content": '<think>hmm</think>{"action": "hold"}'}])
    assert runner.run(_mission()).status == "completed"
```

- [ ] **Step 2: テストが失敗することを確認**

Run: `uv run pytest tests/runners/test_local_runner.py -v`
Expected: FAIL (ImportError)

- [ ] **Step 3: 実装**

`src/agentic_fx/runners/local_runner.py`:

```python
"""LocalRunner — llama-swap OpenAI 互換 API への自前 tool-calling loop (設計書 §4)。"""
from __future__ import annotations

import json
import logging
import time

import httpx
import jsonschema

from agentic_fx.runners.base import AgentRunner, Mission, MissionResult
from agentic_fx.runners.response_parser import ParseError, parse_json_output
from agentic_fx.tools.registry import ToolRegistry

_log = logging.getLogger("agentic_fx.local_runner")
_MAX_REPAIR_RETRIES = 2


class LocalRunner(AgentRunner):
    def __init__(self, *, base_url: str, model: str, registry: ToolRegistry,
                 transport: httpx.BaseTransport | None = None) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._registry = registry
        self._client = httpx.Client(transport=transport)

    def run(self, mission: Mission) -> MissionResult:
        deadline = time.monotonic() + mission.timeout_sec
        messages: list[dict] = [{"role": "user", "content": mission.prompt}]
        tools = self._registry.openai_tools(mission.tools)
        parse_retries = schema_retries = 0

        for _turn in range(mission.max_turns):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return MissionResult("timeout", None, messages)
            try:
                resp = self._client.post(
                    f"{self._base_url}/chat/completions",
                    json={"model": self._model, "messages": messages,
                          "tools": tools, "tool_choice": "auto"},
                    timeout=max(remaining, 1.0))
                resp.raise_for_status()
            except httpx.TimeoutException:
                return MissionResult("timeout", None, messages)
            except httpx.HTTPError as e:
                _log.warning("llama-swap request failed: %s", e)
                return MissionResult("failed", None, messages)

            msg = resp.json()["choices"][0]["message"]
            messages.append(msg)

            if msg.get("tool_calls"):
                for tc in msg["tool_calls"]:
                    try:
                        args = json.loads(tc["function"]["arguments"] or "{}")
                    except json.JSONDecodeError:
                        args = {}
                    result = self._registry.execute(
                        tc["function"]["name"], args, mission.tools)
                    messages.append({"role": "tool",
                                     "tool_call_id": tc["id"],
                                     "content": result})
                continue

            content = msg.get("content") or ""
            try:
                output = parse_json_output(content)
            except ParseError as e:
                parse_retries += 1
                if parse_retries > _MAX_REPAIR_RETRIES:
                    return MissionResult("failed", None, messages)
                messages.append({"role": "user",
                                 "content": f"出力を JSON として解釈できません "
                                            f"({e})。JSON オブジェクトのみを"
                                            f"出力してください。"})
                continue
            try:
                jsonschema.validate(output, mission.output_schema)
            except jsonschema.ValidationError as e:
                schema_retries += 1
                if schema_retries > _MAX_REPAIR_RETRIES:
                    return MissionResult("failed", None, messages)
                messages.append({"role": "user",
                                 "content": f"出力がスキーマに合いません: "
                                            f"{e.message}。修正して JSON のみ"
                                            f"再出力してください。"})
                continue
            return MissionResult("completed", output, messages)

        return MissionResult("max_turns", None, messages)
```

- [ ] **Step 4: テストが通ることを確認**

Run: `uv run pytest tests/runners/test_local_runner.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/agentic_fx/runners/local_runner.py tests/runners/test_local_runner.py
git commit -m "feat: LocalRunner tool-calling loop (MockTransport テスト)"
```

---

### Task 6: LocalRunner 異常系

**Files:**
- Test: `tests/runners/test_local_runner_errors.py`

- [ ] **Step 1: 異常系テストを書く**

`tests/runners/test_local_runner_errors.py`:

```python
import httpx

from agentic_fx.runners.base import Mission
from agentic_fx.runners.local_runner import LocalRunner
from agentic_fx.tools.registry import ToolRegistry

SCHEMA = {"type": "object", "properties": {"action": {"type": "string"}},
          "required": ["action"]}


def _mission(**over):
    d = dict(prompt="p", tools=[], output_schema=SCHEMA, max_turns=8,
             timeout_sec=30)
    d.update(over)
    return Mission(**d)


def _runner(script_or_handler):
    if callable(script_or_handler):
        handler = script_or_handler
    else:
        state = {"i": 0}

        def handler(request):  # noqa: ANN001
            msg = script_or_handler[
                min(state["i"], len(script_or_handler) - 1)]
            state["i"] += 1
            return httpx.Response(200,
                                  json={"choices": [{"message": msg}]})
    return LocalRunner(base_url="http://test/v1", model="m",
                       registry=ToolRegistry(),
                       transport=httpx.MockTransport(handler))


def test_unparsable_output_retried_then_failed():
    runner = _runner([{"role": "assistant", "content": "not json at all"}])
    r = runner.run(_mission())
    assert r.status == "failed"
    # 初回 + リトライ 2 回 = user 修復要求 2 件が transcript に残る
    repair_msgs = [m for m in r.transcript
                   if m.get("role") == "user" and "JSON" in m.get("content", "")]
    assert len(repair_msgs) == 2


def test_schema_violation_retried_then_success():
    bad = {"role": "assistant", "content": '{"wrong": 1}'}
    good = {"role": "assistant", "content": '{"action": "hold"}'}
    runner = _runner([bad, good])
    r = runner.run(_mission())
    assert r.status == "completed"


def test_max_turns():
    tool_call = {"role": "assistant", "content": None, "tool_calls": [
        {"id": "c", "type": "function",
         "function": {"name": "nope", "arguments": "{}"}}]}
    runner = _runner([tool_call])  # 永遠にツールを呼び続ける
    r = runner.run(_mission(max_turns=3))
    assert r.status == "max_turns"


def test_timeout_before_any_call():
    runner = _runner([{"role": "assistant", "content": '{"action": "hold"}'}])
    r = runner.run(_mission(timeout_sec=0.0))
    assert r.status == "timeout"


def test_http_error_fails():
    def handler(request):  # noqa: ANN001
        return httpx.Response(500, text="boom")
    r = _runner(handler).run(_mission())
    assert r.status == "failed"


def test_network_timeout_is_timeout():
    def handler(request):  # noqa: ANN001
        raise httpx.ConnectTimeout("slow")
    r = _runner(handler).run(_mission())
    assert r.status == "timeout"
```

- [ ] **Step 2: テストを実行**

Run: `uv run pytest tests/runners/test_local_runner_errors.py -v`
Expected: PASS (Task 5 の実装で満たされるはず。落ちるものがあれば local_runner.py を修正して green にする)

- [ ] **Step 3: Commit**

```bash
git add tests/runners/test_local_runner_errors.py src/agentic_fx/runners/local_runner.py
git commit -m "test: LocalRunner 異常系 (修復リトライ上限・max_turns・timeout 優先)"
```

---

### Task 7: 仕上げ

- [ ] **Step 1: 全テスト実行**

Run: `uv run pytest -v`
Expected: 全件 PASS

- [ ] **Step 2: セルフレビュー**

- Mission/MissionResult が設計書 §4 のフィールドと一致するか
- LocalRunner のリトライが「上限 2 回・max_turns 算入・timeout 優先」(設計書 §4 + プラン 1 の引き継ぎ) を満たすか
- ツールに書き込み系依存が渡っていないか (`grep -n "state_store\|update\|insert\|transition" src/agentic_fx/tools/*.py` — registry.py 以外でヒットしないこと)
- `runners/` から `core/` への import が contracts のみか (LLM 層はコアの執行系を直接触らない — executor 連携はプラン 5 の trade_loop 経由)

- [ ] **Step 3: Commit (残差分があれば)**

```bash
git add -A ':!data' ':!logs' ':!config/settings.yaml'
git commit -m "chore: プラン 4 完了 — Runner + tools"
```

---

## プラン 5 への引き継ぎ事項

- trade_loop は `LocalRunner(base_url=settings.llama_swap.base_url, model=settings.runner.trade.model, registry=...)` を組み立てる。`Mission.max_turns = settings.llama_swap.max_turns`、`timeout_sec = settings.llama_swap.timeout_sec`
- **ask Mission の output_schema は回答専用** (`{"type": "object", "properties": {"answer": {"type": "string"}}, "required": ["answer"]}`) — TradeIntent スキーマを渡さない (設計書 §5 の発注経路遮断)
- trade Mission の output_schema は TradeIntent v2 の JSON Schema (プラン 5 で定義、`TradeIntent.from_llm_dict` が最終防衛)
- registry の組み立て (どのツールをどの loop に許可するか) はプラン 5 の service 配線で行う
- Phase 2: ClaudeRunner は `AgentRunner` を実装し、`ToolRegistry` に MCP 変換メソッドを追加する (このクラスの既存メソッドは変更しない)
