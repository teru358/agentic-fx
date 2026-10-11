"""`afx-ops notify` の業務処理: event cursor を短い間隔で読み、webhook へ 1 件ずつ送る。

起動順 (隔離・自己検査・server 認証・READY=1) は `startup.run` が済ませてから呼ばれる。
入力は `Isolated` が持つものだけで、鍵・secret dir・設定を読み直さない (隔離後は読めない)。

確定の順序: 送信が 2xx を返した後にだけ、cursor の前進を snapshot の
1 回の置換で確定する。2xx の後・置換の前に落ちたら、再起動後に同じ event_id を再送し得る
(at-least-once。受け手は event_id で識別する)。cursor 以下の event は送らない。

失敗は全部、固定 reason を stderr に 1 行出して非零終了し、systemd の restart に委ねる。
cursor は進めず、次の event も送らない。URL・応答本文・例外文は出さない。

恒久の失敗 (4xx・redirect・payload_invalid 等) の event を黙って飛ばさないのが設計 (fail closed)。
同じ event で落ち続けると unit の再起動回数の上限 (StartLimitBurst) で failed になり、
operator が `afx ops status` で気づく。それを待たずに先へ進む手段は置かない。
"""
from __future__ import annotations

import os
import sys
import time
from collections.abc import Callable
from typing import TextIO

from afx_ops.api_client import RC_UNREACHABLE, ApiError, NotifyClient
from afx_ops.isolation.startup import client_for
from afx_ops.notify import transport
from afx_ops.notify.delivery import DeliveryError, encode_json_object, validate_webhook_url
from afx_ops.notify.payload import PayloadError, encode_event
from afx_ops.notify.retry import RetryError, WarningThrottle, retry
from afx_ops.notify.state import StateError, advance_delivery, load, observe_high, save

SECRET_NAME = "webhook.url"
EVENTS_LIMIT = 500
# 満たない page の後の待ち。長い待受はしない (server は短い polling だけを受ける)
POLL_INTERVAL = 2.0

RC_OK = 0
RC_NOTIFY_FAILED = 1

REASONS = ("url_invalid", "notify_state_invalid", "events_invalid", "high_watermark_regressed",
           "payload_invalid", "retry_exhausted", "redirect", "http_4xx", "response_invalid",
           "transport_failed", "notify_internal_error")


