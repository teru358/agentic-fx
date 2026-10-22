"""候補の質検査のうち、ロジック等価 (logic_hash) と仮説族 (family_key) の
比較に使う母集団の組み立てと判定。

母集団は 3 つ。配備済みの plugin、同梱の example、候補台帳の他 mission の行。
入出力は純粋な値で、DB の書き込みも activity への出力もここでは行わない
(呼び出し側が結果を記録する)。"""
from __future__ import annotations

import os
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from agentic_fx.plugin import loader
from agentic_fx.plugin.logic_identity import LOGIC_HASH_PREFIX, read_identity
from agentic_fx.store import plugin_deploy_ops as deploy_store
from agentic_fx.store.db import OPEN_PHASE_SQL

# 配備ガード (全 plugin 共通の flock) を排他で取るときの待ち上限 (秒)。
# 超えたら比較できないものとして gate_failed にする。
DEPLOY_GUARD_TIMEOUT_S = 30.0

# 台帳の行のうち、ロジック等価の比較対象にする outcome。人間の判断を受けた、
# または質検査で降格した候補に限る。同じ候補の途中の反復 (not_submitted)
# や、収益性で落ちただけの候補は、数値だけを変えた再提出を塞がないよう含めない。
ARCHIVE_COMPARED_OUTCOMES = (
    "pending", "approved", "rejected_by_human", "revoked",
    "duplicate_logic", "duplicate_metrics", "family_redirect")

_LOGIC_HASH_RE = re.compile(
    "^" + re.escape(LOGIC_HASH_PREFIX) + "[0-9a-f]{64}$")


@dataclass(frozen=True)
class IdentityEntry:
    source: str          # "deployed" | "example" | "archive"
    name: str
    kind: str | None
    logic_hash: str
    family_key: str | None
    outcome: str | None = None


@dataclass
class Population:
    """比較対象の一覧。`attempted` は組み立てを試みた件数、`skipped` は
    1 件だけ使えず外した行の説明 (warning 用)。"""
    entries: list[IdentityEntry] = field(default_factory=list)
    attempted: int = 0
    skipped: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class FilePopulation:
    """配備済みと example (ファイルから作る側)。配備ガードを排他で持った状態で
    組み立てる。配備済みは `plugins/` の live の状態そのもの。
    `live_state` は名前ごとの live の姿 (symlink の指す先の文字列 / "plain")。
    `unavailable` は組み立て自体が失敗したときの理由 (空でなければ比較不能)。"""
    deployed: Population
    examples: Population
    unavailable: str | None = None
    live_state: dict | None = None


class LiveStateUnreadable(Exception):
    """live の plugin entry を安全に読めない (正規形でない symlink など)。"""


def load_live_deployed(plugins_root: Path) -> tuple[Population, dict]:
    """`plugins/` の live entry を全部読む。symlink は
    `.versions/<name>/<64hex>` の正規形だけを受け、指す版 dir から identity を
    作る。plain dir はその dir から作る。正規形でない・root の外へ出る
    symlink は読めないものとして例外にする。plugin.py を読めない entry は
    1 行だけ外して skipped に残す。"""
    population = Population()
    state: dict[str, str] = {}
    if not plugins_root.is_dir():
        return population, state
    root_resolved = plugins_root.resolve()
    for item in sorted(plugins_root.iterdir(), key=lambda p: p.name):
        name = item.name
        if name.startswith(".") or not loader.is_canonical_plugin_name(name):
            continue
        if item.is_symlink():
            target = os.readlink(item)
            if not loader.is_canonical_symlink_target(name, target):
                raise LiveStateUnreadable(f"live_symlink_not_canonical:{name}")
            directory = plugins_root / target
            try:
                directory.resolve().relative_to(root_resolved)
            except ValueError:
                raise LiveStateUnreadable(
                    f"live_symlink_escapes_root:{name}") from None
            state[name] = target
        elif item.is_dir():
            directory = item
            state[name] = "plain"
        else:
            continue
        if item.is_symlink() and not directory.is_dir():
            population.attempted += 1
            population.skipped.append(f"deployed:{name}")
            continue
        if not (directory / "plugin.py").is_file():
            if item.is_symlink():
                population.attempted += 1
                population.skipped.append(f"deployed:{name}")
            continue
        population.attempted += 1
        logic_hash, family_key, config = read_identity(directory, name=name)
        if logic_hash is None:
            population.skipped.append(f"deployed:{name}")
            continue
        kind = config.get("kind") if isinstance(config, dict) else None
        population.entries.append(IdentityEntry(
            "deployed", name, kind, logic_hash, family_key))
    return population, state


