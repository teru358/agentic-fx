"""Pure checks used by the indicator fixture gate."""
from __future__ import annotations

import ast
import math
import re
from dataclasses import dataclass
from typing import Any, Mapping

import numpy as np
import pandas as pd
import yaml

from agentic_fx import improve_notice_codes as notices

EXACT_ATOL = 1e-12
SCALE_ATOL = 1e-9
EXACT_RTOL = 1e-6
MIN_CORRELATION_ROWS = 30
MONOTONE_LEVEL_CORR = 0.999
MONOTONE_DIFF_CORR = 0.98
DEFAULT_SIMILAR_CORR = 0.98

# ISO 4217 plus the two metals accepted as currency-like pair legs.
ISO_4217 = (
    "aed", "afn", "all", "amd", "ang", "aoa", "ars", "aud", "awg", "azn",
    "bam", "bbd", "bdt", "bgn", "bhd", "bif", "bmd", "bnd", "bob", "bov",
    "brl", "bsd", "btn", "bwp", "byn", "bzd", "cad", "cdf", "che", "chf",
    "chw", "clp", "cny", "cop", "cou", "crc", "cup", "cve", "czk", "djf",
    "dkk", "dop", "dzd", "egp", "ern", "etb", "eur", "fjd", "fkp", "gbp",
    "gel", "ghs", "gip", "gmd", "gnf", "gtq", "gyd", "hkd", "hnl", "hrk",
    "htg", "huf", "idr", "ils", "inr", "iqd", "irr", "isk", "jmd", "jod",
    "jpy", "kes", "kgs", "khr", "kmf", "kpw", "krw", "kwd", "kyd", "kzt",
    "lak", "lbp", "lkr", "lrd", "lsl", "lyd", "mad", "mdl", "mga", "mkd",
    "mmk", "mnt", "mop", "mru", "mur", "mvr", "mwk", "mxn", "mxv", "myr",
    "mzn", "nad", "ngn", "nio", "nok", "npr", "nzd", "omr", "pab", "pen",
    "pgk", "php", "pkr", "pln", "pyg", "qar", "ron", "rsd", "rub", "rwf",
    "sar", "sbd", "scr", "sdg", "sek", "sgd", "shp", "sle", "sll", "sos",
    "srd", "ssp", "stn", "svc", "syp", "szl", "thb", "tjs", "tmt", "tnd",
    "top", "try", "ttd", "twd", "tzs", "uah", "ugx", "usd", "uyi", "uyu",
    "uzs", "ved", "ves", "vnd", "vuv", "wst", "xaf", "xcd", "xof", "xpf",
    "yer", "zar", "zmw", "zwl", "xau", "xag",
)
ALIASES = {
    "xau": ("gold",), "xag": ("silver",), "usoil": ("oil", "wti", "crude"),
    "us500": ("spx", "sp500"), "jp225": ("nikkei",), "jpy": ("yen",),
    "usd": ("dollar",), "eur": ("euro",),
}
REJECT_CODES = notices.INDICATOR_GATE_REJECT_CODES
WARNING_CODES = notices.INDICATOR_GATE_WARNING_CODES
MODE_CODES = notices.INDICATOR_GATE_MODE_CODES
LEGACY_SYMBOL_NAME = "legacy_symbol_name"
MONOTONE_DUPLICATE = "indicator_monotone_duplicate"
OUTPUT_OVERLAP = "indicator_output_overlap"
INDICATOR_SIMILAR = "indicator_similar"
PRICE_UNIT_TOKENS = frozenset({"pip", "pips", "pipsize", "ticksize"})
PRICE_UNIT_PHRASES = frozenset({
    "pip_value", "pip_size", "tick_size", "point_size", "price_level",
    "price_threshold", "price_offset", "price_distance", "price_step",
    "price_delta", "price_unit", "absolute_price", "abs_price", "min_price",
    "max_price",
})


@dataclass(frozen=True)
class Hit:
    place: str
    token: str


