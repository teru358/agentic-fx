"""verifier だけを直接呼ぶ試験用 probe (固有の admission は通さない)。

usage: python -m afx_ops.server_auth_probe <socket_path> <lock_path> <uid> <token_file>

出力は 1 行。鍵を送って応答を得たら ``rc=0``、鍵を送らなかったら ``rc=3 reason=<固定語>``。
終了コードも同じ。応答を得られなかった場合は ``rc=4`` (鍵は送っている)。
"""
from __future__ import annotations

import sys
from pathlib import Path

from afx_ops.server_auth import ServerAuthError, connect_verified

RC_OK = 0
RC_REJECTED = 3
RC_NO_ANSWER = 4


def main(argv: list[str]) -> int:
    socket_path, lock_path, uid, token_file = argv
    try:
        sock = connect_verified(Path(socket_path), lock_path=Path(lock_path), uid=int(uid))
    except ServerAuthError as exc:
        print(f"rc={RC_REJECTED} reason={exc.reason}", flush=True)
        return RC_REJECTED
    with sock:
        token = Path(token_file).read_text(encoding="ascii").strip()
        sock.settimeout(5)
        sock.sendall(f"GET /whoami HTTP/1.1\r\nHost: afx\r\nAuthorization: Bearer {token}\r\n"
                     "Connection: close\r\n\r\n".encode("ascii"))
        data = b""
        try:
            while chunk := sock.recv(65536):
                data += chunk
        except OSError:
            pass
    if data.startswith(b"HTTP/1.1 200"):
        print(f"rc={RC_OK}", flush=True)
        return RC_OK
    print(f"rc={RC_NO_ANSWER}", flush=True)
    return RC_NO_ANSWER


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
