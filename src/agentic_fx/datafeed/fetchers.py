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
import re
from dataclasses import dataclass
from datetime import datetime, timezone

import feedparser
import httpx
import trafilatura

# feedparser が「解釈できる」タイムゾーン略号の一覧。**自前の略号表は持たず**、
# feedparser 内部の実データ (private だが実ソース) をそのまま参照する。
# rfc822.py の辞書は asctime/greek でも re-export され、w3dtf.py の辞書は
# hungarian/korean でも re-export されている (いずれも `from .rfc822 import`
# / `from .w3dtf import` で日付本体パーサのみ共有し、独自の略号表は持たない)
# ため、この 2 つで named-timezone 略号を扱う全ハンドラをカバーする
# (iso8601.py は略号を一切扱わず数値オフセット/Z のみなので対象外)。
# **壊れやすさの明記**: これは feedparser の公開 API ではない。feedparser が
# モジュール構成や変数名を変えれば ImportError になり得るが、独自の略号表を
# 二重管理してライブラリ更新とずれるより安全と判断した (修正ラウンド 1)。
from feedparser.datetimes.rfc822 import timezone_names as _RFC822_TZ_ABBREVS
from feedparser.datetimes.w3dtf import timezonenames as _W3C_TZ_ABBREVS

# "utc" は feedparser の辞書 (上記 2 つとも) に literal キーとして無いが、これは
# 例外的に安全: UTC の定義上のオフセットは 0 で、feedparser が未知語に割り当てる
# フォールバック値も 0。つまり "UTC" を未知語扱いしても結果の数値は元々正しく、
# 報告すべき「無音の間違い」が存在しない。これは自前の略号表の再構築ではなく、
# 「フォールバック先と定義上の値が一致する」という 1 語だけの特例。
_KNOWN_TZ_ABBREVS = (frozenset(_RFC822_TZ_ABBREVS) | frozenset(_W3C_TZ_ABBREVS)
                     | frozenset({"utc"}))

_log = logging.getLogger("agentic_fx.news")


def _unrecognized_tz_abbrev(raw_date: str | None) -> str | None:
    """raw_date の末尾トークンが「feedparser が誤って +0000 に倒す、未知の
    タイムゾーン略号」に見えるなら、そのトークンをそのまま返す。

    feedparser の rfc822/w3dtf パーサはどちらも `dict.get(token, 0)` で
    未知の略号を**無音で offset 0 (UTC 扱い)** にフォールバックする
    (rfc822.py: `timezone_names.get(parts[4], 0)`、w3dtf.py:
    `timezonenames.get(parts[2], 0)`)。JST はどちらの辞書にも無い。

    誤検知よりも見逃しを選ぶ (要求 4): 生の日付文字列が無い、または末尾トークンが
    英字のみで構成されていない場合 (数値オフセット `+0900`/`+09:00`、`Z` 終端の
    ISO8601 文字列全体、日付のみ等はいずれも非英字を含むためここに落ちる) は
    「判定できない」として None を返し、警告は出さない。数値オフセット/Z 終端を
    個別に正規表現で判定する専用分岐は持たない — 「英字のみでないなら未知語
    ではあり得ない (feedparser の辞書キーはすべて英字)」ため、この非英字判定
    だけで数値オフセット/Z サフィックスも自動的に安全側 (警告なし) に落ちる。
    """
    if not isinstance(raw_date, str) or not raw_date.strip():
        return None
    token = raw_date.strip().split()[-1]
    if not re.fullmatch(r"[A-Za-z]+", token):
        return None  # 英字のみでない → 辞書キーではあり得ない、判定不能として警告なし
    if token.lower() in _KNOWN_TZ_ABBREVS:
        return None
    return token


@dataclass(frozen=True, slots=True)
class Article:
    url: str
    title: str
    body: str
    published: datetime | None
    source_name: str


