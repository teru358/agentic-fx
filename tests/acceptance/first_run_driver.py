#!/usr/bin/env python3
"""`afx init` を端末として起動し、台本どおりに答えて全出力を記録する対話 driver。

標準ライブラリだけで動く (`python3 -I tests/acceptance/first_run_driver.py --help`)。
`agentic_fx` は import しない。

台本は JSON の配列で、1 要素が 1 つの問い:

    [{"expect": "<プロンプトの部分文字列>", "send": "<入力>"}, ...]

* `expect` は、子 process が入力を待っている行 (最後の改行より後ろ) に含まれる文字列。
  合う問いにだけ `send` を送る。どの `expect` にも合わないまま出力が `--unexpected-after` 秒
  (既定 30) 増えなければ「予期しない問い」として記録し、何も送らずに子を止める
  (勝手に Enter を送らない)。途中で出力が増えたら数え直す (進捗表示を問いと取り違えない)。
  1 問の上限 (`--step-timeout` か台本の `timeout`) が `--unexpected-after` より短いときも、
  出力が止まって最終行がどの `expect` にも合わなければ、timeout より先に「予期しない問い」(3) とする。
  timeout (4) は出力が続いたまま上限を超えたときと、最終行が合うのに子が入力を受け取らないときだけ。
* `send` の `${NAME}` は実行時の環境変数に置き換える。未定義の名前があれば、子を起動する前に止める。
  展開した値と、4 文字以上の `send` の値は、記録では (pty の echo も含めて) `***` に置き換える。
  4 文字未満の値は伏せない。秘密 (鍵・token・URL) を台本の値に書かないこと。
  番号や y/n は伏せる対象ではない。
* 任意の項目: `timeout` (この問いが出るまでの秒数)、`optional` (出なければ飛ばす)、
  `comment` (記録には残さない説明)。

終了コード: 0 完走 / 1 台本が済んだあと子が非 0 / 2 使い方か台本の誤り /
3 予期しない問い / 4 timeout / 5 台本が残ったまま子が終了。
"""
from __future__ import annotations

import argparse
import codecs
import errno
import fcntl
import json
import os
import pty
import re
import select
import signal
import struct
import sys
import termios
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parents[2]
DEFAULT_SCRIPT = Path(__file__).resolve().parent / "scripts" / "main-claude.json"
DEFAULT_COMMAND = ["uv", "run", "afx", "init"]

RC_OK = 0
RC_CHILD_FAILED = 1
RC_USAGE = 2
RC_UNEXPECTED = 3
RC_TIMEOUT = 4
RC_SCRIPT_LEFT = 5

_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b[@-Z\\-_]|\x1b\][^\x07]*\x07")
_SECRET_LINE = re.compile(r"http|key|token|secret|password", re.IGNORECASE)
_VARIABLE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_STEP_KEYS = {"expect", "send", "timeout", "optional", "comment"}


class ScriptError(Exception):
    pass


@dataclass(frozen=True)
class Step:
    expect: str
    send: str
    timeout: float | None
    optional: bool


def load_script(path: Path) -> list[Step]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise ScriptError(f"台本を読めません: {path} ({error.strerror or error})") from None
    try:
        raw = json.loads(text)
    except ValueError as error:
        raise ScriptError(f"台本が JSON ではありません: {path} ({error})") from None
    if not isinstance(raw, list) or not raw:
        raise ScriptError("台本は 1 件以上の要素を持つ配列にしてください")
    steps: list[Step] = []
    for index, item in enumerate(raw, 1):
        where = f"{index} 番目"
        if not isinstance(item, dict):
            raise ScriptError(f"{where}: オブジェクトではありません")
        unknown = sorted(set(item) - _STEP_KEYS)
        if unknown:
            raise ScriptError(f"{where}: 未知のキー {', '.join(unknown)}")
        expect, send = item.get("expect"), item.get("send")
        if not isinstance(expect, str) or not expect:
            raise ScriptError(f"{where}: expect (空でない文字列) がありません")
        if not isinstance(send, str):
            raise ScriptError(f"{where}: send (文字列) がありません")
        if "\n" in send or "\r" in send:
            raise ScriptError(f"{where}: send に改行は入れられません (1 問に 1 行)")
        timeout = item.get("timeout")
        if timeout is not None and (isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0):
            raise ScriptError(f"{where}: timeout は正の数にしてください")
        optional = item.get("optional", False)
        if not isinstance(optional, bool):
            raise ScriptError(f"{where}: optional は true / false にしてください")
        steps.append(Step(expect, send, float(timeout) if timeout is not None else None, optional))
    if all(step.optional for step in steps):
        raise ScriptError("台本の要素がすべて optional です")
    return steps


