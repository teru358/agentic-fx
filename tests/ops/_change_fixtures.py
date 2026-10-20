"""変更内容の表示 test が使う、合成した plugin 版 dir の作成部品。"""
from __future__ import annotations

import os
import stat
from pathlib import Path

from agentic_fx.plugin import version_store

PLUGIN_PY = "def compute(df, params):\n    return {'v': params['period']}\n"
CONFIG = "kind: indicator\noutputs: [v]\nparams:\n  period: 14\n"
TEST_PY = "def test_x():\n    pass\n"


def hash_of(plugin_py: str | bytes, config: str | bytes, test: str | bytes) -> str:
    def b(v):
        return v if isinstance(v, bytes) else v.encode()
    return version_store.artifact_hash_bytes(b(plugin_py), b(config), b(test))


def write_files(directory: Path, plugin_py=PLUGIN_PY, config=CONFIG, test=TEST_PY) -> str:
    directory.mkdir(parents=True, exist_ok=True)
    for filename, body in (("plugin.py", plugin_py), ("config.yaml", config),
                           ("test_plugin.py", test)):
        data = body if isinstance(body, bytes) else body.encode()
        (directory / filename).write_bytes(data)
    return hash_of(plugin_py, config, test)


def make_version(plugins_root: Path, name: str, plugin_py=PLUGIN_PY, config=CONFIG,
                 test=TEST_PY) -> str:
    digest = hash_of(plugin_py, config, test)
    write_files(plugins_root / ".versions" / name / digest, plugin_py, config, test)
    return digest


def make_live(plugins_root: Path, name: str, digest: str) -> None:
    link = plugins_root / name
    if link.is_symlink() or link.exists():
        link.unlink()
    os.symlink(f".versions/{name}/{digest}", link)


def make_staging(plugins_root: Path, mission_id: int, name: str, **files) -> str:
    return write_files(plugins_root / "_staging" / str(mission_id) / name, **files)


def make_human(plugins_root: Path, name: str, **files) -> str:
    return write_files(plugins_root / "_human" / name, **files)


def make_archive(plugins_root: Path, mission_id: int, digest_files: dict | None = None,
                 **files) -> str:
    digest = hash_of(files.get("plugin_py", PLUGIN_PY), files.get("config", CONFIG),
                     files.get("test", TEST_PY))
    write_files(plugins_root / "_archive" / str(mission_id) / digest, **files)
    return digest


def is_fifo(path: Path) -> bool:
    return stat.S_ISFIFO(os.lstat(path).st_mode)


# 敵対 fixture: 各 secret は、どの表示面にも現れてはならない部分文字列。
SECRET_PATH = "/home/someone/private/area"
SECRET_HOST = "broker.internal-example.com"
SECRET_URL = "https://api.vendor-example.com/v1/orders?apikey=QQ"
SECRET_IP4 = "192.0.2.77"
SECRET_IP6 = "2001:db8::77"
SECRET_TOKEN = "ghp_Zx81QwErTyUiOpAsDfGhJkLzXcVbNm0123"
SECRET_HEX = "3f9a7c1b2d4e5f60718293a4b5c6d7e8f9012345aabbccdd"
SECRET_CRED = "hunter2xyz"
SECRET_ACCOUNT = "1234567890"
SECRET_ACCOUNT_GROUPED = "0012-345-6789012"
ADVERSARIAL_SECRETS = (SECRET_PATH, SECRET_HOST, "vendor-example.com", SECRET_IP4, SECRET_IP6,
                       SECRET_TOKEN, SECRET_HEX, SECRET_CRED, SECRET_ACCOUNT,
                       SECRET_ACCOUNT_GROUPED)
ADVERSARIAL_PLUGIN = (
    "# data from " + SECRET_PATH + "/x.csv\n"
    "ENDPOINT = '" + SECRET_URL + "'\n"
    "HOST = '" + SECRET_HOST + "'\n"
    "BIND = ('" + SECRET_IP4 + "', 80)  # v6 " + SECRET_IP6 + "\n"
    "api_token = '" + SECRET_TOKEN + "'\n"
    "SALT = '" + SECRET_HEX + "'\n"
    "password = '" + SECRET_CRED + "'\n"
    "ACCOUNT = " + SECRET_ACCOUNT + "  # " + SECRET_ACCOUNT_GROUPED + "\n"
    "```\n@everyone <@123456789012345678>\n"
    "def compute(df, params):\n    return {'v': params['period']}\n")


# ---------------------------------------------------------------- 承認依頼の fixture

import json  # noqa: E402

NOW_ISO = "2026-10-05T00:00:00+00:00"


def metrics(trades=40, pf=1.31, avg_r=0.12, max_drawdown=3.5) -> dict:
    return {"trades": trades, "pf": pf, "avg_r": avg_r, "max_drawdown": max_drawdown}


def approval_payload(name="sma_cross", kind="strategy", digest="", origin="staging",
                     mission_id=7, **extra) -> dict:
    """現行の改善 loop が作る承認 payload と同じ形 (値は匿名化)。"""
    if origin == "staging":
        path = f"plugins/_staging/{mission_id}/{name}"
    else:
        path = f"plugins/_human/{name}"
    payload = {
        "name": name, "kind": kind, "candidate_origin": origin, "candidate_path": path,
        "content_hash": "c" * 64, "artifact_hash": digest,
        "eval_source": "history", "base_interval": "1h", "eval_timeframe": "1h",
        "live_source": "mt5", "mission_id": mission_id if origin == "staging" else None,
        "backlog_id": 3,
        "in_sample": {"USDJPY": metrics()}, "holdout": {"USDJPY": metrics(20, 1.18, 0.08)},
        "baseline": None, "analysis_run_ids": [], "trial_count": 4,
        "analysis_call_count": 1, "backtest_call_count": 3,
        "facts_version": 1,
        "parent_facts": {"backlog": None, "prior_runs": [], "trials": [],
                         "trials_omitted": 0, "analysis_call_count": 0},
        "agent_claims": {"selection_rationale": "RSI の閾値を見直した",
                         "summary": "期間を 5 から 8 に変更"},
        "audit_note": "x", "profitability_floor": {}, "indicator_deps": {},
    }
    payload.update(extra)
    return payload


def insert_approval(env, payload: dict, status: str = "pending", reason=None) -> int:
    cur = env.conn.execute(
        "INSERT INTO approval_requests(kind,payload_json,status,reason,created_at) "
        "VALUES ('plugin',?,?,?,?)",
        (json.dumps(payload, ensure_ascii=False), status, reason, NOW_ISO))
    env.conn.commit()
    return int(cur.lastrowid)


def insert_journal(env, approval_id: int, name: str, old_kind: str, old_target,
                   phase: str = "decided", new_target: str = "x") -> None:
    env.conn.execute(
        "INSERT INTO plugin_switch_journal(kind,approval_id,name,old_kind,old_target,"
        "temp_path,new_target,switch_required,phase,actor,updated_at) "
        "VALUES ('approve',?,?,?,?,'t',?,1,?,'a',?)",
        (approval_id, name, old_kind, old_target, new_target, phase, NOW_ISO))
    env.conn.commit()
