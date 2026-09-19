"""[indicator-initial-set] repo 側の受入テスト (I2 / I5 / I6 / I7 / I8)。

**実 DB (`data/agentic.db`) と実 `plugins/` には一切触れない** — 全て
`tmp_path` 配下に作る ([[tests-touching-real-repo-resources]])。
I1 は `tests/plugin/test_loader.py::test_discover_sample_plugins_directory_not_rejected`
が担保する (本ファイルには無い)。状態遷移そのものは
`tests/plugin/test_switch_journal.py` / `test_reconcile.py` が pin 済みなので
**再実装しない** — I8 が観測するのは「runbook に書いた手順がそのまま通ること」だけ
(設計書 §6.1)。
"""
from __future__ import annotations

import functools
import hashlib
import importlib.util
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
import pandas as pd
import pytest
import yaml

from agentic_fx.activity import ActivityLog
from agentic_fx.commands import Commands
from agentic_fx.core.contracts import FixedClock
from agentic_fx.core.health_latch import HealthLatch
from agentic_fx.core.paper_broker import PaperBroker
from agentic_fx.core.plugin_contract import validate_indicator_result
from agentic_fx.entry import main
from agentic_fx.plugin import switch as plugin_switch
from agentic_fx.plugin.loader import discover, discover_one_with_reason
from agentic_fx.plugin.resolve import lock_config, resolve_indicator_deps
from agentic_fx.plugin.sandbox import PluginSession
from agentic_fx.store import db as db_store
from agentic_fx.store import plugin_switch_journal as journal_store
from agentic_fx.store.state import StateStore
from agentic_fx.tools import plugin_loader
from tests.fixtures.wiring_envs import SETTINGS_FIXTURE as SETTINGS

_REPO = Path(__file__).resolve().parents[2]
EXAMPLES = _REPO / "docs" / "examples" / "plugins"

#: 本束で足した 9 名。**総数は pin しない** — 将来 example が増えても壊れない
#: よう、常に名前で選ぶ (設計書 §6 I1 / r1 M1)。
NINE = ("sma", "ema", "rsi", "macd", "bollinger", "atr", "adx",
        "stochastic", "ichimoku")

NOW = datetime(2026, 9, 19, 3, 0, tzinfo=timezone.utc)

#: 0〜100 スケールの出力。残りは価格スケール (設計書 §6 I5)。
PCT_KEYS = frozenset({"rsi", "k", "d", "adx", "plus_di", "minus_di"})
PRICE_KEYS = frozenset({"value", "upper", "middle", "lower", "atr", "macd",
                        "signal", "hist", "tenkan", "kijun", "senkou_a",
                        "senkou_b", "chikou"})

#: I5 が観測する系列の完全な一覧 (`<plugin 名>.<出力キー>`)。**20 本ある** —
#: 出力キー名だけで束ねると `sma.value` と `ema.value` が衝突して
#: 片方 (dict の後勝ちで `sma`) が一度も観測されない。段 0 の実測では
#: `sma` の実装を `close.expanding(...).mean()` (先頭依存が最大の形) に
#: 差し替えても I5 の 3 テストが全て緑のまま通った。
DELTA_KEYS = frozenset(
    {f"{name}.{key}"
     for name, keys in (("sma", ("value",)), ("ema", ("value",)),
                        ("rsi", ("rsi",)),
                        ("macd", ("macd", "signal", "hist")),
                        ("bollinger", ("upper", "middle", "lower")),
                        ("atr", ("atr",)),
                        ("adx", ("adx", "plus_di", "minus_di")),
                        ("stochastic", ("k", "d")),
                        ("ichimoku", ("tenkan", "kijun", "senkou_a",
                                      "senkou_b", "chikou")))
     for key in keys})


def _tolerance(delta_key: str, base: float) -> float:
    """`<plugin 名>.<出力キー>` の出力キー側で値域を引く (設計書 §6 I5)。

    **どちらにも属さないキーは黙って価格スケール扱いにせず落とす**
    (/code-review #6)。`PRICE_KEYS` は定義だけされて誰も読んでいなかったので、
    新しい出力キーが足されたときに公差の分類漏れが `1e-9 * base` の
    フォールバックで静かに通ってしまう形だった。20 系列の全キーが 2 つの
    集合のどちらかに属することは `test_every_delta_key_is_classified` が
    独立に pin する。
    """
    key = delta_key.split(".", 1)[1]
    assert key in PCT_KEYS or key in PRICE_KEYS, delta_key
    return 1e-6 if key in PCT_KEYS else 1e-9 * base


def test_every_delta_key_is_classified():
    """I5 が観測する 20 系列の出力キーが、`PCT_KEYS` と `PRICE_KEYS` の
    **どちらか一方**に属すること (/code-review #6)。

    `_tolerance` の assert は「その呼び出しで使われたキー」しか見ないので、
    `DELTA_KEYS` 側だけを増やして分類を忘れた場合の観測点をここに置く。
    2 集合が交わらないことも同時に見る — 交わると `_tolerance` の分岐が
    `PCT_KEYS` 優先で静かに片方を選ぶ。
    """
    keys = {k.split(".", 1)[1] for k in DELTA_KEYS}
    assert not (PCT_KEYS & PRICE_KEYS), sorted(PCT_KEYS & PRICE_KEYS)
    assert keys <= (PCT_KEYS | PRICE_KEYS), sorted(keys - PCT_KEYS
                                                   - PRICE_KEYS)


# --- I5 の前提: 宣言 `max_bars` ---------------------------------------------

def test_all_nine_declare_max_bars_400(examples_copy):
    """9 本の `config.yaml` が `max_bars: 400` を宣言していること (設計書 §3.2 / D4)。

    **`_last_row_deltas` の結合だけでは足りない**ので独立に pin する。段 0 の実測:
    `ema` を `max_bars: 50` にすると結合した I5 が red になる
    (span=20 の EMA は 50 本では初期値の重みが `(19/21)**50` = 6.7e-03 残る) が、
    `sma` / `bollinger` / `stochastic` / `ichimoku` は窓が有限なので
    `tail(50)` と全長が最終行で厳密に一致し、**結合しても red にならない**。
    `test_nine_indicators_bless_in_sequence` も同じ値を見ているが、あちらは
    `slow` の bless 実走 (段 0 実測 41 秒) なのでここに 1 秒の観測点を置く。
    """
    for meta in _nine_metas(examples_copy):
        assert meta.max_bars == 400, (meta.name, meta.max_bars)

#: `__pycache__` / `.pytest_cache` は `check_candidate_snapshot` が無視する
#: (`gate_pytest.py:43,46-55`) ので**除外しない** — runbook の
#: `cp -r` がそのまま巻き込んでも通ることを I8(a) で観測する。

