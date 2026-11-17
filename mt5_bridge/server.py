"""MT5 ブリッジ FastAPI サーバー (Phase 1+2: read-only)。

起動方法 (Windows main PC または WSL でテスト):
    cd mt5_bridge
    cp .env.example .env  # 値を埋める
    uv sync
    uv run python server.py
"""
from __future__ import annotations

import logging
import secrets
import time
from contextlib import asynccontextmanager
from dataclasses import asdict
from logging.handlers import RotatingFileHandler
from pathlib import Path

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Query, Request, status
from pydantic import BaseModel
from starlette.responses import JSONResponse

from admin_models import AdminStatus, HaltRequest
from config import BridgeSettings, is_loopback_host, load_settings
import bridge_metrics
import request_log
from mt5_client import Mt5Client, PreflightError, TimeRuleUnavailableError
from ohlcv_models import OhlcvBar, OhlcvResponse
from order_models import (
    ClosedDealResponse,
    ClosePositionResponse,
    ModifyPositionRequest,
    ModifyPositionResponse,
    OrderRequest,
    OrderResponse,
)
from runtime_state import RuntimeState

# ── ログ設定: stdout (色付き) + ローテーション付きファイル (色なし) ──
import sys

_LOG_DIR = Path(__file__).parent / "logs"
_LOG_DIR.mkdir(exist_ok=True)
_LOG_FORMAT = "%(asctime)s UTC [%(levelname)s] %(name)s: %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

# levelname 専用 ANSI 色 (cmd.exe / Windows Terminal / Linux 共通)
_LEVEL_COLORS = {
    logging.DEBUG:    "\033[36m",     # cyan
    logging.INFO:     "\033[32m",     # green
    logging.WARNING:  "\033[33m",     # yellow
    logging.ERROR:    "\033[31m",     # red
    logging.CRITICAL: "\033[1;31m",   # bold red
}
_ANSI_RESET = "\033[0m"


def _colorize_levelname(record: logging.LogRecord, use_colors: bool) -> str | None:
    """record.levelname を色付き文字列に書換え、元の値を返す (None=色なし)。

    呼出側で finally に元値を戻すこと (record の汚染回避)。
    """
    if not use_colors:
        return None
    color = _LEVEL_COLORS.get(record.levelno)
    if not color:
        return None
    original = record.levelname
    record.levelname = f"{color}{original}{_ANSI_RESET}"
    return original


class _ColoredLevelFormatter(logging.Formatter):
    """ターミナル用フォーマッタ。`%(levelname)s` を ANSI で色付けする。

    アプリ標準ログ + uvicorn 起動メッセージ用。
    """

    def __init__(
        self,
        fmt: str | None = None,
        datefmt: str | None = None,
        *,
        use_colors: bool | None = None,
    ) -> None:
        super().__init__(fmt=fmt, datefmt=datefmt)
        self.converter = time.gmtime
        if use_colors is None:
            use_colors = sys.stderr.isatty()
        self._use_colors = bool(use_colors)

    def format(self, record: logging.LogRecord) -> str:
        original = _colorize_levelname(record, self._use_colors)
        try:
            return super().format(record)
        finally:
            if original is not None:
                record.levelname = original


def _make_colored_access_formatter(*args, **kwargs):
    """uvicorn.AccessFormatter を継承して levelname も色付けするクラスを返す。

    AccessFormatter は client_addr/request_line/status_code を scope から
    展開する特殊機能を持つため、置換ではなく継承する必要がある。
    関数で動的生成しているのは uvicorn のインポートをモジュールロード時に
    遅延させるため (mt5_bridge ローカル実行で uvicorn 未インストール環境を許容)。
    """
    from uvicorn.logging import AccessFormatter

    class _ColoredAccessFormatter(AccessFormatter):
        def __init__(self, *a, **kw) -> None:
            super().__init__(*a, **kw)
            self.converter = time.gmtime
            if self.use_colors is None:
                self.use_colors = sys.stderr.isatty()

        def formatMessage(self, record: logging.LogRecord) -> str:
            original = _colorize_levelname(record, self.use_colors)
            try:
                return super().formatMessage(record)
            finally:
                if original is not None:
                    record.levelname = original

    return _ColoredAccessFormatter(*args, **kwargs)


