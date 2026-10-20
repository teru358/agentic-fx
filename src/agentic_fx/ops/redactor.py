"""表示する全ての値に共通で掛ける 1 つの検出と伏せ字の部品。

値の出どころは全て非信頼 (agent の申告、config.yaml の key と value、plugin コードの
文字列・コメント、およびそれらから作る diff の全行) である。検出対象は絶対パス、URL・
裸の hostname・IP、credential の代入、高エントロピーの token、口座らしい数字列。

順序は 入力読み取り上限 → Cc/Cf/Cs 除去 → NFKC 正規化 → redaction → 表示形式の escape →
表示上限とする。この部品は除去と redaction を受け持ち、escape と表示上限は表示面が後から掛ける。
表示面は Cc・Cf・Cs を消すので、検出をその前に掛けると消えた後に繋がる秘密を取り逃がす。
そのため検出は除去・正規化済みの文字列に掛け、出力もその文字列を基に作る (全角の
``ａｐｉ＿ｋｅｙ`` などは正規化で ASCII になる)。``%XX`` を 1 回だけ decode した複製にも検出を
掛け (固定点まで、上限 3 回)、どの段で検出されても値全体を伏せる (decode 結果は表示に使わない)。
正規化後も ASCII 外の文字が credential の key・URL の scheme の位置に残る値も全体を伏せる。

裸の hostname (``evil.xyz`` のように scheme も host= の文脈もないもの) は、最後の label が
TLD (IANA の一覧と特殊用途の名前) か、どれかの label が ``xn--`` で始まる語を伏せる。検出の範囲は
入力の種類で分ける。自由文 (config・申告・理由など) は全体が対象。Python ソースの行は
``python_text_spans`` が求めた文字列リテラルとコメントの範囲だけが対象で、識別子の連なり
(``logger.info``) は hostname とみなさない。構文として読めないファイルは全行を自由文として扱う。改行とタブは
表示面が行の区切り・字下げとして扱うので残す (CR は表示面で改行に正規化されるが、
単独の CR で語を割る回避を防ぐため除去する)。

fail-closed: 1 値の大きさが redaction 対象上限を超えるときは、切らずに全文を伏せる
(切ると token が境界で割れて検出を逃すため)。判定できないファイル (非 UTF-8、NUL を
含む、鍵本体の block を含む) はファイル単位で全文を伏せる。検出処理が例外を出した値も
伏せる側に倒す。
"""
from __future__ import annotations

import ast
import io
import math
import re
import tokenize
import unicodedata
import urllib.parse
from collections import Counter

from .change_limits import (ACCOUNT_MIN_DIGITS, HOST_TLDS, MAX_REDACT_VALUE_BYTES, PLUGIN_FILES,
                            REDACTED, TOKEN_MIN_ENTROPY, TOKEN_MIN_ENTROPY_NO_DIGIT,
                            TOKEN_MIN_LENGTH)

_URL = re.compile(r"(?i)\b[a-z][a-z0-9+.-]*://\S+|\bwww\.[^\s'\"<>]+")
_BEARER = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}")
_CRED_NAME = (r"(?:api[_-]?key|apikey|access[_-]?key|secret[_-]?key|private[_-]?key|"
              r"auth[_-]?token|token|password|passwd|pwd|secret|credentials?|key)s?")
_CRED = re.compile(
    r"(?i)(?<![A-Za-z0-9])((?:[A-Za-z0-9]+[_.-])*" + _CRED_NAME + r"[\"']?\s*[:=]\s*)"
    r"(\"[^\"\n]*\"|'[^'\n]*'|[^\s,;)}\]]+)")
_CRED_KEY = re.compile(r"(?i)(?:^|[_.-])" + _CRED_NAME + r"$")
_HOST_LABELS = r"(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)"
_ANY_TLD = (r"(?:com|net|org|io|jp|co|dev|app|cloud|info|biz|edu|gov|mil|int|xyz|online|site|"
            r"tech|ai|me|us|uk|de|fr|cn|ru|kr|in|au|ca|eu|local|localdomain|lan|internal|home|"
            r"corp|intranet|test|example|invalid)")
