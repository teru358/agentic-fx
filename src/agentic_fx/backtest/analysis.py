"""analysis — 履歴分析: 相関 3 種・非漏洩出力契約・analysis_runs (プラン 6 Task 10)。

バックテスト基盤 (Task 7 runner / Task 8 metrics / Task 9 holdout) の上に、
改善ループ (プラン 9) へ露出する分析面を追加する。

改善ループへ露出する唯一の面は ``analyze_for_agent`` (プラン 9 で tool 化)。
それ以外の低レベル関数 (``corr_matrix`` / ``rolling_corr_summary`` /
``lead_lag`` / ``coverage_report``) は Task 11 (人間 CLI) やテストから直接
呼べるが、期間 (``in_sample_until``) は呼び出し元が明示的に渡す必要がある。
``analyze_for_agent`` はその内部で ``holdout.in_sample_until(now, ...)``
(UTC 正規化 + 分格子切り捨て込みの境界算術の単一所有者 — F1, 最終レビュー
opus I-1) を適用することで期間の所有をこのモジュールに一元化する (Task 9
の holdout 遮断 1 と同じ規律: 改善ループへの返り値には日時型・list[時系列]・
件数・境界日時を含めない)。

エラーは固定コード ``{"error": <code>}`` のみ (レビュー裁定 codex I2)。
メッセージ文字列・件数・利用可能範囲・境界日時はエラーに含めない
(Task 9 F1 と同じ遮断規律 — 例外経路にも適用する)。
"""
from __future__ import annotations

import math
import sqlite3
import statistics
from datetime import datetime, timedelta
from typing import Any

from agentic_fx.backtest.holdout import in_sample_until as _in_sample_until
from agentic_fx.backtest.timeframes import floor_to_bucket, load_resampled_frame
from agentic_fx.config import Settings
from agentic_fx.core.market_hours import is_market_open
from agentic_fx.core.timeutil import as_utc
from agentic_fx.store import analysis_runs as analysis_runs_store

# パラメータは列挙制 (§6) — 自由な数値は受けない。
TIMEFRAMES = ("15m", "1h", "4h", "1d")
WINDOWS = (20, 60, 120)          # rolling_corr_summary の窓幅 (バー数)
LAGS = range(-12, 13)            # lead_lag が走査するラグ幅 (バー数)

# バー幅 (分)。キー集合は TIMEFRAMES と一致させる。
_TF_MINUTES = {"15m": 15, "1h": 60, "4h": 240, "1d": 1440}
assert set(_TF_MINUTES) == set(TIMEFRAMES)

# 1 つの相関値の計算に使う整列済み観測対の最小件数 (多重比較の分母を
# 曖昧にしないための下限)。corr_matrix/lead_lag は「1 つの相関値」= その
# 呼び出しが計算する 1 個の pearson (ペア全体 / 1 ラグ) に直接適用する。
# rolling_corr_summary は WINDOWS に 20 が含まれる (< 30) ため、
# 「窓 1 個あたり」に適用すると window=20 が常に計算不能になってしまう
# (列挙値を殺す解釈は採用しない)。よってここでは「窓に分割する前の、
# 整列済みリターン系列全体の長さ」に適用し、窓の個数そのものは別途
# 「窓数 < 2 は ValueError」(§C) で下限を強制する。
MIN_COMMON_OBS = 30

# バックテスト・分析の正系列は dukascopy 固定 (Task 5 で mt5 は照合用と
# 裁定済み)。request からは受け付けない — request に source キーがあれば
# 余剰キーとして invalid_request になる。
ANALYSIS_SOURCE = "dukascopy"


