# afx-ops

agentic-fx の UDS 操作 API だけを使う付属機能 (bot / web / notify) の独立 package。
本体と同じ repo に置くが、本体 (`agentic_fx`) を import せず、別の lockfile と venv で配布・試験する。

```
uv --directory afx-ops sync
uv --directory afx-ops run pytest -q
afx-ops <bot|web|notify>
afx-ops probe <bot|web|notify>
```

機能の設定は `$XDG_CONFIG_HOME/agentic-fx/ops/<feature>/config/ops.toml` の 1 枚だけ:

```toml
instance_root = "/home/me/agentic-fx"      # 全機能。UDS と instance lock をここから導く
key_dir = "/home/me/.config/agentic-fx/ops-keys"  # 全機能。principal の鍵と .ready
port = 8765                                 # web のみ (1〜65535)
guild_id = 123456789012345678               # bot のみ
channel_id = 223456789012345678             # bot のみ
# 以下は任意 (無ければ既定値)
trade_notices = "all"        # bot のみ: all | results | fills_closes | off。取引通知の種類
trade_amounts = "absolute"   # bot のみ: absolute | relative。relative は数量と損益の絶対額を出さない
deliver_trade_events = false # notify のみ: 取引 event を webhook に配送するか (既定 false)
```

- UDS は `<instance_root>/data/run/api.sock`、lock は `<instance_root>/data/instance.lock` に固定
  (本体の `ops.socket_path` を既定から変えた instance は対象外)。
- secret は `.../ops/<feature>/secret/` (0700) の `discord.token` (bot) / `webhook.url` (notify)、0600。
- API 鍵は起動時に隔離前に `.ready` の state と digest を照合して読み、隔離後は鍵 dir に触れない。
  `READY=1` は server 認証が通った後に送る。
- `afx-ops probe <feature>` は enable 前の疎通確認。隔離せず、鍵 dir を読まず、何も作らない。
  戻りは終了コード (0 成功 / 2 引数 / 69 未実装 / 70 表の値を呼べない / 71 設定・secret /
  73 確認の失敗) と stderr の `afx-ops: <reason>` 1 行だけ。

- `afx_ops/gate/` : 機能ごとの plain gate module の置き場
- 機能別 unit の雛形は本体 `agentic_fx.ops_setup` の package data (`afx ops enable` が設置する)
- `tests/fixtures/server_auth_oracle.json` : 本体 `tests/ops/fixtures/` の写し (本体側のテストが bytes 一致を見張る)
