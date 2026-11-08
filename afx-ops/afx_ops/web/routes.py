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
    values: dict[str, int | str]


ROUTE_TABLE = (
    Route("/", "status"),
    Route("/approvals", "approvals"),
    Route("/approvals/{id}", "approval"),
    Route("/logs", "log"),
    Route("/activity", "activity"),
    Route("/plugins", "plugins"),
    Route("/plugins/{name}", "plugin"),
)

_EXACT = {route.path: route for route in ROUTE_TABLE if "{" not in route.path}
_APPROVAL = re.compile(r"/approvals/([1-9][0-9]{0,17})\Z")
# loader の名前の文法
_PLUGIN = re.compile(r"/plugins/([a-z][a-z0-9_]{0,63})\Z")
_BY_PATH = {route.path: route for route in ROUTE_TABLE}


def resolve_route(path: str) -> RouteMatch | None:
    """許可表に載る path だけを、decode せず照合する。"""
    route = _EXACT.get(path)
    if route is not None:
        return RouteMatch(route, {})
    matched = _APPROVAL.fullmatch(path)
    if matched is not None:
        return RouteMatch(_BY_PATH["/approvals/{id}"], {"id": int(matched.group(1))})
    matched = _PLUGIN.fullmatch(path)
    if matched is not None:
        return RouteMatch(_BY_PATH["/plugins/{name}"], {"name": matched.group(1)})
    return None
