"""AC-3: daemon の event 契約と afx-ops の allowlist・grammar が 2 つの package で一致する。

afx-ops は本体 package を import しない別 venv なので、ここ (本体側) で afx-ops の source を
子 process に読ませ、同じ入力に対する判定を突き合わせる。片方だけ足すと赤になる。
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from agentic_fx.ops import events as ev
from agentic_fx.ops import trade_events as te

AFX_OPS = Path(__file__).resolve().parents[2] / "afx-ops"

ORDER = {"state": "cancelled", "pair": "USDJPY", "side": "long", "entry_type": "limit",
         "qty": "0.1", "price": "150.123", "sl": "149.8", "tp": "150.9",
         "origin_code": "cron", "mode": "paper", "state_mode": "learning",
         "reason_code": "llm_cancel"}
CLOSE = {"pair": "USDJPY", "side": "long", "qty": "0.1", "entry_price": "150.123",
         "close_price": "150.9", "pnl": "-1234.50", "ccy": "JPY", "pnl_r": "-0.50",
         "held_sec": 3600, "reason_code": "sl", "origin_code": "signal:rsi_div",
         "mode": "paper", "state_mode": None}
BASE = {te.ORDER_CODE: ORDER, te.CLOSE_CODE: CLOSE}

VALUES = ["0.1", "0001.20", "0.10000000", "+1.5", "1.50", "0", "0.0", "1e3", "-1", "abc",
          "1" * 12, "1" * 13, "123456789012.12345678", "0.00000001", "0.000000001",
          "-0.00", "0.00", "-1234.50", "1234.5", "999999999999999.99", "1000000000000000.00",
          "999999.99", "1000000.00", "1e999", "nan", "inf", "", " 1", "1 ", "１",
          None, 0, 1, 3600, -1, 10 ** 10, 10 ** 10 - 1, True, 1.5, "JPY", "jpy", "JP", "USDJPY",
          "usdjpy", "US", "A" * 12, "A" * 13, "cron", "signal:x", "signal:", "Signal:x",
          "signal:" + "a" * 57, "signal:" + "a" * 58, "paper", "live", "demo", "learning",
          "trading", "long", "short", "buy", "market", "limit", "placed", "filled", "rejected",
          "cancelled", "expired", "sl", "tp", "llm_cancel", "Bad", "a" * 33, "x" * 32, "x y"]
REFS = ["order:1", "order:0", "order:9223372036854775807", "order:9223372036854775808",
        "order:", "order:-1", "order:00", "order:" + "9" * 19, "order:" + "9" * 20,
        "audit:1", "instance", "order:1\n"]

CHILD = r"""
import json, sys
sys.path.insert(0, sys.argv[1])
from afx_ops import trade_events as t
from afx_ops.notify import payload
case = json.load(sys.stdin)
out = {
  "fields": {c: sorted(f) for c, f in payload.ALLOWED_FIELDS.items()},
  "ref_codes": sorted(payload._REF),
  "refs": {r: t.valid_ref(r) for r in case["refs"]},
  "valid": [t.valid_fields(code, fields) for code, fields in case["fields"]],
  "ref_pattern": {r: all(payload._REF[c].fullmatch(r) is not None for c in t.TRADE_CODES)
                  for r in case["refs"]},
}
print(json.dumps(out))
"""


def _cases() -> list[tuple[str, dict]]:
    cases = []
    for code, base in BASE.items():
        cases.append((code, dict(base)))
        for name in base:
            for value in VALUES:
                cases.append((code, {**base, name: value}))
            cases.append((code, {k: v for k, v in base.items() if k != name}))
        cases.append((code, {**base, "extra": "x"}))
    cases.append((te.ORDER_CODE, {**ORDER, "state": "placed"}))
    return cases


def _afx_ops_view(cases):
    done = subprocess.run(
        [sys.executable, "-c", CHILD, str(AFX_OPS)], input=json.dumps(
            {"refs": REFS, "fields": [[c, f] for c, f in cases]}),
        capture_output=True, text=True, check=True, cwd=str(AFX_OPS.parent))
    return json.loads(done.stdout)


def test_ac3_field_sets_refs_and_value_grammar_agree_across_packages():
    cases = _cases()
    other = _afx_ops_view(cases)
    daemon_fields = {code: sorted(fields) for code, fields in ev.FIELDS.items()}
    for code, fields in daemon_fields.items():
        assert other["fields"][code] == fields
    assert set(daemon_fields) == set(other["fields"])
    assert set(ev._REF) == set(other["ref_codes"])
    for ref in REFS:
        mine = te.valid_ref(ref)
        assert other["refs"][ref] == mine, ref
        assert other["ref_pattern"][ref] == (te.REF_PATTERN.fullmatch(ref) is not None), ref
    mine = [te.valid_fields(code, fields) for code, fields in cases]
    assert other["valid"] == mine
    assert any(mine) and not all(mine)


def test_ac3_daemon_formatter_outputs_are_accepted_by_afx_ops():
    produced = []
    for value in (150.123, 149.8, 0.01, 1.12345, 110.001, 1.2345678912, 0.1 + 0.2, 1.0, 12345.6789):
        produced.append(te.format_price(value))
    pnls = [te.format_money(v) for v in (-1234.5, 0.0, -0.004, 7770.0, 12.345, -99999.999)]
    rs = [te.format_r(v) for v in (2.4, -0.001, 0.0, -1.0, 123.456)]
    cases = []
    for price in produced:
        cases.append((te.ORDER_CODE, {**ORDER, "price": price, "state": "placed",
                                      "reason_code": None}))
    for pnl in pnls:
        cases.append((te.CLOSE_CODE, {**CLOSE, "pnl": pnl}))
    for r in rs:
        cases.append((te.CLOSE_CODE, {**CLOSE, "pnl_r": r}))
    other = _afx_ops_view(cases)
    assert other["valid"] == [True] * len(cases)
