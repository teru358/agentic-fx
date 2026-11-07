"""status の econ_signal.reaction は許可投影を通り、件数と数値だけが残る。"""
from __future__ import annotations

from agentic_fx.ops import service as svc
from tests.ops.test_econ_status_view import _econ, _status


def _row(**over):
    row = {"currency": "USD", "kind": "importance", "key": "3", "window_min": 240,
           "n": 25, "unique_ts_n": 24, "unresolvable_n": 3, "range_median": 12.5,
           "range_p75": 14.0, "abs_median": 2.0, "abs_p75": 3.5, "net_median": 1.0,
           "net_p75": 2.5, "close_change_median": -0.5}
    row.update(over)
    return row


def _reaction(rows=None, **over):
    reaction = {"floor": 20, "bundles": rows if rows is not None else [_row()],
                "backlog": {"sets_without_values": 4, "events_without_currency_key": 1}}
    reaction.update(over)
    return reaction


def _out(env, reaction):
    return _status(env, {"econ_signal": _econ(reaction=reaction)})["econ_signal"]["reaction"]


def test_the_reaction_section_passes_counts_and_numbers_through(ops_env):
    out = _out(ops_env, _reaction())
    assert out["floor"] == 20
    assert out["bundles"] == [_row()]
    assert out["backlog"] == {"sets_without_values": 4, "events_without_currency_key": 1}


def test_a_name_bundle_keeps_its_key_and_a_hostile_key_is_dropped(ops_env):
    rows = [_row(kind="name", key="Non-Farm Payrolls", n=5),
            _row(kind="name", key="x\x1b[31m", n=6),
            _row(kind="name", key="y" * 200, n=7)]
    out = _out(ops_env, _reaction(rows))
    assert [r["key"] for r in out["bundles"]] == ["Non-Farm Payrolls"]


def test_extra_fields_and_out_of_range_values_are_dropped(ops_env):
    hostile = _row(n=-1, range_median=float("nan"), abs_p75="1.0", currency="usd",
                   window_min=7, secret="leak")
    ok = _row(secret="leak", net_p75=float("inf"))
    out = _out(ops_env, _reaction([hostile, ok], extra="leak"))
    assert "extra" not in out
    assert len(out["bundles"]) == 1
    row = out["bundles"][0]
    assert "secret" not in row and row["net_p75"] is None


def test_the_row_count_is_capped_and_the_omission_is_counted(ops_env):
    rows = [_row(key=str(i % 3 + 1), currency=c) for i in range(300)
            for c in ("USD", "JPY", "EUR", "GBP", "AUD")]
    out = _out(ops_env, _reaction(rows))
    assert len(out["bundles"]) == svc._ECON_REACTION_MAX
    assert out["bundles_omitted"] == len(rows) - svc._ECON_REACTION_MAX


def test_a_broken_reaction_section_leaves_the_other_sections(ops_env):
    out = _status(ops_env, {"econ_signal": _econ(reaction="junk")})["econ_signal"]
    assert out["reaction"] is None
    assert out["decisions"]["total"] == 5


def test_status_without_a_reaction_section_has_no_reaction_key(ops_env):
    assert "reaction" not in _status(ops_env, {"econ_signal": _econ()})["econ_signal"]
