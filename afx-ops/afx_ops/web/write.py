"""web からの書込み (承認・却下・改善の開始) の入力処理と確認 record の状態機械。

書込みの POST は 2 段で受ける。1 回目 (承認 / 却下 / 改善 / job の取消の form) は何も実行せず、確認 record
を作って確認画面を返す。2 回目 (`/confirm`) は token だけを送り、中身は record に保存したものを
使う。record は web の memory だけに置く (disk・URL・log に token を出さない)。

要求の検査は 1 回目にも 2 回目にも同じものを掛ける:

1. 経路が loopback か TLS (`WriteContext.transport_ok`)
2. Basic で認証済み (adapter が先に済ませる)
3. `Origin` が自分の origin と完全に一致する (Referer は見ない)
4. (2 回目) token が有効・未使用・認証済みのユーザーと一致する
5. 対象外の method・path は 405 / 404

daemon を呼ぶ口は `WriteClient` に抽象化する。どの拒否でも client は 1 回も呼ばれない。
"""
from __future__ import annotations

import hashlib
import html
import re
import secrets
import threading
import time
import unicodedata
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import parse_qsl

from afx_ops.api_client import ApiError, Reply

APPROVE = "approve"
REJECT = "reject"
IMPROVE = "improve"
CANCEL = "cancel"

# 確認 record の状態。未使用 → 実行中 → 結果判明 / 結果不明
UNUSED = "unused"
RUNNING = "running"
DONE = "done"
UNKNOWN = "unknown"

# 結果判明の種類
APPLIED = "applied"
REFUSED = "refused"
NOT_EXECUTED = "not_executed"

TOKEN_BYTES = 32
TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]{43}\Z")
KEY_PREFIX = "afxweb-"
UNUSED_LIMIT = 32
UNUSED_TTL = 600.0
CONSUMED_LIMIT = 32
CONSUMED_TTL = 3600.0
MAX_REASON_CHARS = 500
MAX_COMMENT_CHARS = 1000
# 画面が許す最長の入力が、UTF-8 で 1 字 4 byte・percent encoding で 3 倍 (12 byte) になっても
# 送れる大きさ。コメント 1000 字で 12000 byte、ほかの field と名前に 288 byte の余裕を見る。
MAX_WRITE_BODY = MAX_COMMENT_CHARS * 12 + 288
MIN_DURATION_SEC, MAX_DURATION_SEC, MAX_RUNS = 60, 86400, 24

DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_INTEGER = re.compile(r"[1-9][0-9]{0,8}\Z")
_JOB_ID = re.compile(r"[0-9a-f]{32}\Z")
_WORD = re.compile(r"[a-z_]{1,40}\Z")
_FORBIDDEN_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Zl", "Zp"})
OUTCOME_UNKNOWN_CODE = "outcome_unknown"
# daemon (本体の ErrorCode) が返しうる code。値の一致は本体側の test で固定する。
KNOWN_DAEMON_CODES = frozenset({
    "invalid_argument", "forbidden", "not_found", "request_timeout", "autopilot_restricted",
    "idempotency_mismatch", "outcome_unknown", "job_running", "invalid_state",
    "attempt_changed", "already_decided", "payload_changed", "payload_too_large",
    "unsupported_kind", "plugin_busy", "mission_busy", "improve_running",
    "decision_in_progress", "generation_mismatch", "not_latched", "database_busy",
    "unavailable", "internal",
})
UNRECOGNIZED_CODE = "other"
_DEFAULT_PORT = {"http": 80, "https": 443}


class WriteReject(Exception):
    """書込みの検査で拒否した。`status` は 4xx。daemon は呼んでいない。"""

    def __init__(self, status: int) -> None:
        super().__init__(status)
        self.status = status


