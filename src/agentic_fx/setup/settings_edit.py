"""コメントや順序を保った settings.yaml の限定的な行編集。"""
from __future__ import annotations

import os
import re
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from agentic_fx.config import Settings


_KEY_LINE = re.compile(r"^(?P<indent>[ ]*)(?P<key>[A-Za-z_][A-Za-z0-9_-]*):(?P<value>.*)$")


@dataclass(frozen=True)
class EditResult:
    ok: bool
    text: str | None = None
    reason: str = ""


@dataclass(frozen=True)
class _Entry:
    path: tuple[str, ...]
    line_number: int
    line_index: int
    indent: int
    value: str


def _entries(text: str) -> list[_Entry]:
    stack: list[tuple[int, str]] = []
    entries: list[_Entry] = []
    for index, line in enumerate(text.splitlines()):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        match = _KEY_LINE.match(line)
        if match is None:
            continue
        indent = len(match.group("indent"))
        while stack and stack[-1][0] >= indent:
            stack.pop()
        key = match.group("key")
        entries.append(_Entry(tuple(part for _, part in stack) + (key,), index + 1,
                              index, indent, match.group("value")))
        stack.append((indent, key))
    return entries


def find_duplicate_keys(text: str) -> dict[str, list[int]]:
    """Return repeated mapping paths and their one-based source line numbers."""
    try:
        root = yaml.compose(text)
    except yaml.YAMLError:
        return {}
    found: dict[tuple[str, ...], list[int]] = {}

    def visit(node: yaml.Node, path: tuple[str, ...]) -> None:
        if not isinstance(node, yaml.MappingNode):
            return
        keys: dict[str, list[int]] = {}
        for key_node, value_node in node.value:
            key = key_node.value if isinstance(key_node, yaml.ScalarNode) else str(key_node.value)
            keys.setdefault(key, []).append(key_node.start_mark.line + 1)
            visit(value_node, path + (key,))
        for key, lines in keys.items():
            if len(lines) > 1:
                found[path + (key,)] = lines

    if root is not None:
        visit(root, ())
    return {".".join(path): lines for path, lines in found.items() if len(lines) > 1}


def _dump_scalar(value: Any) -> str:
    rendered = yaml.safe_dump(value, default_flow_style=True, allow_unicode=True,
                              width=10_000, sort_keys=False).strip()
    if rendered.endswith("\n..."):
        rendered = rendered[:-4]
    if "\n" in rendered:
        raise ValueError("複数行になる値")
    return rendered


def _split_comment(value: str) -> tuple[str, str]:
    quote: str | None = None
    escaped = False
    for index, char in enumerate(value):
        if quote is not None:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
        elif char in "\"'":
            quote = char
        elif char == "#" and (index == 0 or value[index - 1].isspace()):
            return value[:index].rstrip(), value[index:]
    return value.rstrip(), ""


def _comment_gap(original: str, rendered: str, comment: str) -> str:
    if not comment:
        return ""
    comment_at = original.index(comment)
    return " " * max(1, comment_at - len(rendered))


def _get_path(raw: Any, dotted: str) -> Any:
    value = raw
    for part in dotted.split("."):
        if not isinstance(value, dict) or part not in value:
            raise KeyError(dotted)
        value = value[part]
    return value


def _failure(reason: str) -> EditResult:
    return EditResult(ok=False, reason=reason)


def edit_settings(text: str, changes: dict[str, Any]) -> EditResult:
    """Apply scalar changes without reformatting unrelated settings lines."""
    for line_number, line in enumerate(text.splitlines(), 1):
        if "\t" in line:
            return _failure(f"行 {line_number} に tab があります。手で編集してください。")
    duplicates = find_duplicate_keys(text)
    if duplicates:
        details = "; ".join(
            f"{key} (行 " + " と ".join(map(str, lines)) + ")"
            for key, lines in duplicates.items())
        return _failure(f"重複キーがあります: {details}。手で編集してください。")

    lines = text.splitlines(keepends=True)
    entries = {".".join(entry.path): entry for entry in _entries(text)}
    replacements: dict[int, str] = {}
    flow_mappings: dict[int, dict[str, Any]] = {}
    for dotted, wanted in changes.items():
        entry = entries.get(dotted)
        if entry is not None:
            scalar, comment = _split_comment(entry.value)
            if not scalar.strip() or scalar.lstrip().startswith(("|", ">")):
                return _failure(
                    f"{dotted} (行 {entry.line_number}) は複数行または block の形です。"
                    "手で編集してください。")
            try:
                rendered = _dump_scalar(wanted)
            except ValueError as error:
                return _failure(f"{dotted} は {error} のため手で編集してください。")
            newline = "\r\n" if lines[entry.line_index].endswith("\r\n") else ("\n" if lines[entry.line_index].endswith("\n") else "")
            prefix = " " * entry.indent + entry.path[-1] + ": "
            gap = _comment_gap(entry.value, rendered, comment)
            replacements[entry.line_index] = prefix + rendered + gap + comment + newline
            continue

        parent, _, child = dotted.rpartition(".")
        parent_entry = entries.get(parent)
        if parent_entry is None:
            return _failure(f"{dotted}: 所定の block がありません。手で編集してください。")
        scalar, comment = _split_comment(parent_entry.value)
        if not scalar.lstrip().startswith("{"):
            return _failure(
                f"{dotted} (行 {parent_entry.line_number}) は書き換えられない形です。"
                "手で編集してください。")
        try:
            mapping = flow_mappings.get(parent_entry.line_index)
            if mapping is None:
                mapping = yaml.safe_load(scalar)
        except yaml.YAMLError:
            mapping = None
        if not isinstance(mapping, dict) or child not in mapping:
            return _failure(f"{dotted}: 所定の block がありません。手で編集してください。")
        mapping[child] = wanted
        flow_mappings[parent_entry.line_index] = mapping
        rendered = _dump_scalar(mapping)
        newline = "\r\n" if lines[parent_entry.line_index].endswith("\r\n") else ("\n" if lines[parent_entry.line_index].endswith("\n") else "")
        prefix = " " * parent_entry.indent + parent_entry.path[-1] + ": "
        gap = _comment_gap(parent_entry.value, rendered, comment)
        replacements[parent_entry.line_index] = prefix + rendered + gap + comment + newline

    updated = "".join(replacements.get(index, line) for index, line in enumerate(lines))
    try:
        raw = yaml.safe_load(updated) or {}
    except yaml.YAMLError as error:
        return _failure(f"意味的照合に失敗しました: {error}")
    for dotted, wanted in changes.items():
        try:
            if _get_path(raw, dotted) != wanted:
                return _failure(f"意味的照合に失敗しました: {dotted} が意図した値になっていません。")
        except KeyError:
            return _failure(f"意味的照合に失敗しました: {dotted} が見つかりません。")
    try:
        Settings.model_validate(raw)
    except Exception as error:
        return _failure(f"型検証に失敗しました: {error}")
    return EditResult(ok=True, text=updated)


def atomic_write_settings(path: Path, text: str) -> None:
    """Durably replace a regular settings file, retaining its mode or using 0644 when new."""
    parent = path.parent
    try:
        existing = os.lstat(path)
    except FileNotFoundError:
        mode = 0o644
    else:
        if stat.S_ISLNK(existing.st_mode):
            raise ValueError(f"{path}: symlink は書き換えません。手で編集してください。")
        if not stat.S_ISREG(existing.st_mode):
            raise ValueError(f"{path}: 通常の file ではないので書き換えません。手で確かめてください。")
        mode = stat.S_IMODE(existing.st_mode)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=parent)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, path)
        directory = os.open(parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise
