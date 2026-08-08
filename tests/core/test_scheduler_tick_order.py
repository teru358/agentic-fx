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
    確認。account 判定ロジック自体は Task 12 では変更していない。
    """
    # account を意図的に欠損させる既存 fixture の使い方に合わせた設定。
    # 別ペア (EURUSD) の open ポジションを直接挿入。このペアのバーは一度も
    # 与えないため、mark_to_market は毎 tick 陳腐化スキップし続け、
    # snapshot (Env.__init__ 時点の WED) が更新されないまま 10 分の陳腐化
    # チェックを超過する。
    env = Env(tmp_path)
    orders_store.insert(env.conn, pair="EURUSD", direction="long",
                       entry_type="market", horizon="swing", status="open",
                       now=WED, quantity=0.1, avg_fill_price=1.1000,
                       stop_loss=1.0960, take_profit=1.1120)
    oid = env.place_limit()  # USDJPY entry=148.20
    env.bars["USDJPY"] = Bar("USDJPY", "1m", WED + timedelta(minutes=11),
                             148.30, 148.35, 148.15, 148.25, 100)
    env.sched.tick(WED + timedelta(minutes=11))
    # 既存 test_scheduler.py の account_unknown 系アサーションと同じ内容。
    # 目的は「hooks 再編後も同じ結論になる」ことの確認。
    row = orders_store.get(env.conn, oid)
    assert row["status"] == "cancelled"
    assert row["close_reason"] == "account_unknown"


def test_hooks_run_even_when_process_exits_raises(tmp_path):
    """プラン 8 レビュー修正 F1: _process_exits が未捕捉例外を投げても
    hooks は走る (try/finally で構造的に保証される)。また例外は
    呼び出し元に伝播する (握り潰されない)。
    """
    env = Env(tmp_path)
    hooks_called = []
    env.sched.on_news_cycle = lambda: hooks_called.append("news")
    env.sched.on_signal_maintenance = lambda now: hooks_called.append("signal")

    # _process_exits を例外で置き換え
    def raise_in_exits(now, filled_ids):
        raise RuntimeError("simulated _process_exits failure")

    env.sched._process_exits = raise_in_exits

    # 例外は伝播するはず
    with pytest.raises(RuntimeError, match="simulated _process_exits failure"):
        env.sched.tick(WED)

    # しかし hooks は実行される
    assert "news" in hooks_called
    assert "signal" in hooks_called


def test_hooks_run_even_when_early_code_raises(tmp_path):
    """プラン 8 レビュー修正 F1: mark-to-market より前の処理
    (_on_market_close 等) で例外が発生しても hooks は走る。
    """
    env = Env(tmp_path)
    hooks_called = []
    env.sched.on_news_cycle = lambda: hooks_called.append("news")

    # market_hours.is_market_open を例外で置き換え
    from unittest.mock import patch
    with patch("agentic_fx.core.scheduler.market_hours.is_market_open") as mock_open:
        mock_open.side_effect = RuntimeError("simulated early failure")

        # 例外は伝播するはず
        with pytest.raises(RuntimeError, match="simulated early failure"):
            env.sched.tick(WED)

        # しかし hooks は実行される
        assert "news" in hooks_called


def test_hooks_run_exactly_once_per_tick(tmp_path):
    """プラン 8 レビュー修正 F1: 通常経路では hooks が厳密に 1 回だけ
    走ること。複数経路から重複呼び出しされないことの確認。
    """
    env = Env(tmp_path)
    signal_calls = []
    env.sched.on_signal_maintenance = lambda now: signal_calls.append(now)

    # 複数 tick を実行
    env.sched.tick(WED)
    env.sched.tick(WED + timedelta(minutes=1))

    # それぞれ厳密に 1 回ずつ呼ばれたはず
    assert len(signal_calls) == 2
    assert signal_calls[0] == WED
    assert signal_calls[1] == WED + timedelta(minutes=1)
