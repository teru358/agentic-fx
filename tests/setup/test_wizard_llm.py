from __future__ import annotations

import pytest

from agentic_fx.config import load_settings_text
from agentic_fx.setup.wizard import run_wizard
from agentic_fx.store.setup_record import _valid, read_setup_record

from tests.setup.wizard_support import (
    ScriptedWizardIO, codex_model_list, fresh_responder, make_deps, make_executable, make_root,
)

OTHER = "http://other-host:8080/v1"


@pytest.fixture
def home(tmp_path, monkeypatch):
    path = tmp_path / "home"
    path.mkdir()
    monkeypatch.setenv("HOME", str(path))
    return path


def login(home, kind="claude"):
    target = home / (".claude/.credentials.json" if kind == "claude" else ".codex/auth.json")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("{}")


def installed_claude(tmp_path, home, *, node=True, credentials=True):
    binary = make_executable(tmp_path / "bin" / "claude")
    if credentials:
        login(home)
    deps_kwargs = {"which": lambda name: str(binary) if name in ("claude", str(binary)) else None}
    return binary, deps_kwargs


def settings_of(root):
    return load_settings_text((root / "config" / "settings.yaml").read_text())


def record_of(root):
    return read_setup_record(root).record.data


def runner_lines_changed(root):
    example = (root / "config" / "settings.yaml.example").read_text().splitlines()
    actual = (root / "config" / "settings.yaml").read_text().splitlines()
    return [a for e, a in zip(example, actual) if e != a and ("backend" in a or "model" in a)]


def test_when_llama_swap_answers_both_runners_default_to_local(tmp_path):
    root = make_root(tmp_path)
    deps, calls, fetch, run = make_deps(tmp_path)
    io = ScriptedWizardIO(responder=fresh_responder())
    assert run_wizard(root, io, deps) == 0, io.text
    settings = settings_of(root)
    assert settings.runner.trade.backend == "local" and settings.runner.improve.backend == "local"
    assert runner_lines_changed(root) == []
    assert record_of(root)["llm_trade"]["result"] == "ok"
    assert io.asked_with("trade 用の `claude -p`") == []
    assert run.calls == []


def test_without_llama_swap_no_default_is_offered_and_later_leaves_runner_lines_alone(tmp_path):
    root = make_root(tmp_path)
    deps, calls, fetch, run = make_deps(tmp_path)
    fetch.models = {}
    io = ScriptedWizardIO(responder=fresh_responder(("LLM の構成", ("", "", "4"))))
    assert run_wizard(root, io, deps) == 0, io.text
    assert len(io.asked_with("LLM の構成")) == 3
    assert "llama-swap が応答しません。local は既定にしません。" in io.text
    for line in ("1. 取引判断に Claude", "2. 取引判断に Claude", "3. 別ホストの llama-swap", "4. 後で設定する"):
        assert line in io.text
    assert runner_lines_changed(root) == []
    data = record_of(root)
    for key in ("llm_trade", "llm_improve"):
        assert data[key]["result"] == "unverified" and data[key]["reason_code"] == "deferred_by_user"


def test_choosing_later_still_lets_the_unit_be_placed(tmp_path):
    root = make_root(tmp_path)
    deps, calls, fetch, run = make_deps(tmp_path)
    fetch.models = {}
    io = ScriptedWizardIO(responder=fresh_responder(("LLM の構成", "4"), ("systemd のユーザーサービス", "y"),
                                                   ("外から見る手段", "n"), ("enable-linger", "n")))
    run.on_prefix("loginctl", "show-user", stdout="Linger=yes\n")
    assert run_wizard(root, io, deps) == 0, io.text
    assert calls.place_unit and "未完の手順" in io.text and "LLM" in io.text


