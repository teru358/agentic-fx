"""人に見せる時刻の換算の部品 (stdlib だけ)。

内部は UTC のまま運び、表示のときだけ表示 tz に換算する。この module は
`agentic_fx` の他の module を import しない。afx-ops へは byte 一致の写しとして置く
(正本はこの file。直接編集するのは正本だけ)。

この module の関数は送出しない。換算できないものは「換算できない」を返すか、
入力をそのまま返す。
"""
from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable
from datetime import datetime, timedelta, timezone, tzinfo
from functools import lru_cache
from typing import NamedTuple
from zoneinfo import ZoneInfo

PRECISION_MINUTE = "minute"
PRECISION_SECOND = "second"
PRECISION_MILLISECOND = "millisecond"
_PRECISIONS = frozenset({PRECISION_MINUTE, PRECISION_SECOND, PRECISION_MILLISECOND})

_TZ_NAME = re.compile(r"[A-Za-z0-9_+\-/]{1,64}")


class Converted(NamedTuple):
    """換算の結果。`ok` が偽のとき `text` は空 (換算できない)。"""

    ok: bool
    text: str


UNCONVERTIBLE = Converted(False, "")


# ---- 表示 tz ---------------------------------------------------------------

@lru_cache(maxsize=128)
def _resolve(name: str) -> tzinfo | None:
    if _TZ_NAME.fullmatch(name) is None:
        return None
    if name == "UTC":
        return timezone.utc
    try:
        return ZoneInfo(name)
    except Exception:  # noqa: BLE001 — 読めない名前・読めない環境は「換算できない」
        return None


def resolve_zone(tz_name: object) -> tzinfo | None:
    """tz 名から zone を得る。名前の型違い・形違い・読めない名前は None。"""
    if not isinstance(tz_name, str):
        return None
    try:
        return _resolve(tz_name)
    except Exception:  # noqa: BLE001
        return None


def clear_zone_cache() -> None:
    _resolve.cache_clear()


def _format_offset(offset: timedelta) -> str:
    minutes = int(offset.total_seconds()) // 60
    sign = "+" if minutes >= 0 else "-"
    hours, rest = divmod(abs(minutes), 60)
    return f"{sign}{hours:02d}:{rest:02d}"


def _offset_at(zone: tzinfo, when: datetime | None) -> timedelta | None:
    try:
        moment = when if when is not None and when.tzinfo is not None \
            else datetime.now(timezone.utc)
        return moment.astimezone(zone).utcoffset()
    except Exception:  # noqa: BLE001
        return None


def effective_tz_name(tz_name: object) -> str:
    """実際に換算に使える tz の名前。使えなければ `UTC`。"""
    return tz_name if resolve_zone(tz_name) is not None else "UTC"  # type: ignore[return-value]


def display_header(tz_name: object, now: datetime | None = None) -> str:
    """`表示 tz: Asia/Tokyo (+09:00)`。換算に使えない tz は `UTC (+00:00)`。"""
    zone = resolve_zone(tz_name)
    if zone is None:
        return "表示 tz: UTC (+00:00)"
    offset = _offset_at(zone, now)
    if offset is None or offset % timedelta(minutes=1):
        return "表示 tz: UTC (+00:00)"
    return f"表示 tz: {tz_name} ({_format_offset(offset)})"


# ---- 時刻の換算 ------------------------------------------------------------

def _as_aware(value: object) -> datetime | None:
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value)
        except ValueError:
            return None
    else:
        return None
    if dt.tzinfo is None or dt.utcoffset() is None:
        return None
    return dt


def convert_time(value: object, tz_name: object,
                 precision: str = PRECISION_SECOND) -> Converted:
    """aware datetime か ISO 文字列を表示 tz に換算する。

    表示は `YYYY-MM-DD HH:MM[:SS[,mmm]] ±HH:MM`。offset は zone に移した後の
    `utcoffset()` から作る。naive・読めない文字列・範囲外・tz の不正はすべて
    「換算できない」で、送出しない。
    """
    try:
        if precision not in _PRECISIONS:
            return UNCONVERTIBLE
        zone = resolve_zone(tz_name)
        dt = _as_aware(value)
        if zone is None or dt is None:
            return UNCONVERTIBLE
        local = dt.astimezone(zone)
        offset = local.utcoffset()
        if offset is None or offset % timedelta(minutes=1):
            return UNCONVERTIBLE
        text = (f"{local.year:04d}-{local.month:02d}-{local.day:02d} "
                f"{local.hour:02d}:{local.minute:02d}")
        if precision != PRECISION_MINUTE:
            text += f":{local.second:02d}"
        if precision == PRECISION_MILLISECOND:
            text += f",{local.microsecond // 1000:03d}"
        return Converted(True, f"{text} {_format_offset(offset)}")
    except Exception:  # noqa: BLE001 — 部品は送出しない
        return UNCONVERTIBLE