#: `examples_copy` の忠実性比較から外すディレクトリ名。`__pycache__` は
#: 過去の import が repo 側にだけ残していることがあり (`.gitignore` 済)、
#: `.pytest_cache` も同様に**コピー対象外**なので、比較の前に両側から落とす。
_COPY_IGNORED_DIRS = ("__pycache__", ".pytest_cache")


def _relative_file_hashes(root: Path) -> dict[str, str]:
    """`root` 配下の全ファイルを `{相対パス (posix): sha256}` で返す。

    `_COPY_IGNORED_DIRS` 配下は除外する。**ファイル名を列挙せず木を全走査
    する** — 「3 ファイルだけ一致」を見ると、将来 4 つ目のファイルを足した
    ときにコピー側に届いているかを誰も見なくなる。
    """
    out: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if any(part in _COPY_IGNORED_DIRS for part in path.parts):
            continue
        if not path.is_file():
            continue
        out[path.relative_to(root).as_posix()] = hashlib.sha256(
            path.read_bytes()).hexdigest()
    return out


@functools.lru_cache(maxsize=None)
def _nine_metas(root: Path) -> tuple:
    """`root` (= `examples_copy`) を discover して 9 名を名前で取り出す。

    **repo の `docs/examples/plugins` は読まない** (r2 codex C1)。metadata を
    repo から、`compute` をコピーから取ると**二重出所**になり、コピーが
    元と食い違っても (`ignore_patterns` を広げる / コピー元を変える)
    `co_filename` の「repo の外」判定だけでは何も落ちない。忠実性は
    `examples_copy` fixture が hash で観測し、出所の単一性はここで pin する。

    `lru_cache` は `discover` (ディレクトリ走査 + YAML 解析 + AST 検証 +
    ハッシュ) のモジュール中 ~46 回の再実行を 1 回に畳む (/code-review #1。
    指揮者の実測で非 slow 8 本が 1.13s -> 0.85s)。`root` は session スコープ
    fixture が返す不変の `Path`。**戻り値は tuple** — cache が同じ
    オブジェクトを配るので、呼び出し側が並べ替えられる list を渡さない。
    """
    metas = {m.name: m for m in discover(root) if m.name in NINE}
    assert sorted(metas) == sorted(NINE), sorted(metas)
    ordered = tuple(metas[n] for n in NINE)
    for meta in ordered:
        assert Path(meta.path).is_relative_to(root), (meta.name, meta.path)
        assert not Path(meta.path).is_relative_to(EXAMPLES), (meta.name,
                                                              meta.path)
    return ordered


def _load_module(name: str, plugin_py: Path):
    """`plugin.py` を **1 本ずつ別のモジュール名で** ロードする。

    9 本とも `plugin` という名前で `sys.modules` に入れると衝突し、
    最後の 1 本の実装を 9 回検査するだけの恒真テストになる。
    """
    spec = importlib.util.spec_from_file_location(f"_iis_{name}", plugin_py)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.modules.pop(spec.name, None)
    return mod


def _load_compute(name: str, plugin_py: Path):
    """`_load_module` の `compute` だけを返す薄い包み。"""
    return _load_module(name, plugin_py).compute


def _typed(mapping) -> dict:
    """`{キー: (型名, 値)}`。**型のドリフトを潰さない**ための包み —
    `2 == 2.0` なので素の dict 比較では `bollinger` の `num_std` が
    `2.0` から `2` に落ちても気付けない (指揮者の実測: 変異 J は
    出力比較だけの版では緑のまま通った)。"""
    return {k: (type(v).__name__, v) for k, v in mapping.items()}


@pytest.fixture(scope="session")
def examples_copy(tmp_path_factory) -> Path:
    """`docs/examples/plugins` の **コピー** (session に 1 回)。

    I2 / I5 は `plugin.py` を `exec_module` で実際に import する。素の
    importlib は **ソースと同じディレクトリに `__pycache__` を書く** ので、
    repo の `docs/examples/plugins/<名前>/` を直接 import すると
    `tmp_path` の外の実資源を書き換える経路になる
    ([[tests-touching-real-repo-resources]])。bytecode を抑止する設定に
    頼るのではなく、**書き込み先が repo の外になる構造**を採る。
    `__pycache__` / `.pytest_cache` は複製しない (I8 の `_stage` は逆に
    **除外しない** — あちらは `cp -r` が巻き込んでも bless が通ることを
    観測するのが目的)。

    **コピーの忠実性をここで観測する (r2 codex C1)**。9 本それぞれについて
    「`_COPY_IGNORED_DIRS` を落とした後の相対パス集合とファイルごとの
    sha256」が repo 側と一致することを assert する。これが無いと、
    `ignore_patterns` を広げる / コピー元を取り違える類の変更で、I2 / I5 が
    **配備されるものとは別の artifact** を静かに検査する形になる
    (`ignore_patterns` に `*.yaml` を足す変異は、この assert を入れる前は
    受入テスト 8 本すべて緑のまま通った — 指揮者の実測)。assert を
    **fixture 本体に置く**ので、これを使う全テストが同時に fail closed になる。
    """
    dest = tmp_path_factory.mktemp("examples") / "plugins"
    shutil.copytree(EXAMPLES, dest,
                    ignore=shutil.ignore_patterns(*_COPY_IGNORED_DIRS))
    for name in NINE:
        want = _relative_file_hashes(EXAMPLES / name)
        got = _relative_file_hashes(dest / name)
        assert want, name          # 空同士の一致で通らないこと
        assert got == want, (name, sorted(set(want) ^ set(got)),
                             sorted(k for k in set(want) & set(got)
                                    if want[k] != got[k]))
    return dest


def test_loaded_plugins_never_come_from_the_repo_examples_directory(
        examples_copy):
    """I2 / I5 が実行する `compute` が **repo の外**から来ていること。

    この pin が無いと `_load_compute` の引数を `EXAMPLES / meta.name /
    "plugin.py"` に戻すだけで repo へ bytecode を書く形に静かに戻る
    (逆変異で実測)。

    **r2 codex C1 以後、`meta.path` 自体もコピー側を指す** (`_nine_metas` が
    `examples_copy` を discover する) ので、ここは「コピーが repo の外に
    ある」ことの pin であり、**コピーが元と同じ中身であること**は
    `examples_copy` fixture の hash 比較が、**metadata と実装の出所が
    1 つであること**は `_nine_metas` の 2 つの assert が担当する。3 つで
    1 組。
    """
    for meta in _nine_metas(examples_copy):
        compute = _load_compute(meta.name,
                                examples_copy / meta.name / "plugin.py")
        assert not Path(compute.__code__.co_filename).is_relative_to(
            EXAMPLES), (meta.name, compute.__code__.co_filename)


