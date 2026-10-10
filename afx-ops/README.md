# afx-ops

agentic-fx の UDS 操作 API だけを使う付属機能 (bot / web / notify) の独立 package。
本体と同じ repo に置くが、本体 (`agentic_fx`) を import せず、別の lockfile と venv で配布・試験する。

```
uv --directory afx-ops sync
uv --directory afx-ops run pytest -q
afx-ops <bot|web|notify>
```

- `afx_ops/gate/` : 機能ごとの plain gate module の置き場
- `systemd/` : 機能別 unit の雛形を置く場所
- `tests/fixtures/server_auth_oracle.json` : 本体 `tests/ops/fixtures/` の写し (本体側のテストが bytes 一致を見張る)
