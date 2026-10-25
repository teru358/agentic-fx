"""`afx completion bash` renders a bash completion function from the command records."""
from __future__ import annotations

import dataclasses
import re
import shlex
import shutil
import subprocess
import sys

import pytest

from agentic_fx import cli_surface, completion, entry
from tests.status_support import manifest
from tests.test_cli_surface_integration import _CTL_GRAMMAR

BASH = shutil.which("bash")
needs_bash = pytest.mark.skipif(BASH is None, reason="bash is not installed")

FORBIDDEN_WORDS = {"approvals", "reconf", "verify-backend", "stop", "help"}


def _script() -> str:
    return completion.render_bash()


def _help_choices(argv: list[str], capsys) -> set[str]:
    with pytest.raises(SystemExit) as exited:
        entry.main([*argv, "--help"])
    assert exited.value.code == 0
    usage = capsys.readouterr().out.split("\n\n", 1)[0]
    found = re.search(r"\{([^}]*)\}", usage)
    assert found, usage
    return set(found.group(1).split(","))


@pytest.fixture
def script_file(tmp_path):
    path = tmp_path / "afx-completion.bash"
    path.write_text(_script(), encoding="utf-8")
    return path


def _complete(script_file, words: list[str], *, cword: int | None = None, cwd=None, prelude: str = "") -> list[str]:
    index = len(words) - 1 if cword is None else cword
    code = (f"{prelude + '; ' if prelude else ''}source {shlex.quote(str(script_file))}; "
            f"COMP_WORDS=({' '.join(shlex.quote(word) for word in words)}); COMP_CWORD={index}; "
            '_afx; printf "%s\\n" "${COMPREPLY[@]}"')
    done = subprocess.run([BASH, "--norc", "--noprofile", "-c", code], capture_output=True, text=True,
                          cwd=cwd, check=True)
    assert done.stderr == ""
    return [line for line in done.stdout.splitlines() if line]


# --- generation ---

@needs_bash
def test_the_generated_script_passes_bash_syntax_check_and_ends_with_the_registration(tmp_path):
    script = _script()
    path = tmp_path / "script.bash"
    path.write_text(script, encoding="utf-8")
    subprocess.run([BASH, "-n", str(path)], check=True)
    assert script.rstrip("\n").splitlines()[-1] == "complete -F _afx afx"


