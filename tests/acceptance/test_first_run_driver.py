from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

ACCEPTANCE = Path(__file__).resolve().parent
REPOSITORY = ACCEPTANCE.parents[1]
DRIVER = ACCEPTANCE / "first_run_driver.py"
SCRIPTS = ACCEPTANCE / "scripts"
SOURCES = REPOSITORY / "src" / "agentic_fx" / "setup"


def _write(path: Path, text: str) -> Path:
    path.write_text(textwrap.dedent(text), encoding="utf-8")
    return path


def _script(tmp_path: Path, steps: object, name: str = "script.json") -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(steps, ensure_ascii=False), encoding="utf-8")
    return path


def _drive(tmp_path: Path, script: Path, child: Path | None, *extra: str, env: dict[str, str] | None = None,
           timeout: float = 60.0) -> tuple[subprocess.CompletedProcess[str], Path]:
    out = tmp_path / "out"
    command = [sys.executable, "-I", str(DRIVER), "--script", str(script), "--root", str(tmp_path),
               "--out-dir", str(out), "--settle", "0.15", "--unexpected-after", "1", *extra]
    if child is not None:
        command += ["--", sys.executable, "-I", str(child)]
    environment = {key: value for key, value in os.environ.items() if key not in {"VIRTUAL_ENV"}}
    environment.update(env or {})
    completed = subprocess.run(command, capture_output=True, text=True, env=environment, timeout=timeout,
                               check=False)
    return completed, out / "transcript.txt"


QUESTIONS = """
    print("[段 1 はじめに]")
    first = input("読みました [Enter]: ")
    print("got-first=%r" % first)
    print("")
    print("[段 3 価格源]")
    second = input("価格源: ")
    print("got-second=%r" % second)
    print("done")
"""


def test_answers_by_script_and_records_transcript(tmp_path: Path) -> None:
    child = _write(tmp_path / "child.py", QUESTIONS)
    script = _script(tmp_path, [{"expect": "読みました", "send": ""}, {"expect": "価格源", "send": "3"}])
    completed, transcript = _drive(tmp_path, script, child)
    assert completed.returncode == 0, completed.stderr
    text = transcript.read_text(encoding="utf-8")
    assert "[段 1 はじめに]" in text and "読みました [Enter]: " in text
    assert "got-first=''" in text and "got-second='3'" in text
    assert "完走しました" in text


def test_unexpected_question_stops_without_sending_anything(tmp_path: Path) -> None:
    marker = tmp_path / "child.pid"
    child = _write(tmp_path / "child.py", f"""
        import os, time
        open({str(marker)!r}, "w").write(str(os.getpid()))
        first = input("読みました [Enter]: ")
        answer = input("続けますか [y/N] ")
        print("RECEIVED=%r" % answer)
        time.sleep(60)
    """)
    script = _script(tmp_path, [{"expect": "読みました", "send": ""}])
    completed, transcript = _drive(tmp_path, script, child)
    assert completed.returncode == 3
    text = transcript.read_text(encoding="utf-8")
    assert "予期しない問い" in text and "続けますか" in text
    assert "RECEIVED" not in text
    pid = int(marker.read_text())
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        pytest.fail("止めたはずの子 process が残っている")


def test_does_not_answer_a_question_that_only_looks_like_the_expected_one_earlier(tmp_path: Path) -> None:
    # 再質問 (同じ問いの繰り返し) には台本の次の要素で答える。1 つ目の答えを使い回さない。
    child = _write(tmp_path / "child.py", """
        a = input("価格源: ")
        print("first=%r" % a)
        b = input("価格源: ")
        print("second=%r" % b)
    """)
    script = _script(tmp_path, [{"expect": "価格源", "send": "x"}, {"expect": "価格源", "send": "3"}])
    completed, transcript = _drive(tmp_path, script, child)
    assert completed.returncode == 0, completed.stderr
    text = transcript.read_text(encoding="utf-8")
    assert "first='x'" in text and "second='3'" in text


