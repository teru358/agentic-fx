"""対話シェル — 静かなプロンプト。ログは pull 型コマンドのみ (設計書 §8)。

readline 中断 seam (プラン8, 設計書 §6 codex I3-2): 対話モードの main は
`input()` でブロックするため、`stop_event` が別スレッド (watchdog) から
セットされても自然には解けない。`select.select` によるポーリングへ
置き換え、定期的に `stop_event` をチェックできるようにする — 対話モード
で main を wake する唯一の経路。

実装ノート (プランからの逸脱): プラン原文は `stream.buffer.peek(1)` で
Python 側バッファの残存を確認する方式 (裁定書 F-15/IM-8) を指定していたが、
実測したところ `BufferedReader.peek()` は自身の内部バッファが空のとき
(TextIOWrapper が既に読み出し済みで、デコード済みテキストとして自分の
内部バッファ側に保持している場合) **raw stream への読み取りを試みて
ブロックする**ことを確認した (新規データが来るまで無期限に停止し、
stop_event も検知できない — プランの想定より深刻なデッドロック)。
そのため機構を変更し、`TextIOWrapper` (テキスト層) の内部バッファに
一切依存しない設計にした: バイナリ層を自前で `select` → 生バイト読み取り
→ 自前デコード・行分割 → 内部の pending バッファに保持、という形にした。
これにより「Python 側バッファに次行が残るが select は検知しない」問題が
そもそも発生しない (自前バッファの残存は select を経由せず即座に返す)。
"""
from __future__ import annotations

import select
import sys
import threading

from agentic_fx.commands import Commands


class _InterruptibleLineReader:
    """`select` でポーリング可能な行リーダー。

    `TextIOWrapper.readline()` を使わず、バイナリ層 (`stream.buffer`,
    無ければ `stream` 自体) を直接読んで自前で UTF-8 デコード・改行分割を
    行う。複数行が 1 回の read で同時到着した場合も pending バッファに
    保持し、次回の呼び出しでは追加の I/O 待ちなしに即座に返す。
    """

    def __init__(self, stream) -> None:
        self._stream = stream
        self._raw = getattr(stream, "buffer", stream)
        self._pending: str = ""
        self._eof = False

    def readline(self, stop_event: threading.Event, *,
                 poll_interval: float) -> str | None:
        """1 行返す。`stop_event` が立てば `None` (中断)。EOF なら
        `EOFError` を送出する (バッファに残っていた最終行は先に返す)。"""
        while True:
            newline_at = self._pending.find("\n")
            if newline_at != -1:
                line = self._pending[:newline_at]
                self._pending = self._pending[newline_at + 1:]
                return line
            if self._eof:
                if self._pending:
                    line, self._pending = self._pending, ""
                    return line
                raise EOFError()
            if stop_event.is_set():
                return None
            ready, _, _ = select.select([self._stream], [], [], poll_interval)
            if not ready:
                continue
            chunk = self._raw.read1(4096) if hasattr(self._raw, "read1") \
                else self._raw.read(4096)
            if not chunk:
                self._eof = True
                continue
            if isinstance(chunk, bytes):
                chunk = chunk.decode("utf-8", errors="replace")
            self._pending += chunk


def run_shell(commands: Commands, stop_event: threading.Event, *,
              input_fn=input, print_fn=print, stdin_stream=None,
              poll_interval: float = 0.5) -> None:
    use_interruptible = input_fn is input
    stream = stdin_stream if stdin_stream is not None else sys.stdin
    reader = _InterruptibleLineReader(stream) if use_interruptible else None
    while not stop_event.is_set():
        try:
            if use_interruptible:
                print("afx> ", end="", flush=True)
                raw = reader.readline(stop_event, poll_interval=poll_interval)
                if raw is None:
                    return  # stop_event がポーリング中に立った (中断)
                line = raw.strip()
            else:
                line = input_fn("afx> ").strip()
        except (EOFError, KeyboardInterrupt):
            stop_event.set()
            return
        if stop_event.is_set():
            return
        if not line:
            continue
        if line == "stop":
            stop_event.set()
            return
        try:
            print_fn(commands.dispatch(line))
        except KeyboardInterrupt:
            stop_event.set()
            return
