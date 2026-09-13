"""[profitability-floor] T1 Step 1-1 (2026-09-12): `_check_profitability_
floor` の判定規則 pin (設計書 §3「判定規則 (逐語)」)。

SETTINGS (`config/settings.yaml.example` 既定) は
`min_pf=1.0` / `require_positive_avg_r=True` / `require_holdout_evaluable=False`
(T3 で追加済み)。個別ケースは `SETTINGS.model_copy(update=...)` で閾値を
上書きする。
"""
from __future__ import annotations

import pytest

from agentic_fx.plugin.strategy_gate import _check_profitability_floor

from tests.plugin.conftest import SETTINGS


def _m(trades, pf, avg_r, *, evaluable=True):
    return {"trades": trades, "pf": pf, "avg_r": avg_r, "evaluable": evaluable}


# ---- F1: in_sample 段の境界 --------------------------------------------

def test_f1_pf_below_min_pf_fails():
    label, detail = _check_profitability_floor(
        {"USDJPY": _m(50, 0.99, 0.1)}, settings=SETTINGS, scope="in_sample")
    assert label == "unprofitable"
    assert "USDJPY" in detail


def test_f1_pf_equal_to_min_pf_passes():
    """`pf == min_pf` は PASS (`<` であって `<=` ではない)。"""
    label, detail = _check_profitability_floor(
        {"USDJPY": _m(50, 1.0, 0.1)}, settings=SETTINGS, scope="in_sample")
    assert label == ""
    assert detail == ""


def test_f1_pf_none_passes_gross_loss_zero():
    """`pf is None` (gross_loss==0) は PASS。素で `pf < min_pf` を書くと
    `TypeError` になる境界。"""
    label, _ = _check_profitability_floor(
        {"USDJPY": _m(50, None, 0.1)}, settings=SETTINGS, scope="in_sample")
    assert label == ""


def test_f1_avg_r_zero_fails_when_required():
    """`avg_r == 0.0` は FAIL (`<=`)。"""
    label, detail = _check_profitability_floor(
        {"USDJPY": _m(50, 1.5, 0.0)}, settings=SETTINGS, scope="in_sample")
    assert label == "unprofitable"
    assert "avg_r" in detail


def test_f1_4_trades_positive_pf_none_avg_r_positive_passes():
    """F1-4: `trades>0, pf=None, avg_r>0` は PASS。"""
    label, _ = _check_profitability_floor(
        {"USDJPY": _m(50, None, 0.05)}, settings=SETTINGS, scope="in_sample")
    assert label == ""


def test_f1_avg_r_not_required_when_require_positive_avg_r_false():
    settings = SETTINGS.model_copy(deep=True)
    settings.improve.gate.require_positive_avg_r = False
    label, _ = _check_profitability_floor(
        {"USDJPY": _m(50, 1.5, -0.2)}, settings=settings, scope="in_sample")
    assert label == ""


def test_f1_zero_trades_pair_is_skipped():
    label, _ = _check_profitability_floor(
        {"USDJPY": _m(0, None, None)}, settings=SETTINGS, scope="in_sample")
    assert label == ""


def test_f1_max_drawdown_and_kill_switch_latches_not_in_metrics_dict_still_passes():
    """判定式には `max_drawdown`/`kill_switch_latches` を一切見ない —
    metrics dict に含まれていなくても判定は動く (F10 系と対)。"""
    label, _ = _check_profitability_floor(
        {"USDJPY": {"trades": 50, "pf": 1.5, "avg_r": 0.1, "evaluable": True}},
        settings=SETTINGS, scope="in_sample")
    assert label == ""


# ---- F2: holdout 段 -----------------------------------------------------

def test_f2_4_holdout_not_evaluable_passes_by_default():
    """既定 `require_holdout_evaluable=False`: 標本不足は「悪いとは
    言わない」(R8) — evaluable=False でも通す。"""
    label, _ = _check_profitability_floor(
        {"USDJPY": _m(7, None, None, evaluable=False)},
        settings=SETTINGS, scope="holdout")
    assert label == ""


def test_f2_5a_strict_holdout_evaluable_fails_with_nonzero_trades():
    settings = SETTINGS.model_copy(deep=True)
    settings.improve.gate.require_holdout_evaluable = True
    label, detail = _check_profitability_floor(
        {"USDJPY": _m(7, None, None, evaluable=False)},
        settings=settings, scope="holdout")
    assert label == "unprofitable"
    assert "holdout_not_evaluable" in detail