_HOST_CONTEXT = re.compile(
    r"(?i)(@|\b(?:host(?:name)?|server|addr(?:ess)?|domain|endpoint)\s*[:=]\s*[\"']?)"
    + _HOST_LABELS + r"+" + _ANY_TLD + r"(?![\w-])")
_LOCALHOST = re.compile(r"(?i)(?<![\w.-])localhost(?![\w-])")
_IPV4 = re.compile(r"(?<![\w.])\d{1,3}(?:\.\d{1,3}){3}(?![\w.])")
_IPV6 = re.compile(
    r"(?i)(?<![\w:])(?:(?:[0-9a-f]{1,4}:){7}[0-9a-f]{1,4}"
    r"|(?:[0-9a-f]{1,4}:){1,7}:(?:[0-9a-f]{1,4}(?::[0-9a-f]{1,4}){0,5})?"
    r"|::(?:[0-9a-f]{1,4}:){1,6}[0-9a-f]{1,4}|::1)(?![\w:])")
_UNIX_PATH = re.compile(r"(?<![\w.)\]}/~-])(?:~|\.\.?)?/[\w.@+~%=-]+(?:/[\w.@+~%=-]*)*")
_HOME_PATH = re.compile(r"(?<![\w.])~/[\w.@+~%=/-]*")
_WIN_PATH = re.compile(r"(?i)(?<![\w])[a-z]:\\[^\s'\"<>|]*")
_PREFIXED_TOKEN = re.compile(
    r"\b(?:sk-|pk-|ghp_|gho_|ghs_|github_pat_|xox[abprs]-|AKIA|ASIA|AIza|glpat-|eyJ)"
    r"[A-Za-z0-9_\-.]{12,}")
_TOKEN = re.compile(r"(?<![A-Za-z0-9+/_=-])[A-Za-z0-9+/_=-]{%d,}(?![A-Za-z0-9+/_=-])"
                    % TOKEN_MIN_LENGTH)
_ACCOUNT_RUN = re.compile(r"(?<![\w.])\d{%d,}(?![\w.])" % ACCOUNT_MIN_DIGITS)
_ACCOUNT_GROUPS = re.compile(r"(?<![\w.])\d{2,12}(?:[- ]\d{2,12}){2,}(?![\w.])")
# 口座の数字列の検出から除く日付と時刻 (ISO 8601 の日付・日時、``YYYY/MM/DD``、``HH:MM:SS``)
_DATE_TIME = re.compile(
    r"(?<![\w.:-])(?:\d{4}[-/](?:0[1-9]|1[0-2])[-/](?:0[1-9]|[12]\d|3[01])"
    r"(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?)?"
    r"|\d{2}:\d{2}:\d{2}(?:\.\d+)?)(?![\w:-])")
_SUBSCRIPT_BRACKET = re.compile(r"[\w)\]]\[$")
_STRIPPED_CATEGORIES = frozenset({"Cc", "Cf", "Cs"})
_KEPT_CONTROLS = frozenset({"\n", "\t"})
_PERCENT = re.compile(r"%[0-9A-Fa-f]{2}")
_SCHEME_TOKEN = re.compile(r"(?<![^\s/])([^\s/]+)://")
_KEY_TOKEN = re.compile(r"(?<![\w.-])([\w.-]+)[\"']?\s*[:=]")
_CRED_WORDS = tuple(sorted({
    first + sep + second
    for first, second in (("api", "key"), ("access", "key"), ("secret", "key"),
                          ("private", "key"), ("auth", "token"))
    for sep in ("_", "-", "")} | {"apikey", "token", "password", "passwd", "pwd", "secret",
                                  "credential", "credentials", "key"}, key=len, reverse=True))
_PEM = re.compile(rb"-----BEGIN [A-Z ]*(?:PRIVATE KEY|CERTIFICATE|PGP)[A-Z ]*-----")


def _entropy(text: str) -> float:
    counts = Counter(text)
    total = len(text)
    return -sum(n / total * math.log2(n / total) for n in counts.values())


