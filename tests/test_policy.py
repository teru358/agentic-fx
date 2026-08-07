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


def test_tail_zero_returns_empty(tmp_path):
    """M1: tail(0) should return empty, not entire file."""
    p = tmp_path / "directives.md"
    p.write_text("A" * 5000, encoding="utf-8")
    assert Policy(p).tail(0) == ""


def test_tail_multibyte_chars(tmp_path):
    """I3: tail should count characters, not bytes."""
    p = tmp_path / "directives.md"
    # Each Japanese character is 3 bytes in UTF-8
    text = "あ" * 5000
    p.write_text(text, encoding="utf-8")
    result = Policy(p).tail(4000)
    # Should be 4000 characters (12000 bytes), not 4000 bytes
    assert len(result) == 4000
    assert result == "あ" * 4000


def test_size_warning(tmp_path):
    p = tmp_path / "directives.md"
    p.write_text("A" * 20000, encoding="utf-8")
    assert Policy(p).size_warning() is not None
    p.write_text("short", encoding="utf-8")
    assert Policy(p).size_warning() is None


def test_size_warning_boundary(tmp_path):
    """I3: Exactly at limit should return None (only > warns)."""
    p = tmp_path / "directives.md"
    p.write_text("A" * 16000, encoding="utf-8")
    assert Policy(p).size_warning() is None
    p.write_text("A" * 16001, encoding="utf-8")
    assert Policy(p).size_warning() is not None


def test_prompts_exist():
    for name in ("trade_mission", "ask_mission", "reflection"):
        text = load_prompt(name)
        assert len(text) > 100


def test_prompts_content_pinned():
    """I2: Pin safety constraints via content assertions."""
    trade = load_prompt("trade_mission")
    assert "数量はあなたは決めない" in trade
    assert "自動的に却下される" in trade
    assert "stop_loss" in trade

    ask = load_prompt("ask_mission")
    assert "取引を実行できません" in ask

    reflection = load_prompt("reflection")
    assert "教訓" in reflection


def test_tail_returns_empty_on_permission_error(tmp_path, monkeypatch):
    """tail/size_warning が FileNotFoundError だけでなく OSError
    (PermissionError 等) も捕捉することを確認。"""
    p = tmp_path / "directives.md"
    p.write_text("x" * 100)
    policy = Policy(p)

    def boom(*a, **k):
        raise PermissionError("denied")

    monkeypatch.setattr(type(p), "read_text", boom)
    assert policy.tail(10) == ""
    assert policy.size_warning() is None
