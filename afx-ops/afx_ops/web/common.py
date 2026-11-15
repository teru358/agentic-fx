"""Shared helpers for web page rendering."""
from afx_ops.web import render
from afx_ops.web.routes import resolve_route


def pick(value: object, allowed: frozenset[str]) -> dict:
    if type(value) is not dict:
        return {}
    return {key: item for key, item in value.items() if key in allowed}


def plugin_link(name: object) -> str:
    if type(name) is str and resolve_route(f"/plugins/{name}") is not None:
        return f'<a href="/plugins/{name}">{name}</a>'
    return render._text(name) or "-"


def approval_link(approval_id: object) -> str:
    if type(approval_id) is int and resolve_route(f"/approvals/{approval_id}") is not None:
        return f'<a href="/approvals/{approval_id}">#{approval_id}</a>'
    return "-"


def display_time(value: object, tz: str) -> str:
    return render._time_text(value, tz) or "-" if type(value) is str else "-"