def test_step_timeout_stops_with_rc4_and_kills_child(tmp_path: Path) -> None:
    marker = tmp_path / "child.pid"
    child = _write(tmp_path / "child.py", f"""
        import os, time
        open({str(marker)!r}, "w").write(str(os.getpid()))
        time.sleep(60)
        input("読みました [Enter]: ")
    """)
    script = _script(tmp_path, [{"expect": "読みました", "send": ""}])
    started = time.monotonic()
    completed, transcript = _drive(tmp_path, script, child, "--step-timeout", "1")
    assert completed.returncode == 4
    assert time.monotonic() - started < 30
    assert "timeout" in transcript.read_text(encoding="utf-8")
    pid = int(marker.read_text())
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.05)
    pytest.fail("timeout で止めたはずの子 process が残っている")


def test_total_timeout_stops_even_when_each_question_is_answered(tmp_path: Path) -> None:
    child = _write(tmp_path / "child.py", """
        import time
        while True:
            input("読みました [Enter]: ")
            time.sleep(0.4)
    """)
    script = _script(tmp_path, [{"expect": "読みました", "send": ""}] * 50)
    completed, transcript = _drive(tmp_path, script, child, "--total-timeout", "2")
    assert completed.returncode == 4
    assert "全体の上限" in transcript.read_text(encoding="utf-8")


def test_per_question_timeout_in_script_wins_over_default(tmp_path: Path) -> None:
    child = _write(tmp_path / "child.py", """
        import time
        time.sleep(1.5)
        print(input("読みました [Enter]: ") == "")
    """)
    script = _script(tmp_path, [{"expect": "読みました", "send": "", "timeout": 20}])
    completed, _ = _drive(tmp_path, script, child, "--step-timeout", "1")
    assert completed.returncode == 0, completed.stderr


def test_child_exit_with_script_left_is_rc5_and_nonzero_child_is_rc1(tmp_path: Path) -> None:
    child = _write(tmp_path / "child.py", """
        input("読みました [Enter]: ")
        print("終わり")
    """)
    script = _script(tmp_path, [{"expect": "読みました", "send": ""}, {"expect": "価格源", "send": "3"}])
    completed, transcript = _drive(tmp_path, script, child)
    assert completed.returncode == 5
    assert "台本が残ったまま" in transcript.read_text(encoding="utf-8")

    failing = _write(tmp_path / "failing.py", """
        import sys
        input("読みました [Enter]: ")
        sys.exit(3)
    """)
    completed, _ = _drive(tmp_path, _script(tmp_path, [{"expect": "読みました", "send": ""}], "one.json"), failing)
    assert completed.returncode == 1


def test_missing_command_is_reported_not_hung(tmp_path: Path) -> None:
    script = _script(tmp_path, [{"expect": "読みました", "send": ""}])
    out = tmp_path / "out"
    completed = subprocess.run(
        [sys.executable, "-I", str(DRIVER), "--script", str(script), "--root", str(tmp_path), "--out-dir", str(out),
         "--", str(tmp_path / "no-such-command")], capture_output=True, text=True, timeout=60, check=False)
    assert completed.returncode == 5
    assert "起動できません" in (out / "transcript.txt").read_text(encoding="utf-8")


def test_optional_step_is_skipped_when_its_question_does_not_come(tmp_path: Path) -> None:
    child = _write(tmp_path / "child.py", """
        input("読みました [Enter]: ")
        print("got=%r" % input("起動しますか [y/N] "))
    """)
    script = _script(tmp_path, [
        {"expect": "読みました", "send": ""},
        {"expect": "enable-linger", "send": "y", "optional": True},
        {"expect": "起動しますか", "send": "y"}])
    completed, transcript = _drive(tmp_path, script, child)
    assert completed.returncode == 0, completed.stderr
    assert "got='y'" in transcript.read_text(encoding="utf-8")


def test_optional_step_answers_when_its_question_comes(tmp_path: Path) -> None:
    child = _write(tmp_path / "child.py", """
        input("読みました [Enter]: ")
        print("linger=%r" % input("enable-linger を実行しますか [y/N] "))
        print("got=%r" % input("起動しますか [y/N] "))
    """)
    script = _script(tmp_path, [
        {"expect": "読みました", "send": ""},
        {"expect": "enable-linger", "send": "y", "optional": True},
        {"expect": "起動しますか", "send": "n"}])
    completed, transcript = _drive(tmp_path, script, child)
    assert completed.returncode == 0, completed.stderr
    text = transcript.read_text(encoding="utf-8")
    assert "linger='y'" in text and "got='n'" in text


