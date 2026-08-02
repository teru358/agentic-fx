"""承認済み plugin のロード (プラン 7 Task 3、設計書 §6)。

`approval_requests` (`kind="plugin"`) の承認済み行と Task 1 の
`discover`/`content_hash` を突き合わせ、**現在のファイル内容から再算出した
ハッシュが承認時の payload の content_hash と一致する plugin だけ**を返す。
不一致 (承認後の編集) または承認行そのものが無い plugin は「未承認」として
除外し warning ログを出す (fail closed — discover を通っても既定では使われ
ない)。

`kind="plugin"` は `approval_requests.kind` の値 (この承認要求が「plugin の
承認」であることを表す) であり、`PluginMeta.kind` (indicator/signal/strategy
— plugin 自体の種別) とは別の軸。1 つの plugin フォルダには
`kind="plugin"` の承認行が対応し、その payload の中に `PluginMeta.kind` は
含まれない (照合には name + content_hash のみを使う)。

**反映は次回起動時のみ** (hot reload しない — YAGNI、brief の明示指定)。
本番の承認行の書き込み (`approvals.create(kind="plugin", ...)`) は Task 6
の関心。本 task のテストは `approvals.create` + `decide` で承認状態を直接
作る。
"""
from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path

from agentic_fx.plugin.loader import PluginMeta, discover

_log = logging.getLogger(__name__)

# approval_requests.kind の値 (この承認要求の種別)。PluginMeta.kind
# (indicator/signal/strategy) とは別物 — モジュール docstring 参照。
APPROVAL_KIND = "plugin"


def approved_plugins(conn: sqlite3.Connection, plugins_dir: Path) -> list[PluginMeta]:
    """`plugins_dir` を discover し、承認済みかつハッシュ一致の plugin のみ返す。

    `plugins_dir` が存在しない場合は `discover` を呼ばず `[]` を返す
    (`discover` は存在しないディレクトリに対し `iterdir()` で
    `FileNotFoundError` を送出する — 未初期化環境で service.py の起動を
    妨げないよう、ここで吸収する)。

    同名 plugin に対して承認済み行が複数存在する場合、**現在のハッシュに
    一致する行が 1 つでもあれば**承認とみなす (「どれが最新の承認か」は
    判定しない — 一致という事実だけで十分)。
    """
    if not plugins_dir.is_dir():
        _log.info("plugins dir %s does not exist — no plugins loaded", plugins_dir)
        return []

    metas = discover(plugins_dir)
    if not metas:
        return []

    approved_hashes_by_name = _approved_hashes_by_name(conn)

    result: list[PluginMeta] = []
    for meta in metas:
        if meta.content_hash in approved_hashes_by_name.get(meta.name, ()):
            result.append(meta)
        else:
            _log.warning(
                "plugin %s: 未承認 (承認済みハッシュと不一致、または承認要求が"
                "存在しない) — excluding from load", meta.name)
    return result


def _approved_hashes_by_name(conn: sqlite3.Connection) -> dict[str, set[str]]:
    rows = conn.execute(
        "SELECT id, payload_json FROM approval_requests "
        "WHERE kind=? AND status='approved'", (APPROVAL_KIND,)).fetchall()
    out: dict[str, set[str]] = {}
    for row in rows:
        try:
            payload = json.loads(row["payload_json"])
        except (json.JSONDecodeError, TypeError) as exc:
            # DB 破損 (手動編集・移行漏れ等) の観測性のため warning を残す
            # (approved_plugins() 自体は fail closed で継続 — この行は
            # ただ「承認情報として使えない」だけとして扱う)。
            _log.warning(
                "approval_requests id=%s (kind=%s): payload_json が JSON と"
                "して解釈できません (%s) — skipping this row",
                row["id"], APPROVAL_KIND, exc)
            continue
        if not isinstance(payload, dict):
            _log.warning(
                "approval_requests id=%s (kind=%s): payload が dict では"
                "ありません (got %s) — skipping this row",
                row["id"], APPROVAL_KIND, type(payload).__name__)
            continue
        name, content_hash = payload.get("name"), payload.get("content_hash")
        if isinstance(name, str) and isinstance(content_hash, str):
            out.setdefault(name, set()).add(content_hash)
        else:
            _log.warning(
                "approval_requests id=%s (kind=%s): payload に有効な "
                "name/content_hash (str) がありません — skipping this row",
                row["id"], APPROVAL_KIND)
    return out