class WriteClient(Protocol):
    """書込みの daemon 呼出し。`actor` は `web:<user>`。応答の解釈は呼出し側が行う。"""

    def approval(self, approval_id: int) -> Reply: ...

    def approve(self, actor: str, approval_id: int, *, payload_sha256: str) -> Reply: ...

    def reject(self, actor: str, approval_id: int, *, reason: str | None) -> Reply: ...

    def improve_run(self, actor: str, body: dict, *, idempotency_key: str) -> Reply: ...

    def job(self, actor: str, job_id: str) -> Reply: ...

    def cancel_job(self, actor: str, job_id: str) -> Reply: ...


@dataclass(frozen=True)
class Intent:
    """確認 record に結び付ける操作の中身。1 回目の POST で決まり、2 回目では変えられない。"""

    action: str
    approval_id: int | None = None
    payload_sha256: str | None = None
    reason: str | None = None
    duration_sec: int | None = None
    max_runs: int | None = None
    comment: str | None = None
    job_id: str | None = None

    def improve_body(self) -> dict:
        body: dict = {}
        if self.duration_sec is not None:
            body["duration_sec"] = self.duration_sec
        if self.max_runs is not None:
            body["max_runs"] = self.max_runs
        if self.comment is not None:
            body["comment"] = self.comment
        return body


@dataclass(frozen=True)
class Outcome:
    """結果判明の中身。`code` は daemon の固定語 (自由文は持たない)。"""

    kind: str
    status: int | None = None
    code: str | None = None
    job_id: str | None = None


@dataclass(frozen=True)
class Snapshot:
    """record の読み取り専用の写し。token は持たない。"""

    record_id: str
    state: str
    intent: Intent
    outcome: Outcome | None


@dataclass
class _Record:
    record_id: str
    user: str
    intent: Intent
    created: float
    state: str = UNUSED
    consumed_at: float | None = None
    outcome: Outcome | None = None

    def snapshot(self) -> Snapshot:
        return Snapshot(self.record_id, self.state, self.intent, self.outcome)


