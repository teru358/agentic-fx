"""server 認証 verifier の入口。鍵を送る前に接続先が本物の afx か確かめる。

入口は ``connect_verified`` だけ。検査に通らなければ ``ServerAuthError`` を投げ、
呼び出し側は鍵を一切送らない。reason は本体 client の ``ServerAuthError.reason`` と
同じ固定語に揃える。

現状は実装が無いので常に ``peer_check`` で拒否する (未実装を「許可」にしないため)。
"""
from __future__ import annotations

import socket
from pathlib import Path

REASONS = ("socket_dir", "socket_file", "peer_uid", "peer_gone", "peer_check",
           "not_lock_holder", "holder_gone")


class ServerAuthError(Exception):
    """接続先を確かめられない。``reason`` は固定語。"""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def connect_verified(socket_path: Path, *, lock_path: Path, uid: int) -> socket.socket:
    """socket へ接続し、相手が同 uid で instance lock を持つ生きた process と確かめて返す。

    確かめられなければ ``ServerAuthError`` (接続済みの socket は閉じる)。
    戻り値の socket へは、この関数が返った後でだけ鍵を書いてよい。
    """
    raise ServerAuthError("peer_check")
