"""The durable policy record's file projection."""
from __future__ import annotations

import contextlib
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


_MIGRATION = "policy_directives_managed_v1"
_START = re.compile(br"^<!-- afx:managed-policy id=([1-9][0-9]*) -->$")
_END = re.compile(br"^<!-- /afx:managed-policy id=([1-9][0-9]*) -->$")


class ProjectionReconcileBlocked(RuntimeError):
    """The current projection cannot safely be replaced."""


class ProjectionUnreadableError(ProjectionReconcileBlocked):
    """The projection file cannot be read."""


class ProjectionMalformedError(ProjectionReconcileBlocked):
    """The projection contains an unpaired managed-policy marker."""


class MigrationRecordUnknown(RuntimeError):
    """The projection was replaced before recording its migration failed."""


@dataclass(frozen=True, slots=True)
class ProjectionStatus:
    status: str
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _ParsedFile:
    lines: tuple[bytes, ...]
    blocks: tuple[tuple[int, bytes, tuple[int, int, int]], ...]
    malformed: bool


def _render_directive(text: str) -> str:
    return "- " + " ".join(text.split())


def insert_policy(conn, text: str, *, request_id: str, now) -> int:
    """Insert one canonical policy row and return its stable integer ID."""
    timestamp = now.isoformat() if hasattr(now, "isoformat") else str(now)
    cursor = conn.execute(
        "INSERT INTO ops_policies(request_id,directive,created_at) VALUES (?,?,?)",
        (request_id, text, timestamp),
    )
    conn.commit()
    return int(cursor.lastrowid)


def _line_view(line: bytes) -> bytes:
    return line[:-1] if line.endswith(b"\r") else line


def _read(path: Path) -> bytes | None:
    try:
        payload = path.read_bytes()
        payload.decode("utf-8")
        return payload
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as exc:
        raise ProjectionUnreadableError(str(exc)) from exc


def _parse(payload: bytes) -> _ParsedFile:
    lines = tuple(payload.split(b"\n"))
    blocks: list[tuple[int, bytes, tuple[int, int, int]]] = []
    claimed: set[int] = set()
    index = 0
    while index < len(lines):
        start = _START.fullmatch(_line_view(lines[index]))
        if start is not None and index + 2 < len(lines):
            end = _END.fullmatch(_line_view(lines[index + 2]))
            if end is not None and start.group(1) == end.group(1):
                blocks.append((int(start.group(1)), lines[index + 1],
                               (index, index + 1, index + 2)))
                claimed.update((index, index + 1, index + 2))
                index += 3
                continue
        index += 1
    malformed = any(
        position not in claimed
        and (_START.fullmatch(_line_view(line)) is not None
             or _END.fullmatch(_line_view(line)) is not None)
        for position, line in enumerate(lines)
    )
    return _ParsedFile(lines, tuple(blocks), malformed)


def _canonical(conn) -> list[tuple[int, bytes]]:
    rows = conn.execute("SELECT id,directive FROM ops_policies ORDER BY id").fetchall()
    return [(int(row[0]), _render_directive(row[1]).encode("utf-8")) for row in rows]


def _migration_applied(conn) -> bool:
    return conn.execute("SELECT 1 FROM data_migrations WHERE name=?",
                        (_MIGRATION,)).fetchone() is not None


def _without_blocks(parsed: _ParsedFile) -> bytes:
    claimed = {position for _, _, positions in parsed.blocks for position in positions}
    return b"\n".join(line for position, line in enumerate(parsed.lines)
                      if position not in claimed)


def _compose(outside: bytes, canonical: list[tuple[int, bytes]]) -> bytes:
    if outside and not outside.endswith(b"\n"):
        outside += b"\n"
    generated = b"".join(
        b"<!-- afx:managed-policy id=" + str(identifier).encode("ascii") + b" -->\n"
        + directive + b"\n<!-- /afx:managed-policy id="
        + str(identifier).encode("ascii") + b" -->\n"
        for identifier, directive in canonical
    )
    return outside + generated


def _atomic_replace(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as file:
            file.write(payload)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)
        raise


def _replace_if_changed(path: Path, before: bytes | None, candidate: bytes) -> bool:
    if before == candidate:
        return False
    if before is None and not candidate:
        return False
    _atomic_replace(path, candidate)
    return True


