"""承認依頼の変更内容 (前版との差) を読み出し時に計算する。

材料は承認 payload・配備ジャーナル・版 dir だけで、計算結果はどこにも保存しない。
版 dir の bytes は :mod:`safe_open` が hash 検証まで済ませたものだけを使い、表示する
全ての値は :mod:`redactor` を通す。daemon は escape しない構造化 diff を返し、escape と
最終の表示上限は表示面が受け持つ。
"""
from __future__ import annotations

import difflib
import json
import math
import re
import sys
from dataclasses import dataclass
from pathlib import Path

import yaml

from . import change_limits as L
from . import redactor, safe_open

_BASELINE_CLASSES = (L.CLASS_NEW, L.CLASS_UPDATED, L.CLASS_SAME, L.CLASS_UNKNOWN)
_KEY_RE = re.compile(L.KEY_PATTERN)
_PAIR_RE = re.compile(r"[A-Za-z0-9_/.-]{1,16}")
_TOP_LEVEL_SCALARS = ("pairs", "timeframe", "exit_mode", "max_bars")
_NO_VALUE = object()
_MISSING = object()


@dataclass(frozen=True, slots=True)
class ChangeResult:
    summary: dict
    diff: dict | None


# ---------------------------------------------------------------- 候補と比較対象の特定

def _candidate_source(plugins_root: Path, status: str, payload: dict,
                      name: str) -> safe_open.Source:
    artifact_hash = payload.get("artifact_hash")
    mission_id = payload.get("mission_id")
    if status == "pending":
        origin = payload.get("candidate_origin")
        path = payload.get("candidate_path")
        if origin == "staging":
            if type(mission_id) is not int or path != f"plugins/_staging/{mission_id}/{name}":
                raise safe_open.SourceError(L.UNSAFE_SOURCE)
            source = safe_open.staging_source(mission_id, name)
        elif origin == "human":
            if path != f"plugins/_human/{name}":
                raise safe_open.SourceError(L.UNSAFE_SOURCE)
            source = safe_open.human_source(name)
        else:
            raise safe_open.SourceError(L.UNSAFE_SOURCE)
        _lexical_locator_check(plugins_root, origin, path, name)
        return source
    if status == "approved":
        return safe_open.version_source(name, artifact_hash)
    if mission_id is None:
        raise safe_open.SourceError(L.UNAVAILABLE)
    return safe_open.archive_source(mission_id, artifact_hash)


def _lexical_locator_check(plugins_root: Path, origin: str, path: str, name: str) -> None:
    from agentic_fx.plugin.switch import CandidateMissingError, resolve_candidate_dir
    try:
        resolve_candidate_dir(plugins_root, candidate_origin=origin, candidate_path=path,
                              name=name)
    except CandidateMissingError:
        raise safe_open.SourceError(L.UNAVAILABLE) from None
    except (ValueError, OSError):
        raise safe_open.SourceError(L.UNSAFE_SOURCE) from None


@dataclass(frozen=True, slots=True)
class _Baseline:
    classification: str
    hash: str | None = None
    files: safe_open.SourceFiles | None = None
    reason: str | None = None


def _from_target(plugins_root: Path, name: str, target: object,
                 candidate_hash: str, *, need_files: bool) -> _Baseline:
    digest = safe_open.baseline_hash_from_target(name, target)
    if digest is None:
        return _Baseline(L.CLASS_UNKNOWN, reason=L.UNSAFE_SOURCE)
    if digest == candidate_hash:
        return _Baseline(L.CLASS_SAME, digest)
    if not need_files:
        return _Baseline(L.CLASS_UPDATED, digest)
    try:
        files = safe_open.read_source(plugins_root, safe_open.version_source(name, digest),
                                      digest)
    except safe_open.SourceError as exc:
        return _Baseline(L.CLASS_UNKNOWN, reason=exc.code)
    return _Baseline(L.CLASS_UPDATED, digest, files)


