import pytest
from agentic_fx.datafeed.news_probe import NewsInputError, fold_url, normalize_url, validate_name, validate_url


def test_news_input_validation_and_normalization_are_secret_safe():
    validate_name("source-1")
    with pytest.raises(NewsInputError): validate_name("Bad")
    with pytest.raises(NewsInputError): validate_url("https://example.invalid/feed#")
    assert normalize_url("HTTPS://EXAMPLE.INVALID:443/feed?") == "https://example.invalid/feed?"
    assert fold_url("https://[2001:db8::1]/feed?k=x") == "https://[2001:db8::1]/…"
