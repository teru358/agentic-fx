"""表示する全ての値に共通で掛ける 1 つの検出と伏せ字の部品。

値の出どころは全て非信頼 (agent の申告、config.yaml の key と value、plugin コードの
文字列・コメント、およびそれらから作る diff の全行) である。検出対象は絶対パス、URL・
裸の hostname・IP、credential の代入、高エントロピーの token、口座らしい数字列。

順序は 入力読み取り上限 → Cc/Cf/Cs 除去 → NFKC 正規化 → redaction → 表示形式の escape →
表示上限とする。この部品は除去と redaction を受け持ち、escape と表示上限は表示面が後から掛ける。
表示面は Cc・Cf・Cs を消すので、検出をその前に掛けると消えた後に繋がる秘密を取り逃がす。
そのため検出は除去・正規化済みの文字列に掛け、出力もその文字列を基に作る (全角の
``ａｐｉ＿ｋｅｙ`` などは正規化で ASCII になる)。``%XX`` を 1 回だけ decode した複製にも検出を
掛け、どちらかで検出されたら値全体を伏せる (decode 結果は表示に使わない)。正規化後も
ASCII 外の文字が credential の key・URL の scheme・hostname の位置に残る値も全体を伏せる。改行とタブは
表示面が行の区切り・字下げとして扱うので残す (CR は表示面で改行に正規化されるが、
単独の CR で語を割る回避を防ぐため除去する)。

fail-closed: 1 値の大きさが redaction 対象上限を超えるときは、切らずに全文を伏せる
(切ると token が境界で割れて検出を逃すため)。判定できないファイル (非 UTF-8、NUL を
含む、鍵本体の block を含む) はファイル単位で全文を伏せる。検出処理が例外を出した値も
伏せる側に倒す。
"""
from __future__ import annotations

import math
import re
import unicodedata
import urllib.parse
from collections import Counter

from .change_limits import (ACCOUNT_MIN_DIGITS, MAX_REDACT_VALUE_BYTES, REDACTED,
                            TOKEN_MIN_ENTROPY, TOKEN_MIN_ENTROPY_NO_DIGIT,
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
# 文脈なしで hostname とみなす TLD。info / test / app / in / me / co / home / int / local などの
# 一般語は ``obj.info`` のような属性アクセスと区別できないので文脈 (URL・email・host= など)
# が要る。直後が ``(`` なら呼び出しとして除く。
_HOST_BARE = re.compile(
    r"(?i)(?<![\w.-])" + _HOST_LABELS + r"+(?:com|net|org|io|jp|dev|cloud|ai|internal|corp|lan)"
    r"(?![\w(-])")
_HOST_LOCAL_BARE = re.compile(
    r"(?i)(?<![\w.-])" + _HOST_LABELS + r"{2,}local(?![\w(-])")
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
_UNI_HOST = re.compile(
    r"(?i)(?<![\w.-])(?:[\w-]+\.)+"
    r"(?:com|net|org|io|jp|co|dev|app|cloud|info|biz|edu|gov|mil|int|xyz|online|site|"
    r"tech|ai|me|us|uk|de|fr|cn|ru|kr|in|au|ca|eu|local|localdomain|lan|internal|home|"
    r"corp|intranet|test|example|invalid)(?![\w-])")
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


def _normalize(text: str) -> str:
    return strip_controls(unicodedata.normalize("NFKC", strip_controls(text)))


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
    """正規化後も ASCII 外の文字が、credential の key・URL の scheme・hostname の位置に
    残っているか (検出は ASCII 前提なので、残れば伏せる側に倒す)。"""
    if any(not match.group(1).isascii() for match in _SCHEME_TOKEN.finditer(text)):
        return True
    if any(not match.group(0).isascii() for match in _UNI_HOST.finditer(text)):
        return True
    return any(not match.group(1).isascii() and _fuzzy_credential_key(match.group(1))
               for match in _KEY_TOKEN.finditer(text))


def _scan(text: str) -> str:
    text = _URL.sub(REDACTED, text)
    text = _BEARER.sub(REDACTED, text)
    text = _CRED.sub(lambda m: m.group(1) + REDACTED, text)
    text = _PREFIXED_TOKEN.sub(REDACTED, text)
    text = _HOST_CONTEXT.sub(lambda m: m.group(1) + REDACTED, text)
    text = _HOST_BARE.sub(REDACTED, text)
    text = _HOST_LOCAL_BARE.sub(REDACTED, text)
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


def _outside_dates(text: str, scan) -> str:
    """日付・時刻の区間を除いた残りだけに ``scan`` を掛ける。"""
    out: list[str] = []
    position = 0
    for match in _DATE_TIME.finditer(text):
        out.append(scan(text[position:match.start()]))
        out.append(match.group(0))
        position = match.end()
    out.append(scan(text[position:]))
    return "".join(out)


def redact(text: str) -> str:
    """制御文字の除去と NFKC 正規化をした上で、検出した部分を ``[redacted]`` に置き換える。
    ``%XX`` を decode した複製か、ASCII 外の残留で検出されたら値全体を伏せる。"""
    text = _normalize(text)
    out = _scan(text)
    if _non_ascii_residue(out):
        return REDACTED
    if _PERCENT.search(text) is not None:
        decoded = _normalize(urllib.parse.unquote(text, errors="replace"))
        decoded_out = _scan(decoded)
        if decoded_out != decoded or _non_ascii_residue(decoded_out):
            return REDACTED
    return out


def redact_value(value: object) -> str:
    """1 値の redaction。文字列でない値・対象上限 (8 KiB) を超える値・処理に失敗した値は
    切らずに全文を伏せる。"""
    if type(value) is not str:
        return REDACTED
    try:
        if len(value.encode("utf-8", "surrogatepass")) > MAX_REDACT_VALUE_BYTES:
            return REDACTED
        return redact(value)
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
