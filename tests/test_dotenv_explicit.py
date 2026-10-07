"""`.env` の読み込みは明示的な 1 関数だけが行い、設定の検証は環境を変えない。"""
from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import dotenv
import pytest

from agentic_fx import config as config_mod
from agentic_fx.config import load_env_file, load_settings
from agentic_fx.entry import main

_KEY = "AFX_DOTENV_PROBE_VALUE"


def _example_settings(root: Path) -> Path:
    (root / "config").mkdir(exist_ok=True)
    path = root / "config" / "settings.yaml"
    path.write_bytes(Path("config/settings.yaml.example").read_bytes())
    return path


@pytest.fixture
def clean_probe_env(monkeypatch):
    # 後始末は monkeypatch に任せる (load_env_file が直接 os.environ に書くため)
    monkeypatch.setenv(_KEY, "")
    monkeypatch.delenv(_KEY)


def test_load_settings_does_not_read_dotenv_or_touch_environ(
        tmp_path, monkeypatch, clean_probe_env):
    path = _example_settings(tmp_path)
    (tmp_path / ".env").write_text(f"{_KEY}=from-dotenv\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    calls: list[tuple] = []

    def _spy(*a, **kw):
        calls.append((a, kw))
        return False

    monkeypatch.setattr(dotenv, "load_dotenv", _spy)
    monkeypatch.setattr(config_mod, "load_dotenv", _spy, raising=False)
    before = dict(os.environ)

    load_settings(path)

    assert calls == []
    assert dict(os.environ) == before
    assert _KEY not in os.environ


def test_load_env_file_loads_only_the_given_path(
        tmp_path, monkeypatch, clean_probe_env):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / ".env").write_text(f"{_KEY}=wrong\n", encoding="utf-8")
    monkeypatch.chdir(elsewhere)
    target = tmp_path / "root" / ".env"
    target.parent.mkdir()
    target.write_text(f"{_KEY}=right\n", encoding="utf-8")

    load_env_file(target)

    assert os.environ[_KEY] == "right"


def test_load_env_file_does_not_override_existing_environment(
        tmp_path, monkeypatch):
    monkeypatch.setenv(_KEY, "exported")
    env = tmp_path / ".env"
    env.write_text(f"{_KEY}=from-dotenv\n", encoding="utf-8")

    load_env_file(env)

    assert os.environ[_KEY] == "exported"


def test_load_env_file_missing_file_is_a_noop(tmp_path, clean_probe_env):
    load_env_file(tmp_path / "absent.env")
    assert _KEY not in os.environ


def test_run_service_loads_dotenv_before_reading_settings(
        tmp_path, monkeypatch, clean_probe_env):
    """service の top-level が従来どおり settings.yaml と .env の両方を読む。"""
    import agentic_fx.service as service_mod

    _example_settings(tmp_path)
    (tmp_path / ".env").write_text(f"{_KEY}=from-dotenv\n", encoding="utf-8")
    seen: dict = {}

    class _Stop(Exception):
        pass

    def _build_app(root, **kw):
        seen["env"] = os.environ.get(_KEY)
        seen["settings"] = service_mod.load_settings(
            root / "config" / "settings.yaml")
        raise _Stop

    monkeypatch.setattr(service_mod, "ensure_initialized", lambda root: None)
    monkeypatch.setattr(service_mod, "build_app", _build_app)
    monkeypatch.setattr(service_mod, "setup_technical_logging",
                        lambda *a, **kw: None)

    with pytest.raises(_Stop):
        service_mod.run_service(tmp_path, daemon=True)

    assert seen["env"] == "from-dotenv"
    expected = load_settings(tmp_path / "config" / "settings.yaml")
    assert seen["settings"] == expected


def test_cli_dispatch_loads_dotenv_from_root(
        tmp_path, monkeypatch, clean_probe_env):
    """設定を要する既存 CLI 経路 (afx plugin ...) も root の .env を明示的に読む。"""
    _example_settings(tmp_path)
    (tmp_path / "data").mkdir()
    (tmp_path / ".env").write_text(f"{_KEY}=from-dotenv\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    with patch("agentic_fx.backtest.cli.ensure_initialized"), \
         patch("agentic_fx.entry.service.run_service"):
        main(["plugin", "materialize", "absent"])

    assert os.environ[_KEY] == "from-dotenv"


def test_run_init_does_not_touch_environment(
        tmp_path, monkeypatch, clean_probe_env):
    """init は検証だけで .env を読まない。"""
    import agentic_fx.service as service_mod

    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "settings.yaml.example").write_bytes(
        Path("config/settings.yaml.example").read_bytes())
    (tmp_path / ".env").write_text(f"{_KEY}=from-dotenv\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    with patch("agentic_fx.service.PriceProvider") as pp, \
         patch("agentic_fx.service._check_llama_swap"):
        pp.return_value.healthcheck.return_value = "yfinance"
        service_mod.run_init(tmp_path)

    assert _KEY not in os.environ