# ファイル出力 (色なし、ANSI 制御文字を埋め込まない)
_file_handler = RotatingFileHandler(
    _LOG_DIR / "bridge.log",
    maxBytes=5_000_000,    # 5MB ごとに rotate
    backupCount=5,         # bridge.log + bridge.log.1 .. .5 を保持
    encoding="utf-8",
)
_file_formatter = logging.Formatter(_LOG_FORMAT, datefmt=_DATE_FORMAT)
_file_formatter.converter = time.gmtime
_file_handler.setFormatter(_file_formatter)

# ターミナル出力 (色付き、tty 検出で自動切替)
_stream_handler = logging.StreamHandler()
_stream_handler.setFormatter(
    _ColoredLevelFormatter(_LOG_FORMAT, datefmt=_DATE_FORMAT),
)

logging.basicConfig(
    level=logging.INFO,
    handlers=[_stream_handler, _file_handler],
)
logger = logging.getLogger(__name__)


def _hdr(code: str) -> dict[str, str]:
    """HTTPException に載せる固定 error code。body・detail・status は変えない。"""
    return {bridge_metrics.H_ERROR_CODE: code}


def _code(exc: Exception, default: str) -> dict[str, str]:
    return _hdr(getattr(exc, "error_code", default))


# ── 起動時にロードする singleton ────────────────────────────────────

_settings: BridgeSettings | None = None
_client: Mt5Client | None = None
_runtime: RuntimeState | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _settings, _client, _runtime
    _settings = load_settings()
    flag_path = Path(__file__).parent / _settings.halt_state_path
    _runtime = RuntimeState(
        initial_dry_run=_settings.dry_run,
        env_file=Path(__file__).parent / ".env",
        hard_halt_flag=flag_path,
    )
    if _runtime.is_hard_halted:
        logger.error(
            "[ADMIN] starting in HARD HALT state (flag file present). "
            f"Remove {flag_path} and set DRY_RUN=false to recover."
        )
    _client = Mt5Client(
        login=_settings.mt5_login,
        password=_settings.mt5_password,
        server=_settings.mt5_server,
        probe_symbol=_settings.server_time_symbol,
    )
    logger.warning(
        f"DRY_RUN={_runtime.dry_run} | api_key={'set' if _settings.auth_required else 'NOT SET (order endpoints refused)'} | host={_settings.host}"
    )
    try:
        _client.connect()
        logger.info("MT5 connection established")
    except Exception as e:  # noqa: BLE001
        logger.error(f"MT5 connect failed at startup: {e}")
        # ブリッジ自体は起動を続行 (heartbeat に "connected=false" を返すため)
    request_log.request_log.start(_LOG_DIR / "bridge-metrics")
    try:
        yield
    finally:
        try:
            if _client is not None:
                _client.disconnect()
        finally:
            request_log.request_log.stop()


# ── 認証 (api_key 設定時のみ強制) ────────────────────────────────────

def _api_key_matches(raw_key: bytes | None) -> bool:
    """提示された API キー (header の生 byte) が設定と一致するか。判定はここだけ。"""
    if _settings is None or not _settings.auth_required:
        return True
    return raw_key is not None and secrets.compare_digest(raw_key, _settings.api_key.encode())


def require_api_key(x_bridge_api_key: str | None = Header(default=None)) -> None:
    if _settings is None or not _settings.auth_required:
        return
    metrics = bridge_metrics.current_metrics()
    if metrics is not None:
        # middleware が同じ関数で同じ生 byte に下した判定を、そのまま使う
        ok = metrics.authorized
    else:
        ok = _api_key_matches(
            None if x_bridge_api_key is None else x_bridge_api_key.encode("latin-1"))
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid or missing X-Bridge-Api-Key",
            headers=_hdr("auth_invalid"),
        )


LOCAL_ONLY_DETAIL = "API キー未設定の bridge は自機内からの接続のみ受け付ける"


