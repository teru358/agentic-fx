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