@dataclass(frozen=True)
class Verdict:
    ok: bool = True
    code: str = ""
    warnings: tuple[str, ...] = ()
    detail: str = ""


@dataclass(frozen=True)
class CompareResult(Verdict):
    similar_to: tuple[dict[str, str], ...] = ()
    # `similar_to` と同じ並びの、親専有レポート用の数値文 (agent 面には出さない)。
    measures: tuple[str, ...] = ()


def _tokens(value: str) -> list[str]:
    raw = [p for p in re.split(r"[^a-zA-Z0-9]+", value) if p]
    out: list[str] = []
    split: list[str] = []
    for word in raw:
        lowered = word.lower()
        out.append(lowered)
        # camelCase and acronym + Word (XMLParser) feed the single-letter run logic below;
        # acronym + lowercase (EURUSDmomentum) only adds extra tokens.
        for boundary, feeds_runs in (
                (r"(?<=[a-z])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", True),
                (r"(?<=[a-z])(?=[A-Z])|(?<=[A-Z]{2})(?=[a-z])", False)):
            pieces = re.sub(boundary, " ", word)
            pieces = re.sub(r"(?<=[a-zA-Z])(?=\d)|(?<=\d)(?=[a-zA-Z])", " ", pieces)
            parts = [p.lower() for p in pieces.split() if p]
            (split if feeds_runs else out).extend(parts)
    out.extend(split)
    # x_a_u has individual pieces after splitting; retain their intended word too.
    run: list[str] = []
    for p in split + [""]:
        if len(p) == 1:
            run.append(p)
        else:
            if len(run) > 1:
                out.append("".join(run))
            run = []
    return list(dict.fromkeys(out))


def symbol_tokens(symbols: list[str]) -> frozenset[str]:
    """Return normalized symbols, valid currency legs, and their closed aliases."""
    tokens: set[str] = set()
    currencies = frozenset(ISO_4217)
    for symbol in symbols:
        normalized = symbol.lower().split(".", 1)[0]
        tokens.add(normalized)
        if len(normalized) == 6 and normalized.isalpha():
            left, right = normalized[:3], normalized[3:]
            if left in currencies and right in currencies:
                tokens.update((left, right))
    for token in tuple(tokens):
        tokens.update(ALIASES.get(token, ()))
    return frozenset(tokens)


def _get(meta: Any, key: str, default: Any = None) -> Any:
    return meta.get(key, default) if isinstance(meta, Mapping) else getattr(meta, key, default)


def _param_hits(params: Any) -> list[Hit]:
    hits: list[Hit] = []
    if isinstance(params, Mapping):
        for key, value in params.items():
            hits.extend(Hit("param_key", token) for token in _tokens(str(key)))
            hits.extend(_param_hits(value))
    elif isinstance(params, (list, tuple)):
        for value in params:
            hits.extend(_param_hits(value))
    elif isinstance(params, str):
        hits.extend(Hit("param_value", token) for token in _tokens(params))
    return hits


def _docstring_nodes(tree: ast.AST) -> set[int]:
    found: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = node.body
            if (body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                found.add(id(body[0].value))
    return found


def _source_hits(source_text: str) -> list[Hit]:
    tree = ast.parse(source_text)
    docs = _docstring_nodes(tree)
    hits: list[Hit] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            place = "docstring" if id(node) in docs else "string_constant"
            hits.extend(Hit(place, token) for token in _tokens(node.value))
    return hits


def _config_strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, Mapping):
        return [text for item in value.values() for text in _config_strings(item)]
    if isinstance(value, (list, tuple)):
        return [text for item in value for text in _config_strings(item)]
    return []


def candidate_tokens(meta: Any, source_text: str, config_text: str) -> list[Hit]:
    """Extract normalized inspection tokens while retaining their source location."""
    hits = [Hit("name", token) for token in _tokens(str(_get(meta, "name", "")))]
    hits.extend(_param_hits(_get(meta, "params", {})))
    hits.extend(_source_hits(source_text))
    for text in _config_strings(yaml.safe_load(config_text)):
        hits.extend(Hit("config_value", token) for token in _tokens(text))
    return hits