def _load_returns(conn: sqlite3.Connection, symbol: str, timeframe: str, *,
                  source: str, in_sample_until: datetime,
                  since: datetime | None = None,
                  ) -> dict[datetime, float]:
    """symbol の log リターン系列を返す (キーは aware UTC datetime)。

    各 bucket について「ちょうど 1 バー幅前」の close が存在するペアの
    みリターンを定義する — 欠損ギャップを跨ぐリターンは作らない (§C)。

    プラン 7 Task 0 で ohlcv の 1m 行から
    ``load_resampled_frame(...).close`` へ配線を切り替えた (分析面の生産者
    不在ブロッカー解消 — 本ブランチのインポータは 1m しか書かない)。

    境界の対応関係 (レビュー Fix Round 1, sonnet Important-1 で訂正):
    旧実装は ``bar_time < in_sample_until`` (interval=timeframe の行を
    直接読む) だった。新実装は「完成バケットのみ (``bucket_end <=
    until``)」(``load_resampled_frame`` 契約)。**``in_sample_until`` が
    timeframe 幅の格子上にある (on-grid な) 場合に限り**この 2 条件は同値
    になる (``bar_time < until`` ⟺ ``bar_time <= until - width`` ⟺
    ``bar_time + width <= until``。整数個のバー幅刻みでは端数が出ない)。

    **``in_sample_until`` が格子に非整列 (オフグリッド) な場合は同値では
    ない** — これが実運用の通常ケースであることに注意 (``holdout.
    in_sample_until`` は分格子にしか揃わず、4h/1d/15m のようなより粗い
    timeframe の格子には一般に整合しない)。この場合、新実装は「until
    時点でまだ形成中の末尾バケット」を旧実装より 1 本多く落とす —
    旧実装なら ``bar_time < until`` を満たしてしまっていたバー (バケット
    の開始時刻は until より前だが、終了時刻は until を超える) が、新実装
    では ``bucket_end <= until`` を満たさず除外される。これは意図した
    先読み防止の強化 (fail closed 方向) であり、退行ではない。

    ``in_sample_until`` は ``load_resampled_frame`` の ``until`` (排他/
    完成バケットのみ) として渡す。

    ``since`` (Task 11 上書き節 E の最小追加): 指定時は
    ``load_resampled_frame`` の ``since`` (= since 以降に開始する完成
    バケットのみ) として渡す。既定 None は従来どおり全履歴 (改善ループ
    ``analyze_for_agent`` は since を渡さない — 挙動不変)。

    F1 (Fix Round 1, codex Important-1 = sonnet Minor-2): ohlcv.close に
    正値制約は無く (REAL NOT NULL のみ)、close<=0 のバーが混入すると
    ``math.log(c / prev)`` が ``prev==0`` で ZeroDivisionError を送出し
    ``analyze_for_agent`` の ``except ValueError`` を迂回して生の例外が
    改善ループ面へ漏れ得る。log の定義域 (両側とも正の有限値) を明示的に
    ガードし、非正値・非有限バーはリターン対象外とする — 結果として
    観測不足なら insufficient_data に自然合流する (fail closed)。
    """
    until_utc = as_utc(in_sample_until)
    since_utc = None if since is None else as_utc(since)
    df = load_resampled_frame(conn, symbol, timeframe, source=source,
                              since=since_utc, until=until_utc)
    closes = {ts.to_pydatetime(): float(c) for ts, c in df["close"].items()}
    width = timedelta(minutes=_TF_MINUTES[timeframe])
    returns: dict[datetime, float] = {}
    for t, c in closes.items():
        prev = closes.get(t - width)
        if (prev is not None and prev > 0 and c > 0
                and math.isfinite(prev) and math.isfinite(c)):
            returns[t] = math.log(c / prev)
    return returns


def _align_same_time(ret_a: dict[datetime, float], ret_b: dict[datetime, float],
                     ) -> tuple[list[float], list[float]]:
    """bar_time の集合積 (inner join) で 2 リターン系列を整列する。"""
    common = sorted(set(ret_a) & set(ret_b))
    return [ret_a[t] for t in common], [ret_b[t] for t in common]


def _align_lagged(ret_a: dict[datetime, float], ret_b: dict[datetime, float], *,
                  lag_bars: int, width_minutes: int,
                  ) -> tuple[list[float], list[float]]:
    """``{(a_ret[t + lag_bars*width], b_ret[t]) : 両時点にリターンが存在する t}``。

    正の ``lag_bars`` = b が a に先行 (b[t] が a[t+lag] を予告する) の向き。
    """
    shift = timedelta(minutes=lag_bars * width_minutes)
    xs: list[float] = []
    ys: list[float] = []
    for t in sorted(ret_b):
        shifted = t + shift
        val_a = ret_a.get(shifted)
        if val_a is not None:
            xs.append(val_a)
            ys.append(ret_b[t])
    return xs, ys