def _is_token(text: str) -> bool:
    has_digit = any(ch.isdigit() for ch in text)
    has_alpha = any(ch.isalpha() for ch in text)
    entropy = _entropy(text)
    if has_digit and has_alpha:
        return entropy >= TOKEN_MIN_ENTROPY
    has_lower = any(ch.islower() for ch in text)
    has_upper = any(ch.isupper() for ch in text)
    return has_lower and has_upper and entropy >= TOKEN_MIN_ENTROPY_NO_DIGIT


def _token_sub(match: re.Match) -> str:
    return REDACTED if _is_token(match.group(0)) else match.group(0)


def _ipv6_sub(match: re.Match) -> str:
    """``a[1::2]`` のような添字のスライスは IPv6 とみなさない。"""
    if _SUBSCRIPT_BRACKET.search(match.string[max(0, match.start() - 2):match.start()]):
        return match.group(0)
    return REDACTED


def _account_groups_sub(match: re.Match) -> str:
    digits = sum(ch.isdigit() for ch in match.group(0))
    return REDACTED if digits >= ACCOUNT_MIN_DIGITS else match.group(0)


def is_credential_key(key: str) -> bool:
    """config の key が credential を指す名前か (値を無条件に伏せる対象)。"""
    return _CRED_KEY.search(key) is not None


def strip_controls(text: str) -> str:
    """Cc・Cf・Cs (NUL・ゼロ幅・孤立サロゲートなど) を除く。改行とタブだけは残す。"""
    return "".join(ch for ch in text
                   if ch in _KEPT_CONTROLS or unicodedata.category(ch) not in _STRIPPED_CATEGORIES)


_SEPARATORS = str.maketrans({"\u3002": ".", "\uff0e": ".", "\uff61": ".", "\uff0f": "/",
                             "\uff20": "@"})


def _normalize(text: str) -> str:
    """NFKC の前に、NFKC が変換しない (または別の字にする) 区切りを ASCII に寄せる。"""
    return strip_controls(unicodedata.normalize(
        "NFKC", strip_controls(text).translate(_SEPARATORS)))


def _fuzzy_credential_key(key: str) -> bool:
    """ASCII 外の文字を任意の 1 字とみなして、credential 名で終わる key か。"""
    for candidate in (key, key[:-1] if key[-1:] in ("s", "S") else key):
        for word in _CRED_WORDS:
            size = len(word)
            if len(candidate) < size:
                continue
            start = len(candidate) - size
            if start and candidate[start - 1] not in "_.-":
                continue
            pairs = list(zip(candidate[start:], word))
            if (all(ord(ch) > 127 or ch.lower() == expected for ch, expected in pairs)
                    and any(ch.lower() == expected for ch, expected in pairs)):
                return True
    return False


def _non_ascii_residue(text: str) -> bool:
    """正規化後も ASCII 外の文字が、credential の key・URL の scheme の位置に残っているか
    (検出は ASCII 前提なので、残れば伏せる側に倒す。hostname は ``_hosts`` が label ごとに見る)。"""
    if any(not match.group(1).isascii() for match in _SCHEME_TOKEN.finditer(text)):
        return True
    return any(not match.group(1).isascii() and _fuzzy_credential_key(match.group(1))
               for match in _KEY_TOKEN.finditer(text))


def _scan(text: str) -> str:
    text = _URL.sub(REDACTED, text)
    text = _BEARER.sub(REDACTED, text)
    text = _CRED.sub(lambda m: m.group(1) + REDACTED, text)
    text = _PREFIXED_TOKEN.sub(REDACTED, text)
    text = _HOST_CONTEXT.sub(lambda m: m.group(1) + REDACTED, text)
    text = _LOCALHOST.sub(REDACTED, text)
    text = _IPV4.sub(REDACTED, text)
    text = _IPV6.sub(lambda m: _ipv6_sub(m), text)
    text = _WIN_PATH.sub(REDACTED, text)
    text = _HOME_PATH.sub(REDACTED, text)
    text = _UNIX_PATH.sub(REDACTED, text)
    text = _TOKEN.sub(_token_sub, text)
    return _outside_dates(text, _scan_digits)


