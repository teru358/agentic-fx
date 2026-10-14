"""Explicit, line-preserving migrations for settings schema versions."""
from __future__ import annotations

import yaml

from agentic_fx.config import Settings
from agentic_fx.setup.settings_edit import EditResult, find_duplicate_keys


CURRENT_SCHEMA_VERSION = 2
_SCHEMA_LINE = "schema_version: 2  # [init] settings の版。`afx init` が移行する\n"
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
        else:
            return _failure("この settings の移行経路がありません。手で編集してください。")
        if not result.ok or result.text is None:
            return result
        text = result.text
        version += 1
    if version == CURRENT_SCHEMA_VERSION:
        return EditResult(ok=True, text=text)
    return _failure("この settings の移行経路がありません。手で編集してください。")
