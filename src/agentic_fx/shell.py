"""対話シェル — 静かなプロンプト。ログは pull 型コマンドのみ (設計書 §8)。

readline 中断 seam (プラン8, 設計書 §6 codex I3-2): 対話モードの main は
`input()` でブロックするため、`stop_event` が別スレッドからセットされても
自然には解けない。`select.select` によるポーリングへ置き換え、定期的に
`stop_event` をチェックできるようにする。

(レビュー反映 H2) 現時点で `stop_event` を立てる producer は存在しない —
`service.py` の setter は SIGTERM/SIGINT ハンドラ (`if daemon:` の下のみ)
と `finally` だけで、`watchdog_thread` は `stop_event` を立てない。
`run_shell` に到達するのは非 daemon モードのみなので、この seam は現状
end-to-end で一度も駆動されていない。producer は Task 19 (停止状態機械)
で配線する。**本 task はその seam を用意するもの**。

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

import codecs
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

    (レビュー反映 G1) チャンクごとに独立して `decode()` すると、マルチ
    バイト文字がチャンク境界で分断されたときに文字化けする
    (例: `'あ'` の UTF-8 3 バイトが 2 チャンクに割れると `'あ'` の代わりに
    replacement 文字が出る)。`codecs.getincrementaldecoder` を使い、
    境界をまたぐ未確定バイト列をデコーダ内部に保持させる。

    (レビュー反映 G2) `select.select` はストリームが `fileno()` を持たない
    (例: `StringIO`、キャプチャされた stdin) と例外になる。構築時に
    `fileno()` の可否を確認し、`usable` フラグで呼び出し側 (`run_shell`)
    に判断材料を渡す — 使えない場合は割込み可能経路を諦め、
    `_blocking_readline_fallback` (ブロッキング `readline()`) へ
    フォールバックする (黙って停止性を失わないよう、フォールバックした
    事実は `print_fn` 経由でログに残す)。
    """

    def __init__(self, stream, *, chunk_size: int = 4096) -> None:
        self._stream = stream
        self._raw = getattr(stream, "buffer", stream)
        self._pending: str = ""
        self._eof = False
        self._chunk_size = chunk_size
        # H4: input() はストリーム自身の encoding でデコードする。
        # ハードコードした "utf-8" だと encoding が異なるストリームで
        # errors="replace" により UnicodeDecodeError が黙った U+FFFD 化に
        # 変わってしまうため、stream.encoding を優先する。
        encoding = getattr(stream, "encoding", None) or "utf-8"
        self._decoder = codecs.getincrementaldecoder(encoding)(errors="replace")
        self.usable = True
        try:
            stream.fileno()
        except (AttributeError, OSError, ValueError):
            self.usable = False

    def readline(self, stop_event: threading.Event, *,
                 poll_interval: float) -> str | None:
        """1 行返す。`stop_event` が立てば `None` (中断)。EOF なら
        `EOFError` を送出する (バッファに残っていた最終行は先に返す)。"""
        while True:
            newline_at = self._pending.find("\n")
            if newline_at != -1:
                line = self._pending[:newline_at]
                self._pending = self._pending[newline_at + 1:]
                return line.rstrip("\r")  # G4: \r\n の \r を落とす
            if self._eof:
                if self._pending:
                    line, self._pending = self._pending, ""
                    return line.rstrip("\r")
                raise EOFError()
            if stop_event.is_set():
                return None
            # `select` と `read1` の組合せが安全である根拠 (/code-review で
            # 実測確認済み): `BufferedReader.read1(n)` は内部バッファが
            # 空のとき raw からちょうど n バイト読むだけで先読みしない。
            # そのため `read1(1)` の後も残りのバイトは OS 側パイプに
            # 残ったままになり、次回の `select` が正しく検知できる。
            ready, _, _ = select.select([self._stream], [], [], poll_interval)
            if not ready:
                continue
            chunk = self._raw.read1(self._chunk_size) \
                if hasattr(self._raw, "read1") \
                else self._raw.read(self._chunk_size)
            if not chunk:
                self._eof = True
                # H5: デコーダ内部に残った未確定のマルチバイト列を
                # final デコードで確定させる。呼ばなければ、途中で
                # 終わった入力の末尾バイト列が黙って捨てられる。
                self._pending += self._decoder.decode(b"", final=True)
                continue
            if isinstance(chunk, bytes):
                chunk = self._decoder.decode(chunk)
            self._pending += chunk


def _default_prompt_fn(prompt: str) -> None:
    """H3 既定のプロンプト出力。末尾改行なしで即座に flush する。"""
    print(prompt, end="", flush=True)


def _blocking_readline_fallback(prompt: str, stream, *, prompt_fn) -> str:
    """G2: select 非対応ストリーム用の従来型フォールバック。

    `stream.fileno()` が無い/使えない場合は select ベースの割込み可能
    読み取りが構造的に不可能なので、単純なブロッキング `readline()` に
    フォールバックする (中断可能性は失うが、少なくとも停止させずに
    黙って動かなくなることは避ける)。EOF は `EOFError` を送出する。
    """
    prompt_fn(prompt)
    line = stream.readline()
    if line == "":
        raise EOFError()
    return line.rstrip("\n").rstrip("\r")  # H1: \r\n の \r を落とす


def run_shell(commands: Commands, stop_event: threading.Event, *,
              input_fn=input, print_fn=print, prompt_fn=_default_prompt_fn,
              stdin_stream=None, poll_interval: float = 0.5,
              chunk_size: int = 4096) -> None:
    use_interruptible = input_fn is input
    stream = stdin_stream if stdin_stream is not None else sys.stdin
    reader = _InterruptibleLineReader(stream, chunk_size=chunk_size) \
        if use_interruptible else None
    fallback_stream = None
    if reader is not None and not reader.usable:
        # G2: fileno() が使えないストリーム (StringIO / capture 済み stdin
        # 等) では select ベースの割込み可能経路が構造的に使えない。黙って
        # 停止性を失わないよう、フォールバックした事実をログに残して
        # ブロッキング readline() 経路に切り替える。
        print_fn("[shell] stdin が select 非対応のため、"
                 "割込み可能な読み取りを無効化してブロッキング読み取りに"
                 "フォールバックします")
        use_interruptible = False
        reader = None
        fallback_stream = stream
    while not stop_event.is_set():
        try:
            if use_interruptible:
                # H3: プロンプトは print_fn を迂回せず prompt_fn (別の
                # I/O seam) を経由する — 旧コードは input_fn(prompt) 経由
                # だったため呼び出し元が完全にモックできたが、実 print
                # 直書きだとテストで注入した print_fn/stdin_stream が
                # プロンプトの出力先を制御できない。
                prompt_fn("afx> ")
                raw = reader.readline(stop_event, poll_interval=poll_interval)
                if raw is None:
                    return  # stop_event がポーリング中に立った (中断)
                line = raw.strip()
            elif fallback_stream is not None:
                line = _blocking_readline_fallback(
                    "afx> ", fallback_stream, prompt_fn=prompt_fn).strip()
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
