"""notify の結合試験の装置: 試験 CA、loopback TLS の fake webhook、UDP DNS、events daemon。

試験 CA と鍵は試験ごとに openssl で作り、リポジトリには置かない。webhook は受けた接続
ごとに SNI・接続先 address・要求・本文・返した status を記録するが、記録を試験の失敗文に
出すときも URL や secret は含めない (受信した要求の path は試験側で伏せる)。
"""
from __future__ import annotations

import json
import shutil
import socket
import ssl
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from _notify_seams import dns_answer, parse_query

HERE = Path(__file__).resolve().parent
EVENTS_DAEMON = HERE / "_events_daemon.py"
HOST = "hook.afx-test"

needs_openssl = pytest.mark.skipif(shutil.which("openssl") is None, reason="openssl が無い")


def _openssl(*args: str, cwd: Path) -> None:
    subprocess.run(["openssl", *args], cwd=cwd, check=True, capture_output=True, timeout=60)


@dataclass
class Pki:
    """試験 CA (信頼させる側) と、別 CA (信頼させない側) と、その leaf 群。"""
    base: Path
    ca_pem: str

    def leaf(self, name: str) -> tuple[Path, Path]:
        return self.base / f"{name}.crt", self.base / f"{name}.key"


def make_pki(base: Path) -> Pki:
    base.mkdir(parents=True, exist_ok=True)
    for ca in ("ca", "rogue-ca"):
        _openssl("req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256",
                 "-nodes", "-keyout", f"{ca}.key", "-out", f"{ca}.crt", "-days", "2",
                 "-subj", f"/CN=afx notify {ca}", "-addext", "basicConstraints=critical,CA:TRUE",
                 "-addext", "keyUsage=critical,keyCertSign,cRLSign", cwd=base)
    # (名前, 署名する CA, SAN)
    leaves = (("good", "ca", f"DNS:{HOST},DNS:localhost"),
              ("other-name", "ca", "DNS:other.afx-test"),
              ("rogue", "rogue-ca", f"DNS:{HOST},DNS:localhost"))
    for name, ca, san in leaves:
        _openssl("req", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256", "-nodes",
                 "-keyout", f"{name}.key", "-out", f"{name}.csr", "-subj", f"/CN={name}",
                 cwd=base)
        (base / f"{name}.ext").write_text(
            f"subjectAltName={san}\nextendedKeyUsage=serverAuth\nbasicConstraints=CA:FALSE\n")
        _openssl("x509", "-req", "-in", f"{name}.csr", "-CA", f"{ca}.crt", "-CAkey", f"{ca}.key",
                 "-CAcreateserial", "-out", f"{name}.crt", "-days", "2", "-extfile",
                 f"{name}.ext", cwd=base)
    return Pki(base, (base / "ca.crt").read_text(encoding="ascii"))


@dataclass
class Received:
    """webhook が受けた接続 1 本。handshake に失敗した接続は request が None。"""
    local: str
    sni: str | None = None
    request: str | None = None
    headers: dict = field(default_factory=dict)
    body: bytes | None = None
    status: int | None = None

    def json(self) -> dict:
        return json.loads(self.body)


