"""評価用履歴の検査証跡 (承認 payload の ``history_guard``) の生成と照合。

gate が ready を得たとき、検査の結果をこの形で payload に残す。承認・再評価の
重複判定・rollback は、現在の DB を測り直さず、提出時のこの証跡を照合する。

無足日一覧は証跡の dataset・主 pair・評価区間 ``[holdout_start, holdout_end)`` に
当たる項目だけを数える。一覧全体の digest は照合に使わない (別 dataset・別 pair・
区間外の日の変更で承認を落とさないため)。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Mapping

from agentic_fx.backtest import history_health as hh
from agentic_fx.backtest.dataset import DatasetError, HistoryDataset

CHECK_OK = "ok"
# 証跡が確定的に現在の方針に合わない (通常依頼は失効、再評価依頼は更新待ち)。
CHECK_INVALID = "invalid"
# 一覧・設定の読取失敗など、合うかどうか判定できない (依頼は保留)。
CHECK_UNAVAILABLE = "unavailable"

_ONE_DAY = timedelta(days=1)
_PACKAGE_ROOT = Path(__file__).resolve().parents[3]
_EVIDENCE_KEYS = frozenset({
    "version", "verdict", "checked_at", "dataset", "pairs", "holdout_start", "holdout_end",
    "latest_confirmed_bar", "market_present", "market_expected", "missing_days", "worst_day",
    "calendar_version", "threshold_snapshot", "no_bars_entries", "no_bars_digest"})


def no_bars_path(root: Path | None = None) -> Path:
    """無足日一覧の実ファイル。repository root 基準で、カレントディレクトリには依らない。"""
    return (Path(root) if root is not None else _PACKAGE_ROOT) / hh.NO_BARS_RELPATH


def day_intersects(day: date, start: datetime, end: datetime) -> bool:
    """UTC 日 ``day`` が ``[start, end)`` と交わるか。"""
    day_start = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
    return day_start < end and day_start + _ONE_DAY > start


def scope_no_bars(entries: Iterable[hh.NoBarsEntry], *, dataset: HistoryDataset,
                  pairs: Iterable[str], start: datetime, end: datetime
                  ) -> tuple[hh.NoBarsEntry, ...]:
    """証跡の対象範囲 (dataset・主 pair・評価区間) に当たる項目を安定順で返す。"""
    pair_set = set(pairs)
    kept = [e for e in entries
            if (e.source, e.base_interval) == (dataset.source, dataset.base_interval)
            and e.symbol in pair_set and day_intersects(e.date_utc, start, end)]
    return tuple(sorted(kept, key=lambda e: (e.symbol, e.date_utc)))


def scoped_digest(entries: Iterable[hh.NoBarsEntry]) -> str:
    return hh.no_bars_digest(tuple(entries))


def build_evidence(health: hh.HistoryHealth) -> dict:
    """ready の ``HistoryHealth`` から payload に載せる証跡を作る (JSON 安全)。

    人間 (運用者) 専用: 日時・本数・無足日の理由と承認者を含む。agent の観測面へ
    渡さない。"""
    if not health.ready or health.holdout_start is None or health.holdout_end is None:
        raise ValueError("evidence requires a ready history check")
    evidence = dict(health.to_operator_evidence())
    scoped = scope_no_bars(health.applied_no_bars, dataset=health.dataset,
                           pairs=[p.symbol for p in health.pairs],
                           start=health.holdout_start, end=health.holdout_end)
    evidence.pop("no_bars_applied", None)
    evidence["no_bars_entries"] = [e.as_dict() for e in scoped]
    evidence["no_bars_digest"] = scoped_digest(scoped)
    return evidence


def policy_key(evidence: object) -> tuple | None:
    """証跡が前提とした方針 (規則版・暦版・閾値)。読めなければ None。"""
    if not isinstance(evidence, Mapping):
        return None
    thresholds = evidence.get("threshold_snapshot")
    if not isinstance(thresholds, Mapping):
        return None
    return (evidence.get("version"), evidence.get("calendar_version"),
            tuple(sorted((str(k), str(v)) for k, v in thresholds.items())))


def current_policy_key() -> tuple:
    return policy_key({"version": hh.GUARD_VERSION, "calendar_version": hh.CALENDAR_VERSION,
                       "threshold_snapshot": hh.threshold_snapshot()})


@dataclass(frozen=True)
class EvidenceCheck:
    status: str
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.status == CHECK_OK


def _invalid(detail: str) -> EvidenceCheck:
    return EvidenceCheck(CHECK_INVALID, detail)


def _parse_dt(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo is not None else None


def _shape_problem(evidence: Mapping, pairs: list) -> str | None:
    """pair 別の欄が主 pair と同じ鍵集合で、ready と矛盾しない形かだけを見る。"""
    keys = set(pairs)
    for field in ("latest_confirmed_bar", "market_present", "market_expected",
                  "missing_days", "worst_day"):
        value = evidence.get(field)
        if not isinstance(value, Mapping) or set(value) != keys:
            return f"{field} does not cover exactly the primary pairs"
    for pair in pairs:
        expected, present = evidence["market_expected"][pair], evidence["market_present"][pair]
        if (type(expected) is not int or type(present) is not int
                or expected <= 0 or not 0 <= present <= expected):
            return "market counts are not valid"
        if _parse_dt(evidence["latest_confirmed_bar"][pair]) is None:
            return "latest_confirmed_bar is not an aware time"
        if evidence["missing_days"][pair] != []:
            return "a ready evidence cannot list missing days"
    return None


def payload_pairs(payload: Mapping) -> "list[str] | None":
    """payload が評価した pair の集合。決められなければ None。

    ``pairs`` が無い旧い payload は、metrics / in_sample の鍵から推定する。証跡照合の
    入口はすべてこの規則で pair を決める。"""
    pairs = payload.get("pairs")
    if isinstance(pairs, list) and all(isinstance(p, str) for p in pairs):
        return pairs
    for key in ("metrics", "in_sample"):
        value = payload.get(key)
        if (isinstance(value, dict) and value
                and all(isinstance(v, dict) for v in value.values())):
            return list(value)
    return None


def verify_payload(payload: Mapping, *, no_bars_path: Path) -> EvidenceCheck:
    """payload の ``history_guard`` を、その payload の評価対象と現在の方針に照合する。"""
    return verify_evidence(
        payload.get("history_guard"), eval_source=payload.get("eval_source"),
        base_interval=payload.get("base_interval"), pairs=payload_pairs(payload),
        no_bars_path=no_bars_path)


def verify_evidence(evidence: object, *, eval_source: object = None,
                    base_interval: object = None, pairs: object = None,
                    no_bars_path: Path) -> EvidenceCheck:
    """提出時の証跡を現在の方針と照合する。DB は測り直さない。

    見るもの: 証跡の存在と形、``verdict=ready``、規則版・暦版・閾値の完全一致、
    payload の評価対象 (dataset・pair 集合) との一致、証跡の対象範囲に当たる
    無足日項目の一致。時計が進んだだけでは落ちない。

    一覧が読めない・不正なときは ``unavailable`` (修復で元の証跡が合う可能性が
    あるため、確定した不一致とは分ける)。それ以外の不一致は ``invalid``。
    """
    if not isinstance(evidence, dict):
        return _invalid("no evidence")
    if set(evidence) != _EVIDENCE_KEYS:
        return _invalid("evidence has an unexpected set of fields")
    version = evidence["version"]
    if type(version) is not int or version != hh.GUARD_VERSION:
        return _invalid("guard version differs")
    if evidence["verdict"] != hh.HISTORY_READY:
        return _invalid("evidence is not ready")
    if evidence["calendar_version"] != hh.CALENDAR_VERSION:
        return _invalid("calendar version differs")
    if evidence["threshold_snapshot"] != hh.threshold_snapshot():
        return _invalid("thresholds differ")
    raw_dataset = evidence["dataset"]
    if not isinstance(raw_dataset, dict) or set(raw_dataset) != {"source", "base_interval"}:
        return _invalid("dataset is malformed")
    try:
        dataset = HistoryDataset(raw_dataset["source"], raw_dataset["base_interval"])
        dataset.require_configured()
    except (DatasetError, TypeError):
        return _invalid("dataset is not usable")
    pair_list = evidence["pairs"]
    if (not isinstance(pair_list, list) or not pair_list
            or not all(isinstance(p, str) and p for p in pair_list)
            or len(set(pair_list)) != len(pair_list)):
        return _invalid("pairs are malformed")
    if eval_source is not None and eval_source != dataset.source:
        return _invalid("evidence dataset differs from the evaluated source")
    if base_interval is not None and base_interval != dataset.base_interval:
        return _invalid("evidence dataset differs from the evaluated interval")
    if pairs is not None:
        if not isinstance(pairs, (list, tuple, set, frozenset)) or set(pairs) != set(pair_list):
            return _invalid("evidence pairs differ from the evaluated pairs")
    start, end = _parse_dt(evidence["holdout_start"]), _parse_dt(evidence["holdout_end"])
    if start is None or end is None or start >= end:
        return _invalid("evaluation range is malformed")
    if _parse_dt(evidence["checked_at"]) is None:
        return _invalid("checked_at is malformed")
    problem = _shape_problem(evidence, pair_list)
    if problem is not None:
        return _invalid(problem)

    stored_entries, stored_digest = evidence["no_bars_entries"], evidence["no_bars_digest"]
    if not isinstance(stored_entries, list) or not isinstance(stored_digest, str):
        return _invalid("no-bars section is malformed")
    try:
        current = hh.load_no_bars(no_bars_path)
    except hh.NoBarsConfigError:
        return EvidenceCheck(CHECK_UNAVAILABLE, "no-bars list cannot be read")
    scoped = scope_no_bars(current.entries, dataset=dataset, pairs=pair_list,
                           start=start, end=end)
    if (stored_entries != [e.as_dict() for e in scoped]
            or stored_digest != scoped_digest(scoped)):
        return _invalid("no-bars entries in the evaluated range differ")
    return EvidenceCheck(CHECK_OK)
