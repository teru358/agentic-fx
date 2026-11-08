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
    def __init__(self, plugins_root: Path, settings) -> None:
        self._plugins_root = plugins_root
        self._settings = settings
        self._lock = threading.Lock()
        self._key: tuple | None = None
        self._view: PluginView | None = None
        self.builds = 0

    def get(self, conn: sqlite3.Connection) -> PluginView:
        key = (read_model_inputs.live_signature(self._plugins_root),
               read_model_inputs.db_generation(conn))
        with self._lock:
            if self._view is not None and key == self._key and None not in key[1]:
                return self._view
            inputs = read_model_inputs.collect_inputs(self._plugins_root, conn)
            model = read_model.build_read_model(inputs, settings=self._settings)
            view = PluginView(model, read_model.model_to_dto(model))
            self._key, self._view = key, view
            self.builds += 1
            return view
