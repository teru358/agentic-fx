"""afx-ops の画面 test が読む JSON の見本を、daemon の実装で作る。

見本は ``afx-ops/tests/fixtures/plugins_*.json`` に置く。afx-ops は daemon の package を
import できないので、ここで組んだ応答をそのまま写して持ち、``test_web_fixture_agrees`` が
実装との一致を見る。更新は ``python -m tests.plugin.web_fixture_world`` で行う。
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

from agentic_fx.plugin import read_model, read_model_inputs
from tests.backtest.factories import SETTINGS
from tests.plugin.read_model_world import World

FIXTURE_DIR = Path(__file__).resolve().parents[2] / "afx-ops" / "tests" / "fixtures"
ZONE = "Asia/Tokyo"
NAMES = ("rsi", "mom", "brk", "bol", "junk", "ghost")


def _dep(plugin: str, pin: str, alias: str = "a") -> str:
    return f"indicators:\n  {alias}: {{plugin: {plugin}, pin: '{pin}'}}\n"


def build_world(root: Path) -> World:
    world = World(root)
    rsi = world.deploy("rsi")
    world.request(rsi, status="approved", origin="human", mission_id=None)
    mom = world.deploy("mom", "strategy", _dep("rsi", rsi.content_hash, "fast"))
    mom_request = world.request(mom, status="approved", inventory=(rsi,), mission_id=7)
    world.journal("mom", mom_request, phase="decided")
    brk = world.deploy("brk", "strategy", _dep("rsi", "a" * 64))
    brk_request = world.request(brk, status="approved", inventory=(rsi,), mission_id=8)
    world.journal("brk", brk_request, phase="decided")
    world.raw_request({
        "name": "rsi", "kind": "indicator", "candidate_origin": "staging",
        "candidate_path": "plugins/_staging/9/rsi", "content_hash": "e" * 64,
        "artifact_hash": "f" * 64, "mission_id": 9})
    old = world.deploy("old", "strategy")
    world.request(old, status="rejected", deps=None, mission_id=3)
    world.undeploy("old")
    bol_artifact = world.version_only("bol")
    world.raw_request({
        "name": "bol", "kind": "indicator", "candidate_origin": "staging",
        "candidate_path": "plugins/_staging/4/bol", "content_hash": "b" * 64,
        "artifact_hash": bol_artifact, "mission_id": 4})
    (world.plugins / "junk").mkdir()
    (world.plugins / "junk" / "config.yaml").write_text("kind: [oops")
    world.raw_request({"name": "ghost", "kind": "signal", "candidate_origin": "human",
                       "content_hash": "c" * 64, "artifact_hash": "d" * 64}, status="rejected")
    return world


def build_dtos(root: Path) -> dict[str, dict]:
    world = build_world(root)
    try:
        inputs = read_model_inputs.collect_inputs(world.plugins, world.conn)
        model = read_model.build_read_model(inputs, settings=SETTINGS)
        out = {"plugins_list": read_model.model_to_dto(model)}
        for name in NAMES:
            out[f"plugin_{name}"] = read_model.detail_to_dto(model, name)
    finally:
        world.conn.close()
    return {key: {**value, "display_timezone": ZONE} for key, value in out.items()}


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="wf") as tmp:
        dtos = build_dtos(Path(tmp))
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    for key, value in dtos.items():
        (FIXTURE_DIR / f"{key}.json").write_text(
            json.dumps(value, indent=1, sort_keys=True, ensure_ascii=False) + "\n")
    print(f"wrote {len(dtos)} fixtures", file=sys.stderr)


if __name__ == "__main__":
    main()
