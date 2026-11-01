"""抑止通知の dedupe: order ごとに、通知済みの連結成分 (固定 id と event id 集合)。

同じ order の次の抑止は、通知済みの成分と event を 1 つでも共有すれば同じ成分とする。
成分 id は最初に抑止した時点の値のまま変えず、後から event が合流しても、最小 id の
event が supersede で消えても、通知は増えない。共有する event が無い成分は別の帯なので
通知を 1 回増やす。DB の値は JSON `[{"id": int, "events": [int, ...]}, ...]`。
"""
from __future__ import annotations

import json
from typing import Iterable


def load(raw: str | None) -> list[dict]:
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except ValueError:
        return []
    out = []
    if isinstance(data, list):
        for item in data:
            if (isinstance(item, dict) and isinstance(item.get("id"), int)
                    and isinstance(item.get("events"), list)
                    and all(isinstance(e, int) for e in item["events"])):
                out.append({"id": item["id"], "events": sorted(set(item["events"]))})
    return out


def dump(records: list[dict]) -> str:
    return json.dumps(records, sort_keys=True, separators=(",", ":"))


def record_suppression(records: list[dict], component_id: int,
                       event_ids: Iterable[int]) -> tuple[list[dict], bool]:
    """成分を records に反映する。戻り値は (新しい records, 通知が要るか)。

    通知済みの成分と event を共有すれば、その成分に event を足すだけで通知しない
    (共有する成分が複数あれば最初のものに畳む)。共有が無ければ新しい成分として
    追加し、通知が要る。
    """
    events = set(event_ids)
    matched = [r for r in records if events & set(r["events"])]
    if not matched:
        return [*records, {"id": component_id, "events": sorted(events)}], True
    head = matched[0]
    merged = set(events)
    for r in matched:
        merged |= set(r["events"])
    out = []
    for r in records:
        if r is head:
            out.append({"id": head["id"], "events": sorted(merged)})
        elif not any(r is m for m in matched):
            out.append(r)
    return out, False
