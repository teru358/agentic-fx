"""Basic 認証と rate limit の部品 (fake 時計): 認証の形、比較の回数と長さ、窓、上限、Retry-After。"""
from __future__ import annotations

import base64
import hashlib
import threading
import time

import pytest

from afx_ops.isolation.accept_loop import Journal
from afx_ops.web import auth

PASSWORD = b"s3cret:with:colons-1234"
PEER = "192.168.1.20"


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def basic(credential: bytes) -> str:
    return "Basic " + base64.b64encode(credential).decode("ascii")


GOOD = basic(b"afx:" + PASSWORD)
BAD = basic(b"afx:wrong-password-xx")


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def authenticator(clock) -> auth.BasicAuthenticator:
    return auth.BasicAuthenticator(PASSWORD, clock=clock)


@pytest.fixture
def digests(monkeypatch):
    """compare_digest の引数と回数、SHA-256 の入力を記録する。"""
    compares: list[tuple[bytes, bytes]] = []
    hashed: list[bytes] = []
    real_compare, real_sha = auth.hmac.compare_digest, auth.hashlib.sha256

    def compare(a, b):
        compares.append((a, b))
        return real_compare(a, b)

    class Spy:
        def __getattr__(self, name):
            return getattr(auth.hashlib, name)

        @staticmethod
        def sha256(data=b""):
            hashed.append(bytes(data))
            return real_sha(data)

    monkeypatch.setattr(auth.hmac, "compare_digest", compare)
    monkeypatch.setattr(auth, "hashlib", Spy())
    return compares, hashed


def outcome(authenticator, value, peer=PEER) -> str:
    return authenticator.authenticate([] if value is None else [value], peer).outcome


# --- AC-14: username afx と正しい password だけ ---------------------------------------------

def test_only_the_fixed_username_with_the_right_password_passes(authenticator):
    assert outcome(authenticator, GOOD) == auth.OK
    assert outcome(authenticator, basic(b"root:" + PASSWORD)) == auth.UNAUTHORIZED
    assert outcome(authenticator, BAD) == auth.UNAUTHORIZED
    assert outcome(authenticator, basic(b"afx:" + PASSWORD + b"x")) == auth.UNAUTHORIZED
    assert outcome(authenticator, basic(b"AFX:" + PASSWORD)) == auth.UNAUTHORIZED


def test_scheme_is_case_insensitive(authenticator):
    assert outcome(authenticator, "basic " + GOOD[6:]) == auth.OK
    assert outcome(authenticator, "BASIC " + GOOD[6:]) == auth.OK


def test_a_colon_in_the_password_is_part_of_the_password(clock):
    for password in (b"a:b:c:d:e:f:g:h:i:j", b":" * 16, b"x" * 15 + b":"):
        a = auth.BasicAuthenticator(password, clock=clock)
        assert outcome(a, basic(b"afx:" + password)) == auth.OK
        assert outcome(a, basic(b"afx:" + password.split(b":")[0])) == auth.UNAUTHORIZED
        assert outcome(a, basic(b"afx:x:" + password)) == auth.UNAUTHORIZED


# --- AC-15: 失敗の形はすべて同じ 401 -------------------------------------------------------

def _failure_values() -> list:
    token = base64.b64encode(b"afx:" + PASSWORD).decode("ascii")
    urlsafe_source = b"afx:" + bytes([0xfb, 0xff, 0xfe]) * 10
    return [
        None,
        "",
        "Basic",
        "Basic ",
        "Bearer " + token,
        "Digest " + token,
        "Basic" + token,
        "Basic\t" + token,
        "Basic  " + token,
        "Basic " + token + " ",
        "Basic " + token + "\t",
        " Basic " + token,
        "Basic " + token[:-2],
        "Basic " + token + "=",
        "Basic " + token + "==",
        "Basic " + "!" + token[1:],
        "Basic " + base64.urlsafe_b64encode(urlsafe_source).decode("ascii"),
        "Basic " + base64.b64encode(b"afxonly").decode("ascii"),
        basic(b"x" * 133),
        basic(b"afx:" + b"p" * 128 + b"x"),
        "Basic " + "A" * 177,
        "Basic é",
    ]


@pytest.mark.parametrize("value", _failure_values())
def test_every_malformed_form_is_the_same_unauthorized(authenticator, value):
    decision = authenticator.authenticate([] if value is None else [value], PEER)
    assert decision == auth.Decision(auth.UNAUTHORIZED)


