"""shell readline 中断 seam (プラン8, 設計書 §6 codex I3-2)。"""
from __future__ import annotations

import io
import os
import threading
import time

import pytest

from agentic_fx.shell import (
    _blocking_readline_fallback,
    _InterruptibleLineReader,
    run_shell,
)


class _FakeCommands:
    def dispatch(self, line: str) -> str:
        return f"echo: {line}"


def _start_shell(*args, **kwargs) -> tuple[threading.Thread, list[BaseException]]:
    """`run_shell` をスレッドで起動し、スレッド内の例外を呼び出し側へ伝播
    できるようにラップする。

    (レビュー反映 G3) `assert not t.is_alive()` だけでは worker スレッドが
    例外で異常死した場合も PASS してしまう (変異2の実測で偶然 PASS した
    のがまさにこれ)。例外を捕まえてリストに積み、テスト側で
    `assert not err` することで異常死を検出できるようにする。
    """
    err: list[BaseException] = []

    def _worker() -> None:
        try:
            run_shell(*args, **kwargs)
        except BaseException as e:  # noqa: BLE001 — テストへ伝播させる
            err.append(e)

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    return t, err


def test_stop_event_wakes_blocked_shell_promptly(tmp_path):
    """対話モードで stop_event が外部スレッドからセットされたとき、
    input() 相当のブロッキング読み取りが速やかに解ける
    (poll_interval を短く設定し、real select ベースのポーリングで
    実際に解けることを実測する)。
    """
    r_fd, w_fd = os.pipe()  # 読み取り側は「データが来ない」ダミー stdin
    stream = os.fdopen(r_fd, "r")
    stop_event = threading.Event()

    t, err = _start_shell(
        _FakeCommands(), stop_event,
        stdin_stream=stream, poll_interval=0.05, print_fn=lambda *_: None)
    time.sleep(0.1)  # run_shell がポーリングループに入るまで待つ

    start = time.monotonic()
    stop_event.set()
    t.join(timeout=2.0)
    elapsed = time.monotonic() - start

    assert not err, f"shell スレッドが例外で死んだ: {err[0]!r}"
    assert not t.is_alive(), "stop_event セット後、shell スレッドが終了していない"
    assert elapsed < 1.0  # poll_interval=0.05s に対して十分な余裕

    os.close(w_fd)
    stream.close()


def test_line_is_read_when_available(tmp_path):
    """通常経路: stdin にデータが来れば読み取ってコマンドとして処理する。"""
    r_fd, w_fd = os.pipe()
    stream = os.fdopen(r_fd, "r")
    writer = os.fdopen(w_fd, "w")
    stop_event = threading.Event()
    printed: list[str] = []

    t, err = _start_shell(
        _FakeCommands(), stop_event,
        stdin_stream=stream, poll_interval=0.05, print_fn=printed.append)
    time.sleep(0.1)
    writer.write("hello\n")
    writer.flush()
    time.sleep(0.2)

    stop_event.set()
    t.join(timeout=2.0)

    assert not err, f"shell スレッドが例外で死んだ: {err[0]!r}"
    assert "echo: hello" in printed
    writer.close()
    stream.close()


def test_multiple_lines_arriving_together_are_not_stuck(tmp_path):
    """裁定書 F-15 (IM-8) の回帰ピン: ペースト等で複数行が 1 回の書込みで
    同時到着した場合、2 行目以降が「次の新規入力が来るまで」滞留せず、
    追加の書込みなしに両方処理されることを確認する。"""
    r_fd, w_fd = os.pipe()
    stream = os.fdopen(r_fd, "r")
    writer = os.fdopen(w_fd, "w")
    stop_event = threading.Event()
    printed: list[str] = []

    t, err = _start_shell(
        _FakeCommands(), stop_event,
        stdin_stream=stream, poll_interval=0.05, print_fn=printed.append)
    time.sleep(0.1)
    writer.write("hello\nworld\n")  # 2 行を 1 回の書込み・flush でまとめて送る
    writer.flush()
    time.sleep(0.3)  # 追加の書込みは一切行わない — この待ちだけで両方届くはず

    stop_event.set()
    t.join(timeout=2.0)

    assert not err, f"shell スレッドが例外で死んだ: {err[0]!r}"
    assert "echo: hello" in printed
    assert "echo: world" in printed  # 旧稿はここが追加入力なしでは届かなかった
    writer.close()
    stream.close()