def _baseline(plugins_root: Path, status: str, name: str, candidate_hash: str,
              journal_rows: list[dict], *, need_files: bool) -> _Baseline:
    if status == "pending":
        try:
            live = safe_open.inspect_live(plugins_root, name)
        except safe_open.SourceError as exc:
            return _Baseline(L.CLASS_UNKNOWN, reason=exc.code)
        if live.kind == "absent":
            return _Baseline(L.CLASS_NEW)
        if live.kind == "dir":
            return _Baseline(L.CLASS_UNKNOWN)
        if live.kind != "symlink":
            return _Baseline(L.CLASS_UNKNOWN, reason=L.UNSAFE_SOURCE)
        return _from_target(plugins_root, name, live.target, candidate_hash,
                            need_files=need_files)
    decided = [row for row in journal_rows if row.get("phase") == "decided"]
    if not decided:
        return _Baseline(L.CLASS_UNKNOWN)
    decided.sort(key=lambda row: row["op_id"])
    if len({(row["old_kind"], row["old_target"]) for row in decided}) != 1:
        return _Baseline(L.CLASS_UNKNOWN)
    last = decided[-1]
    if last["old_kind"] == "absent":
        return _Baseline(L.CLASS_NEW)
    if last["old_kind"] == "symlink":
        return _from_target(plugins_root, name, last["old_target"], candidate_hash,
                            need_files=need_files)
    return _Baseline(L.CLASS_UNKNOWN, reason=L.UNSAFE_SOURCE)


# ---------------------------------------------------------------- params の差分

def _scalar_text(value: object) -> str:
    if type(value) is str:
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _flatten(value: object, prefix: str, depth: int, out: dict, counter: list,
             limit: int = L.FLATTEN_MAX_ITEMS) -> None:
    """入れ子を ``a.b.c`` の key へ決定的に展開する。最大深さを超えた入れ子は 1 つの項目に
    まとめ、最大項目数を超えた分は ``counter[0]`` に件数だけ積む。"""
    if type(value) is dict and depth < L.FLATTEN_MAX_DEPTH and value:
        for key in sorted(value, key=str):
            _flatten(value[key], f"{prefix}.{key}", depth + 1, out, counter, limit)
        return
    if len(out) >= limit:
        counter[0] += 1
        return
    out[prefix] = _scalar_text(value)


def _config_sections(config: object) -> dict:
    sections: dict = {}
    if type(config) is not dict:
        return sections
    if "params" in config:
        sections["params"] = config["params"]
    indicators = config.get("indicators")
    if type(indicators) is dict:
        for alias in sorted(indicators, key=str):
            entry = indicators[alias]
            if type(entry) is dict and "params" in entry:
                sections[f"indicators.{alias}.params"] = entry["params"]
    for key in _TOP_LEVEL_SCALARS:
        if key in config:
            sections[key] = config[key]
    return sections


def _flatten_config(config: object, limit: int = L.FLATTEN_MAX_ITEMS) -> tuple[dict, int]:
    out: dict = {}
    counter = [0]
    for prefix, value in _config_sections(config).items():
        _flatten(value, prefix, 0 if type(value) is dict else L.FLATTEN_MAX_DEPTH, out, counter,
                 limit)
    return out, counter[0]


def _parse_config(data: bytes) -> object:
    """alias を含む YAML (展開で膨らませられる) と、読めない YAML は ``_NO_VALUE``。"""
    text = redactor.decode_or_none(data)
    if text is None:
        return _NO_VALUE
    try:
        if any(isinstance(event, yaml.AliasEvent) for event in yaml.parse(text)):
            return _NO_VALUE
        return yaml.safe_load(text)
    except Exception:  # noqa: BLE001 — 判定できない config は params 差分を出さない
        return _NO_VALUE


def _param_value(key: str, text: str | None) -> str | None:
    if text is None:
        return None
    leaf = key.rsplit(".", 1)[-1]
    if redactor.is_credential_key(leaf):
        return L.REDACTED
    if redactor.is_account_key(leaf):
        shown = redactor.redact_value(f"{leaf}={text}")
        return shown[len(leaf) + 1:] if shown.startswith(f"{leaf}=") else L.REDACTED
    return redactor.redact_value(text)


