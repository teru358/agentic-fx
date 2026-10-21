"""実運用で承認まで進んだ候補 (SMA クロス系 2 本、RSI + Bollinger 系 2 本) と
同梱 example の実ファイルから、ロジック hash と仮説族 key を固定する。

fixture は plugin.py と config.yaml を逐語で写したもの。名前は仮名にしてある
(名前は hash にも key にも入らない)。"""
from __future__ import annotations

from pathlib import Path

from agentic_fx.plugin.logic_identity import (
    LOGIC_HASH_PREFIX, identity_of_dir, logic_hash_of)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "candidate_quality"
EXAMPLES = Path(__file__).resolve().parents[2] / "docs" / "examples" / "plugins"

SMA_FAST = FIXTURES / "sma_cross_10_30"
SMA_SLOW = FIXTURES / "sma_cross_10_40"
RSI_BB_A = FIXTURES / "rsi_bb_reversion_30_60"
RSI_BB_B = FIXTURES / "rsi_bb_reversion_40_80"

# 実測値 (正規化規則を変えたら版を上げて、ここも更新する)
SMA_LOGIC_HASH = (
    "v1:02c2de9dbb8293bde200393eb9c36c2f12de30a004e7a32816afa47e2384af25")
RSI_BB_A_LOGIC_HASH = (
    "v1:8c5140f5963d245d72ef9ec31c30c6576e87224fda7da27a6767154536b13820")
RSI_BB_B_LOGIC_HASH = (
    "v1:d47ff3b71acc3d733a8d03b432fcb1f312060c7ee78af5df3c00c32713fc5b00")
SMA_CROSS_EXAMPLE_LOGIC_HASH = (
    "v1:6729025cab2ed15a11d75995840229aeae80f452e8a3a3792d1bf87fba12b4e8")


def test_ac5_rsi_bb_pair_has_different_logic_hash_and_same_family():
    hash_a, family_a = identity_of_dir(RSI_BB_A, name="rsi_bb_a")
    hash_b, family_b = identity_of_dir(RSI_BB_B, name="rsi_bb_b")
    assert hash_a == RSI_BB_A_LOGIC_HASH
    assert hash_b == RSI_BB_B_LOGIC_HASH
    assert hash_a != hash_b
    assert family_a == family_b == "strategy:bollinger,rsi@1h@USDJPY"


def test_ac6_sma_pair_shares_the_family():
    _, family_fast = identity_of_dir(SMA_FAST, name="sma_fast")
    _, family_slow = identity_of_dir(SMA_SLOW, name="sma_slow")
    assert family_fast == family_slow == "strategy:sma@1h@USDJPY"


def test_ac6_sma_pair_logic_hash_measured_value_is_pinned():
    """rationale と数値が違うだけの SMA クロス 2 本は、規則を適用すると
    ロジック hash も一致する (実測)。一致しなくても仮説族が捕まえる。"""
    hash_fast, _ = identity_of_dir(SMA_FAST, name="sma_fast")
    hash_slow, _ = identity_of_dir(SMA_SLOW, name="sma_slow")
    assert hash_fast == hash_slow == SMA_LOGIC_HASH


def test_ac7_numeric_docstring_and_rationale_only_differences_match():
    config = (SMA_FAST / "config.yaml").read_text(encoding="utf-8")
    source = (SMA_FAST / "plugin.py").read_text(encoding="utf-8")
    changed = (source.replace("30", "35").replace("0.01", "0.02")
               .replace("fast SMA crossed above slow SMA", "別の説明"))
    changed = '"""追加の docstring"""\n' + changed
    assert changed != source
    assert logic_hash_of(source, config) == logic_hash_of(changed, config)


def test_ac7_decisive_string_change_changes_the_hash():
    config = (SMA_FAST / "config.yaml").read_text(encoding="utf-8")
    source = (SMA_FAST / "plugin.py").read_text(encoding="utf-8")
    changed = source.replace('indicators["fast_sma"]', 'indicators["fast_ma"]')
    assert changed != source
    assert logic_hash_of(source, config) != logic_hash_of(changed, config)


def test_ac7_sma_cross_example_has_no_family_and_is_the_known_gap():
    """indicator を使わず自前計算する戦略は仮説族を持たない。構造を変えれば
    logic_hash も避けられる。この穴は塞がない (既知の未解決)。"""
    logic_hash, family = identity_of_dir(EXAMPLES / "sma_cross", name="sma_cross")
    assert family is None
    assert logic_hash == SMA_CROSS_EXAMPLE_LOGIC_HASH
    assert logic_hash.startswith(LOGIC_HASH_PREFIX)
    source = (EXAMPLES / "sma_cross" / "plugin.py").read_text(encoding="utf-8")
    config = (EXAMPLES / "sma_cross" / "config.yaml").read_text(encoding="utf-8")
    restructured = source.replace(
        "    prev_diff = prev_fast - prev_slow\n",
        "    prev_diff = prev_fast - prev_slow\n    _unused = prev_diff\n")
    assert restructured != source
    assert logic_hash_of(restructured, config) != logic_hash
