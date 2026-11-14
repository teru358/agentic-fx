"""bot の進捗の監視: gateway・配送・poll 周回のそれぞれが 10 分止まっていないか。

時刻は全部 monotonic clock (wall clock の後退・調整に影響されない)。状態はここに集め、判定は
`expired` が理由 (固定語) を返すだけで、終了は呼ぶ側が行う。
"""
from __future__ import annotations

import time
from collections.abc import Callable

# この秒数だけ連続して進捗が無ければ一時の終了にする
WINDOW_SECONDS = 600.0

# 畳んだ件数の上限 (出す数値を有界にする)
REPEAT_CAP = 9999

GATEWAY_STARTING = "starting"
GATEWAY_HEALTHY = "healthy"
GATEWAY_UNHEALTHY = "unhealthy"


class ProgressTracker:
    """gateway / 配送 / poller の進捗。単一の event loop thread だけが更新する。"""

    def __init__(self, clock: Callable[[], float] = time.monotonic,
                 window: float = WINDOW_SECONDS,
                 emit: Callable[[str], None] | None = None) -> None:
        self._clock = clock
        self.window = window
        # gateway の遷移を固定語で出す先。discord の logger は閉じてあるので、journal に残る
        # gateway の診断はこれだけ。不健全の間の disconnect の反復は件数に畳む
        self._emit = emit
        self.gateway_repeats = 0
        started = clock()
        self.started_at = started
        # gateway: process 開始から starting。login 中は窓に含め、起動 reconcile の間は止め、
        # connect を始めた時点から新たに数える
        self.gateway_state = GATEWAY_STARTING
        self.gateway_unhealthy_since: float | None = started
        self.last_ready_at: float | None = None
        self.last_resumed_at: float | None = None
        self.last_disconnect_at: float | None = None
        # 配送: 先頭 event の最初の send の直前に立て、send と cursor 保存の両方が済んだときだけ消す
        self.delivery_stalled_since: float | None = None
        self.delivery_event_id: int | None = None
        self.last_delivery_success_at: float | None = None
        # poller: login 後の起動 reconcile の開始 (最初の窓) から周回の開始・完了。起動 reconcile も
        # 周回も、始めてから 10 分戻らなければ止まっている。login 前は gateway の窓が数える
        self.poll_started_at: float | None = None
        self.last_poll_completed_at: float | None = None

    # ------------------------------------------------------------------ gateway

    def _say(self, text: str) -> None:
        if self._emit is not None:
            try:
                self._emit(text)
            except Exception:  # noqa: BLE001 — 出力の失敗で進捗の判定を止めない
                pass

    def _gateway_healthy(self) -> None:
        before, repeats = self.gateway_state, self.gateway_repeats
        self.gateway_repeats = 0
        if before == GATEWAY_UNHEALTHY:
            self._say(f"gateway_recovered repeats={repeats}")
        elif before == GATEWAY_STARTING and repeats > 0:
            self._say(f"gateway_started repeats={repeats}")

    def gateway_connected(self) -> None:
        """READY (`on_connect`)。健全にして連続の不健全の起点を消す。"""
        self._gateway_healthy()
        self.gateway_state = GATEWAY_HEALTHY
        self.gateway_unhealthy_since = None
        self.last_ready_at = self._clock()

    def gateway_resumed(self) -> None:
        """RESUMED (`on_resumed`)。READY と同じく健全に戻す。"""
        self._gateway_healthy()
        self.gateway_state = GATEWAY_HEALTHY
        self.gateway_unhealthy_since = None
        self.last_resumed_at = self._clock()

    def gateway_pause(self) -> None:
        """login が済み、起動 reconcile を始める。reconcile の間は gateway の窓を止める。"""
        if self.gateway_state != GATEWAY_HEALTHY:
            self.gateway_unhealthy_since = None

    def gateway_begin(self) -> None:
        """`connect()` を始める直前。ここから gateway の 10 分の窓を新たに数える。"""
        if self.gateway_state != GATEWAY_HEALTHY and self.gateway_unhealthy_since is None:
            self.gateway_unhealthy_since = self._clock()

    def gateway_disconnected(self) -> None:
        """`on_disconnect`。既に不健全 (starting を含む) なら起点を動かさない。"""
        now = self._clock()
        self.last_disconnect_at = now
        if self.gateway_state == GATEWAY_HEALTHY:
            self.gateway_state = GATEWAY_UNHEALTHY
            self.gateway_unhealthy_since = now
            self._say("gateway_unhealthy")
        else:
            self.gateway_repeats = min(self.gateway_repeats + 1, REPEAT_CAP)

    # ------------------------------------------------------------------ 配送

    def delivery_begin(self, event_id: int) -> None:
        """先頭 event の最初の send を始める直前。既に立っていれば起点を動かさない。"""
        if self.delivery_stalled_since is None:
            self.delivery_stalled_since = self._clock()
            self.delivery_event_id = event_id

    def delivery_committed(self, event_id: int, *, delivered: bool = True) -> None:
        """cursor の保存まで済んだ。追っている event 以後まで進んだときだけ消す。"""
        stalled = self.delivery_stalled_since is not None
        if stalled and self.delivery_event_id is not None and event_id < self.delivery_event_id:
            return
        self.delivery_stalled_since = None
        self.delivery_event_id = None
        if delivered:
            self.last_delivery_success_at = self._clock()

    # ------------------------------------------------------------------ poller

    def startup_begin(self) -> None:
        """login が済み、起動 reconcile を始める直前。最初の poll の窓の起点。"""
        self.poll_started_at = self._clock()

    def poll_begin(self) -> None:
        self.poll_started_at = self._clock()

    def poll_end(self) -> None:
        self.last_poll_completed_at = self._clock()
        self.poll_started_at = None

    # ------------------------------------------------------------------ 判定

    def expired(self) -> str | None:
        """期限を過ぎた経路の理由 (固定語)。無ければ None。ちょうど窓の長さで期限切れ。"""
        now = self._clock()
        since = self.gateway_unhealthy_since
        if self.gateway_state != GATEWAY_HEALTHY and since is not None \
                and now - since >= self.window:
            return "gateway_unhealthy"
        stalled = self.delivery_stalled_since
        if stalled is not None and now - stalled >= self.window:
            return "delivery_stalled"
        # 起動 reconcile の開始、又は一周の開始から、完了しないまま窓を過ぎた
        begin = self.poll_started_at
        if begin is not None and now - begin >= self.window:
            return "poller_stalled"
        return None