def check_symbol_in_name(hits: list[Hit], symbols: frozenset[str], *,
                         grandfather_hits: list[Hit] | None = None) -> Verdict:
    matched = frozenset(hit for hit in hits if hit.token in symbols)
    if not matched:
        return Verdict()
    if grandfather_hits is not None:
        legacy = frozenset(hit for hit in grandfather_hits if hit.token in symbols)
        if matched <= legacy:
            return Verdict(warnings=(LEGACY_SYMBOL_NAME,))
    return Verdict(False, "symbol_in_name")


def _param_keys(params: Any) -> list[str]:
    keys: list[str] = []
    if isinstance(params, Mapping):
        for key, value in params.items():
            keys.append(str(key))
            keys.extend(_param_keys(value))
    elif isinstance(params, (list, tuple)):
        for value in params:
            keys.extend(_param_keys(value))
    return keys


def _key_phrases(key: str) -> list[str]:
    """Key words plus adjacent word pairs joined by an underscore (``tick_size``)."""
    spaced = re.sub(r"(?<=[a-z])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", " ", key)
    words = [w.lower() for w in re.split(r"[^a-zA-Z0-9]+", spaced) if w]
    pairs = ["_".join(words[i:i + 2]) for i in range(len(words) - 1)]
    return _tokens(key) + words + pairs


def check_param_price_unit(params: Any) -> Verdict:
    for key in _param_keys(params):
        for phrase in _key_phrases(key):
            if phrase in PRICE_UNIT_TOKENS or phrase in PRICE_UNIT_PHRASES:
                return Verdict(False, "param_price_unit")
    return Verdict()


def _allclose(a: Any, b: Any, *, rtol: float = EXACT_RTOL, atol: float = EXACT_ATOL) -> bool:
    """Element-wise |a-b| <= atol + rtol*max(|a|,|b|); NaN positions must coincide."""
    aa, bb = np.asarray(a, dtype=float), np.asarray(b, dtype=float)
    if aa.shape != bb.shape:
        return False
    nan_a, nan_b = np.isnan(aa), np.isnan(bb)
    if not np.array_equal(nan_a, nan_b):
        return False
    keep = ~nan_a
    x, y = aa[keep], bb[keep]
    same = x == y
    with np.errstate(invalid="ignore"):
        near = np.abs(x - y) <= atol + rtol * np.maximum(np.abs(x), np.abs(y))
    return bool(np.all(same | (near & np.isfinite(x) & np.isfinite(y))))


def classify_scale(base: pd.DataFrame, k100: pd.DataFrame, k001: pd.DataFrame,
                   vol100: pd.DataFrame, *, rtol: float = EXACT_RTOL) -> dict[str, tuple[str, str]]:
    result: dict[str, tuple[str, str]] = {}
    for col in base.columns:
        b = base[col]
        price = "neither"
        invariant = _allclose(k100[col], b, rtol=rtol, atol=SCALE_ATOL) and _allclose(k001[col], b, rtol=rtol, atol=SCALE_ATOL)
        proportional = _allclose(k100[col], b * 100, rtol=rtol, atol=SCALE_ATOL) and _allclose(k001[col], b * .01, rtol=rtol, atol=SCALE_ATOL)
        if invariant:
            price = "invariant"
        elif proportional:
            price = "proportional"
        volume = "invariant" if _allclose(vol100[col], b, rtol=rtol, atol=SCALE_ATOL) else "neither"
        result[str(col)] = (price, volume)
    return result


def finite_report(df_out: pd.DataFrame, warmup: int) -> dict[str, tuple[int, int]]:
    report: dict[str, tuple[int, int]] = {}
    for col in df_out.columns:
        values = pd.to_numeric(df_out[col], errors="coerce").iloc[warmup:]
        finite = values[np.isfinite(values)]
        report[str(col)] = (int(len(finite)), int(finite.nunique()))
    return report


def is_all_nan(report: tuple[int, int]) -> bool:
    return report[0] == 0