def _scan_digits(text: str) -> str:
    text = _ACCOUNT_GROUPS.sub(_account_groups_sub, text)
    return _ACCOUNT_RUN.sub(REDACTED, text)


_ACCOUNT_KEY_WORDS = ("account", "acct", "iban", "card", "口座", "customer", "client_id", "login",
                      "user_id", "number")
_ACCOUNT_KEY_NO = re.compile(r"(?:(?<![A-Za-z])[Nn][Oo]|[a-z]No)(?![a-z])")
_KEY_BEFORE_VALUE = re.compile(r"((?:[\w.-]+[ \t]+){0,3}[\w.-]+)[\"']?\s*[:=]\s*[\"']?$")
_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_KEY_SEPARATORS = re.compile(r"[\s-]+")
_ACCOUNT_CONTEXT_WINDOW = 80


def is_account_key(key: str) -> bool:
    """口座らしい key か (その値は日付の形でも伏せる)。空白・``-``・camelCase で区切られた
    複数語の key も、``_`` でつないだ形で見る。``no`` は語として現れるときだけ。"""
    joined = _KEY_SEPARATORS.sub("_", _CAMEL_BOUNDARY.sub("_", key))
    return any(any(word in form.lower() for word in _ACCOUNT_KEY_WORDS)
               or _ACCOUNT_KEY_NO.search(form) is not None for form in (key, joined))


def _is_account_context(before: str) -> bool:
    match = _KEY_BEFORE_VALUE.search(before[-_ACCOUNT_CONTEXT_WINDOW:])
    return match is not None and is_account_key(match.group(1))


def _outside_dates(text: str, scan) -> str:
    """日付・時刻の区間を除いた残りだけに ``scan`` を掛ける。口座らしい key の直後の日付は
    除かない。"""
    out: list[str] = []
    position = 0
    for match in _DATE_TIME.finditer(text):
        if _is_account_context(text[:match.start()]):
            continue
        out.append(scan(text[position:match.start()]))
        out.append(match.group(0))
        position = match.end()
    out.append(scan(text[position:]))
    return "".join(out)


_CHAIN = re.compile(r"(?<![\w-])[\w-]+(?:\.[\w-]+)+")
_ASCII_LEAD = re.compile(r"[A-Za-z0-9_-]*")
_PLUGIN_FILE_NAMES = frozenset(name.lower() for name in PLUGIN_FILES)
_MAX_DECODE_ROUNDS = 3


def _is_host_chain(chain: str) -> bool:
    if chain.lower() in _PLUGIN_FILE_NAMES or chain[:4].lower() == "www.":
        return False  # ``www.`` で始まる語は URL の検出が末尾の path まで含めて伏せる
    labels = chain.split(".")
    if any(label[:4].lower() == "xn--" for label in labels):
        return True
    return any(_ASCII_LEAD.match(label).group(0).lower() in HOST_TLDS for label in labels[1:])


def _hosts(text: str, mask: bytearray | None) -> str:
    """``.`` でつながる語のうち hostname とみなすものを伏せる。``mask`` があれば、対象の範囲
    (文字列・コメント) に 1 字でもかかる語だけ。"""
    def sub(match: re.Match) -> str:
        if mask is not None and not any(mask[match.start():match.end()]):
            return match.group(0)
        return REDACTED if _is_host_chain(match.group(0)) else match.group(0)
    return _CHAIN.sub(sub, text)


def _segments(text: str, spans) -> list[tuple[str, bool]]:
    """``text`` を (部分, hostname の検出対象か) に分ける。``spans`` が None なら全体が対象。"""
    if spans is None:
        return [(text, True)]
    segments: list[tuple[str, bool]] = []
    position = 0
    for start, end in sorted(spans):
        start, end = max(start, position), min(end, len(text))
        if start >= end:
            continue
        if start > position:
            segments.append((text[position:start], False))
        segments.append((text[start:end], True))
        position = end
    if position < len(text):
        segments.append((text[position:], False))
    return segments or [("", True)]