def strip_managed_markers(text: str) -> str:
    """Remove the marker lines of recognised managed blocks, keeping every line ending."""
    parsed = _parse(text.encode("utf-8"))
    dropped = {positions[0] for _, _, positions in parsed.blocks}
    dropped |= {positions[2] for _, _, positions in parsed.blocks}
    last = len(parsed.lines) - 1
    # marker 行はその行終端ごと除く。残る行の終端 (LF / CRLF / 末尾の終端なし) はそのまま。
    return "".join(
        line.decode("utf-8") + ("" if position == last else "\n")
        for position, line in enumerate(parsed.lines)
        if position not in dropped
    )


def _candidate(conn, before: bytes | None) -> bytes:
    parsed = _parse(before or b"")
    return _compose(_without_blocks(parsed), _canonical(conn))


def regenerate_policy_file(conn, path: Path) -> None:
    """Rebuild managed blocks while retaining all non-managed bytes."""
    before = _read(path)
    _replace_if_changed(path, before, _candidate(conn, before))


def _inspect_payload(conn, before: bytes | None) -> ProjectionStatus:
    parsed = _parse(before or b"")
    canonical = _canonical(conn)
    actual = [(identifier, _line_view(directive))
              for identifier, directive, _ in parsed.blocks]
    expected_by_id = dict(canonical)
    actual_ids = [identifier for identifier, _ in actual]
    expected_ids = [identifier for identifier, _ in canonical]
    reasons: list[str] = []
    if not _migration_applied(conn):
        reasons.append("migration_pending")
    if parsed.malformed:
        reasons.append("malformed")
    if any(identifier not in actual_ids for identifier in expected_ids):
        reasons.append("missing")
    if any(identifier not in expected_by_id for identifier in actual_ids):
        reasons.append("orphan")
    if len(actual_ids) != len(set(actual_ids)):
        reasons.append("duplicate")
    if any(identifier in expected_by_id and directive != expected_by_id[identifier]
           for identifier, directive in actual):
        reasons.append("content")
    if not any(reason in reasons for reason in ("missing", "orphan", "duplicate", "content")) \
            and actual_ids != expected_ids:
        reasons.append("order")
    return ProjectionStatus("ok" if not reasons else "stale", tuple(reasons))

def inspect_policy_file(conn, path: Path) -> ProjectionStatus:
    """Compare the file projection with canonical policy rows without writing."""
    try:
        before = _read(path)
    except ProjectionUnreadableError:
        return ProjectionStatus("stale", ("unreadable",))
    return _inspect_payload(conn, before)


def unmanaged_policy_lines(path: Path) -> list[tuple[int, str]]:
    """Return file lines that do not belong to a valid managed block."""
    try:
        before = _read(path)
    except ProjectionUnreadableError:
        return []
    if before is None:
        return []
    parsed = _parse(before)
    claimed = {position for _, _, positions in parsed.blocks for position in positions}
    terminal = len(parsed.lines) - 1
    return [
        (position + 1, line.decode("utf-8"))
        for position, line in enumerate(parsed.lines)
        if position not in claimed and not (position == terminal and line == b"")
    ]


def _record_migration(conn, now=None) -> None:
    timestamp = (now or datetime.now(timezone.utc)).isoformat()
    conn.execute(
        "INSERT INTO data_migrations(name,applied_at) SELECT ?,? "
        "WHERE NOT EXISTS (SELECT 1 FROM data_migrations WHERE name=?)",
        (_MIGRATION, timestamp, _MIGRATION))
    conn.commit()


def reconcile_policy_file(conn, path: Path) -> bool:
    """Migrate legacy directives once, then converge the file to its records."""
    before = _read(path)
    parsed = _parse(before or b"")
    if parsed.malformed:
        raise ProjectionMalformedError("managed-policy markers are malformed")
    changed = False
    current = before
    if not _migration_applied(conn):
        if not parsed.blocks:
            canonical = _canonical(conn)
            legacy_owned = {directive for _, directive in canonical}
            outside = b"\n".join(
                line for line in parsed.lines if _line_view(line) not in legacy_owned
            )
            migrated = _compose(outside, canonical)
            changed = _replace_if_changed(path, before, migrated)
            if changed:
                current = migrated
        try:
            _record_migration(conn)
        except BaseException as exc:
            if changed:
                raise MigrationRecordUnknown() from exc
            raise
    if _inspect_payload(conn, current).status == "ok":
        return changed
    return _replace_if_changed(path, current, _candidate(conn, current)) or changed