class LocalOnlyWithoutKeyMiddleware:
    """API キー未設定なら、接続元が loopback の IP リテラルでない要求を経路解決の前に全て拒否する。

    純粋な ASGI middleware なので、route に属さない応答 (未知の path、404 を含む) も覆う。
    接続元は ASGI scope の client だけを見る (X-Forwarded-For などのヘッダは見ない)。
    待受先 (uvicorn の --host など) がどう指定されても「キー無し = 自機内だけ」を保つ。
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        if _settings is not None and _settings.auth_required:
            await self.app(scope, receive, send)
            return
        client = scope.get("client")
        if client is not None and is_loopback_host(client[0]):
            await self.app(scope, receive, send)
            return
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        await JSONResponse(
            {"detail": LOCAL_ONLY_DETAIL}, status_code=status.HTTP_403_FORBIDDEN,
            headers=_hdr("local_only_forbidden"),
        )(scope, receive, send)


HISTORY_BUSY_DETAIL = "history request in flight: another history request has not completed"
HISTORY_DUPLICATE_DETAIL = "history request already accepted: the same request id is not executed again"
HISTORY_ID_INVALID_DETAIL = "invalid X-Bridge-History-Request-Id"

_METRICS_SCOPE_KEY = "bridge_metrics"


def _fallback_error_code(scope, status_code: int) -> str | None:
    """handler が code を付けなかった応答 (未知 route など) の固定 code。"""
    if status_code == 404 and "endpoint" not in scope:
        return "route_not_found"
    if status_code == 405:
        return "method_not_allowed"
    if status_code == 422:
        return "request_validation_failed"
    return None


def _request_is_authorized(headers) -> bool:
    """業務の認証を通る要求か。履歴 slot の詳細を載せてよいかも、この判定で決まる。"""
    return _api_key_matches(headers.get(b"x-bridge-api-key"))


class MetricsMiddleware:
    """全 http 応答へ受動計測 header を付ける純粋な ASGI middleware。

    body・detail・status は触らない。最も外側に置き、LocalOnlyWithoutKeyMiddleware の 403 や
    経路解決前の 404 も覆う。要求ごとの集計を ContextVar に載せ、thread pool の handler へ渡す。
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        raw = {k: v for k, v in scope.get("headers", [])}
        metrics = bridge_metrics.RequestMetrics()
        history_id = raw.get(b"x-bridge-history-request-id")
        if history_id is not None:
            metrics.history_request_id = history_id.decode("latin-1")
        try:
            metrics.authorized = _request_is_authorized(raw)
        except Exception:  # noqa: BLE001
            metrics.authorized = False
        scope[_METRICS_SCOPE_KEY] = metrics

        async def send_with_metrics(message):
            if message["type"] == "http.response.start":
                message = {**message, "headers": _with_metrics(scope, message, metrics)}
            await send(message)

        token = bridge_metrics.bind_metrics(metrics)
        try:
            await self.app(scope, receive, send_with_metrics)
        finally:
            bridge_metrics.unbind_metrics(token)


def _metrics_headers(metrics, *, error_code: str | None, emit_error_code: bool) -> list:
    """応答に付ける計測 header の組み立て口。slot の可否 (認証済みか・認証拒否 code か) もここで決まる。"""
    return bridge_metrics.response_headers(
        metrics, error_code=error_code, emit_error_code=emit_error_code,
        include_slot=bool(getattr(metrics, "authorized", False)),
    )


def _record_request(scope, status: int, code: str | None, extra: list) -> None:
    """1 要求 1 行の記録。enqueue だけで待たず、失敗は応答へ出さない。"""
    try:
        caller = None
        for key, value in scope.get("headers", []):
            if key == b"x-afx-caller":
                caller = value
        row = request_log.build_row(
            caller_raw=caller, path=scope.get("path", ""), status=status,
            error_code=code, headers=extra)
        request_log.request_log.emit(row)
    except Exception:  # noqa: BLE001
        return


