"""段 3 の参照銘柄の選択: 一覧の取得、既定値、拒否、要約、確定までの無書き込み。"""
from __future__ import annotations

from dataclasses import replace

import pytest

from agentic_fx.config import load_settings
from agentic_fx.setup.settings_edit import edit_settings
from agentic_fx.setup.wizard import run_wizard

from tests.setup.test_wizard_rerun import CONSISTENT, installed_root
from tests.setup.test_wizard_abort import only_locks_added
from tests.setup.wizard_support import (
    FakeResponse, ScriptedWizardIO, fresh_responder, make_deps, make_root, snapshot,
)

SYMBOLS = ["JP225", "US500", "US100", "USDJPY", "EURUSD", "USOIL"]
PROMPT = "参照銘柄"


def watch_of(root):
    return load_settings(root / "config" / "settings.yaml").datafeed.watch_symbols


def run(tmp_path, root, rules, *, symbols=SYMBOLS, status=200, deps_edit=None):
    deps, _, fetch, _ = make_deps(tmp_path)
    fetch.bridge_symbols = symbols
    fetch.bridge_symbols_status = status
    if deps_edit:
        deps = deps_edit(deps)
    io = ScriptedWizardIO(responder=rules)
    rc = run_wizard(root, io, deps)
    return rc, io, fetch


def test_first_run_default_is_empty_and_nothing_is_written_to_settings(tmp_path):
    root = make_root(tmp_path)
    rc, io, fetch = run(tmp_path, root, fresh_responder())
    assert rc == 0, io.text
    assert io.asked_with(PROMPT)
    assert fetch.urls("/symbols")
    assert watch_of(root) == []
    assert "参照銘柄" not in io.text.split("[段 9 要約]")[1]


def test_picked_symbols_reach_the_summary_and_are_written_only_at_commit(tmp_path):
    root = make_root(tmp_path)
    rc, io, _ = run(tmp_path, root, fresh_responder((PROMPT, "JP225 US500")))
    assert rc == 0, io.text
    assert "参照銘柄 (分析専用・発注しない): JP225 US500" in io.text
    assert io.text.index("[段 9 要約]") < io.text.index("参照銘柄 (分析専用")
    assert watch_of(root) == ["JP225", "US500"]


def test_declining_the_commit_after_picking_writes_nothing(tmp_path):
    root = make_root(tmp_path)
    before = snapshot(root)
    rc, io, _ = run(tmp_path, root, fresh_responder((PROMPT, "JP225"), ("書き込みます", "n")))
    assert rc == 1
    only_locks_added(before, snapshot(root))


def test_quitting_at_the_watch_question_writes_nothing(tmp_path):
    root = make_root(tmp_path)
    before = snapshot(root)
    rc, io, _ = run(tmp_path, root, fresh_responder((PROMPT, "q")))
    assert rc == 1
    only_locks_added(before, snapshot(root))


def test_rerun_default_is_the_current_value_and_enter_keeps_it(tmp_path):
    root, before = installed_root(tmp_path, extra={"datafeed.watch_symbols": ["JP225"]})
    rc, io, _ = run(tmp_path, root, fresh_responder())
    assert rc == 0, io.text
    assert any("[JP225]" in prompt for prompt in io.asked_with(PROMPT))
    assert watch_of(root) == ["JP225"]
    assert "参照銘柄 (分析専用・発注しない): JP225" in io.text.split("[段 9 要約]")[1]


def test_rerun_can_clear_with_a_dash_and_the_summary_says_what_is_removed(tmp_path):
    root, _ = installed_root(tmp_path, extra={"datafeed.watch_symbols": ["JP225"]})
    rc, io, _ = run(tmp_path, root, fresh_responder((PROMPT, "-")))
    assert rc == 0, io.text
    assert "参照銘柄: なし (今ある JP225 を外します)" in io.text
    assert watch_of(root) == []


@pytest.mark.parametrize("answer,reason", [
    ("NOPE", "bridge の銘柄にありません"),
    ("JP225 JP225", "重なっています"),
    ("USDJPY", "取引 pair です"),
    (" ".join(f"S{i}" for i in range(11)), "多すぎます"),
])
def test_a_bad_answer_is_refused_with_a_reason_and_asked_again(tmp_path, answer, reason):
    root = make_root(tmp_path)
    symbols = SYMBOLS + [f"S{i}" for i in range(11)]
    rc, io, _ = run(tmp_path, root, fresh_responder((PROMPT, (answer, "JP225"))), symbols=symbols)
    assert rc == 0, io.text
    assert reason in io.text
    assert len(io.asked_with(PROMPT)) == 2
    assert watch_of(root) == ["JP225"]