def load_file_population(plugins_root: Path, examples_dir: Path | None
                         ) -> FilePopulation:
    """ファイルの読込が失敗しても例外にせず、`unavailable` に理由を残す
    (比較できないものを「母集団が空」と取り違えない)。"""
    try:
        deployed, state = load_live_deployed(plugins_root)
        return FilePopulation(
            deployed=deployed, examples=load_examples(examples_dir),
            live_state=state)
    except Exception as exc:
        return FilePopulation(
            deployed=Population(), examples=Population(),
            unavailable=f"file_population_read_failed:{type(exc).__name__}")


def load_examples(examples_dir: Path | None) -> Population:
    population = Population()
    if examples_dir is None or not examples_dir.is_dir():
        return population
    for item in sorted(examples_dir.iterdir()):
        if not item.is_dir() or not (item / "plugin.py").is_file():
            continue
        population.attempted += 1
        logic_hash, family_key, config = read_identity(item, name=item.name)
        if logic_hash is None:
            population.skipped.append(f"example:{item.name}")
            continue
        kind = config.get("kind") if isinstance(config, dict) else None
        population.entries.append(IdentityEntry(
            "example", item.name, kind, logic_hash, family_key))
    return population


def load_archive(conn: sqlite3.Connection, *, mission_id: int) -> Population:
    """他 mission の行から比較対象を作る。同じ mission の行は含めない。
    `logic_hash` が NULL の行 (計算できなかった旧行) は比較から外すだけで
    件数にも入れない。値の形が壊れた行は skipped に残して外す。DB の読み取り
    失敗は例外のまま伝える。"""
    placeholders = ",".join("?" for _ in ARCHIVE_COMPARED_OUTCOMES)
    rows = conn.execute(
        "SELECT id, name, logic_hash, family_key, outcome_class "
        "FROM candidate_archives WHERE mission_id != ? "
        "AND logic_hash IS NOT NULL "
        f"AND outcome_class IN ({placeholders}) ORDER BY id",
        (mission_id, *ARCHIVE_COMPARED_OUTCOMES)).fetchall()
    population = Population()
    for row in rows:
        population.attempted += 1
        value = row["logic_hash"]
        if not isinstance(value, str) or not _LOGIC_HASH_RE.match(value):
            population.skipped.append(f"archive:{row['id']}")
            continue
        population.entries.append(IdentityEntry(
            "archive", row["name"], None, value, row["family_key"],
            row["outcome_class"]))
    return population


def _in_flight_deploy_ops(conn: sqlite3.Connection) -> list[str]:
    names = [row[0] for row in conn.execute(
        "SELECT name FROM plugin_switch_journal "
        "WHERE phase NOT IN ('decided', 'reverted')")]
    names += [row[0] for row in conn.execute(
        "SELECT name FROM plugin_deploy_ops WHERE " + OPEN_PHASE_SQL)]
    return sorted(set(names))


def _recorded_live(conn: sqlite3.Connection) -> dict[str, str]:
    """配備の記録 (切替 journal と配備操作) が最後に残した live の姿を、名前ごとに
    返す。"absent" か symlink の指す先。記録の順序が曖昧でも、使うのは人の
    直接操作の検知 (activity) だけで、母集団の判定には使わない。"""
    names = {row[0] for row in conn.execute(
        "SELECT name FROM plugin_switch_journal "
        "WHERE phase='decided' AND switch_required=1")}
    names |= {row[0] for row in conn.execute(
        "SELECT name FROM plugin_deploy_ops WHERE "
        "(phase='done' AND op_kind IN "
        "('disable_symlink','rollback_symlink','retire_plain')) "
        "OR (phase='needs_retry' AND fail_reason='verify_failed' "
        "AND from_kind='symlink')")}
    recorded: dict[str, str] = {}
    for name in sorted(names):
        events = deploy_store.plugin_live_history(conn, name)
        if not events:
            continue
        last = events[-1]
        recorded[name] = ("absent" if last["to"] == "absent"
                          or last["kind"] == "retire_plain" else last["to"])
    return recorded