def test_a_non_canonical_pad_bit_is_rejected_but_the_canonical_token_passes(clock):
    password = b"q" * 16
    a = auth.BasicAuthenticator(password, clock=clock)
    canonical = base64.b64encode(b"afx:" + password).decode("ascii")
    assert outcome(a, "Basic " + canonical) == auth.OK
    # 末尾の pad bit だけが異なる token は同じ byte 列に decode されるが、再 encode が一致しない
    token = canonical
    assert token.endswith("=") and not token.endswith("==")
    index = len(token) - 2
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
    altered = token[:index] + alphabet[alphabet.index(token[index]) ^ 1] + token[index + 1:]
    assert base64.b64decode(altered, validate=True) == b"afx:" + password
    assert outcome(a, "Basic " + altered) == auth.UNAUTHORIZED


def test_two_authorization_values_fail_even_when_both_are_valid(authenticator):
    assert authenticator.authenticate([GOOD, GOOD], PEER).outcome == auth.UNAUTHORIZED


def test_the_decoded_credential_limit_is_132_bytes(clock):
    long_password = b"p" * 128
    a = auth.BasicAuthenticator(long_password, clock=clock)
    assert len(b"afx:" + long_password) == 132
    assert outcome(a, basic(b"afx:" + long_password)) == auth.OK
    assert outcome(a, basic(b"afx:" + long_password + b"p")) == auth.UNAUTHORIZED


def test_failures_do_not_change_the_log_line_or_the_counter_by_form(authenticator, capsys):
    outcomes = {authenticator.authenticate([] if v is None else [v], f"10.0.0.{i}").outcome
                for i, v in enumerate(_failure_values())}
    assert outcomes == {auth.UNAUTHORIZED}
    lines = capsys.readouterr().err.splitlines()
    assert set(lines) == {"afx-ops: web_auth_failed"}
    # Authorization が無い形だけは失敗に数えない
    assert authenticator.web_auth_failed_total == len(_failure_values()) - 1


# --- AC-16 / AC-34: 比較は 32 byte 対 32 byte を 1 回 --------------------------------------

@pytest.mark.parametrize("value", [GOOD, BAD, "Basic !!", "Bearer x", basic(b"x" * 200)])
def test_each_attempt_compares_two_32_byte_digests_exactly_once(authenticator, digests, value):
    compares, _hashed = digests
    authenticator.authenticate([] if value is None else [value], PEER)
    assert len(compares) == 1
    assert [len(part) for part in compares[0]] == [32, 32]


def test_the_digest_inputs_are_the_whole_credential_with_the_username(authenticator, digests):
    _compares, hashed = digests
    authenticator.authenticate([GOOD], PEER)
    assert hashed.count(b"afx:" + PASSWORD) == 2


def test_the_password_is_never_compared_in_its_variable_length_form(authenticator, digests):
    compares, _ = digests
    for value in (GOOD, BAD, basic(b"afx:" + b"z" * 100)):
        authenticator.authenticate([value], PEER)
    assert all(len(a) == len(b) == 32 for a, b in compares)


def test_a_rate_limited_attempt_does_no_comparison(authenticator, digests):
    compares, hashed = digests
    for _ in range(auth.SOURCE_LIMIT):
        authenticator.authenticate([BAD], PEER)
    compares.clear()
    hashed.clear()
    assert authenticator.authenticate([GOOD], PEER).outcome == auth.LIMITED
    assert compares == [] and hashed == []


def test_a_success_still_compares_once(authenticator, digests):
    compares, _ = digests
    assert authenticator.authenticate([GOOD], PEER).outcome == auth.OK
    assert len(compares) == 1


# --- AC-18: 送信元 5 回 / 60 秒 --------------------------------------------------------------

def test_the_sixth_failure_from_one_source_is_limited_and_not_recorded(authenticator, clock):
    results = [authenticator.authenticate([BAD], PEER).outcome for _ in range(6)]
    assert results == [auth.UNAUTHORIZED] * 5 + [auth.LIMITED]
    assert len(authenticator._sources[PEER]) == 5
    assert len(authenticator._global) == 5
    assert authenticator.authenticate([BAD], PEER).outcome == auth.LIMITED
    assert len(authenticator._sources[PEER]) == 5


def test_fourth_fifth_and_sixth_attempts_are_401_401_429(authenticator):
    results = [authenticator.authenticate([BAD], PEER).outcome for _ in range(6)]
    assert results[3:] == [auth.UNAUTHORIZED, auth.UNAUTHORIZED, auth.LIMITED]