def _raw_corr(xs: list[float], ys: list[float]) -> float:
    """下限件数チェックなしの pearson。定数系列 (分散 0) は ValueError
    (メッセージに件数・値は含めない)。"""
    try:
        return statistics.correlation(xs, ys)
    except statistics.StatisticsError as e:
        raise ValueError("correlation is undefined for this input") from e


def _pearson(xs: list[float], ys: list[float]) -> float:
    """MIN_COMMON_OBS 未満なら計算不能 (件数はメッセージに含めない)。"""
    if len(xs) < MIN_COMMON_OBS:
        raise ValueError("insufficient aligned observations for correlation")
    return _raw_corr(xs, ys)


def _validate_timeframe(timeframe: str) -> None:
    if timeframe not in TIMEFRAMES:
        raise ValueError("timeframe is not one of the enumerated values")


def _pick_peak(corrs: dict[int, float]) -> int:
    """lead_lag の peak 選択 (F3, Fix Round 1, sonnet 自己変異 M-S2 対策で
    独立関数へ抽出): 符号付き最大 corr の k、同点は |k| 最小 → さらに
    同点は k 昇順 (決定的 tie-break)。"""
    return max(corrs, key=lambda k: (corrs[k], -abs(k), -k))


# --- 相関 3 種 (低レベル API — 期間は呼び出し元が渡す) -----------------

def _corr_matrix_impl(conn: sqlite3.Connection, symbols: list[str], *,
                      timeframe: str, source: str, in_sample_until: datetime,
                      since: datetime | None = None,
                      ) -> tuple[dict[tuple[str, str], float], int]:
    _validate_timeframe(timeframe)
    returns = {s: _load_returns(conn, s, timeframe, source=source,
                                in_sample_until=in_sample_until, since=since)
              for s in symbols}
    result: dict[tuple[str, str], float] = {}
    for i in range(len(symbols)):
        for j in range(i + 1, len(symbols)):
            a, b = symbols[i], symbols[j]
            xs, ys = _align_same_time(returns[a], returns[b])
            # 1 ペアでも計算不能なら全体を ValueError (fail closed — §C):
            # 部分結果は多重比較の分母 (trial_count) を曖昧にする。
            result[(a, b)] = _pearson(xs, ys)
    return result, len(result)


def corr_matrix(conn: sqlite3.Connection, symbols: list[str], *,
                timeframe: str, source: str, in_sample_until: datetime,
                since: datetime | None = None,
                ) -> dict[tuple[str, str], float]:
    """symbols の全 2-組合せ (入力順で i < j) の log リターン相関 (ピアソン)。

    ``since`` (Task 11 上書き節 E): 指定時は ``since <= bar_time`` に限定する
    (人間 CLI の ``analyze corr --from`` 用。既定 None = 従来どおり全履歴)。
    """
    result, _ = _corr_matrix_impl(conn, symbols, timeframe=timeframe,
                                  source=source,
                                  in_sample_until=in_sample_until,
                                  since=since)
    return result


def _rolling_corr_summary_impl(conn: sqlite3.Connection, a: str, b: str, *,
                               timeframe: str, window: int, source: str,
                               in_sample_until: datetime,
                               ) -> tuple[dict[str, float], int]:
    _validate_timeframe(timeframe)
    if window not in WINDOWS:
        raise ValueError("window is not one of the enumerated values")
    ret_a = _load_returns(conn, a, timeframe, source=source,
                          in_sample_until=in_sample_until)
    ret_b = _load_returns(conn, b, timeframe, source=source,
                          in_sample_until=in_sample_until)
    xs, ys = _align_same_time(ret_a, ret_b)
    if len(xs) < MIN_COMMON_OBS:
        raise ValueError("insufficient aligned observations for correlation")
    n_windows = len(xs) - window + 1
    if n_windows < 2:
        raise ValueError("insufficient windows for rolling correlation")
    corrs = [_raw_corr(xs[i:i + window], ys[i:i + window])
            for i in range(n_windows)]
    summary = {
        "mean": statistics.mean(corrs),
        "std": statistics.stdev(corrs),
        "min": min(corrs),
        "max": max(corrs),
    }
    return summary, n_windows


def rolling_corr_summary(conn: sqlite3.Connection, a: str, b: str, *,
                         timeframe: str, window: int, source: str,
                         in_sample_until: datetime) -> dict[str, float]:
    """整列済み観測対列上の連続 window 対のスライド窓ごとに pearson を計算し、
    その系列の固定 4 統計のみを返す (``{mean, std, min, max}`` — 窓系列・
    件数・日時は返さない — §6 出力契約)。"""
    result, _ = _rolling_corr_summary_impl(
        conn, a, b, timeframe=timeframe, window=window, source=source,
        in_sample_until=in_sample_until)
    return result


