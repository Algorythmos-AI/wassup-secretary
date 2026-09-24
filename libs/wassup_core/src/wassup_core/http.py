"""HTTP plumbing shared by every service: problem details, body limits, request ids."""

from __future__ import annotations

import time
import uuid
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from wassup_core.logging import get_logger

PROBLEM_CONTENT_TYPE = "application/problem+json"
log = get_logger(__name__)


def problem(status: int, title: str, code: str, detail: str | None = None) -> JSONResponse:
    """An RFC 9457 problem-details response. Never carries internal error text."""
    body: dict[str, Any] = {"type": "about:blank", "title": title, "status": status, "code": code}
    if detail:
        body["detail"] = detail
    return JSONResponse(body, status_code=status, media_type=PROBLEM_CONTENT_TYPE)


def install_problem_handlers(app: FastAPI) -> None:
    async def http_error(_request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = "not_found" if exc.status_code == 404 else f"http_{exc.status_code}"
        return problem(exc.status_code, str(exc.detail), code)

    async def validation_error(_request: Request, exc: RequestValidationError) -> JSONResponse:
        # Field locations only; never echo the submitted values back.
        fields = sorted({".".join(str(p) for p in err.get("loc", ())) for err in exc.errors()})
        return problem(
            422, "Invalid request", "validation_failed", "Invalid fields: " + ", ".join(fields)
        )

    async def unhandled(_request: Request, exc: Exception) -> JSONResponse:
        log.error("unhandled_error", code=type(exc).__name__)
        return problem(500, "Internal error", "internal_error")

    app.add_exception_handler(StarletteHTTPException, http_error)  # type: ignore[arg-type]
    app.add_exception_handler(RequestValidationError, validation_error)  # type: ignore[arg-type]
    app.add_exception_handler(Exception, unhandled)


class BodyLimitMiddleware:
    """Reject request bodies over a per-path-prefix limit with 413, before buffering them.

    ``limits`` maps a path prefix to a byte limit; the longest matching prefix wins and
    ``default_limit`` applies otherwise. Declared Content-Length is checked first; streamed
    bodies are counted chunk by chunk.
    """

    def __init__(self, app: ASGIApp, limits: Mapping[str, int], default_limit: int) -> None:
        self.app = app
        self.limits = sorted(limits.items(), key=lambda kv: len(kv[0]), reverse=True)
        self.default_limit = default_limit

    def limit_for(self, path: str) -> int:
        for prefix, limit in self.limits:
            if path.startswith(prefix):
                return limit
        return self.default_limit

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        limit = self.limit_for(scope.get("path", ""))
        headers = dict(scope.get("headers") or [])
        declared = headers.get(b"content-length")
        if declared is not None and declared.isdigit() and int(declared) > limit:
            await self._reject(send, limit)
            return

        received = 0
        rejected = False

        async def limited_receive() -> Message:
            # Over the limit: answer 413 ourselves, then tell the app the client went away so
            # it stops reading. Raising here would be swallowed by the framework's body parser.
            nonlocal received, rejected
            if rejected:
                return {"type": "http.disconnect"}
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    rejected = True
                    await self._reject(send, limit)
                    return {"type": "http.disconnect"}
            return message

        async def guarded_send(message: Message) -> None:
            if not rejected:  # the 413 is already on the wire; drop the app's response
                await send(message)

        try:
            await self.app(scope, limited_receive, guarded_send)
        except Exception:
            if not rejected:
                raise

    @staticmethod
    async def _reject(send: Send, limit: int) -> None:
        log.warning("body_too_large", limit_bytes=limit)
        response = problem(413, "Payload too large", "payload_too_large")
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [
                    (b"content-type", PROBLEM_CONTENT_TYPE.encode()),
                    (b"connection", b"close"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": response.body})


RequestHandler = Callable[[Request], Awaitable[Any]]


def install_request_logging(app: FastAPI) -> None:
    """One structured line per request: id, method, route path (no query string), status, time."""

    @app.middleware("http")
    async def _log_requests(request: Request, call_next: RequestHandler) -> Any:
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex
        started = time.perf_counter()
        response = await call_next(request)
        response.headers["x-request-id"] = request_id
        log.info(
            "request",
            request_id=request_id,
            method=request.method,
            path=request.url.path,
            status=response.status_code,
            duration_ms=round((time.perf_counter() - started) * 1000, 1),
        )
        return response
