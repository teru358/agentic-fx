import pytest

from agentic_fx.runners.response_parser import ParseError, parse_json_output


def test_plain_json():
    assert parse_json_output('{"action": "hold"}') == {"action": "hold"}


def test_think_tag_stripped():
    text = "<think>色々考えた</think>\n{\"action\": \"hold\"}"
    assert parse_json_output(text)["action"] == "hold"


def test_code_fence_stripped():
    text = "```json\n{\"action\": \"hold\"}\n```"
    assert parse_json_output(text)["action"] == "hold"


def test_json_embedded_in_prose():
    text = "判断は以下の通りです。 {\"action\": \"hold\", \"reasoning\": \"a{b}\"} 以上。"
    assert parse_json_output(text)["reasoning"] == "a{b}"


def test_nested_braces():
    text = 'result: {"a": {"b": 1}, "c": 2}'
    assert parse_json_output(text) == {"a": {"b": 1}, "c": 2}


def test_unparsable_raises():
    with pytest.raises(ParseError):
        parse_json_output("no json here")


def test_broken_json_raises():
    with pytest.raises(ParseError):
        parse_json_output('{"action": "hold"')
