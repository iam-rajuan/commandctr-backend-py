from __future__ import annotations

from collections import defaultdict, deque
from datetime import datetime
from time import perf_counter
from uuid import uuid4

from fastapi import FastAPI, Response
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes import api_router
from app.config import get_settings
from app.db import lifespan, now_utc
from app.errors import register_error_handlers, success
from app.request_logging import print_request_log, read_payload_for_log

settings = get_settings()
app = FastAPI(title="CommandCtr Backend API", version="1.0.0", lifespan=lifespan)
register_error_handlers(app)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])
rate_buckets: dict[str, deque[datetime]] = defaultdict(deque)


@app.middleware("http")
async def request_middleware(request, call_next):
    start = perf_counter()
    request_id = request.headers.get("x-request-id") or str(uuid4())
    payload = await read_payload_for_log(request)
    query = dict(request.query_params.multi_items())
    client = request.client.host if request.client else "unknown"
    client_key = request.client.host if request.client else "unknown"
    bucket = rate_buckets[client_key]
    cutoff = now_utc().timestamp() - settings.RATE_LIMIT_WINDOW_MS / 1000
    while bucket and bucket[0].timestamp() < cutoff:
        bucket.popleft()
    if len(bucket) >= settings.RATE_LIMIT_MAX:
        print_request_log(
            service="CommandCtr Backend",
            method=request.method,
            path=request.url.path,
            status_code=429,
            duration_ms=(perf_counter() - start) * 1000,
            request_id=request_id,
            client=client,
            query=query,
            payload=payload,
        )
        return Response(
            content='{"success":false,"message":"Too many requests. Please try again later.","errors":null}',
            status_code=429,
            media_type="application/json",
            headers={"x-request-id": request_id},
        )
    bucket.append(now_utc())
    try:
        response = await call_next(request)
    except Exception:
        print_request_log(
            service="CommandCtr Backend",
            method=request.method,
            path=request.url.path,
            status_code="ERROR",
            duration_ms=(perf_counter() - start) * 1000,
            request_id=request_id,
            client=client,
            query=query,
            payload=payload,
        )
        raise
    response.headers["x-request-id"] = request_id
    print_request_log(
        service="CommandCtr Backend",
        method=request.method,
        path=request.url.path,
        status_code=response.status_code,
        duration_ms=(perf_counter() - start) * 1000,
        request_id=request_id,
        client=client,
        query=query,
        payload=payload,
    )
    return response


@app.get("/")
async def root():
    return success("CommandCtr backend is running", {"version": "1.0.0"})


app.include_router(api_router, prefix=settings.API_PREFIX)