def test_claude_for_both_runners_asks_before_spending_the_subscription(tmp_path, home):
    root = make_root(tmp_path)
    binary, kwargs = installed_claude(tmp_path, home)
    deps, calls, fetch, run = make_deps(tmp_path, **kwargs)
    fetch.models = {}
    run.on("node", "--version", stdout="v22\n").on(str(binary), "--version", stdout="1.0\n")
    io = ScriptedWizardIO(responder=fresh_responder(("LLM の構成", "1")))
    assert run_wizard(root, io, deps) == 0, io.text
    settings = settings_of(root)
    assert settings.runner.trade.backend == "claude" and settings.runner.improve.backend == "claude"
    assert [m for m in run.calls if "-p" in m] == []
    data = record_of(root)
    assert data["llm_trade"]["result"] == "unverified" and data["llm_trade"]["reason_code"] == "user_declined_probe"
    assert data["llm_improve"]["result"] == "ok"
    assert io.asked_with("trade 用の `claude -p`")[0].endswith("[y/N] ")
    assert "サブスク枠を使います" in io.text


def test_claude_selection_records_the_path_found_on_path(tmp_path, home):
    root = make_root(tmp_path)
    binary, kwargs = installed_claude(tmp_path, home)
    deps, calls, fetch, run = make_deps(tmp_path, **kwargs)
    fetch.models = {}
    run.on("node", "--version", stdout="v22\n").on(str(binary), "--version", stdout="1.0\n")
    io = ScriptedWizardIO(responder=fresh_responder(("LLM の構成", "1")))

    assert run_wizard(root, io, deps) == 0, io.text

    assert settings_of(root).runner.claude.bin == str(binary)
    assert f"claude の実体: {binary}" in io.text


def test_claude_selection_keeps_an_existing_absolute_executable_path(tmp_path, home):
    root = make_root(tmp_path)
    binary, kwargs = installed_claude(tmp_path, home)
    settings = (root / "config" / "settings.yaml.example").read_text()
    (root / "config" / "settings.yaml.example").write_text(
        settings.replace("bin: claude", f"bin: {binary}"), encoding="utf-8")
    deps, calls, fetch, run = make_deps(tmp_path, **kwargs)
    fetch.models = {}
    run.on("node", "--version", stdout="v22\n").on(str(binary), "--version", stdout="1.0\n")
    io = ScriptedWizardIO(responder=fresh_responder(("LLM の構成", "1")))

    assert run_wizard(root, io, deps) == 0, io.text

    assert "runner.claude.bin" not in io.text
    assert f"claude の実体: {binary}" not in io.text


def test_claude_selection_expands_home_path_before_recording_it(tmp_path, home):
    from agentic_fx.setup import checks

    root = make_root(tmp_path)
    binary = make_executable(home / "bin" / "claude")
    login(home)
    settings = (root / "config" / "settings.yaml.example").read_text()
    (root / "config" / "settings.yaml.example").write_text(
        settings.replace("bin: claude", "bin: ~/bin/claude"), encoding="utf-8")
    deps, calls, fetch, run = make_deps(
        tmp_path, which=lambda name: str(binary) if name == "~/bin/claude" else None)
    assert checks.cli_candidate("~/bin/claude", which=deps.which) == str(binary)
    fetch.models = {}
    run.on("node", "--version", stdout="v22\n").on(str(binary), "--version", stdout="1.0\n")
    io = ScriptedWizardIO(responder=fresh_responder(("LLM の構成", "1")))

    assert run_wizard(root, io, deps) == 0, io.text

    assert settings_of(root).runner.claude.bin == str(binary)
    assert f"claude の実体: {binary}" in io.text


def test_old_landlock_abi_skips_the_improve_exchange_but_keeps_schedule_questions(tmp_path, home):
    root = make_root(tmp_path)
    binary, kwargs = installed_claude(tmp_path, home)
    deps, calls, fetch, run = make_deps(tmp_path, landlock_abi=lambda: 4, **kwargs)
    fetch.models = {}
    run.on("node", "--version", stdout="v22\n").on(str(binary), "--version", stdout="1.0\n")
    io = ScriptedWizardIO(responder=fresh_responder(("LLM の構成", "1")))

    assert run_wizard(root, io, deps) == 0, io.text

    assert "この kernel (Landlock ABI 4) では改善ループの隔離が使えません" in io.text
    assert io.asked_with("改善 backend の実往復") == []
    assert io.text.count("改善の実往復の試験は、この kernel では隔離が使えないため行いません。") == 1
    assert io.asked_with("改善の周期 (weekly / daily)")
    assert "この kernel では改善は走りません (段 0 の表示)。" in io.text
    record = record_of(root)
    assert record["landlock"]["result"] == "ok"
    assert record["landlock"]["reason_code"] == "landlock_abi_too_old"
    assert _valid(record)


