"""afx-ops の描画関数を、afx-ops の venv の python で呼ぶ小さな実行体。

親 (agentic-fx 本体) の venv は `afx_ops` を持たないので、本体の試験は実 DB の
payload や応答 DTO を JSON で渡し、ここで描画した文字列を受け取る。

標準入力: `{"kind": "discord_finish", "event": {...}, "dto": {...}}` または
`{"kind": "web_approval", "data": {...}}`。標準出力: `{"text": "..."}`。
"""
from __future__ import annotations

import json
import sys


def main() -> int:
    request = json.load(sys.stdin)
    kind = request["kind"]
    if kind == "discord_finish":
        from afx_ops.gate.improve_render import (
            mission_id_of, render_improve_finish, validate_improve_run)
        event = request["event"]
        detail = validate_improve_run(request["dto"], mission_id_of(event))
        text = render_improve_finish(event, detail)
        out = {"text": text, "validated": detail is not None}
    elif kind == "web_approval":
        from afx_ops.web.render import render_approval
        out = {"text": render_approval(request["data"], None).decode("utf-8")}
    else:
        raise SystemExit(f"unknown kind {kind}")
    json.dump(out, sys.stdout, ensure_ascii=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
