"""CONNECT 要求の incremental parser と、1 接続から要求を読み切る関数。

parser は bytes の上の純粋な状態機械で、与え方 (1 byte ずつ / 一括) によらず
同じ結果になる。判定は「行が完結した時点」で行い、完結前に見るのは行長だけ。
authority の許可集合との照合や DNS 解決はここでは行わない。
"""

from __future__ import annotations

import enum
import socket
import time
from dataclasses import dataclass

MAX_LINE_BYTES = 1024  # CRLF を含む 1 行の上限
MAX_HEADER_LINES = 32  # Host を含む header 行数の上限
MAX_HEADER_BYTES = 8 * 1024  # header 行 (CRLF 込み、request line と終端の空行は除く) の合計
REQUEST_DEADLINE_SECONDS = 5.0  # accept から DONE までの期限
ALLOWED_PORT = "443"
MAX_HOSTNAME_BYTES = 253
MAX_LABEL_BYTES = 63
RECV_BYTES = 4096

_METHOD = b"CONNECT"
_VERSION = b"HTTP/1.1"
_LABEL_CHARS = frozenset(b"abcdefghijklmnopqrstuvwxyz0123456789-")
_DIGITS = frozenset(b"0123456789")
# RFC 9110 の token 文字
_TOKEN_CHARS = frozenset(
    b"!#$%&'*+-.^_`|~0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
)


class State(enum.Enum):
    REQ_LINE = "req_line"
    HEADERS = "headers"
    DONE = "done"
    REJECTED = "rejected"


class Reject(enum.Enum):
    LINE_TOO_LONG = "line_too_long"
    BARE_LF = "bare_lf"
    NUL_BYTE = "nul_byte"
    CONTROL_CHAR = "control_char"
    BAD_REQUEST_LINE = "bad_request_line"
    BAD_METHOD = "bad_method"
    BAD_VERSION = "bad_version"
    BAD_AUTHORITY = "bad_authority"
    USER_INFO = "user_info"
    IP_LITERAL = "ip_literal"
    NON_ASCII = "non_ascii"
    IDN_LABEL = "idn_label"
    BAD_PORT = "bad_port"
    OBS_FOLD = "obs_fold"
    BAD_HEADER = "bad_header"
    TOO_MANY_HEADERS = "too_many_headers"
    HEADERS_TOO_LARGE = "headers_too_large"
    DUPLICATE_HOST = "duplicate_host"
    DUPLICATE_PROXY_AUTHORIZATION = "duplicate_proxy_authorization"
    HOST_MISSING = "host_missing"
    HOST_MISMATCH = "host_mismatch"


def _check_authority(authority: bytes) -> tuple[str | None, Reject | None]:
    """authority を検査して (hostname, None) か (None, 拒否理由) を返す。"""
    if any(b > 0x7F for b in authority):
        return None, Reject.NON_ASCII
    if b"@" in authority:
        return None, Reject.USER_INFO
    if authority.startswith(b"["):
        return None, Reject.IP_LITERAL
    host, sep, port = authority.rpartition(b":")
    if not sep:
        return None, Reject.BAD_PORT
    if port != ALLOWED_PORT.encode():
        return None, Reject.BAD_PORT
    if not host:
        return None, Reject.BAD_AUTHORITY
    if b":" in host:
        return None, Reject.BAD_AUTHORITY
    if len(host) > MAX_HOSTNAME_BYTES:
        return None, Reject.BAD_AUTHORITY
    labels = host.split(b".")
    for label in labels:
        if not label or len(label) > MAX_LABEL_BYTES:
            return None, Reject.BAD_AUTHORITY
        if not _LABEL_CHARS.issuperset(label):
            return None, Reject.BAD_AUTHORITY
        if label[:1] == b"-" or label[-1:] == b"-":
            return None, Reject.BAD_AUTHORITY
        if label.startswith(b"xn--"):
            return None, Reject.IDN_LABEL
    # 末尾 label が数字だけの名前は dotted IPv4 (や数値 host) と区別できないので落とす
    if _DIGITS.issuperset(labels[-1]):
        return None, Reject.IP_LITERAL
    return host.decode("ascii"), None


