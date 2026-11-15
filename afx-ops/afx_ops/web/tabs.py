"""route ごとのタブの固定表と、帯の描画。

query の検査・帯・renderer の振り分けは、この表だけを読む。
"""
from __future__ import annotations

import html
from collections.abc import Mapping
from dataclasses import dataclass

TAB = "tab"
PATH = "path"
CATEGORY = "category"


@dataclass(frozen=True)
class Link:
    """帯の項目の link 先。`kind` が tab なら `?tab=<value>`、path なら別 route、category なら
    `?category=<value>` (value が None なら category を付けない)。"""

    kind: str
    value: str | None = None


@dataclass(frozen=True)
class Tab:
    name: str
    label: str
    link: Link
    write_only: bool = False


ACTIVITY_CATEGORIES = ("NEWS", "TECH", "AGGREGATE", "TRADE", "IMPROVE", "APPROVAL", "SYSTEM")
ALL = "all"

# 表の先頭が既定。帯を持たない route はここに載せない
TABS: dict[str, tuple[Tab, ...]] = {
    "/": (
        Tab("overview", "概要", Link(TAB, "overview")),
        Tab("bot", "Bot", Link(TAB, "bot")),
        Tab("econ", "経済指標", Link(TAB, "econ")),
        Tab("reaction", "反応統計", Link(TAB, "reaction")),
    ),
    "/activity": (Tab(ALL, "全件", Link(CATEGORY, None)),)
    + tuple(Tab(name, name, Link(CATEGORY, name)) for name in ACTIVITY_CATEGORIES),
}


def tab_names(path: str, *, writable: bool = False) -> tuple[str, ...]:
    """`?tab=` に書ける値。link 先が tab の項目だけで、書込み専用は書込みが使えるときだけ。"""
    return tuple(item.name for item in TABS.get(path, ())
                 if item.link.kind == TAB and (writable or not item.write_only))


def default_tab(path: str) -> str:
    return TABS[path][0].name


def _href(path: str, item: Tab, carry: Mapping[str, int]) -> str:
    params: list[tuple[str, str]] = []
    link = item.link
    if link.kind == PATH:
        path = link.value or path
    elif link.kind == TAB:
        params.append(("tab", link.value or ""))
    elif link.kind == CATEGORY and link.value is not None:
        params.append(("category", link.value))
    for key, number in carry.items():
        if type(number) is int:
            params.append((key, str(number)))
    query = "&".join(f"{key}={value}" for key, value in params)
    return html.escape(path + ("?" + query if query else ""), quote=True)


def bar(path: str, current: str, *, carry: Mapping[str, int] | None = None,
        writable: bool = False) -> str:
    """h1 の下に置く帯。href は path・固定表の値・検査済みの整数だけで組む。"""
    items = [item for item in TABS.get(path, ()) if writable or not item.write_only]
    if not items:
        return ""
    links = []
    for item in items:
        mark = ' aria-current="page"' if item.name == current else ""
        links.append(f'<a href="{_href(path, item, carry or {})}"{mark}>'
                     f"{html.escape(item.label, quote=True)}</a>")
    return '<nav class="tabs">' + "".join(links) + "</nav>"