def _params_diff(old_data: bytes | None, new_data: bytes) -> dict:
    result = {"items": [], "omitted": 0, "invalid_keys": 0, "flatten_omitted": 0,
              "reason": None}
    if old_data is None:
        return result
    old_config, new_config = _parse_config(old_data), _parse_config(new_data)
    if old_config is _NO_VALUE or new_config is _NO_VALUE:
        result["reason"] = L.REDACTED_CODE
        return result
    # 旧・新の key の和集合を並べて先頭だけを両側に当てる (片側だけ切ると、同じ key が
    # 一方では「なし」に見える)
    old_flat, _ = _flatten_config(old_config, sys.maxsize)
    new_flat, _ = _flatten_config(new_config, sys.maxsize)
    union = sorted(set(old_flat) | set(new_flat))
    result["flatten_omitted"] = max(0, len(union) - L.FLATTEN_MAX_ITEMS)
    for key in union[:L.FLATTEN_MAX_ITEMS]:
        before, after = old_flat.get(key), new_flat.get(key)
        if before == after:
            continue
        if _KEY_RE.fullmatch(key) is None:
            result["invalid_keys"] += 1
            continue
        if len(result["items"]) >= L.PARAMS_DIFF_MAX:
            result["omitted"] += 1
            continue
        result["items"].append({"key": redactor.redact_value(key),
                                "old": _param_value(key, before),
                                "new": _param_value(key, after)})
    return result


# ---------------------------------------------------------------- 構造化 diff

def _lines_of(text: str) -> list[str]:
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def _hunk_header(a_start: int, a_len: int, b_start: int, b_len: int) -> str:
    def span(start: int, length: int) -> str:
        if length == 1:
            return f"{start}"
        return f"{start - 1 if length == 0 else start},{length}"
    return f"@@ -{span(a_start, a_len)} +{span(b_start, b_len)} @@"


def _redact_line(line: str, spans: list | None = None) -> str:
    return redactor.redact_value(line, spans)


def _line_spans(path: str, text: str) -> list | None:
    """Python ソースの行ごとの文字列・コメントの桁範囲。他のファイルと読めないソースは None
    (全行を自由文として扱う)。"""
    return redactor.python_text_spans(text) if path.endswith(".py") else None


def _diff_degraded(old: list[str], new: list[str]) -> bool:
    """差分の計算を省いて全置換にするか (行数が多く、二次の計算量になるとき)。"""
    return (len(old) > L.DIFF_MAX_LINES or len(new) > L.DIFF_MAX_LINES
            or len(old) * len(new) > L.DIFF_MAX_CELLS)


def _spans_at(spans: list | None, index: int) -> list | None:
    return None if spans is None or index >= len(spans) else spans[index]


def _hunks(old: list[str], new: list[str], old_spans: list | None = None,
           new_spans: list | None = None) -> list[dict]:
    if _diff_degraded(old, new):
        groups = [[("replace", 0, len(old), 0, len(new))]] if old or new else []
    else:
        matcher = difflib.SequenceMatcher(None, old, new, autojunk=False)
        groups = matcher.get_grouped_opcodes(L.DIFF_CONTEXT)
    hunks = []
    for group in groups:
        first, last = group[0], group[-1]
        lines: list[str] = []
        for tag, i1, i2, j1, j2 in group:
            if tag == "equal":
                lines += [" " + _redact_line(old[i], _spans_at(old_spans, i))
                          for i in range(i1, i2)]
                continue
            if tag in ("replace", "delete"):
                lines += ["-" + _redact_line(old[i], _spans_at(old_spans, i))
                          for i in range(i1, i2)]
            if tag in ("replace", "insert"):
                lines += ["+" + _redact_line(new[j], _spans_at(new_spans, j))
                          for j in range(j1, j2)]
        a_len, b_len = last[2] - first[1], last[4] - first[3]
        hunks.append({"header": _hunk_header(first[1] + 1, a_len, first[3] + 1, b_len),
                      "lines": lines})
    return hunks


