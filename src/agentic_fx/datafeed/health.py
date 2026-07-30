"""データ健全性検証 — 「取得成功」でなくこの検証の通過がフォールバック採用条件 (設計書 §5)。

バー timestamp はバーの開始時刻とする。"""
from __future__ import annotations

import math
from datetime import datetime, timedelta

from agentic_fx.core import market_hours
from agentic_fx.core.contracts import Bar, ConversionRate, Quote
from agentic_fx.core.timeutil import as_utc

_MAX_GAP_BARS = 3
_SPIKE_PCT = 10.0
# 未来時刻の許容幅 (分)。ホスト時計のわずかなずれを吸収するための小さな値。
# バーの ts はバー開始時刻であり、形成中のバーであっても開始時刻は常に
# 過去である (この不変条件が破れる = 時刻情報が壊れている、という意味)。
# MT5 bridge はサーバのローカル時刻を無条件に UTC として解釈しており、
# ブローカーのサーバ時刻が UTC より進んでいると未来時刻の timestamp が
# 混入し得る。既存の stale (負方向) 検査だけでは正方向のずれを検出できない
# (Task 2 修正ラウンド 1: 指摘 3 — このファイル内の「修正ラウンド 1/2」
# ラベルは祝日ギャップ回帰などの別レビューのものであり、本定数とは無関係)。
_MAX_FUTURE_MIN = 2.0
# 休場時間サンプリングの上限回数。超過する場合は「数え切れない = 健全と
# 断言できない」として fail closed (DataUnhealthy) にする (修正ラウンド 1: 指摘 1)。
_MAX_CLOSED_TIME_SAMPLES = 100_000
# 連続性 (gap) 検査の対象を末尾からの窓に限定する (修正ラウンド 2: 指摘 —
# 祝日隣接週の回帰)。
#
# market_hours は祝日カレンダーを持たず週次の開場パターンしか判定できないため、
# 祝日で実際には休場だった平日時間帯を「開場中の欠損」として検出してしまう
# (例: 12/25 木曜のクリスマスは平日扱いのまま)。バー列全体を毎回走査すると、
# この誤検出が「その穴がバー列の窓に残っている間ずっと」健全性判定を
# 落とし続けてしまう。
#
# gap 検査が答えるべき問いは「フィードは “いま” ちゃんと動いているか」であり、
# 過去に穴があっても以後データが連続して回復していれば feed 自体は健全と
# 判断してよい。そこで検査対象を末尾直近の窓に絞る。
#
# 窓の幅は「バー本数」と「wall-clock 時間の下限」の大きい方
# (`_GAP_CHECK_WINDOW_BARS * interval_min` 分 と `_GAP_CHECK_WINDOW_MIN_MINUTES`
# 分の max) で決める。バー本数だけで決めると、1m 足では 24 本 = 24 分しか
# ならず、フィード障害から 25 分程度で回復しただけで「もう健全」と誤判定
# してしまう (advisor レビューで指摘)。逆に wall-clock 下限だけだと日足以上の
# 粗い足で窓が 1 本未満になりかねないため、下限をバー本数でも設けている。
#
# 値の根拠:
# - 指摘 1 (Critical) の再現ケース (金曜最後のバー + 月曜のバー) は
#   穴がバー列の末尾に隣接しており、窓の広さによらず常に窓内に入るため
#   検出され続ける (この定数を弱めても Critical の検出力は落ちない)。
# - 祝日ギャップは、休場明けから概ね 1 日分の新しい定時データが蓄積されれば
#   窓の外に押し出され、健全性判定への影響が消える。取引判断 loop は
#   1 時間毎に実行される (設計書 §4) ため、1h 足なら丸 1 日、より粗い
#   足でも概ね数日以内に通常運転へ復帰できることを優先し 24h とした。
#   **ただし裏を返せば、祝日を挟んだ週は復帰まで最大 ~24h は健全性判定に
#   落ち続けるということでもある** (今回のスコープでは祝日カレンダーを
#   導入しないための意図的なトレードオフ。許容できない場合はプラン 3
#   完了後に祝日カレンダー導入を検討)。
# - 小さすぎると一時的なブローカー障害の検出漏れにつながるため、
#   判断 loop の実行間隔に対して十分な余裕を持たせた。
# - 祝日カレンダーの導入自体はスコープ外 (別途プラン 3 完了後に判断)。
_GAP_CHECK_WINDOW_BARS = 24
_GAP_CHECK_WINDOW_MIN_MINUTES = 24 * 60  # 24 時間 (sub-hour 足でも下限を保証)


class DataUnhealthy(Exception):
    pass


def validate_quote(quote: Quote, now: datetime,
                   freshness_max_min: float) -> None:
    now = as_utc(now)
    ts = as_utc(quote.ts)
    if now - ts > timedelta(minutes=freshness_max_min):
        raise DataUnhealthy(f"quote stale: {ts} (source={quote.source})")
    if ts - now > timedelta(minutes=_MAX_FUTURE_MIN):
        raise DataUnhealthy(
            f"quote timestamp in the future: {ts} (now={now}, "
            f"source={quote.source})")
    if not (quote.bid > 0 and quote.ask > 0 and
            math.isfinite(quote.bid) and math.isfinite(quote.ask)):
        raise DataUnhealthy("quote has non-positive/NaN price")
    if quote.bid > quote.ask:
        raise DataUnhealthy(f"bid/ask inverted: {quote.bid} > {quote.ask}")


