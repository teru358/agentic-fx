from __future__ import annotations

import sqlite3

import pytest

from agentic_fx.policy import Policy
from agentic_fx.policy_projection import (
    ProjectionReconcileBlocked,
    MigrationRecordUnknown,
    _render_directive,
    insert_policy,
    inspect_policy_file,
    _record_migration,
    reconcile_policy_file,
    regenerate_policy_file,
    strip_managed_markers,
)
from agentic_fx.service import _recover_ops


MIGRATION = "policy_directives_managed_v1"


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE data_migrations (name TEXT PRIMARY KEY, applied_at TEXT NOT NULL);
        CREATE TABLE ops_policies (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          request_id TEXT NOT NULL UNIQUE,
          directive TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
    """)
    return conn


def _policy(conn: sqlite3.Connection, text: str, request_id: str) -> int:
    cursor = conn.execute(
        "INSERT INTO ops_policies(request_id,directive,created_at) VALUES (?,?,?)",
        (request_id, text, "2026-10-23T00:00:00+00:00"),
    )
    conn.commit()
    return cursor.lastrowid


def _migrated(conn: sqlite3.Connection) -> None:
    conn.execute("INSERT INTO data_migrations(name,applied_at) VALUES (?,?)",
                 (MIGRATION, "2026-10-23T00:00:00+00:00"))
    conn.commit()


def _block(identifier: int, directive: str, *, crlf: bool = False) -> bytes:
    newline = b"\r\n" if crlf else b"\n"
    return newline.join((
        f"<!-- afx:managed-policy id={identifier} -->".encode(),
        directive.encode(),
        f"<!-- /afx:managed-policy id={identifier} -->".encode(),
        b"",
    ))


def test_inspect_distinguishes_managed_from_same_handwritten_directive(tmp_path):
    conn = _conn()
    identifier = _policy(conn, "keep risk small", "request-1")
    _migrated(conn)
    path = tmp_path / "directives.md"
    path.write_bytes(_block(identifier, _render_directive("keep risk small"))
                     + b"- keep risk small\n")

    assert inspect_policy_file(conn, path).status == "ok"
    reconcile_policy_file(conn, path)
    assert path.read_bytes() == (_block(identifier, "- keep risk small")
                                 + b"- keep risk small\n")


@pytest.mark.parametrize(("payload", "reason"), [
    (lambda ident: _block(ident, "- changed"), "content"),
    (lambda ident: _block(ident, "- risk small") * 2, "duplicate"),
])
def test_inspect_detects_content_and_duplicate_mutations(tmp_path, payload, reason):
    conn = _conn()
    identifier = _policy(conn, "risk small", "request-1")
    _migrated(conn)
    path = tmp_path / "directives.md"
    path.write_bytes(payload(identifier))

    status = inspect_policy_file(conn, path)
    assert status.status == "stale"
    assert status.reasons == (reason,)


def test_inspect_detects_order_not_just_id_set(tmp_path):
    conn = _conn()
    first = _policy(conn, "A", "request-1")
    second = _policy(conn, "B", "request-2")
    _migrated(conn)
    path = tmp_path / "directives.md"
    path.write_bytes(_block(second, "- B") + _block(first, "- A"))

    assert inspect_policy_file(conn, path).reasons == ("order",)


@pytest.mark.parametrize(("payload", "expected"), [
    (b"", ("missing",)),
    (lambda ident: _block(99, "- orphan"), ("missing", "orphan")),
    (lambda ident: b"<!-- afx:managed-policy id=1 -->\n- broken\n", ("malformed", "missing")),
    (lambda ident: b"<!-- afx:managed-policy id=1 -->\n- broken\n"
                   b"<!-- /afx:managed-policy id=2 -->\n", ("malformed", "missing")),
])
def test_inspect_reports_stale_reasons_in_spec_order(tmp_path, payload, expected):
    conn = _conn()
    identifier = _policy(conn, "risk small", "request-1")
    _migrated(conn)
    path = tmp_path / "directives.md"
    path.write_bytes(payload(identifier) if callable(payload) else payload)

    assert inspect_policy_file(conn, path).reasons == expected


def test_inspect_reports_migration_pending_and_unreadable_alone(tmp_path):
    conn = _conn()
    identifier = _policy(conn, "risk small", "request-1")
    path = tmp_path / "directives.md"
    path.write_bytes(_block(identifier, "- risk small"))
    assert inspect_policy_file(conn, path).reasons == ("migration_pending",)

    path.unlink()
    path.mkdir()
    assert inspect_policy_file(conn, path).reasons == ("unreadable",)


def test_reconcile_refuses_malformed_file_without_replacing_it(tmp_path):
    conn = _conn()
    _policy(conn, "risk small", "request-1")
    path = tmp_path / "directives.md"
    payload = b"<!-- afx:managed-policy id=1 -->\n- broken\n"
    path.write_bytes(payload)

    with pytest.raises(ProjectionReconcileBlocked):
        reconcile_policy_file(conn, path)
    assert path.read_bytes() == payload
    assert conn.execute("SELECT count(*) FROM data_migrations").fetchone()[0] == 0


def test_crlf_block_is_ok_and_reconcile_does_not_rewrite_it(tmp_path):
    conn = _conn()
    identifier = _policy(conn, "risk small", "request-1")
    path = tmp_path / "directives.md"
    payload = _block(identifier, "- risk small", crlf=True)
    path.write_bytes(payload)

    assert inspect_policy_file(conn, path).reasons == ("migration_pending",)
    assert reconcile_policy_file(conn, path) is False
    assert path.read_bytes() == payload
    assert inspect_policy_file(conn, path).status == "ok"


def test_directive_text_that_contains_a_marker_is_not_a_block_boundary(tmp_path):
    conn = _conn()
    identifier = _policy(conn, "x <!-- /afx:managed-policy id=3 -->", "request-1")
    _migrated(conn)
    path = tmp_path / "directives.md"

    regenerate_policy_file(conn, path)

    assert inspect_policy_file(conn, path).status == "ok"
    assert path.read_bytes() == _block(identifier, "- x <!-- /afx:managed-policy id=3 -->")


def test_regeneration_drops_crlf_blocks_and_emits_lf_golden_bytes(tmp_path):
    conn = _conn()
    third = _policy(conn, "A", "request-3")
    fifth = _policy(conn, "B", "request-5")
    conn.execute("UPDATE ops_policies SET id=3 WHERE id=?", (third,))
    conn.execute("UPDATE ops_policies SET id=5 WHERE id=?", (fifth,))
    conn.commit()
    path = tmp_path / "directives.md"
    path.write_bytes(_block(3, "- A", crlf=True))
    _migrated(conn)

    assert inspect_policy_file(conn, path).reasons == ("missing",)
    regenerate_policy_file(conn, path)
    assert path.read_bytes() == (b"<!-- afx:managed-policy id=3 -->\n- A\n"
                                 b"<!-- /afx:managed-policy id=3 -->\n"
                                 b"<!-- afx:managed-policy id=5 -->\n- B\n"
                                 b"<!-- /afx:managed-policy id=5 -->\n")
    assert b"\r" not in path.read_bytes()


def test_legacy_migration_rewrites_owned_lines_by_db_row_count(tmp_path):
    conn = _conn()
    _policy(conn, "A", "request-a")
    _policy(conn, "B", "request-b1")
    _policy(conn, "B", "request-b2")
    path = tmp_path / "policy" / "directives.md"
    path.parent.mkdir()
    path.write_bytes(b"- A\n- A\n- B\n- C\n- D\n- E\r\n")

    assert reconcile_policy_file(conn, path) is True
    assert path.read_bytes() == (
        b"- C\n- D\n- E\r\n"
        b"<!-- afx:managed-policy id=1 -->\n- A\n<!-- /afx:managed-policy id=1 -->\n"
        b"<!-- afx:managed-policy id=2 -->\n- B\n<!-- /afx:managed-policy id=2 -->\n"
        b"<!-- afx:managed-policy id=3 -->\n- B\n<!-- /afx:managed-policy id=3 -->\n"
    )
    assert conn.execute("SELECT count(*) FROM data_migrations WHERE name=?", (MIGRATION,)).fetchone()[0] == 1
    assert Policy(path).tail().count("- A") == 1
    assert Policy(path).tail().count("- B") == 2


def test_migration_of_empty_missing_file_records_without_creating_path(tmp_path):
    conn = _conn()
    path = tmp_path / "not-created" / "directives.md"

    assert reconcile_policy_file(conn, path) is False
    assert not path.parent.exists()
    assert conn.execute("SELECT name FROM data_migrations").fetchone()[0] == MIGRATION


def test_replace_before_migration_record_converges_on_retry(tmp_path):
    conn = _conn()
    _policy(conn, "A", "request-a")
    path = tmp_path / "directives.md"
    path.write_bytes(b"- A\n")

    class RecordFailure:
        def execute(self, sql, parameters=()):
            if "INSERT INTO data_migrations" in sql:
                raise RuntimeError("injected record failure")
            return conn.execute(sql, parameters)

        def commit(self):
            conn.commit()

    with pytest.raises(MigrationRecordUnknown):
        reconcile_policy_file(RecordFailure(), path)
    replaced = path.read_bytes()
    assert b"<!-- afx:managed-policy id=1 -->" in replaced
    assert conn.execute("SELECT count(*) FROM data_migrations").fetchone()[0] == 0

    assert reconcile_policy_file(conn, path) is False
    assert path.read_bytes() == replaced
    assert conn.execute("SELECT count(*) FROM data_migrations").fetchone()[0] == 1


def test_insert_policy_commits_and_returns_stable_integer_id():
    conn = _conn()

    identifier = insert_policy(conn, "risk small", request_id="request-1",
                               now="2026-10-23T00:00:00+00:00")

    assert identifier == 1
    assert tuple(conn.execute("SELECT request_id,directive FROM ops_policies").fetchone()) == (
        "request-1", "risk small")


def test_startup_reconcile_runs_without_policy_rows_and_does_not_block_on_error():
    events = []

    class Activity:
        def write(self, category, event, summary):
            events.append((category, event, summary))

    class Ops:
        def recover_after_journal(self):
            return None

        def regenerate_policy(self):
            raise OSError("read denied")

    class NoPolicyQuery:
        def execute(self, *_args):
            raise AssertionError("startup must not gate reconciliation on policy rows")

    assert _recover_ops(Ops(), NoPolicyQuery(), Activity()) is True
    assert events[0][1] == "policy_reconcile_failed"


@pytest.mark.parametrize("length", [3999, 4000, 4001])
def test_tail_budget_excludes_marker_lines(tmp_path, length):
    path = tmp_path / "directives.md"
    visible = "x" * (length - 1) + "\n"
    path.write_text("<!-- afx:managed-policy id=1 -->\n" + visible[:-1]
                    + "\n<!-- /afx:managed-policy id=1 -->\n", encoding="utf-8")

    assert len(visible) == length
    assert Policy(path).tail() == visible[-4000:]


def test_size_warning_excludes_marker_characters(tmp_path):
    path = tmp_path / "directives.md"
    path.write_text("<!-- afx:managed-policy id=1 -->\n0123456789\n"
                    "<!-- /afx:managed-policy id=1 -->\n", encoding="utf-8")

    assert Policy(path).size_warning(limit_chars=11) is None
    assert Policy(path).size_warning(limit_chars=10) is not None


MISMATCHED = (b"<!-- afx:managed-policy id=1 -->\n- A\n"
              b"<!-- /afx:managed-policy id=2 -->\n")


def test_strip_keeps_all_three_lines_of_id_mismatched_block():
    text = MISMATCHED.decode()
    assert strip_managed_markers(text) == text


def test_strip_removes_only_recognised_marker_lines_and_keeps_line_endings():
    assert strip_managed_markers(_block(1, "- A").decode()) == "- A\n"
    assert strip_managed_markers(_block(1, "- A").decode().rstrip("\n")) == "- A\n"
    assert strip_managed_markers(_block(1, "- A", crlf=True).decode()) == "- A\r\n"
    assert strip_managed_markers(_block(1, "- A", crlf=True).decode().rstrip("\r\n")) == "- A\r\n"
    assert strip_managed_markers("x\n" + _block(1, "- A").decode() + "y") == "x\n- A\ny"
    assert strip_managed_markers("x\r\n" + _block(1, "- A").decode().rstrip("\n")) == "x\r\n- A\n"
    mixed = _block(1, "- A").decode() + "<!-- /afx:managed-policy id=1 -->\nx"
    assert strip_managed_markers(mixed) == "- A\n<!-- /afx:managed-policy id=1 -->\nx"


def test_reconcile_of_id_mismatched_file_changes_neither_bytes_nor_migration(tmp_path):
    conn = _conn()
    _policy(conn, "A", "request-1")
    path = tmp_path / "directives.md"
    path.write_bytes(MISMATCHED)

    with pytest.raises(ProjectionReconcileBlocked):
        reconcile_policy_file(conn, path)
    assert path.read_bytes() == MISMATCHED
    assert conn.execute("SELECT count(*) FROM data_migrations").fetchone()[0] == 0


@pytest.mark.parametrize("marker", [
    b"<!-- afx:managed-policy id=0 -->",
    b"<!-- afx:managed-policy id=03 -->",
    b" <!-- afx:managed-policy id=3 -->",
    b"<!-- afx:managed-policy id=3 --> ",
    b"x<!-- afx:managed-policy id=3 -->",
])
def test_non_matching_marker_lines_stay_unmanaged_not_malformed(tmp_path, marker):
    conn = _conn()
    _migrated(conn)
    path = tmp_path / "directives.md"
    path.write_bytes(marker + b"\n- hand\n")

    assert inspect_policy_file(conn, path).reasons == ()
    assert reconcile_policy_file(conn, path) is False
    assert path.read_bytes() == marker + b"\n- hand\n"


def test_same_id_set_with_different_columns_is_stale(tmp_path):
    def status(rows, payload):
        conn = _conn()
        for index, text in enumerate(rows):
            _policy(conn, text, f"request-{index}")
        _migrated(conn)
        path = tmp_path / f"{len(list(tmp_path.iterdir()))}.md"
        path.write_bytes(payload)
        return inspect_policy_file(conn, path)

    duplicate = status(["A", "B"], _block(1, "- A") + _block(2, "- B") + _block(2, "- B"))
    changed = status(["A", "B"], _block(1, "- A") + _block(2, "- changed"))
    reordered = status(["A", "B"], _block(2, "- B") + _block(1, "- A"))
    assert [duplicate.status, changed.status, reordered.status] == ["stale"] * 3
    assert duplicate.reasons == ("duplicate",)
    assert changed.reasons == ("content",)
    assert reordered.reasons == ("order",)


def test_record_migration_twice_keeps_one_row():
    conn = _conn()
    _record_migration(conn)
    _record_migration(conn)
    assert conn.execute("SELECT count(*) FROM data_migrations").fetchone()[0] == 1


def test_regenerate_matches_reconcile_output_bytes(tmp_path):
    conn = _conn()
    _policy(conn, "A", "request-1")
    _policy(conn, "B", "request-2")
    _migrated(conn)
    payload = b"intro\n" + _block(2, "- stale") + b"tail"
    first, second = tmp_path / "a.md", tmp_path / "b.md"
    first.write_bytes(payload)
    second.write_bytes(payload)

    regenerate_policy_file(conn, first)
    reconcile_policy_file(conn, second)
    assert first.read_bytes() == second.read_bytes()