def test_secret_and_url_lines_are_masked_in_transcript(tmp_path: Path) -> None:
    child = _write(tmp_path / "child.py", """
        print("MT5_BRIDGE_API_KEY=abc123 が必要です")
        value = input("llama-swap の URL: ")
        print("使う宛先は " + value)
        input("読みました [Enter]: ")
        print("普通の行です")
    """)
    script = _script(tmp_path, [
        {"expect": "llama-swap の URL", "send": "http://secret-host.example/v1"},
        {"expect": "読みました", "send": ""}])
    completed, transcript = _drive(tmp_path, script, child)
    assert completed.returncode == 0, completed.stderr
    text = transcript.read_text(encoding="utf-8")
    for leaked in ("abc123", "secret-host", "http", "MT5_BRIDGE_API_KEY"):
        assert leaked not in text
    assert "***" in text and "普通の行です" in text
    assert "secret-host" not in completed.stderr and "secret-host" not in completed.stdout


def test_environment_variable_in_send_is_expanded_and_undefined_one_stops(tmp_path: Path) -> None:
    child = _write(tmp_path / "child.py", """
        print("path=%s" % input("codex の実体の絶対パス: "))
    """)
    script = _script(tmp_path, [{"expect": "実体の絶対パス", "send": "${FR_DRIVER_PATH}"}])
    completed, transcript = _drive(tmp_path, script, child, env={"FR_DRIVER_PATH": "/opt/codex/bin/codex"})
    assert completed.returncode == 0, completed.stderr
    text = transcript.read_text(encoding="utf-8")
    assert "path=***" in text and "/opt/codex" not in text
    environment = {key: value for key, value in os.environ.items() if key != "FR_DRIVER_PATH"}
    completed = subprocess.run(
        [sys.executable, "-I", str(DRIVER), "--script", str(script), "--root", str(tmp_path),
         "--out-dir", str(tmp_path / "out2"), "--settle", "0.15", "--", sys.executable, "-I", str(child)],
        capture_output=True, text=True, env=environment, timeout=60, check=False)
    assert completed.returncode == 2
    assert "FR_DRIVER_PATH" in (tmp_path / "out2" / "transcript.txt").read_text(encoding="utf-8")


def test_default_output_directory_is_under_root_tmp_acceptance(tmp_path: Path) -> None:
    child = _write(tmp_path / "child.py", 'input("読みました [Enter]: ")\n')
    script = _script(tmp_path, [{"expect": "読みました", "send": ""}])
    completed = subprocess.run(
        [sys.executable, "-I", str(DRIVER), "--script", str(script), "--root", str(tmp_path), "--settle", "0.15",
         "--", sys.executable, "-I", str(child)], capture_output=True, text=True, timeout=60, check=False)
    assert completed.returncode == 0, completed.stderr
    found = list((tmp_path / "tmp" / "acceptance").glob("*/transcript.txt"))
    assert len(found) == 1
    assert re.fullmatch(r"\d{8}T\d{6}Z", found[0].parent.name)


@pytest.mark.parametrize("content, message", [
    ("not json", "JSON"),
    ('{"expect": "x", "send": ""}', "配列"),
    ("[]", "配列"),
    ('[{"send": ""}]', "expect"),
    ('[{"expect": "", "send": ""}]', "expect"),
    ('[{"expect": "x"}]', "send"),
    ('[{"expect": "x", "send": 1}]', "send"),
    ('[{"expect": "x", "send": "a\\nb"}]', "改行"),
    ('[{"expect": "x", "send": "", "timeout": 0}]', "timeout"),
    ('[{"expect": "x", "send": "", "optional": "yes"}]', "optional"),
    ('[{"expect": "x", "send": "", "other": 1}]', "未知のキー"),
    ('["x"]', "オブジェクト"),
    ('[{"expect": "x", "send": "", "optional": true}]', "optional"),
])
def test_dry_run_rejects_broken_scripts(tmp_path: Path, content: str, message: str) -> None:
    path = tmp_path / "bad.json"
    path.write_text(content, encoding="utf-8")
    completed, _ = _drive(tmp_path, path, None, "--dry-run")
    assert completed.returncode == 2
    assert message in completed.stderr
    assert not (tmp_path / "out").exists()


