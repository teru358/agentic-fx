"""対話シェル — 静かなプロンプト。ログは pull 型コマンドのみ (設計書 §8)。"""
from __future__ import annotations

import threading

from agentic_fx.commands import Commands


def run_shell(commands: Commands, stop_event: threading.Event, *,
              input_fn=input, print_fn=print) -> None:
    while not stop_event.is_set():
        try:
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
