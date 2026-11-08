"""WebClient の plugin 読取り: 名前は loader の文法で照合し、path は読取りだけ。"""
from __future__ import annotations

import inspect

import pytest

from afx_ops.api_client import NotifyClient, WebClient


def _client():
    calls = []
    client = WebClient.__new__(WebClient)
    client._get = lambda path, **kwargs: calls.append((path, kwargs)) or "reply"
    return client, calls


def test_plugins_and_plugin_read_the_two_routes():
    client, calls = _client()
    assert client.plugins() == "reply" and client.plugin("rsi_14") == "reply"
    assert calls == [("/v1/plugins", {}), ("/v1/plugins/rsi_14", {})]


@pytest.mark.parametrize("name", ["", "RSI", "1rsi", "r-s", "../x", "a/b", "%2e%2e",
                                  "a" * 65, "rsi\n", None, 5])
def test_a_name_outside_the_grammar_is_refused_before_any_request(name):
    client, calls = _client()
    with pytest.raises(ValueError):
        client.plugin(name)
    assert calls == []


def test_the_new_methods_are_reads_that_take_no_actor():
    for name in ("plugins", "plugin"):
        assert "actor" not in inspect.signature(getattr(WebClient, name)).parameters
        assert not hasattr(NotifyClient, name)
