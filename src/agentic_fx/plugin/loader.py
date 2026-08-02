"""plugin discovery + config 検証 + content_hash (プラン 7 Task 1、設計書 §6)。

- **content_hash の算出規則は spec 逐語の 1 箇所** (設計書 §5 必須事項 2):
  `sha256(b"plugin.py\\0" + <plugin.py bytes> + b"\\0config.yaml\\0" +
  <config.yaml bytes>)`。承認ハッシュ (Task 6) と signals の content_hash
  (Task 7/8) は必ずこの関数のみを使う — 二重実装は許さない。
  `test_plugin.py` は署名対象外 (Task 6 の別関心)。
- discover は **plugin コードを import/実行しない** — kind 別の必須関数
  (compute/detect/evaluate) の存在と引数名一致は `ast` モジュールで静的に
  検証する。承認前の plugin をロードしない (§6) の一部。
- 1 フォルダの reject/skip は他フォルダへ波及しない (fail closed はフォル
  ダ単位)。
"""
from __future__ import annotations

import ast
import hashlib
import logging
from dataclasses import dataclass
from pathlib import Path

import yaml

from agentic_fx.backtest.timeframes import PLUGIN_TIMEFRAMES

_log = logging.getLogger(__name__)

REQUIRED_FILES = ("plugin.py", "config.yaml", "test_plugin.py")

DEFAULT_MAX_BARS = 200

# kind → (必須関数名, 位置引数名の並び) — discover の AST 検証がこれと
# 完全一致するかを見る (関数存在 + 引数名一致)。
_KIND_FUNCS: dict[str, tuple[str, tuple[str, ...]]] = {
    "indicator": ("compute", ("df", "params")),
    "signal": ("detect", ("df", "params")),
    "strategy": ("evaluate", ("df", "indicators", "signals", "params")),
}

_KNOWN_KINDS = frozenset(_KIND_FUNCS)

# config.yaml で許容するトップレベルキー。未知キーは fail closed で reject。
_KNOWN_CONFIG_KEYS = frozenset(
    {"kind", "params", "timeframe", "pairs", "exit_mode", "max_bars"})


@dataclass(frozen=True, slots=True)
class PluginMeta:
    name: str
    kind: str
    path: Path
    params: dict
    timeframe: str | None
    pairs: tuple[str, ...]
    max_bars: int
    content_hash: str


def content_hash(plugin_dir: Path) -> str:
    """plugin.py + config.yaml の内容から sha256 を算出する (spec 逐語)。

    署名対象は plugin.py と config.yaml のみ — test_plugin.py は含まない。
    承認 (Task 6) と signals 書き込み (Task 7/8) はこの関数だけを呼ぶ。
    """
    plugin_bytes = (plugin_dir / "plugin.py").read_bytes()
    config_bytes = (plugin_dir / "config.yaml").read_bytes()
    return hashlib.sha256(
        b"plugin.py\0" + plugin_bytes + b"\0config.yaml\0" + config_bytes
    ).hexdigest()


def _reject(name: str, reason: str) -> None:
    _log.warning("plugin %s: %s — rejecting", name, reason)