def test_a_success_does_not_clear_the_failure_history(authenticator):
    for _ in range(4):
        authenticator.authenticate([BAD], PEER)
    assert authenticator.authenticate([GOOD], PEER).outcome == auth.OK
    assert authenticator.authenticate([BAD], PEER).outcome == auth.UNAUTHORIZED
    assert authenticator.authenticate([GOOD], PEER).outcome == auth.LIMITED


def test_a_correct_password_is_refused_while_limited(authenticator):
    for _ in range(5):
        authenticator.authenticate([BAD], PEER)
    assert authenticator.authenticate([GOOD], PEER).outcome == auth.LIMITED
    assert authenticator.authenticate([GOOD], "10.1.1.1").outcome == auth.OK


@pytest.mark.parametrize(("elapsed", "limited"), [(59.999, True), (60.000, False), (60.001, False)])
def test_the_oldest_failure_leaves_the_window_at_exactly_60_seconds(authenticator, clock, elapsed, limited):
    for _ in range(5):
        authenticator.authenticate([BAD], PEER)
    clock.now += elapsed
    got = authenticator.authenticate([BAD], PEER).outcome
    assert (got == auth.LIMITED) is limited


def test_failures_expire_individually(authenticator, clock):
    authenticator.authenticate([BAD], PEER)
    clock.now += 30
    for _ in range(4):
        authenticator.authenticate([BAD], PEER)
    clock.now += 30.0
    assert authenticator.authenticate([BAD], PEER).outcome == auth.UNAUTHORIZED
    assert authenticator.authenticate([BAD], PEER).outcome == auth.LIMITED


# --- AC-19: 全体 100 回 / 60 秒 ----------------------------------------------------------------

def _distinct(index: int) -> str:
    return f"10.{index // 250}.{index % 250}.1"


def test_global_99_100_101_are_401_401_429(authenticator):
    results = [authenticator.authenticate([BAD], _distinct(i)).outcome for i in range(101)]
    assert results[98:] == [auth.UNAUTHORIZED, auth.UNAUTHORIZED, auth.LIMITED]
    assert len(authenticator._global) == 100


def test_the_global_window_is_shared_between_sources(authenticator):
    for i in range(100):
        authenticator.authenticate([BAD], _distinct(i))
    assert authenticator.authenticate([GOOD], "172.16.0.1").outcome == auth.LIMITED


class CountingTable(dict):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.lookups = 0
        self.writes = 0

    def get(self, *args):
        self.lookups += 1
        return super().get(*args)

    def __getitem__(self, key):
        self.lookups += 1
        return super().__getitem__(key)

    def __contains__(self, key):
        self.lookups += 1
        return super().__contains__(key)

    def setdefault(self, *args):
        self.writes += 1
        return super().setdefault(*args)

    def __setitem__(self, key, value):
        self.writes += 1
        super().__setitem__(key, value)


def test_a_global_429_neither_looks_up_nor_allocates_a_source_entry(authenticator):
    for i in range(100):
        authenticator.authenticate([BAD], _distinct(i))
    table = CountingTable(authenticator._sources)
    authenticator._sources = table
    for peer in ("10.200.0.1", _distinct(3)):
        assert authenticator.authenticate([BAD], peer).outcome == auth.LIMITED
    assert (table.lookups, table.writes) == (0, 0)
    assert len(table) == 100


# --- AC-20: Retry-After ----------------------------------------------------------------------

def test_the_source_retry_after_counts_from_its_oldest_failure(authenticator, clock):
    authenticator.authenticate([BAD], PEER)
    clock.now += 10
    for _ in range(4):
        authenticator.authenticate([BAD], PEER)
    clock.now += 0.5
    assert authenticator.authenticate([BAD], PEER) == auth.Decision(auth.LIMITED, 50)


@pytest.mark.parametrize(("elapsed", "expected"), [(0.0, 60), (0.001, 60), (59.0, 1), (59.5, 1),
                                                    (59.999, 1), (30.2, 30)])
def test_retry_after_is_a_rounded_up_integer_between_1_and_60(authenticator, clock, elapsed, expected):
    for _ in range(5):
        authenticator.authenticate([BAD], PEER)
    clock.now += elapsed
    assert authenticator.authenticate([BAD], PEER).retry_after == expected