# ---- 自由文の時刻 token ----------------------------------------------------

_TOKEN = re.compile(
    r"(?<![\w.:+-])\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\.\d{1,6})?(Z|[+-]\d\d:\d\d)"
    r"(?![\w+-]|:\S|\.\d)")


def convert_tokens(text: str, tz_name: object) -> str:
    """自由文の中の offset 0 の ISO 時刻だけを表示 tz に換算する (秒まで)。"""
    try:
        if not isinstance(text, str) or resolve_zone(tz_name) is None:
            return text

        def replace(match: re.Match[str]) -> str:
            raw = match.group(0)
            try:
                if datetime.fromisoformat(raw).utcoffset() != timedelta(0):
                    return raw
            except ValueError:
                return raw
            converted = convert_time(raw, tz_name, PRECISION_SECOND)
            return converted.text if converted.ok else raw

        return _TOKEN.sub(replace, text)
    except Exception:  # noqa: BLE001
        return text


# ---- ログ行頭 --------------------------------------------------------------

# activity.py の Category の値。一致は test で検査する。
ACTIVITY_CATEGORIES = frozenset(
    {"NEWS", "TECH", "AGGREGATE", "TRADE", "IMPROVE", "APPROVAL", "SYSTEM"})
ACTIVITY_FIELD_COUNT = 5

_UTC_ISO = re.compile(
    r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\.\d{1,6})?(Z|[+-]00:00)")


def _split_newline(line: str) -> tuple[str, str]:
    body = line.rstrip("\r\n")
    return body, line[len(body):]


def convert_activity_line(line: str, tz_name: object) -> str:
    """activity の行 (tab 区切り 5 field) の行頭を換算する。

    第 1 field が UTC の ISO で、第 2 field が既知の category のときだけ行頭を
    換算する。本文 (第 4 field) の token は `(category, event)` が builder の表に
    ある行に限って換算する。形が合わない行はそのまま返す。
    """
    try:
        if not isinstance(line, str) or resolve_zone(tz_name) is None:
            return line
        body, newline = _split_newline(line)
        fields = body.split("\t")
        if len(fields) != ACTIVITY_FIELD_COUNT:
            return line
        stamp, category, event, summary, _ref = fields
        if category not in ACTIVITY_CATEGORIES or _UTC_ISO.fullmatch(stamp) is None:
            return line
        head = convert_time(stamp, tz_name, PRECISION_SECOND)
        if not head.ok:
            return line
        fields[0] = head.text
        if (category, event) in BUILDERS:
            fields[3] = convert_tokens(summary, tz_name)
        return "\t".join(fields) + newline
    except Exception:  # noqa: BLE001
        return line


_LOG_HEAD = re.compile(
    r"(\d{4})-(\d\d)-(\d\d) (\d\d):(\d\d):(\d\d),(\d{3}) UTC "
    r"(?:DEBUG|INFO|WARNING|ERROR|CRITICAL) [A-Za-z0-9_.\-]+:(?: |$)")


def convert_log_line(line: str, tz_name: object) -> str:
    """agentic.log の行頭 (`YYYY-MM-DD HH:MM:SS,mmm UTC <LEVEL> <logger>:`) を換算する。"""
    try:
        if not isinstance(line, str) or resolve_zone(tz_name) is None:
            return line
        match = _LOG_HEAD.match(line)
        if match is None:
            return line
        year, month, day, hour, minute, second, milli = (int(g) for g in match.groups())
        stamp = datetime(year, month, day, hour, minute, second, milli * 1000,
                         tzinfo=timezone.utc)
        converted = convert_time(stamp, tz_name, PRECISION_MILLISECOND)
        if not converted.ok:
            return line
        # 行頭の「<時刻> UTC」を換算後の時刻に置き換え、` <LEVEL> ...` はそのまま
        utc_end = match.start() + len("YYYY-MM-DD HH:MM:SS,mmm")
        return converted.text + line[utc_end + len(" UTC"):]
    except Exception:  # noqa: BLE001
        return line


# ---- 時刻 field の宣言 -----------------------------------------------------

