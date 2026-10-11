"""隔離の下で CLI 役として exec される probe (source を `python -I -c` で渡す)。

argv[1] は JSON の設定 (`spool`、`connect`、`hold`、`out`)。観測を 1 行の JSON で stdout に出し、
`out` があれば同じものを file にも書く (codex の `-o` と同じ受け渡し)。
"""
import errno
import json
import os
import socket
import sys
import time

cfg = json.loads(sys.argv[1])
r = {"pid": os.getpid(), "ppid": os.getppid()}
st = os.stat("/proc/self/exe")
r["exe"] = [st.st_dev, st.st_ino]
r["env"] = {k: os.environ.get(k) for k in (
    "HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "NO_PROXY", "no_proxy")
    if k in os.environ}
r["managed_env"] = sorted(k for k in os.environ if k.startswith("CODEX_MANAGED_"))

if cfg.get("spool"):
    from agentic_fx.tools import mcp_spool_client as C

    init = C.call(cfg["spool"], {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                 "params": {"protocolVersion": "2025-06-18"}}, timeout_sec=30)
    call = C.call(cfg["spool"], {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                                 "params": {"name": "list_staging", "arguments": {}}},
                  timeout_sec=30)
    r["spool"] = {"init": "result" in init, "call": "result" in call,
                  "call_error": call.get("error")}


def _connect(host, port, payload):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(5)
    try:
        s.connect((host, port))
    except OSError as e:
        return {"connect": errno.errorcode.get(e.errno, str(e.errno))}
    try:
        s.sendall(payload)
        got = b""
        while True:
            chunk = s.recv(4096)
            if not chunk:
                break
            got += chunk
        return {"connect": "ok", "reply": got[:64].decode("latin-1")}
    except OSError as e:
        return {"connect": "ok", "io": errno.errorcode.get(e.errno, str(e.errno))}
    finally:
        s.close()


if cfg.get("connect") == "proxy":
    url = os.environ.get("HTTPS_PROXY", "")
    port = int(url.rsplit(":", 1)[1]) if url.startswith("http://127.0.0.1:") else 0
    # 要求行を不正にして、proxy に名前解決も外部接続もさせない (handoff だけを通す)
    r["proxy"] = _connect("127.0.0.1", port, b"BROKEN\r\n\r\n") if port else {"connect": "no_env"}
elif cfg.get("connect") == "saturate":
    # 2 本を同時に: slot の飽和なら片方が EAGAIN、tunnel の上限なら片方が 503
    import threading
    url = os.environ.get("HTTPS_PROXY", "")
    port = int(url.rsplit(":", 1)[1])
    got = []

    def hold_one():
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(8)
        try:
            s.connect(("127.0.0.1", port))
            data = s.recv(64)
            got.append(data[:12].decode("latin-1") or "eof")
        except OSError as e:
            got.append(errno.errorcode.get(e.errno, str(e.errno)))
        finally:
            s.close()

    ts = [threading.Thread(target=hold_one) for _ in range(2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    r["saturate"] = sorted(got)
    if any(g in ("EAGAIN", "HTTP/1.1 503") for g in got):
        # 実 CLI と同じく、要求の失敗で異常終了する
        print(json.dumps({"probe": r}), flush=True)
        sys.exit(7)
elif isinstance(cfg.get("connect"), int):
    r["direct"] = _connect("127.0.0.1", cfg["connect"], b"x")

time.sleep(float(cfg.get("hold", 0)))
line = json.dumps({"probe": r})
if cfg.get("out"):
    with open(cfg["out"], "w") as f:
        f.write(line)
print(line, flush=True)
