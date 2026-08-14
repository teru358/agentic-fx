from datetime import datetime, timezone
from unittest.mock import patch

import httpx
import pytest
import yaml

from agentic_fx.datafeed.health import DataUnhealthy
from agentic_fx.datafeed.news_collector import DEFAULT_SOURCES
from agentic_fx.entry import main as entry_main
from agentic_fx.service import ensure_initialized, run_init
from agentic_fx.store import news_sources
from agentic_fx.store.db import connect
from agentic_fx.store.state import StateStore


def _example(root):
    (root / "config").mkdir(parents=True)
    src = open("config/settings.yaml.example", encoding="utf-8").read()
    (root / "config" / "settings.yaml.example").write_text(src)


def _settings(root, **datafeed):
    """example を元に settings.yaml を先に置く (init は既存を上書きしない)。

    価格ソースの構成をテスト毎に変えるために使う。
    """
    raw = yaml.safe_load(
        (root / "config" / "settings.yaml.example").read_text(encoding="utf-8"))
    for key, value in datafeed.items():
        raw["datafeed"][key] = value
    (root / "config" / "settings.yaml").write_text(
        yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")


@pytest.fixture(autouse=True)
def mock_price_check():
    """既定で価格ソース確認をモックする — テストは外部アクセスしない (§9)。

    init は実ネットワークを叩く healthcheck を含むため、モックしないと
    既存テスト (init_creates_everything 等) が本物の yfinance を呼ぶ。
    本物の PriceProvider を通したいテストは `real_price_provider` を要求する。
    """
    with patch("agentic_fx.service.PriceProvider") as mock:
        mock.return_value.healthcheck.return_value = "yfinance"
        yield mock


@pytest.fixture(autouse=True)
def mock_llama_swap_check():
    """既定で llama-swap 接続確認をモックする (上書き 2)。

    `_check_llama_swap` 自体を patch する — mock 漏れで timeout=120 の
    実待ちが混入するのを防ぐ。挙動そのものを検証するテストは
    tests/test_service_app.py 側に置く。
    """
    with patch("agentic_fx.service._check_llama_swap") as mock:
        yield mock


@pytest.fixture
def real_price_provider(mock_price_check):
    """autouse のモックの上に本物のクラスを被せる (opt-in)。"""
    from agentic_fx.datafeed.price_provider import PriceProvider
    with patch("agentic_fx.service.PriceProvider", PriceProvider) as real:
        yield real


def test_guard_blocks_before_init(tmp_path):
    _example(tmp_path)
    with pytest.raises(SystemExit) as e:
        ensure_initialized(tmp_path)
    assert e.value.code == 2


def test_init_creates_everything(tmp_path, mock_llama_swap_check):
    _example(tmp_path)
    assert run_init(tmp_path) == 0
    assert (tmp_path / "config" / "settings.yaml").exists()
    assert (tmp_path / "data" / "agentic.db").exists()
    conn = connect(tmp_path / "data" / "agentic.db")
    tables = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "orders" in tables
    state = StateStore(tmp_path / "data" / "state" / "app_state.json").load()
    assert state.initialized is True
    assert state.mode.value == "learning"
    act = (tmp_path / "logs" / "activity.log").read_text(encoding="utf-8")
    assert "init_completed" in act
    ensure_initialized(tmp_path)  # ガード通過 (例外なし)
    # W4: run_init が _check_llama_swap を実際に呼んでいることの配線 assert
    # (呼び出し行を削除しても全テスト緑という実測ギャップを閉じる。
    # ensure_initialized は _check_llama_swap を経由しないので call_count は
    # run_init の 1 回分のみ)
    mock_llama_swap_check.assert_called_once()


def test_init_is_idempotent(tmp_path):
    _example(tmp_path)
    assert run_init(tmp_path) == 0
    marker = tmp_path / "config" / "settings.yaml"
    marker.write_text(marker.read_text() + "\n# user edit\n")
    assert run_init(tmp_path) == 0
    assert "# user edit" in marker.read_text()  # 既存 settings.yaml を上書きしない


def test_reinit_in_trading_keeps_mode(tmp_path):
    # init をモード遷移ガード (§3) の迂回路にしない: trading 中は mode/autopilot 不変
    from agentic_fx.core.contracts import Mode
    _example(tmp_path)
    run_init(tmp_path)
    store = StateStore(tmp_path / "data" / "state" / "app_state.json")
    store.update(mode=Mode.TRADING, autopilot=True)
    run_init(tmp_path)
    s = store.load()
    assert s.mode is Mode.TRADING
    assert s.autopilot is True
    assert s.initialized is True


def test_entry_init_subcommand(tmp_path, monkeypatch):
    _example(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert entry_main(["init"]) == 0


def test_entry_default_requires_init(tmp_path, monkeypatch):
    _example(tmp_path)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as e:
        entry_main([])
    assert e.value.code == 2


def test_init_without_example_returns_1(tmp_path, capsys):
    """example が無いときの失敗経路 (戻り値 1) を固定する。

    変異テストで、この `return 1` を `return 0` にしても全テストが通って
    しまう (未ピン) ことが判明したため追加。
    """
    (tmp_path / "config").mkdir(parents=True)
    assert run_init(tmp_path) == 1
    assert "settings.yaml.example" in capsys.readouterr().err
    with pytest.raises(SystemExit):     # 初期化済みにはならない
        ensure_initialized(tmp_path)


# ---- Task 9: 基本ニュースソース投入 + 価格ソース接続確認 --------------------


def test_init_seeds_news_sources_and_checks_price(tmp_path, capsys,
                                                  mock_price_check):
    _example(tmp_path)
    assert run_init(tmp_path) == 0
    conn = connect(tmp_path / "data" / "agentic.db")
    # 件数リテラルに結合しない (ソース選定は後日見直される)。list_enabled で
    # 数えることで「全件 enabled で入る」ことも同時に固定する
    assert len(news_sources.list_enabled(conn)) == len(DEFAULT_SOURCES)
    out = capsys.readouterr().out
    assert "価格ソース OK (USDJPY, source=yfinance)" in out
    # 件数リテラルを書かない。tmp_path に数字が混ざるので部分一致では
    # 弱すぎる (print を消しても通ってしまう) — 文言ごと突き合わせる
    assert f"基本ニュースソースを {len(DEFAULT_SOURCES)} 件登録しました" in out
    # 確認するのは設定の先頭ペア (プラン 5 の fail closed と同じ対象)
    mock_price_check.return_value.healthcheck.assert_called_once_with("USDJPY")


def test_init_price_check_constructor_args(tmp_path, mock_price_check):
    """`run_init` が PriceProvider を**正しい接続・設定・時計で構築している**
    ことを固定する。

    裏取り実測 (codex 指摘 1): 直上の
    `healthcheck.assert_called_once_with("USDJPY")` は「呼んでいるか」しか
    見ないため、構築引数の誤配線は全 1892 テスト緑のまま生存する
    (実測: `conn` を `sqlite3.connect(":memory:")` に差し替える変異、
    `clock` を `FixedClock(1970)` に差し替える変異が共に生存)。
    E2E (`tests/datafeed/test_cache_fallback_e2e.py`) は `run_init` の
    PriceProvider を MagicMock で置換するため、構造的にここを観測できない。
    既存の `_check_llama_swap` 配線ピン (91-94 行) の欠けた兄弟にあたる。
    """
    from agentic_fx.core.contracts import SystemClock
    _example(tmp_path)
    assert run_init(tmp_path) == 0
    conn_arg, settings_arg, clock_arg = mock_price_check.call_args.args
    files = [row[2] for row in conn_arg.execute("PRAGMA database_list")]
    assert str(tmp_path / "data" / "agentic.db") in files
    assert settings_arg.pairs == ["USDJPY"]
    assert isinstance(clock_arg, SystemClock)


def test_init_seeds_with_a_real_utc_clock(tmp_path, mock_price_check):
    """`run_init` が **`SystemClock` を実際に使っている**ことを固定する。

    レビュー指摘 (I-1 / Minor-5): `SystemClock` 側のテストは
    `tests/core/test_contracts.py` にあるが、**入口がそれを使っていること**には
    ピンが 1 本も無かった。実測で 2 つの改変が全テスト緑のまま生存している:

    ①`clock = SystemClock()` を `FixedClock(2020-01-01Z)` に差し替える →
      止まった時計は `validate_quote` の鮮度検証を必ず落とすので、init が
      **オンラインでも常に「接続できません」を出す**。exit 0 のままなので誰も気づかない
    ②`seed_default_sources(conn, clock.now().replace(tzinfo=None))` で naive を渡す →
      `store/news_sources.add` は `Rag.cleanup_news` と違って tz 検証を持たず
      素で `now.isoformat()` を書く。**Task 9 がこの無防備な書き込み経路の
      初の production 呼び出し元**

    `created_at` の tz と実時刻近傍性を見ることで両方を同時に閉じる。
    """
    _example(tmp_path)
    before = datetime.now(timezone.utc)
    assert run_init(tmp_path) == 0
    after = datetime.now(timezone.utc)
    conn = connect(tmp_path / "data" / "agentic.db")
    created = datetime.fromisoformat(news_sources.list_all(conn)[0]["created_at"])
    assert created.tzinfo is not None          # naive を書かない (②)
    assert before <= created <= after          # 実時計である (①)


def test_init_seeding_is_idempotent(tmp_path, capsys):
    _example(tmp_path)
    run_init(tmp_path)
    capsys.readouterr()
    assert run_init(tmp_path) == 0
    conn = connect(tmp_path / "data" / "agentic.db")
    assert len(news_sources.list_all(conn)) == len(DEFAULT_SOURCES)
    # 2 回目は「登録しました」を出さない (0 件を報告しない)
    assert "ニュースソース" not in capsys.readouterr().out


def test_init_survives_price_check_failure(tmp_path, capsys, mock_price_check):
    """オフライン等で価格ソースが全滅しても init は完了する (警告のみ)。"""
    _example(tmp_path)
    mock_price_check.return_value.healthcheck.side_effect = \
        DataUnhealthy("all down")
    assert run_init(tmp_path) == 0
    out = capsys.readouterr().out
    assert "警告" in out and "all down" in out
    # 警告で終わっても初期化自体は完了していること (次回起動でガードに落ちない)
    state = StateStore(tmp_path / "data" / "state" / "app_state.json").load()
    assert state.initialized is True
    ensure_initialized(tmp_path)


def test_init_completes_offline_with_unreachable_bridge(tmp_path, capsys,
                                                        real_price_provider):
    """**本物の** PriceProvider で、実際に到達不能なソースを叩いて完了すること。

    モックではなく閉じたローカルポートへ実接続する (外部ホストには触れない)。
    httpx.ConnectError がソース層から上がる経路を実際に通し、
    「オフラインでも init が完了する」を経路ごと確かめる。
    """
    _example(tmp_path)
    _settings(tmp_path, yfinance={"enabled": False},
              twelvedata={"enabled": False},
              mt5={"enabled": True, "bridge_url": "http://127.0.0.1:1"})
    assert run_init(tmp_path) == 0
    out = capsys.readouterr().out
    assert "警告" in out
    # ネットワーク層の例外が実際に通った証跡 (モックした風ではないこと)
    assert "mt5: ConnectError" in out
    ensure_initialized(tmp_path)


def test_init_completes_when_every_source_refuses(tmp_path, capsys,
                                                  monkeypatch,
                                                  real_price_provider):
    """3 ソースすべてが接続エラーでも完了する (yfinance 分岐も含めて網羅)。"""
    monkeypatch.setenv("TWELVEDATA_API_KEY", "k")
    _example(tmp_path)
    _settings(tmp_path, yfinance={"enabled": True},
              twelvedata={"enabled": True},
              mt5={"enabled": True, "bridge_url": "http://127.0.0.1:1"})
    err = httpx.ConnectError("[Errno -3] Temporary failure in name resolution")
    with patch("agentic_fx.datafeed.price_provider.sources.mt5_quote",
               side_effect=err), \
         patch("agentic_fx.datafeed.price_provider.sources.td_quote",
               side_effect=err), \
         patch("agentic_fx.datafeed.price_provider.sources.yf_quote",
               side_effect=OSError("network is unreachable")):
        assert run_init(tmp_path) == 0
    out = capsys.readouterr().out
    assert "警告" in out
    # 3 ソース全部の失敗が 1 つの DataUnhealthy に集約されて警告になる
    for name in ("mt5", "twelvedata", "yfinance"):
        assert name in out


def test_init_price_warning_does_not_leak_api_key(tmp_path, capsys, monkeypatch,
                                                  real_price_provider):
    """**標準出力**に API キーが出ないこと。

    init の出力は人が見てコピペする場所であり、技術ログより漏洩の帰結が重い。
    price_provider 側の抑止 (_safe_error) が init の print 経路まで届いて
    いることを、TD の 401 を実際に起こして確かめる。
    """
    monkeypatch.setenv("TWELVEDATA_API_KEY", "SECRET_KEY_123")
    _example(tmp_path)
    _settings(tmp_path, yfinance={"enabled": False},
              twelvedata={"enabled": True}, mt5={"enabled": False})
    url = ("https://api.twelvedata.com/quote"
           "?symbol=USD%2FJPY&apikey=SECRET_KEY_123")
    req = httpx.Request("GET", url)
    err = httpx.HTTPStatusError(
        f"Client error '401 Unauthorized' for url '{url}'",
        request=req, response=httpx.Response(401, request=req))
    with patch("agentic_fx.datafeed.price_provider.sources.td_quote",
               side_effect=err):
        assert run_init(tmp_path) == 0
    cap = capsys.readouterr()
    assert "SECRET_KEY_123" not in cap.out and "SECRET_KEY_123" not in cap.err
    assert "api.twelvedata.com" not in cap.out   # URL ごと出さない
    assert "401" in cap.out                      # 診断情報は残す
    # 技術ログ側も同じ (logging_setup は propagate=False なのでファイルを見る)
    log = (tmp_path / "logs" / "agentic.log").read_text(encoding="utf-8")
    assert "SECRET_KEY_123" not in log


def test_init_warning_redacts_secrets_in_the_exception_itself(tmp_path, capsys,
                                                              mock_price_check):
    """多層防御: DataUnhealthy のメッセージ自体に秘密が載っていても伏字にする。

    現状 price_provider 側で抑止済みだが、そこに依存すると抑止層が 1 枚に
    なる (将来のソース追加や、_safe_error を通さない DataUnhealthy 送出で
    無音の穴が開く)。init の print 側でも通していることを固定する。
    """
    _example(tmp_path)
    mock_price_check.return_value.healthcheck.side_effect = DataUnhealthy(
        "boom while calling https://api.twelvedata.com/quote"
        "?apikey=SECRET_KEY_123")
    assert run_init(tmp_path) == 0
    out = capsys.readouterr().out
    assert "SECRET_KEY_123" not in out
    assert "boom" in out                # 診断情報は落とさない


def test_init_does_not_swallow_unexpected_errors(tmp_path, mock_price_check):
    """DataUnhealthy 以外は握り潰さない (init を「常に成功する」コマンドにしない)。

    実装バグや設定ミスまで警告に落とすと init の意味が無くなる。
    かつ healthcheck は state 更新の前に走るので、落ちた場合は
    **未初期化のまま**残り、起動ガードが引き続き止める。
    """
    _example(tmp_path)
    mock_price_check.return_value.healthcheck.side_effect = \
        TypeError("bug in provider")
    with pytest.raises(TypeError):
        run_init(tmp_path)
    with pytest.raises(SystemExit) as e:
        ensure_initialized(tmp_path)
    assert e.value.code == 2