class Webhook:
    """loopback の TLS server。statuses の順に応答し、尽きたら 200。

    status が bytes ならその bytes をそのまま返して閉じる (壊れた応答用)。
    status が "hang" の要求には応答せず hang 秒待って閉じる。"trickle" は応答を 1 byte ずつ
    0.3 秒おきに hang 秒の間返し続ける (読みの待ちを切らさず終わらない相手)。3xx には Location を付ける。
    """

    def __init__(self, cert: Path, key: Path, *, addresses=("127.0.0.1",),
                 statuses=(), hang: float = 3.0, retry_after: str | None = None) -> None:
        self.context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.context.load_cert_chain(cert, key)
        self.context.sni_callback = self._sni
        self.statuses = list(statuses)
        self.hang = hang
        self.retry_after = retry_after
        self.connections: list[Received] = []
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.listeners = []
        port = 0
        for address in addresses:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((address, port))
            port = sock.getsockname()[1]
            sock.listen(16)
            sock.settimeout(0.1)
            self.listeners.append(sock)
        self.port = port
        self._snis: dict[int, str | None] = {}
        self.threads = [threading.Thread(target=self._accept, args=(s,), daemon=True)
                        for s in self.listeners]
        for thread in self.threads:
            thread.start()

    def _sni(self, ssl_sock, server_name, _context) -> None:
        self._snis[id(ssl_sock)] = server_name

    def _accept(self, listener: socket.socket) -> None:
        while not self.stop.is_set():
            try:
                conn, _ = listener.accept()
            except (TimeoutError, OSError):
                continue
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _next_status(self):
        with self.lock:
            return self.statuses.pop(0) if self.statuses else 200

    def _handle(self, raw: socket.socket) -> None:
        record = Received(local=raw.getsockname()[0])
        with self.lock:
            self.connections.append(record)
        raw.settimeout(10)
        try:
            conn = self.context.wrap_socket(raw, server_side=True)
        except (ssl.SSLError, OSError):
            raw.close()
            return
        try:
            record.sni = self._snis.pop(id(conn), None)
            data = b""
            while b"\r\n\r\n" not in data:
                chunk = conn.recv(65536)
                if not chunk:
                    return
                data += chunk
            head, _, rest = data.partition(b"\r\n\r\n")
            lines = head.decode("latin-1").split("\r\n")
            headers = {k.strip().lower(): v.strip()
                       for k, _, v in (line.partition(":") for line in lines[1:])}
            length = int(headers.get("content-length", "0"))
            while len(rest) < length:
                chunk = conn.recv(65536)
                if not chunk:
                    break
                rest += chunk
            record.request, record.headers, record.body = lines[0], headers, rest[:length]
            status = self._next_status()
            if status == "hang":
                time.sleep(self.hang)
                return
            if status == "trickle":
                end = time.monotonic() + self.hang
                for byte in b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n":
                    if time.monotonic() >= end:
                        break
                    conn.sendall(bytes([byte]))
                    time.sleep(0.3)
                return
            if isinstance(status, bytes):
                # HTTP として壊れた応答をそのまま返す
                conn.sendall(status)
                return
            record.status = status
            extra = (f"Location: https://{HOST}/elsewhere\r\n" if 300 <= status < 400 else "")
            if status == 429 and self.retry_after is not None:
                extra += f"Retry-After: {self.retry_after}\r\n"
            # 204 / 304 は本文を持てない
            body = "" if status in (204, 304) else "ok"
            conn.sendall(f"HTTP/1.1 {status} X\r\n{extra}Content-Length: {len(body)}\r\n"
                         f"Connection: close\r\n\r\n{body}".encode("ascii"))
        except (ssl.SSLError, OSError):
            pass
        finally:
            conn.close()

    @property
    def requests(self) -> list[Received]:
        with self.lock:
            return [c for c in self.connections if c.request is not None]

    def close(self) -> None:
        self.stop.set()
        for thread in self.threads:
            thread.join(5)
        for sock in self.listeners:
            sock.close()


class Dns:
    """UDP の DNS server。answers の順に A を返し (最後の値を繰り返す)、問合せ名を記録する。"""

    def __init__(self, answers: list[str | None]) -> None:
        self.answers = list(answers)
        self.queries: list[str] = []
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.settimeout(0.1)
        self.port = self.sock.getsockname()[1]
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self) -> None:
        while not self.stop.is_set():
            try:
                packet, peer = self.sock.recvfrom(512)
            except (TimeoutError, OSError):
                continue
            _id, name, _q = parse_query(packet)
            self.queries.append(name)
            answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
            self.sock.sendto(dns_answer(packet, answer if name == HOST else None), peer)

    def close(self) -> None:
        self.stop.set()
        self.thread.join(5)
        self.sock.close()


class EventsDaemon:
    """`_events_daemon.py` を起こし、script を書き換えられるようにする。"""

    def __init__(self, socket_path: Path, data_dir: Path, work: Path) -> None:
        self.script = work / "events-script.json"
        self.record = work / "events-record.jsonl"
        self.set([])
        self.proc = subprocess.Popen([sys.executable, "-I", str(EVENTS_DAEMON), str(socket_path),
                                      str(data_dir), str(self.script), str(self.record)],
                                     stdout=subprocess.PIPE, text=True)
        word, _pid = self.proc.stdout.readline().split()
        assert word == "ready"

    def set(self, events: list[dict], *, high_watermark: int | None = None,
            ignore_after: bool = False) -> None:
        tmp = self.script.with_suffix(".tmp")
        tmp.write_text(json.dumps({"events": events, "high_watermark": high_watermark,
                                   "ignore_after": ignore_after}))
        tmp.replace(self.script)

    def requests(self) -> list[dict]:
        if not self.record.exists():
            return []
        return [json.loads(line) for line in self.record.read_text().splitlines()]

    def close(self) -> None:
        if self.proc.poll() is None:
            self.proc.kill()
        self.proc.wait(10)
        self.proc.stdout.close()


def event(event_id: int) -> dict:
    return {"id": event_id, "ts": "2026-10-11T00:00:00+00:00",
            "code": "job_state_changed", "ref": "job:" + "0123456789abcdef" * 2,
            "fields": {"job_kind": "ask", "state": "done", "result_code": "ok"}}


def expected_body(event_id: int) -> dict:
    return {"event_id": event_id, "ts": "2026-10-11T00:00:00+00:00", "code": "job_state_changed",
            "ref": "job:" + "0123456789abcdef" * 2, "job_kind": "ask", "state": "done",
            "result_code": "ok"}
