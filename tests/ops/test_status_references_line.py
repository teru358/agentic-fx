"""`format_status` and `status_provider` for reference series."""
from __future__ import annotations

from agentic_fx.ops import client


def _row(symbol="JP225", interval="1h", source="mt5-live", state="ok", age=None):
    return {"symbol": symbol, "interval": interval, "source": source, "state": state,
            "age_seconds": age}


def test_each_of_the_six_states_is_shown():
    rows = [_row("A1", state="ok"), _row("A2", state="stale", age=4 * 3600 + 120),
            _row("A3", state="missing"), _row("A4", state="fetch_failed"),
            _row("A5", state="budget_deferred"), _row("A6", state="source_unavailable")]
    line = client.format_status({"references": rows}).splitlines()[-1]
    assert line == ("references: A1/1h/mt5-live=ok A2/1h/mt5-live=stale(4h02m) "
                    "A3/1h/mt5-live=missing A4/1h/mt5-live=fetch_failed "
                    "A5/1h/mt5-live=budget_deferred A6/1h/mt5-live=source_unavailable")


def test_without_references_the_status_text_is_unchanged():
    base = {"balance": 1, "equity": 1, "data": {"state": "ready"}}
    assert "references" not in client.format_status(base)
    assert client.format_status({**base, "references": []}) == client.format_status(base)
    assert client.format_status({**base, "references": "junk"}) == client.format_status(base)


def test_a_hostile_symbol_cannot_break_the_line():
    hostile = "JP\x1b[31m225\nfake: line\r\n x=y"
    text = client.format_status({"references": [_row(hostile, state="stale", age=60)]})
    line = next(line for line in text.splitlines() if line.startswith("references:"))
    assert "\x1b" not in text and "\r" not in text
    base = client.format_status({}).count("\n")
    assert text.count("\n") == base + 1 and text.splitlines()[-1] == line
    assert len(line.split(" ")) == 2
    assert "=stale(1m)" in line


def test_an_unknown_state_or_age_is_not_echoed():
    text = client.format_status({"references": [
        _row(state="\x1b[31mboom"), _row("B", state="stale", age="x")]})
    assert "boom" not in text and "=unknown" in text and "stale(-)" in text


def test_the_number_of_rows_is_bounded():
    rows = [_row(f"S{i}") for i in range(40)]
    line = client.format_status({"references": rows})
    assert line.count("=ok") == 16 and line.endswith("(+24)")
