"""Pure checks used by the indicator fixture gate."""
from __future__ import annotations

import ast
import math
import re
from dataclasses import dataclass
from typing import Any, Mapping

import numpy as np
import pandas as pd

from agentic_fx import improve_notice_codes as notices

EXACT_ATOL = 1e-9
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
LEGACY_SYMBOL_NAME = next(code for code in WARNING_CODES if code == "legacy_symbol_name")
MONOTONE_DUPLICATE = next(code for code in WARNING_CODES if code == "indicator_monotone_duplicate")
OUTPUT_OVERLAP = next(code for code in WARNING_CODES if code == "indicator_output_overlap")
INDICATOR_SIMILAR = next(code for code in WARNING_CODES if code == "indicator_similar")
PRICE_UNIT_TOKENS = frozenset({"pip", "pips", "pipsize", "point", "points", "ticksize",
                               "tick_size", "point_size", "pip_value", "price_level"})


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


def _tokens(value: str) -> list[str]:
    raw = [p for p in re.split(r"[^a-zA-Z0-9]+", value) if p]
    out: list[str] = []
    split: list[str] = []
    for word in raw:
        lowered = word.lower()
        out.append(lowered)
        pieces = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", word)
        pieces = re.sub(r"(?<=[a-zA-Z])(?=\d)|(?<=\d)(?=[a-zA-Z])", " ", pieces)
        split.extend(p.lower() for p in pieces.split() if p)
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


def _source_hits(source_text: str) -> list[Hit]:
    try:
        tree = ast.parse(source_text)
    except SyntaxError:
        return []
    doc_values = {ast.get_docstring(tree, clean=False)}
    doc_values.update(ast.get_docstring(node, clean=False) for node in ast.walk(tree)
                      if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)))
    hits: list[Hit] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            place = "docstring" if node.value in doc_values else "string_constant"
            hits.extend(Hit(place, token) for token in _tokens(node.value))
    return hits


def candidate_tokens(meta: Any, source_text: str, config_text: str) -> list[Hit]:
    """Extract normalized inspection tokens while retaining their source location."""
    hits = [Hit("name", token) for token in _tokens(str(_get(meta, "name", "")))]
    hits.extend(_param_hits(_get(meta, "params", {})))
    hits.extend(_source_hits(source_text))
    hits.extend(Hit("config_value", token) for token in _tokens(config_text))
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


def check_param_price_unit(params: Any) -> Verdict:
    for hit in _param_hits(params):
        if hit.place == "param_key" and (hit.token in PRICE_UNIT_TOKENS or
                                           "price" in hit.token):
            return Verdict(False, "param_price_unit")
    return Verdict()


def _allclose(a: Any, b: Any, *, rtol: float = EXACT_RTOL) -> bool:
    aa, bb = np.asarray(a, dtype=float), np.asarray(b, dtype=float)
    return aa.shape == bb.shape and np.allclose(aa, bb, rtol=rtol, atol=EXACT_ATOL,
                                                equal_nan=True)


def classify_scale(base: pd.DataFrame, k100: pd.DataFrame, k001: pd.DataFrame,
                   vol100: pd.DataFrame, *, rtol: float = EXACT_RTOL) -> dict[str, tuple[str, str]]:
    result: dict[str, tuple[str, str]] = {}
    for col in base.columns:
        b = base[col]
        price = "neither"
        invariant = _allclose(k100[col], b, rtol=rtol) and _allclose(k001[col], b, rtol=rtol)
        proportional = _allclose(k100[col], b * 100, rtol=rtol) and _allclose(k001[col], b * .01, rtol=rtol)
        if invariant:
            price = "invariant"
        elif proportional:
            price = "proportional"
        volume = "invariant" if _allclose(vol100[col], b, rtol=rtol) else "neither"
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


def _as_series(value: Any) -> pd.Series | None:
    return value if isinstance(value, pd.Series) else None


def _exact(a: Any, b: Any) -> bool:
    if not isinstance(a, pd.Series) or not isinstance(b, pd.Series):
        return np.isscalar(a) and np.isscalar(b) and bool(np.isfinite(a) and np.isfinite(b) and abs(a - b) <= EXACT_ATOL + EXACT_RTOL * max(abs(a), abs(b)))
    a, b = a.align(b, join="outer")
    mask_a, mask_b = np.isfinite(a), np.isfinite(b)
    return bool(np.array_equal(mask_a, mask_b) and _allclose(a[mask_a], b[mask_b]))


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


def _spearman(a: np.ndarray, b: np.ndarray) -> float:
    return _pearson(_rank(a), _rank(b))


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
    relations: list[tuple[str, str, str, float]] = []
    for name, outputs in deployed.items():
        exact_edges = {col: [old for old, value in outputs.items() if _exact(series, value)]
                       for col, series in candidate.items()}
        exact_match = _best_one_to_one(exact_edges, list(candidate))
        if exact_match:
            for col, old in exact_match.items(): relations.append((name, old, "duplicate", 1.0))
            continue
        for col, olds in exact_edges.items():
            for old in olds: relations.append((name, old, "overlap", 1.0))
        series_cols = [col for col, value in candidate.items()
                       if isinstance(value, pd.Series) and classifications.get(col, ("invariant", "invariant"))[0] != "neither"]
        mono_edges: dict[str, list[str]] = {}
        similar_edges: dict[str, list[tuple[str, float]]] = {}
        for col in series_cols:
            for old, value in outputs.items():
                if not isinstance(value, pd.Series): continue
                a, b = _common(candidate[col], value)
                if len(a) < MIN_CORRELATION_ROWS: continue
                level, diff = _spearman(a, b), _spearman(np.diff(a), np.diff(b))
                if abs(level) >= MONOTONE_LEVEL_CORR and abs(diff) >= MONOTONE_DIFF_CORR:
                    mono_edges.setdefault(col, []).append(old)
                corr = _pearson(np.diff(a), np.diff(b))
                if abs(corr) >= similar_corr:
                    similar_edges.setdefault(col, []).append((old, abs(corr)))
        mono_match = _best_one_to_one(mono_edges, series_cols) if series_cols else None
        if mono_match:
            for col, old in mono_match.items(): relations.append((name, old, "monotone", 1.0))
        else:
            similar_match = _best_one_to_one(
                {col: [old for old, _ in edges] for col, edges in similar_edges.items()},
                series_cols) if series_cols else None
            if similar_match:
                for col, old in similar_match.items():
                    score = next(score for match, score in similar_edges[col] if match == old)
                    relations.append((name, old, "similar", score))
    strength = {"duplicate": 4, "monotone": 3, "overlap": 2, "similar": 1}
    relations.sort(key=lambda x: (-strength[x[2]], -x[3], x[0], x[1]))
    similar_to = tuple({"name": name, "column": col, "kind": kind} for name, col, kind, _ in relations[:3])
    kinds = {kind for _, _, kind, _ in relations}
    if "duplicate" in kinds:
        return CompareResult(False, "indicator_duplicate", similar_to=similar_to)
    if "monotone" in kinds:
        return CompareResult(mode != "enforce", "" if mode == "warn" else "indicator_monotone_duplicate",
                             (MONOTONE_DUPLICATE,) if mode == "warn" else (), similar_to=similar_to)
    warnings = tuple(code for code in (OUTPUT_OVERLAP, INDICATOR_SIMILAR)
                     if (code == OUTPUT_OVERLAP and "overlap" in kinds) or
                     (code == INDICATOR_SIMILAR and "similar" in kinds))
    return CompareResult(warnings=warnings, similar_to=similar_to)
