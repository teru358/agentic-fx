from __future__ import annotations

from dataclasses import dataclass

from afx_ops.api_client import Decision, Reply
from afx_ops.gate.reconcile import MessageMetadata


class FakeBotClient:
    def __init__(self, detail: Reply | Exception, decision: Decision | Exception | None = None):
        self.detail = detail
        self.decision = Decision(Reply(200, {}, None), None) if decision is None else decision
        self.decide_calls: list[tuple[str, int, str]] = []
        self.poll_calls = 0
        self.poll_reply: Reply | Exception = Reply(200, {"state": "done"}, None)
        self.poll_replies: list[Reply | Exception] = []

    def approval(self, approval_id: int) -> Reply:
        if isinstance(self.detail, Exception):
            raise self.detail
        return self.detail

    def approve(self, actor, approval_id: int, *, payload_sha256: str) -> Decision:
        self.decide_calls.append(("approve", approval_id, actor.value))
        return self._decision()

    def reject(self, actor, approval_id: int, *, reason=None) -> Decision:
        self.decide_calls.append(("reject", approval_id, actor.value))
        assert reason is None
        return self._decision()

    def retry(self, actor, approval_id: int, *, payload_sha256: str) -> Decision:
        self.decide_calls.append(("retry", approval_id, actor.value))
        return self._decision()

    def _decision(self) -> Decision:
        if isinstance(self.decision, Exception):
            raise self.decision
        return self.decision

    def poll(self, job) -> Reply:
        self.poll_calls += 1
        reply = self.poll_replies.pop(0) if self.poll_replies else self.poll_reply
        if isinstance(reply, Exception):
            raise reply
        return reply


@dataclass
class FakeDiscordStore:
    messages: list[MessageMetadata]
    operations: list[str]

    def apply(self, effects) -> None:
        self.operations.extend(effect.kind for effect in effects)
