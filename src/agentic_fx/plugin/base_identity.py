"""候補の土台版 (候補が何の版から作られたか) の保存形と、承認時の照合。

承認依頼の payload に `base` を 1 つ持たせ、承認の lock の中で今の live と比べる。
違えば配備しない (別の候補の変更を、知らないまま上書きしないため)。

保存の形:

    {"kind": "version", "artifact_hash": "<64 hex>"}   live が .versions/<name>/<hash> の symlink
    {"kind": "plain", "content_hash": "<64 hex>"}      live が plain dir (旧形式)
    {"kind": "none"}                                    同名の配備が無かった
    {"kind": "unchecked", "by": "bless"}                bless の依頼。照合しない印

`base` キー自体が無い依頼は、この仕組みより前に作られたものだけで、照合しない。
キーがあるのに形が壊れている依頼は、照合を省かず不一致として扱う。
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from agentic_fx.plugin import loader
from agentic_fx.store import plugin_deploy_ops as deploy_store

BASE_CHANGED = "base_changed"
BASE_CHANGED_TEXT = ("土台の版が配備中の版と違うため配備しませんでした。"
                     "配備中の版から作り直して提出してください")

_HEX64 = re.compile(r"[0-9a-f]{64}")

STATE_MATCH = "match"
STATE_MISMATCH = "mismatch"
STATE_NONE_BUT_LIVE = "none_but_live"
STATE_UNREADABLE = "unreadable"
STATE_LEGACY = "legacy"
STATE_UNCHECKED = "unchecked"
_PASSING = frozenset({STATE_MATCH, STATE_LEGACY, STATE_UNCHECKED})

UNCHECKED_BY_BLESS = {"kind": "unchecked", "by": "bless"}


class BaseUnresolvable(ValueError):
    """土台を確定できない。提出の時点で失敗させる (fail closed)。"""


@dataclass(frozen=True)
class BaseCheck:
    state: str
    base: str | None
    live: str | None

    @property
    def passes(self) -> bool:
        return self.state in _PASSING


def read_live(plugins_root: Path, name: str) -> dict:
    """`plugins_root/name` の今の形。`unreadable` は正規形でない target や読めない plain。

    symlink は生の target 文字列だけを見る (版 dir の中身や存在は問わない)。"""
    live = Path(plugins_root) / name
    if live.is_symlink():
        try:
            target = os.readlink(live)
        except OSError:
            return {"kind": "unreadable"}
        if loader.is_canonical_symlink_target(name, target):
            return {"kind": "version", "artifact_hash": target.rsplit("/", 1)[1]}
        return {"kind": "unreadable"}
    if live.is_dir():
        try:
            return {"kind": "plain", "content_hash": loader.content_hash(live)}
        except (OSError, ValueError):
            return {"kind": "unreadable"}
    if live.exists():
        return {"kind": "unreadable"}
    return {"kind": "none"}


def base_of_live(plugins_root: Path, name: str) -> dict:
    """提出時の live をそのまま土台にする。読めない形は拒否する。

    symlink は target の文字列だけでなく、root 内の版 dir が実在して読めることまで確かめる
    (承認時の `check` は文字列の比較のままで、ここだけが実体を見る)。"""
    current = read_live(plugins_root, name)
    if current["kind"] == "unreadable":
        raise BaseUnresolvable("live_unreadable")
    if current["kind"] == "version":
        identity = deploy_store.live_identity(plugins_root, name)
        if (identity is None or identity.kind != "symlink"
                or identity.artifact_hash is None):
            raise BaseUnresolvable("live_unreadable")
    return current


def base_of_snapshot(metas, name: str, plugins_root: Path) -> dict:
    """mission 開始時に写した版 (`PluginMeta`) から土台を導く。live は読み直さない。

    `meta.path` は `.versions/<name>/<hash>` の実体 (version)、または `plugins/<name>`
    自身 (plain)。どちらでもなければ失敗させる。同名が無ければ `none`。"""
    for meta in metas:
        if meta.name != name:
            continue
        path = Path(meta.path)
        if (path.parent.parent.name == ".versions" and path.parent.name == name
                and _HEX64.fullmatch(path.name)):
            return {"kind": "version", "artifact_hash": path.name}
        try:
            same = path.resolve() == (Path(plugins_root) / name).resolve()
        except OSError:
            same = False
        if same and isinstance(meta.content_hash, str) and _HEX64.fullmatch(meta.content_hash):
            return {"kind": "plain", "content_hash": meta.content_hash}
        raise BaseUnresolvable("base_unresolvable")
    return {"kind": "none"}


def _label(entry: dict) -> str:
    kind = entry["kind"]
    if kind == "version":
        return entry["artifact_hash"][:12]
    return kind


def is_valid_base(base) -> bool:
    if not isinstance(base, dict):
        return False
    kind = base.get("kind")
    if kind == "version":
        value = base.get("artifact_hash")
    elif kind == "plain":
        value = base.get("content_hash")
    elif kind == "none":
        return True
    elif kind == "unchecked":
        return base.get("by") == "bless"
    else:
        return False
    return isinstance(value, str) and _HEX64.fullmatch(value) is not None


def check(payload: dict, plugins_root: Path | None, name: str) -> BaseCheck:
    """payload の `base` と今の live を比べる。読み取りだけで、何も変えない。"""
    if "base" not in payload:
        return BaseCheck(STATE_LEGACY, None, None)
    base = payload["base"]
    if not is_valid_base(base):
        return BaseCheck(STATE_MISMATCH, "unknown", None)
    kind = base["kind"]
    if kind == "unchecked":
        return BaseCheck(STATE_UNCHECKED, None, None)
    if plugins_root is None:
        return BaseCheck(STATE_UNREADABLE, _label(base), None)
    live = read_live(plugins_root, name)
    base_label, live_label = _label(base), (
        "unreadable" if live["kind"] == "unreadable" else _label(live))
    if kind == "none":
        state = STATE_MATCH if live["kind"] == "none" else STATE_NONE_BUT_LIVE
    elif live["kind"] == "unreadable":
        state = STATE_UNREADABLE
    elif kind == "version":
        state = (STATE_MATCH if live["kind"] == "version"
                 and live["artifact_hash"] == base["artifact_hash"] else STATE_MISMATCH)
    else:  # plain: 同じ plain か、retire で外れた後の「無し」は通す
        state = (STATE_MATCH if live["kind"] == "none" or (
            live["kind"] == "plain" and live["content_hash"] == base["content_hash"])
            else STATE_MISMATCH)
    return BaseCheck(state, base_label, live_label)
