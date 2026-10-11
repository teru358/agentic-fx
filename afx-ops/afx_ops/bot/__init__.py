"""同梱 bot (`afx-ops bot`): gate を discord.py に結ぶ adapter と業務処理。

起動口の表 (`cli.RUNTIMES`) はこの ``run`` を指す。discord.py と runtime module は起動口の
preload が隔離前に import するので、起動口自身 (``afx_ops.cli`` の import) は discord /
aiohttp を読まない。
"""
from __future__ import annotations


def run(isolated) -> int:
    from afx_ops.bot import runtime
    return runtime.run(isolated)