def test_f2_5b_strict_holdout_evaluable_fails_even_with_zero_trades():
    """codex I9 の契約: ① (strict holdout 判定) は zero-trade shortcut
    (②) より前 — trades==0 でも FAIL する。この判定を②の後に置く変異は
    段0 変異で殺す対象 (下記 mutation テストで確認)。"""
    settings = SETTINGS.model_copy(deep=True)
    settings.improve.gate.require_holdout_evaluable = True
    label, detail = _check_profitability_floor(
        {"USDJPY": _m(0, None, None, evaluable=False)},
        settings=settings, scope="holdout")
    assert label == "unprofitable"
    assert "holdout_not_evaluable" in detail


def test_f2_7_label_is_identical_string_across_scopes():
    label_in_sample, _ = _check_profitability_floor(
        {"USDJPY": _m(50, 0.5, 0.1)}, settings=SETTINGS, scope="in_sample")
    label_holdout, _ = _check_profitability_floor(
        {"USDJPY": _m(50, 0.5, 0.1)}, settings=SETTINGS, scope="holdout")
    assert label_in_sample == label_holdout == "unprofitable"


# ---- F3: 複数 pair -------------------------------------------------------

def test_f3_1_one_failing_pair_fails_whole_candidate():
    label, detail = _check_profitability_floor(
        {"USDJPY": _m(50, 1.5, 0.1), "EURUSD": _m(50, 0.5, 0.1)},
        settings=SETTINGS, scope="in_sample")
    assert label == "unprofitable"
    assert "EURUSD" in detail
    assert "USDJPY" not in detail


def test_f3_2_all_pairs_passing_passes():
    label, _ = _check_profitability_floor(
        {"USDJPY": _m(50, 1.5, 0.1), "EURUSD": _m(50, 1.2, 0.2)},
        settings=SETTINGS, scope="in_sample")
    assert label == ""


# ---- F10: DD / kill_switch_latches は判定に無関係 -----------------------

def test_f10_1_extreme_drawdown_and_latches_do_not_change_verdict():
    m_extreme = {"trades": 50, "pf": 1.5, "avg_r": 0.1, "evaluable": True,
                 "max_drawdown": 0.99, "kill_switch_latches": 999}
    m_zero = {"trades": 50, "pf": 1.5, "avg_r": 0.1, "evaluable": True,
              "max_drawdown": 0.0, "kill_switch_latches": 0}
    label_extreme, _ = _check_profitability_floor(
        {"USDJPY": m_extreme}, settings=SETTINGS, scope="in_sample")
    label_zero, _ = _check_profitability_floor(
        {"USDJPY": m_zero}, settings=SETTINGS, scope="in_sample")
    assert label_extreme == label_zero == ""


# ---- F 番号 gap 充足 (2026-09-13、コーディネータ指示): F1-2/F1-5/F1-7 --

def test_f1_2_pf_just_below_min_pf_fails_with_exact_label():
    label, _ = _check_profitability_floor(
        {"USDJPY": _m(50, 1.0 - 1e-9, 0.1)}, settings=SETTINGS,
        scope="in_sample")
    assert label == "unprofitable"  # 固定文言、完全一致


def test_f1_5_pf_pass_but_avg_r_fail_independent_gates():
    """F1-5: `pf==1.5` (合格) かつ `avg_r==-0.01` (不合格) → 2 門は独立
    なので FAIL。変異: avg_r 検査を落とす → killer (下記逆変異で確認)。"""
    label, detail = _check_profitability_floor(
        {"USDJPY": _m(50, 1.5, -0.01)}, settings=SETTINGS, scope="in_sample")
    assert label == "unprofitable"
    assert "avg_r" in detail


def test_f1_7_min_pf_zero_lets_low_pf_pass():
    settings = SETTINGS.model_copy(deep=True)
    settings.improve.gate.min_pf = 0.0
    label, _ = _check_profitability_floor(
        {"USDJPY": _m(50, 0.5, 0.1)}, settings=settings, scope="in_sample")
    assert label == ""


# ---- F2-1/F2-2/F2-3/F2-6 ------------------------------------------------

def test_f2_2_holdout_pf_equal_min_pf_passes_and_below_fails():
    label_pass, _ = _check_profitability_floor(
        {"USDJPY": _m(50, 1.0, 0.1, evaluable=True)}, settings=SETTINGS,
        scope="holdout")
    label_fail, _ = _check_profitability_floor(
        {"USDJPY": _m(50, 1.0 - 1e-9, 0.1, evaluable=True)},
        settings=SETTINGS, scope="holdout")
    assert label_pass == ""
    assert label_fail == "unprofitable"


def test_f2_3_holdout_evaluable_true_pf_pass_avg_r_fail():
    label, detail = _check_profitability_floor(
        {"USDJPY": _m(50, 1.2, -0.01, evaluable=True)}, settings=SETTINGS,
        scope="holdout")
    assert label == "unprofitable"
    assert "avg_r" in detail