def test_a_question_mark_searches_the_candidates(tmp_path):
    root = make_root(tmp_path)
    rc, io, _ = run(tmp_path, root, fresh_responder((PROMPT, ("?us", "US500"))))
    assert rc == 0, io.text
    assert "US500 US100 USDJPY EURUSD USOIL" in io.text


@pytest.mark.parametrize("status", [401, 503])
def test_a_failed_listing_skips_the_question_and_keeps_the_current_value(tmp_path, status):
    root, before = installed_root(tmp_path, extra={"datafeed.watch_symbols": ["JP225"]})
    rc, io, _ = run(tmp_path, root, fresh_responder(), status=status)
    assert rc == 0, io.text
    assert not io.asked_with(PROMPT)
    assert "参照銘柄の選択は飛ばします" in io.text
    assert watch_of(root) == ["JP225"]


def test_a_connection_error_is_the_same_safe_message_as_the_price_probe(tmp_path):
    import httpx
    root = make_root(tmp_path)

    def edit(deps):
        base = deps.fetch

        def fetch(url, **kwargs):
            if url.endswith("/symbols"):
                raise httpx.ConnectError("refused")
            return base(url, **kwargs)
        return replace(deps, fetch=fetch)

    rc, io, _ = run(tmp_path, root, fresh_responder(), deps_edit=edit)
    assert rc == 0, io.text
    assert "MT5 bridge に接続できません" in io.text
    assert watch_of(root) == []


def test_an_empty_listing_asks_nothing(tmp_path):
    root = make_root(tmp_path)
    rc, io, _ = run(tmp_path, root, fresh_responder(), symbols=[])
    assert rc == 0, io.text
    assert not io.asked_with(PROMPT)


def test_the_yfinance_branch_never_asks_or_lists(tmp_path):
    root = make_root(tmp_path)
    deps, _, fetch, _ = make_deps(tmp_path)
    fetch.bridge_symbols = SYMBOLS
    io = ScriptedWizardIO(responder=fresh_responder(("価格源", ("3", "1"))))
    assert run_wizard(root, io, deps) == 0, io.text
    assert not fetch.urls("/symbols")
    assert not io.asked_with(PROMPT)


def test_a_planned_bridge_that_was_not_probed_never_asks(tmp_path):
    root = make_root(tmp_path)
    rc, io, fetch = run(tmp_path, root, fresh_responder(("価格源", "2"), ("今 bridge への接続を試しますか", "n")))
    assert rc == 0, io.text
    assert not fetch.urls("/symbols")
    assert not io.asked_with(PROMPT)


def test_the_bridge_key_is_sent_to_symbols_like_to_health(tmp_path):
    root = make_root(tmp_path)
    deps, _, fetch, _ = make_deps(tmp_path, environ={"MT5_BRIDGE_API_KEY": "KEYVALUE-1"})
    fetch.bridge_symbols = SYMBOLS
    io = ScriptedWizardIO(responder=fresh_responder(("bridge 側で API キー", "1")))
    assert run_wizard(root, io, deps) == 0, io.text
    headers = [kwargs.get("headers") for url, kwargs in fetch.calls if url.endswith("/symbols")]
    assert headers == [{"X-Bridge-Api-Key": "KEYVALUE-1"}]
    assert "KEYVALUE-1" not in io.text


def test_with_no_watch_symbols_the_run_is_identical_to_a_bridge_without_a_listing(tmp_path):
    first = make_root(tmp_path / "a")
    second = make_root(tmp_path / "b")
    rc1, io1, _ = run(tmp_path / "a", first, fresh_responder(), symbols=[])
    rc2, io2, _ = run(tmp_path / "b", second, fresh_responder(), symbols=SYMBOLS)
    assert rc1 == rc2 == 0
    assert (first / "config" / "settings.yaml").read_text() == (second / "config" / "settings.yaml").read_text()
    stripped = [line for line in io2.out if "参照銘柄" not in line and "bridge の銘柄は" not in line]
    assert [l for l in io1.out if l] == [l for l in stripped if l]
