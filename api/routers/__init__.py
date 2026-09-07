"""API 路由模块"""
from api.routers import auth, health, interview, upload, websocket

__all__ = ["auth", "health", "interview", "upload", "websocket"]