def _validate_config(raw: dict, name: str) -> dict | None:
    """config.yaml の dict 表現を検証し、正規化済みフィールドを返す。

    不正なら None を返し (呼び出し側で reject 済みとして扱う)、その場で
    warning ログを出す。
    """
    extra_keys = set(raw) - _KNOWN_CONFIG_KEYS
    if extra_keys:
        _reject(name, f"unknown config keys: {sorted(extra_keys)}")
        return None

    kind = raw.get("kind")
    if kind not in _KNOWN_KINDS:
        _reject(name, f"invalid kind: {kind!r} (allowed: {sorted(_KNOWN_KINDS)})")
        return None

    params = raw.get("params", {})
    if not isinstance(params, dict):
        _reject(name, "params must be a mapping")
        return None

    timeframe_required = kind in ("signal", "strategy")
    timeframe = raw.get("timeframe")
    if timeframe_required:
        if timeframe not in PLUGIN_TIMEFRAMES:
            _reject(name, f"invalid timeframe: {timeframe!r} "
                          f"(allowed: {PLUGIN_TIMEFRAMES})")
            return None
    elif timeframe is not None and timeframe not in PLUGIN_TIMEFRAMES:
        _reject(name, f"invalid timeframe: {timeframe!r} "
                      f"(allowed: {PLUGIN_TIMEFRAMES})")
        return None

    pairs_required = kind in ("signal", "strategy")
    pairs_raw = raw.get("pairs")
    if pairs_required:
        if (not isinstance(pairs_raw, list) or not pairs_raw
                or not all(isinstance(p, str) for p in pairs_raw)):
            _reject(name, "pairs must be a non-empty list of str")
            return None
        pairs = tuple(pairs_raw)
    elif pairs_raw is None:
        pairs = ()
    elif (not isinstance(pairs_raw, list)
          or not all(isinstance(p, str) for p in pairs_raw)):
        _reject(name, "pairs must be a list of str")
        return None
    else:
        pairs = tuple(pairs_raw)

    if kind == "strategy":
        exit_mode = raw.get("exit_mode")
        if exit_mode != "levels":
            _reject(name, f"unsupported exit_mode: {exit_mode!r} "
                          "(only 'levels' is implemented; 'evaluate' is 未対応)")
            return None
    elif "exit_mode" in raw:
        _reject(name, "exit_mode is only valid for kind=strategy")
        return None

    max_bars = raw.get("max_bars", DEFAULT_MAX_BARS)
    if isinstance(max_bars, bool) or not isinstance(max_bars, int) or max_bars < 1:
        _reject(name, f"invalid max_bars: {max_bars!r} (must be an int >= 1)")
        return None

    return {
        "kind": kind,
        "params": params,
        "timeframe": timeframe,
        "pairs": pairs,
        "max_bars": max_bars,
    }


def _has_matching_function(plugin_py: Path, func_name: str,
                           arg_names: tuple[str, ...]) -> bool:
    """plugin.py を **import/実行せず** AST で静的に検証する。

    `func_name(arg_names[0], arg_names[1], ...)` という形の**モジュール
    トップレベル**の関数定義が存在するかを見る (`tree.body` のみを走査 —
    `ast.walk` で全ノードを辿ると class/関数の内側にネストされた同名
    関数にも一致してしまい、実際には呼び出し不能なのにゲートを通す
    fail-open になる)。位置引数名の並びが完全一致しないもの (引数名違い・
    過不足) は不一致として扱う。
    """
    try:
        tree = ast.parse(plugin_py.read_text())
    except (SyntaxError, UnicodeDecodeError, OSError):
        return False
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == func_name:
            actual_args = tuple(a.arg for a in node.args.args)
            if actual_args == arg_names:
                return True
    return False


def discover(plugins_dir: Path) -> list[PluginMeta]:
    """plugins_dir 直下のフラットなフォルダ群から plugin を検出する。

    1 plugin = 1 フォルダ。3 ファイル (plugin.py/config.yaml/test_plugin.py)
    のいずれか欠落 → skip + warning。config 不正・AST 不一致 → そのフォル
    ダのみ reject + warning (他フォルダには波及しない)。plugin コードは
    import/実行しない。
    """
    metas: list[PluginMeta] = []
    for entry in sorted(p for p in plugins_dir.iterdir() if p.is_dir()):
        name = entry.name

        missing = [f for f in REQUIRED_FILES if not (entry / f).is_file()]
        if missing:
            _log.warning("plugin %s: missing %s — skipping", name, missing)
            continue

        try:
            raw = yaml.safe_load((entry / "config.yaml").read_bytes())
        except yaml.YAMLError as exc:
            _reject(name, f"invalid YAML ({exc})")
            continue
        if not isinstance(raw, dict):
            _reject(name, "config.yaml must be a mapping")
            continue

        fields = _validate_config(raw, name)
        if fields is None:
            continue

        kind = fields["kind"]
        func_name, arg_names = _KIND_FUNCS[kind]
        if not _has_matching_function(entry / "plugin.py", func_name, arg_names):
            _reject(name, f"plugin.py must define "
                          f"{func_name}({', '.join(arg_names)})")
            continue

        metas.append(PluginMeta(
            name=name,
            kind=kind,
            path=entry,
            params=fields["params"],
            timeframe=fields["timeframe"],
            pairs=fields["pairs"],
            max_bars=fields["max_bars"],
            content_hash=content_hash(entry),
        ))

    return metas