def _feed_published(entry: object, source_name: str,
                    feed_url: str) -> datetime | None:
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

    既知の限界 (修正ラウンド 1 で「無音」を解消): feedparser の RFC822/W3CDTF
    タイムゾーン略号表は米国略号 (EST/PST 等) と UT/GMT/Z/MET/MEST のみを
    収録し、JST 等の非収録略号は未知語として黙って offset=0 (UTC 扱い) に
    フォールバックする。これ自体は feedparser 側の挙動でここでは是正しない
    (略号変換表を自前で持つのは、どの news ソースを使うか未確定な現時点では
    投機的 — Task 5 修正ラウンド 1 の指摘どおり)。代わりに
    `_unrecognized_tz_abbrev` で「無音の間違い」を検知し、技術ログに残す。
    記事は捨てない (ニュースは fail-open — 価格と違い欠損で取引を止める
    種類のデータではない)。

    Atom フィードは `<published>` が任意・`<updated>` が必須のため、
    `published_parsed` が無い場合は `updated_parsed` にフォールバックする
    (同じ GMT 正規化コードパスを通るため、上記の理由がそのまま成り立つ)。
    どちらも無ければ日時不明として None を返す (捏造しない)。
    """
    if getattr(entry, "published_parsed", None):
        parsed = entry.published_parsed
        raw = getattr(entry, "published", None)
    elif getattr(entry, "updated_parsed", None):
        parsed = entry.updated_parsed
        raw = getattr(entry, "updated", None)
    else:
        return None
    abbrev = _unrecognized_tz_abbrev(raw)
    if abbrev is not None:
        _log.warning(
            "feed %s (%s): date %r has unrecognized timezone abbreviation "
            "%r; feedparser silently treats it as +0000 (UTC), which may "
            "be wrong — article is kept but its published time may be off",
            source_name, feed_url, raw, abbrev)
    return datetime(*parsed[:6], tzinfo=timezone.utc)


class FeedFetchError(Exception):
    """feedparser が bozo (取得/パース失敗) を報告し、1 件もエントリを
    得られなかった場合に送出する。"取得失敗" を可視化するための例外。"""


def _bozo_reason(bozo_exception: BaseException | None) -> str:
    """bozo_exception を「ログ・例外メッセージに出してよい」文字列にする。

    feedparser はネットワーク層の失敗 (urllib 由来の接続エラー等) も
    bozo_exception に格納するため、その str() には URL やホスト名が
    乗ることがある (price_provider._safe_error_text と同じ懸念)。
    ここでは型名のみを使う — 診断には十分で、URL を含む本文は一切
    出さない。
    """
    if bozo_exception is None:
        return "unknown parse error"
    return type(bozo_exception).__name__


def fetch_feed(url: str, source_name: str, *, timeout_sec: float) -> list[Article]:
    """RSS/Atom フィードを取得して Article のリストにする。

    1 エントリの欠損フィールド (title/link 等) でフィード全体が空になる
    ことがないよう、エントリ単位で防御的に読む (title は空文字に、
    link を欠くエントリのみ記事として成立しないためスキップし技術ログに
    残す)。

    **修正ラウンド 1 (レビュー指摘)**: ネットワーク層の失敗は feedparser
    自身が例外化せず bozo フラグ (+ bozo_exception) に無音で吸収する。
    これを素通しすると「死んだフィードは 0 件を返し続けるだけで、技術
    ログにも activity にも何も残らない」状態になり、グローバル制約
    「失敗は技術ログに残す」に反する。そこで:
    - bozo かつ entries が空 → 「フィードが空」ではなく「取得/パースに
      失敗した」とみなし `FeedFetchError` を送出する (呼び出し側
      `NewsCollector.collect` の per-source except が拾い、失敗として
      技術ログに残す形になる)。
    - bozo だが entries はある (部分的なパースエラー) → 取れたものは
      返しつつ warning のみ出す (ニュースは fail-open — 価格と違い
      欠損で取引を止める種類のデータではない)。
    - bozo が立っていない (健全なフィードがたまたま 0 件) は従来どおり
      無警告で空リストを返す — これはエラーではない。
    """
    response = httpx.get(url, timeout=timeout_sec, follow_redirects=True)
    response.raise_for_status()
    parsed = feedparser.parse(response.content)
    if getattr(parsed, "bozo", False):
        reason = _bozo_reason(getattr(parsed, "bozo_exception", None))
        if not parsed.entries:
            raise FeedFetchError(f"{source_name}: feed fetch/parse failed "
                                 f"({reason})")
        _log.warning(
            "feed %s: bozo flag set (%s) but %d entr%s recovered; "
            "returning partial results", source_name, reason,
            len(parsed.entries), "y" if len(parsed.entries) == 1 else "ies")
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
            published=_feed_published(e, source_name, url),
            source_name=source_name))
    return out


def fetch_web(url: str, source_name: str, *, timeout_sec: float) -> list[Article]:
    """単一 web ページから本文を抽出する (1 記事)。抽出失敗は空リスト。

    published は常に None — 単一ページから発行日時を tz-aware UTC で
    機械的に確定する手段がない (trafilatura のメタデータ日付は tz を
    持たない文字列であることが多く、tz 不明のまま UTC を名乗らせるのは
    禁止) ため、捏造せず None を返す設計判断。

    include_comments=False はブリーフの逐語コードから意図的に変更した点:
    trafilatura.extract の既定は True で、そのままだとコメント欄の文章が
    本文に混入し、LLM の取引判断材料として記事本文と同格に扱われてしまう。
    """
    r = httpx.get(url, timeout=timeout_sec, follow_redirects=True)
    r.raise_for_status()
    body = trafilatura.extract(r.text, include_comments=False)
    if not body:
        return []
    meta = trafilatura.extract_metadata(r.text)
    title = meta.title if meta and meta.title else url
    return [Article(url=url, title=title, body=body, published=None,
                    source_name=source_name)]