def test_multibyte_char_split_across_chunk_boundary_is_not_corrupted(tmp_path):
    """G1 pin: `read1()` のチャンク境界でマルチバイト文字が分断されても
    文字化けしないことを確認する。`chunk_size=1` で 1 バイトずつ読ませ、
    UTF-8 マルチバイト文字を強制的に複数チャンクへ分断させる。"""
    r_fd, w_fd = os.pipe()
    stream = os.fdopen(r_fd, "r")
    writer = os.fdopen(w_fd, "wb")  # バイナリで UTF-8 バイト列を直接送る
    stop_event = threading.Event()
    printed: list[str] = []

    t, err = _start_shell(
        _FakeCommands(), stop_event,
        stdin_stream=stream, poll_interval=0.05, print_fn=printed.append,
        chunk_size=1)
    time.sleep(0.1)
    writer.write("あいうえお\n".encode("utf-8"))
    writer.flush()
    time.sleep(0.3)

    stop_event.set()
    t.join(timeout=2.0)

    assert not err, f"shell スレッドが例外で死んだ: {err[0]!r}"
    assert "echo: あいうえお" in printed  # チャンク境界分断で文字化けしていないこと
    writer.close()
    stream.close()


def test_stdin_without_fileno_falls_back_without_raising(tmp_path):
    """G2 pin: `fileno()` が使えないストリーム (StringIO 等) を渡しても
    例外にならず、フォールバックした事実をログに残しつつ従来型の
    ブロッキング読み取りで動作を継続することを確認する。"""
    stream = io.StringIO("hello\n")
    stop_event = threading.Event()
    printed: list[str] = []
    logs: list[str] = []

    def _print_fn(msg: str) -> None:
        (logs if msg.startswith("[shell]") else printed).append(msg)

    t, err = _start_shell(
        _FakeCommands(), stop_event,
        stdin_stream=stream, poll_interval=0.05, print_fn=_print_fn)
    t.join(timeout=2.0)  # StringIO は "hello\n" の後 EOF になり自然終了する

    assert not err, f"shell スレッドが例外で死んだ: {err[0]!r}"
    assert not t.is_alive()
    assert "echo: hello" in printed
    assert any("フォールバック" in m for m in logs), \
        "フォールバックした事実がログに残っていない"


def test_reader_strips_carriage_return_from_crlf_line():
    """G4 pin: `_InterruptibleLineReader.readline()` 単体で \\r\\n の \\r が
    行末に残らないことを確認する (`run_shell` 側の `.strip()` に頼らない —
    `.strip()` は \\r も除去してしまうため、この契約は reader 単体でしか
    検証できない)。"""
    r_fd, w_fd = os.pipe()
    stream = os.fdopen(r_fd, "r")
    writer = os.fdopen(w_fd, "wb")
    writer.write(b"hello\r\n")
    writer.flush()
    time.sleep(0.1)

    reader = _InterruptibleLineReader(stream)
    stop_event = threading.Event()
    line = reader.readline(stop_event, poll_interval=0.05)

    assert line == "hello"  # "hello\r" ではないこと
    writer.close()
    stream.close()


def test_eof_on_stream_sets_stop_event_and_returns(tmp_path):
    """G5 pin (1/2): stdin が EOF になったら `stop_event` がセットされ
    `run_shell` が正常に返ることを確認する (`EOFError` 経由の停止経路)。"""
    r_fd, w_fd = os.pipe()
    stream = os.fdopen(r_fd, "r")
    writer = os.fdopen(w_fd, "w")
    stop_event = threading.Event()
    printed: list[str] = []

    t, err = _start_shell(
        _FakeCommands(), stop_event,
        stdin_stream=stream, poll_interval=0.05, print_fn=printed.append)
    time.sleep(0.1)
    writer.close()  # EOF を発生させる

    t.join(timeout=2.0)

    assert not err, f"shell スレッドが例外で死んだ: {err[0]!r}"
    assert not t.is_alive()
    assert stop_event.is_set()
    stream.close()