def _file_diff(path: str, old: bytes | None, new: bytes) -> dict:
    new_text = redactor.decode_or_none(new)
    old_text = redactor.decode_or_none(old) if old is not None else ""
    if new_text is None or old_text is None:
        return {"path": path, "status": "redacted", "reason": L.REDACTED_CODE,
                "hunks": [], "omitted_lines": 0}
    if old is not None and old == new:
        return {"path": path, "status": "unchanged", "reason": None, "hunks": [],
                "omitted_lines": 0}
    old_lines, new_lines = _lines_of(old_text), _lines_of(new_text)
    entry = {"path": path, "status": "added" if old is None else "changed", "reason": None,
             "hunks": _hunks(old_lines, new_lines, _line_spans(path, old_text),
                             _line_spans(path, new_text)), "omitted_lines": 0}
    if _diff_degraded(old_lines, new_lines):
        entry["diff_degraded"] = L.DIFF_DEGRADED_TOO_MANY_LINES
    return entry


def _serialized_bytes(response: dict) -> int:
    """応答全体を JSON にした byte 数。非 ASCII を ``\\uXXXX`` に escape する側 (最大 6 倍)
    で数えるので、実際の送出形式がどちらでも上限を超えない。"""
    return len(json.dumps(response).encode())


def fit_response_to_bytes(response: dict, limit: int | None = None) -> int:
    """応答全体の JSON byte 数が ``limit`` 以下になるよう、diff の末尾の行を省略して
    ``omitted_lines`` に載せる。残す行数を、応答全体を実際に直列化した大きさで二分探索して
    決める (構造の overhead と escape の増幅を見積もらない)。省略した行の合計を返す。"""
    limit = L.API_DIFF_BYTES if limit is None else limit
    diff = response["diff"]
    originals = [(entry, entry["hunks"]) for entry in diff["files"]]
    total = sum(1 + len(hunk["lines"]) for _, hunks in originals for hunk in hunks)

    def apply(keep: int) -> int:
        omitted_total = 0
        for entry, hunks in originals:
            kept: list[dict] = []
            omitted = 0
            for hunk in hunks:
                lines = hunk["lines"]
                if keep <= 0:
                    omitted += len(lines)
                    continue
                keep -= 1
                shown = min(keep, len(lines))
                keep -= shown
                omitted += len(lines) - shown
                if shown or not lines:
                    kept.append({"header": hunk["header"], "lines": lines[:shown]})
            entry["hunks"] = kept
            entry["omitted_lines"] = omitted
            omitted_total += omitted
        diff["omitted_lines"] = omitted_total
        return omitted_total

    if apply(total) == 0 and _serialized_bytes(response) <= limit:
        return 0
    low, high = 0, total
    while low < high:
        middle = (low + high + 1) // 2
        apply(middle)
        if _serialized_bytes(response) <= limit:
            low = middle
        else:
            high = middle - 1
    omitted_total = apply(low)
    if _serialized_bytes(response) > limit:
        _shrink_to_reason_only(response, limit)
    return omitted_total


def _shrink_to_reason_only(response: dict, limit: int) -> None:
    """diff の行を全て省いても ``limit`` を超えるとき、diff の file 一覧、申告、params の
    項目の順に落とす。それでも超えるなら理由だけの応答にして、必ず ``limit`` 以下にする。"""
    diff = response["diff"]
    diff["files"] = []
    steps = (lambda: response.update(claims={}),
             lambda: response["change"].get("params", {}).update(items=[])
             if isinstance(response["change"].get("params"), dict) else None)
    for step in (None, *steps):
        if step is not None:
            step()
        if _serialized_bytes(response) <= limit:
            return
    change = response["change"]
    response["change"] = {"classification": change.get("classification"),
                          "reason": L.TOO_LARGE}
    response["claims"] = {}


# ---------------------------------------------------------------- 成績と申告

def _metric_number(value: object) -> int | float | None:
    if type(value) is int:
        return value
    if type(value) is float and math.isfinite(value):
        return value
    return None


def _metric_row(pair: object, metrics: object) -> dict | None:
    if type(metrics) is not dict:
        return None
    return {"pair": (pair if type(pair) is str and _PAIR_RE.fullmatch(pair) else None),
            "trades": _metric_number(metrics.get("trades")),
            "pf": _metric_number(metrics.get("pf")),
            "avg_r": _metric_number(metrics.get("avg_r"))}


def _performance(value: object) -> list[dict]:
    if type(value) is not dict or not value:
        return []
    if all(type(item) is dict for item in value.values()):
        rows = [_metric_row(pair, metrics) for pair, metrics in list(value.items())[:16]]
    else:
        rows = [_metric_row(None, value)]
    return [row for row in rows if row is not None]


