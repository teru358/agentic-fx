"""enroll 中の afx 起動抑止に使う durable marker の置き場と、afx 側の判定。

ops_setup (書く側) と afx の起動経路 (読む側) が同じ path 定義を共有するため、本体側の
軽い module に置く。ops_setup が daemon 全体を import しないで済むようにする。
"""
from __future__ import annotations

import os
from pathlib import Path

ENROLL_IN_PROGRESS = "enroll_in_progress"
# systemd の RestartPreventExitStatus で即終了を再起動対象から外すための専用値。
# 既存の終了コード (0 / 1 / 未初期化の 2) と衝突しない。
EXIT_ENROLL_IN_PROGRESS = 75


class EnrollInProgress(Exception):
    """enable が鍵の enroll 中で、afx は manifest を読まずに終了すべき。"""


def enroll_marker_path(root: Path) -> Path:
    # ops_setup の stage file と同じ dir に置く。
    return Path(root) / "data" / "ops" / ENROLL_IN_PROGRESS


def enroll_marker_present(root: Path) -> bool:
    # 中身が壊れていても在れば止まる側に倒す (symlink も在るとみなす)。
    return os.path.lexists(enroll_marker_path(root))