def _lead_lag_impl(conn: sqlite3.Connection, a: str, b: str, *,
                   timeframe: str, source: str, in_sample_until: datetime,
                   ) -> tuple[dict[str, float], int]:
    _validate_timeframe(timeframe)
    width = _TF_MINUTES[timeframe]
    ret_a = _load_returns(conn, a, timeframe, source=source,
                          in_sample_until=in_sample_until)
    ret_b = _load_returns(conn, b, timeframe, source=source,
                          in_sample_until=in_sample_until)
    corrs: dict[int, float] = {}
    for k in LAGS:
        xs, ys = _align_lagged(ret_a, ret_b, lag_bars=k, width_minutes=width)
        # 全 lag のうち 1 つでも観測不足なら ValueError (分母 25 を固定 — §C)
        corrs[k] = _pearson(xs, ys)
    peak_lag = _pick_peak(corrs)
    return {"peak_lag": peak_lag, "peak_corr": corrs[peak_lag]}, len(LAGS)


def lead_lag(conn: sqlite3.Connection, a: str, b: str, *, timeframe: str,
            source: str, in_sample_until: datetime) -> dict[str, float]:
    """各 ``k ∈ LAGS`` について ``pearson(a_ret[t + k*width], b_ret[t])`` を
    計算し、符号付き最大の ``k`` (peak_lag) とその相関 (peak_corr) を返す。

    正の ``peak_lag`` = b が a に先行する (b の動きが a より早い) ことを
    意味する。返り値は ``{peak_lag, peak_corr}`` のみ。
    """
    result, _ = _lead_lag_impl(conn, a, b, timeframe=timeframe, source=source,
                               in_sample_until=in_sample_until)
    return result


# --- coverage_report (人間 CLI 用 — Task 11) ---------------------------

# F3 (最終レビュー opus I-2 must-fix): coverage_report だけが 1m を許可する。
# 本ブランチのインポータ (importer.py / mt5_import.py) は 1m しか書かない
# ので、TIMEFRAMES (15m/1h/4h/1d) しか受けない coverage_report では人間が
# 唯一投入されるデータを検査できなかった。spec §6 の列挙制は改善ループ向け
# API (analyze_for_agent 経由) の契約であり、coverage_report は人間 CLI 専用
# (§6 遮断の対象外、docstring どおり) なのでここだけ 1m を追加する。
# **TIMEFRAMES / _TF_MINUTES / 直後の assert は一切変更しない** — 改善ループ
# 面の列挙は不変 (ここを触るとモジュール import 時の assert が落ちる)。
_COVERAGE_TF_MINUTES = {"1m": 1, **_TF_MINUTES}