def variables_in(steps: list[Step]) -> list[str]:
    names: list[str] = []
    for step in steps:
        for name in _VARIABLE.findall(step.send):
            if name not in names:
                names.append(name)
    return names


def expand(send: str, environ: dict[str, str]) -> str:
    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in environ:
            raise ScriptError(f"環境変数 {name} が定義されていません (台本の send が参照しています)")
        return environ[name]

    return _VARIABLE.sub(replace, send)


def redact(line: str) -> str:
    """http / key / token などを含む行は丸ごと伏せる (秘密と URL を記録に残さない)。"""
    return "***" if _SECRET_LINE.search(line) else line


_MASK_MIN_LENGTH = 4


class Transcript:
    """行単位で伏せ字にして記録ファイルへ書く。"""

    def __init__(self, path: Path, secrets: tuple[str, ...] = ()) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._file = open(path, "w", encoding="utf-8")
        self._partial = ""
        self._secrets = tuple(sorted({value for value in secrets if value}, key=len, reverse=True))

    def _mask(self, line: str) -> str:
        for value in self._secrets:
            line = line.replace(value, "***")
        return redact(line)

    def _write_line(self, line: str) -> None:
        self._file.write(self._mask(line) + "\n")

    def feed(self, text: str) -> None:
        self._partial += text
        while "\n" in self._partial:
            line, self._partial = self._partial.split("\n", 1)
            self._write_line(line)
        self._file.flush()

    def event(self, message: str) -> None:
        if self._partial:
            self._write_line(self._partial)
            self._partial = ""
        self._file.write(f"[driver] {self._mask(message)}\n")
        self._file.flush()

    def close(self) -> None:
        if self._partial:
            self._write_line(self._partial)
            self._partial = ""
        self._file.close()


class Normalizer:
    """ANSI の制御列を除き、改行を `\\n` に揃える。read の境界で分断された制御列と `\\r` は次へ持ち越す。"""

    _CARRY_LIMIT = 256

    def __init__(self) -> None:
        self._carry = ""

    def feed(self, text: str) -> str:
        text = self._carry + text
        self._carry = ""
        cut = text.rfind("\x1b")
        if cut != -1:
            tail = text[cut:]
            unfinished = _ANSI.match(tail) is None or (tail.startswith("\x1b]") and "\x07" not in tail)
            if unfinished:
                text = text[:cut]
                if len(tail) <= self._CARRY_LIMIT:
                    self._carry = tail  # 長すぎる未完の制御列は本文に出さず捨てる
        text = _ANSI.sub("", text)
        if text.endswith("\r"):
            self._carry = "\r" + self._carry
            text = text[:-1]
        return text.replace("\r\n", "\n").replace("\r", "\n")

    def flush(self) -> str:
        rest, self._carry = self._carry, ""
        cut = rest.find("\x1b")
        if cut != -1:
            rest = rest[:cut]  # EOF まで完結しなかった制御列は捨てる
        return rest.replace("\r\n", "\n").replace("\r", "\n")


def _stop(pid: int) -> int:
    """子の process group を止めて終了状態を返す (pid は自分が起動した子だけ)。"""
    status = 0
    for sig, wait in ((signal.SIGTERM, 3.0), (signal.SIGKILL, 3.0)):
        try:
            os.killpg(pid, sig)
        except ProcessLookupError:
            pass
        except PermissionError:
            pass
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            try:
                done, status = os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                return 0
            if done:
                return _exit_code(status)
            time.sleep(0.05)
    return _exit_code(status)


def _exit_code(status: int) -> int:
    if os.WIFEXITED(status):
        return os.WEXITSTATUS(status)
    if os.WIFSIGNALED(status):
        return 128 + os.WTERMSIG(status)
    return 1


