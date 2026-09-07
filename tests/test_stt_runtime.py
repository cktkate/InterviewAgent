"""STT 懒加载与 WebSocket 非阻塞语音处理测试。"""
from __future__ import annotations

import asyncio
import json
import threading
import time

from audio.stt import LazySTTEngine


class _FakeEngine:
    available = True

    def __init__(self, text: str = "识别结果") -> None:
        self.text = text

    def transcribe_bytes(self, audio_bytes, is_wav=False, sample_rate=16000):
        return self.text


def test_stt_factory_is_not_called_until_first_transcription():
    calls = []

    def factory():
        calls.append("load")
        return _FakeEngine()

    lazy = LazySTTEngine(factory)
    assert lazy.status == "unloaded"
    assert lazy.is_loaded is False
    assert calls == []

    assert lazy.transcribe_bytes(b"audio") == "识别结果"
    assert calls == ["load"]
    assert lazy.status == "ready"


def test_concurrent_first_use_only_loads_model_once():
    load_count = 0
    load_guard = threading.Lock()

    def factory():
        nonlocal load_count
        time.sleep(0.03)
        with load_guard:
            load_count += 1
        return _FakeEngine()

    lazy = LazySTTEngine(factory)
    threads = [threading.Thread(target=lazy.ensure_loaded) for _ in range(5)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert load_count == 1
    assert lazy.status == "ready"


class _FakeWebSocket:
    def __init__(self) -> None:
        self.messages = []

    async def send_text(self, raw: str) -> None:
        self.messages.append(json.loads(raw))


def test_websocket_stt_rejects_oversized_audio(monkeypatch):
    from api.routers import websocket as websocket_router

    ws = _FakeWebSocket()
    monkeypatch.setattr(websocket_router.settings, "stt_max_audio_bytes", 4)
    asyncio.run(websocket_router._handle_stt_audio(ws, b"12345"))

    statuses = [m.get("status") for m in ws.messages if m["type"] == "stt_status"]
    assert statuses == ["accepted", "failed"]
    assert ws.messages[-1]["type"] == "stt_result"
    assert "过大" in ws.messages[-1]["message"]


def test_websocket_stt_runs_transcription_in_worker_thread(monkeypatch):
    from api.routers import websocket as websocket_router

    main_thread = threading.get_ident()

    class FakeLazy:
        available = True
        is_loaded = True

        def __init__(self):
            self.worker_thread = None

        def transcribe_bytes(self, audio_bytes, is_wav=False):
            self.worker_thread = threading.get_ident()
            time.sleep(0.02)
            return "线程池识别"

    fake = FakeLazy()
    ws = _FakeWebSocket()
    monkeypatch.setattr(websocket_router, "stt_engine", fake)
    monkeypatch.setattr(websocket_router.settings, "stt_max_audio_bytes", 1024)
    monkeypatch.setattr(websocket_router.settings, "stt_transcribe_timeout_seconds", 1.0)
    asyncio.run(websocket_router._handle_stt_audio(ws, b"audio"))

    assert fake.worker_thread is not None
    assert fake.worker_thread != main_thread
    statuses = [m.get("status") for m in ws.messages if m["type"] == "stt_status"]
    assert statuses == ["accepted", "processing", "completed"]
    assert ws.messages[-1]["text"] == "线程池识别"


def test_websocket_stt_timeout_returns_structured_failure(monkeypatch):
    from api.routers import websocket as websocket_router

    class SlowLazy:
        available = True
        is_loaded = True

        @staticmethod
        def transcribe_bytes(audio_bytes, is_wav=False):
            time.sleep(0.05)
            return "太晚了"

    ws = _FakeWebSocket()
    monkeypatch.setattr(websocket_router, "stt_engine", SlowLazy())
    monkeypatch.setattr(websocket_router.settings, "stt_max_audio_bytes", 1024)
    monkeypatch.setattr(websocket_router.settings, "stt_transcribe_timeout_seconds", 0.01)
    asyncio.run(websocket_router._handle_stt_audio(ws, b"audio"))

    failures = [m for m in ws.messages if m.get("status") == "failed"]
    assert failures[0]["code"] == "transcribe_timeout"
    assert ws.messages[-1]["type"] == "stt_result"
    assert ws.messages[-1]["text"] == ""
