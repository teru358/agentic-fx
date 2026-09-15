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
import contextvars
import hashlib
import json
import logging
import math
import os
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from agentic_fx.backtest.timeframes import PLUGIN_TIMEFRAMES

_log = logging.getLogger(__name__)

# ContextVar isolates rejection capture per execution context (effectively per
# thread/async task), so parallel missions cannot cross-contaminate reasons.
_reject_sink: contextvars.ContextVar[list[str] | None] = contextvars.ContextVar(
    "plugin_loader_reject_sink", default=None)

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
    {"kind", "params", "timeframe", "pairs", "exit_mode", "max_bars",
     "indicators", "outputs"})

# 上限 (設計書 §2.2、codex r5 C1)。handshake 総量上限は resolve.py が持つ。
MAX_INDICATOR_DEPS = 8
MAX_OUTPUTS = 32
MAX_PARAMS_BYTES = 8192

_ALIAS_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_OUTPUT_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_PIN_RE = re.compile(r"^[0-9a-f]{64}$")
_INDICATOR_REF_KEYS = frozenset({"plugin", "params", "pin"})


@dataclass(frozen=True, slots=True)
class IndicatorRef:
    alias: str
    plugin: str
    params: dict
    pin: str | None


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
    artifact_hash: str | None = None  # プラン10 Task 5 5-F (申し送り③)
    # [indicator-consumption-wiring] 設計書 §2.2。strategy 以外は必ず ()、
    # indicator 以外の outputs は必ず None (kind 限定は _validate_config)。
    indicators: tuple["IndicatorRef", ...] = ()
    outputs: tuple[str, ...] | None = None


_PLUGIN_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_SYMLINK_TARGET_RE_TMPL = r"^\.versions/{name}/[0-9a-f]{{64}}$"


def artifact_hash_bytes(plugin_py: bytes, config_yaml: bytes,
                        test_plugin: bytes) -> str:
    """3 本全体の sha256 (版ストアのキー)。`version_store.artifact_hash_bytes`
    の薄いラッパ (B-5 是正、設計書 §5.2) — 式の実体は version_store 側の
    1 箇所にのみ存在する。"""
    from agentic_fx.plugin import version_store
    return version_store.artifact_hash_bytes(plugin_py, config_yaml, test_plugin)


def content_hash(plugin_dir: Path) -> str:
    """plugin.py + config.yaml の内容から sha256 を算出する (spec 逐語)。

    署名対象は plugin.py と config.yaml のみ — test_plugin.py は含まない。
    承認 (Task 6) と signals 書き込み (Task 7/8) はこの関数だけを呼ぶ。
    `version_store.content_hash_bytes` の薄いラッパ (レビュー1周目 M2、設計書 §5.2) —
    式の実体は `version_store` 側の 1 箇所にのみ存在する。
    """
    from agentic_fx.plugin import version_store
    plugin_bytes = (plugin_dir / "plugin.py").read_bytes()
    config_bytes = (plugin_dir / "config.yaml").read_bytes()
    return version_store.content_hash_bytes(plugin_bytes, config_bytes)


def _reject(name: str, reason: str) -> None:
    _log.warning("plugin %s: %s — rejecting", name, reason)
    sink = _reject_sink.get()
    if sink is not None:
        sink.append(reason)


