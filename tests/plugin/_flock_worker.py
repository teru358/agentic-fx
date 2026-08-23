"""multi-process flock test の子プロセスエントリ (テストヘルパ、src/ には
置かない — pytest 収集対象外)。sys.argv: [db_path, plugins_root, role,
approval_id, barrier_file]

B-13 是正: 旧稿は approve が「barrier 書込 → sleep(0.3) → approve_candidate 呼出」
の順で、sleep 中は flock をまだ取っていなかった。reject は barrier を見た
瞬間に reject_candidate を呼ぶため、sleep の 0.3 秒の間に reject が先に
flock を取って先に決定してしまい (競合の向きが逆)、期待 (reject が
AlreadyDecidedError になる) と逆の結果になっていた。是正: sleep ベースの
順序仮定をやめ、reject 側が `plugins/.locks/<name>.lock` への
`LOCK_EX|LOCK_NB` 試行が失敗すること (= approve が実際に flock を握った
こと) を実測してから reject_candidate (本物のブロッキング flock) を呼ぶ。
"""
import fcntl
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from agentic_fx.config import load_settings
from agentic_fx.plugin import switch
from agentic_fx.store import db as db_store

NOW = datetime(2026, 8, 20, 3, 0, tzinfo=timezone.utc)


def main() -> None:
    db_path, plugins_root, role, approval_id, barrier_file = sys.argv[1:6]
    plugins_root = Path(plugins_root)
    settings = load_settings(Path(__file__).resolve().parents[2] / "config" / "settings.yaml.example")
    conn = db_store.connect(Path(db_path))
    approval_id = int(approval_id)

    if role == "approve":
        # B-1 是正: argv[2] で受け取っていた plugins_root を実際に使う
        # (旧稿は一度も使っていなかった症状の解消)
        switch.approve_candidate(conn, approval_id, decided_by="p1", now=NOW,
                                 plugins_root=plugins_root, settings=settings)
        Path(barrier_file).write_text("approve_done")
    elif role == "reject":
        # B-13 是正: approve が実際に plugin flock を握るまで non-blocking
        # trylock のポーリングで待つ (barrier ファイルのタイミングではなく
        # flock の実状態で同期する)
        lock_path = plugins_root / ".locks" / "sma.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            probe = open(lock_path, "a+")
            try:
                fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(probe, fcntl.LOCK_UN)
            except BlockingIOError:
                probe.close()
                break  # approve が flock を握った — reject を投入してよい
            probe.close()
            time.sleep(0.01)
        try:
            switch.reject_candidate(conn, approval_id, decided_by="p2",
                                    reason="race_test", now=NOW,
                                    plugins_root=plugins_root)
        except Exception as exc:  # AlreadyDecidedError 等
            Path(barrier_file + ".reject_result").write_text(type(exc).__name__)
        else:
            Path(barrier_file + ".reject_result").write_text("ok")


if __name__ == "__main__":
    main()
