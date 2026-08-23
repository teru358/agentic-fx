"""Mission worker の JSON 行プロトコル共有定義 (プラン8, 設計書 §4.3)。

`mission_worker.py` (子) と `runners/worker_runner.py` (親, Task 10) の
両方が import する。ワイヤ契約を 2 箇所の docstring で同期する方式
(sandbox.py/worker.py) ではなく、seq 検証ロジックそのものを共有コードに
する — 正しさが資金保護 (Mission 実行の停止窓) に波及するため、実装の
乖離が起き得ない形にする。

フレーム型 (全フレームに `seq` を付す。方向別に 1 起点の単調増加 —
codex M2-1):
- 親→子: `handshake` (起動時 1 回) / `tool_rpc_result` / `go` (裁定 RW1 —
  `on_ready` 完了後に親が送る実フレーム。improve profile のみ使用。子は
  `go` を受けるまで Mission/LLM を起動しない — 「`go` 前は副作用ゼロ」)
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


FRAME_TYPES_PARENT_TO_CHILD = frozenset({"handshake", "tool_rpc_result", "go"})
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


def encode_frame(frame: dict) -> bytes:
    """フレームを wire 表現 (JSON 1 行) に変換する。**ストリームには触れない**。

    レビュー 2 周目 (codex): 送出は「serialize 失敗 (wire 未接触 — 同じ seq で
    別フレームを送り直してよい)」と「transport 失敗 (`write`/`flush` の例外 —
    **配信の有無が確定できない**)」を区別しなければならない。両者を
    `write_frame` の中で一体にしていると、呼び出し側はどちらが起きたのか
    判定できず、部分書込み後の再送が wire 上に壊れた行を作る。
    `mission_worker._send_frame` はこの関数で先に serialize してから
    ストリームへ書く。

    `json.dumps` の `TypeError` は `ProtocolError` に**正規化しない**
    (レビュー 2 周目 codex/sonnet で確認した意図的な非対称)。`ProtocolError`
    は「**受け取った**入力がプロトコル契約に反する」ことの表現であり、
    こちらは「自分が送ろうとした値が JSON にならない」ローカルなプログラム
    不備 — 別の故障クラスなので同じ型に潰すと親の分岐が誤る。
    """
    return json.dumps(frame, ensure_ascii=False).encode("utf-8") + b"\n"


def write_frame(stream: BinaryIO, frame: dict) -> None:
    stream.write(encode_frame(frame))
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
