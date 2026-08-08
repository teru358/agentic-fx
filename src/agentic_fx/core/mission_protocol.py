"""Mission worker の JSON 行プロトコル共有定義 (プラン8, 設計書 §4.3)。

`mission_worker.py` (子) と `runners/worker_runner.py` (親, Task 10) の
両方が import する。ワイヤ契約を 2 箇所の docstring で同期する方式
(sandbox.py/worker.py) ではなく、seq 検証ロジックそのものを共有コードに
する — 正しさが資金保護 (Mission 実行の停止窓) に波及するため、実装の
乖離が起き得ない形にする。

フレーム型 (全フレームに `seq` を付す。方向別に 1 起点の単調増加 —
codex M2-1):
- 親→子: `handshake` (起動時 1 回) / `tool_rpc_result`
- 子→親: `ready` (起動応答) / `event` (transcript メッセージ 1 件) /
  `tool_rpc` (RPC 要求) / `result` (最終ステータス、正常終端で 1 回)
"""
from __future__ import annotations

import json
from typing import Any, BinaryIO


class ProtocolError(Exception):
    """フレーム形式・seq 違反など、プロトコル契約に反する入力全般の単一
    表現 (fail closed — sandbox.py の `SandboxError`/`_dead` 意味論と同じ:
    違反を検出したらセッションは即座に死んだものとして扱う)。"""


FRAME_TYPES_PARENT_TO_CHILD = frozenset({"handshake", "tool_rpc_result"})
FRAME_TYPES_CHILD_TO_PARENT = frozenset({"ready", "event", "tool_rpc", "result"})


class SeqTracker:
    """方向別の 1 起点単調増加 seq 検証 (codex M2-1)。重複・逆行・欠番は
    すべて `ProtocolError` (「次に来るべき値と一致しない」の一律判定 —
    重複/逆行/欠番を種類分けしない。呼び出し側は方向ごとに別インスタンス
    を持つこと (親→子と子→親は別カウンタ)。"""

    def __init__(self) -> None:
        self._expected = 1

    def check(self, seq: Any) -> None:
        if not isinstance(seq, int) or isinstance(seq, bool):
            raise ProtocolError(f"seq must be an int, got {seq!r}")
        if seq != self._expected:
            raise ProtocolError(
                f"seq out of order: expected {self._expected}, got {seq}")
        self._expected += 1


def write_frame(stream: BinaryIO, frame: dict) -> None:
    stream.write(json.dumps(frame, ensure_ascii=False).encode("utf-8") + b"\n")
    stream.flush()


def read_frame(stream: BinaryIO) -> dict | None:
    line = stream.readline()
    if not line:
        return None
    try:
        frame = json.loads(line)
    except ValueError as exc:
        # レビュー 1 周目 (codex I-1 / sonnet C1): `ProtocolError` は
        # 「プロトコル契約に反する入力全般の単一表現」と定義されているのに、
        # 旧実装は `json.JSONDecodeError` (と不正 UTF-8 の
        # `UnicodeDecodeError` — どちらも `ValueError` の派生) を素通し
        # していた。親 (`WorkerRunner`, Task 10) が `except ProtocolError`
        # をセッション違反の統一経路として実装しても捕捉できず、起動失敗の
        # 分類・後始末が例外型に依存してしまう。
        raise ProtocolError(f"malformed frame line: {exc}") from exc
    if not isinstance(frame, dict):
        # 同上。JSON の配列・文字列・数値は「行としては妥当」だがフレーム
        # 契約には反する。型注釈上の `dict` を裏切ったまま返すと、受け手の
        # `.get()` が `AttributeError` になり単一表現が崩れる。
        raise ProtocolError(
            f"frame must be a JSON object, got {type(frame).__name__}")
    return frame
