"""Explicit, line-preserving migrations for settings schema versions."""
from __future__ import annotations

import re

import yaml

from agentic_fx.config import Settings
from agentic_fx.setup.settings_edit import EditResult, find_duplicate_keys


CURRENT_SCHEMA_VERSION = 5
_SCHEMA_LINE = "schema_version: 5  # [init] settings の版。`afx init` が移行する\n"
_V1_SCHEMA_LINE = "schema_version: 1  # settings の版。`afx init` が移行する\n"


def _failure(reason: str) -> EditResult:
    return EditResult(ok=False, reason=reason)


def _schema_version(text: str) -> tuple[int, str | None]:
    try:
        raw = yaml.safe_load(text) or {}
    except yaml.YAMLError as error:
        return -1, f"settings を読めません: {error}。手で編集してください。"
    if not isinstance(raw, dict):
        return -1, "settings のトップレベルが mapping ではありません。手で編集してください。"
    value = raw.get("schema_version", 0)
    if type(value) is not int or value < 0:
        return -1, "schema_version は 0 以上の整数にしてください。手で編集してください。"
    return value, None


def _migrate_zero_to_one(text: str) -> EditResult:
    """Introduce v1's explicit marker while accepting unmarked v0 settings.

    Version 0 predates schema tracking, so the marker establishes a stable
    migration starting point without changing existing settings semantics.
    Future migrations remain explicit and preserve untouched lines.
    """
    lines = text.splitlines(keepends=True)
    insertion = 0
    while insertion < len(lines) and (not lines[insertion].strip() or lines[insertion].lstrip().startswith("#")):
        insertion += 1
    if insertion == len(lines) or not lines[insertion].startswith("display_timezone:"):
        return _failure(
            "schema_version を入れる位置を決められません。"
            "先頭の display_timezone の前に `schema_version: 1` を足して手で編集してください。")
    updated = "".join(lines[:insertion] + [_V1_SCHEMA_LINE] + lines[insertion:])
    try:
        Settings.model_validate(yaml.safe_load(updated) or {})
    except Exception as error:
        return _failure(f"移行後の型検証に失敗しました: {error}。手で編集してください。")
    return EditResult(ok=True, text=updated)


def _migrate_one_to_two(text: str) -> EditResult:
    """Update only the v1 marker to v2, preserving explicit API choices.

    The example default for ``api.enabled`` changed from false to true. Existing
    settings retain their chosen value; only step 8 of ``afx init`` changes it.
    """
    lines = text.splitlines(keepends=True)
    for index, line in enumerate(lines):
        if line.startswith("schema_version: 1"):
            lines[index] = line.replace("schema_version: 1", "schema_version: 2", 1)
            return EditResult(ok=True, text="".join(lines))
    return _failure("schema_version: 1 の行を見つけられません。手で編集してください。")


_PLUGIN_LOCK_LINE = ("plugin_lock_timeout_sec: 2      "
                     "# signal producer / OPEN の plugin name lock 待機上限 (秒)\n")
_RECONCILE_LOCK_LINE = ("reconcile_lock_timeout_sec: 30  "
                        "# 起動時 deploy reconcile 全体の plugin name lock 待機上限 (秒)\n")


_VERSION_TWO_LINE = re.compile(r"(schema_version:[ \t]*)2(([ \t]+#.*)?\r?\n?)")


def _migrate_two_to_three(text: str) -> EditResult:
    """Bump the marker to v3 and add the worker lock timeouts that are absent.

    ``worker.plugin_lock_timeout_sec`` and ``worker.reconcile_lock_timeout_sec`` are
    each appended only when missing; an existing value is never touched. When
    ``worker`` is absent or not a block mapping the model defaults apply, so only
    the marker changes.
    """
    lines = text.splitlines(keepends=True)
    marker = next((i for i, line in enumerate(lines) if _VERSION_TWO_LINE.fullmatch(line)), None)
    if marker is None:
        return _failure("schema_version: 2 の行を見つけられません。手で編集してください。")
    lines[marker], replaced = _VERSION_TWO_LINE.subn(r"\g<1>3\g<2>", lines[marker], count=1)
    if replaced != 1:
        return _failure("schema_version: 2 の行を書き換えられませんでした。手で編集してください。")
    raw = yaml.safe_load(text) or {}
    worker = raw.get("worker")
    if isinstance(worker, dict) and ("plugin_lock_timeout_sec" not in worker
                                     or "reconcile_lock_timeout_sec" not in worker):
        head = next((i for i, line in enumerate(lines) if line.startswith("worker:")), None)
        child = None
        if head is not None:
            for i in range(head + 1, len(lines)):
                stripped = lines[i].strip()
                if not stripped or stripped.startswith("#"):
                    continue
                if lines[i][0] in " \t":
                    child = i
                break
        if child is not None:
            indent = lines[child][:len(lines[child]) - len(lines[child].lstrip())]
            last = child
            for i in range(child, len(lines)):
                if lines[i].strip() and not lines[i].startswith(indent):
                    break
                if lines[i].strip() and not lines[i].strip().startswith("#"):
                    last = i
            if not lines[last].endswith("\n"):
                lines[last] += "\n"
            additions = []
            if "plugin_lock_timeout_sec" not in worker:
                additions.append(indent + _PLUGIN_LOCK_LINE)
            if "reconcile_lock_timeout_sec" not in worker:
                additions.append(indent + _RECONCILE_LOCK_LINE)
            lines[last + 1:last + 1] = additions
    updated = "".join(lines)
    try:
        Settings.model_validate(yaml.safe_load(updated) or {})
    except Exception as error:
        return _failure(f"移行後の型検証に失敗しました: {error}。手で編集してください。")
    return EditResult(ok=True, text=updated)


