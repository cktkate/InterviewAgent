"""
健康检查路由
GET /health/live     -> process liveness
GET /health/startup  -> application startup completion
GET /health/ready    -> required dependency readiness
GET /health and /api/health remain compatibility liveness aliases
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response

from api.schemas import HealthResponse
from config import settings
from observability.health import check_readiness
from observability.metrics import render_metrics

router = APIRouter(tags=["健康检查"])


@router.get("/health", response_model=HealthResponse)
@router.get("/api/health", response_model=HealthResponse, include_in_schema=False)
@router.get("/health/live", response_model=HealthResponse)
@router.get("/livez", response_model=HealthResponse, include_in_schema=False)
async def health_check():
    return HealthResponse(status="ok", version="1.0.0")


@router.get("/health/startup")
@router.get("/startupz", include_in_schema=False)
async def startup_check(request: Request):
    complete = bool(getattr(request.app.state, "startup_complete", False))
    return JSONResponse(
        status_code=200 if complete else 503,
        content={"status": "ok" if complete else "starting", "version": "1.0.0"},
    )


@router.get("/health/ready")
@router.get("/readyz", include_in_schema=False)
async def readiness_check():
    dependencies = await check_readiness()
    ready = all(
        item.status == "up" for item in dependencies.values() if item.required
    )
    return JSONResponse(
        status_code=200 if ready else 503,
        content={
            "status": "ready" if ready else "not_ready",
            "version": "1.0.0",
            "dependencies": {
                name: result.as_dict() for name, result in dependencies.items()
            },
        },
    )


@router.get("/metrics", include_in_schema=False)
async def metrics():
    if not settings.metrics_enabled:
        raise HTTPException(status_code=404, detail="metrics disabled")
    payload, content_type = render_metrics()
    return Response(content=payload, headers={"Content-Type": content_type})
