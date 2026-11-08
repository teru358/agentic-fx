"""afx-ops の画面 test が読む JSON の見本は、daemon の実装が今作る応答と一致する。"""
from __future__ import annotations

import json

from tests.plugin.web_fixture_world import FIXTURE_DIR, build_dtos


def test_the_committed_samples_are_what_the_daemon_builds_now(tmp_path):
    dtos = build_dtos(tmp_path)
    for key, value in dtos.items():
        stored = json.loads((FIXTURE_DIR / f"{key}.json").read_text())
        assert stored == json.loads(json.dumps(value)), key