def _closed_minutes(a: datetime, b: datetime, interval_min: float) -> float | None:
    """[a, b] のうち市場クローズだった時間 (分) を interval_min 刻みでサンプリング
    して推定する。刻み幅はバーの足種 (1m/5m/15m/... ) に追従させる (固定 1 時間
    刻みだと sub-hour 足の休場境界を誤判定するため)。

    サンプル数が上限を超える場合は None を返す (fail closed: 呼び出し側で
    健全性を断言しない)。
    """
    step_min = max(interval_min, 1.0)
    total_min = (b - a).total_seconds() / 60
    if total_min <= 0:
        return 0.0
    n_samples = int(total_min / step_min) + 2
    if n_samples > _MAX_CLOSED_TIME_SAMPLES:
        return None
    step = timedelta(minutes=step_min)
    cur = a
    closed = 0
    samples = 0
    while cur <= b:
        samples += 1
        if not market_hours.is_market_open(cur):
            closed += 1
        cur += step
    if samples == 0:
        return 0.0
    return closed / samples * total_min


def validate_conversion_skew(rate: ConversionRate, *, reference_ts: datetime,
                             max_skew_min: float) -> None:
    """換算レートの脚間・判断内スナップショットの時刻差を検証する (設計書 §5)。

    各脚の**鮮度**自体は脚を取得する quote の `validate_quote` 呼び出しで
    既に検証済み (fail closed)。ここで見るのは残り 2 つ:
    ①クロス (2 脚) の脚同士の時刻差 ②この換算レートと、それが使われる
    判断 (gate 評価・予約再検証サイクル) の基準時刻 (`reference_ts`、
    通常は判断に使う quote の ts か tick の `now`) との時刻差。
    2 脚しかない場合でも `reference_ts` を含めた 3 点の最大-最小で両方を
    一度に検証できる (直接・逆数ペアの 1 脚だけの場合は reference_ts との
    差のみが効く)。超過は fail closed (DataUnhealthy)。
    """
    times = [as_utc(t) for t in rate.leg_ts] + [as_utc(reference_ts)]
    span = max(times) - min(times)
    if span > timedelta(minutes=max_skew_min):
        raise DataUnhealthy(
            f"conversion rate skew {span} exceeds {max_skew_min}min "
            f"({rate.from_ccy}->{rate.to_ccy})")


def validate_bars(bars: list[Bar], now: datetime, freshness_max_min: float,
                  interval_min: float) -> None:
    if not bars:
        raise DataUnhealthy("empty bars")
    now = as_utc(now)
    last_ts = as_utc(bars[-1].ts)
    # ts はバー開始時刻: 確定直後を stale にしないため interval 分を許容に足す
    allowed = timedelta(minutes=freshness_max_min + interval_min)
    if now - last_ts > allowed:
        raise DataUnhealthy(f"bars stale: last={last_ts}")
    # gap (連続性) 検査は末尾直近の窓に限定する。zero/NaN・スパイク検査は
    # バー個々のデータ整合性の話であり窓の対象外 (バー列全体で検査する)。
    gap_window_minutes = max(_GAP_CHECK_WINDOW_BARS * interval_min,
                             _GAP_CHECK_WINDOW_MIN_MINUTES)
    gap_window_start = last_ts - timedelta(minutes=gap_window_minutes)
    prev_ts: datetime | None = None
    prev_close: float | None = None
    for b in bars:
        ts = as_utc(b.ts)
        # 未来時刻の検査は末尾バーだけでなく全バーに対して行う (ソースが
        # 途中に未来時刻を混ぜるケースを取りこぼさないため。指摘 3)。
        if ts - now > timedelta(minutes=_MAX_FUTURE_MIN):
            raise DataUnhealthy(
                f"bar timestamp in the future: {ts} (now={now})")
        vals = (b.open, b.high, b.low, b.close)
        if any(v <= 0 or not math.isfinite(v) for v in vals):
            raise DataUnhealthy(f"anomalous bar (zero/NaN) at {ts}")
        if prev_ts is not None:
            gap_units = (ts - prev_ts).total_seconds() / 60 / interval_min
            if gap_units > _MAX_GAP_BARS and ts >= gap_window_start:
                # 区間全体を免除するのではなく、休場だった時間だけを差し引き、
                # 残り (開場中のはずの欠損) が閾値を超えるかで判定する
                # (修正ラウンド 1: 指摘 1 — 丸ごと免除は fail-open だった)。
                closed_min = _closed_minutes(prev_ts, ts, interval_min)
                if closed_min is None:
                    raise DataUnhealthy(
                        f"gap of {gap_units:.0f} bars before {ts} "
                        "(unable to verify market hours within sample limit)")
                open_gap_units = gap_units - closed_min / interval_min
                if open_gap_units > _MAX_GAP_BARS:
                    raise DataUnhealthy(
                        f"gap of {open_gap_units:.0f} open-market bars before {ts}")
            move = abs(b.close - prev_close) / prev_close * 100
            if move > _SPIKE_PCT:
                raise DataUnhealthy(f"anomalous spike {move:.1f}% at {ts}")
        prev_ts = ts
        prev_close = b.close
