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

from agentic_fx.plugin.loader import PluginMeta, content_hash

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


def resolve_indicator_deps(meta: PluginMeta, inventory: ApprovedInventory, *,
                           settings, pin_mode: PinMode) -> ResolvedIndicatorSet:
    """[indicator-consumption-wiring] T1 Step 1-3 の骨格スタブ。本実装は
    Step 1-4 で入れる。"""
    raise NotImplementedError
