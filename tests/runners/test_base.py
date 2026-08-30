from agentic_fx.runners.base import AgentRunner, Mission, MissionResult
from agentic_fx.runners.fake_runner import FakeRunner


def _mission(prompt="do"):
    return Mission(prompt=prompt, tools=[], output_schema={"type": "object"},
                   max_turns=4, timeout_sec=30)


def test_fake_runner_returns_scripted_results():
    r1 = MissionResult(status="completed", output={"a": 1}, transcript=[])
    r2 = MissionResult(status="failed", output=None, transcript=[])
    fake = FakeRunner([r1, r2])
    assert fake.run(_mission("m1")).output == {"a": 1}
    assert fake.run(_mission("m2")).status == "failed"
    assert fake.run(_mission("m3")).status == "failed"  # 尽きたら最後を繰り返す
    assert [m.prompt for m in fake.missions] == ["m1", "m2", "m3"]


def test_fake_runner_is_agent_runner():
    assert isinstance(FakeRunner([]), AgentRunner)


def test_fake_runner_empty_results_returns_failed():
    """Pin: empty results list should return status='failed', not 'completed'."""
    fake = FakeRunner([])
    result = fake.run(_mission("m1"))
    assert result.status == "failed", f"Expected 'failed', got '{result.status}'"
    assert result.output is None


def test_fake_runner_repeat_last_with_distinctive_value():
    """Pin: repeat-last behavior distinguishable from default 'failed' status."""
    r1 = MissionResult(status="completed", output={"a": 1}, transcript=[])
    # Use max_turns as distinctive value different from default 'failed'
    r2 = MissionResult(status="max_turns", output=None, transcript=[])
    fake = FakeRunner([r1, r2])
    assert fake.run(_mission("m1")).status == "completed"
    assert fake.run(_mission("m2")).status == "max_turns"
    # Third call should repeat the last result (r2 with status="max_turns")
    assert fake.run(_mission("m3")).status == "max_turns"
    # Verify it's still repeating the last result
    assert fake.run(_mission("m4")).status == "max_turns"


def test_fake_runner_missions_exact_object_recording():
    """Pin: fake.missions records exact Mission objects, not just prompts."""
    m1 = _mission("m1")
    m2 = _mission("m2")
    m3 = _mission("m3")
    r1 = MissionResult(status="completed", output={"a": 1}, transcript=[])
    fake = FakeRunner([r1])
    fake.run(m1)
    fake.run(m2)
    fake.run(m3)
    # Check exact object references
    assert fake.missions[0] is m1
    assert fake.missions[1] is m2
    assert fake.missions[2] is m3
    assert len(fake.missions) == 3


def test_agent_runner_cannot_be_instantiated():
    """Pin: AgentRunner is abstract and cannot be instantiated directly."""
    try:
        AgentRunner()
        assert False, "AgentRunner() should raise TypeError"
    except TypeError as e:
        assert "abstract" in str(e).lower()


def test_mission_result_reason_defaults_to_none_via_keywords():
    """Task 1: reason は既定 None (キーワード構築)。"""
    r = MissionResult(status="completed", output={"a": 1}, transcript=[])
    assert r.reason is None


def test_mission_result_reason_defaults_to_none_via_existing_positional_call():
    """Task 1: 既存の 3 positional 引数の呼び出しパターン (プラン 8 以前
    からの全呼び出し site — 本文中で確認済み) が無変更のまま動作し、
    reason は既定 None になる。この形が壊れると reason を必須化する
    変異を見逃す。"""
    r = MissionResult("failed", None, [])
    assert r.reason is None


def test_mission_result_reason_can_be_set_via_keyword():
    """Task 1: reason はキーワード引数で明示設定できる。"""
    r = MissionResult(status="failed", output=None, transcript=[],
                      reason="context exceeded: prompt 1 tokens > n_ctx 2")
    assert r.reason == "context exceeded: prompt 1 tokens > n_ctx 2"


def test_mission_result_recovered_defaults_to_false_via_positional_call():
    """M4: recovered は既定 False。既存の 3 positional 引数の呼び出し
    (プラン10 以前からの全呼び出し site) が無変更のまま動作すること。"""
    r = MissionResult("completed", {"a": 1}, [])
    assert r.recovered is False


def test_mission_result_recovered_can_be_set_via_keyword():
    """M4: recovered は追撃回収成功経路でのみ True になる — キーワードで
    明示設定できる。"""
    r = MissionResult(status="completed", output={"a": 1}, transcript=[],
                      recovered=True)
    assert r.recovered is True


def test_agent_runner_docstring_states_reason_contract():
    """Task 1: AgentRunner の docstring に reason の規範が書かれている
    (設計: 2026-08-10-context-overflow-diagnosis-design.md §4.3)。"""
    doc = AgentRunner.__doc__ or ""
    assert "reason" in doc
    # ⚠️ pin は**動詞まで**含める。`"外部応答の本文を生で"` だけだと
    # `入れない` → `入れてよい` の**意味反転を素通り**させる (ローカル LLM
    # レビュー 3 本が一致して指摘・指揮者が実測確認)。
    assert "外部応答の本文を生で入れない" in doc
    # 「現在の適用範囲」段落 (spec §4.3 が本 task に含めよと明示した内容) は
    # **丸ごと削除しても上の 2 assert が通ってしまう**ため個別に pin する
    # (sonnet レビュー I1・実測確認済み)。特に最後の一文は**プラン 10 実装者
    # への申し送り**であり、黙って消えると誰も気づけない。
    assert "現在の適用範囲" in doc
    assert "ブロッキングチェックリスト" in doc
