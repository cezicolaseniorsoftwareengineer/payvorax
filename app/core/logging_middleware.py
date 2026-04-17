import uuid
import time
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from .logging import get_logger


class CorrelationIdMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        correlation_id = request.headers.get("X-Correlation-ID") or str(uuid.uuid4())
        # attach to request.state for handlers to use
        request.state.correlation_id = correlation_id
        start = time.time()
        try:
            response: Response = await call_next(request)
        except Exception as exc:
            logger = get_logger("http")
            logger.exception("request_failed", method=request.method, path=str(request.url), correlation_id=correlation_id)
            raise
        duration = int((time.time() - start) * 1000)
        logger = get_logger("http")
        logger.info(
            "request_completed",
            method=request.method,
            path=str(request.url.path),
            status_code=response.status_code,
            duration_ms=duration,
            correlation_id=correlation_id,
        )
        # expose correlation id to downstream systems
        response.headers["X-Correlation-ID"] = correlation_id
        return response
