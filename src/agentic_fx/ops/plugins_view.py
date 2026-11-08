"""plugin の一覧・詳細の読み出しと、世代 key の memo。

入力は FS (``plugins/``) と DB の行で、組み立ては ``plugin.read_model`` の純関数。
content hash の計算を含む組み立ては、配備の世代 (``plugins/`` の見た目の署名と、承認・
配備・切替の行の最終更新) が変わったときだけやり直す。何も書かず、plugin の lock も取らない。
"""
from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path

from agentic_fx.plugin import read_model, read_model_inputs


@dataclass(frozen=True, slots=True)
class PluginView:
    model: read_model.ReadModel
    listing: dict


class PluginViewCache:
    """世代 key が同じ間は組み立て済みの view を返す。

    ``lane`` は ops lane を取る context manager の factory。lane の中では DB を読むだけで、
    FS の走査・hash・resolver は memo 自身の lock の中 (lane の外) で行う。一時的に読み損ねた
    入力を含む view は返すが覚えない。"""

    def __init__(self, plugins_root: Path, settings) -> None:
        self._plugins_root = plugins_root
        self._settings = settings
        self._lock = threading.Lock()
        self._key: tuple | None = None
        self._view: PluginView | None = None
        self.builds = 0

    def get(self, conn: sqlite3.Connection, lane, acquire) -> PluginView:
        """``acquire(lock)`` は memo の lock を期限つきで取る (取れなければ例外)。"""
        signature = read_model_inputs.live_signature(self._plugins_root)
        with lane():
            generation = read_model_inputs.db_generation(conn)
        key = (signature, generation)
        keyable = None not in generation and signature != ("unavailable",)
        acquire(self._lock)
        try:
            return self._get_locked(conn, lane, key, keyable)
        finally:
            self._lock.release()

    def _get_locked(self, conn, lane, key, keyable) -> PluginView:
        if self._view is not None and key == self._key and keyable:
            return self._view
        with lane():
            db_rows = read_model_inputs.read_db_inputs(conn)
        live = read_model_inputs.scan_live(self._plugins_root)
        inputs = read_model_inputs.assemble_inputs(live, db_rows)
        model = read_model.build_read_model(inputs, settings=self._settings)
        view = PluginView(model, read_model.model_to_dto(model))
        self.builds += 1
        if keyable and not read_model_inputs.is_degraded(inputs):
            self._key, self._view = key, view
        return view