class _Failure(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def run(isolated, *, client=None, send: Callable[..., None] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        stop: Callable[[int], bool] | None = None, stderr: TextIO | None = None) -> int:
    """配送 loop。production は `isolated` だけで呼ばれ、失敗するまで戻らない。

    client / send / sleep / monotonic / stop / stderr は試験だけが渡す。stop は取得 1 回ごとに
    その回に送った件数で呼ばれ、真なら rc 0 で戻る。
    """
    out = sys.stderr if stderr is None else stderr
    try:
        _Loop(isolated, client=client, send=transport.post if send is None else send,
              sleep=sleep, monotonic=monotonic, out=out).run(stop)
    except _Failure as failure:
        _say(out, failure.reason)
        return RC_NOTIFY_FAILED
    except ApiError as exc:
        _say(out, exc.reason)
        return RC_UNREACHABLE
    except Exception:  # 例外文には URL や secret が入り得るので、固定語だけにして traceback を出さない
        _say(out, "notify_internal_error")
        return RC_NOTIFY_FAILED
    return RC_OK


def _say(out: TextIO, text: str) -> None:
    try:
        print(f"afx-ops: {text}", file=out, flush=True)
    except (OSError, ValueError):
        pass


class _Loop:
    def __init__(self, isolated, *, client, send, sleep, monotonic, out) -> None:
        self._isolated = isolated
        self._client = client
        self._send = send
        self._sleep = sleep
        self._monotonic = monotonic
        self._out = out
        # 同じ原因の warning は event をまたいで間引く
        self._throttle = WarningThrottle()

    def run(self, stop: Callable[[int], bool] | None) -> None:
        try:
            target = validate_webhook_url(self._isolated.secrets[SECRET_NAME])
        except (DeliveryError, KeyError, TypeError):
            raise _Failure("url_invalid") from None
        client = self._client
        if client is None:
            client = client_for(self._isolated, NotifyClient)
        dir_fd = self._open_state_dir()
        try:
            try:
                state = load(dir_fd)
            except StateError:
                raise _Failure("notify_state_invalid") from None
            while True:
                state, fetched, delivered = self._poll_once(client, target, dir_fd, state)
                if stop is not None and stop(delivered):
                    return
                # 続きを急いで読むのは、満杯の page から実際に配送できたときだけ。進捗のない
                # 満杯 page (API の異常) で tight loop にならないよう待つ
                if not (fetched >= EVENTS_LIMIT and delivered > 0):
                    self._sleep(POLL_INTERVAL)
        finally:
            os.close(dir_fd)

    def _open_state_dir(self) -> int:
        try:
            return os.open(self._isolated.state_dir,
                           os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        except OSError:
            raise _Failure("notify_state_invalid") from None

    def _poll_once(self, client, target, dir_fd, state):
        reply = client.events(after=state.cursor, limit=EVENTS_LIMIT)
        events, high, unreadable = _page(reply)
        saved_high = state.high_watermark
        # 保存済みの最大 watermark (cursor 以上) より後退した値は DB の巻き戻り等の異常。何も確定しない
        if high < state.high_watermark:
            raise _Failure("high_watermark_regressed")
        # 新しい watermark は最初の配送確定と同じ置換で保存する (失敗時に snapshot を動かさない)
        state = observe_high(state, high)
        delivered = 0
        for event in events:
            if event["id"] <= state.cursor:
                # 配送済み (cursor 以下)。送らない
                continue
            try:
                body = encode_json_object(encode_event(event))
            except (PayloadError, DeliveryError):
                raise _Failure("payload_invalid") from None
            self._deliver(target, body)
            state = self._save(dir_fd, advance_delivery(state, event["id"]))
            delivered += 1
        if unreadable:
            # id が読めない event より前の正常分は配送済み。ここで止まり、その event は飛ばさない
            raise _Failure("payload_invalid")
        if not delivered and state.high_watermark != saved_high:
            state = self._save(dir_fd, state)
        return state, len(events), delivered

    @staticmethod
    def _save(dir_fd, state):
        try:
            save(dir_fd, state)
        except StateError:
            raise _Failure("notify_state_invalid") from None
        return state

    def _deliver(self, target, body: bytes) -> None:
        def attempt() -> None:
            try:
                self._send(target, body)
            except DeliveryError as exc:
                raise RetryError(exc.reason, retryable=exc.retryable,
                                 retry_after=exc.retry_after) from None

        try:
            retry(attempt, sleep=self._sleep, warning=self._warn, monotonic=self._monotonic,
                  throttle=self._throttle)
        except RetryError as exc:
            raise _Failure(exc.reason) from None

    def _warn(self, reason: str) -> None:
        _say(self._out, f"warning {reason}")


def _page(reply) -> tuple[list[dict], int, bool]:
    """events 応答の形を確かめ、(id 順の event、high watermark、id が読めない event があったか) を返す。

    id が読めない event に page の中で最初に当たったところで打ち切る (それ以降は並べ替えの根拠が
    ない)。その前の event は送れるので捨てない。
    """
    data = getattr(reply, "data", None)
    if (getattr(reply, "status", None) != 200 or getattr(reply, "code", "x") is not None
            or type(data) is not dict):
        raise _Failure("events_invalid")
    events, high = data.get("events"), data.get("high_watermark")
    if type(events) is not list or type(high) is not int or high < 0:
        raise _Failure("events_invalid")
    readable = []
    for event in events:
        # 形の検査は送信直前の encode_event が行う。並べ替えに要る id だけ先に確かめる
        if type(event) is not dict or type(event.get("id")) is not int or event["id"] <= 0:
            return sorted(readable, key=lambda item: item["id"]), high, True
        readable.append(event)
    return sorted(readable, key=lambda item: item["id"]), high, False