def _df(n: int, *, base: float = 150.0, seed: int = 0) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    sigma = base * 0.002
    close = base + np.cumsum(rng.normal(0.0, sigma, n))
    return pd.DataFrame(
        {"open": np.concatenate([[close[0]], close[:-1]]),
         "high": close + sigma, "low": close - sigma, "close": close,
         "volume": np.ones(n)},
        index=pd.date_range("2024-01-01", periods=n, freq="1h", tz="UTC"))


# --- I2: validator を直接通す ------------------------------------------------

def test_all_nine_pass_the_indicator_validator(examples_copy):
    """I2: 9 本の戻り値が `validate_indicator_result` を通る
    (キー集合完全一致 / index 一致 / Inf 不在)。

    **`docs/examples/plugins/` 配下で pytest を回さないこと** —
    `.pytest_cache` は `.gitignore` に無く、`tests/conftest.py` の
    untracked ガードに当たる。import も repo では行わない
    (`examples_copy` の注記)。
    """
    df = _df(300)
    for meta in _nine_metas(examples_copy):
        compute = _load_compute(meta.name,
                                examples_copy / meta.name / "plugin.py")
        out = compute(df, dict(meta.params))
        validate_indicator_result(out, df_index=df.index,
                                  outputs=meta.outputs)


def test_declared_params_match_each_plugins_own_defaults(examples_copy):
    """`config.yaml` の `params` が `plugin.py` の既定値と一致していること。

    **どちらのテストも片側しか見ていなかった**: 9 本の自己テストは
    `compute(df, {})` (= `plugin.py` の既定値) だけを、I2 は
    `compute(df, dict(meta.params))` (= `config.yaml` の宣言値) だけを通す。
    段 0 の実測で `sma` の `period: 20 -> 5`、`bollinger` の
    `num_std: 2.0 -> 3.0` がどちらも全テストを素通りした (M39 / M40)。
    宣言値は**実際に本番で使われる値**であり、ずれると「自己テストが緑の
    まま、配備された指標だけ別物」になる。

    値の表を手で持たずに**振る舞いで**比べる (どちらの向きのずれも捕まる)。

    **振る舞い比較だけでは足りない (r2 codex C2)**。出力に効かない差異
    (plugin が読まないキーが `config.yaml` に混じる / `2.0` が `2` に
    型落ちする) は両方の出力が同じなので緑のまま通る。そこで 2 段にした:

    1. **plugin 側が未知の params キーを拒否する**ようになったので
       (`_reject_unknown_params`)、`config.yaml` に余計なキーがあれば
       この下の `compute(df, dict(meta.params))` が `ValueError` で落ちる
       — 受入テスト側に「キー集合 ⊆ 既知キー」を書く必要がない
       (config に `unused: 1` を足す変異 K で実測)。
    2. **`_DEFAULTS` との辞書同値比較**を型込みで行う。値の表は
       `plugin.py` の `_DEFAULTS` に 1 箇所だけ置き、**受入テスト側に
       重複させない**。
    """
    df = _df(300)
    for meta in _nine_metas(examples_copy):
        mod = _load_module(meta.name,
                           examples_copy / meta.name / "plugin.py")
        assert set(mod._DEFAULTS) == set(mod._KNOWN_PARAMS), meta.name
        assert _typed(meta.params) == _typed(mod._DEFAULTS), (
            meta.name, _typed(meta.params), _typed(mod._DEFAULTS))
        compute = mod.compute
        declared = compute(df, dict(meta.params))
        builtin = compute(df, {})
        for key in meta.outputs:
            assert np.array_equal(
                declared[key].to_numpy(dtype="float64"),
                builtin[key].to_numpy(dtype="float64"),
                equal_nan=True), (meta.name, key, dict(meta.params))


# --- I5: 先頭依存の回帰 ------------------------------------------------------

def _spike_df(n=5000, base=150.0, spike=0.0, seed=1):
    """設計書 §6 I5 の fixture 生成式 (逐語)。`spike` は 401 本目 (= 400 本窓の
    ちょうど手前) に置く — `|Δseed|` を人為的に最大化する構成。"""
    rng = np.random.default_rng(seed)
    sigma = base * 0.002
    close = base + np.cumsum(rng.normal(0.0, sigma, n))
    if spike:
        close[n - 401] += spike
    high = close + np.abs(rng.normal(0.0, sigma / 2.0, n))
    low = close - np.abs(rng.normal(0.0, sigma / 2.0, n))
    open_ = np.concatenate([[close[0]], close[:-1]])
    index = pd.date_range("2020-01-01", periods=n, freq="1h", tz="UTC")
    return pd.DataFrame({"open": open_, "high": high, "low": low,
                         "close": close, "volume": np.ones(n)}, index=index)


def _trend_df(n=5000, base=150.0, direction=1, noise=True, seed=2):
    """設計書 §6 I5 の fixture ②「明確な単調トレンド」の生成式 (逐語)。

    `drift = 基準価格 × 1e-4` / 本 — `n=5000` で基準価格の ±50% を動く
    (150 → 225 / 75)。**下降でも価格が 0 を跨がない**幅にしてある
    (跨ぐと `1e-9 × 基準価格` の絶対公差と `rsi` の相対 ε 基準
    (`<= 1e-9·|close|`) の意味がどちらも壊れる)。
    `noise=True` は drift + §6 I5 のランダムウォーク項、`noise=False` は
    **close が厳密に単調** (high / low の揺らぎだけ残す) — 後者は `rsi` を
    ε 規則②に突き当てる (上昇で 100.0、下降で 0.0 に張り付く)。
    """
    rng = np.random.default_rng(seed)
    sigma = base * 0.002
    drift = base * 1e-4
    walk = np.cumsum(rng.normal(0.0, sigma, n)) if noise else np.zeros(n)
    close = base + direction * drift * np.arange(n) + walk
    high = close + np.abs(rng.normal(0.0, sigma / 2.0, n))
    low = close - np.abs(rng.normal(0.0, sigma / 2.0, n))
    open_ = np.concatenate([[close[0]], close[:-1]])
    index = pd.date_range("2020-01-01", periods=n, freq="1h", tz="UTC")
    return pd.DataFrame({"open": open_, "high": high, "low": low,
                         "close": close, "volume": np.ones(n)}, index=index)


