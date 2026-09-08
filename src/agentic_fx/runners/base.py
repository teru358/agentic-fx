"""AgentRunner 抽象 — 設計書 §4 の Mission/MissionResult をそのまま固定。"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Literal


@dataclass
class Mission:
    """Agent mission specification (設計書 §4).

    Attributes:
        prompt: Task description for the agent.
        tools: Names of tools available to the agent.
        output_schema: JSON schema for expected output (dict[str, Any]).
        max_turns: Maximum number of turns. Runner-neutral definition:
            1 turn = 1 LLM request + response pair.
            Tool execution within that request/response is part of the same turn.
            JSON repair and schema re-output retries each consume a new turn
            (each retry is a new LLM request). Each retry type has a 2-retry limit;
            if exhausted, return status="failed". Retries count toward max_turns.
            timeout_sec always takes priority and may force termination before max_turns.

            Phase 2 ClaudeRunner (claude-agent-sdk) does not have native turn limits.
            It must count turns by this definition, force-terminate when max_turns
            is exceeded, and return status="max_turns" to notify the executor.
        timeout_sec: Wall-clock timeout in seconds. Always takes priority over max_turns.
    """

    prompt: str
    tools: list[str]
    output_schema: dict[str, Any]
    max_turns: int
    timeout_sec: float


#: ツール予算 abort の reason prefix (runner が構築し、worker / improve_loop が判定)。
TOOL_BUDGET_ABORT_PREFIX = "tool_budget_abort:"


def is_tool_budget_abort(reason: str | None) -> bool:
    return bool(reason) and reason.startswith(TOOL_BUDGET_ABORT_PREFIX)


@dataclass
class MissionResult:
    status: Literal["completed", "failed", "timeout", "max_turns"]
    output: dict[str, Any] | None
    transcript: list[dict[str, Any]] = field(default_factory=list)
    # runner が診断できた失敗理由 (安全化済み・単一行・上限内)。既定 None —
    # 既存呼び出しは無変更 (設計書 2026-08-10-context-overflow-diagnosis-
    # design.md §4.2)。「なぜ失敗したか」の軸であり、`status` (「どの
    # 終端状態か」の軸) とは独立 — 新しい status 値は作らない (同 §4.1)。
    reason: str | None = None
    # M4 (2026-08-30、codex レビュー Major 4): timeout/no-output からの
    # session resume 追撃 (`CliRunner._recover_output`) で回収した出力
    # かどうかの provenance。既定 False — 既存呼び出しは無変更。
    # improve_loop はこれを見て report artifact を observation へ降格する
    # (plugin artifact は既存の決定論 gate が防衛線のため対象外)。
    recovered: bool = False


class AgentRunner(ABC):
    """Mission 実行を抽象化する runner インターフェース。

    **reason の規範** (設計書 docs/superpowers/specs/2026-08-10-context-
    overflow-diagnosis-design.md §4.3): runner は `failed`/`timeout`/
    `max_turns` を返すとき、可能な限り安全化済みの `reason` を設定する。
    外部応答の本文を生で入れない — activity ログ・Discord 通知・worker
    の result frame をそのまま経由しうるため、秘密や長大なペイロードを
    漏らしてはならない。

    ツール予算による abort は新しい status を増やさず、`status="failed"`
    かつ `reason` が `TOOL_BUDGET_ABORT_PREFIX` (`tool_budget_abort:`) で
    始まる形で返す。abort 後の追撃 (session resume) で最終出力が回収できた
    場合は `status="completed"`, `recovered=True` のまま **同じ reason を
    保持する** (provenance を落とさない — /code-review 2 周目 #2)。
    判定は `is_tool_budget_abort(reason)` を使い、文字列を直書きしない。

    **現在の適用範囲**: 本規範を満たすのは `LocalRunner` (HTTP failure
    から解釈できた reason — プラン 9 Task 2) のみ。`WorkerRunner` が
    親側で生成する失敗 (起動 timeout・protocol error・EOF) と
    `ClaudeRunner` (未実装、プラン 10 スコープ) の理由付けはこの規範の
    対象外 — 予測実装しない (同 §4.3)。プラン 10 で `ClaudeRunner` を
    実装する task は、この docstring の規範を満たす契約テストを
    ブロッキングチェックリストに含めること。
    """

    @abstractmethod
    def run(self, mission: Mission) -> MissionResult: ...