def claims_of(payload: dict) -> dict:
    claims = payload.get("agent_claims")
    if type(claims) is not dict:
        claims = {key: payload[key] for key in ("selection_rationale", "summary")
                  if key in payload}
    result = {}
    for key in ("summary", "selection_rationale"):
        value = claims.get(key)
        result[key] = redactor.redact_value(value) if type(value) is str else None
    return result


# ---------------------------------------------------------------- 入口

def failure_summary(payload: dict, now: str, reason: str) -> dict:
    """計算が失敗したときの、候補の hash と成績・申告を含まない最小の要約。"""
    return {"name": None, "kind": None, "classification": L.CLASS_UNKNOWN,
            "candidate_hash": None, "baseline_hash": None, "computed_at": now,
            "reason": reason,
            "params": {"items": [], "omitted": 0, "invalid_keys": 0, "flatten_omitted": 0,
                       "reason": None},
            "performance": {"in_sample": [], "holdout": []}, "summary": None}


def compute_change(*, status: str, payload: dict, journal_rows: list[dict],
                   plugins_root: Path | None, now: str, include_diff: bool) -> ChangeResult:
    """承認 1 件の変更要約 (と、``include_diff`` なら構造化 diff) を計算する。例外にしない。"""
    name = payload.get("name")
    name = name if type(name) is str and safe_open.NAME_RE.fullmatch(name) else None
    kind = payload.get("kind")
    candidate_hash = payload.get("artifact_hash")
    if type(candidate_hash) is not str or safe_open.HASH_RE.fullmatch(candidate_hash) is None:
        candidate_hash = None
    claims = claims_of(payload)
    summary: dict = {
        "name": name, "kind": kind if kind in L.PLUGIN_KINDS else None,
        "classification": L.CLASS_UNKNOWN, "candidate_hash": candidate_hash,
        "baseline_hash": None, "computed_at": now, "reason": None,
        "params": {"items": [], "omitted": 0, "invalid_keys": 0, "flatten_omitted": 0,
                   "reason": None},
        "performance": {"in_sample": _performance(payload.get("in_sample")),
                        "holdout": _performance(payload.get("holdout"))},
        "summary": claims["summary"],
    }
    diff = {"files": [], "omitted_lines": 0} if include_diff else None

    def fail(code: str) -> ChangeResult:
        summary["reason"] = code
        return ChangeResult(summary, diff)

    if plugins_root is None:
        return fail(L.UNAVAILABLE)
    if name is None or candidate_hash is None:
        return fail(L.UNSAFE_SOURCE)
    try:
        source = _candidate_source(plugins_root, status, payload, name)
        candidate = safe_open.read_source(plugins_root, source, candidate_hash)
    except safe_open.SourceError as exc:
        return fail(exc.code)
    except Exception:  # noqa: BLE001 — 読めない理由は固定 code にして path を出さない
        return fail(L.UNSAFE_SOURCE)
    baseline = _baseline(plugins_root, status, name, candidate_hash, journal_rows,
                         need_files=True)
    summary["classification"] = baseline.classification
    summary["baseline_hash"] = baseline.hash
    summary["reason"] = baseline.reason
    old = baseline.files
    if baseline.classification == L.CLASS_UPDATED and old is not None:
        summary["params"] = _params_diff(old.config_yaml, candidate.config_yaml)
    if diff is not None and baseline.classification in (L.CLASS_NEW, L.CLASS_UPDATED,
                                                        L.CLASS_SAME):
        pairs = (("plugin.py", "plugin_py"), ("config.yaml", "config_yaml"),
                 ("test_plugin.py", "test_plugin"))
        if baseline.classification == L.CLASS_SAME:
            diff["files"] = [{"path": path, "status": "unchanged", "reason": None,
                              "hunks": [], "omitted_lines": 0} for path, _ in pairs]
        else:
            for path, attr in pairs:
                diff["files"].append(_file_diff(
                    path, getattr(old, attr) if old is not None else None,
                    getattr(candidate, attr)))
    return ChangeResult(summary, diff)