def _degenerate_df(n_pre=200, n_flat=400, base=150.0, spike=1.0, seed=0):
    """設計書 §3.2 (i-b) の反例: 通常データ -> DM/TR 1 本 -> 完全横ばい 400 本。"""
    rng = np.random.default_rng(seed)
    sigma = base * 0.002
    close = list(base + np.cumsum(rng.normal(0.0, sigma, n_pre)))
    high = [v + sigma / 2.0 for v in close]
    low = [v - sigma / 2.0 for v in close]
    top = close[-1] + spike
    close.append(top)
    high.append(top)
    low.append(close[-2])
    for _ in range(n_flat):
        close.append(top)
        high.append(top)
        low.append(top)
    open_ = [close[0]] + close[:-1]
    index = pd.date_range("2020-01-01", periods=len(close), freq="5min",
                          tz="UTC")
    return pd.DataFrame({"open": open_, "high": high, "low": low,
                         "close": close, "volume": [1.0] * len(close)},
                        index=index)


def _last_row_deltas(df: pd.DataFrame, examples_copy: Path) -> dict:
    """全 9 本について「**その plugin が宣言した `max_bars`** 本だけで計算した
    最終行」と「全 `len(df)` 本で計算した最終行」の差の絶対値を
    `{"<plugin 名>.<出力キー>": 値}` で返す。
    両方 NaN のキー (`ichimoku.chikou` 等) は 0.0 とみなす。

    **キーは plugin 名で修飾する** (`DELTA_KEYS` の注記) — 出力キー名だけだと
    `sma.value` が `ema.value` に上書きされて消える。

    **末尾の本数は `meta.max_bars` から取る** — 400 を引数既定値に固定すると、
    I5 が「宣言 `max_bars` の本数で保証が成り立つ」ではなく「400 本で
    成り立つ」しか観測せず、`config.yaml` の宣言を変えても何も red に
    ならない (段 0 の実測)。
    """
    out: dict[str, float] = {}
    for meta in _nine_metas(examples_copy):
        compute = _load_compute(meta.name,
                                examples_copy / meta.name / "plugin.py")
        tail = df.tail(meta.max_bars).copy(deep=True)
        full_res = compute(df, dict(meta.params))
        tail_res = compute(tail, dict(meta.params))
        for key, series in full_res.items():
            a = float(series.iloc[-1])
            b = float(tail_res[key].iloc[-1])
            out[f"{meta.name}.{key}"] = (
                0.0 if (np.isnan(a) and np.isnan(b)) else abs(a - b))
    assert set(out) == DELTA_KEYS, sorted(set(out))
    return out


def test_head_dependence_within_tolerance_on_random_walks(examples_copy):
    """I5 (ランダムウォーク): 4 値域 × seed 0〜7。
    0〜100 スケールは `< 1e-6`、価格スケールは `< 1e-9 * 基準価格`。
    指揮者の実測: 全キーの最大誤差 **3.104e-10 / 違反 0**。"""
    worst = 0.0
    for base in (0.5, 1.5, 150.0, 300.0):
        for seed in range(8):
            deltas = _last_row_deltas(_spike_df(base=base, seed=seed),
                                      examples_copy)
            for key, delta in deltas.items():
                tol = _tolerance(key, base)
                assert delta < tol, (base, seed, key, delta, tol)
                worst = max(worst, delta)
    assert worst < 1e-6, worst


def test_head_dependence_on_the_spike_fixture(examples_copy):
    """I5 (スパイク): 401 本目に基準価格の 60% (= +90) を置く。
    公差は上と同じ。指揮者の実測 (参考): `rsi` 1.42e-10 / `adx` 3.40e-09 /
    `macd` 2.56e-13 / `atr` 1.79e-12。"""
    base = 150.0
    deltas = _last_row_deltas(_spike_df(base=base, spike=base * 0.6, seed=1),
                              examples_copy)
    for key, delta in deltas.items():
        tol = _tolerance(key, base)
        assert delta < tol, (key, delta, tol)


def test_head_dependence_on_monotonic_trends(examples_copy):
    """I5 (単調トレンド): 設計書 §6 I5 の fixture ② — **ランダムウォークでは
    出ない「持続的な一方向の drift」での先頭依存**を見る。公差はランダム
    ウォークと同じキー別の表。

    4 象限 × 2 値域 = 8 系列: 上昇 / 下降 × `noise=True` (drift + 揺らぎ) /
    `noise=False` (close が厳密に単調) × 基準価格 1.5 / 150。

    指揮者の実測 (公差に対する最大比は `bollinger.upper` / `lower` の
    **0.4%**): `rsi` は `noise=True` で 1.01e-11、`noise=False` では
    **厳密に 0.0** (ε 規則②で 100.0 / 0.0 に張り付くため両側が一致する)。
    `adx` は 1.5e-11 〜 1.4e-10。**`noise=False` でも `adx` は飽和しない**
    (high / low の揺らぎが ±DM を両方立てるので、実測で +DI 25.4 / −DI 13.4
    / ADX 22.3、下降で +DI 18.4 / −DI 21.4 / ADX 26.4)。
    """
    for noise in (True, False):
        for direction in (1, -1):
            for base in (1.5, 150.0):
                deltas = _last_row_deltas(
                    _trend_df(base=base, direction=direction, noise=noise),
                    examples_copy)
                for key, delta in deltas.items():
                    tol = _tolerance(key, base)
                    assert delta < tol, (noise, direction, base, key,
                                         delta, tol)


def test_head_dependence_on_the_degenerate_fixture(examples_copy):
    """I5 (退化): 設計書 §3.2 (i-b) の反例。**公差はランダムウォークの表では
    なく §3.2 (i-b) の `1e-4` を全キーに適用する** — この fixture は
    `bollinger` の `upper` / `lower` に **1.08e-06** を出し、価格スケールの
    `1e-9 * base` (= 1.5e-7) を超える (指揮者の実測)。`1e-4` は全キーを覆う
    (最大は `adx` の **4.31e-06**)。

    比を取る指標 (`rsi` / `plus_di` / `minus_di`) と rolling の有限記憶
    (`k` / `d`) は**厳密に 0** になることを個別に pin する — ここが
    `EPS` 規則の唯一の観測点。"""
    deltas = _last_row_deltas(_degenerate_df(), examples_copy)
    for key, delta in deltas.items():
        assert delta < 1e-4, (key, delta)
    for key in ("rsi.rsi", "adx.plus_di", "adx.minus_di",
                "stochastic.k", "stochastic.d"):
        assert deltas[key] == 0.0, (key, deltas[key])
    assert 0.0 < deltas["adx.adx"] < 1e-4, deltas["adx.adx"]


# --- I6 / I8: bless の tmp 環境 ---------------------------------------------

def _bless_env(tmp_path: Path):
    """`(root, plugins_dir, conn)`。`tests/fixtures/wiring_envs.py:46-52` の
    `switch_env` と同じ流儀 (実 DB / 実 `plugins/` を触らない)。
    **`.locks` は作らなくてよい** — `switch._plugin_lock` (`switch.py:806-807`)
    が `mkdir(parents=True, exist_ok=True)` する
    (`tests/plugin/test_switch_paths.py:46-47` は明示的に作っているが、
    どちらでも成立する)。"""
    root = tmp_path
    plugins_dir = root / "plugins"
    (plugins_dir / "_human").mkdir(parents=True)
    (root / "logs").mkdir(exist_ok=True)
    conn = db_store.connect(root / "agentic.db")
    db_store.init_db(conn)
    return root, plugins_dir, conn


