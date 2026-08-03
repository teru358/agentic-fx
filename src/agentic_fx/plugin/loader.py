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
  ダ単位。config 読み込み・AST 解析・content_hash 算出のいずれで例外が
  出てもフォルダ単位の reject に閉じ込める — レビュー fix round 1 C10)。
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

# discover が読む plugin.py / config.yaml のサイズ上限 (レビュー fix round 1
# C7)。巨大ファイルで discovery が資源を食い潰すのを防ぐ。テストは
# monkeypatch でこの定数を小さくして高速化してよい (関数内で毎回この
# モジュール属性を参照するため monkeypatch が効く)。
_MAX_FILE_BYTES = 1_048_576  # 1 MiB


class _NoDuplicateKeySafeLoader(yaml.SafeLoader):
    """`yaml.SafeLoader` に「マッピング内の重複キーを reject」を足しただけ
    のサブクラス (レビュー fix round 1 C1)。

    通常の `yaml.safe_load` は重複キーを PyYAML/YAML 1.1 仕様どおり
    「後勝ち」で無言に受理する。plugin config.yaml で `kind: strategy` の
    後に `kind: indicator` のような重複が書かれると、人間レビュー時に
    見えるテキストと実際に discovery が解釈する値が食い違う ("承認迂回"
    のリスク) — fail closed で reject する。
    """


def _construct_mapping_no_duplicates(loader: yaml.SafeLoader, node: yaml.Node,
                                     deep: bool = False) -> dict:
    mapping: dict = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise yaml.YAMLError(f"duplicate key {key!r} in mapping")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_NoDuplicateKeySafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_mapping_no_duplicates)

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
                or not all(isinstance(p, str) and p.strip() for p in pairs_raw)):
            _reject(name, "pairs must be a non-empty list of non-blank str")
            return None
        pairs = tuple(pairs_raw)
    elif pairs_raw is None:
        pairs = ()
    elif (not isinstance(pairs_raw, list)
          or not all(isinstance(p, str) and p.strip() for p in pairs_raw)):
        _reject(name, "pairs must be a list of non-blank str")
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

    追加で fail closed にする 2 点 (レビュー fix round 1 C2/C3) — どちらも
    「AST は通るが契約どおりの位置引数だけで呼ぶと実際には失敗する/
    差し替えられ得る」形:

    - **デフォルト無しの keyword-only 引数が 1 つでもあれば不一致。**
      ハーネスは `func_name(df, ...)` のように位置引数のみで呼ぶため、
      `*, secret` のような必須 kwonly があると TypeError になる。デフォル
      ト付き kwonly・`*args`/`**kwargs` は呼び出し可能なので許容する。
    - **decorator_list が非空なら不一致。** デコレータはモジュール属性を
      非 callable な別物に差し替え得る (例: `@(lambda fn: 0)`)。善意の
      indicator/signal/strategy 関数にデコレータは不要という前提で、
      契約関数への一切のデコレータ付与を fail closed で reject する。
    """
    try:
        tree = ast.parse(plugin_py.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError, OSError):
        return False
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == func_name:
            if node.decorator_list:
                continue
            actual_args = tuple(a.arg for a in node.args.args)
            if actual_args != arg_names:
                continue
            required_kwonly = any(
                default is None for default in node.args.kw_defaults)
            if required_kwonly:
                continue
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
            meta = _discover_one(entry, name)
        except OSError as exc:
            # C10: config 読み込み・AST 解析対象の stat/read・content_hash の
            # いずれで OSError が出ても、そのフォルダのみ reject して次へ
            # 進む (他フォルダの discovery を道連れにしない)。
            _reject(name, f"I/O error ({exc})")
            continue
        if meta is not None:
            metas.append(meta)

    return metas


def _reject_unexpected_py_files(entry: Path, name: str) -> bool:
    """plugin フォルダ直下に規定 3 ファイル以外の `.py` ファイルがあれば
    reject する (F3, codex Critical レビュー fix)。

    典型例は `conftest.py`: 同梱すると submit 時に走る pytest が AST
    検査もハッシュ照合も受けずに自動ロードしてしまう (pytest の仕様上、
    テストファイルと同じディレクトリの `conftest.py` は明示 import なし
    に読み込まれる)。`.py` 拡張子のみを対象とし、`__pycache__` ディレクト
    リや `.yaml` などの非 `.py` ファイルは対象外 (直下の**ファイル**のみを
    見る — サブディレクトリは走査しない)。
    """
    extra = sorted(
        p.name for p in entry.iterdir()
        if p.is_file() and p.suffix == ".py" and p.name not in REQUIRED_FILES)
    if extra:
        _reject(name, f"unexpected .py file(s) in plugin folder: {extra} "
                      f"(only {list(REQUIRED_FILES)} are allowed — "
                      "conftest.py 等の同梱は pytest の自動ロード対象になる "
                      "ため reject する)")
        return True
    return False


def _discover_one(entry: Path, name: str) -> PluginMeta | None:
    """1 フォルダ分の config 検証 + AST 検証 + content_hash 算出。

    呼び出し元 (`discover`) が OSError を一元的に捕捉するため、ここでは
    OSError を握りつぶさない (fail closed の隔離境界は `discover` 側)。
    """
    if _reject_unexpected_py_files(entry, name):
        return None

    config_path = entry / "config.yaml"
    plugin_path = entry / "plugin.py"

    # C7: 巨大ファイルで discovery が資源を食い潰さないよう、読む前に
    # サイズを確認する。
    for label, path in (("plugin.py", plugin_path), ("config.yaml", config_path)):
        if path.stat().st_size > _MAX_FILE_BYTES:
            _reject(name, f"{label} exceeds size limit "
                          f"({_MAX_FILE_BYTES} bytes)")
            return None

    try:
        raw = yaml.load(config_path.read_bytes(), Loader=_NoDuplicateKeySafeLoader)
    except yaml.YAMLError as exc:
        _reject(name, f"invalid YAML ({exc})")
        return None
    if not isinstance(raw, dict):
        _reject(name, "config.yaml must be a mapping")
        return None

    fields = _validate_config(raw, name)
    if fields is None:
        return None

    kind = fields["kind"]
    func_name, arg_names = _KIND_FUNCS[kind]
    if not _has_matching_function(plugin_path, func_name, arg_names):
        _reject(name, f"plugin.py must define "
                      f"{func_name}({', '.join(arg_names)})")
        return None

    return PluginMeta(
        name=name,
        kind=kind,
        path=entry,
        params=fields["params"],
        timeframe=fields["timeframe"],
        pairs=fields["pairs"],
        max_bars=fields["max_bars"],
        content_hash=content_hash(entry),
    )
