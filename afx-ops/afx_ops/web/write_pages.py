"""書込みの form と確認・結果の画面。値はすべて escape し、token は hidden field にだけ出す。"""
from __future__ import annotations

from afx_ops.web import render
from afx_ops.web.write import (
    APPLIED, APPROVE, CANCEL, DONE, IMPROVE, NOT_EXECUTED, REJECT, UNKNOWN, UNUSED_TTL,
    Intent, Snapshot, escape,
)

_ACTION_LABELS = {APPROVE: "承認", REJECT: "却下", IMPROVE: "改善を実行", CANCEL: "job の取消"}
_NOT_EXECUTED_TEXT = {
    "digest_changed": "承認の内容が確認した後に変わったため、却下していません",
    "verify_failed": "承認の内容を取り直せなかったため、却下していません",
}


def _page(title: str, body: str) -> bytes:
    """書込みの画面は自動で読み直さない (入力中の値を消さない)。"""
    return render._document(title, body, refresh=False)


def approval_forms(approval_id: int, digest: str) -> str:
    """承認詳細に足す「承認」「却下」の form。押しても確認画面が出るだけで、実行は 2 回目。"""
    hidden = f'<input type="hidden" name="payload_sha256" value="{escape(digest)}">'
    base = f"/approvals/{int(approval_id)}"
    return ("<section><h2>決定</h2>"
            f'<form method="post" action="{base}/approve">{hidden}'
            '<button type="submit">承認</button></form>'
            f'<form method="post" action="{base}/reject">{hidden}'
            '<label>却下の理由 <input type="text" name="reason" maxlength="500"></label> '
            '<button type="submit">却下</button></form></section>')


def inject_forms(page: bytes, forms: str) -> bytes:
    """承認詳細の末尾に form を足し、自動の読み直しを外す。上限を超えるなら足さない。"""
    refresh = f'<meta http-equiv="refresh" content="{render.REFRESH_SECONDS}">'.encode()
    tail = b"</body></html>"
    if not page.endswith(tail) or refresh not in page:
        return page
    merged = page[:-len(tail)].replace(refresh, b"", 1) + forms.encode("utf-8") + tail
    return merged if len(merged) <= render.MAX_RESPONSE_BYTES else page


def improve_form_page() -> bytes:
    body = ("<section><form method=\"post\" action=\"/improve/runs\">"
            "<p><label>期間 (秒、省略可) <input type=\"text\" name=\"duration_sec\" "
            "inputmode=\"numeric\" maxlength=\"5\"></label></p>"
            "<p><label>回数 (省略可) <input type=\"text\" name=\"max_runs\" "
            "inputmode=\"numeric\" maxlength=\"2\"></label></p>"
            "<p><label>一時指示 (省略可) <input type=\"text\" name=\"comment\" "
            "maxlength=\"1000\"></label></p>"
            "<button type=\"submit\">確認へ進む</button></form></section>")
    return _page("改善を実行", body)


def _intent_rows(intent: Intent) -> str:
    rows = [("操作", _ACTION_LABELS[intent.action])]
    if intent.action == CANCEL:
        rows.append(("job", intent.job_id))
    elif intent.action in (APPROVE, REJECT):
        rows.append(("承認", f"#{intent.approval_id}"))
        rows.append(("payload_sha256", intent.payload_sha256[:12]))
        if intent.action == REJECT:
            rows.append(("理由", intent.reason or "(なし)"))
    else:
        rows.append(("期間 (秒)", "既定" if intent.duration_sec is None else intent.duration_sec))
        rows.append(("回数", "既定" if intent.max_runs is None else intent.max_runs))
        rows.append(("一時指示", intent.comment or "(なし)"))
    return "<dl>" + "".join(f"<dt>{escape(label)}</dt><dd>{escape(value)}</dd>"
                            for label, value in rows) + "</dl>"