def _with_metrics(scope, message, metrics) -> list:
    headers = list(message.get("headers", []))
    present = {k.lower() for k, _ in headers}
    code_name = bridge_metrics.H_ERROR_CODE.lower().encode()
    code = None
    for k, v in headers:
        if k.lower() == code_name:
            code = v.decode("latin-1")
    if code is None:
        code = _fallback_error_code(scope, message["status"])
    extra = _metrics_headers(
        metrics, error_code=code, emit_error_code=code_name not in present)
    _record_request(scope, message["status"], code, extra)
    for name, value in extra:
        if name.lower().encode() not in present:
            headers.append((name.encode(), value.encode("latin-1")))
    return headers


ORDER_KEY_REQUIRED_DETAIL = "発注系は API キーが必要"


def require_order_api_key(x_bridge_api_key: str | None = Header(default=None)) -> None:
    """発注系 endpoint 用。API キー未設定なら (DRY_RUN に関係なく) 拒否する。"""
    if _settings is None or not _settings.auth_required:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=ORDER_KEY_REQUIRED_DETAIL,
            headers=_hdr("order_key_required"),
        )
    require_api_key(x_bridge_api_key)


app = FastAPI(
    title="MT5 Bridge",
    version="0.1.0",
    description="HTTP bridge over MetaTrader5 Python package (price data + order endpoints).",
    lifespan=lifespan,
    # 取引 bridge に対話 docs は不要。公開面を減らすため生成しない
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)
app.add_middleware(LocalOnlyWithoutKeyMiddleware)
app.add_middleware(MetricsMiddleware)   # 最も外側 (後に足したものが外側)


@app.exception_handler(Exception)
async def _unexpected_error(request: Request, exc: Exception):
    """想定外の例外。既定の 500 応答と同じ本文のまま、計測 header だけ付ける。"""
    from starlette.responses import PlainTextResponse

    metrics = request.scope.get(_METRICS_SCOPE_KEY)
    extra = _metrics_headers(
        metrics, error_code="internal_error", emit_error_code=True)
    _record_request(request.scope, 500, "internal_error", extra)
    headers = dict(extra)
    return PlainTextResponse("Internal Server Error", status_code=500, headers=headers)

# 発注・管理書き込み系。認証は router 単位で付ける (個別 decorator には付けない)
order_router = APIRouter(dependencies=[Depends(require_order_api_key)])


# ── レスポンスモデル ────────────────────────────────────────────────

class HealthResponse(BaseModel):
    status: str
    mt5_connected: bool
    dry_run: bool
    server: str | None = None
    login: int | None = None
    time_rule_state: str | None = None
    measured_at: str | None = None


# ── endpoints ────────────────────────────────────────────────────

@app.get("/health", response_model=HealthResponse)
def health():
    """ブリッジ生存確認 (heartbeat 用、無認証)。"""
    connected = _client is not None and _client.ping()
    time_rule_state = measured_at = None
    if connected and _client is not None and hasattr(_client, "time_rule_status"):
        time_rule_state, measured_at = _client.time_rule_status()
    return HealthResponse(
        status="ok",
        mt5_connected=connected,
        dry_run=_runtime.dry_run if _runtime else True,
        server=_settings.mt5_server if _settings else None,
        login=_settings.mt5_login if _settings else None,
        time_rule_state=time_rule_state,
        measured_at=measured_at,
    )


@app.get("/account", dependencies=[Depends(require_api_key)])
def account():
    if _client is None or not _client.is_connected:
        raise HTTPException(503, "MT5 not connected", headers=_hdr("mt5_not_connected"))
    return asdict(_client.get_account())


@app.get("/server-time", dependencies=[Depends(require_api_key)])
def server_time():
    """こちらの UTC とブローカーのサーバ時刻を並べて返す (検証・運用の切り分け用)。

    MT5 の時刻はすべてサーバ時間帯のエポック秒で、bridge は NY の規則から換算する。
    tick から測った offset は換算には使わず、規則との一致の検査にだけ使う。
    両方の時計、測定値、検査の状態をここから覗ける。
    """
    if _client is None or not _client.is_connected:
        raise HTTPException(503, "MT5 not connected", headers=_hdr("mt5_not_connected"))
    return _client.get_server_time()


