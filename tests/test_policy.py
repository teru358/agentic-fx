from agentic_fx.loops.prompts_loader import load_prompt
from agentic_fx.policy import Policy


def test_tail_missing_file_empty(tmp_path):
    assert Policy(tmp_path / "directives.md").tail() == ""


def test_tail_returns_last_chars(tmp_path):
    p = tmp_path / "directives.md"
    p.write_text("A" * 1000 + "B" * 4000, encoding="utf-8")
    result = Policy(p).tail(4000)
    assert len(result) == 4000
    assert result == "B" * 4000  # Must be the LAST 4000, not first


def test_size_warning(tmp_path):
    p = tmp_path / "directives.md"
    p.write_text("A" * 20000, encoding="utf-8")
    assert Policy(p).size_warning() is not None
    p.write_text("short", encoding="utf-8")
    assert Policy(p).size_warning() is None


def test_prompts_exist():
    for name in ("trade_mission", "ask_mission", "reflection"):
        text = load_prompt(name)
        assert len(text) > 100