# API の DTO と event の fields で、人に見せうる時刻を持つ field の名前。
# 新しい時刻 field はここに足す (足せば web / ctl の汎用描画で換算される)。
TIME_FIELDS = frozenset({
    "ts", "created_at", "updated_at", "started_at", "latched_at", "decided_at",
    "computed_at", "observed_at", "effective_at", "deadline_at", "expires_at",
    "first_failed_at", "last_failed_at", "last_attempt_at",
    "expected_last_attempt_at", "restricted_since", "since", "deadline", "until",
})


# ---- 識別子と本文 builder --------------------------------------------------

def utc_token(dt: object) -> str:
    """時点を `YYYY-MM-DDTHH:MM:SSZ` にする。naive は `naive:` を前に付けた ISO。"""
    if not isinstance(dt, datetime):
        return _non_datetime_text(dt)
    if dt.tzinfo is None or dt.utcoffset() is None:
        return f"naive:{dt.isoformat()}"
    try:
        u = dt.astimezone(timezone.utc)
        return (f"{u.year:04d}-{u.month:02d}-{u.day:02d}T"
                f"{u.hour:02d}:{u.minute:02d}:{u.second:02d}Z")
    except Exception:  # noqa: BLE001 — 範囲外
        return f"invalid:{dt.isoformat()}"


def _non_datetime_text(value: object) -> str:
    if isinstance(value, str):
        try:
            return utc_token(datetime.fromisoformat(value))
        except ValueError:
            return value
    return str(value)


def bar_label(dt: object) -> str:
    """足のラベル `YYYY-MM-DDTHH:MM UTC` (識別子。換算しない)。"""
    if not isinstance(dt, datetime):
        return _non_datetime_text(dt)
    if dt.tzinfo is None or dt.utcoffset() is None:
        return f"naive:{dt.isoformat(timespec='minutes')}"
    try:
        u = dt.astimezone(timezone.utc)
        return (f"{u.year:04d}-{u.month:02d}-{u.day:02d}T"
                f"{u.hour:02d}:{u.minute:02d} UTC")
    except Exception:  # noqa: BLE001
        return f"invalid:{dt.isoformat(timespec='minutes')}"


_BODY_UNAVAILABLE = "(本文を組み立てられない)"

Built = tuple[str, str, str]


def _build(category: str, event: str, make: Callable[[], str]) -> Built:
    try:
        body = make()
    except Exception:  # noqa: BLE001 — builder は送出しない
        body = _BODY_UNAVAILABLE
    return category, event, body


def _fetch_skipped(kind: str, due: object) -> Built:
    return _build("NEWS", f"{kind}_fetch_skipped",
                  lambda: f"前回の取得から間もないので次回は {utc_token(due)}")


def news_fetch_skipped(due: object) -> Built:
    return _fetch_skipped("news", due)


def econ_fetch_skipped(due: object) -> Built:
    return _fetch_skipped("econ", due)


def news_source_skipped(prefix: str, until: object) -> Built:
    return _build("NEWS", "news_source_skipped",
                  lambda: f"{prefix}{utc_token(until)} まで待つ")


def datafeed_restricted(epoch: object, stalled: Iterable[object],
                        expected: object, deadline: object) -> Built:
    return _build(
        "SYSTEM", "datafeed_restricted",
        lambda: (f"epoch={epoch} stalled={sorted(stalled)} "
                 f"expected={utc_token(expected)} deadline={utc_token(deadline)}"))


def time_rule_cutover(capture: dict) -> Built:
    """保存した capture の JSON (`activity_logged` は除く)。"""
    return _build(
        "SYSTEM", "time_rule_cutover",
        lambda: json.dumps({k: v for k, v in capture.items() if k != "activity_logged"},
                           sort_keys=True, ensure_ascii=False, default=str))


def kill_switch_reset(generation: object, latched_at: object) -> Built:
    return _build(
        "SYSTEM", "kill_switch_reset",
        lambda: ("human explicit reset via shell "
                 f"gen={generation} latched_at={utc_token(latched_at)}"))


# `(category, event)` -> builder。この表の key の集合が、本文の token を換算する
# event の集合そのもの。
BUILDERS: dict[tuple[str, str], Callable[..., Built]] = {
    ("NEWS", "news_fetch_skipped"): news_fetch_skipped,
    ("NEWS", "econ_fetch_skipped"): econ_fetch_skipped,
    ("NEWS", "news_source_skipped"): news_source_skipped,
    ("SYSTEM", "datafeed_restricted"): datafeed_restricted,
    ("SYSTEM", "time_rule_cutover"): time_rule_cutover,
    ("SYSTEM", "kill_switch_reset"): kill_switch_reset,
}
