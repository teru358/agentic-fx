from __future__ import annotations

import re
import subprocess
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parents[2]
DOC = REPOSITORY / "docs" / "operations" / "first-run-acceptance.md"
OPS_API_DOC = REPOSITORY / "docs" / "operations" / "ops-api-2026-10-09.md"
README = REPOSITORY / "README.md"
WIZARD = REPOSITORY / "src" / "agentic_fx" / "setup" / "wizard.py"
INDICATORS = REPOSITORY / "src" / "agentic_fx" / "setup" / "indicators.py"


def _doc() -> str:
    return DOC.read_text(encoding="utf-8")


def _ops_api_doc() -> str:
    return OPS_API_DOC.read_text(encoding="utf-8")


def _principle_section() -> str:
    text = _ops_api_doc()
    return text.split("## CLI の置き場の原則", 1)[1].split("## 1. 前提と有効化の条件", 1)[0]


def test_stage_headings_are_quoted_verbatim_from_the_wizard() -> None:
    source = WIZARD.read_text(encoding="utf-8")
    headings = re.findall(r'sess\.say\("(\[段 \d+[^"]*)"\)', source)
    assert len(headings) >= 15, headings
    text = _doc()
    missing = [heading for heading in headings if heading not in text]
    assert not missing, f"手順書がウィザードの見出しをそのまま引用していない: {missing}"


def _wizard_literals() -> tuple[list[str], list[str]]:
    source = WIZARD.read_text(encoding="utf-8")
    options = [line.strip() for line in re.findall(r'sess\.say\("(  (?:[0-9]+|local)\. [^"]*)"\)', source)]
    labels: list[str] = []
    for is_format, literal in re.findall(r'sess\.(?:choice|text|confirm)\(\s*(f?)"((?:[^"\\\n]|\\.)*)"', source):
        chunks = re.split(r"\{[^}]*\}", literal) if is_format else [literal]
        labels += [chunk.strip() for chunk in chunks
                   if len(chunk.strip()) >= 4 and re.search(r"[0-9A-Za-z\u3040-\u30ff\u4e00-\u9fff]", chunk)]
    return options, labels


def test_option_lines_and_question_labels_are_quoted_from_the_wizard() -> None:
    options, labels = _wizard_literals()
    assert len(options) >= 12, options
    assert len(labels) >= 20, labels
    text = _doc()
    missing = [item for item in options + labels if item not in text]
    assert not missing, f"手順書がウィザードの選択肢・問いの文面を引用していない: {missing}"


# 手順書に載せない文。ここに足すときは理由を添えること。
_NOT_DOCUMENTED: frozenset[str] = frozenset()

_SAY_CALL = re.compile(r'sess\.say\(\s*((?:f?"(?:[^"\\\n]|\\.)*"\s*)+)\)')
_STRING_PART = re.compile(r'(f?)"((?:[^"\\\n]|\\.)*)"')


def _plain_say_messages() -> list[str]:
    source = WIZARD.read_text(encoding="utf-8")
    messages: list[str] = []
    for call in _SAY_CALL.findall(source):
        parts = _STRING_PART.findall(call)
        if any(prefix for prefix, _ in parts):
            continue
        text = "".join(chunk for _, chunk in parts)
        if len(text) >= 8 and text not in _NOT_DOCUMENTED:
            messages.append(text)
    return messages


def test_every_plain_say_message_is_quoted_from_the_wizard() -> None:
    messages = _plain_say_messages()
    assert len(messages) >= 80, len(messages)
    text = _doc()
    missing = [message for message in messages if message not in text]
    assert not missing, f"手順書がウィザードの案内文を引用していない: {missing}"


def test_unit_placement_branches_are_documented_with_their_recorded_reasons() -> None:
    text = _doc()
    for needle in ("systemd_unavailable", "user_session_missing", "systemctl --user disable --now afx",
                   "MT5_BRIDGE_API_KEY がありません。段 8 で確認します"):
        assert needle in text, needle


def test_indicator_messages_and_questions_are_quoted_from_the_implementation() -> None:
    source = INDICATORS.read_text(encoding="utf-8")
    literals = re.findall(r'io\.(?:stdout|ask)\("((?:[^"\\\\\n]|\\\\.)*)"', source)
    assert literals, "指標選択の利用者向け文面を抽出できない"
    text = _doc()
    missing = [literal for literal in literals if literal not in text]
    assert not missing, f"手順書が標準指標の文面を引用していない: {missing}"