def test_global_first_retry_after_ignores_the_source_window(authenticator, clock):
    """global の残り 1 秒、同じ送信元の残り 50 秒なら global だけから 1 を返す。"""
    authenticator.authenticate([BAD], "10.9.9.9")   # global の最古
    clock.now += 49.0
    for _ in range(5):
        authenticator.authenticate([BAD], PEER)       # 送信元 PEER の窓は 50 秒残る
    clock.now += 10.0                                 # global 最古は 59 秒経過 (残り 1 秒)
    for i in range(94):
        authenticator.authenticate([BAD], _distinct(i + 1))
    assert len(authenticator._global) == 100
    assert len(authenticator._sources[PEER]) == 5
    decision = authenticator.authenticate([BAD], PEER)
    assert decision == auth.Decision(auth.LIMITED, 1)
    other = authenticator.authenticate([BAD], "10.250.250.250")
    assert other == auth.Decision(auth.LIMITED, 1)


def test_a_source_retry_after_is_computed_from_the_source_when_global_has_room(authenticator, clock):
    for _ in range(5):
        authenticator.authenticate([BAD], PEER)
        clock.now += 1.0
    assert len(authenticator._global) == 5
    assert authenticator.authenticate([BAD], PEER) == auth.Decision(auth.LIMITED, 55)


# --- AC-21: prune と上限 -----------------------------------------------------------------------

def test_queue_lengths_and_the_table_stay_within_the_fixed_limits(authenticator, clock):
    for i in range(300):
        authenticator.authenticate([BAD], _distinct(i % 150))
        assert len(authenticator._global) <= auth.GLOBAL_LIMIT
        assert len(authenticator._sources) <= auth.GLOBAL_LIMIT
        assert all(len(q) <= auth.SOURCE_LIMIT for q in authenticator._sources.values())


def test_the_101st_source_is_never_allocated(authenticator):
    for i in range(100):
        authenticator.authenticate([BAD], _distinct(i))
    assert len(authenticator._sources) == 100
    authenticator.authenticate([BAD], "10.254.254.254")
    assert "10.254.254.254" not in authenticator._sources
    assert len(authenticator._sources) == 100


@pytest.mark.parametrize(("elapsed", "removed"), [(59.999, False), (60.0, True), (60.001, True)])
def test_expired_entries_are_collected_before_the_next_lookup(authenticator, clock, elapsed, removed):
    authenticator.authenticate([BAD], "10.0.0.1")
    authenticator.authenticate([BAD], "10.0.0.2")
    clock.now += elapsed
    authenticator.authenticate([GOOD], "10.0.0.3")
    assert ("10.0.0.1" not in authenticator._sources) is removed
    assert (len(authenticator._global) == 0) is removed


def test_a_full_global_window_frees_up_after_60_seconds(authenticator, clock):
    for i in range(100):
        authenticator.authenticate([BAD], _distinct(i))
    assert authenticator.authenticate([BAD], "10.254.0.1").outcome == auth.LIMITED
    clock.now += 60.0
    assert authenticator.authenticate([BAD], "10.254.0.1").outcome == auth.UNAUTHORIZED


def test_there_is_no_overflow_bucket(authenticator):
    for i in range(100):
        authenticator.authenticate([BAD], _distinct(i))
    assert set(authenticator._sources) == {_distinct(i) for i in range(100)}


