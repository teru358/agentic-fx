from unittest.mock import patch

from agentic_fx.core.notifier import Notifier


def test_disabled_is_noop():
    n = Notifier(enabled=False, webhook_url="https://example.com/hook")
    with patch("urllib.request.urlopen") as mock:
        n.send("hello")
        mock.assert_not_called()


def test_send_posts_json():
    n = Notifier(enabled=True, webhook_url="https://example.com/hook")
    with patch("urllib.request.urlopen") as mock:
        n.send("hello")
        req = mock.call_args[0][0]
        assert req.full_url == "https://example.com/hook"
        assert b"hello" in req.data


def test_send_failure_swallowed():
    n = Notifier(enabled=True, webhook_url="https://example.com/hook")
    with patch("urllib.request.urlopen", side_effect=OSError("down")):
        n.send("hello")  # 例外が漏れないこと
