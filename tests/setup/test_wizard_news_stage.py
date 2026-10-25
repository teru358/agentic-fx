from datetime import datetime, timezone

from agentic_fx.datafeed.news_probe import ProbeResult
from agentic_fx.setup import wizard
from tests.setup.wizard_support import ScriptedWizardIO, make_deps, make_root


def test_news_stage_keeps_valid_entry_for_commit_and_reports_probe_count(tmp_path, monkeypatch):
    root = make_root(tmp_path)
    deps, *_ = make_deps(tmp_path)
    session = wizard._Session(root, ScriptedWizardIO(["nhk-keizai https://example.invalid/feed", ""]), deps)
    start = wizard._stage0(session)
    answers = wizard.Answers("UTC", "JPY", ["USDJPY"], 1_000_000)
    calls = []

    def probe(fetcher, url, name, *, timeout_sec):
        calls.append((fetcher, url, name, timeout_sec))
        return ProbeResult(2, 1)

    with monkeypatch.context() as m:
        m.setattr(wizard.news_probe, "probe", probe)
        wizard._stage3_news_sources(session, start, answers)

    assert answers.news_sources == [("nhk-keizai", "https://example.invalid/feed")]
    assert calls == [("feed", "https://example.invalid/feed", "nhk-keizai", 30.0)]
    assert "取得: 2 件 (うち本文・要約あり 1 件)" in session.io.text
