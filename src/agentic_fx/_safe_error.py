"""例外を「外部に出してよい」文字列にする共通ヘルパー。

datafeed の各モジュール (price_provider / news_collector / econ_calendar) と、
それらを起動する core/service は
外部 HTTP を叩くため、例外文字列にリクエスト URL がそのまま載る
(`httpx.HTTPStatusError.__str__` は URL を含む)。これらのメッセージは
技術ログだけでなく mission 記録・activity・Discord 通知に載りうるので、
**URL を出さない**形に落としてから使う。

以前は price_provider と news_collector に同じ関数が複製されていた
(「private ヘルパーはモジュール間で import せず複製する」方針)。3 つ目の
複製が生じる時点でその方針は割に合わない — 秘密抑止のパターンは
1 箇所で更新できないと片側だけ古いままになり、抑止の穴は無音で残る。

**配置**: 当初は `datafeed/` に置いていたが、`core/scheduler.py` (データ経路の
障害を握って技術ログに残す) と `service.py` (init の標準出力) も使うため、
パッケージ直下へ移した。`core` が `datafeed` を import するのは層の逆転で、
`datafeed/__init__.py` に import が足された時点で本物の循環参照になる
(`datafeed/econ_calendar.py` は既に `core.contracts` を import している)。
このモジュールは `re` と `httpx` にしか依存しないので、どの層からでも安全に使える。
"""
from __future__ import annotations

import re

import httpx

# 例外メッセージから伏字にする秘密のパターン (多層防御)。Twelve Data は
# apikey をクエリパラメータで送る仕様なので、httpx の例外文字列に URL ごと
# 載る。URL 抑止 (下の型別分岐) を擦り抜けた経路でもここで止める。
_SECRET_RE = re.compile(r"((?:api[-_]?key|apikey|token|secret)=)[^&\s'\"]+",
                        re.IGNORECASE)


def safe_error_text(e: BaseException) -> str:
    """例外を「外部に出してよい」文字列にする (ログ・DataUnhealthy 共通)。

    httpx 由来は **URL を出さない** (秘密が載る) が、status code は診断に
    要るので残す。自前の例外 (DataUnhealthy / ValueError 等) は URL を
    含まないので情報量を落とさない。
    """
    if isinstance(e, httpx.HTTPStatusError):
        text = f"{type(e).__name__}: HTTP {e.response.status_code}"
    elif isinstance(e, httpx.HTTPError):
        # ConnectError 等。message に URL が載る実装があるので型名だけにする
        text = type(e).__name__
    else:
        text = f"{type(e).__name__}: {e}"
    return _SECRET_RE.sub(r"\1***", text)