@app.get("/quote/{symbol}", dependencies=[Depends(require_api_key)])
def quote(symbol: str):
    if _client is None or not _client.is_connected:
        raise HTTPException(503, "MT5 not connected", headers=_hdr("mt5_not_connected"))
    try:
        return asdict(_client.get_quote(symbol))
    except TimeRuleUnavailableError as e:
        raise HTTPException(503, e.detail, headers=_hdr(e.detail))
    except RuntimeError as e:
        raise HTTPException(404, str(e), headers=_code(e, "quote_failed"))


@app.get("/positions", dependencies=[Depends(require_api_key)])
def positions():
    if _client is None or not _client.is_connected:
        raise HTTPException(503, "MT5 not connected", headers=_hdr("mt5_not_connected"))
    return [asdict(p) for p in _client.get_positions()]


@app.get("/positions/{ticket}/closed-deal", response_model=ClosedDealResponse,
         dependencies=[Depends(require_api_key)])
def closed_deal(ticket: int):
    if _client is None or not _client.is_connected:
        raise HTTPException(503, "MT5 not connected", headers=_hdr("mt5_not_connected"))
    deal = _client.get_closed_deal(ticket)
    if deal is None:
        raise HTTPException(404, f"closed deal not found for ticket={ticket}",
                            headers=_hdr("closed_deal_not_found"))
    return ClosedDealResponse(**asdict(deal))


@app.get("/symbols", dependencies=[Depends(require_api_key)])
def symbols():
    if _client is None or not _client.is_connected:
        raise HTTPException(503, "MT5 not connected", headers=_hdr("mt5_not_connected"))
    return _client.get_symbols()


@app.get("/ohlcv/{symbol}", response_model=OhlcvResponse,
         dependencies=[Depends(require_api_key)])
def ohlcv(
    symbol: str,
    from_: str = Query(..., alias="from"),
    to: str = Query(...),
    interval: str = Query("1h"),
):
    """OHLCV を MT5 から取得 (copy_rates_range ベース)。

    query: from=ISO8601&to=ISO8601&interval=1h
    interval: 1m | 5m | 15m | 30m | 1h | 4h | 1d
    """
    from datetime import datetime as _dt

    metrics = bridge_metrics.current_metrics()
    history_id = metrics.history_request_id if metrics is not None else None
    if history_id is not None and not bridge_metrics.valid_history_request_id(history_id):
        raise HTTPException(400, HISTORY_ID_INVALID_DETAIL,
                            headers=_hdr("history_request_id_invalid"))

    if _client is None or not _client.is_connected:
        raise HTTPException(503, "MT5 not connected", headers=_hdr("mt5_not_connected"))
    try:
        date_from = _dt.fromisoformat(from_.replace("Z", "+00:00"))
        date_to = _dt.fromisoformat(to.replace("Z", "+00:00"))
    except ValueError as e:
        raise HTTPException(400, f"invalid timestamp: {e}", headers=_hdr("invalid_timestamp"))

    if date_to <= date_from:
        raise HTTPException(400, "to must be after from", headers=_hdr("invalid_range"))

    # 履歴専用 slot: native 取得の前に登録し、全 native 呼出しと lock 解放の後だけ完了にする。
    # この thread は HTTP の切断に関わらず最後まで走るので、追跡は切断で消えない。
    entry = None
    if history_id is not None:
        verdict, entry = bridge_metrics.history_slot.admit(history_id, metrics)
        if verdict == bridge_metrics.HistorySlot.DUPLICATE:
            raise HTTPException(409, HISTORY_DUPLICATE_DETAIL,
                                headers=_hdr("history_request_duplicate"))
        if verdict == bridge_metrics.HistorySlot.BUSY:
            raise HTTPException(409, HISTORY_BUSY_DETAIL,
                                headers=_hdr("history_request_busy"))
    try:
        bars_raw = _client.copy_rates_range(symbol, interval, date_from, date_to)
    except ValueError as e:
        raise HTTPException(400, str(e), headers=_hdr("invalid_request"))
    except TimeRuleUnavailableError as e:
        raise HTTPException(503, e.detail, headers=_hdr(e.detail))
    except RuntimeError as e:
        raise HTTPException(404, str(e), headers=_code(e, "history_failed"))
    finally:
        if entry is not None:
            bridge_metrics.history_slot.complete(entry)

    return OhlcvResponse(
        symbol=symbol,
        interval=interval,  # type: ignore[arg-type]
        bars=[OhlcvBar(**b) for b in bars_raw],
    )