def is_sparse(report: tuple[int, int]) -> bool:
    return 0 < report[0] < MIN_CORRELATION_ROWS


def _exact(a: Any, b: Any) -> bool:
    """Exact match: same finite mask, at least one common finite point, symmetric tolerance."""
    try:
        if not isinstance(a, pd.Series) or not isinstance(b, pd.Series):
            return bool(np.isscalar(a) and np.isscalar(b) and np.isfinite(a) and np.isfinite(b)
                        and abs(a - b) <= EXACT_ATOL + EXACT_RTOL * max(abs(a), abs(b)))
        if a.index.has_duplicates or b.index.has_duplicates:
            return False
        a, b = a.align(b, join="outer")
        mask_a, mask_b = np.isfinite(a), np.isfinite(b)
        if not mask_a.any() or not np.array_equal(mask_a, mask_b):
            return False
        return bool(_allclose(a[mask_a], b[mask_b]))
    except (TypeError, ValueError):
        return False


def _common(a: pd.Series, b: pd.Series) -> tuple[np.ndarray, np.ndarray]:
    a, b = a.align(b, join="inner")
    mask = np.isfinite(a) & np.isfinite(b)
    return a[mask].to_numpy(dtype=float), b[mask].to_numpy(dtype=float)


def _pearson(a: np.ndarray, b: np.ndarray) -> float:
    if len(a) < 2 or np.std(a) == 0 or np.std(b) == 0:
        return math.nan
    return float(np.corrcoef(a, b)[0, 1])


def _rank(values: np.ndarray) -> np.ndarray:
    return pd.Series(values).rank(method="average").to_numpy(dtype=float)


def _spearman(a: np.ndarray, b: np.ndarray, *, rank_a: np.ndarray | None = None) -> float:
    return _pearson(_rank(a) if rank_a is None else rank_a, _rank(b))


@dataclass(frozen=True)
class _Prepared:
    """Per-candidate-column values computed once, reused for every deployed column."""
    series: pd.Series
    finite: pd.Series
    rank: np.ndarray
    diff: np.ndarray
    diff_rank: np.ndarray
    reusable: bool


def _prepare(series: pd.Series) -> _Prepared:
    finite = series[np.isfinite(series)]
    values = finite.to_numpy(dtype=float)
    diff = np.diff(values)
    reusable = (not series.index.has_duplicates and finite.index.is_monotonic_increasing
                and len(values) >= MIN_CORRELATION_ROWS)
    return _Prepared(series, finite, _rank(values), diff, _rank(diff), reusable)


def _pair_stats(prep: _Prepared, other: pd.Series) -> tuple[float, float, float] | None:
    """(level Spearman, diff Spearman, diff Pearson) over the common finite points."""
    if prep.series.index.has_duplicates or other.index.has_duplicates:
        return None
    rank_a = diff_a = diff_rank_a = None
    if prep.reusable:
        b = other.reindex(prep.finite.index).to_numpy(dtype=float)
        if np.isfinite(b).all():
            a, rank_a, diff_a, diff_rank_a = (prep.finite.to_numpy(dtype=float), prep.rank,
                                              prep.diff, prep.diff_rank)
        else:
            a, b = _common(prep.series, other)
    else:
        a, b = _common(prep.series, other)
    if len(a) < MIN_CORRELATION_ROWS:
        return None
    if diff_a is None:
        diff_a = np.diff(a)
    diff_b = np.diff(b)
    return (_spearman(a, b, rank_a=rank_a), _spearman(diff_a, diff_b, rank_a=diff_rank_a),
            _pearson(diff_a, diff_b))


def _best_one_to_one(edges: dict[str, list[str]], required: list[str]) -> dict[str, str] | None:
    used: set[str] = set()
    chosen: dict[str, str] = {}
    def visit(i: int) -> bool:
        if i == len(required):
            return True
        key = required[i]
        for other in edges.get(key, []):
            if other not in used:
                used.add(other); chosen[key] = other
                if visit(i + 1): return True
                used.remove(other); chosen.pop(key)
        return False
    return chosen.copy() if visit(0) else None


