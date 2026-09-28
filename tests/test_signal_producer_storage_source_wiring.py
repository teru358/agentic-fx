from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

from agentic_fx.config import load_settings
from agentic_fx.core.contracts import Bar
from agentic_fx.plugin.loader import PluginMeta
from agentic_fx.plugin.resolve import ResolvedIndicatorSet
from agentic_fx.plugin.signal_producer import SignalProducer
from agentic_fx.service import _run_signal_maintenance
from agentic_fx.store import ohlcv
from agentic_fx.store.db import connect, init_db


UTC = timezone.utc
EXAMPLE = Path(__file__).resolve().parents[1] / "config" / "settings.yaml.example"


def test_signal_maintenance_reads_mt5_live_cache_for_mt5_primary(tmp_path, caplog,
                                                                   monkeypatch):
    conn = connect(tmp_path / "agentic.db")
    init_db(conn)
    base_settings = load_settings(EXAMPLE)
    settings = base_settings.model_copy(update={
        "datafeed": base_settings.datafeed.model_copy(update={
            "primary": "mt5",
            "mt5": base_settings.datafeed.mt5.model_copy(
                update={"enabled": True}),
            "decision_timeframes": ["15m"],
        }),
    })
    now = datetime(2026, 9, 28, 11, 5, tzinfo=UTC)
    bar = Bar("USDJPY", "1h", datetime(2026, 9, 28, 10, 0, tzinfo=UTC),
              156.70, 156.90, 156.60, 156.80, 10.0)
    ohlcv.upsert_cache_bars(conn, [bar], source="mt5-live")
    meta = PluginMeta("mt5_strategy", "strategy", tmp_path, {}, "1h",
                      ("USDJPY",), 1, "mt5-strategy-hash")
    calls: list[dict] = []

    def record(_meta, payload, *, settings):
        calls.append(payload)
        return {"action": "hold", "rationale": "no-op", "direction": None,
                "entry_type": None, "limit_price": None, "stop_loss": None,
                "take_profit": None}

    producer = SignalProducer()
    evaluate_due_plugins = producer.evaluate_due_plugins

    def evaluate_with_record(*args, **kwargs):
        return evaluate_due_plugins(*args, sandbox_run=record, **kwargs)

    monkeypatch.setattr(producer, "evaluate_due_plugins", evaluate_with_record)
    caplog.set_level(logging.WARNING, logger="agentic_fx.plugin.signal_producer")
    _run_signal_maintenance(
        conn=conn, signal_producer=producer, approved=[meta],
        settings=settings, now=now,
        resolved_by_identity={(meta.name, meta.content_hash):
                              ResolvedIndicatorSet.empty(tmp_path)},
    )

    assert len(calls) == 1
    frame = calls[0]["df"]
    assert frame.index[-1].to_pydatetime() == bar.ts
    assert frame.iloc[-1]["close"] == bar.close
    assert not any("target bucket not yet present" in record.getMessage()
                   for record in caplog.records)
    conn.close()