# ── 発注系 endpoint (Phase 3a: DRY_RUN のみ) ────────────────────────
# 実発注 (DRY_RUN=false) は Phase 3b で資金チェック・slippage 制御等とセットで実装。

# MT5 retcode → 処理分岐 (Phase 3b タスク 5)
_RETCODE_DONE = 10009         # TRADE_RETCODE_DONE
_RETCODE_PARTIAL = 10010      # TRADE_RETCODE_DONE_PARTIAL
_RETCODE_RETRYABLE = {10004, 10020}  # REQUOTE / PRICE_CHANGED → 1 回再試行
_RETCODE_RATE_LIMIT = 10024   # TOO_MANY_REQUESTS → 100ms backoff + 1 回再試行


@order_router.post("/order", response_model=OrderResponse)
def place_order(req: OrderRequest):
    if _client is None or not _client.is_connected:
        raise HTTPException(503, "MT5 not connected", headers=_hdr("mt5_not_connected"))
    if _runtime is None:
        raise HTTPException(503, "runtime not initialized", headers=_hdr("runtime_not_initialized"))

    # 経路 1: hard halt or 通常 DRY_RUN モード → DRY_RUN パス
    if _runtime.dry_run:
        try:
            result = _client.place_order_dry_run(
                symbol=req.symbol, side=req.side, volume_lots=req.volume_lots,
                sl=req.sl, tp=req.tp, magic=req.magic, comment=req.comment,
            )
            return OrderResponse(**result)
        except RuntimeError as e:
            raise HTTPException(404, str(e), headers=_code(e, "order_failed"))

    # 経路 2: soft halt 中 → 423 Locked
    if _runtime.soft_halted:
        raise HTTPException(
            423,
            "bridge is soft-halted, new orders rejected. "
            "POST /admin/resume to clear halt.",
            headers=_hdr("soft_halted"),
        )

    # 経路 3: live mode
    # ── 事前証拠金チェック (5% バッファ) — Mt5Client の lock 下で実行 (直 _mt5 アクセス禁止) ──
    try:
        _client.preflight_order_margin(
            req.symbol, req.side, req.volume_lots, margin_buffer=1.05,
        )
    except PreflightError as e:
        raise HTTPException(e.http_status, str(e), headers=_hdr("preflight_rejected"))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"pre-flight check failed: {e}", headers=_hdr("preflight_error"))

    # ── 発注 + retry (REQUOTE/PRICE_CHANGED 1 回、TOO_MANY_REQUESTS は 100ms backoff) ──
    last_result: dict | None = None
    for attempt in range(2):
        try:
            last_result = _client.place_order_live(
                symbol=req.symbol, side=req.side, volume_lots=req.volume_lots,
                sl=req.sl, tp=req.tp, magic=req.magic, comment=req.comment,
                filling_mode=_settings.filling_mode,
                deviation_points=_settings.deviation_points,
            )
        except RuntimeError as e:
            raise HTTPException(500, str(e), headers=_code(e, "order_failed"))

        retcode = last_result["retcode"]
        if retcode in (_RETCODE_DONE, _RETCODE_PARTIAL):
            return OrderResponse(
                ticket=last_result["ticket"],
                symbol=last_result["symbol"],
                side=last_result["side"],
                volume_lots=last_result["volume_lots"],
                fill_price=last_result["fill_price"],
                sl=last_result["sl"], tp=last_result["tp"],
                time=last_result["time"],
                dry_run=False,
                magic=last_result["magic"],
            )
        if retcode in _RETCODE_RETRYABLE and attempt == 0:
            continue
        if retcode == _RETCODE_RATE_LIMIT and attempt == 0:
            import time as _t
            _t.sleep(0.1)
            continue
        break

    raise HTTPException(
        409,
        f"order rejected: retcode={last_result['retcode']} "
        f"comment={last_result.get('comment_response', '')}",
        headers=_hdr("order_rejected"),
    )