def test_deprecated_manual_start_command_is_absent_from_user_documents_and_source() -> None:
    result = subprocess.run(["grep", "-rn", "uv run main.py", "src", "docs/operations", "README.md"],
                            cwd=REPOSITORY, capture_output=True, text=True, check=False)
    assert result.returncode == 1, result.stdout


def test_cli_install_section_name_matches_the_pointer_the_wizard_prints() -> None:
    from agentic_fx.setup import pending_text, wizard

    assert wizard.CLI_DOC_SECTION is pending_text.CLI_DOC_SECTION
    pointer = pending_text.CLI_DOC_SECTION
    path = re.match(r"(\S+) の『(.+)』", pointer)
    assert path, pointer
    assert path.group(1) == "docs/operations/first-run-acceptance.md"
    assert re.search(rf"^#+ .*{re.escape(path.group(2))}\s*$", _doc(), flags=re.MULTILINE)


def test_every_stage_has_its_own_section() -> None:
    text = _doc()
    for number in range(0, 15):
        assert re.search(rf"^### 段 {number}[: ]", text, flags=re.MULTILINE), number


def test_fixed_vocabulary_and_scenarios_are_documented() -> None:
    text = _doc()
    for word in ("bin_not_found", "credentials_missing", "auth_unsupported", "model_list_unavailable",
                 "unverified", "deferred_by_user", "user_declined_probe"):
        assert word in text, word
    for heading in ("### 5.1 主シナリオ", "### 5.2 追加 2", "### 5.3 追加 3", "### 5.4 追加 4", "### 5.5 追加 5"):
        assert heading in text, heading


def test_observation_commands_and_journal_contract_are_present() -> None:
    text = _doc()
    for command in ("systemctl --user cat afx.service",
                    "-p Type -p NotifyAccess -p KillMode -p DelegateControllers -p MainPID -p TimeoutStopUSec "
                    "-p SendSIGKILL -p FinalKillSignal -p TimeoutStopFailureMode -p RestartPreventExitStatus "
                    "-p Restart -p RestartUSec",
                    "loginctl show-user", "systemctl --user show afx.service -p InvocationID --value",
                    "journalctl --user -u afx.service -n 0 --show-cursor", "_SYSTEMD_INVOCATION_ID",
                    "USER_INVOCATION_ID", "--after-cursor", "-p ExecMainStatus -p NRestarts -p Result",
                    "-p Wants -p Requisite -p PartOf -p After", "afx ctl status", "afx ops web status",
                    "systemctl --user restart afx", "scripts/first-run-reset.sh", "git status --porcelain --ignored"):
        assert command in text, command


def test_prerequisite_commands_and_test_environment_notes_are_present() -> None:
    text = _doc()
    for needle in ("node --version", "claude --version", 'claude -p "ok だけ答えて" --max-turns 1',
                   'readlink -f "$(command -v codex)"', "cgroup2fs", "systemctl --user is-system-running",
                   "ServerAliveInterval", "Restart=always", "DRY_RUN",
                   "--yes --stop-running --include-linger", "owned_by_wizard", "already_enabled",
                   "journalctl --user -u afx -n 100", "秘密と URL は貼らない"):
        assert needle in text, needle
    assert "ELF" in text


def test_document_carries_no_process_labels() -> None:
    text = _doc()
    for label in ("周目", "レビュー", "是正"):
        assert label not in text, label
    assert not re.search(r"\bT\d{1,2}[ab]?\b", text)


def test_cli_placement_principle_keeps_the_seven_groups_in_surface_order() -> None:
    section = _principle_section()
    expected = [
        "導入・接続管理", "稼働観測・実行追跡", "市場入力を整える", "調査・評価する",
        "問い合わせ・改善運用", "Plugin を採否・配備する", "保護状態を回復する",
    ]
    rows = re.findall(r"^\| (.+?) \|", section, flags=re.MULTILINE)[1:8]
    assert rows == expected


def test_readme_links_to_the_canonical_cli_placement_principle() -> None:
    assert "docs/operations/ops-api-2026-10-09.md#cli-placement-principle" in README.read_text(encoding="utf-8")


