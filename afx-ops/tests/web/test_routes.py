from __future__ import annotations

from afx_ops.web.routes import ROUTE_TABLE, resolve_route


def test_route_table_is_exactly_the_read_only_surface():
    assert [(route.path, route.client_method) for route in ROUTE_TABLE] == [
        ("/", "status"), ("/approvals", "approvals"), ("/approvals/{id}", "approval"),
        ("/logs", "log"), ("/activity", "activity"),
        ("/plugins", "plugins"), ("/plugins/{name}", "plugin"),
    ]
    assert resolve_route("/approvals/42").values == {"id": 42}
    assert resolve_route("/approvals/0") is None


def test_write_route_table_is_exactly_the_decision_improve_and_job_surface():
    from afx_ops.web.write import resolve_write_route
    fixed = {path: (match.kind, match.methods) for path in
             ("/improve", "/improve/runs", "/confirm", "/confirm/check")
             if (match := resolve_write_route(path)) is not None}
    assert fixed == {"/improve": ("improve_form", ("GET", "HEAD")),
                     "/improve/runs": ("improve", ("POST",)),
                     "/confirm": ("confirm", ("POST",)),
                     "/confirm/check": ("check", ("POST",))}
    approve = resolve_write_route("/approvals/42/approve")
    reject = resolve_write_route("/approvals/42/reject")
    assert (approve.kind, approve.methods, approve.approval_id) == ("approve", ("POST",), 42)
    assert (reject.kind, reject.methods, reject.approval_id) == ("reject", ("POST",), 42)
    job = "0123456789abcdef" * 2
    view = resolve_write_route(f"/jobs/{job}")
    cancel = resolve_write_route(f"/jobs/{job}/cancel")
    assert (view.kind, view.methods, view.job_id) == ("job_view", ("GET", "HEAD"), job)
    assert (cancel.kind, cancel.methods, cancel.job_id) == ("cancel", ("POST",), job)
    assert resolve_route(f"/jobs/{job}") is None and resolve_route(f"/jobs/{job}/cancel") is None
    # 読取りの route と重ならず、対象外の path はどれも書込みの route にならない
    for path in ["/", "/approvals", "/approvals/42", "/logs", "/activity", "/plugins",
                 "/plugins/x", "/approvals/42/retry", "/approvals/0/approve",
                 "/approvals/042/approve", "/approvals/42/approve/", "/decide", "/jobs", "/jobs/x",
                 "/events", "/improve/", "/improve/run", "/confirm/", "/Confirm"]:
        assert resolve_write_route(path) is None, path
        if path in {"/", "/approvals", "/approvals/42", "/logs", "/activity", "/plugins",
                    "/plugins/x"}:
            assert resolve_route(path) is not None
