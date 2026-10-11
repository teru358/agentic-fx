"""gate の ViewModel を discord.py の persistent View へ写す。

View の中身 (custom_id、label、破壊的操作か) は gate の ViewModel だけから作り、ここで補わない。
View は timeout を持たない (期限は gate の確認 record が 10 分で決める)。button の callback は
押された component と、この View を付けた投稿 (表示起点) を adapter へ渡すだけで、認可も
状態遷移もしない。

discord は起動口の preload が隔離前に import 済みなので、module の import 時に読んでよい。
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable

import discord

from afx_ops.gate.types import ViewModel

OnClick = Callable[[discord.Interaction], Awaitable[None]]


class _GateButton(discord.ui.Button):
    def __init__(self, *, custom_id: str, label: str, destructive: bool, on_click: OnClick) -> None:
        super().__init__(custom_id=custom_id, label=label,
                         style=discord.ButtonStyle.danger if destructive
                         else discord.ButtonStyle.secondary)
        self._on_click = on_click

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._on_click(interaction)


class GateView(discord.ui.View):
    """ViewModel 1 つ分の persistent View。"""

    def __init__(self, model: ViewModel, on_click: OnClick) -> None:
        super().__init__(timeout=None)
        for component in model.components:
            self.add_item(_GateButton(custom_id=component.custom_id, label=component.label,
                                      destructive=component.destructive, on_click=on_click))

    @property
    def custom_ids(self) -> frozenset[str]:
        return frozenset(item.custom_id for item in self.children
                         if isinstance(item, discord.ui.Button) and item.custom_id)
