"""組み込み news fetcher: feed / web の 2 つのみ (設計書 §6)。

feed / web の追加は news_sources テーブルへのデータ追加で行う (plugin では
ない)。fetcher はここでは **1 ソース単位の取得** のみを担う。複数ソースを
束ねて 1 つ失敗しても他は返す (fail-open) 役割は将来の集約層
(news_provider 相当) の責務であり、本モジュールは意図的に持たない。
fetch_web が抽出失敗時に返す `[]` は「1 ページの本文抽出に失敗した」という
parse 結果であって、ソース全体の成否 (ネットワーク層の例外) とは別の話 —
ネットワーク層の失敗はここでは素通しで例外を送出する (sources.py と同じ方針)。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

import feedparser
import httpx
import trafilatura

_log = logging.getLogger("agentic_fx.news")


@dataclass(frozen=True, slots=True)
class Article:
    url: str
    title: str
    body: str
    published: datetime | None
    source_name: str


def _feed_published(entry: object) -> datetime | None:
    """entry の日付を tz-aware UTC datetime にする (naive は作らない)。

    feedparser は RFC822/W3CDTF 等あらゆる日付形式を解釈し、**GMT に
    正規化した 9-tuple** を `*_parsed` に格納する
    (feedparser/datetimes/rfc822.py の `_parse_date_rfc822`:
    docstring 「a UTC time tuple」、実装は `(stamp - delta).utctimetuple()`
    で常に UTC 側に寄せてから tz を落とした tuple を返す)。つまりここで
    `tzinfo=timezone.utc` を付けるのは「naive を UTC とみなす」自己流の
    仮定ではなく、**feedparser が保証する不変条件を信頼している**だけ —
    sources.py の `_to_utc` が拒否する「vendor API が naive を返す」ケース
    (yfinance 等、実際には非 UTC) とは性質が異なる。

    既知の限界 (是正不可、報告書に明記): feedparser の RFC822 タイムゾーン
    略号表 (rfc822.py `timezone_names`) は米国略号 (EST/PST 等) と
    UT/GMT/Z/MET/MEST のみを収録し、JST 等の非収録略号は未知語として
    黙って offset=0 (UTC 扱い) にフォールバックする
    (`timezone_names.get(parts[4], 0)` — 例外にならない)。実務上フィードの
    大半は数値オフセット (+0900 等、こちらは正しく解釈される) か収録済み
    略号を使うため実害は小さいと見るが、自前で RFC822 パーサを書き直さない
    限り本モジュール側では是正できない依存ライブラリの限界として残す。

    Atom フィードは `<published>` が任意・`<updated>` が必須のため、
    `published_parsed` が無い場合は `updated_parsed` にフォールバックする
    (同じ GMT 正規化コードパスを通るため、上記の理由がそのまま成り立つ)。
    どちらも無ければ日時不明として None を返す (捏造しない)。
    """
    parsed = (getattr(entry, "published_parsed", None)
              or getattr(entry, "updated_parsed", None))
    if not parsed:
        return None
    return datetime(*parsed[:6], tzinfo=timezone.utc)


def fetch_feed(url: str, source_name: str) -> list[Article]:
    """RSS/Atom フィードを取得して Article のリストにする。

    1 エントリの欠損フィールド (title/link 等) でフィード全体が空になる
    ことがないよう、エントリ単位で防御的に読む (title は空文字に、
    link を欠くエントリのみ記事として成立しないためスキップし技術ログに
    残す)。ネットワーク層の失敗は feedparser 自身が bozo フラグに吸収し
    例外化しないため、本関数から通常は例外を送出しない。
    """
    parsed = feedparser.parse(url)
    out: list[Article] = []
    for e in parsed.entries:
        try:
            link = e.link
        except AttributeError:
            _log.warning("%s: entry missing link, skipped", source_name)
            continue
        try:
            title = e.title
        except AttributeError:
            title = ""
        out.append(Article(
            url=link, title=title,
            body=getattr(e, "summary", "") or "",
            published=_feed_published(e), source_name=source_name))
    return out


def fetch_web(url: str, source_name: str) -> list[Article]:
    """単一 web ページから本文を抽出する (1 記事)。抽出失敗は空リスト。

    published は常に None — 単一ページから発行日時を tz-aware UTC で
    機械的に確定する手段がない (trafilatura のメタデータ日付は tz を
    持たない文字列であることが多く、tz 不明のまま UTC を名乗らせるのは
    禁止) ため、捏造せず None を返す設計判断。

    include_comments=False はブリーフの逐語コードから意図的に変更した点:
    trafilatura.extract の既定は True で、そのままだとコメント欄の文章が
    本文に混入し、LLM の取引判断材料として記事本文と同格に扱われてしまう。
    """
    r = httpx.get(url, timeout=30, follow_redirects=True)
    r.raise_for_status()
    body = trafilatura.extract(r.text, include_comments=False)
    if not body:
        return []
    meta = trafilatura.extract_metadata(r.text)
    title = meta.title if meta and meta.title else url
    return [Article(url=url, title=title, body=body, published=None,
                    source_name=source_name)]
