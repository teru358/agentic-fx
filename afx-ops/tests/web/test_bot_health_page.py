"""web の Status に出す bot の gateway health: daemon API の値だけを、固定の語で、秘密なしで出す。"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from afx_ops.api_client import Reply
from afx_ops.web import render
from afx_ops.web.request import Request, handle_request

# Discord の bot token の形をした偽の値。実行時に組み立てて、source に token の形の文字列を残さない
_CANARY_TOKEN = ".".join(("MTIzNDU2Nzg5MDEyMzQ1Njc4", "GabCdE", "abcdefghijklmnopqrstuvwxyz0123456789"))
SECRETS = (_CANARY_TOKEN,
           "https://discord.com/api/webhooks/123456789012345678/secret-webhook-token",
           "123456789012345678", "<script>alert(1)</script>")
STATES = ("healthy", "unhealthy", "stalled", "stale", "unknown", "not_configured", "permanent-failure")


class Client:
    def __init__(self, data):
        self.data = data

    def status(self):
        return Reply(200, self.data, None)


def _page(health) -> str:
    data = {"mode": "paper"}
    if health is not None:
        data["bot_health"] = health
    return handle_request(Request("GET", "/", {}), Client(data)).body.decode()


@pytest.mark.parametrize("state", STATES)
def test_each_gateway_health_word_is_shown(state):
    page = _page({"state": state, "alternate_push": "configured"})
    assert f"<dd>{state}</dd>" in page


def test_the_state_words_match_the_daemon_contract():
    import json
    golden = json.loads(Path(__file__).resolve().parents[3].joinpath(
        "tests", "fixtures", "bot_health_contract.json").read_text(encoding="utf-8"))
    assert list(render.BOT_HEALTH_STATES) == golden["states"]


@pytest.mark.parametrize("state", [s for s in STATES if s != "healthy"])
def test_approval_interaction_is_reported_unavailable_unless_healthy(state):
    assert render.INTERACTION_UNAVAILABLE_TEXT in _page({"state": state, "alternate_push": "configured"})


def test_a_healthy_gateway_does_not_say_interaction_is_unavailable():
    assert render.INTERACTION_UNAVAILABLE_TEXT not in _page(
        {"state": "healthy", "alternate_push": "configured"})


def test_alternate_push_none_has_no_warning_when_the_bot_is_not_configured():
    assert "alternate_push=none (" not in _page({"state": "not_configured", "alternate_push": "none"})


def test_alternate_push_none_shows_the_warning_and_configured_does_not():
    assert "alternate_push=none" in _page({"state": "healthy", "alternate_push": "none"})
    page = _page({"state": "healthy", "alternate_push": "configured"})
    assert "alternate_push=none" not in page and "<dd>configured</dd>" in page


def test_notify_not_configured_is_not_presented_as_unhealthy():
    page = _page({"state": "healthy", "alternate_push": "none"})
    assert "<dd>healthy</dd>" in page and "<dd>unhealthy</dd>" not in page


def test_unknown_values_are_not_echoed():
    page = _page({"state": SECRETS[0], "alternate_push": SECRETS[1], "gateway_state": SECRETS[2],
                  "delivery_state": SECRETS[3], "terminal_code": SECRETS[1],
                  "observed_at": "not-a-time", "token": SECRETS[0]})
    assert not any(secret in page for secret in SECRETS)
    assert "<dd>unknown</dd>" in page and "<dd>none</dd>" in page


def test_a_terminal_code_and_the_gateway_fields_are_shown_when_well_formed():
    page = _page({"state": "permanent-failure", "alternate_push": "none", "terminal_code": "token_invalid",
                  "gateway_state": "unhealthy", "delivery_state": "stalled",
                  "observed_at": "2026-11-14T12:00:00Z"})
    assert "token_invalid" in page and "<dd>unhealthy</dd>" in page and "<dd>stalled</dd>" in page


def test_no_section_when_the_daemon_has_no_bot_health():
    assert "gateway health" not in _page(None)
    assert "gateway health" not in _page("text")


def test_the_web_package_never_reads_the_bot_state_dir():
    """web は daemon API だけを読む。bot の snapshot の reader や state dir を import しない。"""
    root = Path(render.__file__).resolve().parent
    for path in sorted(root.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.ImportFrom):
                names = [node.module or ""] + [a.name for a in node.names]
            elif isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            for name in names:
                assert "afx_ops.health" not in name and name != "health", path.name