def _scan_segments(segments: list[tuple[str, bool]]) -> str | None:
    """検出して伏せた文字列。値全体を伏せるべきなら None。"""
    text = "".join(part for part, _ in segments)
    mask = None
    if not all(zone for _, zone in segments):
        mask = bytearray(b"".join((b"\x01" if zone else b"\x00") * len(part)
                                  for part, zone in segments))
    out = _scan(_hosts(text, mask))
    return None if _non_ascii_residue(out) else out


def _has_percent(segments: list[tuple[str, bool]]) -> bool:
    return any(zone and _PERCENT.search(part) is not None for part, zone in segments)


def redact(text: str, spans=None) -> str:
    """制御文字の除去と NFKC 正規化をした上で、検出した部分を ``[redacted]`` に置き換える。
    ``%XX`` を decode した複製 (固定点まで、上限 3 回) か、ASCII 外の残留で検出されたら
    値全体を伏せる。``spans`` は Python ソースの 1 行のうち文字列・コメントの桁範囲で、
    指定すると裸の hostname の検出はその範囲だけに掛ける。"""
    segments = [(_normalize(part), zone) for part, zone in _segments(text, spans)]
    out = _scan_segments(segments)
    if out is None:
        return REDACTED
    for _ in range(_MAX_DECODE_ROUNDS):
        if not _has_percent(segments):
            return out
        segments = [(_normalize(urllib.parse.unquote(part, errors="replace")) if zone else part,
                     zone) for part, zone in segments]
        scanned = _scan_segments(segments)
        if scanned is None or scanned != "".join(part for part, _ in segments):
            return REDACTED
    return REDACTED if _has_percent(segments) else out


_ODD_LINE_CHARS = re.compile("[\r\x0b\x0c\x1c-\x1e\x85\u2028\u2029]")
_FSTRING_START = getattr(tokenize, "FSTRING_START", None)
_FSTRING_END = getattr(tokenize, "FSTRING_END", None)


def python_text_spans(source: str) -> list[list[tuple[int, int]]] | None:
    """Python ソース全文のうち、文字列リテラルとコメントの桁範囲を行ごとに返す (行は ``\\n``
    区切り、桁は文字単位)。構文として読めない、または行の数え方が tokenize と食い違いうる
    ソースは None (呼び出し側が全行を自由文として扱う)。"""
    if _ODD_LINE_CHARS.search(source) is not None:
        return None
    lines = source.split("\n")
    spans: list[list[tuple[int, int]]] = [[] for _ in lines]

    def add(start: tuple[int, int], end: tuple[int, int]) -> None:
        for row in range(start[0], end[0] + 1):
            first = start[1] if row == start[0] else 0
            last = end[1] if row == end[0] else len(lines[row - 1])
            spans[row - 1].append((first, last))

    try:
        ast.parse(source)
        depth, opened = 0, (0, 0)
        for token in tokenize.generate_tokens(io.StringIO(source).readline):
            if token.type == tokenize.ERRORTOKEN:
                return None
            if token.type == _FSTRING_START:
                if depth == 0:
                    opened = token.start
                depth += 1
            elif token.type == _FSTRING_END:
                depth -= 1
                if depth == 0:
                    add(opened, token.end)
            elif depth == 0 and token.type in (tokenize.STRING, tokenize.COMMENT):
                add(token.start, token.end)
    except Exception:  # noqa: BLE001 — 読めないソースは全行を自由文として扱う
        return None
    return spans


def redact_value(value: object, spans=None) -> str:
    """1 値の redaction。文字列でない値・対象上限 (8 KiB) を超える値・処理に失敗した値は
    切らずに全文を伏せる。``spans`` は ``redact`` と同じ。"""
    if type(value) is not str:
        return REDACTED
    try:
        if len(value.encode("utf-8", "surrogatepass")) > MAX_REDACT_VALUE_BYTES:
            return REDACTED
        return redact(value, spans)
    except Exception:  # noqa: BLE001 — 検出に失敗した値は伏せる側に倒す
        return REDACTED


def decode_or_none(data: bytes) -> str | None:
    """redactor が判定できるファイルだけ text にする。非 UTF-8、NUL を含む、鍵本体の
    block を含むものは None (呼び出し側がそのファイルの全文を伏せる)。"""
    if b"\x00" in data or _PEM.search(data) is not None:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None
