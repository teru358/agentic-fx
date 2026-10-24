from __future__ import annotations

import re
from pathlib import Path

import pytest

from agentic_fx.ops_setup import reconfigure, web_setup, web_wizard
from agentic_fx.ops_setup.feature_config import WebAccess

REPOSITORY = Path(__file__).resolve().parents[2]
ACCEPTANCE = REPOSITORY / "docs" / "operations" / "first-run-acceptance.md"
OPS_API = REPOSITORY / "docs" / "operations" / "ops-api-2026-10-09.md"
RECONFIGURE_SOURCE = REPOSITORY / "src" / "agentic_fx" / "ops_setup" / "reconfigure.py"
DOCS = {"first-run-acceptance": ACCEPTANCE, "ops-api": OPS_API}


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _status_lines() -> dict[str, str]:
    """実装が出す status の公開範囲行を、入力ごとに実際に作らせる。"""
    stage = {"operation": "enable", "stage": "enabled"}
    lines = {}
    cases = {
        "restricted": {"port": 8788, "bind": "192.0.2.10", "allow": ["10.0.0.0/8"], "auth": "basic",
                       "user": "afx"},
        "restricted-none": {"port": 8788, "bind": "192.0.2.10", "allow": ["10.0.0.0/8"], "auth": "none"},
        "unrestricted": {"port": 8788, "bind": "192.0.2.10", "allow": ["0.0.0.0/0"], "auth": "none"},
        "loopback-default": {"port": 8788},
    }
    for kind, values in cases.items():
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(web_setup, "web_access_check", lambda *_a, _v=values, **_k: WebAccess(None, _v))
            lines[kind] = web_setup.access_line(Path("/nonexistent"), stage)
    return lines


def _summary_templates() -> list[str]:
    exposure = web_setup.exposure({"bind": "192.0.2.10", "allow": ["10.0.0.0/8"]})
    assert exposure == "許可範囲 1 件"
    templates = []
    for values in ({"port": 8788, "bind": "192.0.2.10", "allow": ["10.0.0.0/8"], "auth": "none"},
                   {"port": 8788, "bind": "192.0.2.10", "allow": ["10.0.0.0/8"], "auth": "basic",
                    "user": "ops-web"}):
        line = web_setup.summary_line(values)
        templates.append(line.replace("192.0.2.10", "<bind>").replace("8788", "<port>")
                         .replace(exposure, "<範囲>").replace("ops-web", "<user>"))
    return templates


def _exposures() -> list[str]:
    loopback = web_setup.exposure({"bind": "127.0.0.1"})
    unrestricted = web_setup.exposure({"bind": "192.0.2.10", "allow": ["0.0.0.0/0"]})
    limited = web_setup.exposure({"bind": "192.0.2.10", "allow": ["10.0.0.0/8"]}).replace("1", "<N>")
    return [loopback, unrestricted, limited]


def test_the_literals_taken_from_the_implementation_are_the_expected_shapes() -> None:
    assert _summary_templates() == ["web: 待受 <bind>:<port> / 公開 <範囲> / 認証 none",
                                    "web: 待受 <bind>:<port> / 公開 <範囲> / 認証 basic (<user>)"]
    assert _exposures() == ["自機のみ", "到達できる全 IPv4", "許可範囲 <N> 件"]
    assert _status_lines()["restricted"] == "web-access: bind=192.0.2.10:8788 allow=restricted auth=basic user=afx"
    assert _status_lines()["restricted-none"] == "web-access: bind=192.0.2.10:8788 allow=restricted auth=none"
    assert web_setup.access_line(Path("/nonexistent"), None) == "web-access: unavailable reason=not_configured"


@pytest.mark.parametrize("name", sorted(DOCS))
def test_wizard_question_warning_and_summary_are_quoted_verbatim(name: str) -> None:
    text = _text(DOCS[name])
    assert web_wizard.ONE_QUESTION in text
    assert web_wizard.UNRESTRICTED_WARNING in text
    assert all(template in text for template in _summary_templates())
    assert "`yes`" in text
    assert "既定" not in web_wizard.ONE_QUESTION


