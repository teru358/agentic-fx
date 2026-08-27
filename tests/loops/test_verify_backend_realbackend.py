"""実 backend (local/claude/codex) を実際に叩く `verify_backend` の統合
テスト。既定スイートからは除外される (`pyproject.toml` の `addopts`)。
人間が `-m realbackend` を明示して実行する — 課金枠消費の上限は
「実測回数の上限」節 (手動ランブック) と同じ (各構成 3 回以内)。

<!-- precheck 2026-08-23 wave3: T13-B9 -->
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from agentic_fx.config import load_settings
from agentic_fx.core.contracts import SystemClock
from agentic_fx.loops.verify_backend import verify_backend

pytestmark = pytest.mark.realbackend

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _settings():
    return load_settings(_REPO_ROOT / "config" / "settings.yaml.example")


def test_verify_backend_real_local(tmp_path):
    if shutil.which("curl") is None:
        pytest.skip("curl not available to probe llama-swap")
    result = verify_backend(tmp_path, _settings(), backend="local",
                           provider=None, clock=SystemClock())
    assert result.ok is True, result.detail


def test_verify_backend_real_codex_llama_swap(tmp_path):
    if not (Path("~/.codex/auth.json").expanduser().exists()):
        pytest.skip("~/.codex/auth.json not present")
    result = verify_backend(tmp_path, _settings(), backend="codex",
                           provider="llama_swap", clock=SystemClock())
    assert result.ok is True, result.detail


def test_verify_backend_real_codex_chatgpt(tmp_path):
    if not (Path("~/.codex/auth.json").expanduser().exists()):
        pytest.skip("~/.codex/auth.json not present")
    result = verify_backend(tmp_path, _settings(), backend="codex",
                           provider="chatgpt", clock=SystemClock())
    assert result.ok is True, result.detail
