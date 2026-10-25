from email.message import Message

from agentic_fx.datafeed.user_agent import user_agent


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