def confirm_page(intent: Intent, token: str) -> bytes:
    minutes = int(UNUSED_TTL // 60)
    body = ("<section>" + _intent_rows(intent)
            + f"<p>この内容でよければ実行します (有効 {minutes} 分、1 回だけ)。</p>"
            '<form method="post" action="/confirm">'
            f'<input type="hidden" name="token" value="{escape(token)}">'
            '<button type="submit">実行する</button></form></section>')
    return _page("確認", body)


def _outcome_text(snapshot: Snapshot) -> str:
    outcome = snapshot.outcome
    if snapshot.state == UNKNOWN or outcome is None:
        return "daemon の応答を受け取れず、結果が分かりません。"
    if outcome.kind == APPLIED:
        job = (f' (job <a href="/jobs/{escape(outcome.job_id)}">{escape(outcome.job_id)}</a>)'
               if outcome.job_id else "")
        return "受け付けました" + job + "。"
    if outcome.kind == NOT_EXECUTED:
        text = _NOT_EXECUTED_TEXT.get(outcome.code or "")
        return text or "実行していません。"
    status = "" if outcome.status is None else f" status={escape(outcome.status)}"
    code = "" if outcome.code is None else f" code={escape(outcome.code)}"
    return "daemon が受け付けませんでした" + status + code + "。"


def result_page(snapshot: Snapshot, token: str) -> bytes:
    """結果の画面。結果不明のときだけ、確かめる form を (token 付きで) 出す。"""
    body = "<section>" + _intent_rows(snapshot.intent) + "<p>" + _outcome_text(snapshot) + "</p>"
    if snapshot.state == UNKNOWN:
        if snapshot.intent.action == CANCEL:
            body += (f'<p><a href="/jobs/{escape(snapshot.intent.job_id)}">'
                     "job の状態で結果を確かめてください</a></p>")
        elif snapshot.intent.action == IMPROVE:
            body += ('<form method="post" action="/confirm/check">'
                     f'<input type="hidden" name="token" value="{escape(token)}">'
                     '<button type="submit">結果を確かめる</button></form>')
        else:
            body += (f'<p><a href="/approvals/{int(snapshot.intent.approval_id)}">'
                     "承認の状態で結果を確かめてください</a></p>")
    elif snapshot.intent.action == CANCEL:
        body += f'<p><a href="/jobs/{escape(snapshot.intent.job_id)}">job の状態へ</a></p>'
    elif snapshot.intent.approval_id is not None:
        body += f'<p><a href="/approvals/{int(snapshot.intent.approval_id)}">承認の詳細へ</a></p>'
    return _page("結果", body + "</section>")


def result_status(snapshot: Snapshot) -> int:
    """結果の画面の HTTP status。受け付けた 200、受け付けなかった・実行しなかった 409、結果不明 502。"""
    if snapshot.state == UNKNOWN:
        return 502
    if snapshot.state == DONE and snapshot.outcome is not None and snapshot.outcome.kind == APPLIED:
        return 200
    return 409


_JOB_STATES = {"queued": "待機中", "running": "実行中", "done": "完了", "failed": "失敗",
               "cancelled": "取消済み", "shutdown": "停止"}
_JOB_KINDS = {"improve": "改善", "ask": "質問"}
# (status, daemon の code) ごとの固定文。daemon は持ち主の不一致と権限の不足をどちらも
# 403 forbidden で返すので、この組は原因を決めつけない。表に無い組は汎用の文にする。
_JOB_REFUSALS = {(403, "forbidden"): "daemon がこの job の閲覧を許可しませんでした "
                                     "(この画面の主体の job でないか、権限が足りません)",
                 (404, "not_found"): "job が見つかりません"}
_JOB_REFUSED_GENERIC = "daemon がこの job を見せませんでした"
_JOB_REFUSED_STATUSES = frozenset({403, 404})


def job_page(job_id: str, data: dict) -> bytes | None:
    """job の画面。daemon の語は固定の表に載るものだけを出し、載らなければ None (502 にする)。"""
    state = data.get("state")
    kind = data.get("kind")
    if data.get("id") != job_id or type(state) is not str or state not in _JOB_STATES:
        return None
    result = data.get("result")
    stop_requested = type(result) is dict and result.get("stop_requested") is True
    rows = [("job", job_id), ("種類", _JOB_KINDS.get(kind, "その他") if type(kind) is str
                              else "その他"),
            ("状態", _JOB_STATES[state]),
            ("停止の要求", "あり (走行中の分は完走します)" if stop_requested else "なし")]
    body = ("<section><dl>" + "".join(f"<dt>{escape(label)}</dt><dd>{escape(value)}</dd>"
                                      for label, value in rows) + "</dl>")
    if _can_cancel(state, result):
        body += (f'<form method="post" action="/jobs/{escape(job_id)}/cancel">'
                 '<button type="submit">取消へ進む</button></form>')
    return _page("job", body + "</section>")


def _can_cancel(state: str, result: object) -> bool:
    """daemon が取り消せる job か。待機中は取り消せる。実行中は、繰り返しの job (結果に
    停止の要求の真偽値がある) で、まだ停止を要求していないものだけ。1 回だけの実行中の job は取り消せない。"""
    if state == "queued":
        return True
    if state != "running" or type(result) is not dict:
        return False
    return result.get("stop_requested") is False


def job_refused_page(reply: object) -> bytes:
    text = _JOB_REFUSALS.get((getattr(reply, "status", None), getattr(reply, "code", None)),
                             _JOB_REFUSED_GENERIC)
    return _page("job", f"<section><p>{escape(text)}。</p></section>")


def job_refused_status(reply: object) -> int | None:
    """daemon が job を拒否した応答 (403 / 404) ならその status。それ以外は None。"""
    status = getattr(reply, "status", None)
    if type(status) is int and status in _JOB_REFUSED_STATUSES:
        return status
    return None
