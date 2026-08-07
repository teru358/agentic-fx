"""test_plugin.py 実行専用のサブプロセスエントリ (プラン 8 B 束)。

approval.py の `_default_pytest_runner` が spawn する唯一の想定呼び出し元。
`python -m pytest` を直接 spawn するのをやめてこのモジュールを経由させる
理由: pytest がテストモジュール (test_plugin.py 経由で plugin.py) を
import する**前**にネットワーク毒入れを適用するため。resource limit
(RLIMIT_AS/NOFILE/FSIZE) は起動側 (approval.py の `preexec_fn`) が
fork 直後・exec 直前に設定済みの前提で、ここでは毒入れと pytest 起動のみ
行う。
"""
from __future__ import annotations

import sys


def main() -> None:
    from agentic_fx.plugin.worker import _poison_network_modules
    _poison_network_modules()

    import pytest

    args = ["-p", "no:cacheprovider", "--noconftest", "-q", *sys.argv[1:]]
    raise SystemExit(pytest.main(args))


if __name__ == "__main__":
    main()