def _spawn(command: list[str], cwd: Path, environ: dict[str, str]) -> tuple[int, int]:
    pid, master = pty.fork()
    if pid == 0:
        try:
            os.chdir(cwd)
            os.execvpe(command[0], command, environ)
        except OSError as error:
            os.write(2, f"起動できません: {command[0]} ({error.strerror})\n".encode())
        os._exit(127)
    try:
        fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack("HHHH", 50, 200, 0, 0))
    except OSError:
        pass
    return pid, master


def _match(steps: list[Step], index: int, prompt: str) -> int | None:
    for candidate in range(index, len(steps)):
        if steps[candidate].expect in prompt:
            return candidate
        if not steps[candidate].optional:
            break
    return None


def _unexpected_message(prompt: str, steps: list[Step], index: int) -> str:
    if index < len(steps):
        return (f"予期しない問い: 「{prompt.strip()}」 (台本の {index + 1} 番目は "
                f"「{steps[index].expect}」を待っていました。何も送らずに止めます)")
    return f"予期しない問い: 「{prompt.strip()}」 (台本はすべて使い切りました。何も送らずに止めます)"


def run(command: list[str], steps: list[Step], *, cwd: Path, transcript_path: Path,
        total_timeout: float, step_timeout: float, settle: float, unexpected_after: float = 30.0) -> int:
    environ = dict(os.environ)
    environ.setdefault("TERM", "dumb")
    environ.setdefault("NO_COLOR", "1")
    environ.setdefault("PYTHONUNBUFFERED", "1")
    answers: list[str] = []
    problem: str | None = None
    for step in steps:
        try:
            answers.append(expand(step.send, environ))
        except ScriptError as error:
            problem = str(error)
            break
    long_expanded = [value for step, value in zip(steps, answers)
                     if value != step.send and len(value) >= _MASK_MIN_LENGTH]
    transcript = Transcript(transcript_path, tuple(
        long_expanded + [value for value in answers if len(value) >= _MASK_MIN_LENGTH]))
    if problem is not None:
        transcript.event(problem + " (子は起動していません)")
        transcript.close()
        return RC_USAGE
    pid, master = _spawn(command, cwd, environ)
    normalizer = Normalizer()
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    started = time.monotonic()
    step_started = started
    last_output = started
    index = 0
    buffer = ""
    rc: int | None = None
    try:
        while True:
            now = time.monotonic()
            limit = (steps[index].timeout if index < len(steps) and steps[index].timeout else step_timeout)
            if now - started > total_timeout:
                transcript.event(f"timeout: 全体の上限 {total_timeout:g} 秒を超えました")
                rc = RC_TIMEOUT
                break
            if now - step_started > limit:
                prompt = buffer.rsplit("\n", 1)[-1]
                if prompt and now - last_output >= settle and _match(steps, index, prompt) is None:
                    transcript.event(_unexpected_message(prompt, steps, index))
                    rc = RC_UNEXPECTED
                    break
                waiting = steps[index].expect if index < len(steps) else "(台本の終わり)"
                transcript.event(f"timeout: 1 問の上限 {limit:g} 秒を超えました (待っていた問い: {waiting})")
                rc = RC_TIMEOUT
                break
            ready, _, _ = select.select([master], [], [], 0.1)
            if ready:
                try:
                    data = os.read(master, 4096)
                except OSError as error:
                    if error.errno != errno.EIO:
                        raise
                    data = b""
                if not data:
                    tail = normalizer.flush()
                    if tail:
                        transcript.feed(tail)
                    break
                text = normalizer.feed(decoder.decode(data))
                transcript.feed(text)
                buffer += text
                last_output = time.monotonic()
                continue
            if not buffer or time.monotonic() - last_output < settle:
                continue
            prompt = buffer.rsplit("\n", 1)[-1]
            if not prompt:
                continue  # 行が閉じている = 子はまだ処理中
            target = _match(steps, index, prompt)
            if target is None:
                if time.monotonic() - last_output < unexpected_after:
                    continue  # 進捗表示かもしれない: 出力が増えない状態が続くまで待つ
                transcript.event(_unexpected_message(prompt, steps, index))
                rc = RC_UNEXPECTED
                break
            os.write(master, (answers[target] + "\n").encode())
            index = target + 1
            buffer = ""
            step_started = time.monotonic()
            last_output = step_started
        if rc is None:
            # 子が出力を閉じた: 終了を待つ
            child_rc = None
            wait_until = time.monotonic() + 10
            while time.monotonic() < wait_until:
                try:
                    done, status = os.waitpid(pid, os.WNOHANG)
                except ChildProcessError:
                    child_rc = 0
                    break
                if done:
                    child_rc = _exit_code(status)
                    break
                time.sleep(0.05)
            if child_rc is None:
                child_rc = _stop(pid)
                transcript.event("子が出力を閉じたあと終了しないので止めました")
            left = [s for s in steps[index:] if not s.optional]
            if left:
                transcript.event(f"台本が残ったまま子が終了しました (rc={child_rc}、"
                                 f"次に待っていた問い: 「{left[0].expect}」)")
                rc = RC_SCRIPT_LEFT
            elif child_rc != 0:
                transcript.event(f"台本は済みましたが子の終了コードが {child_rc} です")
                rc = RC_CHILD_FAILED
            else:
                transcript.event("完走しました (子の終了コード 0)")
                rc = RC_OK
        else:
            _stop(pid)
    finally:
        try:
            os.close(master)
        except OSError:
            pass
        transcript.close()
    return rc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="first_run_driver.py",
        description="`uv run afx init` を端末として起動し、台本の答えを送って全出力を記録する。"
                    "予期しない問いが来たら何も送らずに止める。",
        epilog="コマンドを変えるときは `--` の後ろに書く (例: -- python3 fake.py)。"
               "記録に出る行のうち http / key / token / secret / password を含む行は *** に伏せる。"
               "4 文字未満の send の値は伏せない。秘密 (鍵・token・URL) を台本の値に書かないこと。"
               "番号や y/n は伏せる対象ではない。")
    parser.add_argument("--script", type=Path, default=DEFAULT_SCRIPT,
                        help="台本 JSON (既定: tests/acceptance/scripts/main-claude.json)")
    parser.add_argument("--root", type=Path, default=REPOSITORY,
                        help="子を起動する作業ディレクトリ (既定: この repo の root)")
    parser.add_argument("--out-dir", type=Path, default=None,
                        help="記録の置き場 (既定: <root>/tmp/acceptance/<UTC 時刻>/)")
    parser.add_argument("--total-timeout", type=float, default=7200.0, help="全体の上限秒 (既定 7200)")
    parser.add_argument("--step-timeout", type=float, default=300.0,
                        help="1 問ごとの上限秒 (既定 300。台本の timeout が優先)")
    parser.add_argument("--settle", type=float, default=0.3,
                        help="出力が止まってから問いと見なすまでの秒 (既定 0.3)")
    parser.add_argument("--unexpected-after", type=float, default=30.0,
                        help="どの問いにも合わないまま出力が増えない状態がこの秒数続いたら予期しない問いとする (既定 30)")
    parser.add_argument("--dry-run", action="store_true", help="台本の検証だけ行う (何も起動せず、何も書かない)")
    parser.add_argument("command", nargs="*", help="`--` の後ろ: 起動するコマンド (既定: uv run afx init)")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        steps = load_script(args.script)
    except ScriptError as error:
        print(f"台本の誤り: {error}", file=sys.stderr)
        return RC_USAGE
    names = variables_in(steps)
    if args.dry_run:
        print(f"台本 OK: {args.script} ({len(steps)} 問)")
        for number, step in enumerate(steps, 1):
            tail = " (optional)" if step.optional else ""
            print(f"  {number}. expect={step.expect!r}{tail}")
        for name in names:
            print(f"  環境変数 {name}: {'設定あり' if name in os.environ else '未設定'}")
        return RC_OK
    command = list(args.command) or list(DEFAULT_COMMAND)
    root = args.root.resolve()
    if not root.is_dir():
        print(f"--root がディレクトリではありません: {root}", file=sys.stderr)
        return RC_USAGE
    out_dir = args.out_dir or root / "tmp" / "acceptance" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    transcript_path = out_dir / "transcript.txt"
    print(f"記録: {transcript_path}", file=sys.stderr)
    rc = run(command, steps, cwd=root, transcript_path=transcript_path, total_timeout=args.total_timeout,
             step_timeout=args.step_timeout, settle=args.settle,
             unexpected_after=args.unexpected_after)
    print(f"終了コード {rc} (記録: {transcript_path})", file=sys.stderr)
    return rc


if __name__ == "__main__":
    sys.exit(main())
