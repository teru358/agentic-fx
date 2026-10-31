"""ウィザードの試験で使う台本入出力と、外部境界の fake。"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import httpx

from agentic_fx.ops_setup.interactive import EnableResult
from agentic_fx.setup import post_start, unit as unit_module
from agentic_fx.setup.wizard import WizardDeps

REPO = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 10, 2, 6, 0, tzinfo=timezone.utc)  # 2026-10-02 15:00 JST


class ScriptedWizardIO:
    """回答の列、または質問文に応じた回答で動く入出力。

    `answers` を使うときは質問の過不足を厳密に見る (足りなければ失敗、余れば `assert_consumed` が失敗)。
    """

    def __init__(self, answers=(), *, tty: bool = True, responder: Callable[[str], str] | None = None,
                 raise_at: tuple[int, BaseException] | None = None, eof_when_exhausted: bool = False):
        self.answers = list(answers)
        self.tty = tty
        self.responder = responder
        self.raise_at = raise_at
        self.eof_when_exhausted = eof_when_exhausted
        self.asked: list[str] = []
        self.out: list[str] = []

    def isatty(self) -> bool:
        return self.tty

    def say(self, text: str) -> None:
        self.out.append(text)

    def ask(self, prompt: str) -> str:
        index = len(self.asked)
        self.asked.append(prompt)
        if self.raise_at is not None and index >= self.raise_at[0]:
            raise self.raise_at[1]
        if self.responder is not None:
            return self.responder(prompt)
        if not self.answers:
            if self.eof_when_exhausted:
                raise EOFError
            raise AssertionError(f"台本が尽きました: {prompt!r}")
        return self.answers.pop(0)

    def assert_consumed(self) -> None:
        assert not self.answers, f"台本が余りました: {self.answers}"

    @property
    def text(self) -> str:
        return "\n".join(self.out)

    def asked_with(self, fragment: str) -> list[str]:
        return [prompt for prompt in self.asked if fragment in prompt]


class Rules:
    """質問文の断片 → 回答。断片ごとに回答の列を持ち、尽きたら最後の回答を繰り返す。既定は Enter。"""

    def __init__(self, *rules: tuple[str, str | tuple[str, ...]], default: str = ""):
        self.rules = [(fragment, [answer] if isinstance(answer, str) else list(answer))
                      for fragment, answer in rules]
        self.default = default

    def __call__(self, prompt: str) -> str:
        for fragment, queue in self.rules:
            if fragment in prompt:
                return queue.pop(0) if len(queue) > 1 else queue[0]
        if "afx を常駐サービス" in prompt:
            for fragment, queue in self.rules:
                if fragment == "systemd のユーザーサービス":
                    return queue.pop(0) if len(queue) > 1 else queue[0]
        return self.default


class FakeResponse:
    def __init__(self, data: Any, status_code: int = 200) -> None:
        self._data = data
        self.status_code = status_code

    def json(self) -> Any:
        return self._data

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            request = httpx.Request("GET", "http://fake.invalid/")
            raise httpx.HTTPStatusError("failed", request=request,
                                        response=httpx.Response(self.status_code, request=request))


DEFAULT_MODELS = ("qwen3.6-35b-a3b_Q4", "qwen3-coder-30b-a3b-instruct", "other-model")


class Fetch:
    """bridge と llama-swap の応答を台本で返す。台本に無い URL は失敗させる。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.models: dict[str, Any] = {"http://localhost:8080/v1": list(DEFAULT_MODELS)}
        self.bridge_health: Any = {"mt5_connected": True}
        self.bridge_status = 200
        self.bridge_time = "2026-10-02T06:00:00+00:00"
        self.props: dict[str, int] = {}
        self.bridge_symbols: Any = []
        self.bridge_symbols_status = 200

    def __call__(self, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append((url, kwargs))
        if url.endswith("/models"):
            base = url[: -len("/models")]
            entry = self.models.get(base)
            if entry is None:
                raise httpx.ConnectError("refused")
            if isinstance(entry, int):
                return FakeResponse({"data": []}, entry)
            return FakeResponse({"data": [{"id": name} for name in entry]})
        if url.endswith("/symbols"):
            if self.bridge_symbols_status != 200:
                return FakeResponse({}, self.bridge_symbols_status)
            return FakeResponse(self.bridge_symbols)
        if url.endswith("/props"):
            return FakeResponse({"default_generation_settings": {"n_ctx": self.props.get("n_ctx", 8192)}})
        if url.endswith("/health"):
            if self.bridge_status != 200:
                return FakeResponse({}, self.bridge_status)
            return FakeResponse(self.bridge_health)
        if "/quote/" in url:
            return FakeResponse({"bid": 1.0, "ask": 1.1, "time": self.bridge_time})
        raise AssertionError(f"unexpected fetch: {url}")

    def urls(self, suffix: str) -> list[str]:
        return [url for url, _ in self.calls if url.endswith(suffix)]


@dataclass
class Completed:
    returncode: int = 0
    stdout: str = ""
    stderr: str = ""


class Run:
    """argv の完全一致で返答を決める。台本に無い呼び出しは失敗させる (実コマンドを呼ばない)。"""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.kwargs: list[dict] = []
        self.rules: list[tuple[tuple[str, tuple[str, ...]], Any]] = []

    def on(self, *argv: str, returncode: int = 0, stdout: str = "", raises: BaseException | None = None):
        self.rules.insert(0, (("exact", argv), raises if raises is not None else Completed(returncode, stdout)))
        return self

    def on_prefix(self, *prefix: str, returncode: int = 0, stdout: str = "", raises: BaseException | None = None):
        self.rules.insert(0, (("prefix", prefix), raises if raises is not None else Completed(returncode, stdout)))
        return self

    def on_prefix_sequence(self, *prefix: str, replies: list[tuple[int, str]]):
        self.rules.insert(0, (("prefix", prefix), [Completed(code, out) for code, out in replies]))
        return self

    def on_sequence(self, *argv: str, replies: list[tuple[int, str]]):
        """同じ argv に対し、呼ぶたびに次の (returncode, stdout) を返す (最後は繰り返す)。"""
        self.rules.insert(0, (("exact", argv), [Completed(code, out) for code, out in replies]))
        return self

    def __call__(self, argv, **kwargs):
        argv = [str(part) for part in argv]
        self.calls.append(argv)
        self.kwargs.append(kwargs)
        for (kind, expected), reply in self.rules:
            matched = tuple(argv) == expected if kind == "exact" else tuple(argv[: len(expected)]) == expected
            if matched:
                if isinstance(reply, list):
                    return reply.pop(0) if len(reply) > 1 else reply[0]
                if isinstance(reply, BaseException):
                    raise reply
                return reply
        raise AssertionError(f"unscripted subprocess call: {argv}")

    def matching(self, *prefix: str) -> list[list[str]]:
        return [call for call in self.calls if tuple(call[: len(prefix)]) == prefix]


@dataclass
class Calls:
    """順序つきの呼び出し記録。"""

    log: list[str] = field(default_factory=list)
    place_unit: list[dict] = field(default_factory=list)
    enable: list[tuple] = field(default_factory=list)
    enable_and_start: list[dict] = field(default_factory=list)
    yf: list[str] = field(default_factory=list)
    admission: list[dict] = field(default_factory=list)


def make_executable(path: Path, content: bytes = b"#!/bin/sh\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def make_root(tmp_path: Path) -> Path:
    root = tmp_path / "root"
    (root / "config").mkdir(parents=True)
    shutil.copy(REPO / "config" / "settings.yaml.example", root / "config" / "settings.yaml.example")
    return root


def make_deps(tmp_path: Path, **overrides: Any) -> tuple[WizardDeps, Calls, Fetch, Run]:
    calls = Calls()
    fetch = Fetch()
    run = Run().on_prefix("loginctl", "show-user", stdout="Linger=yes\n")

    def place_unit(root, settings, **kwargs):
        calls.log.append("place_unit")
        calls.place_unit.append(kwargs)
        return unit_module.PlacementResult("installed")

    def enable(root, feature, *, lease=None, io=None, **kwargs):
        assert io is None, "ウィザードは ops の入出力を渡してはいけない"
        calls.log.append(f"enable:{feature}")
        held = lease is not None and lease.active
        calls.enable.append((feature, held))
        return EnableResult(0, "enabled", None)

    def enable_and_start(root, **kwargs):
        calls.log.append("enable_and_start")
        calls.enable_and_start.append(kwargs)
        return post_start.StartOutcome("skipped", "enable_declined", kwargs["now"]())

    def read_admission_line(run_, invocation_id, **kwargs):
        calls.admission.append({"invocation_id": invocation_id, **kwargs})
        return None

    def yf_quote(pair):
        calls.yf.append(pair)
        return object()

    values: dict[str, Any] = dict(
        fetch=fetch, run=run, which=lambda name: None, environ={}, now=lambda: NOW,
        clock=lambda: 0.0, sleep=lambda seconds: None, unit_dir=tmp_path / "units",
        landlock_available=lambda: True, landlock_abi=lambda: 8, find_spec=lambda name: object(),
        local_timezone=lambda: "Asia/Tokyo", yf_quote=yf_quote,
        resolve_ops=lambda root: post_start.OpsPrecheck(True),
        place_unit=place_unit, enable=enable, enable_and_start=enable_and_start,
        read_admission_line=read_admission_line,
        deployed_plugins=lambda root, conn, settings: ())
    values.update(overrides)
    return WizardDeps(**values), calls, fetch, run


def snapshot(root: Path) -> dict[str, str]:
    """root 配下の全ファイルの相対パス → sha256 (ディレクトリは '/' つきの空文字)。"""
    result: dict[str, str] = {}
    for current, dirs, files in os.walk(root):
        for name in dirs:
            result[str((Path(current) / name).relative_to(root)) + "/"] = ""
        for name in files:
            path = Path(current) / name
            result[str(path.relative_to(root))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


LOCK_FILES = {"data/", "data/ops/", "data/instance.lock", "data/ops/setup.lock"}

# 既定の回答だけで fresh な root を最後まで通す (価格源は bridge に接続済み、常駐化はしない)。
FRESH_RULES = (("価格源", "1"), ("方針文", ""), ("最初の課題", ""), ("書き込みます", "y"),
               ("afx を常駐サービス", "n"))


def fresh_responder(*extra: tuple[str, str | tuple[str, ...]]) -> Rules:
    translated = tuple(("afx を常駐サービス" if fragment == "systemd のユーザーサービス" else fragment, answer)
                       for fragment, answer in extra)
    return Rules(*translated, *FRESH_RULES)


def codex_model_list(*ids: str, hidden: tuple[str, ...] = ()) -> str:
    """`codex app-server` の `initialize` と `model/list` への応答 (実機の形)。"""
    entries = [{"id": name, "model": name, "displayName": name.upper(),
                "description": f"{name} の説明", "hidden": name in hidden, "isDefault": False}
               for name in ids]
    return (json.dumps({"id": 1, "result": {"userAgent": "codex/0.0"}}) + "\n"
            + json.dumps({"id": 2, "result": {"data": entries, "nextCursor": None}}) + "\n")
