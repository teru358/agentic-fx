"""候補の質検査のうち、ロジック等価 (logic_hash) と仮説族 (family_key) の
比較に使う母集団の組み立てと判定。

母集団は 3 つ。配備済みの plugin、同梱の example、候補台帳の他 mission の行。
入出力は純粋な値で、DB の書き込みも activity への出力もここでは行わない
(呼び出し側が結果を記録する)。"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from agentic_fx.plugin.logic_identity import (
    LOGIC_HASH_PREFIX, identity_of_dir, read_identity)
from agentic_fx.store import plugin_deploy_ops as deploy_store
from agentic_fx.store.db import OPEN_PHASE_SQL

# 質検査で降格・拒否した候補について、agent 面に出す唯一の文言。
# 理由・他候補の名前や hash・成績は含めない。
QUALITY_NOTICE = "理由非公開。同一候補を再提出しない"

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
    """配備済みの出発点と example (ファイルから作る側)。tx の外で組み立てる。
    配備済みは、配備切替の記録が無い plugin の分だけがここから使われ、
    記録のある plugin は tx の中で `deployed_from_db` が確定する。
    `unavailable` は組み立て自体が失敗したときの理由 (空でなければ比較不能)。"""
    deployed: Population
    examples: Population
    unavailable: str | None = None


def load_deployed(metas: Iterable) -> Population:
    population = Population()
    for meta in metas:
        population.attempted += 1
        logic_hash, family_key = identity_of_dir(
            Path(meta.path), name=meta.name)
        if logic_hash is None:
            population.skipped.append(f"deployed:{meta.name}")
            continue
        population.entries.append(IdentityEntry(
            "deployed", meta.name, meta.kind, logic_hash, family_key))
    return population


def load_file_population(metas: Iterable, examples_dir: Path | None
                         ) -> FilePopulation:
    """ファイルの読込が失敗しても例外にせず、`unavailable` に理由を残す
    (比較できないものを「母集団が空」と取り違えない)。"""
    try:
        return FilePopulation(
            deployed=load_deployed(metas), examples=load_examples(examples_dir))
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


def _history_names(conn: sqlite3.Connection) -> list[str]:
    names = {row[0] for row in conn.execute(
        "SELECT name FROM plugin_switch_journal "
        "WHERE phase='decided' AND switch_required=1")}
    names |= {row[0] for row in conn.execute(
        "SELECT name FROM plugin_deploy_ops WHERE "
        "(phase='done' AND op_kind IN "
        "('disable_symlink','rollback_symlink','retire_plain')) "
        "OR (phase='needs_retry' AND fail_reason='verify_failed' "
        "AND from_kind='symlink')")}
    return sorted(names)


def deployed_from_db(conn: sqlite3.Connection, base: Population, *,
                     plugins_root: Path) -> tuple[Population, str | None]:
    """tx の中で現在の配備済み母集団を確定する。配備の切替 (承認・無効化・
    rollback・退避) は DB に記録されるので、その最後の記録が今の配備を決める。
    記録のある plugin は、記録が指す不変の版ディレクトリから identity を作る。
    記録の無い plugin (人が直接置いたもの) だけ、mission 開始時の `base` を使う。
    切替が途中 (未完了の記録がある) なら今の配備を一意に決められないので、
    第 2 戻り値に理由を返す。"""
    in_flight = _in_flight_deploy_ops(conn)
    if in_flight:
        return Population(), "deploy_operation_in_flight"
    names = set(_history_names(conn))
    population = Population()
    for entry in base.entries:
        if entry.name not in names:
            population.entries.append(entry)
            population.attempted += 1
    for skipped in base.skipped:
        if skipped.split(":", 1)[-1] not in names:
            population.skipped.append(skipped)
            population.attempted += 1
    for name in sorted(names):
        events = deploy_store.plugin_live_history(conn, name)
        if not events:
            continue
        last = events[-1]
        if last["to"] == "absent" or last["kind"] == "retire_plain":
            continue
        population.attempted += 1
        logic_hash, family_key, config = read_identity(
            plugins_root / last["to"], name=name)
        if logic_hash is None:
            population.skipped.append(f"deployed:{name}")
            continue
        kind = config.get("kind") if isinstance(config, dict) else None
        population.entries.append(IdentityEntry(
            "deployed", name, kind, logic_hash, family_key))
    return population, None


def base_unavailable(*populations: Population) -> bool:
    """比較基盤が全部使えない: 組み立てを試みた行があるのに 1 件も使えない。
    母集団が元から空 (初回起動など) の場合は使えない状態ではない。"""
    attempted = sum(p.attempted for p in populations)
    usable = sum(len(p.entries) for p in populations)
    return attempted > 0 and usable == 0


def find_duplicate_logic(*, name: str, logic_hash: str,
                         populations: Iterable[Population]
                         ) -> IdentityEntry | None:
    """別名の配備済み・example・却下済み・降格済みなどとロジックが一致する
    最初の行。配備済みの同名旧版 (と、その版として承認された台帳の行) との
    一致は更新なので数えない。"""
    populations = list(populations)
    deployed_names = {
        entry.name for population in populations
        for entry in population.entries if entry.source == "deployed"}
    for population in populations:
        for entry in population.entries:
            if entry.logic_hash != logic_hash:
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
        if (entry.kind == kind and entry.name != name
                and entry.family_key == family_key):
            return entry
    return None
