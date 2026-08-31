import pytest

from agentic_fx.runners.response_parser import ParseError, parse_json_output
from tests.runners.m30_fixture import M30_TEXT


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


def test_think_tag_with_newline():
    """Mutation killer: DOTALL flag is needed for newlines inside think tag"""
    text = "<think>考える\n前提をチェック\n結論</think>\n{\"action\": \"sell\"}"
    assert parse_json_output(text)["action"] == "sell"


def test_string_with_closing_brace():
    """Mutation killer: in_str tracking needed to avoid counting braces in strings"""
    text = '結果: {"key": "}", "action": "buy"}'
    result = parse_json_output(text)
    assert result["key"] == "}"
    assert result["action"] == "buy"


def test_fence_mutation_killer():
    """Mutation killer: fence processing changes result (removes decoy before fence)"""
    # Without fence processing would adopt {"wrong": 1} instead of fenced answer
    text = 'ignore this: {"wrong": 1} ```json\n{"action": "buy"}\n```'
    result = parse_json_output(text)
    assert result["action"] == "buy"
    assert "wrong" not in result


def test_unclosed_think_with_decoy_raises():
    """Fix 1: Unclosed think tag with JSON inside → discard rest, raise error"""
    # This was the critical bug: decoy JSON inside unclosed think was adopted
    text = '<think>他の案 {"decoy": true} を検討\n{"action": "hold"}'
    with pytest.raises(ParseError):
        parse_json_output(text)


def test_json_before_unclosed_think_succeeds():
    """Fix 1: JSON before unclosed think is valid (answer comes first)"""
    text = '{"action": "hold"}<think>切断される思考...'
    assert parse_json_output(text)["action"] == "hold"


def test_top_level_array_raises():
    """Fix 2: Top-level array is valid JSON but not dict → ParseError"""
    with pytest.raises(ParseError):
        parse_json_output('[{"action": "hold"}, {"action": "sell"}]')


def test_top_level_string_raises():
    """Fix 2: Top-level string is valid JSON but not dict → ParseError"""
    with pytest.raises(ParseError):
        parse_json_output('"just a string"')


IMPROVE_KEYS = frozenset({"discoveries", "selected", "artifact"})


def test_prefer_keys_selects_final_object_from_real_m30_text():
    result = parse_json_output(M30_TEXT, prefer_keys=IMPROVE_KEYS)
    assert "discoveries" in result


def test_real_m30_text_preserves_first_object_default():
    assert parse_json_output(M30_TEXT) == {"candidates": []}


def test_prefer_keys_without_match_returns_first_parseable_dict():
    text = 'prefix {broken} then {"first": 1} then {"second": 2}'
    assert parse_json_output(text, prefer_keys=frozenset({"missing"})) == {"first": 1}


def test_prefer_keys_ignores_matching_object_after_64_object_cap():
    text = " ".join([f'{{"index": {index}}}' for index in range(64)] +
                    ['{"wanted": true}'])
    assert parse_json_output(text, prefer_keys=frozenset({"wanted"})) == {"index": 0}