def test_dry_run_accepts_a_valid_script_without_starting_anything(tmp_path: Path) -> None:
    marker = tmp_path / "ran"
    child = _write(tmp_path / "child.py", f'open({str(marker)!r}, "w").write("x")\n')
    script = _script(tmp_path, [{"expect": "読みました", "send": "${SOME_DRIVER_VAR}"}])
    completed, _ = _drive(tmp_path, script, child, "--dry-run")
    assert completed.returncode == 0, completed.stderr
    assert "SOME_DRIVER_VAR" in completed.stdout
    assert not marker.exists() and not (tmp_path / "out").exists()


def test_help_describes_usage_and_runs_under_isolated_python(tmp_path: Path) -> None:
    completed = subprocess.run([sys.executable, "-I", str(DRIVER), "--help"], capture_output=True, text=True,
                               timeout=60, check=False)
    assert completed.returncode == 0
    for word in ("--script", "--dry-run", "--out-dir", "--step-timeout", "--total-timeout"):
        assert word in completed.stdout


def test_driver_imports_only_the_standard_library() -> None:
    source = DRIVER.read_text(encoding="utf-8")
    assert "agentic_fx" not in source.replace("`agentic_fx` は import しない", "")
    modules = set(re.findall(r"^(?:import|from) ([a-z_]+)", source, flags=re.MULTILINE))
    assert modules <= set(sys.stdlib_module_names) | {"__future__"}


SHIPPED = ("main-claude.json", "codex-improve.json", "cli-missing.json")


@pytest.mark.parametrize("name", SHIPPED)
def test_shipped_scripts_pass_dry_run(tmp_path: Path, name: str) -> None:
    completed = subprocess.run([sys.executable, "-I", str(DRIVER), "--script", str(SCRIPTS / name), "--dry-run"],
                               capture_output=True, text=True, timeout=60, check=False)
    assert completed.returncode == 0, completed.stderr


@pytest.mark.parametrize("name", SHIPPED)
def test_shipped_scripts_expect_text_exists_in_the_wizard_source(name: str) -> None:
    sources = "\n".join(path.read_text(encoding="utf-8") for path in sorted(SOURCES.glob("*.py")))
    steps = json.loads((SCRIPTS / name).read_text(encoding="utf-8"))
    missing = [step["expect"] for step in steps if step["expect"] not in sources]
    assert not missing, f"実装に無い文面を expect にしている: {missing}"


@pytest.mark.parametrize("name", SHIPPED)
def test_shipped_scripts_never_send_secrets_or_urls(name: str) -> None:
    steps = json.loads((SCRIPTS / name).read_text(encoding="utf-8"))
    for step in steps:
        assert not re.search(r"http|key|token|secret|password", step["send"], re.IGNORECASE)


def test_main_script_walks_every_question_stage_in_order() -> None:
    steps = json.loads((SCRIPTS / "main-claude.json").read_text(encoding="utf-8"))
    expects = [step["expect"] for step in steps]
    order = ["読みました", "表示タイムゾーン", "口座通貨", "対象ペア", "初期残高", "価格源", "LLM の構成",
             "判断足", "改善の周期", "改善の時刻", "最初の 1 本", "afx を常駐サービス", "書き込みますか",
             "方針文", "最初の課題", "起動しますか"]
    positions = []
    for needle in order:
        found = [i for i, text in enumerate(expects) if needle in text]
        assert found, f"主シナリオに {needle} の問いが無い"
        positions.append(found[0])
    assert positions == sorted(positions)