def test_negative_landlock_abi_is_reported_as_zero_and_recorded_as_too_old(tmp_path, home):
    root = make_root(tmp_path)
    binary, kwargs = installed_claude(tmp_path, home)
    deps, calls, fetch, run = make_deps(tmp_path, landlock_abi=lambda: -1, **kwargs)
    fetch.models = {}
    run.on("node", "--version", stdout="v22\n").on(str(binary), "--version", stdout="1.0\n")
    io = ScriptedWizardIO(responder=fresh_responder(("LLM の構成", "1")))

    assert run_wizard(root, io, deps) == 0, io.text

    assert "Landlock ABI 0" in io.text
    assert record_of(root)["landlock"]["reason_code"] == "landlock_abi_too_old"


def test_agreeing_sends_exactly_one_claude_exchange_and_records_it(tmp_path, home):
    root = make_root(tmp_path)
    binary, kwargs = installed_claude(tmp_path, home)
    deps, calls, fetch, run = make_deps(tmp_path, **kwargs)
    fetch.models = {}
    run.on("node", "--version", stdout="v22\n").on(str(binary), "--version", stdout="1.0\n")
    run.on(str(binary), "-p", "ok だけ答えて", "--max-turns", "1", stdout="ok\n")
    io = ScriptedWizardIO(responder=fresh_responder(("LLM の構成", "1"), ("trade 用の `claude -p`", "y")))
    assert run_wizard(root, io, deps) == 0, io.text
    assert len(run.matching(str(binary), "-p")) == 1
    assert record_of(root)["llm_trade"]["result"] == "ok"


def test_a_failed_claude_exchange_is_recorded_without_stopping(tmp_path, home):
    root = make_root(tmp_path)
    binary, kwargs = installed_claude(tmp_path, home)
    deps, calls, fetch, run = make_deps(tmp_path, **kwargs)
    fetch.models = {}
    run.on("node", "--version", stdout="v22\n").on(str(binary), "--version", stdout="1.0\n")
    run.on(str(binary), "-p", "ok だけ答えて", "--max-turns", "1", returncode=1)
    io = ScriptedWizardIO(responder=fresh_responder(("LLM の構成", "1"), ("trade 用の `claude -p`", "y")))
    assert run_wizard(root, io, deps) == 0, io.text
    assert record_of(root)["llm_trade"]["result"] == "failed"


@pytest.mark.parametrize("node,binary_present,credentials,reason", [
    (True, False, True, "bin_not_found"),
    (True, True, False, "credentials_missing"),
    (False, True, True, None)])
def test_missing_cli_login_or_node_never_blocks_completion(tmp_path, home, node, binary_present, credentials, reason):
    root = make_root(tmp_path)
    binary = make_executable(tmp_path / "bin" / "claude")
    if credentials:
        login(home)
    deps, calls, fetch, run = make_deps(
        tmp_path, which=lambda name: str(binary) if binary_present and name in ("claude", str(binary)) else None)
    fetch.models = {}
    if node:
        run.on("node", "--version", stdout="v22\n")
    else:
        run.on("node", "--version", raises=FileNotFoundError())
    run.on(str(binary), "--version", stdout="1.0\n")
    io = ScriptedWizardIO(responder=fresh_responder(("LLM の構成", "1")))
    assert run_wizard(root, io, deps) == 0, io.text
    data = record_of(root)
    if reason is not None:
        assert data["llm_trade"]["result"] == "unverified" and data["llm_trade"]["reason_code"] == reason
        assert data["llm_improve"]["reason_code"] == reason
        assert io.asked_with("trade 用の `claude -p`") == []
        assert "first-run-acceptance.md" in io.text or "ログイン" in io.text
    if not node:
        assert "Node.js が見つかりません" in io.text


def test_choosing_later_from_the_cli_failure_menu_defers_the_whole_choice(tmp_path, home):
    root = make_root(tmp_path)
    deps, calls, fetch, run = make_deps(tmp_path)
    fetch.models = {}
    run.on("node", "--version", stdout="v22\n")
    io = ScriptedWizardIO(responder=fresh_responder(("LLM の構成", "1"), ("[r] 導入・ログイン後に再試行", "d")))
    assert run_wizard(root, io, deps) == 0, io.text
    assert record_of(root)["llm_trade"]["reason_code"] == "deferred_by_user"
    assert runner_lines_changed(root) == []