def _stage(plugins_dir: Path, name: str) -> Path:
    """runbook 手順 (1): `cp -r docs/examples/plugins/<名前> plugins/_human/<名前>`。
    **`__pycache__` / `.pytest_cache` を除外しない** — 巻き込んでも
    `check_candidate_snapshot` が無視する (`gate_pytest.py:43,46-55`)
    ことを実地で観測するため。"""
    dest = plugins_dir / "_human" / name
    shutil.copytree(EXAMPLES / name, dest)
    return dest


def _bless(conn, plugins_dir: Path, name: str) -> int:
    """runbook 手順 (2): `afx plugin bless <名前> --from _human` の中身
    (`backtest/cli.py:585-588` が呼ぶのと同じ引数)。"""
    return plugin_switch.bless_candidate(
        conn, name=name, human_dir=plugins_dir / "_human" / name,
        settings=SETTINGS, now=NOW, decided_by="human_cli")


def _inventory_names(conn, plugins_dir: Path) -> list[str]:
    """`approved_plugins` は **`InventoryBuildResult`** を返す
    (`tools/plugin_loader.py:41-59`) — 一覧は `result.inventory.metas`。"""
    result = plugin_loader.approved_plugins(conn, plugins_dir,
                                            settings=SETTINGS)
    return sorted(m.name for m in result.inventory.metas)


@pytest.mark.slow
def test_nine_indicators_bless_in_sequence(tmp_path):
    """I6: 9 本を 1 本ずつ順に bless でき、9 回とも `int` (approval_id) が返り、
    inventory に 9 名が `outputs` 付きで並ぶ。

    **pytest ゲートを double にしない** — ここはゲートを実物で回すことが
    受入そのもの。指揮者の実測で 9 本合計 **11.7 秒** (1 本 1.2〜1.4 秒、
    `plugin.pytest_timeout_sec: 300` に対して桁で余裕)、Landlock 下でも
    `import pandas` と自己テストは落ちない (`gate_pytest` が
    `_SINGLE_THREAD_ENV` を子 env に入れるため)。`slow` marker を付けるが、
    `pyproject.toml:44` の `addopts` は slow を除外しないのでフルスイートには残る。

    同時に観測していること: (i) `noop_gate.find_noop_copy` はこの経路に**無い**
    (`switch._run_full_gate` のゲート列に現れない) ので「example の丸写し」で
    弾かれない (ii) `outputs_required` に掛からない (iii) `max_bars_limit`
    に掛からない (400 <= 1000)。
    """
    _root, plugins_dir, conn = _bless_env(tmp_path)
    for name in NINE:
        _stage(plugins_dir, name)
        approval_id = _bless(conn, plugins_dir, name)
        assert isinstance(approval_id, int), (name, approval_id)
        assert (plugins_dir / name).is_symlink(), name

    result = plugin_loader.approved_plugins(conn, plugins_dir,
                                            settings=SETTINGS)
    by_name = {m.name: m for m in result.inventory.metas}
    assert sorted(by_name) == sorted(NINE)
    for name in NINE:
        assert by_name[name].kind == "indicator", name
        assert by_name[name].outputs, name
        assert by_name[name].max_bars == 400, name


# --- I7: 新 `rsi` を宣言した strategy の E2E ---------------------------------

_STRATEGY_PY = '''\
def evaluate(df, indicators, signals, params):
    rsi = indicators["rsi"]["rsi"]
    return {"action": "hold",
            "rationale": f"len={len(rsi)} nonnan={int(rsi.notna().sum())} "
                         f"last={float(rsi.iloc[-1]):.6f}"}
'''

_STRATEGY_TEST_PY = '''\
from plugin import evaluate


def test_evaluate_is_callable():
    assert callable(evaluate)
'''


def _write_strategy(base: Path, name: str, *, max_bars: int) -> Path:
    d = base / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "plugin.py").write_text(_STRATEGY_PY)
    (d / "config.yaml").write_text(yaml.safe_dump(
        {"kind": "strategy", "timeframe": "1h", "pairs": ["USDJPY"],
         "exit_mode": "levels", "max_bars": max_bars,
         "indicators": {"rsi": {"plugin": "rsi", "params": {"period": 14}}},
         "params": {}}, sort_keys=False))
    (d / "test_plugin.py").write_text(_STRATEGY_TEST_PY)
    return d


def _deploy_rsi_and_resolve(tmp_path: Path, *, max_bars: int):
    """新 `rsi` を bless で配備し、`max_bars` の strategy 候補に pin を書いて
    `pin_mode="require"` で解決する。戻り値 `(meta, resolved)`。

    pin の書き込みは **`plugin.resolve.lock_config`** で行う
    (`resolve.py:207-208`、人間 CLI の `afx plugin lock` と同じ関数)。
    `tools.improve_staging_tools` の `lock_staging_deps` は tooldef の
    **内部クロージャ**で import できず、`afx plugin lock` の CLI 自体は
    init 済みリポジトリ root を要求する (tmp では
    `初期化が完了していません` で rc=2)。
    """
    _root, plugins_dir, conn = _bless_env(tmp_path)
    _stage(plugins_dir, "rsi")
    _bless(conn, plugins_dir, "rsi")
    inventory = plugin_loader.approved_plugins(
        conn, plugins_dir, settings=SETTINGS).inventory
    rsi_meta = inventory.by_name("rsi")
    assert rsi_meta is not None and rsi_meta.max_bars == 400

    cand = _write_strategy(tmp_path / "cand", f"s{max_bars}",
                           max_bars=max_bars)
    lock_config(cand, {"rsi": rsi_meta.content_hash})
    meta, reason = discover_one_with_reason(cand, f"s{max_bars}")
    assert reason is None, reason
    # `resolve_indicator_deps` の第 2 引数は **`ApprovedInventory`**
    # (`InventoryBuildResult` ではない — `resolve.py:161-162`)。
    resolved = resolve_indicator_deps(meta, inventory, settings=SETTINGS,
                                      pin_mode="require")
    assert [(i.alias, i.plugin_name, i.pinned) for i in resolved.items] == [
        ("rsi", "rsi", True)]
    return meta, resolved


