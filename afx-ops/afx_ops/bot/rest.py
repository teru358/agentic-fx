"""Discord REST の失敗を、呼出しの文脈と status / JSON error code で分類する。

例外の型 (`NotFound` など) や HTTP 404 だけでは、投稿の消失とも恒久の拒否とも決めない。
code・本文・ID・例外文は返さず、分類の結果 (固定語) だけを返す。
"""
from __future__ import annotations

import asyncio

import aiohttp
import discord

PERMANENT = "permanent"
TRANSIENT = "transient"

# 既知の投稿 (message) が無い
UNKNOWN_MESSAGE = 10008

# 共有の配送先・権限の拒否 (channel が無い / 見えない / 投稿できない)。HTTP status が 400 でも
# 一投稿に閉じた失敗ではなく、設定の修復が要る
SHARED_REJECTION_CODES = frozenset({10003, 50001, 50013})

# 一つの message の body が不正 (長さ超過など)。その message に閉じた拒否で、設定の修復は要らない
INVALID_BODY = 50035

# 一つの REST 呼出しが失敗として返し得る例外。network / timeout は一時
REST_ERRORS = (discord.HTTPException, OSError, asyncio.TimeoutError, aiohttp.ClientError)


def is_message_gone(exc: BaseException) -> bool:
    """fetch / edit に限り、404 かつ code 10008 だけが既知の投稿の消失。"""
    return (isinstance(exc, discord.HTTPException) and exc.status == 404
            and exc.code == UNKNOWN_MESSAGE)


def is_message_local_rejection(exc: BaseException) -> bool:
    """送った message 固有の不正な body (400 かつ code 50035)。他の 400 は含めない。"""
    return (isinstance(exc, discord.HTTPException) and exc.status == 400
            and exc.code == INVALID_BODY)


def _is_codeless_gateway_reply(exc: discord.HTTPException) -> bool:
    """Discord の JSON code を持たない 403 / 408 (前段の proxy や Cloudflare の一時の拒否)。"""
    return exc.status in (403, 408) and not exc.code


def is_shared_rejection(exc: BaseException) -> bool:
    """共有の配送先・認証・権限の拒否か。code を先に見て、次に 401 / 403 / 404 を見る。

    404 は投稿の消失 (`is_message_gone`) を呼出し側が先に除いた後の、その他の 404。message 局所の
    400 などはここに入らない。
    """
    if not isinstance(exc, discord.HTTPException):
        return False
    if _is_codeless_gateway_reply(exc):
        return False
    return exc.code in SHARED_REJECTION_CODES or exc.status in (401, 403, 404)


def classify(exc: BaseException) -> str:
    """恒久 (設定・認証・権限の修復が要る拒否) か一時 (429 / 5xx / network / timeout) か。

    共有の配送先の code (10003 / 50001 / 50013) は status によらず恒久。401 / 403 / 404 と、
    429 以外の 4xx も恒久。ただし JSON code を持たない 403 と 408 は前段の一時の拒否として
    一時にする。投稿の消失 (`is_message_gone`) の判定は呼出し側が先に行う。判別できない失敗は
    一時にする。message 固有の不正な body (`is_message_local_rejection`) の扱いは送信側が決める。
    """
    if isinstance(exc, discord.LoginFailure):
        return PERMANENT
    if isinstance(exc, discord.HTTPException):
        if exc.code in SHARED_REJECTION_CODES:
            return PERMANENT
        if _is_codeless_gateway_reply(exc):
            return TRANSIENT
        status = exc.status
        if 400 <= status < 500 and status != 429:
            return PERMANENT
    return TRANSIENT
