"""Windows desktop launcher for InterviewAgent.

The existing FastAPI application remains the single source of truth.  This
module only starts it on localhost and displays it in a native WebView window.
It is intentionally separate from ``api.main`` so the original web and CLI
entry points continue to work unchanged.
"""
from __future__ import annotations

import os
import socket
import sys
import threading
import time
import traceback
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

import uvicorn
import webview


APP_NAME = "InterviewAgent"
HOST = "127.0.0.1"
DEFAULT_PORT = 8000


def _application_dir() -> Path:
    """Return the portable application directory in source and frozen modes."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def _prepare_working_directory() -> None:
    # Existing settings intentionally read .env and data paths from cwd.
    os.chdir(_application_dir())


def _port_is_available(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        return sock.connect_ex((HOST, port)) != 0


def _select_port() -> int:
    configured = os.getenv("APP_PORT")
    if configured:
        port = int(configured)
        if not _port_is_available(port):
            raise RuntimeError(f"端口 {port} 已被占用，请关闭占用程序或修改 APP_PORT。")
        return port

    for port in range(DEFAULT_PORT, DEFAULT_PORT + 20):
        if _port_is_available(port):
            return port
    raise RuntimeError("找不到可用端口（8000-8019）。")


def _wait_until_ready(url: str, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urlopen(f"{url}/health", timeout=1) as response:
                if response.status == 200:
                    return
        except (OSError, URLError):
            time.sleep(0.15)
    raise RuntimeError("本地服务启动超时，请查看 logs 目录中的日志。")


def main() -> None:
    _prepare_working_directory()

    # Import after changing cwd so existing .env/data/log path semantics remain
    # exactly the same as launching uvicorn from the project directory.
    from api.main import app

    port = _select_port()
    base_url = f"http://{HOST}:{port}"
    server = uvicorn.Server(
        uvicorn.Config(app, host=HOST, port=port, log_level="info", access_log=False)
    )
    server_thread = threading.Thread(target=server.run, name="fastapi", daemon=True)
    server_thread.start()

    try:
        _wait_until_ready(base_url)
        webview.create_window(
            APP_NAME,
            base_url,
            width=1280,
            height=820,
            min_size=(960, 640),
            confirm_close=True,
        )
        webview.start(debug=os.getenv("DESKTOP_DEBUG", "0") == "1")
    finally:
        server.should_exit = True
        server_thread.join(timeout=5)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        # A windowed PyInstaller build has no console, so surface startup errors
        # to the user instead of failing silently.
        error_text = f"InterviewAgent 启动失败：\n\n{exc}"
        try:
            import ctypes

            ctypes.windll.user32.MessageBoxW(0, error_text, APP_NAME, 0x10)
        except Exception:
            print(error_text, file=sys.stderr)
            traceback.print_exc()
        raise
