"""公開する read-only route の固定表。"""
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Route:
    path: str
    client_method: str


@dataclass(frozen=True)
class RouteMatch:
    route: Route
    values: dict[str, int]


ROUTE_TABLE = (
    Route("/", "status"),
    Route("/approvals", "approvals"),
    Route("/approvals/{id}", "approval"),
    Route("/logs", "log"),
    Route("/activity", "activity"),
)

_EXACT = {route.path: route for route in ROUTE_TABLE if "{" not in route.path}
_APPROVAL = re.compile(r"/approvals/([1-9][0-9]{0,17})\Z")


def resolve_route(path: str) -> RouteMatch | None:
    """許可表に載る path だけを、decode せず照合する。"""
    route = _EXACT.get(path)
    if route is not None:
        return RouteMatch(route, {})
    matched = _APPROVAL.fullmatch(path)
    if matched is None:
        return None
    return RouteMatch(ROUTE_TABLE[2], {"id": int(matched.group(1))})