def _question_texts() -> list[str]:
    return [
        web_wizard.BIND_EXPLANATION,
        web_wizard.choice_text(web_wizard.ACCESS_LABEL, web_wizard.ACCESS_OPTIONS),
        web_wizard.choice_text(web_wizard.KEEP_LABEL, web_wizard.KEEP_OPTIONS),
        web_wizard.USER_LABEL + " [afx]:",
        web_wizard.PASSWORD_LABEL + ":",
        web_wizard.CONFIRM_LABEL + ":",
        web_setup.generated_password_line("<user>", "<password>"),
    ]


def test_the_basic_user_and_password_questions_are_quoted_in_the_ops_api_document() -> None:
    text = _text(OPS_API)
    for question in _question_texts():
        assert question in text, question
    assert "8〜32" in text and "33 文字以上" in text
    assert "web が止まっている間に `web.password` を手で削除" in text


def test_the_acceptance_document_names_the_user_display_and_the_old_password_recovery() -> None:
    text = _text(ACCEPTANCE)
    assert web_wizard.BIND_EXPLANATION in text
    assert web_wizard.choice_text(web_wizard.KEEP_LABEL, web_wizard.KEEP_OPTIONS) in text
    assert "`ユーザー名: <user>`" in text
    assert "web が止まっている間に `web.password` を手で削除" in text


@pytest.mark.parametrize("name", sorted(DOCS))
def test_the_old_password_recovery_guidance_is_quoted_verbatim(name: str) -> None:
    assert web_setup.OLD_PASSWORD_RECOVERY in _text(DOCS[name])
    for verb in ("enable", "reconfigure"):
        assert web_setup.OLD_PASSWORD_RECOVERY in web_setup.SECRET_GUIDANCE_TEMPLATE.format(verb=verb)


def test_exposure_words_are_quoted_in_the_ops_api_document() -> None:
    text = _text(OPS_API)
    assert all(word in text for word in _exposures())


@pytest.mark.parametrize("name", sorted(DOCS))
def test_status_line_format_is_quoted_verbatim(name: str) -> None:
    text = _text(DOCS[name])
    lines = _status_lines()
    unavailable = "web-access: unavailable reason="
    assert unavailable + "<固定 reason>" in text
    assert web_setup.access_line(Path("/nonexistent"), None).startswith(unavailable)
    if name == "ops-api":
        assert lines["restricted"] in text and lines["restricted-none"] in text
        assert all(f"`{kind}`" in text for kind in ("restricted", "unrestricted", "loopback-default"))
    else:
        for key in ("restricted", "restricted-none"):
            template = lines[key].replace("192.0.2.10", "<bind>").replace("8788", "<port>")
            template = template.replace("allow=restricted", "allow=<restricted|unrestricted|loopback-default>")
            template = template.replace("user=afx", "user=<user>")
            assert template in text


def _reconfigure_outputs() -> list[str]:
    source = RECONFIGURE_SOURCE.read_text(encoding="utf-8")
    outputs = sorted(set(re.findall(r'io\.stdout\("(web: reconfigured[^"]*)"\)', source)))
    assert outputs == ["web: reconfigured", "web: reconfigured ops=starts_with_afx"], outputs
    return outputs


@pytest.mark.parametrize("name", sorted(DOCS))
def test_reconfigure_completion_lines_are_quoted_verbatim(name: str) -> None:
    text = _text(DOCS[name])
    assert all(f"`{output}`" in text for output in _reconfigure_outputs())


def test_reconfigure_guidance_and_afx_untouched_statement_are_quoted_in_the_ops_api_document() -> None:
    text = _text(OPS_API)
    for guidance in (reconfigure.RERUN, reconfigure.RECOVER, reconfigure.RELOAD, reconfigure.KEYS):
        assert guidance in text
    assert "止まるのは **ops-web だけ**" in text
    assert "afx (ペーパー運用)・鍵・enable の記録には触れない" in text
    assert "設定の変更は afx ops web reconfigure" in text


def test_reconfigure_does_not_stop_afx_is_stated_in_the_acceptance_document() -> None:
    text = _text(ACCEPTANCE)
    assert "afx は止まらない" in text
    assert "ActiveEnterTimestampMonotonic" in text
    assert "`afx-ops@web` だけが再起動する" in text


def test_operation_documents_have_no_trailing_blank_lines() -> None:
    for path in DOCS.values():
        data = path.read_bytes()
        assert data.endswith(b"\n") and not data.endswith(b"\n\n"), path
