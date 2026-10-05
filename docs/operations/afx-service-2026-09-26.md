# afx を systemd user unit で常駐させる (仮運用、2026-09-26)

端末を開いたままにしなくても afx が動き続けるようにする。対話シェル (`afx>`) は screen セッションの中で
生きているので、必要なときに attach してコマンドを打てる。恒久形は `[first-run-setup]` の対話ウィザードが
unit を設置する想定で、本手順はそれまでの仮運用。

## 構成

| 部品 | 役割 |
|---|---|
| `deploy/afx.service` | user unit。`WorkingDirectory` はリポジトリ root、`After=mt5-bridge.service` (Wants。bridge 不通は afx 側が degraded で扱う)、`Restart=on-failure`、停止猶予 630 秒 |
| `deploy/afx-start.sh` | `screen -DmS afx .venv/bin/python main.py`。フォークしないので screen が unit の main PID。同名セッションがあれば二重起動を拒否 |
| `deploy/afx-stop.sh` | セッションに `stop` を送り、終了を最長 600 秒待つ。過ぎたら非 0 で戻り、systemd が cgroup を止める |
| `logs/screen-afx.log` | screen の画面ログ (追記、ローテーション無し。溜まったら手で消す)。アプリのログは従来どおり `logs/agentic.log` |

前提: `loginctl show-user $USER -p Linger` が `Linger=yes` (確認済み)。`.venv` が `uv sync` 済み。

## 設置 (初回、ユーザー操作)

1. いま端末で動いている afx を止める: その端末で `stop`。
2. unit を置いて有効化:

```bash
mkdir -p ~/.config/systemd/user
cp ~/project/agentic-fx/deploy/afx.service ~/.config/systemd/user/afx.service
systemctl --user daemon-reload
systemctl --user enable --now afx.service
systemctl --user status afx.service --no-pager
```

3. 起動確認: `screen -r afx` で `afx>` に入り `status`。抜けるのは `Ctrl-a d` (detach)。**`exit` や `stop` を打つと afx が終了し、unit は `Restart=on-failure` なので正常終了扱いで再起動しない。** 再起動は `systemctl --user restart afx`。

## 日常操作

| したいこと | コマンド |
|---|---|
| シェルに入る / 抜ける | `screen -r afx` / `Ctrl-a d` |
| 停止 (graceful) | `systemctl --user stop afx` (内部で `stop` を送る) |
| 再起動 (コード更新の反映) | `systemctl --user restart afx` |
| 状態・ログ | `systemctl --user status afx`、`journalctl --user -u afx -n 50`、`tail -f logs/agentic.log` |
| 自動起動をやめる | `systemctl --user disable --now afx` |

## 注意

- screen セッション内は tty なので afx は対話モードで動く (`--daemon` ではない)。SIGTERM も受けるので、`systemctl stop` が `stop` コマンドで止められなかった場合も graceful 経路に入る。
- unit の `PATH` に nvm の bin を入れてある (claude / codex runner が CLI を探すため)。node のバージョンを変えたら unit の PATH も直す。
- `CLAUDE_CODE_OPENAI_CONTEXT_WINDOWS` など Claude Code 端末由来の環境変数は systemd 環境には入らないので、手動起動時に付けていた `env -u` は不要。
- 二重起動防止は screen セッション名だけで見ている。手動で `main.py` を別に起こすと DB を 2 プロセスが触るので、unit 運用中は手動起動しない。
