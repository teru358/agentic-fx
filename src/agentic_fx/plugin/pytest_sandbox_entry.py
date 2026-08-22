"""test_plugin.py 実行専用のサブプロセスエントリ (プラン 8 B 束)。

<!-- precheck 2026-08-22: T6 検収是正 m1 --> 旧 `approval.py` の
`_default_pytest_runner` が spawn していたエントリ (裁定2 で削除済み、
本番経路は Task 6 の `gate_pytest_worker.py` 経由の Landlock ゲートに
置き替わっている)。本モジュール自体は削除しない — `_poison_network_
modules()` を pytest 起動前に適用する順序保証のテスト
(`test_pytest_sandbox_entry_calls_poison_before_pytest`) が引き続き
参照する。`python -m pytest` を直接 spawn するのをやめてこのモジュール
を経由させる理由: pytest がテストモジュール (test_plugin.py 経由で
plugin.py) を import する**前**にネットワーク毒入れを適用するため。
resource limit (RLIMIT_AS/NOFILE/FSIZE) は起動側の `preexec_fn` が
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