def test_generation_is_deterministic_and_matches_the_command_output(capsys, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert entry.main(["completion", "bash"]) == 0
    out = capsys.readouterr()
    assert out.err == ""
    assert out.out == _script() == completion.render_bash()


def _without(command):
    return tuple(r for r in cli_surface.COMMANDS if r.command != command)


def test_adding_a_record_changes_the_output():
    extra = dataclasses.replace(
        next(r for r in cli_surface.COMMANDS if r.command == "status"),
        command="status extra", usage="afx status extra")
    changed = completion.render_bash((*cli_surface.COMMANDS, extra))
    assert changed != _script()
    assert "extra" in changed
    assert "extra" not in _script()


def test_removing_a_record_changes_the_output():
    changed = completion.render_bash(_without("analyze corr"))
    assert changed != _script()
    assert "corr" in _script()
    assert "corr" not in changed


def test_flipping_exposed_changes_the_output_in_both_directions():
    flipped = tuple(dataclasses.replace(r, exposed=False) if r.command == "plugin list" else r
                    for r in cli_surface.COMMANDS)
    assert completion.render_bash(flipped) != _script()
    assert "list" not in completion.candidate_table(flipped)[("plugin",)]
    reserved = tuple(dataclasses.replace(r, planned=True, implemented=False, exposed=True)
                     if r.command == "plugin list" else r for r in cli_surface.COMMANDS)
    # a planned record never becomes a candidate even when it is marked exposed
    assert "list" not in completion.candidate_table(reserved)[("plugin",)]
    bot = tuple(dataclasses.replace(r, exposed=True, implemented=True) if r.command == "ops bot reconfigure"
                else r for r in cli_surface.COMMANDS)
    assert "reconfigure" in completion.candidate_table(bot)[("ops", "bot")]


def test_changing_only_a_summary_leaves_the_output_alone():
    changed = tuple(dataclasses.replace(r, summary="別の説明") for r in cli_surface.COMMANDS)
    assert completion.render_bash(changed) == _script()


# --- the candidate sets agree with the parsers ---

def test_top_level_candidates_equal_the_help_choices(capsys):
    assert set(completion.candidate_table()[()]) == _help_choices([], capsys)


@pytest.mark.parametrize("parent", ["check", "keys", "ops", "completion", "history", "backtest",
                                    "analyze", "plugin"])
def test_parent_candidates_equal_the_parent_help_choices(parent, capsys):
    assert set(completion.candidate_table()[(parent,)]) == _help_choices([parent], capsys)


@pytest.mark.parametrize("feature", ["bot", "web", "notify"])
def test_ops_feature_candidates_equal_the_feature_help_choices(feature, capsys):
    assert set(completion.candidate_table()[("ops", feature)]) == _help_choices(["ops", feature], capsys)


def test_ctl_candidates_are_the_canonical_words_of_the_ctl_grammar():
    from agentic_fx.ops.client import parse

    table = completion.candidate_table()
    grammar = {usage: tokens for usage, (tokens, _name) in _CTL_GRAMMAR.items() if usage != "afx stop"}
    firsts = {tokens[0] for tokens in grammar.values()}
    assert set(table[("ctl",)]) == firsts
    seconds: dict[str, set[str]] = {}
    for usage, tokens in grammar.items():
        words = usage.split()[2:]
        if len(words) > 1 and re.fullmatch(r"[a-z]+", words[1]):
            seconds.setdefault(words[0], set()).add(words[1])
    for first, verbs in seconds.items():
        assert set(table[("ctl", first)]) == verbs, first
        for verb in verbs:
            sample = next(tokens for tokens in grammar.values() if tokens[:2] == [first, verb])
            parse(sample)
    assert {key[1] for key in table if len(key) == 2 and key[0] == "ctl"} == set(seconds)


def test_no_planned_private_alias_or_retired_word_is_a_candidate():
    table = completion.candidate_table()
    for key, words in table.items():
        assert not FORBIDDEN_WORDS & set(words), (key, words)
    assert "improve" not in table[()]
    assert "retry" not in table[("ctl",)]
    assert "retry" in table[("ctl", "approval")]
    assert "reconfigure" not in table[("ops", "bot")]
    assert "reconfigure" not in table[("ops", "notify")]
    assert "reconfigure" in table[("ops", "web")]
    assert not any(word in _script() for word in ("verify-backend", "approvals"))
    assert "news" in table[()]
    assert table[("news",)] == ["list", "test", "add", "enable", "disable"]


# --- a real bash ---

@needs_bash
@pytest.mark.parametrize(("words", "expected"), [
    (["afx", "ct"], ["ctl"]),
    (["afx", "ctl", "pol"], ["policy"]),
    (["afx", "ctl", "policy", ""], ["add", "show", "remove", "reconcile"]),
    (["afx", "ops", "w"], ["web"]),
    (["afx", "ops", "web", ""], ["enable", "disable", "reconfigure", "status"]),
    (["afx", "ctl", "--as", ""], ["operator", "approver"]),
    (["afx", "completion", ""], ["bash"]),
])
def test_a_real_bash_completes_the_documented_cases(script_file, words, expected):
    assert sorted(_complete(script_file, words)) == sorted(expected)


@needs_bash
def test_a_real_bash_completion_survives_a_newline_ifs(script_file):
    replies = _complete(script_file, ["afx", "ops", "web", ""], prelude="IFS=$'\\n'")
    assert sorted(replies) == ["disable", "enable", "reconfigure", "status"]


@needs_bash
def test_a_real_bash_does_not_offer_reconfigure_for_bot(script_file):
    replies = _complete(script_file, ["afx", "ops", "bot", ""])
    assert sorted(replies) == ["disable", "enable", "status"]


@needs_bash
@pytest.mark.parametrize(("words", "expected"), [
    (["afx", "--root", "/somewhere", "ctl", "pol"], ["policy"]),
    (["afx", "--daemon", "--root", "/somewhere", "ops", "web", "st"], ["status"]),
    (["afx", "ctl", "--as", "operator", "back"], ["backlog"]),
    (["afx", "ctl", "--as", "approver", "backlog", ""], ["add", "list", "reject", "reopen", "note"]),
    (["afx", "-"], ["--root", "--daemon"]),
    (["afx", "ctl", "-"], ["--as"]),
    (["afx", "status", ""], []),
    (["afx", "ctl", "approve", ""], []),
    (["afx", "ctl", "jobs", ""], ["get", "cancel"]),
    (["afx", "keys", ""], ["init", "enroll", "rotate", "revoke"]),
])
def test_a_real_bash_skips_option_values_and_never_completes_arguments(script_file, words, expected):
    assert sorted(_complete(script_file, words)) == sorted(expected)


@needs_bash
def test_a_real_bash_completes_the_root_option_value_with_directories(script_file, tmp_path):
    (tmp_path / "alpha").mkdir()
    (tmp_path / "alpine").mkdir()
    (tmp_path / "afile").write_text("", encoding="utf-8")
    replies = _complete(script_file, ["afx", "--root", "al"], cwd=tmp_path)
    assert sorted(replies) == ["alpha", "alpine"]


@needs_bash
def test_a_real_bash_completion_is_registered_by_eval(tmp_path):
    code = f'eval "$({shlex.quote(sys.executable)} -m agentic_fx.entry completion bash)"; complete -p afx'
    done = subprocess.run([BASH, "--norc", "--noprofile", "-c", code], capture_output=True, text=True,
                          cwd=tmp_path, check=True)
    assert done.stdout.strip() == "complete -F _afx afx"


# --- unsupported shells and zero-write ---

@pytest.mark.parametrize("shell", ["zsh", "fish", "sh"])
def test_other_shells_stop_with_a_fixed_sentence(shell, capsys, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    before = manifest(tmp_path)
    assert entry.main(["completion", shell]) == 2
    out = capsys.readouterr()
    assert out.out == ""
    assert out.err == "afx: 補完は bash だけです\n"
    assert manifest(tmp_path) == before


def test_generation_writes_nothing_and_ignores_a_bad_root_setting(tmp_path, monkeypatch, capsys):
    work = tmp_path / "work"
    (work / "config").mkdir(parents=True)
    (work / "config" / "settings.yaml").write_text("x: 1\n", encoding="utf-8")
    monkeypatch.chdir(work)
    monkeypatch.setenv("AFX_ROOT", str(tmp_path / "does-not-exist"))
    before = manifest(tmp_path)
    assert entry.main(["completion", "bash"]) == 0
    assert capsys.readouterr().out == _script()
    assert manifest(tmp_path) == before


def test_the_command_runs_as_a_separate_process_with_clean_streams(tmp_path):
    done = subprocess.run([sys.executable, "-m", "agentic_fx.entry", "completion", "bash"],
                          capture_output=True, text=True, cwd=tmp_path, check=False)
    assert (done.returncode, done.stderr) == (0, "")
    assert done.stdout == _script()
    assert manifest(tmp_path) == {}
    refused = subprocess.run([sys.executable, "-m", "agentic_fx.entry", "completion", "zsh"],
                             capture_output=True, text=True, cwd=tmp_path, check=False)
    assert (refused.returncode, refused.stdout, refused.stderr) == (2, "", "afx: 補完は bash だけです\n")


def test_completion_is_listed_once_in_the_principle_table_of_the_operations_document():
    from pathlib import Path

    text = (Path(__file__).resolve().parents[1] / "docs" / "operations" / "ops-api-2026-10-09.md"
            ).read_text(encoding="utf-8")
    rows = [line for line in text.splitlines()
            if line.startswith("| ") and "`afx completion bash`" in line]
    assert len(rows) == 1
    assert rows[0].startswith("| 導入・接続管理 |")
    assert "`[読取]` `afx completion bash`" in rows[0]