def test_undefined_variable_stops_before_anything_is_started(tmp_path: Path) -> None:
    started = tmp_path / "started.txt"
    child = _write(tmp_path / "child.py", f"""
        open({str(started)!r}, "w").write("started")
        input("最初の問い: ")
        input("実体のパス: ")
    """)
    script = _script(tmp_path, [{"expect": "最初の問い", "send": "1"},
                                {"expect": "実体のパス", "send": "${FR_DRIVER_UNDEFINED}"}])
    environment = {key: value for key, value in os.environ.items() if key != "FR_DRIVER_UNDEFINED"}
    completed = subprocess.run(
        [sys.executable, "-I", str(DRIVER), "--script", str(script), "--root", str(tmp_path),
         "--out-dir", str(tmp_path / "out"), "--", sys.executable, "-I", str(child)],
        capture_output=True, text=True, env=environment, timeout=60, check=False)
    assert completed.returncode == 2
    assert not started.exists()
    assert "FR_DRIVER_UNDEFINED" in (completed.stderr + (tmp_path / "out" / "transcript.txt").read_text(encoding="utf-8"))


def test_silent_progress_line_is_not_taken_for_an_unexpected_question(tmp_path: Path) -> None:
    child = _write(tmp_path / "child.py", """
        import sys, time
        sys.stdout.write("確認中 ...")
        sys.stdout.flush()
        time.sleep(1.2)
        sys.stdout.write("\\r確認が済みました\\n")
        value = input("価格源: ")
        print("got=%r" % value)
    """)
    script = _script(tmp_path, [{"expect": "価格源", "send": "3"}])
    completed, transcript = _drive(tmp_path, script, child, "--unexpected-after", "5")
    assert completed.returncode == 0, completed.stderr
    assert "got='3'" in transcript.read_text(encoding="utf-8")


def test_a_really_unknown_question_is_unexpected_after_the_quiet_period(tmp_path: Path) -> None:
    child = _write(tmp_path / "child.py", """
        import time
        input("知らない問い [y/N] ")
        time.sleep(30)
    """)
    script = _script(tmp_path, [{"expect": "価格源", "send": "3"}])
    started = time.monotonic()
    completed, transcript = _drive(tmp_path, script, child, "--unexpected-after", "1")
    assert completed.returncode == 3
    assert time.monotonic() - started < 20
    assert "予期しない問い" in transcript.read_text(encoding="utf-8")


def test_output_that_keeps_growing_postpones_the_unexpected_verdict(tmp_path: Path) -> None:
    child = _write(tmp_path / "child.py", """
        import sys, time
        for _ in range(8):
            sys.stdout.write("."); sys.stdout.flush()
            time.sleep(0.4)
        value = input("価格源: ")
        print("got=%r" % value)
    """)
    script = _script(tmp_path, [{"expect": "価格源", "send": "3"}])
    completed, transcript = _drive(tmp_path, script, child, "--unexpected-after", "1")
    assert completed.returncode == 0, completed.stderr
    assert "got='3'" in transcript.read_text(encoding="utf-8")


def test_expanded_and_sent_values_are_masked_even_when_the_prompt_has_no_secret_word(tmp_path: Path) -> None:
    child = _write(tmp_path / "child.py", """
        path = input("実体のパス: ")
        print("使う実体は " + path)
        other = input("別名: ")
        print("別名は " + other)
        print("普通の行です")
    """)
    script = _script(tmp_path, [{"expect": "実体のパス", "send": "${FR_DRIVER_PLAIN}"},
                                {"expect": "別名", "send": "literal-answer-42"}])
    completed, transcript = _drive(tmp_path, script, child, env={"FR_DRIVER_PLAIN": "/opt/plain-value-777/bin"})
    assert completed.returncode == 0, completed.stderr
    text = transcript.read_text(encoding="utf-8")
    assert "plain-value-777" not in text and "literal-answer-42" not in text
    assert "実体のパス: ***" in text and "普通の行です" in text


