"""改善 agent への入力に実ホスト名・秘密が含まれないこと。

承認の diff の伏せ字は字面に現れる値だけを保証する (spec §7.1)。その受容は
「改善 agent の入力に実ホスト名・秘密が無い」ことが前提なので、この前提を
プロンプトと tool 応答の両面で pin する。意図して渡している staging の絶対
パスは対象外 (canary は staging の外に置く)。
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

from agentic_fx.activity import ActivityLog
from agentic_fx.config import load_settings
from agentic_fx.core.contracts import FixedClock
from agentic_fx.loops.improve_context import build_improve_context
from agentic_fx.loops.improve_loop import ImproveLoop
from agentic_fx.loops.improve_rpc_ledger import ImproveRpcLedger
from agentic_fx.store.db import connect, init_db
from agentic_fx.store.rag import Rag
from agentic_fx.tools import research_tools
from agentic_fx.tools.mission_registry import build_mission_registry

NOW = datetime(2026, 10, 19, 12, 0)
BASE = load_settings(
    Path(__file__).resolve().parents[2] / "config" / "settings.yaml.example")

CANARY_BRIDGE_HOST = "canary-mt5-host-7f3a91.invalid"
CANARY_BRIDGE_PORT = "48812"
CANARY_WEBHOOK_HOST = "canary-discord-host-c52e07.invalid"
CANARY_WEBHOOK_TOKEN = "canarywebhooktoken0c4b8d"
CANARY_API_KEY = "canary-mt5-api-key-91d3f6"
CANARY_HOME_DIR = "canaryhome-5be2a8"

CANARIES = (
    CANARY_BRIDGE_HOST, CANARY_BRIDGE_PORT, CANARY_WEBHOOK_HOST,
    CANARY_WEBHOOK_TOKEN, CANARY_API_KEY, CANARY_HOME_DIR,
    "agentic-fx/api",
)


class _FakeEmbedding:
    def __call__(self, input):  # noqa: A002
        return [[0.0] * 16 for _ in input]

    def embed_query(self, input):  # noqa: A002
        return self(input)

    def name(self):
        return "fake"


class _FakeSearch:
    def text(self, query, *, max_results):
        return [{"title": "t", "href": "https://news.example/a", "body": "b"}]


class _FakeFetch:
    def fetch(self, url, *, user_agent=None):
        return 200, "本文"


def _canary_settings():
    mt5 = BASE.datafeed.mt5.model_copy(update={
        "enabled": True,
        "bridge_url": f"http://{CANARY_BRIDGE_HOST}:{CANARY_BRIDGE_PORT}"})
    datafeed = BASE.datafeed.model_copy(update={"mt5": mt5})
    return BASE.model_copy(update={"datafeed": datafeed})


@pytest.fixture
def env(tmp_path, monkeypatch):
    home = tmp_path / CANARY_HOME_DIR
    keydir = home / ".config" / "agentic-fx" / "api" / "inst"
    keydir.mkdir(parents=True)
    (keydir / "key").write_text(CANARY_API_KEY, encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv(
        "DISCORD_WEBHOOK_URL",
        f"https://{CANARY_WEBHOOK_HOST}/api/webhooks/1/{CANARY_WEBHOOK_TOKEN}")
    monkeypatch.setenv("MT5_BRIDGE_API_KEY", CANARY_API_KEY)
    settings = _canary_settings()
    # 入力側に canary が存在すること
    assert CANARY_BRIDGE_HOST in (settings.datafeed.mt5.bridge_url or "")
    assert CANARY_WEBHOOK_HOST in os.environ["DISCORD_WEBHOOK_URL"]
    assert os.environ["MT5_BRIDGE_API_KEY"] == CANARY_API_KEY
    assert CANARY_HOME_DIR in str(Path.home())
    assert (Path.home() / ".config" / "agentic-fx" / "api" / "inst" / "key"
            ).read_text(encoding="utf-8") == CANARY_API_KEY
    return settings


def _assert_clean(label: str, text: str) -> None:
    assert text, label
    for canary in CANARIES:
        assert canary not in text, f"{label} に {canary!r} が現れた"


def test_improve_prompt_has_no_host_or_secret_canary(tmp_path, env):
    conn = connect(tmp_path / "t.db")
    init_db(conn)
    root = tmp_path / "root"
    root.mkdir()
    ctx_data = build_improve_context(
        conn, settings=env, now=NOW, root=root, allowed_backlog_ids=None,
        inventory_view={"plugins": [], "pin_broken_strategies": []})

    class _Ctx:
        staging_dir = tmp_path / "staging"
        source_snapshot_dir = tmp_path / "source"
        inventory_view = {"plugins": [], "pin_broken_strategies": []}

    loop = ImproveLoop.__new__(ImproveLoop)
    loop._settings = env
    rendered = loop._render_improve_mission_prompt(ctx_data, ctx=_Ctx())

    _assert_clean("prompt", rendered)
    _assert_clean("context", json.dumps(ctx_data, ensure_ascii=False,
                                        default=str))


def test_improve_tool_outputs_have_no_host_or_secret_canary(
        tmp_path, env, monkeypatch):
    """research と staging の代表ツールの応答。staging の絶対パス自体は意図して
    渡すので、canary は staging の外に置いてある。"""
    monkeypatch.setattr(research_tools, "_default_search_backend",
                        lambda: _FakeSearch())
    monkeypatch.setattr(research_tools, "_default_fetch_backend",
                        lambda: _FakeFetch())
    conn = connect(tmp_path / "x.db")
    init_db(conn)
    rag = Rag(tmp_path / "rag", embedding_function=_FakeEmbedding())
    staging = tmp_path / "staging"
    source = tmp_path / "source"
    (staging / "cand").mkdir(parents=True)
    (staging / "cand" / "plugin.py").write_text("X = 1\n", encoding="utf-8")
    (source / "dep").mkdir(parents=True)
    (source / "dep" / "plugin.py").write_text("Y = 2\n", encoding="utf-8")
    (source / "_examples" / "ex").mkdir(parents=True)
    (source / "_examples" / "ex" / "plugin.py").write_text(
        "Z = 3\n", encoding="utf-8")
    registry = build_mission_registry(
        "improve", conn, env, FixedClock(datetime(
            2026, 10, 19, 12, 0, tzinfo=timezone.utc)), rag,
        activity=ActivityLog(tmp_path / "logs" / "activity.log"),
        staging_dir=staging, source_snapshot_dir=source,
        ledger=ImproveRpcLedger(rpc_timeout_sec_by_kind={}),
        rpc_handlers={"run_backtest": lambda a: {},
                      "analyze_corr": lambda a: {}})
    allowed = registry.names()
    calls = [
        ("web_search", {"query": "fx", "max_results": 3}),
        ("fetch_article", {"url": "https://news.example/a"}),
        ("list_staging", {}),
        ("read_staging_file", {"name": "cand", "rel": "plugin.py"}),
        ("read_plugin_source", {"name": "dep"}),
        ("list_examples", {}),
        ("read_example_plugin", {"name": "ex"}),
        ("list_deployed_plugins", {}),
    ]
    for name, args in calls:
        out = registry.execute(name, args, allowed)
        assert "error" not in json.loads(out), (name, out)
        _assert_clean(f"tool {name}", out)
    _assert_clean("tool schema", json.dumps(registry.openai_tools(allowed),
                                            ensure_ascii=False))