@pytest.mark.slow
def test_strategy_declaring_new_rsi_runs_end_to_end(tmp_path):
    """I7: 新 `rsi` を宣言した strategy を **実 worker サブプロセス**で回し、
    `indicators["rsi"]["rsi"]` が df と同じ index の系列として届く
    (**モックで worker を潰さない** — [[test-fixtures-from-real-transcripts]])。

    `PluginSession` のシグネチャは
    `PluginSession(meta, *, settings: PluginSettings, resolved=None)`
    (`sandbox.py:330-331`) — **`kind=` という引数は無く**、`settings` は
    `Settings` 全体ではなく **`Settings.plugin`**。
    """
    meta, resolved = _deploy_rsi_and_resolve(tmp_path, max_bars=400)
    df = _df(600).tail(400)
    with PluginSession(meta, settings=SETTINGS.plugin,
                       resolved=resolved) as session:
        out = session.call({"df": df, "params": {}})
    assert out["action"] == "hold"
    # worker は indicator 自身の `max_bars` で `df.tail(...)` したうえで
    # `reindex(df.index)` を掛ける (`worker.py:318,321-325`) ので、
    # strategy に渡した df と同じ長さで届く。
    assert f"len={len(df)}" in out["rationale"], out["rationale"]
    # warmup (period=14) の分だけ NaN が先頭に残る = 全 NaN でも全非 NaN でもない
    assert "nonnan=386" in out["rationale"], out["rationale"]


@pytest.mark.slow
def test_strategy_with_max_bars_200_runs_but_is_outside_the_i5_guarantee(
        tmp_path):
    """I7 (保証外ケース): 同じ strategy を `max_bars: 200` で作ると**動く**
    (例外にならない) が、**I5 の保証範囲の外**。

    worker は `sub_df = df.tail(dep["max_bars"])` (`worker.py:318`) と
    indicator 自身の `max_bars` (= 400) で tail するが、strategy の
    `max_bars` が 200 なら df 自体が 200 本しかないので 200 本で計算される。
    **値の一致は要求しない** — 指揮者の実測で最終 `rsi` は
    `65.797529` (400 本) と `65.797530` (200 本) で食い違う。
    """
    meta, resolved = _deploy_rsi_and_resolve(tmp_path, max_bars=200)
    df = _df(600).tail(200)
    with PluginSession(meta, settings=SETTINGS.plugin,
                       resolved=resolved) as session:
        out = session.call({"df": df, "params": {}})
    assert out["action"] == "hold"
    assert "len=200" in out["rationale"], out["rationale"]


# --- I8: runbook の逐語再現 --------------------------------------------------

@pytest.mark.slow
def test_runbook_normal_sequence_deploys_all_nine(tmp_path):
    """I8(a) 正常系: runbook の手順 (1) コピー → (2) bless → (4) 後片付け →
    (5) 9 本ぶん繰り返す、をそのまま実行する。コピーが `__pycache__` /
    `.pytest_cache` を巻き込んでも通る。"""
    _root, plugins_dir, conn = _bless_env(tmp_path)
    for name in NINE:
        _stage(plugins_dir, name)
        _bless(conn, plugins_dir, name)
        shutil.rmtree(plugins_dir / "_human" / name)   # 手順 (4)
    assert _inventory_names(conn, plugins_dir) == sorted(NINE)


@pytest.mark.slow
def test_runbook_gate_failure_keeps_earlier_deployments_and_resumes(tmp_path):
    """I8(b) ゲート前・ゲート中の失敗 (設計書 §6.3 (A)): 5 本目の候補の
    `test_plugin.py` をわざと落として bless を失敗させる。

    - **4 本は配備済のまま残る** (9 本は束ではない)
    - 失敗した 1 本には**何も残らない** — `.versions/<名前>` も、未終端 journal も、
      pending approval も。**「journal テーブルの行数が 0」を assert しては
      いけない** — 先行 4 本の**終端済** journal が 4 行残っている (指揮者の実測)
    - 候補を直して 5 本目から再開すると最終的に 9 本揃う
    """
    _root, plugins_dir, conn = _bless_env(tmp_path)
    broken = NINE[4]   # bollinger
    for name in NINE:
        _stage(plugins_dir, name)
        if name == broken:
            (plugins_dir / "_human" / name / "test_plugin.py").write_text(
                "def test_broken():\n    assert False\n")
            with pytest.raises(ValueError, match="failed pytest gate"):
                _bless(conn, plugins_dir, name)
            assert _inventory_names(conn, plugins_dir) == sorted(NINE[:4])
            assert not (plugins_dir / ".versions" / name).exists()
            assert journal_store.get_open_by_name(conn, name) is None
            assert conn.execute(
                "SELECT COUNT(*) c FROM approval_requests WHERE status='pending'"
            ).fetchone()["c"] == 0
            # 候補を直して同じ名前でやり直す (runbook (A))
            shutil.rmtree(plugins_dir / "_human" / name)
            _stage(plugins_dir, name)
        _bless(conn, plugins_dir, name)
        shutil.rmtree(plugins_dir / "_human" / name)
    assert _inventory_names(conn, plugins_dir) == sorted(NINE)


#: `_fail_record_version_once` が 1 回目に送出する文言。2 つのテストが
#: `match` / `in stderr` でこの文字列を見るので定数で持つ。
_INJECTED = "injected: after version dir, before symlink switch"


