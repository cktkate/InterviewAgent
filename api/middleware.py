"""
FastAPI 中间件：CORS、请求日志、全局异常处理
"""
from __future__ import annotations

import time

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from loguru import logger
from observability.metrics import HTTP_DURATION, HTTP_REQUESTS


def register_middlewares(app: FastAPI) -> None:
    """注册所有中间件"""

    # ── CORS ──────────────────────────────────────────────────────────────────
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],          # 生产环境改为具体域名
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ── 请求日志 ──────────────────────────────────────────────────────────────
    @app.middleware("http")
    async def log_requests(request: Request, call_next):
        start = time.perf_counter()
        response = None
        try:
            response = await call_next(request)
            return response
        finally:
            elapsed_seconds = time.perf_counter() - start
            status_code = response.status_code if response is not None else 500
            route = request.scope.get("route")
            route_path = getattr(route, "path", "unmatched")
            if request.url.path != "/metrics":
                HTTP_REQUESTS.labels(
                    method=request.method,
                    route=route_path,
                    status_class=f"{status_code // 100}xx",
                ).inc()
                HTTP_DURATION.labels(
                    method=request.method, route=route_path
                ).observe(elapsed_seconds)
            logger.info(
                "{} {} {} {:.1f}ms",
                request.method,
                request.url.path,
                status_code,
                elapsed_seconds * 1000,
            )

    # ── 全局异常捕获 ──────────────────────────────────────────────────────────
    @app.exception_handler(Exception)
    async def global_exception_handler(request: Request, exc: Exception):
        logger.error("未捕获异常: {}", type(exc).__name__)
        return JSONResponse(
            status_code=500,
            content={"error": "internal_server_error"},
        )
