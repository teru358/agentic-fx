from agentic_fx.setup.pending_steps import PendingFacts, pending_steps


def test_news_steps_are_sorted_folded_and_trailing():
    facts = PendingFacts(record_state="ok", failing_news_sources=("z", "a", "b", "c", "d", "e", "Bad"))
    steps = pending_steps(facts)
    assert [step.params["name"] for step in steps if step.code == "news_source_failing"] == ["a", "b", "c", "d", "e"]
    assert [(step.code, step.params) for step in steps[-1:]] == [("news_source_failing_more", {"count": 2})]


def test_news_steps_do_not_displace_full_existing_pending_list():
    facts = PendingFacts(record_state="ok", failing_news_sources=("a",), extra_steps=[])
    facts.extra_steps.extend([])
    assert any(step.code == "news_source_failing" for step in pending_steps(facts))


def test_missing_setup_record_has_no_news_step():
    steps = pending_steps(PendingFacts(record_state="missing", failing_news_sources=("a",)))
    assert [step.code for step in steps] == ["setup_record_missing"]
