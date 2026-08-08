"""tick 順序契約の回帰ピン (プラン8, 設計書 §3.2 codex C2-1/C3-1)。

決定論ブロック (mark-to-market → account/予約再検証 → fills_allowed →
fills → exits) の内部順序・processed-bar マーキング位置・fills_allowed
ゲートを固定する。hooks (news/econ/signal_maintenance) は市場開閉に
関わらず毎 tick 走ることも固定する。
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from agentic_fx.core.contracts import Bar
from agentic_fx.store import orders as orders_store
from tests.core.test_scheduler import Env, WED, SAT  # 既存 fixture を再利用


def test_hooks_run_even_when_market_closed(tmp_path):
    """cross-plan 修正① の再発防止: 市場閉鎖中も news/econ/signal
    maintenance は毎 tick 走る。"""
    env = Env(tmp_path)
    news_calls = []
    econ_calls = []
    maint_calls = []
    env.sched.on_news_cycle = lambda: news_calls.append(1)
    env.sched.on_econ_cycle = lambda: econ_calls.append(1)
    env.sched.on_signal_maintenance = lambda now: maint_calls.append(now)

    env.sched.tick(SAT)  # 市場閉鎖 (土曜)

    assert news_calls == [1]
    assert econ_calls == [1]
    assert maint_calls == [SAT]


def test_hooks_run_after_deterministic_block_when_market_open(tmp_path):
    """通常経路: hooks は fills_allowed ゲート・fills・exits の**後**に
    走る (fills_allowed の判定材料を hooks が汚染しないことの構造確認 —
    news/econ/signal_maintenance の呼び出し順を記録し、
    _process_limit_fills/_process_exits より後であることを確認する)。
    """
    env = Env(tmp_path)
    order: list[str] = []
    env.sched.on_news_cycle = lambda: order.append("news")
    orig_fills = env.sched._process_limit_fills
    env.sched._process_limit_fills = lambda now: (
        order.append("fills") or orig_fills(now))
    orig_exits = env.sched._process_exits
    env.sched._process_exits = lambda now, filled_ids: (
        order.append("exits") or orig_exits(now, filled_ids))

    env.sched.tick(WED)

    assert order.index("fills") < order.index("exits") < order.index("news")


def test_hooks_run_even_when_mark_to_market_regresses(tmp_path):
    """_mark_to_market が時系列逆行で False を返して tick が早期 return
    しても hooks は走る。"""
    env = Env(tmp_path)
    news_calls = []
    env.sched.on_news_cycle = lambda: news_calls.append(1)
    env.sched._mark_to_market = lambda now: False

    env.sched.tick(WED)

    assert news_calls == [1]


def test_account_unknown_still_cancels_pending_before_hooks_run(tmp_path):
    """account 不明時の全 pending 取消 (fail closed) は hooks より先に
    確定していること — 決定論ブロックの内部順序が保存されている回帰
    確認 (既存 test_scheduler.py の account 系テストと同型だが、hooks
    再編後も同じ結論になることをこのファイルでも固定する)。
    """
    env = Env(tmp_path)  # account を意図的に欠損させる既存 fixture の使い方に
                          # 合わせて実装すること (既存 test_scheduler.py の該当
                          # テストの account snapshot 未記録パターンを踏襲)
    # 別ペア (EURUSD) の open ポジションを直接挿入。このペアのバーは一度も
    # 与えないため、mark_to_market は毎 tick 陳腐化スキップし続け、
    # snapshot (Env.__init__ 時点の WED) が更新されないまま 10 分の陳腐化
    # チェックを超過する
    orders_store.insert(env.conn, pair="EURUSD", direction="long",
                       entry_type="market", horizon="swing", status="open",
                       now=WED, quantity=0.1, avg_fill_price=1.1000,
                       stop_loss=1.0960, take_profit=1.1120)
    oid = env.place_limit()  # USDJPY entry=148.20
    env.bars["USDJPY"] = Bar("USDJPY", "1m", WED + timedelta(minutes=11),
                             148.30, 148.35, 148.15, 148.25, 100)
    env.sched.tick(WED + timedelta(minutes=11))
    # 既存 test_scheduler.py の account_unknown 系アサーションと同じ
    # 内容を実装者が転記する (このファイルの目的は「hooks 再編後も同じ
    # 結論になる」ことの確認であり、account 判定ロジック自体は Task 12
    # で変更しない)。
    row = orders_store.get(env.conn, oid)
    assert row["status"] == "cancelled"
    assert row["close_reason"] == "account_unknown"