def coverage_report(conn: sqlite3.Connection, symbol: str, *, timeframe: str,
                    source: str, start: datetime, end: datetime) -> dict:
    """``{bars, expected_open_bars, gap_pct}``。

    ``expected_open_bars`` は ``bars`` (``load_resampled_frame`` の epoch
    錨バケット契約) と**同じバケット格子**で数える (レビュー Fix Round 1,
    codex Medium F4): バケット開始 ``t`` を ``floor_to_bucket(start,
    timeframe)`` から ``step`` (timeframe 幅) 刻みで進め、``t >= start``
    かつ ``t + step <= end`` (= ``bucket_end <= end``) を満たすものだけを
    候補にし、そのうち ``is_market_open(t)`` が True の個数を数える。
    ``start``/``end`` が timeframe の格子に整列している入力では、この
    走査は「``[start, end)`` を timeframe 幅で刻んだ各バー始点」を数える
    旧実装と同じ結果になる。**非整列な ``start``/``end`` では旧実装は
    ``load_resampled_frame`` (``since``/``until`` = ``bucket_start >=
    since`` かつ ``bucket_end <= until`` の epoch 錨契約) と格子がずれ、
    データが完全に揃っていても偽の欠損率を報告し得た** (例:
    12:30〜14:30・1h では、実際に判定対象となるのは epoch 錨バケット
    [13:00,14:00) の 1 本のみだが、旧実装は ``[12:30,14:30)`` を 1h ごとに
    刻んで 2 本と数えていた)。

    ``bars`` は プラン 7 Task 0 で「同じ範囲に実在する ohlcv 行の総数
    (COUNT(*))」から「``load_resampled_frame(..., since=start, until=end)``
    が返す完成リサンプル・バケットの本数」へ切り替えた (本ブランチの
    インポータは 1m しか書かないため、旧 COUNT(*) は interval=timeframe
    (例: "1h") の行を素通りで数えており、1m 化以降は常に 0 になっていた)。
    市場時間外に紛れ込んだ行があれば ``gap_pct`` が負になり得るが、これは
    それ自体データ品質の異常信号なので人間 CLI 向けにそのまま見せる
    (隠さない判断 — この振る舞いは resample 切り替え後も変わらない)。
    ``expected_open_bars`` が 0 なら計算不能として ValueError (fail
    closed)。start/end は naive なら ValueError (watch 銘柄選定基準③ を
    人間が判定するための関数 — 改善ループには露出しないため §6 遮断の
    対象外)。

    ``timeframe`` は ``TIMEFRAMES`` (改善ループ向け列挙) に加えて ``"1m"``
    も受け付ける (F3, 最終レビュー opus I-2) — 本ブランチのインポータが
    書くのは 1m のみのため (``load_resampled_frame`` は "1m" を素通しで
    返す)。
    """
    if timeframe not in _COVERAGE_TF_MINUTES:
        raise ValueError("timeframe is not one of the enumerated values")
    start_utc = as_utc(start)
    end_utc = as_utc(end)
    step = timedelta(minutes=_COVERAGE_TF_MINUTES[timeframe])
    expected_open_bars = 0
    # F4 (レビュー Fix Round 1, codex Medium): actual (load_resampled_frame)
    # と同じ epoch 錨バケット格子を歩く — start からではなく
    # floor_to_bucket(start) から刻み、t>=start かつ t+step<=end (=
    # bucket_end<=end) のバケットだけを候補にする。
    t = floor_to_bucket(start_utc, timeframe)
    while t + step <= end_utc:
        if t >= start_utc and is_market_open(t):
            expected_open_bars += 1
        t += step
    if expected_open_bars == 0:
        raise ValueError("expected_open_bars is zero (empty or fully closed "
                         "range)")
    df = load_resampled_frame(conn, symbol, timeframe, source=source,
                              since=start_utc, until=end_utc)
    bars = len(df)
    gap_pct = (expected_open_bars - bars) / expected_open_bars * 100
    return {"bars": bars, "expected_open_bars": expected_open_bars,
            "gap_pct": gap_pct}


# --- analyze_for_agent (改善ループへ露出する唯一の面) -------------------

_REQUEST_SCHEMA = {
    "corr_matrix": {"kind", "timeframe"},
    "rolling_corr_summary": {"kind", "a", "b", "timeframe", "window"},
    "lead_lag": {"kind", "a", "b", "timeframe"},
}


