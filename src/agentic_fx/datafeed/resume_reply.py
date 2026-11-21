"""`data resume` の応答文。ctl と対話 shell で同じ文を使う。

要求の記録は再開ではない。新規・照会 (既に記録済み)・ack の置換を区別し、
いずれも「次の開場中の判定待ち」と案内する。
"""
from __future__ import annotations

OUTCOMES = ("new", "existing", "replaced")

_WAIT = "次の開場中の tick の判定待ちです。"


def resume_reply(outcome: str, acknowledge: bool) -> str:
    """応答の本文 (未処理建玉の詳細は呼出し側が後ろに足す)。

    ``acknowledge`` は要求が持つ有効な ack。照会では既存の要求と ack を保ったことを示す。
    """
    mark = " (--acknowledge 指定あり)" if acknowledge else ""
    if outcome == "existing":
        return ("resume 要求は既に記録済みです。要求時刻と --acknowledge の指定は保ったままです"
                + mark + "。" + _WAIT)
    if outcome == "replaced":
        return "先の要求を --acknowledge 付き要求に置き換えました。" + _WAIT
    return "resume 要求を記録しました。" + _WAIT + mark
