"""AC-7: 起動後の env・/proc/self/environ・worker env・log・activity に鍵文字列が無い。"""
from __future__ import annotations

import json
import os
import resource
import subprocess
import sys
from pathlib import Path

from agentic_fx.ops import keys
from agentic_fx.ops.contracts import Principal

CHILD = Path(__file__).with_name("_keyleak_child.py")


def test_ac7_keys_never_reach_env_argv_worker_env_log_or_activity(tmp_path):
    root = tmp_path / "root"
    (root / "data").mkdir(parents=True)
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "LANG")}
    env["HOME"] = os.environ["HOME"]
    result = subprocess.run([sys.executable, str(CHILD), str(root)], capture_output=True,
                            text=True, timeout=120, env=env,
                            preexec_fn=lambda: resource.setrlimit(resource.RLIMIT_CORE,
                                                                  (0, 0)))
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout.strip().splitlines()[-1])
    assert report["leaks"] == []
    # 正しい鍵の要求が実際に通っている (拒否だけを見て緑にしない)
    assert report["statuses"] == [200, 200, 200, 200, 401, 401]
    assert report["log_bytes"] > 0 and report["activity_bytes"] > 0
    tokens = [keys.read_token(keys.key_dir(root), p) for p in Principal]
    for token in tokens:
        assert token not in result.stdout and token not in result.stderr
