"""認証済み主体から呼ぶ、HTTP 非依存の運用操作層。

UDS listener は :class:`OpsService` の公開メソッドへ principal と
scope を渡すだけにする。ここでは鍵や socket を扱わない。
"""
from .contracts import ErrorCode, OpsError, Principal, Scope, assert_authorized
from .service import OpsService

__all__ = ["ErrorCode", "OpsError", "OpsService", "Principal", "Scope",
           "assert_authorized"]