def deployed_from_live(conn: sqlite3.Connection, file_population: FilePopulation
                       ) -> tuple[Population, list[str] | None, str | None]:
    """配備ガードを排他で持ったまま、tx の中で配備済み母集団を確定する。
    母集団の正は live のファイルの状態で、DB の履歴からは復元しない。
    切替が途中 (未完了の記録がある) なら第 3 戻り値に理由を返す。DB の最後の
    記録と live が食い違う名前は人の直接操作として第 2 戻り値に返す
    (母集団は live のまま採用する)。記録が読めず食い違いを調べられないときは
    第 2 戻り値が `None`。"""
    in_flight = _in_flight_deploy_ops(conn)
    if in_flight:
        return Population(), [], "deploy_operation_in_flight"
    unreadable = [row.split(":", 1)[1]
                  for row in file_population.deployed.skipped]
    if unreadable:
        # 配備済みは同名の更新と別名の重複判定の根拠。読めない entry を外した
        # まま続けると、その名前と同ロジックの別名候補を素通しにする。
        return Population(), [], f"deployed_unreadable:{unreadable[0]}"
    drift: list[str] = []
    live_state = file_population.live_state
    if live_state is not None:
        try:
            recorded = _recorded_live(conn)
        except (ValueError, KeyError, TypeError):
            # 検出は助言的: 記録が読めなければ drift 検出だけを諦める。
            return file_population.deployed, None, None
        for name, expected in recorded.items():
            if live_state.get(name, "absent") != expected:
                drift.append(name)
    return file_population.deployed, drift, None


def base_unavailable(*populations: Population) -> bool:
    """比較基盤が全部使えない: 組み立てを試みた行があるのに 1 件も使えない。
    母集団が元から空 (初回起動など) の場合は使えない状態ではない。"""
    attempted = sum(p.attempted for p in populations)
    usable = sum(len(p.entries) for p in populations)
    return attempted > 0 and usable == 0


def find_duplicate_logic(*, name: str, kind: str | None = None, logic_hash: str,
                         populations: Iterable[Population]
                         ) -> IdentityEntry | None:
    """別名の配備済み・example・却下済み・降格済みなどとロジックが一致する
    最初の行。配備済みの同名旧版 (と、その版として承認された台帳の行) との
    一致は更新なので数えない。"""
    populations = list(populations)
    deployed_entries = [
        entry for population in populations for entry in population.entries
        if entry.source == "deployed"]
    deployed_names = {entry.name for entry in deployed_entries}
    for population in populations:
        for entry in population.entries:
            if entry.logic_hash != logic_hash:
                continue
            if (entry.source == "archive" and entry.outcome == "family_redirect"
                    and entry.family_key is not None and any(
                        deployed.name == name and deployed.kind == kind
                        and deployed.family_key == entry.family_key
                        for deployed in deployed_entries)):
                continue
            if entry.name == name and name in deployed_names and (
                    entry.source == "deployed"
                    or (entry.source == "archive"
                        and entry.outcome == "approved")):
                continue
            return entry
    return None


def find_same_family(*, name: str, kind: str | None, family_key: str | None,
                     deployed: Population) -> IdentityEntry | None:
    """配備済みの同 kind の別名 plugin で、仮説族が一致する最初の行。
    同名は更新なので数えない。却下済み・降格済みの族は含めない。"""
    if family_key is None:
        return None
    for entry in deployed.entries:
        if (entry.source == "deployed" and entry.kind == kind
                and entry.name != name and entry.family_key == family_key):
            return entry
    return None
