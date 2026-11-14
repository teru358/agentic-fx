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

# 一つの REST 呼出しが失敗として返し得る例外。network / timeout は一時
REST_ERRORS = (discord.HTTPException, OSError, asyncio.TimeoutError, aiohttp.ClientError)


def is_message_gone(exc: BaseException) -> bool:
    """fetch / edit に限り、404 かつ code 10008 だけが既知の投稿の消失。"""
    return (isinstance(exc, discord.HTTPException) and exc.status == 404
            and exc.code == UNKNOWN_MESSAGE)


def classify(exc: BaseException) -> str:
    """恒久 (設定・認証・権限の修復が要る拒否) か一時 (429 / 5xx / network / timeout) か。

    401 / 403 / 404 と、429 以外の 4xx は恒久。投稿の消失 (`is_message_gone`) の判定は呼出し側が
    先に行う。判別できない失敗は一時にする。
    """
    if isinstance(exc, discord.LoginFailure):
        return PERMANENT
    if isinstance(exc, discord.HTTPException):
        status = exc.status
        if 400 <= status < 500 and status != 429:
            return PERMANENT
    return TRANSIENT
