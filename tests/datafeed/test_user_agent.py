from email.message import Message

import pytest

from agentic_fx.datafeed.user_agent import user_agent


@pytest.fixture(autouse=True)
def _fresh_user_agent():
    user_agent.cache_clear()
    yield
    user_agent.cache_clear()


def test_user_agent_reads_metadata_once(monkeypatch):
    calls = []

    def version(_):
        calls.append(1)
        return "9.8.7"

    with monkeypatch.context() as patch:
        patch.setattr("importlib.metadata.version", version)
        patch.setattr("importlib.metadata.metadata", lambda _: Message())
        assert user_agent() == user_agent() == "agentic-fx/9.8.7"
    assert len(calls) == 1


def test_user_agent_uses_repository_metadata(monkeypatch):
    message = Message()
    message["Project-URL"] = "Repository, https://example.invalid/repository"
    with monkeypatch.context() as patch:
        patch.setattr("importlib.metadata.version", lambda _: "9.8.7")
        patch.setattr("importlib.metadata.metadata", lambda _: message)
        assert user_agent() == "agentic-fx/9.8.7 (+https://example.invalid/repository)"


def test_user_agent_omits_missing_repository(monkeypatch):
    with monkeypatch.context() as patch:
        patch.setattr("importlib.metadata.version", lambda _: "9.8.7")
        patch.setattr("importlib.metadata.metadata", lambda _: Message())
        assert user_agent() == "agentic-fx/9.8.7"


def test_user_agent_without_distribution_never_falls_back_to_httpx(monkeypatch):
    from importlib import metadata

    def missing(_):
        raise metadata.PackageNotFoundError("agentic-fx")

    with monkeypatch.context() as patch:
        patch.setattr("importlib.metadata.version", missing)
        patch.setattr("importlib.metadata.metadata", missing)
        assert user_agent() == "agentic-fx/unknown"


def test_user_agent_repository_label_is_case_insensitive_and_other_labels_ignored(monkeypatch):
    message = Message()
    message["Project-URL"] = "Homepage, https://example.invalid/home"
    message["Project-URL"] = "repository, https://example.invalid/repo"
    with monkeypatch.context() as patch:
        patch.setattr("importlib.metadata.version", lambda _: "1.2.3")
        patch.setattr("importlib.metadata.metadata", lambda _: message)
        assert user_agent() == "agentic-fx/1.2.3 (+https://example.invalid/repo)"


def _pyproject():
    import tomllib
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    return root, tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))


def test_user_agent_is_built_from_the_project_declaration(monkeypatch):
    """Metadata is stood in from pyproject.toml, so a stale installed dist-info cannot fail this."""
    _, data = _pyproject()
    message = Message()
    message["Project-URL"] = f"Repository, {data['project']['urls']['Repository']}"
    with monkeypatch.context() as patch:
        patch.setattr("importlib.metadata.version", lambda _: data["project"]["version"])
        patch.setattr("importlib.metadata.metadata", lambda _: message)
        expected = f"agentic-fx/{data['project']['version']} (+{data['project']['urls']['Repository']})"
        assert user_agent() == expected


def test_installed_distribution_matches_pyproject():
    """If this fails the installed dist-info is older than pyproject.toml: run uv sync."""
    _, data = _pyproject()
    ua = user_agent()
    assert ua == f"agentic-fx/{data['project']['version']} (+{data['project']['urls']['Repository']})", ua


def test_repository_url_literal_lives_only_in_pyproject():
    from pathlib import Path
    root, data = _pyproject()
    url = data["project"]["urls"]["Repository"]
    hits = [str(p.relative_to(root)) for p in (root / "src").rglob("*.py") if url in p.read_text(encoding="utf-8")]
    assert hits == []
    assert not [p for p in (root / "src").rglob("*.py") if "github.com/teru358" in p.read_text(encoding="utf-8")]
