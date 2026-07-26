from datetime import datetime, timedelta, timezone

import pytest

from agentic_fx.store import approvals
from agentic_fx.store.approvals import AlreadyDecidedError
from agentic_fx.store.db import connect, init_db

NOW = datetime(2026, 7, 26, 12, 0, tzinfo=timezone.utc)


def _conn(tmp_path):
    c = connect(tmp_path / "t.db")
    init_db(c)
    return c


def test_create_and_decide(tmp_path):
    c = _conn(tmp_path)
    aid = approvals.create(c, "tech_plugin", {"path": "plugins/tech/rsi"}, NOW)
    assert len(approvals.pending(c)) == 1
    approvals.decide(c, aid, status="approved", decided_by="shell", now=NOW)
    assert approvals.pending(c) == []


def test_double_decide_rejected(tmp_path):
    c = _conn(tmp_path)
    aid = approvals.create(c, "news_source", {"url": "https://x"}, NOW)
    approvals.decide(c, aid, status="rejected", decided_by="shell", now=NOW,
                     reason="低品質")
    with pytest.raises(AlreadyDecidedError):
        approvals.decide(c, aid, status="approved", decided_by="shell", now=NOW)


def test_expire_due(tmp_path):
    c = _conn(tmp_path)
    approvals.create(c, "live_trade", {"pair": "USDJPY"}, NOW,
                     expires_at=NOW + timedelta(minutes=15))
    n = approvals.expire_due(c, NOW + timedelta(minutes=16))
    assert n == 1
    assert approvals.pending(c) == []


def test_pending_filter_by_kind(tmp_path):
    c = _conn(tmp_path)
    approvals.create(c, "tech_plugin", {}, NOW)
    approvals.create(c, "news_source", {}, NOW)
    assert len(approvals.pending(c, kind="tech_plugin")) == 1
