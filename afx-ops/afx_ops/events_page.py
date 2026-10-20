"""/events 応答の形の検査。bot と notify が同じ規則で page を読む。"""
from __future__ import annotations

from collections.abc import Callable


def parse_page(reply, failure: Callable[[str], Exception]) -> tuple[list[dict], int, int]:
    """(id 順の読める event、有効な high watermark、id が読めない event 以降の件数) を返す。

    daemon は MAX(id) と行の取得を別の文で実行するので、正常でも watermark より大きい id の event
    が同じ page に入り得る。有効な watermark は両者の大きい方とし、cursor が後ろに戻らないようにする。
    id が読めない event に page の中で最初に当たったところで打ち切る (それ以降は並べ替えの根拠が
    ない)。その前の event は送れるので捨てない。形が不正なら failure("events_invalid") を投げる。
    """
    data = getattr(reply, "data", None)
    if (getattr(reply, "status", None) != 200 or getattr(reply, "code", "x") is not None
            or type(data) is not dict):
        raise failure("events_invalid")
    events, high = data.get("events"), data.get("high_watermark")
    if type(events) is not list or type(high) is not int or high < 0:
        raise failure("events_invalid")
    readable = []
    for event in events:
        # 形の検査は送信直前に行う。並べ替えに要る id だけ先に確かめる
        if type(event) is not dict or type(event.get("id")) is not int or event["id"] <= 0:
            break
        readable.append(event)
    readable.sort(key=lambda item: item["id"])
    if readable:
        high = max(high, readable[-1]["id"])
    return readable, high, len(events) - len(readable)