def test_partial_line_before_eof_is_processed(tmp_path):
    """G5 pin (2/2): 改行なしで終わる最終行 (直後に EOF) も処理される
    ことを確認する (バッファに残った最終行を EOF 時に取りこぼさない)。"""
    r_fd, w_fd = os.pipe()
    stream = os.fdopen(r_fd, "r")
    writer = os.fdopen(w_fd, "w")
    stop_event = threading.Event()
    printed: list[str] = []

    t, err = _start_shell(
        _FakeCommands(), stop_event,
        stdin_stream=stream, poll_interval=0.05, print_fn=printed.append)
    time.sleep(0.1)
    writer.write("partial")  # 改行なし
    writer.flush()
    writer.close()  # 直後に EOF

    t.join(timeout=2.0)

    assert not err, f"shell スレッドが例外で死んだ: {err[0]!r}"
    assert "echo: partial" in printed
    stream.close()


def test_fallback_strips_carriage_return_from_crlf_line():
    """H1 pin: フォールバック経路 (`_blocking_readline_fallback`) 単体で
    \\r\\n の \\r が行末に残らないことを確認する。`run_shell` 経由だと
    `.strip()` が \\r も除去してしまい欠陥を隠すため、関数を直接呼ぶ
    (G4 と同じ理由で単体テストでしか検証できない契約)。"""
    stream = io.StringIO("hello\r\n")
    line = _blocking_readline_fallback(
        "afx> ", stream, prompt_fn=lambda *_: None)
    assert line == "hello"  # "hello\r" ではないこと


def test_prompt_goes_through_prompt_fn_not_real_stdout(tmp_path, capsys):
    """H3 pin: プロンプトが `prompt_fn` (専用の I/O seam) 経由で出力され、
    実 stdout へ直接漏れないことを確認する。旧コードは `print()` 直書き
    だったため、`print_fn`/`stdin_stream` を注入しても呼び出し元は
    プロンプト出力を制御できなかった。"""
    r_fd, w_fd = os.pipe()
    stream = os.fdopen(r_fd, "r")
    writer = os.fdopen(w_fd, "w")
    stop_event = threading.Event()
    prompts: list[str] = []

    t, err = _start_shell(
        _FakeCommands(), stop_event,
        stdin_stream=stream, poll_interval=0.05, print_fn=lambda *_: None,
        prompt_fn=prompts.append)
    time.sleep(0.1)
    writer.write("hello\n")
    writer.flush()
    time.sleep(0.2)

    stop_event.set()
    t.join(timeout=2.0)

    assert not err, f"shell スレッドが例外で死んだ: {err[0]!r}"
    assert "afx> " in prompts  # prompt_fn 経由で呼ばれたこと
    captured = capsys.readouterr()
    assert "afx> " not in captured.out  # 実 stdout に直接漏れていないこと
    writer.close()
    stream.close()


def test_partial_multibyte_sequence_at_eof_is_finalized(tmp_path):
    """H5 pin: 改行なしで UTF-8 マルチバイト文字の途中 (先頭2バイトのみ)
    で入力が終わっても (EOF)、デコーダ内部に残った未確定バイト列が
    黙って捨てられず finalize されることを確認する。"""
    r_fd, w_fd = os.pipe()
    stream = os.fdopen(r_fd, "r")
    writer = os.fdopen(w_fd, "wb")
    stop_event = threading.Event()
    printed: list[str] = []

    t, err = _start_shell(
        _FakeCommands(), stop_event,
        stdin_stream=stream, poll_interval=0.05, print_fn=printed.append)
    time.sleep(0.1)
    # "あ" (U+3042) の UTF-8 は 3 バイト (0xE3 0x81 0x82)。先頭 2 バイトだけ
    # 送って改行なしで EOF にする — マルチバイト文字の途中で入力が
    # 終わるケースを再現する。
    writer.write("prefix-".encode("utf-8") + "あ".encode("utf-8")[:2])
    writer.flush()
    writer.close()  # 改行なしで EOF

    t.join(timeout=2.0)

    assert not err, f"shell スレッドが例外で死んだ: {err[0]!r}"
    # 黙って捨てられていれば "echo: prefix-" (末尾バイトが消える) になる。
    # finalize されていれば末尾に replacement 文字が付き長さが伸びる。
    assert any(p.startswith("echo: prefix-") and len(p) > len("echo: prefix-")
               for p in printed), printed
    stream.close()
