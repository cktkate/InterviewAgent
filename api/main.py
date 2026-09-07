"""
FastAPI 应用入口
启动命令：uvicorn api.main:app --host 0.0.0.0 --port 8000
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from loguru import logger
import os

from api.middleware import register_middlewares
from api.routers import auth, health, interview, job_targets, learning_plans, practice, upload, websocket
from config import settings
from config.logging import setup_logger

setup_logger()


@asynccontextmanager
async def lifespan(application: FastAPI):
    application.state.startup_complete = False
    application.state.started_at = asyncio.get_running_loop().time()
    logger.info("InterviewAgent API 启动成功")
    logger.info("面试界面: http://127.0.0.1:8000/")
    logger.info("接口文档: http://127.0.0.1:8000/docs")
    if settings.stt_preload:
        from audio.stt import stt_engine

        application.state.stt_warmup_task = asyncio.create_task(
            asyncio.to_thread(stt_engine.ensure_loaded)
        )
        logger.info("STT 后台预热已提交，API 启动不等待模型加载")
    application.state.startup_complete = True
    try:
        yield
    finally:
        application.state.startup_complete = False
        warmup = getattr(application.state, "stt_warmup_task", None)
        if warmup and not warmup.done():
            warmup.cancel()
        logger.info("InterviewAgent API 已关闭")


app = FastAPI(
    title="InterviewAgent API",
    description="AI 模拟面试官后端接口",
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan,
)

# 注册中间件
register_middlewares(app)

# 注册路由
app.include_router(health.router)
app.include_router(auth.router)
app.include_router(interview.router)
app.include_router(job_targets.router)
app.include_router(learning_plans.router)
app.include_router(practice.router)
app.include_router(upload.router)
app.include_router(websocket.router)

# 挂载静态文件（CSS / JS）
_static_dir = os.path.join(os.path.dirname(__file__), "..", "frontend", "static")
if os.path.isdir(_static_dir):
    app.mount("/static", StaticFiles(directory=_static_dir), name="static")

# 前端首页
_index_html = os.path.join(os.path.dirname(__file__), "..", "frontend", "index.html")

@app.get("/", include_in_schema=False)
async def serve_index():
    """返回前端首页"""
    if os.path.exists(_index_html):
        return FileResponse(
            _index_html,
            headers={
                "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
                "Pragma": "no-cache",
                "Expires": "0",
            },
        )
    return {"message": "InterviewAgent API 运行中，访问 /docs 查看接口文档"}