def test_codex_for_improvement_asks_for_the_native_binary_and_checks_login(tmp_path, home):
    root = make_root(tmp_path)
    binary, kwargs = installed_claude(tmp_path, home)
    elf = make_executable(tmp_path / "vendor" / "codex", b"\x7fELF" + b"\0" * 16)
    login(home, "codex")
    deps, calls, fetch, run = make_deps(tmp_path, **kwargs)
    fetch.models = {}
    run.on("node", "--version", stdout="v22\n").on(str(binary), "--version", stdout="1.0\n")
    run.on(str(elf), "--version", stdout="0.1\n")
    run.on(str(elf), "app-server", stdout=codex_model_list("gpt-6.1-sol", "gpt-6.1-terra"))
    io = ScriptedWizardIO(responder=fresh_responder(("LLM の構成", "2"), ("codex の実体", str(elf))))
    assert run_wizard(root, io, deps) == 0, io.text
    settings = settings_of(root)
    assert settings.runner.trade.backend == "claude" and settings.runner.improve.backend == "codex"
    assert settings.runner.codex.bin == str(elf)
    assert record_of(root)["llm_improve"]["result"] == "ok"


def test_a_remote_llama_swap_url_is_listed_and_selected(tmp_path):
    root = make_root(tmp_path)
    deps, calls, fetch, run = make_deps(tmp_path)
    fetch.models = {OTHER: ["alpha", "beta"]}
    io = ScriptedWizardIO(responder=fresh_responder(
        ("LLM の構成", "3"), ("llama-swap の URL", OTHER), ("取引判断のモデルの番号", "2"), ("改善のモデルの番号", "1")))
    assert run_wizard(root, io, deps) == 0, io.text
    settings = settings_of(root)
    assert settings.llama_swap.base_url == OTHER
    assert settings.runner.trade.model == "beta" and settings.runner.improve.model == "alpha"


@pytest.mark.parametrize("entry,expected,reason", [
    (None, "接続できません (connect_error)", "connect_error"),
    (401, "auth_unsupported", "auth_unsupported"),
    (403, "auth_unsupported", "auth_unsupported"),
    ([], "model_not_found", "model_not_found")])
def test_remote_llama_swap_failures_are_told_apart(tmp_path, entry, expected, reason):
    root = make_root(tmp_path)
    deps, calls, fetch, run = make_deps(tmp_path)
    fetch.models = {} if entry is None else {OTHER: entry}
    io = ScriptedWizardIO(responder=fresh_responder(
        ("LLM の構成", ("3", "4")), ("llama-swap の URL", OTHER), ("[r] 構成の選び直し", "r")))
    assert run_wizard(root, io, deps) == 0, io.text
    assert expected in io.text


def test_a_remote_url_with_credentials_is_refused(tmp_path):
    root = make_root(tmp_path)
    deps, calls, fetch, run = make_deps(tmp_path)
    fetch.models = {}
    io = ScriptedWizardIO(responder=fresh_responder(
        ("LLM の構成", ("3", "4")), ("llama-swap の URL", "http://u:SECRET-MARK-9@h:8080/v1")))
    assert run_wizard(root, io, deps) == 0, io.text
    assert "認証情報を入れないでください" in io.text and "SECRET-MARK-9" not in io.text
    assert all("SECRET-MARK-9" not in url for url, _ in fetch.calls)


def test_unlisted_remote_models_can_be_typed_and_stay_unverified(tmp_path):
    root = make_root(tmp_path)
    deps, calls, fetch, run = make_deps(tmp_path)
    fetch.models = {}
    io = ScriptedWizardIO(responder=fresh_responder(
        ("LLM の構成", "3"), ("llama-swap の URL", OTHER), ("[r] 構成の選び直し", "m"),
        ("取引判断のモデル名", "typed-model"), ("改善のモデル名", "typed-model")))
    assert run_wizard(root, io, deps) == 0, io.text
    assert settings_of(root).runner.trade.model == "typed-model"
    assert record_of(root)["llm_trade"]["result"] == "unverified"


