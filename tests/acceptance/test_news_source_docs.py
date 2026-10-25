"""News-source wording in the user documents and the design documents."""
from __future__ import annotations

import re
from pathlib import Path

from agentic_fx.datafeed import news_probe, news_url

REPOSITORY = Path(__file__).resolve().parents[2]
README = (REPOSITORY / "README.md").read_text(encoding="utf-8")
ACCEPTANCE = (REPOSITORY / "docs" / "operations" / "first-run-acceptance.md").read_text(encoding="utf-8")
BASE_DESIGN = (REPOSITORY / "docs" / "superpowers" / "specs" / "2026-07-25-agentic-fx-design.md")

CANDIDATES = (
    "| `nhk-keizai` | `https://www3.nhk.or.jp/rss/news/cat5.xml` | `feed` |",
    "| `fxstreet-news` | `https://www.fxstreet.com/rss/news` | `feed` |")
DISCLAIMER = "これは例であって推奨でも保証でもなく、各源の利用規約の確認は運用者の責任です。"


def test_both_user_documents_list_the_two_candidates_and_never_the_third():
    for text in (README, ACCEPTANCE):
        for row in CANDIDATES:
            assert row in text
        assert "https://finance.yahoo.com" not in text
    assert DISCLAIMER in README
    assert "例であって、推奨でも保証でもありません" in ACCEPTANCE


def test_the_readme_states_the_operator_responsibilities_and_the_key_handling():
    for needle in ("afx news list", "afx news add <name> <url>", "afx news enable <name>",
                   "afx news disable <name>", "afx news test <name>",
                   "運用者の判断", "利用規約の確認は運用者の責任", "連絡先であり、責任の引き受けを意味しません",
                   "afx news add <name> -", "平文で残り", "shell 履歴", "`web` 方式で RAG に出る"):
        assert needle in README, needle


def test_the_acceptance_document_quotes_every_fixed_word_the_news_step_can_show():
    for bad_input in ("Bad_Name", "ftp://x"):
        try:
            news_url.validate_name(bad_input) if bad_input == "Bad_Name" else news_url.validate_url(bad_input)
        except news_url.NewsInputError as error:
            assert str(error) in ACCEPTANCE, str(error)
    for reason in (news_probe.REASON_TIMEOUT, news_probe.REASON_CONNECT, news_probe.REASON_OTHER, "HTTP <code>"):
        assert f"`{reason}`" in ACCEPTANCE, reason
    for needle in ("`afx init --non-interactive` は入力式の段が無いのでニュース源を登録しません",
                   "afx news disable yahoo-finance-topstories",
                   "入力済みのニュース源: <名前>", "既に登録されています: <名前>",
                   "既に登録されていたので飛ばしました: <名前>"):
        assert needle in ACCEPTANCE, needle


_RETIRED = (
    re.compile("基本ニュースソース"),
    re.compile("kind=news_source"),
    re.compile(r"POST /news"),
    re.compile(r"`news add"),
    re.compile(r"news ソース \(agent 追加\)"),
    re.compile(r"agent 追加"))
_MARKERS = ("廃止", "取り下げ", "~~")


def test_the_base_design_keeps_no_retired_news_path_as_a_current_description():
    """A line naming a retired path must carry a retirement marker itself or follow a line that does."""
    lines = BASE_DESIGN.read_text(encoding="utf-8").splitlines()
    bad = []
    for number, line in enumerate(lines):
        if not any(pattern.search(line) for pattern in _RETIRED):
            continue
        previous = next((lines[i] for i in range(number - 1, -1, -1) if lines[i].strip()), "")
        if not any(marker in line or marker in previous for marker in _MARKERS):
            bad.append((number + 1, line[:80]))
    assert bad == []