class ConnectRequestParser:
    def __init__(self) -> None:
        self.state = State.REQ_LINE
        self.reject_reason: Reject | None = None
        self.authority: str | None = None
        self.hostname: str | None = None
        self.preread = b""
        self._buf = b""
        self._header_lines = 0
        self._header_bytes = 0
        self._host_seen = False
        self._proxy_auth_seen = False

    def feed(self, data: bytes) -> State:
        if self.state is State.DONE:
            raise ValueError("feed after DONE")
        if self.state is State.REJECTED:
            return self.state
        self._buf += data
        while self.state in (State.REQ_LINE, State.HEADERS):
            idx = self._buf.find(b"\n")
            if idx < 0:
                if len(self._buf) >= MAX_LINE_BYTES:
                    # LF が付けば必ず上限を超える
                    self._reject(Reject.LINE_TOO_LONG)
                break
            raw_len = idx + 1
            if raw_len > MAX_LINE_BYTES:
                self._reject(Reject.LINE_TOO_LONG)
                break
            if idx == 0 or self._buf[idx - 1] != 0x0D:
                self._reject(Reject.BARE_LF)
                break
            line = self._buf[: idx - 1]
            self._buf = self._buf[idx + 1 :]
            if self.state is State.REQ_LINE:
                self._on_request_line(line)
            else:
                self._on_header_line(line, raw_len)
        if self.state is State.HEADERS and self._header_lines < MAX_HEADER_LINES:
            # 未終端の行も 8 KiB の合計に入れ、CRLF を待たずに超過を拒否する。
            # 終端の空行の先頭 CR だけは header に数えない
            pending = 0 if self._buf == b"\r" else len(self._buf)
            if self._header_bytes + pending > MAX_HEADER_BYTES:
                self._reject(Reject.HEADERS_TOO_LARGE)
        if self.state is State.DONE:
            self.preread = self._buf
            self._buf = b""
        elif self.state is State.REJECTED:
            self._buf = b""
        return self.state

    def _reject(self, reason: Reject) -> None:
        self.state = State.REJECTED
        self.reject_reason = reason

    @staticmethod
    def _bad_bytes(line: bytes, allow_tab: bool) -> Reject | None:
        if b"\x00" in line:
            return Reject.NUL_BYTE
        for b in line:
            if (b < 0x20 and not (allow_tab and b == 0x09)) or b == 0x7F:
                return Reject.CONTROL_CHAR
        return None

    def _on_request_line(self, line: bytes) -> None:
        bad = self._bad_bytes(line, allow_tab=False)
        if bad:
            return self._reject(bad)
        parts = line.split(b" ")
        if len(parts) != 3 or not all(parts):
            return self._reject(Reject.BAD_REQUEST_LINE)
        method, authority, version = parts
        if method != _METHOD:
            return self._reject(Reject.BAD_METHOD)
        if version != _VERSION:
            return self._reject(Reject.BAD_VERSION)
        hostname, reason = _check_authority(authority)
        if reason:
            return self._reject(reason)
        self.authority = authority.decode("ascii")
        self.hostname = hostname
        self.state = State.HEADERS

    def _on_header_line(self, line: bytes, raw_len: int) -> None:
        if not line:
            if not self._host_seen:
                return self._reject(Reject.HOST_MISSING)
            self.state = State.DONE
            return
        self._header_lines += 1
        self._header_bytes += raw_len
        if self._header_lines > MAX_HEADER_LINES:
            return self._reject(Reject.TOO_MANY_HEADERS)
        if self._header_bytes > MAX_HEADER_BYTES:
            return self._reject(Reject.HEADERS_TOO_LARGE)
        if line[0] in (0x20, 0x09):
            return self._reject(Reject.OBS_FOLD)
        bad = self._bad_bytes(line, allow_tab=True)
        if bad:
            return self._reject(bad)
        name, sep, value = line.partition(b":")
        if not sep or not name or not _TOKEN_CHARS.issuperset(name):
            return self._reject(Reject.BAD_HEADER)
        lowered = name.lower()
        if lowered == b"host":
            if self._host_seen:
                return self._reject(Reject.DUPLICATE_HOST)
            self._host_seen = True
            # 空白は区切りの SP 1 個までしか許さず、前後の余分な空白は不一致にする
            if value[:1] == b" ":
                value = value[1:]
            if value.decode("ascii", "replace") != self.authority:
                return self._reject(Reject.HOST_MISMATCH)
        elif lowered == b"proxy-authorization":
            if self._proxy_auth_seen:
                return self._reject(Reject.DUPLICATE_PROXY_AUTHORIZATION)
            self._proxy_auth_seen = True


class Outcome(enum.Enum):
    DONE = "done"
    REJECTED = "rejected"
    TIMEOUT = "timeout"
    EOF = "eof"


@dataclass(frozen=True)
class ReadResult:
    outcome: Outcome
    parser: ConnectRequestParser


def read_connect_request(
    sock: socket.socket,
    *,
    accepted_at: float,
    deadline_seconds: float = REQUEST_DEADLINE_SECONDS,
) -> ReadResult:
    """accept 直後の socket から CONNECT 要求を 1 つ読み切る。

    DONE 以外 (拒否・期限切れ・EOF) は何も書かずに socket を閉じて戻る。
    DONE では閉じず、同じ recv で届いた後続 bytes を parser.preread に残す。
    DONE で返すときは socket の timeout を呼出前の値に戻す。
    recv 中の OSError は ECONNRESET を含めすべて EOF として扱い、区別しない。
    accepted_at は accept 直後に取った time.monotonic() の値で、必須。
    """
    # 期限の起点は accept 時刻に固定する。呼び出し時刻で代用すると、
    # accept から呼ぶまでの処理時間だけ相手に猶予を与えてしまう
    if isinstance(accepted_at, bool) or not isinstance(accepted_at, (int, float)):
        raise TypeError("accepted_at must be a time.monotonic() value")
    deadline = accepted_at + deadline_seconds
    parser = ConnectRequestParser()
    outcome = Outcome.TIMEOUT
    original_timeout = sock.gettimeout()
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                outcome = Outcome.TIMEOUT
                break
            sock.settimeout(remaining)
            try:
                chunk = sock.recv(RECV_BYTES)
            except socket.timeout:
                outcome = Outcome.TIMEOUT
                break
            except OSError:
                outcome = Outcome.EOF
                break
            if not chunk:
                outcome = Outcome.EOF
                break
            state = parser.feed(chunk)
            if state is State.DONE:
                outcome = Outcome.DONE
                break
            if state is State.REJECTED:
                outcome = Outcome.REJECTED
                break
    finally:
        if outcome is Outcome.DONE:
            # 残量値のまま返すと、後続の送信・中継で不意に timeout が出る
            sock.settimeout(original_timeout)
        else:
            try:
                sock.close()
            except OSError:
                pass
    return ReadResult(outcome, parser)
