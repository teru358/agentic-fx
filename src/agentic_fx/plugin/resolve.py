"""indicator 依存の解決 — 唯一の場所 ([indicator-consumption-wiring] 設計書 §2.3)。

strategy の `config.yaml` の `indicators:` 宣言を、承認済み inventory
(`ApprovedInventory`) に対して解決し、worker へ渡す `ResolvedIndicatorSet`
を作る。**merge/lookup/pin 検査の規則はこのモジュールの 1 箇所にのみ存在する** —
composition root (service / 改善 mission / 人間 CLI / 承認回廊) は
`resolve_indicator_deps` を呼ぶだけで、独自の突合を書かない。

`pin_mode` の使い分け (設計書 §2.3 の表が正):
- `"require"`: submit / bless / commit gate / `approved_plugins` 第 2 相。
  pin 必須かつ inventory の hash と一致。
- `"check"`: 探索中の `run_backtest` / 人間 CLI。pin があれば一致を要求、
  無ければ通す。
- `"ignore"`: ロック操作。既存 pin が古くても名前で解決し直す
  (`require`/`check` のままでは古い pin の再ロックに到達できない — codex r4)。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Mapping

import yaml

from agentic_fx.plugin.loader import PluginMeta, _check_json_safe, content_hash

# 展開後の canonical handshake 総 byte 数の上限 (設計書 §2.2、codex r5 C1)。
# 既存 `sandbox._STARTUP_MAX_BYTES` (65536、worker → 親の起動応答) とは
# 別枠 — こちらは親 → worker の送信側上限。
MAX_HANDSHAKE_BYTES = 262144

PinMode = Literal["require", "check", "ignore"]


class IndicatorResolutionError(Exception):
    """解決失敗の単一表現。`str(exc)` は固定文言
    `indicator_unresolved:<alias>:<reason>` (alias が None なら `-`)。
    呼び出し元はこの文字列をそのまま stderr / 例外文言に使う。"""

    def __init__(self, alias: str | None, reason: str) -> None:
        self.alias = alias
        self.reason = reason
        super().__init__(f"indicator_unresolved:{alias or '-'}:{reason}")


def freeze_params(obj: Any):
    """再帰的に hashable な frozen 表現へ。dict は ("d", frozenset of
    (key, frozen)) 、list は ("l", tuple of frozen)、スカラーはそのまま。
    `PluginMeta.params` は従来どおり dict のまま (現行 wire の json.dumps
    を壊さない、codex r3 C3) なので、不変性はこの別表現で担保する。"""
    if isinstance(obj, dict):
        return ("d", frozenset((k, freeze_params(v)) for k, v in obj.items()))
    if isinstance(obj, list):
        return ("l", tuple(freeze_params(v) for v in obj))
    return obj


def thaw(frozen: Any):
    """`freeze_params` の逆。**call ごとに完全に独立した plain JSON object**
    を作る (deep) — 同じ frozen から 2 回 thaw した結果は互いに影響しない。"""
    if isinstance(frozen, tuple) and len(frozen) == 2 and frozen[0] == "d":
        return {k: thaw(v) for k, v in frozen[1]}
    if isinstance(frozen, tuple) and len(frozen) == 2 and frozen[0] == "l":
        return [thaw(v) for v in frozen[1]]
    return frozen


@dataclass(frozen=True, slots=True)
class ApprovedInventory:
    root: Path                       # 実体パス (resolve() 済み)
    metas: tuple[PluginMeta, ...]

    def by_name(self, name: str) -> PluginMeta | None:
        for meta in self.metas:
            if meta.name == name:
                return meta
        return None


@dataclass(frozen=True, slots=True)
class ResolvedIndicator:
    alias: str
    plugin_name: str
    plugin_py: Path
    content_hash: str
    params: Any                      # freeze_params の戻り
    max_bars: int
    outputs: tuple[str, ...]
    pinned: bool


@dataclass(frozen=True, slots=True)
class ResolvedIndicatorSet:
    inventory_root: Path
    items: tuple[ResolvedIndicator, ...]
    all_pinned: bool

    @staticmethod
    def empty(inventory_root: Path) -> "ResolvedIndicatorSet":
        return ResolvedIndicatorSet(inventory_root=inventory_root, items=(),
                                    all_pinned=True)

    def pins(self) -> dict:
        """`lock_config` へ渡す `{alias: content_hash}` (alias 昇順)。"""
        return {i.alias: i.content_hash for i in self.items}

    def pin_object(self) -> dict:
        """承認 payload 用の plain JSON object (alias 昇順)。"""
        return {i.alias: {"plugin": i.plugin_name,
                          "content_hash": i.content_hash,
                          "params": thaw(i.params)}
                for i in self.items}

    def handshake_items(self) -> list[dict]:
        """`PluginSession.__enter__` が handshake に載せる形 (alias 順)。"""
        return [{"alias": i.alias, "plugin_py": str(i.plugin_py),
                 "params": thaw(i.params), "max_bars": i.max_bars,
                 "outputs": list(i.outputs)}
                for i in self.items]


@dataclass(frozen=True, slots=True)
class RejectedStrategy:
    name: str
    content_hash: str
    alias: str | None
    reason: str


@dataclass(frozen=True)
class InventoryBuildResult:
    inventory: ApprovedInventory
    phase1_metas: tuple[PluginMeta, ...]
    resolved: Mapping[tuple[str, str], ResolvedIndicatorSet]
    rejected_strategies: tuple[RejectedStrategy, ...]


def _merge_params(base: dict, override: dict) -> dict:
    """indicator の params の deep copy に strategy 側を 1 段上書き
    (設計書 §2.3)。base は絶対に書き換えない。

    **codex r1 束1 Important**: `json.dumps` は非 str キー (int / float /
    bool) を JSON object key の**文字列へ暗黙変換**するので、roundtrip の
    後に `allow_nan=False` だけを見ても `{1: "x"}` は `{"1": "x"}` として
    素通りしてしまう。§2.2/§2.3 は params の dict key を str に限定して
    いるので、**merge の前に**両辺へ loader と同じ再帰型検査 (`str` キー・
    有限数) をかけ、`ValueError` にして呼び出し元の
    `params_not_json_safe` へ写像する。同時に、キー型が混在したときに
    後段の `json.dumps(..., sort_keys=True)` が生の `TypeError` を
    送出する経路 (handshake 検査は try の外にある) も塞ぐ。"""
    for label, obj in (("base", base), ("override", override)):
        bad = _check_json_safe(obj, label)
        if bad is not None:
            raise ValueError(bad)
    merged = json.loads(json.dumps(base))   # deep copy (JSON-safe が前提)
    merged.update(json.loads(json.dumps(override)))
    return merged


def resolve_indicator_deps(meta: PluginMeta, inventory: ApprovedInventory, *,
                           settings, pin_mode: PinMode) -> ResolvedIndicatorSet:
    """`meta.indicators` を `inventory` に対して解決する。失敗は
    `IndicatorResolutionError(alias, reason)` — reason 語彙は設計書 §2.3 の
    8 語のみ。`max_bars` は「渡す履歴の最大本数」であって必要 warmup 本数
    ではないので、大小比較による拒否は入れない (codex r4 I3)。"""
    items: list[ResolvedIndicator] = []
    for ref in meta.indicators:
        dep = inventory.by_name(ref.plugin)
        if dep is None:
            raise IndicatorResolutionError(ref.alias, "not_found")
        if dep.kind != "indicator":
            raise IndicatorResolutionError(ref.alias, "not_indicator")
        if dep.outputs is None:
            raise IndicatorResolutionError(ref.alias, "outputs_undeclared")
        if dep.max_bars > settings.plugin.max_bars_limit:
            raise IndicatorResolutionError(ref.alias, "over_max_bars_limit")
        if pin_mode != "ignore":
            if ref.pin is None:
                if pin_mode == "require":
                    raise IndicatorResolutionError(ref.alias, "unpinned")
            elif ref.pin != dep.content_hash:
                raise IndicatorResolutionError(ref.alias, "pin_mismatch")
        try:
            merged = _merge_params(dep.params, ref.params)
            json.dumps(merged, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise IndicatorResolutionError(
                ref.alias, "params_not_json_safe") from exc
        items.append(ResolvedIndicator(
            alias=ref.alias, plugin_name=dep.name,
            plugin_py=dep.path / "plugin.py", content_hash=dep.content_hash,
            params=freeze_params(merged), max_bars=dep.max_bars,
            outputs=dep.outputs,
            pinned=(pin_mode == "ignore") or ref.pin is not None))
    items.sort(key=lambda i: i.alias)
    resolved = ResolvedIndicatorSet(
        inventory_root=inventory.root, items=tuple(items),
        all_pinned=all(i.pinned for i in items))
    encoded = json.dumps(resolved.handshake_items(), separators=(",", ":"),
                         sort_keys=True, ensure_ascii=False, allow_nan=False)
    if len(encoded.encode("utf-8")) > MAX_HANDSHAKE_BYTES:
        raise IndicatorResolutionError(None, "handshake_too_large")
    return resolved


def lock_config(candidate_dir: Path, pins: Mapping[str, str]
                ) -> tuple[str, str, str]:
    """候補の `config.yaml` の各 alias に `pin` を書き込む (U5: 全体を
    `yaml.safe_load` → pin 追加 → `yaml.safe_dump(sort_keys=False)` で
    再シリアライズ。コメント・キー順は保持しない — 呼び出し元が差分を
    人間へ表示する)。戻り値 `(before_text, after_text, new_content_hash)`。

    `pins` は `{alias: content_hash}`。resolver 経路の呼び出し元は
    `ResolvedIndicatorSet.pins()` を渡し (`pin_mode="ignore"` で解決した
    もの)、改善 worker の `lock_staging_deps` は `inventory_view` から
    組んだ dict を渡す — **YAML 書き換えの実装はここ 1 箇所に閉じる**
    (設計書 §2.3)。`pins` に無い alias は触らない。**既に同じ pin なら
    書き込み内容は変わらない** (before == after)。

    **snapshot 再取得** (ユーザー裁定 2026-09-14 ⑥): 書き込み後、disk 上の
    `config.yaml` を独立に再読して `content_hash()` を計算し直し、それを
    `new_content_hash` として返す。`check_candidate_snapshot`
    (`gate_pytest.py`) は 3 ファイルの存在・属性しか見ず内容の一致は
    検査しないため、その「lock で壊れるものはない」という主張だけに
    依存せず、呼び出し元 (`_plugin_lock` CLI / `lock_staging_deps` tool)
    は **resolve 時点で計算した hash を使い回さず、必ずこの戻り値を
    使う**。"""
    path = candidate_dir / "config.yaml"
    before_text = path.read_text(encoding="utf-8")
    config = yaml.safe_load(before_text)
    if not isinstance(config, dict):
        raise ValueError(f"lock_config: {path} is not a mapping")
    refs = config.get("indicators") or {}
    for alias, pin in pins.items():
        ref = refs.get(alias)
        if isinstance(ref, dict):
            ref["pin"] = pin
    after_text = yaml.safe_dump(config, sort_keys=False, allow_unicode=True,
                                default_flow_style=False)
    if after_text != before_text:
        path.write_text(after_text, encoding="utf-8")
    new_content_hash = content_hash(candidate_dir)
    return before_text, after_text, new_content_hash


def strip_pins(config: dict) -> dict:
    """`config` の deep copy から `indicators.<alias>.pin` だけを除いたもの。
    入力は書き換えない。pin は作者の設計判断ではなくハーネスの派生値なので、
    「実質的に同じ候補か」の比較はこの形で行う (設計書 §2.7)。

    **比較専用** (opus r1 M2): `default=str` は YAML の date / datetime を
    黙って文字列にする。loader が JSON-safe 検証をかけるのは `params` だけ
    なので、`config.yaml` の他キー (例: 作者が書いた `note: 2026-01-01`) に
    date があると、この関数を通した値は元の config と型が変わる。**この
    戻り値は `same_modulo_pins` / `is_relock_transition` の等価比較に
    しか使わない** — 両辺を同じ変換に通すので比較意味論は壊れないが、
    **この戻り値を書き戻したり handshake に載せたりしてはならない**
    (書き戻しは `lock_config` の `yaml.safe_load` → `safe_dump` 経路のみ)。
    """
    out = json.loads(json.dumps(config, default=str))
    refs = out.get("indicators")
    if isinstance(refs, dict):
        for ref in refs.values():
            if isinstance(ref, dict):
                ref.pop("pin", None)
    return out


def _config_of(plugin_dir: Path) -> dict:
    return yaml.safe_load((plugin_dir / "config.yaml").read_text(encoding="utf-8"))


def same_modulo_pins(candidate_dir: Path, other_dir: Path) -> bool:
    """正規化 AST の一致 **かつ** `strip_pins(config)` の一致。
    `noop_gate.normalized_plugin_ast` を再利用する (AST 正規化の実装は
    noop_gate 側の 1 箇所のみ)。"""
    from agentic_fx.plugin.noop_gate import normalized_plugin_ast
    if (normalized_plugin_ast(candidate_dir / "plugin.py")
            != normalized_plugin_ast(other_dir / "plugin.py")):
        return False
    return strip_pins(_config_of(candidate_dir)) == strip_pins(_config_of(other_dir))


def _pins_of(plugin_dir: Path) -> dict[str, str | None]:
    refs = (_config_of(plugin_dir) or {}).get("indicators") or {}
    return {alias: (ref.get("pin") if isinstance(ref, dict) else None)
            for alias, ref in refs.items()}


def is_relock_transition(candidate_dir: Path, deployed_dir: Path,
                         inventory: ApprovedInventory) -> bool:
    """「正式な再ロック経路 (複製 → pin だけ I1→I2 → 提出)」かどうか
    (設計書 §2.7、codex r4 C2)。`same_modulo_pins` **かつ** deployed の
    pin の少なくとも 1 つが現在 inventory と不一致 **かつ** candidate の
    pin が全て現在 inventory と一致。依存が 0 本なら常に False。"""
    if not same_modulo_pins(candidate_dir, deployed_dir):
        return False
    deployed_pins = _pins_of(deployed_dir)
    candidate_pins = _pins_of(candidate_dir)
    if not deployed_pins or not candidate_pins:
        return False

    # opus r1 M1 是正: 元案は `_current(alias, pins)` と引数 `pins` を
    # 取りながら本文で使っていなかった (死に引数 = 嘘のシグネチャ)。
    # plugin 名の引き元は candidate / deployed のどちらでもよい
    # (`same_modulo_pins` が `strip_pins` 一致を先に保証しているので
    # `indicators` の alias → plugin 対応は両者で同一) が、**どちらを
    # 読むかを明示**するため `plugin_dir` を引数にする。
    def _current(alias: str, plugin_dir: Path) -> str | None:
        refs = (_config_of(plugin_dir) or {}).get("indicators") or {}
        ref = refs.get(alias)
        if not isinstance(ref, dict):
            return None
        dep = inventory.by_name(ref.get("plugin"))
        return dep.content_hash if dep is not None else None

    stale = any(pin != _current(alias, deployed_dir)
                for alias, pin in deployed_pins.items())
    fresh = all(pin is not None and pin == _current(alias, candidate_dir)
                for alias, pin in candidate_pins.items())
    return stale and fresh
