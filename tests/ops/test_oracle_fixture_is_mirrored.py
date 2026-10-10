"""afx-ops 側に置いた server 認証 oracle の写しが、本体側の正と bytes 一致している。

afx-ops は独立配布なので本体の file を path 参照できず写しを持つ。写しが drift すると
本体 client と afx-ops verifier が別の oracle を通ることになるので、ここで一致を固定する。
"""
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
ORIGINAL = REPO / "tests" / "ops" / "fixtures" / "server_auth_oracle.json"
MIRROR = REPO / "afx-ops" / "tests" / "fixtures" / "server_auth_oracle.json"


def test_afx_ops_copy_of_server_auth_oracle_is_identical():
    assert MIRROR.is_file()
    assert MIRROR.read_bytes() == ORIGINAL.read_bytes()