def test_canonical_cli_names_are_listed_in_their_one_home_group() -> None:
    section = _principle_section()
    homes = {
        "afx status": "稼働観測・実行追跡",
        "afx ctl status": "稼働観測・実行追跡",
        "afx check backend": "導入・接続管理",
        "afx ctl backlog add": "問い合わせ・改善運用",
        "afx ctl backlog list": "問い合わせ・改善運用",
        "afx ctl policy show": "問い合わせ・改善運用",
        "afx ctl policy remove": "問い合わせ・改善運用",
        "afx ctl policy reconcile": "問い合わせ・改善運用",
        "afx plugin list": "Plugin を採否・配備する",
    }
    rows = [line for line in section.splitlines() if re.match(r"^\| [^|]+ \|", line)][1:]
    assert len(rows) == 7, rows
    for command, group in homes.items():
        holders = [row.split("|")[1].strip() for row in rows if command in row]
        assert holders == [group], (command, holders)


# 旧形式・旧名を説明する追補行。ここに無い行は旧コマンドを含んではならない。
_LEGACY_NOTES: frozenset[tuple[str, str]] = frozenset({
    ("docs/operations/ops-api-2026-10-09.md",
     "旧形式 (`\"version\": 1`、operator と approver の 2 本だけ) はそのまま読める。読むだけでは書き換えず、"
     "最初に成功した鍵変更 (enroll / rotate / revoke) で version 2 に書き換わる。手で書き換えない。"),
    ("docs/operations/ops-api-2026-10-09.md",
     "方針文は DB の managed 行と、`policy/directives.md` の marker 外にある unmanaged 行に分かれる。"
     "旧形式のファイルは最初の投影照合で 1 回だけ移行され、DB に対応する行は ID marker 付き managed block へ再構成される。"
     "手書き行と legacy 行は unmanaged のまま保持される。"),
})


def test_current_user_documents_do_not_present_deprecated_cli_names_as_commands() -> None:
    paths = [README, *sorted((REPOSITORY / "docs" / "operations").glob("*.md"))]
    legacy_seen: list[tuple[str, str]] = []
    offenders: list[tuple[str, str]] = []
    for path in paths:
        relative = path.relative_to(REPOSITORY).as_posix()
        for line in path.read_text(encoding="utf-8").splitlines():
            if "旧名" in line or "旧形" in line:
                legacy_seen.append((relative, line))
            if (relative, line) in _LEGACY_NOTES:
                continue
            if ("afx improve verify-backend" in line or "afx ctl improve add" in line
                    or re.search(r"afx ops (enable|disable|status) (bot|web|notify|<feature>)", line)):
                offenders.append((relative, line))
    assert not offenders, offenders
    assert len(_LEGACY_NOTES) == 2
    assert sorted(legacy_seen) == sorted(_LEGACY_NOTES), legacy_seen


def test_cli_placement_principle_records_labels_for_both_lanes() -> None:
    section = _principle_section()
    for label in ("[読取]", "[変更]", "[確認]", "[approver]"):
        assert label in section
    assert "`afx`（daemon 不要）" in section
    assert "`afx ctl`（稼働 daemon）" in section
    rows = [line for line in section.splitlines() if re.match(r"^\| [^|]+ \|", line)][1:]
    assert len(rows) == 7, rows
    labelled = re.compile(r"^`\[(?:読取|変更|確認|approver)\](?: \[(?:読取|変更|確認|approver)\])*` `afx[ `]")
    commands = 0
    for row in rows:
        cells = row.strip().strip("|").split("|")[1:]
        assert len(cells) == 2, row
        for cell in cells:
            for segment in cell.strip().split(" / "):
                if segment == "—":
                    continue
                assert labelled.match(segment), segment
                assert segment.startswith(("`[読取]", "`[変更]")), segment
                commands += segment.count("`afx")
    assert commands >= 20, commands


def test_new_ops_feature_then_verb_form_is_in_operator_guides() -> None:
    for path in (DOC, OPS_API_DOC):
        text = path.read_text(encoding="utf-8")
        assert "afx ops <feature> enable|disable|status" in text


def test_ops_api_document_pins_backlog_usage_and_managed_only_policy_removal() -> None:
    text = _ops_api_doc()
    assert "afx ctl backlog list [--all] [--limit N]" in text
    assert "削除できるのは ID marker 付きの managed 行だけで、marker 外の手書き行と legacy 行は削除できない" in text