_VERSION_THREE_LINE = re.compile(r"(schema_version:[ \t]*)3(([ \t]+#.*)?\r?\n?)")
_VERSION_FOUR_LINE = re.compile(r"(schema_version:[ \t]*)4(([ \t]+#.*)?\r?\n?)")


def _migrate_three_to_four(text: str) -> EditResult:
    """Bump the marker to v4 only.

    ``notify.trade_events`` is optional and defaults to true, so an older file keeps its
    meaning without a new section; only the marker line changes.
    """
    lines = text.splitlines(keepends=True)
    marker = next((i for i, line in enumerate(lines) if _VERSION_THREE_LINE.fullmatch(line)), None)
    if marker is None:
        return _failure("schema_version: 3 の行を見つけられません。手で編集してください。")
    lines[marker], replaced = _VERSION_THREE_LINE.subn(r"\g<1>4\g<2>", lines[marker], count=1)
    if replaced != 1:
        return _failure("schema_version: 3 の行を書き換えられませんでした。手で編集してください。")
    updated = "".join(lines)
    try:
        Settings.model_validate(yaml.safe_load(updated) or {})
    except Exception as error:
        return _failure(f"移行後の型検証に失敗しました: {error}。手で編集してください。")
    return EditResult(ok=True, text=updated)


def _migrate_four_to_five(text: str) -> EditResult:
    """Refuse ambiguous reference symbols before marking the v5 contract."""
    try:
        raw = yaml.safe_load(text) or {}
        datafeed = raw.get("datafeed") if isinstance(raw, dict) else None
        watch = datafeed.get("watch_symbols", []) if isinstance(datafeed, dict) else []
        pairs = raw.get("pairs", []) if isinstance(raw, dict) else []
    except yaml.YAMLError as error:
        return _failure(f"settings を読めません: {error}。手で編集してください。")
    if isinstance(watch, list):
        if len(set(watch)) != len(watch):
            return _failure("watch_symbols_duplicate")
        if set(watch) & set(pairs if isinstance(pairs, list) else []):
            return _failure("watch_symbols_overlap")
        if watch and datafeed.get("primary") != "mt5":
            return _failure(
                "watch_symbols_require_mt5_primary: datafeed.primary を mt5 にしてから "
                "afx init を再実行してください。")
    lines = text.splitlines(keepends=True)
    marker = next((i for i, line in enumerate(lines) if _VERSION_FOUR_LINE.fullmatch(line)), None)
    if marker is None:
        return _failure("schema_version: 4 の行を見つけられません。手で編集してください。")
    lines[marker], replaced = _VERSION_FOUR_LINE.subn(r"\g<1>5\g<2>", lines[marker], count=1)
    if replaced != 1:
        return _failure("schema_version: 4 の行を書き換えられませんでした。手で編集してください。")
    updated = "".join(lines)
    try:
        Settings.model_validate(yaml.safe_load(updated) or {})
    except Exception as error:
        return _failure(f"移行後の型検証に失敗しました: {error}。手で編集してください。")
    return EditResult(ok=True, text=updated)


def migrate(text: str) -> EditResult:
    """Migrate a settings document only through the explicitly listed versions."""
    duplicates = find_duplicate_keys(text)
    if duplicates:
        details = "; ".join(f"{key} (行 " + " と ".join(map(str, lines)) + ")"
                            for key, lines in duplicates.items())
        return _failure(f"重複キーがあります: {details}。手で編集してください。")
    version, reason = _schema_version(text)
    if reason:
        return _failure(reason)
    if version > CURRENT_SCHEMA_VERSION:
        return _failure("settings がこの afx より新しい版です。afx を更新してから再実行してください。")
    while version < CURRENT_SCHEMA_VERSION:
        if version == 0:
            result = _migrate_zero_to_one(text)
        elif version == 1:
            result = _migrate_one_to_two(text)
        elif version == 2:
            result = _migrate_two_to_three(text)
        elif version == 3:
            result = _migrate_three_to_four(text)
        elif version == 4:
            result = _migrate_four_to_five(text)
        else:
            return _failure("この settings の移行経路がありません。手で編集してください。")
        if not result.ok or result.text is None:
            return result
        text = result.text
        version += 1
    if version == CURRENT_SCHEMA_VERSION:
        return EditResult(ok=True, text=text)
    return _failure("この settings の移行経路がありません。手で編集してください。")
