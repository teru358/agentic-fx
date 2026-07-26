"""起動シーケンス: init ウィザードと起動ガード — 設計書 §8。
本プランのスコープは基盤部分のみ (価格ソース接続確認はプラン 3、llama-swap 確認はプラン 5 で追加)。"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

from agentic_fx.activity import ActivityLog, Category
from agentic_fx.config import load_settings
from agentic_fx.core.contracts import Mode
from agentic_fx.logging_setup import setup_technical_logging
from agentic_fx.store.db import connect, init_db
from agentic_fx.store.state import StateStore


def _state_store(root: Path) -> StateStore:
    return StateStore(root / "data" / "state" / "app_state.json")


def ensure_initialized(root: Path) -> None:
    if not _state_store(root).load().initialized:
        print("初期化が完了していません。先に `uv run main.py init` を実行してください。",
              file=sys.stderr)
        raise SystemExit(2)


def run_init(root: Path) -> int:
    cfg_dir = root / "config"
    example = cfg_dir / "settings.yaml.example"
    target = cfg_dir / "settings.yaml"
    if not example.exists():
        print(f"settings.yaml.example が見つかりません: {example}", file=sys.stderr)
        return 1
    if not target.exists():
        shutil.copy(example, target)
        print(f"作成: {target} (必要に応じて編集してください)")
    settings = load_settings(target)  # 検証を兼ねる

    (root / "logs").mkdir(parents=True, exist_ok=True)
    setup_technical_logging(root / "logs", settings.logging.level)

    conn = connect(root / "data" / "agentic.db")
    init_db(conn)

    # learning への切替はモード遷移ガード (§3) を通る mode コマンドのみ。
    # init はガードの迂回路にしない: trading 中は mode/autopilot に触れない。
    store = _state_store(root)
    if store.load().mode is Mode.TRADING:
        state = store.update(initialized=True)
        print("警告: 現在 trading モードです。init は mode/autopilot を変更しません "
              "(切替は mode コマンドを使用)。")
    else:
        state = store.update(initialized=True, mode=Mode.LEARNING,
                             autopilot=False)

    ActivityLog(root / "logs" / "activity.log").write(
        Category.SYSTEM, "init_completed",
        f"pairs={settings.pairs} mode={state.mode.value} "
        f"autopilot={'on' if state.autopilot else 'off'}")
    print("初期化が完了しました。`uv run main.py` でサービスを起動できます。")
    return 0


def run_service(root: Path) -> int:
    ensure_initialized(root)
    # サービス本体 (scheduler / 対話シェル) はプラン 5 で実装する。
    print("起動ガードを通過しました。サービス本体は Phase 1 プラン 5 で実装されます。")
    return 0