def test_concurrent_attempts_never_exceed_the_source_limit(authenticator):
    results: list[str] = []
    lock = threading.Lock()

    def attempt() -> None:
        got = authenticator.authenticate([BAD], PEER).outcome
        with lock:
            results.append(got)

    threads = [threading.Thread(target=attempt) for _ in range(40)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert results.count(auth.UNAUTHORIZED) == 5
    assert results.count(auth.LIMITED) == 35


# --- 記録 ----------------------------------------------------------------------------------------

def test_requests_without_authorization_never_exhaust_the_window(authenticator):
    for _ in range(10):
        assert authenticator.authenticate([], PEER).outcome == auth.UNAUTHORIZED
    assert authenticator.web_auth_failed_total == 0 and authenticator._sources == {}
    assert authenticator.authenticate([GOOD], PEER).outcome == auth.OK


def test_requests_without_authorization_are_still_refused_while_limited(authenticator):
    for _ in range(auth.SOURCE_LIMIT):
        authenticator.authenticate([BAD], PEER)
    assert authenticator.authenticate([], PEER).outcome == auth.LIMITED


def test_a_journal_that_raises_does_not_hold_the_lock(clock):
    class Broken:
        def sampled(self, _event):
            raise OSError("log closed")

    a = auth.BasicAuthenticator(PASSWORD, clock=clock, journal=Broken())
    for _ in range(auth.SOURCE_LIMIT):
        try:
            a.authenticate([BAD], PEER)
        except OSError:
            pass
    with pytest.raises(OSError):
        a.authenticate([BAD], PEER)  # limited: journal raises after the lock is released
    assert a._lock.acquire(blocking=False)
    a._lock.release()


def test_counters_and_fixed_journal_lines_only(authenticator, capsys):
    for _ in range(7):
        authenticator.authenticate([BAD], PEER)
    err = capsys.readouterr().err
    assert err.splitlines() == ["afx-ops: web_auth_failed"] * 5 + ["afx-ops: web_auth_limited"] * 2
    assert (authenticator.web_auth_failed_total, authenticator.web_auth_limited_total) == (5, 2)


def test_each_journal_kind_stops_after_10_lines_but_counters_continue(clock, capsys):
    a = auth.BasicAuthenticator(PASSWORD, clock=clock)
    for i in range(30):
        a.authenticate([BAD], f"10.0.{i}.1")
    lines = capsys.readouterr().err.splitlines()
    assert lines == ["afx-ops: web_auth_failed"] * 10
    assert a.web_auth_failed_total == 30


class _HandshakeSeen(dict):
    """count を読んだ thread が、もう 1 本の thread にも読む機会を与えてから書く。

    lock が無ければ 2 本とも同じ count を読んで同じ枠を 2 回使う (決定的に再現)。
    lock があれば 2 本目は lock で待つので、1 本目の「相手の読み待ち」は時間切れになり、
    読み書きは直列になる。"""

    def __init__(self) -> None:
        super().__init__()
        self.reads = 0
        self.first_read = threading.Event()
        self.second_read = threading.Event()
        self._state_lock = threading.Lock()

    def get(self, key, default=None):  # type: ignore[override]
        value = super().get(key, default)  # 読んだ値を先に確定させる
        with self._state_lock:
            self.reads += 1
            order = self.reads
        if order == 1:
            self.first_read.set()
            self.second_read.wait(timeout=0.5)
        elif order == 2:
            self.second_read.set()
        return value


def test_two_racing_failures_reserve_distinct_journal_slots(clock, capsys):
    """同じ枠を 2 本の thread が同時に取ろうとしても、予約は直列化される。
    lock を外すと 2 本が count 0 を読んで両方書き、count は 1 で終わる (決定的)。"""
    journal = Journal(limit=1)
    seen = _HandshakeSeen()
    journal._seen = seen
    a = auth.BasicAuthenticator(PASSWORD, clock=clock, journal=journal)
    start = threading.Barrier(2)

    def attempt(i: int) -> None:
        start.wait()
        a.authenticate([BAD], f"10.1.{i}.1")

    threads = [threading.Thread(target=attempt, args=(i,)) for i in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
    assert not any(t.is_alive() for t in threads)
    assert seen["web_auth_failed"] == 2
    assert seen.reads == 2
    # 2 本目は 1 本目の予約が終わるまで読めなかった (読みの handshake が時間切れ)。
    assert seen.first_read.is_set() and seen.second_read.is_set()
    assert capsys.readouterr().err.splitlines() == ["afx-ops: web_auth_failed"]
    assert a.web_auth_failed_total == 2


def test_many_concurrent_failures_journal_exactly_the_sample_limit(clock, capsys):
    journal = Journal()
    a = auth.BasicAuthenticator(PASSWORD, clock=clock, journal=journal)
    start = threading.Barrier(48)

    def attempt(i: int) -> None:
        start.wait()
        a.authenticate([BAD], f"10.1.{i}.1")

    threads = [threading.Thread(target=attempt, args=(i,)) for i in range(48)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert capsys.readouterr().err.splitlines() == ["afx-ops: web_auth_failed"] * 10
    assert journal._seen == {"web_auth_failed": 48}
    assert a.web_auth_failed_total == 48


def test_nothing_secret_reaches_the_journal_or_repr(authenticator, capsys):
    secret_value = basic(b"afx:" + PASSWORD)
    authenticator.authenticate([BAD, secret_value], "10.77.77.77")
    authenticator.authenticate([secret_value], "10.77.77.77")
    out = capsys.readouterr()
    text = out.out + out.err + repr(authenticator)
    for canary in ("10.77.77.77", PASSWORD.decode(), "afx:", secret_value, BAD):
        assert canary not in text
    expected_digest = hashlib.sha256(b"afx:" + PASSWORD).hexdigest()
    assert expected_digest not in text