def analyze_for_agent(conn: sqlite3.Connection, settings: Settings,
                      request: dict, *, now: datetime) -> dict:
    """改善ループ (プラン 9) に露出する唯一の分析面。

    ``in_sample_until = holdout.in_sample_until(now, settings.backtest.
    holdout_months)`` (F1, 最終レビュー opus I-1 — UTC 正規化 + 分格子切り
    捨て込みの境界算術の単一所有者) を内部で適用する。symbols は
    ``settings.pairs + settings.datafeed.
    watch_symbols`` 内に限定 (重複除去・順序維持)。実行毎に
    ``analysis_runs.save`` し、返り値に ``analysis_run_id`` を含める
    (成功時のみ — 計算していない呼び出しを分母に入れない)。

    ハーネス側引数の欠陥 (naive now、settings 側の候補数超過) は request
    由来ではないので例外送出 (fail closed)。request 由来の失敗は固定コード
    ``{"error": <code>}`` のみを返す (無送出)。

    許容漏洩の明文化 (F6, Fix Round 1 — コントローラ裁定 codex Important-3):
    応答コードの違い (成功 / invalid_request / unknown_symbol /
    insufficient_data) から「閾値を満たすか否か」の 1 ビットが観測できる
    ことは、エラー応答を返す設計上不可避であり許容する。§6 の遮断対象は
    期間・端点・件数の**値そのもの** (メッセージ・追加キーに含めないこと)
    であって、応答コードの分岐自体ではない。問い合わせ回数の予算・
    レート制限は改善ループ tool 側 (プラン 9) のスコープであり、この
    モジュールの責務ではない。
    """
    now_utc = as_utc(now)  # naive now はハーネス側の欠陥 → 例外 (fail closed)
    candidates = list(dict.fromkeys(list(settings.pairs)
                                    + list(settings.datafeed.watch_symbols)))
    max_candidates = settings.analysis.max_watch_symbols + len(settings.pairs)
    if len(candidates) > max_candidates:
        # settings 側の候補数超過もハーネス側の欠陥 → 例外 (fail closed)。
        # Task 2 のバリデータで通常は防がれるが、実行時にも再検証する。
        raise ValueError("candidate symbol count exceeds configured maximum")

    if not isinstance(request, dict):
        return {"error": "invalid_request"}
    kind = request.get("kind")
    if not isinstance(kind, str) or kind not in _REQUEST_SCHEMA:
        # kind が非 hashable (list 等) だと `in` の hashing で TypeError に
        # なり得る — request 由来の失敗は無送出契約 (§D) なので、まず型を
        # 検証してから列挙判定する。
        return {"error": "invalid_request"}
    if set(request.keys()) != _REQUEST_SCHEMA[kind]:
        return {"error": "invalid_request"}
    timeframe = request["timeframe"]
    if timeframe not in TIMEFRAMES:
        return {"error": "invalid_request"}
    window = None
    if kind == "rolling_corr_summary":
        window = request["window"]
        if window not in WINDOWS:
            return {"error": "invalid_request"}
    a = b = None
    if kind != "corr_matrix":
        a, b = request["a"], request["b"]
        if a == b:
            return {"error": "invalid_request"}
        if a not in candidates or b not in candidates:
            return {"error": "unknown_symbol"}

    # F1 (最終レビュー opus I-1 是正): 境界算術は holdout.in_sample_until が
    # 単一所有する (UTC 正規化 + 分格子切り捨てを含む) — ここで
    # holdout_boundary を直接呼ばない (run_in_sample/run_holdout_gate と
    # 同じ経路で境界を得ることで、同じ now に対する境界のずれを無くす)。
    in_sample_until = _in_sample_until(now_utc,
                                       settings.backtest.holdout_months)

    try:
        if kind == "corr_matrix":
            result, trial_count = _corr_matrix_impl(
                conn, candidates, timeframe=timeframe, source=ANALYSIS_SOURCE,
                in_sample_until=in_sample_until)
            if trial_count == 0:
                # 候補が 2 未満、またはペアが計算できなかった —
                # 計算対象そのものが無いので insufficient_data 扱い。
                raise ValueError("no correlation pairs to compute")
            payload_body: dict[str, Any] = {
                "pairs": {f"{p[0]}/{p[1]}": v for p, v in result.items()}}
        elif kind == "rolling_corr_summary":
            result, trial_count = _rolling_corr_summary_impl(
                conn, a, b, timeframe=timeframe, window=window,
                source=ANALYSIS_SOURCE, in_sample_until=in_sample_until)
            payload_body = dict(result)
        else:  # lead_lag
            result, trial_count = _lead_lag_impl(
                conn, a, b, timeframe=timeframe, source=ANALYSIS_SOURCE,
                in_sample_until=in_sample_until)
            payload_body = dict(result)
    except (ValueError, ArithmeticError):
        # データ不足 (相関計算に必要な共通観測が閾値未満) は in-sample 境界
        # の前後どちら起因でも同一の insufficient_data (サイドチャネル遮断
        # — §6)。ArithmeticError (F1, Fix Round 1): ZeroDivisionError /
        # OverflowError は防御の深層 (`_load_returns` の正値ガードが主防御
        # だが、想定外経路からの到達に備えて改善ループ面への生例外漏洩を
        # 二重に塞ぐ)。
        return {"error": "insufficient_data"}

    run_id = analysis_runs_store.save(
        conn, params={"request": dict(request),
                      "in_sample_until": in_sample_until.isoformat()},
        trial_count=trial_count, source=ANALYSIS_SOURCE, now=now_utc)
    return {"analysis_run_id": run_id, **payload_body}