def opencode_listing(home, run, *names):
    run.on(str(home / ".opencode" / "bin" / "opencode"), "models",
           stdout="".join(f"{name}\n" for name in names or ("llama-swap/qwen3-coder-30b",)))


def test_opencode_needs_a_context_limit_and_is_never_marked_verified(tmp_path, home):
    root = make_root(tmp_path)
    deps, calls, fetch, run = make_deps(tmp_path)
    opencode_listing(home, run)
    io = ScriptedWizardIO(responder=fresh_responder(
        ("改善の backend", "2"), ("改善のモデル (番号", "1"), ("llama-swap に問い合わせて取得", "n"),
        ("context_limit", ("0", "abc", "16384"))))
    assert run_wizard(root, io, deps) == 0, io.text
    settings = settings_of(root)
    assert settings.runner.improve.backend == "opencode" and settings.runner.opencode.context_limit == 16384
    assert settings.improve.llama_swap_verified is False
    assert fetch.urls("/props") == []
    assert "verify-backend --backend opencode" in io.text


def test_the_context_limit_is_read_from_llama_swap_only_when_asked(tmp_path, home):
    root = make_root(tmp_path)
    deps, calls, fetch, run = make_deps(tmp_path)
    opencode_listing(home, run)
    fetch.props["n_ctx"] = 12288
    io = ScriptedWizardIO(responder=fresh_responder(
        ("改善の backend", "2"), ("改善のモデル (番号", "1"), ("llama-swap に問い合わせて取得", "y")))
    assert run_wizard(root, io, deps) == 0, io.text
    assert settings_of(root).runner.opencode.context_limit == 12288
    assert len(fetch.urls("/props")) == 1


@pytest.mark.parametrize("returncode,expected", [(0, "ok"), (1, "failed")])
def test_the_improve_exchange_runs_as_a_child_command_after_the_commit(tmp_path, home, returncode, expected):
    root = make_root(tmp_path)
    binary, kwargs = installed_claude(tmp_path, home)
    deps, calls, fetch, run = make_deps(tmp_path, **kwargs)
    fetch.models = {}
    run.on("node", "--version", stdout="v22\n").on(str(binary), "--version", stdout="1.0\n")
    run.on(__import__("sys").executable, "main.py", "improve", "verify-backend", "--backend", "claude", returncode=returncode,
           stdout="anything: ok\n")
    io = ScriptedWizardIO(responder=fresh_responder(("LLM の構成", "1"), ("改善 backend の実往復", "y")))
    assert run_wizard(root, io, deps) == 0, io.text
    child = run.matching(__import__("sys").executable, "main.py")
    assert len(child) == 1 and child[0][-2:] == ["--backend", "claude"]
    assert run.kwargs[run.calls.index(child[0])]["cwd"] == str(root.resolve())
    assert record_of(root)["llm_improve"]["result"] == expected


def test_declining_the_improve_exchange_starts_no_child(tmp_path, home):
    root = make_root(tmp_path)
    binary, kwargs = installed_claude(tmp_path, home)
    deps, calls, fetch, run = make_deps(tmp_path, **kwargs)
    fetch.models = {}
    run.on("node", "--version", stdout="v22\n").on(str(binary), "--version", stdout="1.0\n")
    io = ScriptedWizardIO(responder=fresh_responder(("LLM の構成", "1")))
    assert run_wizard(root, io, deps) == 0, io.text
    assert run.matching(__import__("sys").executable) == []


def test_exported_secret_like_variable_names_are_warned_without_their_values(tmp_path, home):
    root = make_root(tmp_path)
    binary, kwargs = installed_claude(tmp_path, home)
    deps, calls, fetch, run = make_deps(tmp_path, environ={"FOO_API_KEY": "SECRET-MARK-1"}, **kwargs)
    fetch.models = {}
    run.on("node", "--version", stdout="v22\n").on(str(binary), "--version", stdout="1.0\n")
    io = ScriptedWizardIO(responder=fresh_responder(("LLM の構成", "1")))
    assert run_wizard(root, io, deps) == 0, io.text
    assert "FOO_API_KEY" in io.text and "SECRET-MARK-1" not in io.text
