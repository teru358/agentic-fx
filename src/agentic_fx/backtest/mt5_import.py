"""MT5 bridge 一括インポータ + 価格系差照合。

MT5 bridge (`GET {base}/ohlcv/{sym}?from=ISO&to=ISO&interval=1m`) から
1 分足を 1 日窓でページングして取り込み (`import_bars(source="mt5")`)、
Dukascopy 等の他 source と重複期間の close 差を照合する。

MT5 は bid 系列 — mid 近似としてそのまま保存する (spec §6 の但し書きどおり。
spread=None)。照合時は `compare_sources` 側で assumed half-spread を引いて
Dukascopy 側 (mid 系列) と揃える。
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import httpx

from agentic_fx.datafeed.price_provider import _SPECS
from agentic_fx.datafeed.sources import _mt5_headers
from agentic_fx.store.ohlcv import ImportResult, import_history_bars

_log = logging.getLogger(__name__)


def _default_fetch(url: str) -> dict:
    """既定 fetch。httpx.get + `X-Bridge-Api-Key` ヘッダ + `raise_for_status`。

    Dukascopy と異なり 404 は MT5 bridge では想定外 (欠損日は
    `bars: []` の 200 で返る実測仕様) — 素通しで raise する。
    URL・ヘッダ (API キー) はログに出さない。
    """
    resp = httpx.get(url, headers=_mt5_headers(), timeout=30)
    resp.raise_for_status()
    return resp.json()


def _window_url(base_url: str, symbol: str, start: datetime, end: datetime) -> str:
    return (f"{base_url}/ohlcv/{symbol}?from={quote(start.isoformat())}"
            f"&to={quote(end.isoformat())}&interval=1m")


def _normalize_bar_time(raw: str) -> tuple[str, bool]:
    """bridge の "time" は naive ISO の可能性がある。naive なら UTC とみなし
    (実測仕様。§ 上書き 3)、aware isoformat 文字列に正規化する。

    これを怠ると bar_time の文字列表現が Dukascopy 行 ("+00:00" 付き) と
    食い違い、compare_sources の bar_time JOIN が 0 件になる無音故障を
    起こす。

    Returns:
        (正規化済み isoformat 文字列, naive だったか)。呼び出し側 (import_mt5)
        は naive 件数を集計し 1 回だけ warning を出す (F2, fix round 1)。
        同じ bridge の時刻系統が実際に破損した実績があり、将来 bridge が
        naive 応答を返し始めたときに無音で UTC 仮定され続けるのを防ぐ。
    """
    dt = datetime.fromisoformat(raw)
    was_naive = dt.tzinfo is None
    if was_naive:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt.isoformat(), was_naive


def import_mt5(conn, symbol: str, start: datetime, end: datetime, *,
               base_url: str, fetch=None) -> ImportResult:
    """MT5 bridge から 1 分足を 1 日窓でページングして取り込む。

    Args:
        conn: sqlite3 connection
        symbol: 取引ペア
        start: 開始 (inclusive) — aware UTC 必須 (naive は ValueError)
        end: 終了 (exclusive) — aware UTC 必須 (naive は ValueError)
        base_url: bridge のベース URL
        fetch: Optional fetch function (url: str) -> dict。既定は
            `_default_fetch` (httpx + raise_for_status)。

    Returns:
        ImportResult (inserted/unchanged/conflicted 合算)。

    Raises:
        ValueError: start/end が naive、または UTC 以外の場合 (正時境界は
            不問 — Task 4 の hour boundary 要件はここには適用しない)。
            または bridge が返したバーの time が要求窓の外にある場合
            (F5, 最終レビュー codex I2 — fail loud)。**窓終端ちょうどは
            例外にせず黙って落とす** — bridge の `to` は inclusive で
            (実機実測)、その 1 本は次のページング窓が取り直すため欠損しない。
            最終窓の右端だけは落ちるが `end` は exclusive なのでそれが正しい。
    """
    for dt, name in [(start, "start"), (end, "end")]:
        if dt.tzinfo is None:
            raise ValueError(
                f"{name} must be timezone-aware; got naive datetime")
        if dt.tzinfo != timezone.utc:
            raise ValueError(f"{name} must be UTC; got {dt.tzinfo}")

    if fetch is None:
        fetch = _default_fetch

    total_inserted = 0
    total_unchanged = 0
    total_conflicted = 0
    naive_count = 0
    right_edge_drops = 0

    current = start
    while current < end:
        window_end = min(current + timedelta(days=1), end)
        url = _window_url(base_url, symbol, current, window_end)
        payload = fetch(url)

        # payload["bars"] を必須で読む (F: sources.py:mt5_bars_range と同じ
        # fail-loud パターン)。.get(..., []) にすると HTTP 200 でエラー body
        # を返す bridge 障害時に「0 件」と区別が付かなくなる。
        rows = []
        for b in payload["bars"]:
            bar_time_iso, was_naive = _normalize_bar_time(b["time"])
            if was_naive:
                naive_count += 1
            # F5 (最終レビュー codex I2): 正規化後の timestamp を現在の取得窓
            # [current, window_end) に対して検証する。bridge が "to" を
            # inclusive 解釈した場合の境界重複や、bridge の不具合・キャッシュ
            # 汚染による窓外行の無言混入を防ぐ (fail loud — import_bars の
            # 既存行不変性は値の上書きを防ぐだけで、窓外の新規キー挿入は
            # 防がない)。
            bar_dt = datetime.fromisoformat(bar_time_iso)
            if bar_dt == window_end:
                # bridge は "to" を **inclusive** で返す (2026-08-12 実機実測:
                # 1 日窓に対し先頭 T00:00・末尾は翌 T00:00 が含まれ 1440 本)。
                # 終端ちょうどは「次窓の開始」であって不具合ではないので
                # 落とす。次のページング窓が current=window_end で取り直すため
                # **欠損しない** (最終窓の右端だけは落ちるが、end は exclusive
                # なのでそれが正しい)。
                #
                # 旧実装はここも fail loud にしており、境界に 1 本乗るだけで
                # **取り込み全体が失敗**していた (実機で 1440 本が 1 本も
                # 入らなかった)。窓外の無言混入を防ぐ F5 の目的は下の判定で維持。
                right_edge_drops += 1
                continue
            if not (current <= bar_dt < window_end):
                raise ValueError(
                    f"import_mt5: symbol={symbol!r} のバー time={b['time']!r} "
                    f"が要求窓 [{current.isoformat()}, {window_end.isoformat()}) "
                    "の外です (bridge の不具合の可能性)")
            rows.append((symbol, "1m", bar_time_iso,
                        float(b["open"]), float(b["high"]), float(b["low"]),
                        float(b["close"]), float(b["volume"]), None))
        if rows:
            result = import_history_bars(conn, rows, source="mt5")
            total_inserted += result.inserted
            total_unchanged += result.unchanged
            total_conflicted += result.conflicted

        current = window_end

    if right_edge_drops > 0:
        # 期待どおりの挙動なので debug。ただし**件数は残す** — bridge が
        # exclusive へ変わればここが 0 になり、逆に窓数より大きく増えれば
        # bridge 側の異常を疑える。
        _log.debug(
            "import_mt5: dropped %d right-edge bar(s) at window_end for "
            "symbol=%s (bridge returns \"to\" inclusive; picked up by the "
            "next window)", right_edge_drops, symbol)

    if naive_count > 0:
        # F2 (fix round 1, sonnet): 同じ bridge の時刻系統が実際に破損した
        # 直後であり、将来 bridge が naive 応答を返し始めたときに無音で UTC
        # 仮定され続けるのを防ぐ。毎バーではうるさいので import_mt5 呼び出し
        # 単位で 1 回だけ warning する。
        _log.warning(
            "import_mt5: %d/%d bar(s) had naive \"time\" (assumed UTC) for "
            "symbol=%s — bridge may be returning tz-less timestamps",
            naive_count, total_inserted + total_unchanged + total_conflicted,
            symbol)

    return ImportResult(total_inserted, total_unchanged, total_conflicted)


def compare_sources(conn, symbol: str, settings, *,
                    a: str = "dukascopy", b: str = "mt5") -> dict:
    """両 source が重複する期間の close 差を集計する (人間 CLI / 報告用)。

    `a` は mid 系列 (Dukascopy) を仮定し、`assumed_half_spread` を引いてから
    `b` (MT5, bid 系列) と比較する。`assumed_half_spread` は
    `settings.risk.pair_rules[symbol].assumed_spread_pips` から
    `_SPECS[symbol].pip_size` で換算する。symbol が pair_rules または _SPECS
    に無い場合は KeyError (文字列推測禁止)。

    Returns:
        {"count", "mean", "std", "max_abs"}。重複期間が 0 件なら
        {"count": 0, "mean": None, "std": None, "max_abs": None}。
    """
    pip_size = _SPECS[symbol].pip_size
    assumed_spread_pips = settings.risk.pair_rules[symbol].assumed_spread_pips
    half_spread = assumed_spread_pips * pip_size / 2

    cur = conn.execute(
        "SELECT ta.close AS a_close, tb.close AS b_close "
        "FROM ohlcv_history ta JOIN ohlcv_history tb "
        "ON ta.symbol = tb.symbol AND ta.interval = tb.interval "
        "AND ta.bar_time = tb.bar_time "
        "WHERE ta.symbol = ? AND ta.interval = '1m' "
        "AND ta.source = ? AND tb.source = ?",
        (symbol, a, b))

    # F3 (fix round 1, codex Important-1): ストリーミング集計 (Welford 法)。
    # 従来は全行を rows/diffs の 2 本の list に丸ごと実体化していたため、
    # 数年分の 1m 照合ではメモリを不要に食う。カーソルを 1 行ずつ回し
    # count/mean/m2 (分散の中間値)/max_abs を逐次更新する。返り値の契約
    # (count/mean/std/max_abs、count=0→全 None、std は母標準偏差) は不変
    # — count=1 のとき Welford は m2=0.0 を厳密に生成するため std=0.0 も
    # 特別扱い無しで成立する (数学的に等価)。
    count = 0
    mean = 0.0
    m2 = 0.0
    max_abs = 0.0
    for r in cur:
        d = (r["a_close"] - half_spread) - r["b_close"]
        count += 1
        delta = d - mean
        mean += delta / count
        m2 += delta * (d - mean)
        ad = abs(d)
        if ad > max_abs:
            max_abs = ad

    if count == 0:
        return {"count": 0, "mean": None, "std": None, "max_abs": None}

    std = (m2 / count) ** 0.5
    return {"count": count, "mean": mean, "std": std, "max_abs": max_abs}