@order_router.post("/positions/{ticket}/modify", response_model=ModifyPositionResponse)
def modify_position(ticket: int, req: ModifyPositionRequest):
    if req.sl is None and req.tp is None:
        raise HTTPException(400, "sl or tp must be provided", headers=_hdr("modify_no_fields"))
    if _client is None or not _client.is_connected:
        raise HTTPException(503, "MT5 not connected", headers=_hdr("mt5_not_connected"))
    if _runtime is None:
        raise HTTPException(503, "runtime not initialized", headers=_hdr("runtime_not_initialized"))

    # SL/TP modify は既存ポジションのリスク低減用途なので soft halt 中も許可する。
    try:
        if _runtime.dry_run:
            result = _client.modify_position_dry_run(ticket, sl=req.sl, tp=req.tp)
        else:
            result = _client.modify_position_live(ticket, sl=req.sl, tp=req.tp)
        return ModifyPositionResponse(**result)
    except RuntimeError as e:
        raise HTTPException(409, str(e), headers=_hdr("modify_failed"))


@order_router.post("/positions/{ticket}/close", response_model=ClosePositionResponse)
def close_position(ticket: int, symbol: str | None = None):
    if _client is None or not _client.is_connected:
        raise HTTPException(503, "MT5 not connected", headers=_hdr("mt5_not_connected"))
    if _runtime is None:
        raise HTTPException(503, "runtime not initialized", headers=_hdr("runtime_not_initialized"))

    if _runtime.dry_run:
        try:
            result = _client.close_position_dry_run(ticket, symbol=symbol)
            return ClosePositionResponse(**result)
        except RuntimeError as e:
            raise HTTPException(500, str(e), headers=_hdr("close_failed"))

    # close は soft halt 中も実行可能 (既存ポジ管理のため)
    try:
        result = _client.close_position_live(ticket)
        return ClosePositionResponse(**result)
    except RuntimeError as e:
        raise HTTPException(404, str(e), headers=_hdr("close_failed"))


# ── 管理エンドポイント (Phase 3b: 2-tier halt) ────────────────────────

def _build_admin_status() -> AdminStatus:
    return AdminStatus(
        dry_run=_runtime.dry_run,
        soft_halted=_runtime.soft_halted,
        is_hard_halted=_runtime.is_hard_halted,
        accepts_new_orders=_runtime.accepts_new_orders,
        mt5_connected=_client is not None and _client.ping(),
    )


@order_router.post("/admin/halt", response_model=AdminStatus)
def admin_halt(req: HaltRequest):
    if _runtime is None:
        raise HTTPException(503, "runtime not initialized", headers=_hdr("runtime_not_initialized"))
    if req.mode == "hard":
        _runtime.hard_halt(reason=req.reason)
    else:
        _runtime.soft_halt(reason=req.reason)
    return _build_admin_status()


@order_router.post("/admin/resume", response_model=AdminStatus)
def admin_resume():
    if _runtime is None:
        raise HTTPException(503, "runtime not initialized", headers=_hdr("runtime_not_initialized"))
    ok, msg = _runtime.resume()
    if not ok:
        raise HTTPException(403, msg, headers=_hdr("resume_refused"))
    return _build_admin_status()


@app.get("/admin/status", response_model=AdminStatus,
         dependencies=[Depends(require_api_key)])
def admin_status():
    if _runtime is None:
        raise HTTPException(503, "runtime not initialized", headers=_hdr("runtime_not_initialized"))
    return _build_admin_status()


