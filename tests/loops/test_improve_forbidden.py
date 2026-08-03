"""④改善ループ非露出の回帰ピン (プラン 7 Task 9)。

改善ループ (プラン 9) の allowed tools リストはまだ実装されていない
(loops/ に improve loop 無し)。現時点で改善ループの Mission 構築点は
``loops/reflection_cycle.py`` の ``tools=[]`` のみ。ここでは
``signal_tools.IMPROVE_FORBIDDEN`` (= {"get_signals", "bless"}) が
その唯一の構築点から実際に組み立てられる Mission.tools に現れないことを
実行時に検証する。プラン 9 で改善ループの実 allowed リストが実装された
ら、その組み立て箇所でも本集合との非交差を assert すること (引き継ぎ)。
"""
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

from agentic_fx.activity import ActivityLog
from agentic_fx.config import load_settings
from agentic_fx.core.contracts import FixedClock, OrderStatus
from agentic_fx.loops.reflection_cycle import ReflectionCycle
from agentic_fx.runners.base import MissionResult
from agentic_fx.runners.fake_runner import FakeRunner
from agentic_fx.store import orders
from agentic_fx.store.db import connect, init_db
from agentic_fx.tools import signal_tools

NOW = datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc)
SETTINGS = load_settings(
    Path(__file__).resolve().parents[2] / "config" / "settings.yaml.example")


def test_improve_forbidden_contains_get_signals_and_bless():
    assert signal_tools.IMPROVE_FORBIDDEN == frozenset({"get_signals", "bless"})


def test_reflection_cycle_mission_tools_disjoint_from_improve_forbidden(tmp_path):
    """現存する唯一の改善系 Mission 構築点 (ReflectionCycle) が組み立てる
    Mission.tools を実際に実行させ、IMPROVE_FORBIDDEN と非交差であること
    を検証する (ハードコードした tools=[] の目視確認ではなく、実際に
    runner へ渡された Mission を捕捉する)。"""
    conn = connect(tmp_path / "t.db")
    init_db(conn)
    runner = FakeRunner([MissionResult("completed", {"content": "x"}, [])])
    cyc = ReflectionCycle(
        conn=conn, runner=runner, rag=MagicMock(), settings=SETTINGS,
        activity=ActivityLog(tmp_path / "a.log"), clock=FixedClock(NOW))
    orders.insert(
        conn, pair="USDJPY", direction="long", entry_type="market",
        horizon="day", status=OrderStatus.CLOSED, now=NOW, quantity=0.1,
        realized_pnl=-100.0, close_reason="sl",
        avg_fill_price=148.5, close_price=148.0)
    cyc.run_pending()
    assert len(runner.missions) == 1
    assert not (set(runner.missions[0].tools) & signal_tools.IMPROVE_FORBIDDEN)
