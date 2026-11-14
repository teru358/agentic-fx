"""表示 tz の読み出しと名前の検査 (settings の他の項目や .env に依存しない)。"""
from __future__ import annotations

from pathlib import Path

import yaml

from agentic_fx.core.display_time import effective_tz_name


def display_timezone_from_text(text: str | None) -> str:
    """読み済みの `settings.yaml` の本文から `display_timezone` だけを解釈する。

    送出しない。本文が無い・壊れている・tz として読めないときは `UTC`。
    """
    if text is None:
        return "UTC"
    try:
        data = yaml.safe_load(text)
        name = data.get("display_timezone") if isinstance(data, dict) else None
    except Exception:  # noqa: BLE001 — 読めなければ UTC
        return "UTC"
    return effective_tz_name(name)


def read_display_timezone(settings_path: Path) -> str:
    """`settings.yaml` の `display_timezone` だけを読む。

    送出しない。ファイルが無い・壊れている・tz として読めないときは `UTC`。
    `.env` も他の設定も読まない (鍵が無い環境でも表示 tz だけは決まる)。
    """
    try:
        text = Path(settings_path).read_text(encoding="utf-8")
    except Exception:  # noqa: BLE001 — 読めなければ UTC
        return "UTC"
    return display_timezone_from_text(text)


def is_region_name(name: object) -> bool:
    """`Area/Location` の形 (`/` を含み `Etc/` で始まらない) か `UTC`。

    略称 (`EST`) や固定 offset の名前 (`Etc/GMT-9`)、夏時間の規則を持たない
    互換名 (`Japan`) を弾くための形の検査で、zone が実在するかは見ない。
    """
    if not isinstance(name, str):
        return False
    if name == "UTC":
        return True
    return "/" in name and not name.startswith("Etc/")


REGION_NAME_HINT = "`America/New_York` のような地域名で入力してください (略称や `Etc/GMT-9` は不可)"


def region_name_warning(name: object) -> str | None:
    """settings に書かれた名前が地域名の形でないときの警告文。問題が無ければ None。"""
    if is_region_name(name):
        return None
    return (f"display_timezone {str(name)[:64]!r} は地域名ではありません。"
            f"{REGION_NAME_HINT}")