def compare_outputs(candidate: dict[str, Any], deployed: dict[str, dict[str, Any]], *, mode: str,
                    similar_corr: float = DEFAULT_SIMILAR_CORR,
                    classifications: dict[str, tuple[str, str]] | None = None) -> CompareResult:
    """Compare candidate outputs to deployed plugins without executing plugin code."""
    if mode not in {"warn", "enforce"}:
        raise ValueError("mode must be warn or enforce")
    classifications = classifications or {}
    series_cols = [col for col, value in candidate.items()
                   if isinstance(value, pd.Series) and classifications.get(col, ("invariant", "invariant"))[0] != "neither"]
    prepared = {col: _prepare(candidate[col]) for col in series_cols}
    relations: list[tuple[str, str, str, float, str]] = []
    for name, outputs in deployed.items():
        exact_edges = {col: [old for old, value in outputs.items() if _exact(series, value)]
                       for col, series in candidate.items()}
        exact_match = _best_one_to_one(exact_edges, list(candidate))
        if exact_match and len(outputs) == len(candidate):
            for col, old in exact_match.items(): relations.append((name, old, "duplicate", 1.0, f"exact column={col}"))
            continue
        exact_pairs = {(col, old) for col, olds in exact_edges.items() for old in olds}
        for col, old in sorted(exact_pairs): relations.append((name, old, "overlap", 1.0, f"exact column={col}"))
        mono_edges: dict[str, list[str]] = {}
        similar_edges: dict[str, list[tuple[str, float]]] = {}
        measured: dict[tuple[str, str], tuple[float, float, float]] = {}
        for col in series_cols:
            for old, value in outputs.items():
                if (col, old) in exact_pairs or not isinstance(value, pd.Series): continue
                stats = _pair_stats(prepared[col], value)
                if stats is None: continue
                level, diff, corr = stats
                measured[(col, old)] = stats
                if abs(level) >= MONOTONE_LEVEL_CORR and abs(diff) >= MONOTONE_DIFF_CORR:
                    mono_edges.setdefault(col, []).append(old)
                if abs(corr) >= similar_corr:
                    similar_edges.setdefault(col, []).append((old, abs(corr)))
        mono_match = _best_one_to_one(mono_edges, series_cols) if series_cols else None
        if mono_match:
            for col, old in mono_match.items():
                level, diff, _ = measured[(col, old)]
                relations.append((name, old, "monotone", 1.0,
                                  f"column={col} level_spearman={level:.4f} diff_spearman={diff:.4f}"))
        else:
            similar_match = _best_one_to_one(
                {col: [old for old, _ in edges] for col, edges in similar_edges.items()},
                series_cols) if series_cols else None
            if similar_match:
                for col, old in similar_match.items():
                    score = next(score for match, score in similar_edges[col] if match == old)
                    relations.append((name, old, "similar", score,
                                      f"column={col} diff_pearson={score:.4f}"))
    strength = {"duplicate": 4, "monotone": 3, "overlap": 2, "similar": 1}
    relations.sort(key=lambda x: (-strength[x[2]], -x[3], x[0], x[1]))
    similar_to = tuple({"name": name, "column": col, "kind": kind} for name, col, kind, _, _ in relations[:3])
    measures = tuple(text for *_, text in relations[:3])
    kinds = {kind for _, _, kind, _, _ in relations}
    if "duplicate" in kinds:
        return CompareResult(False, "indicator_duplicate", similar_to=similar_to, measures=measures)
    warnings = tuple(code for code, kind in ((MONOTONE_DUPLICATE, "monotone"), (OUTPUT_OVERLAP, "overlap"),
                                             (INDICATOR_SIMILAR, "similar"))
                     if kind in kinds and not (kind == "monotone" and mode == "enforce"))
    if "monotone" in kinds and mode == "enforce":
        return CompareResult(False, MONOTONE_DUPLICATE, warnings, similar_to=similar_to, measures=measures)
    return CompareResult(warnings=warnings, similar_to=similar_to, measures=measures)
