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