class _UvicornNameRewriter(logging.Filter):
    """uvicorn.error logger name を 'uvicorn' に書き換えて表示する。

    uvicorn は起動・停止・通常 INFO ログをすべて `uvicorn.error` logger に流す
    仕様 (logger 名に "error" が入っていても誤りではない) のため、ログ表示が
    `[INFO] uvicorn.error:` となって誤解を招く。本フィルタで表示名のみ書換える。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if record.name == "uvicorn.error":
            record.name = "uvicorn"
        return True


class _PollingAccessFilter(logging.Filter):
    """quote polling の成功 access log を落とすフィルタ。

    quote-stream producer (app 側) が全 trade pair を短周期 polling するため、
    `GET /quote/{symbol} 2xx` の access log が毎秒出てターミナルを汚染する。
    成功した定常ポーリングのみ drop し、エラー (非 2xx)・他 endpoint は keep する。
    /health は低頻度 (発注 preflight / halt resume / app proxy) のため対象外。
    uvicorn.access の record.args は
    (client_addr, method, full_path, http_version, status_code) の 5-tuple。
    想定外の形状は keep (fail-open — 落とすより出す方が安全)。args 形状は
    uvicorn 0.46 時点の内部実装 (h11_impl / httptools_impl 共通)。将来の
    uvicorn 更新で形状が変わっても fail-open で抑制が外れるだけで害はない。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if not isinstance(args, tuple) or len(args) != 5:
            return True
        _client, method, path, _ver, status = args
        if method != "GET" or not isinstance(path, str) or not path.startswith("/quote/"):
            return True
        return not (isinstance(status, int) and 200 <= status < 300)


def _build_log_config() -> dict:
    """uvicorn 用 log_config を生成する。

    main() インラインだと access ハンドラへのフィルタ配線漏れをテストで
    検出できないため関数化している (tests/test_access_log_filter.py が検証)。

    - uvicorn のデフォルトログは `INFO: ...` でタイムスタンプ無し
      → アプリと同じフォーマット (時刻付き) に統一、`uvicorn.error` 表記も整理
    - access ハンドラには _PollingAccessFilter を配線し、quote polling の
      成功行 (GET /quote/* 2xx) を落とす
    """
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "filters": {
            "uvicorn_name_fix": {
                "()": _UvicornNameRewriter,
            },
            "polling_access": {
                "()": _PollingAccessFilter,
            },
        },
        "formatters": {
            "default": {
                "()": _ColoredLevelFormatter,    # levelname に色
                "format": _LOG_FORMAT,
                "datefmt": _DATE_FORMAT,
            },
            "access": {
                # AccessFormatter を継承して levelname も色付け
                # status_code/request_line の色付けは AccessFormatter 標準のまま
                "()": _make_colored_access_formatter,
                "format": "%(asctime)s UTC [%(levelname)s] uvicorn.access: %(client_addr)s - \"%(request_line)s\" %(status_code)s",
                "datefmt": _DATE_FORMAT,
            },
        },
        "handlers": {
            "default": {
                "formatter": "default",
                "class": "logging.StreamHandler",
                "stream": "ext://sys.stderr",
                "filters": ["uvicorn_name_fix"],
            },
            "access": {
                "formatter": "access",
                "class": "logging.StreamHandler",
                "stream": "ext://sys.stdout",
                "filters": ["polling_access"],
            },
        },
        "loggers": {
            "uvicorn":        {"handlers": ["default"], "level": "INFO", "propagate": False},
            "uvicorn.error":  {"handlers": ["default"], "level": "INFO", "propagate": False},
            "uvicorn.access": {"handlers": ["access"],  "level": "INFO", "propagate": False},
        },
    }


app.include_router(order_router)


def main() -> None:
    """`python server.py` で uvicorn を直接起動する。"""
    import uvicorn

    cfg = load_settings()

    # app オブジェクト直渡し (reload 不要、import 文字列のパス問題を回避)
    uvicorn.run(
        app, host=cfg.host, port=cfg.port,
        log_level="info", log_config=_build_log_config(),
        # X-Forwarded-For で scope の client が置き換わる経路を閉じる
        proxy_headers=False,
    )


if __name__ == "__main__":
    main()
