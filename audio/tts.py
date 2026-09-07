"""
TTS 语音合成：通义千问 CosyVoice API
- 流式合成（AsyncIterator[bytes]）供 WebSocket 实时推送
- 同步合成（bytes）供 REST API 返回
- 自动降级：API 不可用时返回空字节，不中断主流程
"""
from __future__ import annotations

import asyncio
import base64
import json
from http import HTTPStatus
from typing import AsyncIterator, Optional

import dashscope
import httpx
from loguru import logger

from config.settings import settings

# CosyVoice REST API 端点
_COSYVOICE_URL = (
    "https://dashscope.aliyuncs.com/api/v1/services/aigc/text2audio/text-synthesis"
)

# 支持的音色列表（供前端选择）
AVAILABLE_VOICES = [
    "longxiaochun",   # 龙小淳（女，温柔）
    "longxiaoxia",    # 龙小夏（女，活泼）
    "longjiangwai",   # 龙江外（男，沉稳）
    "longyue",        # 龙悦（女，专业）
]


class TTSEngine:
    """
    TTS 引擎（CosyVoice 流式 + 同步双模式）
    
    Args:
        voice:       音色名称
        audio_format: 输出格式，mp3 / wav / pcm
        sample_rate:  采样率
    """

    def __init__(
        self,
        voice: str        = "longxiaochun",
        audio_format: str = "mp3",
        sample_rate: int  = 22050,
    ) -> None:
        self.voice        = voice
        self.audio_format = audio_format
        self.sample_rate  = sample_rate
        self._api_key     = getattr(settings, "dashscope_api_key", "") or ""

        if not self._api_key:
            logger.warning("DASHSCOPE_API_KEY 未配置，TTS 功能不可用")

    @property
    def available(self) -> bool:
        return bool(self._api_key)

    # ── 流式合成（WebSocket 推流） ─────────────────────────────────────────────

    @staticmethod
    def _extract_audio_payload(data: dict) -> tuple[Optional[str], Optional[str]]:
        """从 DashScope JSON/SSE 响应中提取音频 URL 或 base64 数据。"""
        output = data.get("output") or {}
        audio = output.get("audio") if isinstance(output, dict) else None
        if isinstance(audio, dict):
            return audio.get("url"), audio.get("data") or audio.get("base64")
        if isinstance(audio, str):
            if audio.startswith("http"):
                return audio, None
            return None, audio
        return None, None

    async def _yield_audio_from_payload(
        self,
        client: httpx.AsyncClient,
        audio_url: Optional[str],
        audio_b64: Optional[str],
    ) -> AsyncIterator[bytes]:
        """把 URL/base64 音频转换成前端可播放的二进制块。"""
        if audio_b64:
            try:
                raw = base64.b64decode(audio_b64)
                if raw:
                    yield raw
            except Exception as e:
                logger.error("TTS base64 音频解析失败: {}", e)
            return

        if audio_url:
            async with client.stream("GET", audio_url) as audio_resp:
                if audio_resp.status_code != 200:
                    body = await audio_resp.aread()
                    logger.error(
                        "TTS 音频下载失败: {} {}", audio_resp.status_code, body[:200]
                    )
                    return
                async for chunk in audio_resp.aiter_bytes(chunk_size=4096):
                    if chunk:
                        yield chunk

    async def _yield_from_sse(
        self,
        client: httpx.AsyncClient,
        raw: bytes,
    ) -> AsyncIterator[bytes]:
        """解析 SSE 文本响应，提取其中的音频结果。"""
        text = raw.decode("utf-8", errors="ignore")
        emitted = False
        for line in text.splitlines():
            line = line.strip()
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if not payload or payload == "[DONE]":
                continue
            try:
                data = json.loads(payload)
            except json.JSONDecodeError:
                logger.debug("忽略无法解析的 TTS SSE 数据: {}", payload[:120])
                continue
            audio_url, audio_b64 = self._extract_audio_payload(data)
            async for chunk in self._yield_audio_from_payload(client, audio_url, audio_b64):
                emitted = True
                yield chunk
        if not emitted:
            logger.warning("TTS SSE 响应中未找到音频数据: {}", text[:300])

    async def synthesize_stream(
        self,
        text: str,
        voice: Optional[str] = None,
    ) -> AsyncIterator[bytes]:
        """
        流式合成音频，逐块 yield bytes
        
        Usage:
            async for chunk in tts_engine.synthesize_stream(text):
                await websocket.send_bytes(chunk)
        """
        if not self.available:
            logger.warning("TTS 不可用，跳过合成")
            return

        if not text.strip():
            return

        sdk_audio = await asyncio.to_thread(self._synthesize_with_sdk, text, voice)
        if sdk_audio:
            yield sdk_audio
            return

        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type":  "application/json",
            "X-DashScope-SSE": "enable",
        }
        payload = {
            "model": "cosyvoice-v1",
            "input": {"text": text},
            "parameters": {
                "voice":       voice or self.voice,
                "format":      self.audio_format,
                "sample_rate": self.sample_rate,
            },
        }

        try:
            async with httpx.AsyncClient(timeout=60) as client:
                resp = await client.post(_COSYVOICE_URL, headers=headers, json=payload)
                if resp.status_code != 200:
                    logger.error("TTS API 错误: {} {}", resp.status_code, resp.content[:200])
                    return

                content_type = resp.headers.get("content-type", "").lower()
                raw = resp.content
                if "audio/" in content_type or raw[:3] == b"ID3" or raw[:4] == b"RIFF":
                    yield raw
                    return

                if "text/event-stream" in content_type or raw.lstrip().startswith(b"event:"):
                    async for chunk in self._yield_from_sse(client, raw):
                        yield chunk
                    return

                try:
                    data = resp.json()
                except json.JSONDecodeError:
                    logger.error("TTS 返回了未知格式: type={}, body={}", content_type, raw[:300])
                    return

                audio_url, audio_b64 = self._extract_audio_payload(data)
                async for chunk in self._yield_audio_from_payload(client, audio_url, audio_b64):
                    yield chunk

        except httpx.TimeoutException:
            logger.error("TTS 请求超时")
        except Exception as e:
            logger.error("TTS 流式合成异常: {}", e)

    def _synthesize_with_sdk(self, text: str, voice: Optional[str] = None) -> bytes:
        """使用 DashScope 官方 SDK 合成，避免手写 REST 参数不兼容。"""
        try:
            from dashscope.audio.tts_v2 import AudioFormat, SpeechSynthesizer

            dashscope.api_key = self._api_key
            audio_format = (
                AudioFormat.MP3_22050HZ_MONO_256KBPS
                if self.audio_format == "mp3"
                else AudioFormat.WAV_22050HZ_MONO_16BIT
            )
            synthesizer = SpeechSynthesizer(
                model="cosyvoice-v1",
                voice=voice or self.voice,
                format=audio_format,
            )
            return synthesizer.call(text, timeout_millis=60000) or b""
        except Exception as e:
            logger.error("TTS SDK 合成异常: {}", e)
            return b""

    # ── 同步合成（REST API 返回） ──────────────────────────────────────────────

    def synthesize(
        self,
        text: str,
        voice: Optional[str] = None,
    ) -> bytes:
        """
        同步合成，返回完整音频字节
        
        适合 REST 接口：response = tts_engine.synthesize(text)
        """
        if not self.available or not text.strip():
            return b""

        chunks: list[bytes] = []

        async def _collect() -> None:
            async for chunk in self.synthesize_stream(text, voice=voice):
                chunks.append(chunk)

        try:
            # 兼容已有事件循环（FastAPI 环境）和无事件循环（脚本环境）
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    # 在 FastAPI 中，使用 asyncio.run_coroutine_threadsafe
                    import concurrent.futures
                    future = asyncio.run_coroutine_threadsafe(_collect(), loop)
                    future.result(timeout=60)
                else:
                    loop.run_until_complete(_collect())
            except RuntimeError:
                asyncio.run(_collect())
        except Exception as e:
            logger.error("TTS 同步合成失败: {}", e)
            return b""

        return b"".join(chunks)

    async def synthesize_async(
        self,
        text: str,
        voice: Optional[str] = None,
    ) -> bytes:
        """异步全量合成（FastAPI async 路由使用）"""
        chunks: list[bytes] = []
        async for chunk in self.synthesize_stream(text, voice=voice):
            chunks.append(chunk)
        return b"".join(chunks)


# 全局单例
tts_engine = TTSEngine()
