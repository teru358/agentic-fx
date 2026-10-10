"""試験用の鍵 dir を本体の manifest v2 と同じ形で組み立てる。"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

PRINCIPALS = ("operator", "approver", "ops-bot", "ops-web", "ops-notify")


def token_for(principal: str) -> str:
    return hashlib.sha256(f"test-token:{principal}".encode()).hexdigest()


def digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def write_ready(directory: Path, principals: dict, *, version: int = 2) -> None:
    path = directory / ".ready"
    path.write_text(json.dumps({"version": version, "principals": principals},
                               sort_keys=True, indent=1) + "\n")
    os.chmod(path, 0o600)


def build_key_dir(directory: Path, principals=PRINCIPALS) -> Path:
    """全 principal を active で持つ鍵 dir (0700、各 file 0600)。"""
    directory.mkdir(parents=True, mode=0o700)
    os.chmod(directory, 0o700)
    entries = {}
    for name in principals:
        path = directory / f"{name}.token"
        path.write_text(token_for(name) + "\n")
        os.chmod(path, 0o600)
        entries[name] = {"generation": 1, "state": "active", "sha256": digest(token_for(name))}
    write_ready(directory, entries)
    return directory
