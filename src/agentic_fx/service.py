"""起動シーケンス: init ウィザードと起動ガード — 設計書 §8。
本プランのスコープは基盤部分のみ (価格ソース接続確認はプラン 3、llama-swap 確認はプラン 5 で追加)。"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

from agentic_fx.activity import ActivityLog, Category
from agentic_fx.config import load_settings
from agentic_fx.core.contracts import Mode, SystemClock
from agentic_fx.datafeed._safe_error import safe_error_text
from agentic_fx.datafeed.health import DataUnhealthy
from agentic_fx.datafeed.news_collector import seed_default_sources
from agentic_fx.datafeed.price_provider import PriceProvider
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

    clock = SystemClock()
    seeded = seed_default_sources(conn, clock.now())
    if seeded:
        print(f"基本ニュースソースを {seeded} 件登録しました")

    # 価格ソースの接続確認。**DataUnhealthy は警告に留めて init は成功させる**
    # (オフライン環境でも初期化を完了できるようにする。データ不健全時に取引を
    # 止める本防御線はサービス起動後の fail closed — 設計書 §5)。
    # ただし DataUnhealthy 以外は握り潰さない: 設定ミスや実装バグまで警告に
    # 落とすと init が「常に成功する」だけのコマンドになり、確認の意味が
    # 無くなる。この確認は state 更新より前に置いてあるので、想定外の例外で
    # 落ちた場合は未初期化のまま残り、起動ガードが引き続き止める。
    try:
        source = PriceProvider(conn, settings, clock).healthcheck(
            settings.pairs[0])   # pairs は空を config が弾く (min_length=1)
    except DataUnhealthy as e:
        # safe_error_text を再度通す (多層防御)。init の標準出力は人が見て
        # コピペする場所で、技術ログより秘密が漏れたときの帰結が重い
        print(f"警告: 価格ソースに接続できません ({safe_error_text(e)})。"
              "サービス起動後は fail closed で保護されます。")
    else:
        # 確認したペアを明示する。config は複数ペアを許すが healthcheck は
        # 先頭 1 ペアしか見ないため、無限定の「OK」は 2 ペア目以降が壊れて
        # いても OK に見える (レビュー指摘 Minor-3)。
        print(f"価格ソース OK ({settings.pairs[0]}, source={source})")

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
