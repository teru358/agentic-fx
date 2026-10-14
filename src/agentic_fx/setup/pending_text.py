"""Fixed wording and parameter whitelists for pending setup steps (stdlib only)."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import re
from typing import Any, Mapping

# code -> {param name: (kind, fallback)}. Shared by the server serializer,
# the client and the renderer so the three cannot drift apart.
STEP_PARAMS: Mapping[str, Mapping[str, tuple[str, object]]] = {
    "setup_record_missing": {"state": ("ident", "unknown")},
    "llm_unverified": {"target": ("ident", "unknown"), "reason_code": ("ident", "unknown")},
    "env_key_missing": {"key": ("env", "required_key")},
    "indicators_missing": {"installed": ("int", 0), "total": ("int", 0)},
    "history_missing": {"symbol": ("symbol", "pair")},
    "approval_stalled": {"approval_id": ("int", 0)},
    "ops_incomplete": {"feature": ("ident", "feature"), "stage": ("ident", "unknown"),
                       "reason_code": ("ident", "unknown")},
    "ops_not_ready": {"feature": ("ident", "feature")},
    "isolation_not_admitted": {"reason_code": ("ident", "unknown")},
}


_PATTERNS = {
    "ident": re.compile(r"[a-z0-9_.-]{1,64}"),
    "env": re.compile(r"[A-Z][A-Z0-9_]{0,63}"),
    "symbol": re.compile(r"[A-Z0-9_.-]{1,64}"),
}
_INT_MAX = 2**31 - 1


_CLI_INSTALL_REASONS = frozenset({"bin_not_found", "credentials_missing"})
CLI_DOC_SECTION = "docs/operations/first-run-acceptance.md の『前提: CLI の導入』"


def is_ident(value: object) -> bool:
    return isinstance(value, str) and _PATTERNS["ident"].fullmatch(value) is not None


def sanitize_param(kind: str, value: object, fallback: object) -> object:
    """Return value when it satisfies the kind, otherwise fallback (bool is never an int)."""
    if kind == "int":
        if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= _INT_MAX:
            return value
        return fallback
    pattern = _PATTERNS.get(kind)
    if pattern is not None and isinstance(value, str) and pattern.fullmatch(value):
        return value
    return fallback


def parse_observed_at(value: object) -> datetime | None:
    """Parse an ISO-8601 instant into an aware UTC datetime, or None when unusable."""
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


@dataclass(frozen=True, slots=True)
class PendingStep:
    code: str
    severity: str
    observed_at: datetime
    params: Mapping[str, Any] = field(default_factory=dict)
    visible_in_status: bool = True


def render_pending_step(step: PendingStep) -> str:
    """Render only fixed language and whitelisted parameters from a step."""
    p = step.params
    def ident(value: object, fallback: str) -> str:
        return sanitize_param("ident", value, fallback)  # type: ignore[return-value]

    def env_key(value: object) -> str:
        return sanitize_param("env", value, "required_key")  # type: ignore[return-value]

    def number(value: object) -> int:
        return sanitize_param("int", value, 0)  # type: ignore[return-value]

    messages = {
        "setup_record_missing": "確認記録が読めません。`afx init` を再実行してください" if p.get("state") == "corrupt" else "初期設定の確認記録がありません。`afx init` を再実行してください",
        "opencode_unverified": "`afx improve verify-backend --backend opencode` を実行してください",
        "bridge_not_connected": "MT5 bridge はまだつながっていません。接続後に `afx init` を再実行してください",
        "indicators_missing": f"標準指標 {number(p.get('total', 9))} 本中 {number(p.get('installed', 0))} 本が未導入です。`afx init` を再実行してください",
        "no_strategy": "戦略がまだありません。改善の承認を確認してください",
        "history_source_unconfigured": "履歴源が設定されていません。MT5 bridge 接続後に履歴源を設定してください",
        "landlock_unavailable": "この環境では改善ループと plugin の採用ができません",
        "yfinance_weekend_gap": "yfinance は週明けにしばらく停止します",
        "linger_off": "ログアウトすると afx も止まります。`loginctl enable-linger $USER` を実行してください",
        "start_pending": "サービスはまだ起動していません。`systemctl --user start afx`",
    }
    if step.code == "llm_unverified":
        reason = ident(p.get("reason_code"), "unverified")
        head = f"{ident(p.get('target'), 'LLM')} の LLM は未確認です ({reason})。"
        if reason in _CLI_INSTALL_REASONS:
            return f"{head}{CLI_DOC_SECTION} を見て `afx init` を再実行してください"
        return f"{head}`afx init` を再実行して確認してください"
    if step.code == "env_key_missing":
        return f".env に {env_key(p.get('key'))} がありません"
    if step.code == "history_missing":
        symbol = p.get("symbol")
        safe_symbol = sanitize_param("symbol", symbol, "pair")
        return f"戦略の採用審査に使う履歴がありません ({safe_symbol})"
    if step.code == "approval_stalled":
        return f"承認 #{number(p.get('approval_id'))} が途中で止まっています。`afx ctl --as approver approval retry {number(p.get('approval_id'))}`"
    if step.code == "ops_incomplete":
        feature = ident(p.get("feature"), "feature")
        return f"{feature} の有効化が途中です ({ident(p.get('reason_code'), 'unknown')})。`afx ops enable {feature}` を再実行してください"
    if step.code == "ops_not_ready":
        feature = ident(p.get("feature"), "feature")
        return f"{feature} は有効化済みですが ready を確認できていません。`afx ops status {feature}`"
    if step.code == "isolation_not_admitted":
        return f"改善の CLI backend は隔離の適合検査を通っていません ({ident(p.get('reason_code'), 'unknown')})"
    if step.code == "api_disabled":
        return "操作 API が無効です。settings.yaml の api.enabled を true にして再起動してください"
    if step.code == "api_start_failed":
        return f"操作 API の起動確認に失敗しました ({ident(p.get('reason_code'), 'unknown')})"
    return messages.get(step.code, f"未知の手順 (code={step.code})。afx を更新してください")