def test_escape_sequence_split_at_the_read_boundary_is_removed(tmp_path: Path) -> None:
    child = _write(tmp_path / "child.py", """
        import os
        os.write(1, b"x" * 4094 + b"\\x1b[31mred\\x1b[0m end\\n")
        input("読みました [Enter]: ")
    """)
    script = _script(tmp_path, [{"expect": "読みました", "send": ""}])
    completed, transcript = _drive(tmp_path, script, child)
    assert completed.returncode == 0, completed.stderr
    text = transcript.read_text(encoding="utf-8")
    assert "\x1b" not in text and "[31m" not in text and "[0m" not in text
    assert "x" * 4094 + "red end\n" in text


def test_crlf_split_at_the_read_boundary_does_not_add_a_blank_line(tmp_path: Path) -> None:
    child = _write(tmp_path / "child.py", """
        import os
        os.write(1, b"y" * 4095 + b"\\nafter\\n")
        input("読みました [Enter]: ")
    """)
    script = _script(tmp_path, [{"expect": "読みました", "send": ""}])
    completed, transcript = _drive(tmp_path, script, child)
    assert completed.returncode == 0, completed.stderr
    text = transcript.read_text(encoding="utf-8")
    assert "y" * 4095 + "\nafter\n" in text


def test_unknown_question_is_unexpected_not_timeout_even_when_step_timeout_is_shorter(tmp_path: Path) -> None:
    child = _write(tmp_path / "child.py", """
        import time
        input("知らない問い [y/N] ")
        time.sleep(30)
    """)
    script = _script(tmp_path, [{"expect": "価格源", "send": "3"}])
    completed, transcript = _drive(tmp_path, script, child, "--step-timeout", "2", "--unexpected-after", "30")
    assert completed.returncode == 3, completed.stderr
    assert "予期しない問い" in transcript.read_text(encoding="utf-8")


def test_silent_child_without_any_question_is_still_a_timeout(tmp_path: Path) -> None:
    child = _write(tmp_path / "child.py", """
        import time
        print("処理中です")
        time.sleep(30)
    """)
    script = _script(tmp_path, [{"expect": "価格源", "send": "3"}])
    completed, _ = _drive(tmp_path, script, child, "--step-timeout", "2", "--unexpected-after", "30")
    assert completed.returncode == 4, completed.stderr


def test_short_expanded_value_is_left_in_the_transcript(tmp_path: Path) -> None:
    child = _write(tmp_path / "child.py", """
        print("[段 1 はじめに]")
        value = input("選択: ")
        print("次へ進みます")
    """)
    script = _script(tmp_path, [{"expect": "選択", "send": "${FR_DRIVER_SHORT}"}])
    completed, transcript = _drive(tmp_path, script, child, env={"FR_DRIVER_SHORT": "1"})
    assert completed.returncode == 0, completed.stderr
    text = transcript.read_text(encoding="utf-8")
    assert "選択: 1" in text
    assert "***" not in text


def test_expanded_value_of_four_or_more_characters_is_masked(tmp_path: Path) -> None:
    child = _write(tmp_path / "child.py", """
        value = input("選択: ")
        print("受け取りました")
    """)
    script = _script(tmp_path, [{"expect": "選択", "send": "${FR_DRIVER_LONG}"}])
    completed, transcript = _drive(tmp_path, script, child, env={"FR_DRIVER_LONG": "abcd"})
    assert completed.returncode == 0, completed.stderr
    text = transcript.read_text(encoding="utf-8")
    assert "abcd" not in text and "選択: ***" in text


def _load_driver():
    import importlib.util
    spec = importlib.util.spec_from_file_location("first_run_driver_under_test", DRIVER)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_overlong_unfinished_escape_is_dropped_entirely() -> None:
    normalizer = _load_driver().Normalizer()
    out = normalizer.feed("\x1b]" + "x" * 300)
    out += normalizer.flush()
    assert out == ""


def test_unfinished_escape_at_end_of_output_is_dropped(tmp_path: Path) -> None:
    child = _write(tmp_path / "child.py", """
        import os
        os.write(1, "進捗\\x1b[".encode())
    """)
    script = _script(tmp_path, [{"expect": "なにか", "send": ""}])
    completed, transcript = _drive(tmp_path, script, child)
    text = transcript.read_text(encoding="utf-8")
    assert "\x1b" not in text
    assert "進捗" in text