def _fail_record_version_once(monkeypatch) -> None:
    """`switch.history_git.record_version` を **1 回だけ** `OSError` にする。

    版ディレクトリ作成 (`switch.py:1198`) の**後**・symlink 切替
    (`:1219`) の**前**という §6.3 (B) の位置に故障を作るための注入で、
    差し替えはモジュール属性 (`test_reconcile.py:268,471` と同じ形)。
    `_advance_to_decided` 自体を差し替えるとこの位置は作れない。

    I8(c) と CLI (B) の 2 テストが逐語で同じブロックを持っていたので
    ここへ寄せた (/code-review #5)。**挙動は不変** — 2 回目以降は本物へ
    委譲するので、収束手順 (retry / 再 bless) はそのまま成功する。
    """
    real_record = plugin_switch.history_git.record_version
    calls = {"n": 0}

    def _fail_once(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError(_INJECTED)
        return real_record(*args, **kwargs)

    monkeypatch.setattr(plugin_switch.history_git, "record_version",
                        _fail_once)


@pytest.mark.slow
def test_runbook_post_gate_failure_converges_via_approval_retry(tmp_path,
                                                                monkeypatch):
    """I8(c) ゲート後の失敗 (設計書 §6.3 (B)): 版ディレクトリ作成後・symlink
    切替前で 1 回だけ失敗させ、runbook の収束手順 5 ステップが逐語で通ること
    を観測する。

    **故障注入は `switch.history_git.record_version` のモジュール属性差し替え**
    (`test_reconcile.py:268,471` と同じ形)。`_advance_to_decided` は
    版作成 (`switch.py:1198`) → `phase="versioned"` (`:1205`) →
    history 記録 (`:1208`) → `phase="recorded"` (`:1212`) → symlink 切替
    (`:1219`) の順に進むので、ここで落とすと **`.versions/<名前>/<hash>` は
    残り / journal は `versioned` / live symlink は無い**という §6.3 (B) の
    症状 (「`.versions/` が増えている」) がそのまま再現する。
    `_advance_to_decided` 自体を差し替えるとこの位置は作れない。

    **`approval retry` の効き方は失敗 phase で変わる (指揮者の実測、2026-09-19)**:

    | 失敗 phase | retry 後 | 再 bless |
    |---|---|---|
    | `preparing` (版作成で失敗) | journal 終端 + **配備完了** | 手順 5 の確認。同内容なので no-op |
    | `versioned` / `recorded` (history / 切替直前) | journal 終端 + **配備完了** | 同上 (本テストのケース) |
    | `switched` (切替で失敗し live が新 target でない) | journal 終端 + **配備完了** (停止行を巻き戻して閉じ、新しい journal 行で手順を頭から流す — [switch-ops-hardening] 案 C) | 確認 (no-op) |

    `switched` の行は [switch-ops-hardening] で解消済み。0d は live の指す先を
    分類し、`not_switched` なら巻き戻してから新しい行で配備まで完了させる。
    live が第三者に触られている (`foreign`) ときだけ、触らず人間待ちになる。
    §6.3 (B) の手順 5 (「もう一度 bless して `UnresolvedJournalError` が出ない
    ことを確認する」) は **どの phase でも確認 (no-op)** として働く。

    状態遷移そのものは `tests/plugin/test_switch_journal.py` /
    `test_reconcile.py` が pin 済みなので再実装しない (設計書 §6.1)。
    """
    root, plugins_dir, conn = _bless_env(tmp_path)
    _stage(plugins_dir, "rsi")

    _fail_record_version_once(monkeypatch)

    with pytest.raises(OSError, match="injected"):
        _bless(conn, plugins_dir, "rsi")

    # 残るもの: `.versions/` + 未終端 journal (phase=versioned) + pending approval。
    open_journal = journal_store.get_open_by_name(conn, "rsi")
    assert open_journal is not None
    assert open_journal["phase"] == "versioned"
    approval_id = open_journal["approval_id"]
    assert conn.execute(
        "SELECT status FROM approval_requests WHERE id=?",
        (approval_id,)).fetchone()["status"] == "pending"
    assert (plugins_dir / ".versions" / "rsi").is_dir()
    assert not (plugins_dir / "rsi").exists()
    assert _inventory_names(conn, plugins_dir) == []

    # 手順 2: 同じ bless をもう一度実行して `op_id` / `approval_id` を読む。
    # 例外は `UnresolvedJournalError` で **`ValueError` ではない**
    # (`switch.py:1572` は `Exception` を継承) ため、CLI の
    # `except (ValueError, SandboxError)` (`backtest/cli.py:590`) を
    # すり抜けて Python traceback が出る — runbook (B) の記述の根拠。
    with pytest.raises(plugin_switch.UnresolvedJournalError) as excinfo:
        _bless(conn, plugins_dir, "rsi")
    assert not isinstance(excinfo.value, ValueError)
    assert f"op_id={open_journal['op_id']}" in str(excinfo.value)
    assert f"approval_id={approval_id}" in str(excinfo.value)

    # 手順 3: 対話シェルの `afx> approval retry <id>`
    # (`commands.py:139-153`。`plugins_root` / `settings` が未配線だと
    # 何もせず文字列を返すだけなので、必ず両方渡す)。
    clock = FixedClock(NOW)
    cmds = Commands(
        conn=conn, state_store=StateStore(root / "state.json"),
        broker=PaperBroker(conn, SETTINGS, clock), trade_loop=MagicMock(),
        activity=ActivityLog(root / "logs" / "activity.log"),
        log_dir=root / "logs", clock=clock, health_latch=HealthLatch(),
        plugins_root=plugins_dir, settings=SETTINGS)
    # [switch-ops-hardening] T5: シェルは lock 内で確定した outcome を文言に
    # 写す (設計書 §3.5)。`versioned` からの retry は新規経路を流し切るので
    # `deployed`。
    retry_out = cmds.dispatch(f"approval retry {approval_id}")
    assert retry_out.startswith(f"approval #{approval_id} を再試行しました: ")
    assert "配備まで完了しました" in retry_out
    assert f"plugins/rsi → .versions/rsi/" in retry_out

    # `versioned` からの retry は手順を頭から冪等に流すので、journal の終端と
    # **配備の完了**が同時に起きる。
    assert journal_store.get_open_by_name(conn, "rsi") is None
    assert conn.execute(
        "SELECT status FROM approval_requests WHERE id=?",
        (approval_id,)).fetchone()["status"] == "approved"
    assert (plugins_dir / "rsi").is_symlink()
    assert _inventory_names(conn, plugins_dir) == ["rsi"]

    # 手順 5: もう一度 bless して `UnresolvedJournalError` が出ないことを確認する。
    # 同内容なので切替は no-op だが、**approval 行は 1 本増える** (仕様)。
    second_id = _bless(conn, plugins_dir, "rsi")
    assert isinstance(second_id, int) and second_id != approval_id
    assert (plugins_dir / "rsi").is_symlink()
    assert _inventory_names(conn, plugins_dir) == ["rsi"]

    # 承認詳細に依存 strategy の 2 欄が出る (`commands.py:394-400`) —
    # runbook の「任意の後片付け」で人間が退役前に確認する手段。
    detail = cmds.dispatch(f"approval {second_id}")
    assert "dependent_pinned_here=" in detail
    assert "dependent_pinned_elsewhere=" in detail


# --- I8: runbook の CLI 境界 (`afx plugin bless <名前> --from _human`) --------

def _cli_env(tmp_path: Path) -> Path:
    """`afx` の CLI が要求する形の tmp root を作って返す。

    `backtest/cli.py:dispatch` は root を `Path.cwd()` として受け取り
    (`entry.py:23`)、**`ensure_initialized(root)`** (`service.py:75-79`、
    `data/state/app_state.json` の `initialized`) と
    **`config/settings.yaml`**、**`data/agentic.db`** を見る。上の
    `_bless_env` は DB を `root/agentic.db` に置くので**混ぜない** — CLI 用は
    このビルダで別に作る。

    `ensure_initialized` を monkeypatch で潰さない (`tests/backtest/test_cli.py`
    は潰している) — ここで観測したいのは「runbook の人間が打つコマンドが
    そのまま通ること」なので、初期化済みの状態も実物で用意する。
    `settings.yaml.example` は `SETTINGS_FIXTURE` の生成元そのもの
    (`tests/fixtures/wiring_envs.py:24`) なので `plugin.*` の値は上の
    テスト群と同じ。
    """
    (tmp_path / "config").mkdir(parents=True, exist_ok=True)
    shutil.copy(_REPO / "config" / "settings.yaml.example",
                tmp_path / "config" / "settings.yaml")
    (tmp_path / "data" / "state").mkdir(parents=True, exist_ok=True)
    (tmp_path / "logs").mkdir(exist_ok=True)
    (tmp_path / "plugins" / "_human").mkdir(parents=True, exist_ok=True)
    StateStore(tmp_path / "data" / "state" / "app_state.json").update(
        initialized=True)
    conn = db_store.connect(tmp_path / "data" / "agentic.db")
    db_store.init_db(conn)
    conn.close()
    return tmp_path


@pytest.mark.slow
def test_runbook_cli_bless_succeeds_and_prints_approval_id(
        tmp_path, monkeypatch, capsys):
    """I8 (CLI 正常系): runbook 手順 (2) の
    `afx plugin bless <名前> --from _human` を **CLI の入口から**実行し、
    rc=0 と stdout の `approval id=<N>` を観測する。

    `entry.main` を **in-process** で呼ぶ (`tests/backtest/test_cli.py` の流儀)。
    subprocess にしないのは、(a) `main` が argparse → `dispatch` →
    `_plugin_bless` の CLI 境界そのものであり rc / stdout / stderr が
    同じであること、(b) 下の (B) が `record_version` の monkeypatch を
    同一プロセスに載せる必要があること、の 2 点による。
    `pyproject.toml:28` の `afx = "agentic_fx.entry:main"` が runbook の
    `afx ...` と同じ入口。
    """
    root = _cli_env(tmp_path)
    monkeypatch.chdir(root)
    shutil.copytree(EXAMPLES / "sma", root / "plugins" / "_human" / "sma")

    rc = main(["plugin", "bless", "sma", "--from", "_human"])

    assert rc == 0
    out = capsys.readouterr().out
    assert re.search(r"^approval id=\d+$", out, re.M), out

    # **「symlink がある」だけでは足りない (r2 codex C3)** — 壊れた target を
    # 指していても通ってしまう。runbook の bless 成功は「live artifact が
    # 配備されたこと」なので、target の形 / 解決先の実在 / inventory から
    # metadata が引けることまで見る。target は相対 `.versions/<名前>/<hash>`
    # (`switch.py:1218-1219`)。
    live = root / "plugins" / "sma"
    assert live.is_symlink()
    target = live.readlink()
    assert target.parts[:2] == (".versions", "sma"), target
    assert len(target.parts) == 3, target
    assert re.fullmatch(r"[0-9a-f]{8,}", target.parts[2]), target
    resolved = (root / "plugins" / target).resolve()
    assert resolved.is_dir(), resolved
    assert (resolved / "plugin.py").is_file(), resolved
    assert (live / "config.yaml").is_file(), live

    conn = db_store.connect(root / "data" / "agentic.db")
    try:
        inv = plugin_loader.approved_plugins(
            conn, root / "plugins", settings=SETTINGS).inventory
    finally:
        conn.close()
    sma = inv.by_name("sma")
    assert sma is not None and sma.kind == "indicator", sma
    assert tuple(sma.outputs) == ("value",), sma.outputs


@pytest.mark.slow
def test_runbook_cli_bless_gate_failure_exits_1_with_error_on_stderr(
        tmp_path, monkeypatch, capsys):
    """I8 (CLI ゲート失敗、設計書 §6.3 (A)): 候補の `test_plugin.py` を壊すと
    `bless_candidate` が `ValueError` を投げ、`_plugin_bless` の
    `except (ValueError, SandboxError)` (`backtest/cli.py:591-593`) が
    **rc=1 + stderr の `エラー: `** に写像する。承認行も symlink も残らない。
    """
    root = _cli_env(tmp_path)
    monkeypatch.chdir(root)
    dest = root / "plugins" / "_human" / "sma"
    shutil.copytree(EXAMPLES / "sma", dest)
    (dest / "test_plugin.py").write_text(
        "def test_broken():\n    assert False\n")

    rc = main(["plugin", "bless", "sma", "--from", "_human"])

    assert rc == 1
    captured = capsys.readouterr()
    assert "エラー: " in captured.err, captured.err
    assert "failed pytest gate" in captured.err, captured.err
    assert not (root / "plugins" / "sma").exists()
    conn = db_store.connect(root / "data" / "agentic.db")
    try:
        assert conn.execute(
            "SELECT COUNT(*) c FROM approval_requests").fetchone()["c"] == 0
    finally:
        conn.close()


@pytest.mark.slow
def test_runbook_cli_bless_after_post_gate_failure_raises_traceback(
        tmp_path, monkeypatch, capsys):
    """I8 (CLI ゲート後失敗、設計書 §6.3 (B)): 未終端 journal が残った状態で
    同名を再 bless すると、CLI は **rc=1 と `エラー: ` の 1 行**で拒否する
    ([switch-ops-hardening] T6 で是正。旧稿は `UnresolvedJournalError` が
    `_plugin_bless` の `except (ValueError, SandboxError)` を素通りして
    **traceback** になるのを pin していた — その旧挙動をこの版で置き換えた)。
    メッセージには収束に必要な `op_id` と `approval_id`、および次の 1 手が
    含まれる。

    1 回目の失敗注入 (`record_version` の `OSError`) は逆に
    `dispatch` の `except OSError` に**捕まる**ので rc=1 になる — 同じ
    §6.3 (B) でも CLI から見える形が 1 回目と 2 回目で違うことを両方観測する。
    """
    root = _cli_env(tmp_path)
    monkeypatch.chdir(root)
    shutil.copytree(EXAMPLES / "sma", root / "plugins" / "_human" / "sma")

    _fail_record_version_once(monkeypatch)

    rc = main(["plugin", "bless", "sma", "--from", "_human"])
    assert rc == 1
    assert "injected" in capsys.readouterr().err

    conn = db_store.connect(root / "data" / "agentic.db")
    try:
        open_journal = journal_store.get_open_by_name(conn, "sma")
        assert open_journal is not None and open_journal["phase"] == "versioned"
    finally:
        conn.close()

    rc2 = main(["plugin", "bless", "sma", "--from", "_human"])
    err2 = capsys.readouterr().err
    assert rc2 == 1
    assert "Traceback" not in err2
    assert f"op_id={open_journal['op_id']}" in err2
    assert f"approval_id={open_journal['approval_id']}" in err2
    # **この行は T6 で足した案内文そのもの** — 元の例外文言にも
    # "approval retry" は含まれるので、そちらでは判別力がない (変異で実測)。
    assert "収束手順: サービスの対話シェルで `approval list`" in err2
