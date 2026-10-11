from __future__ import annotations

from afx_ops.web.routes import ROUTE_TABLE, resolve_route


def test_route_table_is_exactly_the_read_only_surface():
    assert [(route.path, route.client_method) for route in ROUTE_TABLE] == [
        ("/", "status"), ("/approvals", "approvals"), ("/approvals/{id}", "approval"),
        ("/logs", "log"), ("/activity", "activity"),
    ]
    assert resolve_route("/approvals/42").values == {"id": 42}
    assert resolve_route("/approvals/0") is None