def record_id_of(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def idempotency_key_of(token: str) -> str:
    """daemon が永続化する key。token 自体ではなく、固定 prefix + token の SHA-256。"""
    return KEY_PREFIX + record_id_of(token)


class ConfirmationStore:
    """確認 record の memory 上の表。状態の遷移はすべて 1 つの lock の下で行う。

    未使用は `UNUSED_LIMIT` 件・`UNUSED_TTL` 秒まで。満杯なら新規作成を拒否し、既存の record は
    そのまま。消費済み (実行中・結果判明・結果不明) は `CONSUMED_LIMIT` 件・`CONSUMED_TTL` 秒
    まで残して結果の回復に使う。
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._records: dict[str, _Record] = {}

    def create(self, user: str, intent: Intent) -> str:
        """未使用の record を作り、使い捨ての token を返す。満杯は `WriteReject(429)`。"""
        with self._lock:
            now = self._clock()
            self._purge(now)
            if sum(1 for r in self._records.values() if r.state == UNUSED) >= UNUSED_LIMIT:
                raise WriteReject(429)
            token = secrets.token_urlsafe(TOKEN_BYTES)
            record_id = record_id_of(token)
            self._records[record_id] = _Record(record_id, user, intent, now)
            return token

    def consume(self, token: str, user: str) -> Snapshot:
        """未使用の record を実行中にする。同じ token は 1 回しか通らない。"""
        with self._lock:
            now = self._clock()
            record = self._find(token, user, now)
            if record.state != UNUSED:
                raise WriteReject(409)
            if self._consumed_count() >= CONSUMED_LIMIT and not self._evict_consumed():
                raise WriteReject(429)
            record.state = RUNNING
            record.consumed_at = now
            return record.snapshot()

    def finish(self, record_id: str, state: str, outcome: Outcome | None) -> None:
        if state not in (DONE, UNKNOWN):
            raise ValueError("state")
        with self._lock:
            record = self._records.get(record_id)
            if record is not None and record.state == RUNNING:
                record.state = state
                record.outcome = outcome

    def check(self, token: str, user: str) -> Snapshot:
        """「結果を確かめる」の入口。結果不明の改善の record だけを実行中に戻して返す。

        結果判明は保存した結果をそのまま返す。承認・却下は replay しないので結果不明のまま。
        未使用・実行中は 409。
        """
        with self._lock:
            now = self._clock()
            record = self._find(token, user, now)
            if record.state in (UNUSED, RUNNING):
                raise WriteReject(409)
            snapshot = record.snapshot()
            if record.state == UNKNOWN and record.intent.action == IMPROVE:
                record.state = RUNNING
            return snapshot

    def counts(self) -> tuple[int, int]:
        """(未使用, 消費済み) の件数。"""
        with self._lock:
            unused = sum(1 for r in self._records.values() if r.state == UNUSED)
            return unused, len(self._records) - unused

    def _find(self, token: str, user: str, now: float) -> _Record:
        if TOKEN_PATTERN.fullmatch(token) is None:
            raise WriteReject(400)
        record = self._records.get(record_id_of(token))
        if record is None:
            raise WriteReject(403)
        if record.state == UNUSED and record.created + UNUSED_TTL <= now:
            del self._records[record.record_id]
            raise WriteReject(410)
        if record.user != user:
            raise WriteReject(403)
        if self._consumed_expired(record, now):
            del self._records[record.record_id]
            raise WriteReject(410)
        return record

    @staticmethod
    def _consumed_expired(record: _Record, now: float) -> bool:
        return (record.state != UNUSED and record.state != RUNNING
                and record.consumed_at is not None
                and record.consumed_at + CONSUMED_TTL <= now)

    def _purge(self, now: float) -> None:
        for record_id, record in list(self._records.items()):
            if record.state == UNUSED:
                expired = record.created + UNUSED_TTL <= now
            else:
                expired = self._consumed_expired(record, now)
            if expired:
                del self._records[record_id]

    def _consumed_count(self) -> int:
        return sum(1 for r in self._records.values() if r.state != UNUSED)

    def _evict_consumed(self) -> bool:
        """実行中でない最も古い消費済みを 1 件捨てる。無ければ False。"""
        candidates = [r for r in self._records.values()
                      if r.state in (DONE, UNKNOWN) and r.consumed_at is not None]
        if not candidates:
            return False
        oldest = min(candidates, key=lambda r: r.consumed_at)
        del self._records[oldest.record_id]
        return True


@dataclass(frozen=True)
class WriteContext:
    """書込みを有効にした web の状態。adapter が受け取ったときだけ書込みの route が生きる。"""

    user: str
    port: int
    client: WriteClient
    store: ConfirmationStore
    scheme: str = "http"
    loopback: bool = True
    tls: bool = False

    @property
    def transport_ok(self) -> bool:
        """書込みを許す経路か (loopback か TLS)。LAN の平文は書けない。"""
        return self.loopback or self.tls

    @property
    def actor(self) -> str:
        return f"web:{self.user}"

    def expected_origin(self, host: str) -> str:
        """検査済みの Host (小文字化) と設定の scheme・port から作る自分の origin。"""
        origin = f"{self.scheme}://{host.partition(':')[0].lower()}"
        if self.port != _DEFAULT_PORT.get(self.scheme):
            origin += f":{self.port}"
        return origin


# --- route ---------------------------------------------------------------------------------

@dataclass(frozen=True)
class WriteMatch:
    kind: str
    methods: tuple[str, ...]
    approval_id: int | None = None
    job_id: str | None = None


_APPROVAL_DECISION = re.compile(r"/approvals/([1-9][0-9]{0,17})/(approve|reject)\Z")
_JOB_PATH = re.compile(r"/jobs/([0-9a-f]{32})(/cancel)?\Z")
_FIXED = {
    "/improve": WriteMatch("improve_form", ("GET", "HEAD")),
    "/improve/runs": WriteMatch(IMPROVE, ("POST",)),
    "/confirm": WriteMatch("confirm", ("POST",)),
    "/confirm/check": WriteMatch("check", ("POST",)),
}


def resolve_write_route(path: str) -> WriteMatch | None:
    fixed = _FIXED.get(path)
    if fixed is not None:
        return fixed
    matched = _APPROVAL_DECISION.fullmatch(path)
    if matched is not None:
        return WriteMatch(matched.group(2), ("POST",), int(matched.group(1)))
    matched = _JOB_PATH.fullmatch(path)
    if matched is not None:
        if matched.group(2):
            return WriteMatch(CANCEL, ("POST",), job_id=matched.group(1))
        return WriteMatch("job_view", ("GET", "HEAD"), job_id=matched.group(1))
    return None


# --- 入力の検査 ----------------------------------------------------------------------------

def check_origin(context: WriteContext, headers: tuple[tuple[str, str], ...],
                 host_values: list[str]) -> None:
    """`Origin` は 1 つだけで、自分の origin と完全に一致すること。無い・`null`・重複・不正は拒否。"""
    values = [value for name, value in headers if name.lower() == "origin"]
    if len(values) != 1 or len(host_values) != 1:
        raise WriteReject(403)
    if values[0] != context.expected_origin(host_values[0]):
        raise WriteReject(403)


def parse_form(headers: tuple[tuple[str, str], ...], body: bytes,
               fields: Mapping[str, bool]) -> dict[str, str]:
    """form-urlencoded の body を厳密に読む。`fields` は許す名前 → 必須か。"""
    types = [value for name, value in headers if name.lower() == "content-type"]
    if len(types) != 1:
        raise WriteReject(415)
    media, _sep, params = types[0].partition(";")
    if media.strip().lower() != "application/x-www-form-urlencoded":
        raise WriteReject(415)
    if params.strip().lower().replace(" ", "") not in ("", "charset=utf-8"):
        raise WriteReject(415)
    if len(body) > MAX_WRITE_BODY:
        raise WriteReject(413)
    try:
        text = body.decode("ascii")
        pairs = parse_qsl(text, keep_blank_values=True, strict_parsing=bool(text),
                          encoding="utf-8", errors="strict", max_num_fields=len(fields))
    except (UnicodeDecodeError, ValueError):
        raise WriteReject(400) from None
    values: dict[str, str] = {}
    for name, value in pairs:
        if name not in fields or name in values:
            raise WriteReject(400)
        values[name] = value
    if any(required and name not in values for name, required in fields.items()):
        raise WriteReject(400)
    return values


def _clean_text(value: str, limit: int) -> str | None:
    """前後の空白を除いた 1 行の文。空は None。制御・書式文字や長すぎるものは拒否。"""
    text = value.strip()
    if not text:
        return None
    if len(text) > limit or any(unicodedata.category(ch) in _FORBIDDEN_CATEGORIES
                                for ch in text):
        raise WriteReject(400)
    return text


def _integer(value: str, low: int, high: int) -> int | None:
    if value == "":
        return None
    if _INTEGER.fullmatch(value) is None or not low <= int(value) <= high:
        raise WriteReject(400)
    return int(value)


def decision_intent(kind: str, approval_id: int, form: Mapping[str, str]) -> Intent:
    digest = form["payload_sha256"]
    if DIGEST.fullmatch(digest) is None:
        raise WriteReject(400)
    if kind == APPROVE:
        return Intent(APPROVE, approval_id, digest)
    return Intent(REJECT, approval_id, digest, reason=_clean_text(form.get("reason", ""),
                                                               MAX_REASON_CHARS))


def improve_intent(form: Mapping[str, str]) -> Intent:
    return Intent(IMPROVE,
                  duration_sec=_integer(form.get("duration_sec", ""), MIN_DURATION_SEC,
                                        MAX_DURATION_SEC),
                  max_runs=_integer(form.get("max_runs", ""), 1, MAX_RUNS),
                  comment=_clean_text(form.get("comment", ""), MAX_COMMENT_CHARS))


FORM_FIELDS: Mapping[str, Mapping[str, bool]] = {
    APPROVE: {"payload_sha256": True},
    REJECT: {"payload_sha256": True, "reason": False},
    IMPROVE: {"duration_sec": False, "max_runs": False, "comment": False},
    "confirm": {"token": True},
    "check": {"token": True},
    CANCEL: {},
}


# --- 実行 ----------------------------------------------------------------------------------

def _fixed_word(value: object) -> str | None:
    return value if type(value) is str and _WORD.fullmatch(value) else None


def _classify(reply: object, expect_id: str | None = None) -> tuple[str, Outcome | None]:
    """daemon の応答を (状態, 結果) にする。

    5xx と読めない応答、および daemon が `outcome_unknown` と答えたもの (HTTP status は問わない。
    実際は 409) は結果不明。副作用が起きたか分からないので「受け付けなかった」とは言わない。
    """
    status = getattr(reply, "status", None)
    if type(status) is not int or isinstance(status, bool) or status >= 500 or status < 200:
        return UNKNOWN, None
    code = _fixed_word(getattr(reply, "code", None))
    if code == OUTCOME_UNKNOWN_CODE:
        return UNKNOWN, None
    if code is not None and code not in KNOWN_DAEMON_CODES:
        code = UNRECOGNIZED_CODE
    if 200 <= status < 300 and code is None:
        data = getattr(reply, "data", None)
        if expect_id is not None and not (type(data) is dict and data.get("id") == expect_id):
            return UNKNOWN, None
        job = data.get("job_id") if type(data) is dict else None
        job_id = job if type(job) is str and _JOB_ID.fullmatch(job) else None
        return DONE, Outcome(APPLIED, status, None, job_id)
    return DONE, Outcome(REFUSED, status, code)


def _call(function: Callable[[], object],
          expect_id: str | None = None) -> tuple[str, Outcome | None]:
    """daemon への書込み 1 回。応答を失った (例外) ときは結果不明にする。

    鍵を 1 byte も送っていない失敗だけは、実行されていないと言える。
    """
    try:
        reply = function()
    except ApiError as error:
        if error.token_sent is False:
            return DONE, Outcome(NOT_EXECUTED, None, _fixed_word(error.reason))
        return UNKNOWN, None
    except Exception:  # noqa: BLE001 — 原因の文は持たない
        return UNKNOWN, None
    return _classify(reply, expect_id)


def _reject_verified(context: WriteContext, intent: Intent) -> tuple[str, Outcome | None]:
    """却下の直前に詳細を取り直し、見た時点の digest と違えば実行しない。"""
    try:
        reply = context.client.approval(intent.approval_id)
    except Exception:  # noqa: BLE001
        return DONE, Outcome(NOT_EXECUTED, None, "verify_failed")
    data = getattr(reply, "data", None)
    if getattr(reply, "status", None) != 200 or type(data) is not dict:
        return DONE, Outcome(NOT_EXECUTED, None, "verify_failed")
    if data.get("payload_sha256") != intent.payload_sha256:
        return DONE, Outcome(NOT_EXECUTED, None, "digest_changed")
    return _call(lambda: context.client.reject(context.actor, intent.approval_id,
                                               reason=intent.reason))


def cancel_intent(job_id: str) -> Intent:
    return Intent(CANCEL, job_id=job_id)


def execute(context: WriteContext, snapshot: Snapshot, token: str) -> Snapshot:
    """実行中にした record の中身を daemon に送り、結果を record に残して返す。"""
    intent = snapshot.intent
    if intent.action == APPROVE:
        state, outcome = _call(lambda: context.client.approve(
            context.actor, intent.approval_id, payload_sha256=intent.payload_sha256))
    elif intent.action == REJECT:
        state, outcome = _reject_verified(context, intent)
    elif intent.action == CANCEL:
        state, outcome = _call(lambda: context.client.cancel_job(context.actor, intent.job_id),
                               expect_id=intent.job_id)
    elif intent.action == IMPROVE:
        key = idempotency_key_of(token)
        state, outcome = _call(lambda: context.client.improve_run(
            context.actor, intent.improve_body(), idempotency_key=key))
    else:
        raise ValueError("action")
    context.store.finish(snapshot.record_id, state, outcome)
    return Snapshot(snapshot.record_id, state, intent, outcome)


def escape(value: object) -> str:
    return html.escape(str(value), quote=True)