def _check_json_safe(value, path: str) -> str | None:
    """`params` が JSON-safe かを再帰検査する (設計書 §2.2)。
    reject reason 文字列 (`params_not_json_safe:<path>` /
    `params_too_large:<path>`) を返す。問題なければ None。

    許容: str / int (bool 含む) / 有限 float / None / list / dict (キーは str)。
    YAML の date・datetime・set・bytes・±Inf・NaN は reject
    (json.dumps は NaN/Inf を通してしまうため型検査で先に落とす)。
    """
    stack = [(value, path)]
    while stack:
        node, node_path = stack.pop()
        if node is None or isinstance(node, (str, bool)):
            continue
        if isinstance(node, int):
            continue
        if isinstance(node, float):
            if not math.isfinite(node):
                return f"params_not_json_safe:{node_path}"
            continue
        if isinstance(node, list):
            for i, item in enumerate(node):
                stack.append((item, f"{node_path}[{i}]"))
            continue
        if isinstance(node, dict):
            for key, item in node.items():
                if not isinstance(key, str):
                    return f"params_not_json_safe:{node_path}"
                stack.append((item, f"{node_path}.{key}"))
            continue
        return f"params_not_json_safe:{node_path}"
    try:
        encoded = json.dumps(value, separators=(",", ":"), sort_keys=True,
                             ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError):
        return f"params_not_json_safe:{path}"
    if len(encoded.encode("utf-8")) > MAX_PARAMS_BYTES:
        return f"params_too_large:{path}"
    return None


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
    bad = _check_json_safe(params, "params")
    if bad is not None:
        _reject(name, bad)
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

    indicators: tuple[IndicatorRef, ...] = ()
    if "indicators" in raw:
        if kind != "strategy":
            _reject(name, "indicators_not_allowed_for_kind")
            return None
        refs = raw["indicators"]
        if not isinstance(refs, dict) or not refs:
            _reject(name, "indicator_ref_bad_alias")
            return None
        if len(refs) > MAX_INDICATOR_DEPS:
            _reject(name, "too_many_indicators")
            return None
        built: list[IndicatorRef] = []
        seen: set[str] = set()
        for alias, ref in refs.items():
            if not isinstance(alias, str) or not _ALIAS_RE.fullmatch(alias):
                _reject(name, "indicator_ref_bad_alias")
                return None
            if alias in seen:
                _reject(name, "indicator_ref_duplicate_alias")
                return None
            seen.add(alias)
            if not isinstance(ref, dict):
                _reject(name, "indicator_ref_missing_plugin")
                return None
            unknown = sorted(set(ref) - _INDICATOR_REF_KEYS)
            if unknown:
                _reject(name, f"indicator_ref_unknown_key:{unknown[0]}")
                return None
            plugin_name = ref.get("plugin")
            if not isinstance(plugin_name, str) or not plugin_name:
                _reject(name, "indicator_ref_missing_plugin")
                return None
            if not _PLUGIN_NAME_RE.fullmatch(plugin_name):
                _reject(name, "indicator_ref_bad_plugin_name")
                return None
            ref_params = ref.get("params", {})
            if not isinstance(ref_params, dict):
                _reject(name, f"params_not_json_safe:indicators.{alias}.params")
                return None
            bad = _check_json_safe(ref_params, f"indicators.{alias}.params")
            if bad is not None:
                _reject(name, bad)
                return None
            pin = ref.get("pin")
            if pin is not None and (not isinstance(pin, str)
                                    or not _PIN_RE.fullmatch(pin)):
                _reject(name, "indicator_ref_bad_pin")
                return None
            built.append(IndicatorRef(alias=alias, plugin=plugin_name,
                                      params=ref_params, pin=pin))
        indicators = tuple(built)

    outputs: tuple[str, ...] | None = None
    if "outputs" in raw:
        if kind != "indicator":
            _reject(name, "outputs_not_allowed_for_kind")
            return None
        raw_outputs = raw["outputs"]
        if not isinstance(raw_outputs, list) or not raw_outputs:
            _reject(name, "outputs_bad_entry")
            return None
        if len(raw_outputs) > MAX_OUTPUTS:
            _reject(name, "too_many_outputs")
            return None
        for item in raw_outputs:
            if not isinstance(item, str) or not _OUTPUT_RE.fullmatch(item):
                _reject(name, "outputs_bad_entry")
                return None
        if len(set(raw_outputs)) != len(raw_outputs):
            _reject(name, "outputs_duplicate")
            return None
        outputs = tuple(raw_outputs)

    return {
        "kind": kind,
        "params": params,
        "timeframe": timeframe,
        "pairs": pairs,
        "max_bars": max_bars,
        "indicators": indicators,
        "outputs": outputs,
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


def _resolve_entity(plugins_dir: Path, name: str, activity=None) -> Path | None:
    """`plugins_dir / name` が {プレーンディレクトリ / 正規形 symlink} の
    いずれかであることを検査し、実体ディレクトリ (プレーンならそのまま、
    symlink なら版ディレクトリ) を返す (プラン10 Task 5 5-F、設計書 §2.3)。

    **`resolve()` の使い方に注意**: 妥当性の判定はリンク先の**文字列**
    (`os.readlink` の戻り値) を正規表現で字句検証することのみで行う —
    ここで `.match()` に失敗すれば `.resolve()` へは進まない。`.resolve()`
    は**検証に通った後**、正規形だと確定済みの相対パスを絶対化するためだけ
    に使う (§2.3 が禁じる「リンク先が正規形かどうかを `resolve()` の結果で
    判定すること」という逆順の用法ではない)。
    """
    entry = plugins_dir / name
    if entry.is_symlink():
        target = os.readlink(entry)
        pattern = re.compile(_SYMLINK_TARGET_RE_TMPL.format(name=re.escape(name)))
        # round2 M1 是正 (2026-08-29、verified-round2.md M1): fullmatch に
        # 揃える (`.match()` + `$` は末尾改行を受理する — probe 実測)。
        if not pattern.fullmatch(target):
            _reject(name, f"symlink target does not match canonical form: {target!r}")
            return None
        version_dir = (plugins_dir / target).resolve()
        if not version_dir.is_dir():
            _reject(name, f"symlink target is not a directory: {version_dir}")
            return None
        # E3 裁定 (2026-08-25、A16 + B-20): 字句正規形検査 (上の
        # `pattern.match`) を通っても、`.versions/<name>` 自体が symlink で
        # 実体が `plugins_dir` の外に出る形は拒否する — `resolve()` は
        # 「正規形と確定した後の絶対化」にのみ使う (docstring どおり)、
        # ここで足すのは resolve 後の containment 検査のみで正規形判定を
        # resolve() に委ねるわけではない。
        plugins_root_real = plugins_dir.resolve()
        try:
            version_dir.relative_to(plugins_root_real)
        except ValueError:
            _reject(name, f"symlink target escapes plugins_root after "
                          f"resolving intermediate symlinks: {version_dir}")
            return None
        return version_dir
    return entry


def discover(plugins_dir: Path, *, activity=None) -> list[PluginMeta]:
    """plugins_dir 直下のフラットなフォルダ群から plugin を検出する。

    1 plugin = 1 フォルダ。3 ファイル (plugin.py/config.yaml/test_plugin.py)
    のいずれか欠落 → skip + warning。config 不正・AST 不一致 → そのフォル
    ダのみ reject + warning (他フォルダには波及しない)。plugin コードは
    import/実行しない。

    プラン10 Task 5 5-F (設計書 §2.3) — 先頭が `_`/`.` のディレクトリは
    除外、名前は正規形 (`^[a-z][a-z0-9_]{0,63}$`) のみ受理、正規形の
    相対 symlink (`plugins/<name>` → `.versions/<name>/<artifact_hash>`)
    は版ディレクトリへ追従して `PluginMeta.path`/`artifact_hash` を
    確定する。`activity` は不一致時の ERROR 記録用 (申し送り⑤、省略可)。
    """
    metas: list[PluginMeta] = []
    for entry in sorted(p for p in plugins_dir.iterdir()
                        if p.is_dir() or p.is_symlink()):
        name = entry.name
        if name.startswith("_") or name.startswith("."):
            continue
        # round2 M1 是正 (2026-08-29、verified-round2.md M1): fullmatch に
        # 揃える (`.match()` + `$` は末尾改行を受理する — probe 実測)。
        if not _PLUGIN_NAME_RE.fullmatch(name):
            _reject(name, f"non-canonical plugin name: {name!r}")
            continue

        resolved = _resolve_entity(plugins_dir, name, activity=activity)
        if resolved is None:
            continue

        missing = [f for f in REQUIRED_FILES if not (resolved / f).is_file()]
        if missing:
            _log.warning("plugin %s: missing %s — skipping", name, missing)
            continue

        try:
            meta = _discover_one(resolved, name)
        except OSError as exc:
            # C10: config 読み込み・AST 解析対象の stat/read・content_hash の
            # いずれで OSError が出ても、そのフォルダのみ reject して次へ
            # 進む (他フォルダの discovery を道連れにしない)。
            _reject(name, f"I/O error ({exc})")
            continue
        if meta is None:
            continue

        if entry.is_symlink():
            expected_hash = resolved.name
            if meta.artifact_hash != expected_hash:
                _reject(name, f"version dir name {expected_hash!r} does not "
                              f"match computed artifact_hash "
                              f"{meta.artifact_hash!r} — rejecting (in-place "
                              "edit detected)")
                if activity is not None:
                    activity.error("plugin_artifact_hash_mismatch",
                                   {"name": name, "expected": expected_hash,
                                    "computed": meta.artifact_hash})
                continue
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

    plugin_bytes = plugin_path.read_bytes()
    config_bytes = config_path.read_bytes()
    test_bytes = (entry / "test_plugin.py").read_bytes()

    return PluginMeta(
        name=name,
        kind=kind,
        path=entry,
        params=fields["params"],
        timeframe=fields["timeframe"],
        pairs=fields["pairs"],
        max_bars=fields["max_bars"],
        content_hash=content_hash(entry),
        artifact_hash=artifact_hash_bytes(plugin_bytes, config_bytes, test_bytes),
        indicators=fields["indicators"],
        outputs=fields["outputs"],
    )


def discover_one_with_reason(
        entry: Path, name: str) -> tuple[PluginMeta | None, str | None]:
    """Discover one plugin and return its first logged rejection reason."""
    reasons: list[str] = []
    token = _reject_sink.set(reasons)
    try:
        missing = [f for f in REQUIRED_FILES if not (entry / f).is_file()]
        if missing:
            _reject(name, f"missing required files: {missing}")
            meta = None
        else:
            try:
                meta = _discover_one(entry, name)
            except OSError as exc:
                _reject(name, f"I/O error ({exc})")
                meta = None
    finally:
        _reject_sink.reset(token)
    if meta is None:
        return None, reasons[0] if reasons else None
    return meta, None
