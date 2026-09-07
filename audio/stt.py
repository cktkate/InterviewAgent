"""
STT 语音识别：基于 faster-whisper 本地模型
支持 WAV / WebM / PCM，自动格式检测
"""
from __future__ import annotations

import io
import importlib.util
import subprocess
import tempfile
import os
import threading
from typing import Any, Callable, Optional

import numpy as np
from loguru import logger

from config import settings

try:
    from opencc import OpenCC
    _T2S_CONVERTER = OpenCC("t2s")
except ImportError:
    _T2S_CONVERTER = None

# 这里只探测包，不导入 faster_whisper；SDK 本身也较重，必须延迟到首次语音请求。
_WHISPER_AVAILABLE = importlib.util.find_spec("faster_whisper") is not None


def _detect_format(audio_bytes: bytes) -> str:
    """
    通过魔数检测音频格式
    Returns: 'wav' | 'webm' | 'ogg' | 'mp4' | 'unknown'
    """
    if len(audio_bytes) < 12:
        return 'unknown'
    header = audio_bytes[:12]
    if header[:4] == b'RIFF':
        return 'wav'
    if header[:4] == b'\x1aE\xdf\xa3' or header[:4] == b'\x1aE\xdf\xa3':
        return 'webm'
    # WebM/MKV EBML 头
    if header[:4] in (b'\x1aE\xdf\xa3',):
        return 'webm'
    # 更宽松的 WebM 检测（浏览器 MediaRecorder 输出）
    if b'webm' in audio_bytes[:64].lower() if hasattr(audio_bytes[:64], 'lower') else False:
        return 'webm'
    # OGG
    if header[:4] == b'OggS':
        return 'ogg'
    # MP4/M4A
    if header[4:8] in (b'ftyp', b'moov', b'mdat'):
        return 'mp4'
    # EBML (WebM)
    if header[:2] == b'\x1a\x45':
        return 'webm'
    return 'unknown'


def _convert_to_wav_ffmpeg(audio_bytes: bytes, fmt: str = 'webm') -> Optional[bytes]:
    """
    用 ffmpeg 将任意格式转为 16kHz 单声道 WAV
    需要系统安装 ffmpeg
    """
    in_path: Optional[str] = None
    out_path: Optional[str] = None
    try:
        with tempfile.NamedTemporaryFile(suffix=f'.{fmt}', delete=False) as fin:
            fin.write(audio_bytes)
            in_path = fin.name

        out_path = in_path.replace(f'.{fmt}', '.wav')

        result = subprocess.run(
            [
                'ffmpeg', '-y',
                '-i', in_path,
                '-ar', '16000',
                '-ac', '1',
                '-f', 'wav',
                out_path,
            ],
            capture_output=True,
            timeout=30,
        )

        if result.returncode != 0:
            logger.warning("ffmpeg 转换失败: {}", result.stderr.decode()[:200])
            return None

        with open(out_path, 'rb') as f:
            wav_bytes = f.read()

        return wav_bytes

    except FileNotFoundError:
        logger.warning("ffmpeg 未安装，无法转换音频格式")
        return None
    except Exception as e:
        logger.error("ffmpeg 转换异常: {}", e)
        return None
    finally:
        for p in [in_path, out_path]:
            try:
                if p and os.path.exists(p):
                    os.unlink(p)
            except Exception:
                pass


def _convert_to_float32_pyav(audio_bytes: bytes) -> Optional[np.ndarray]:
    """使用 PyAV 解码浏览器录音，无需系统安装 ffmpeg 可执行文件。"""
    try:
        import av

        chunks: list[np.ndarray] = []
        with av.open(io.BytesIO(audio_bytes), mode="r") as container:
            resampler = av.AudioResampler(format="flt", layout="mono", rate=16000)
            for frame in container.decode(audio=0):
                for converted in resampler.resample(frame):
                    chunks.append(converted.to_ndarray().reshape(-1).astype(np.float32))
            for converted in resampler.resample(None):
                chunks.append(converted.to_ndarray().reshape(-1).astype(np.float32))

        if chunks:
            return np.concatenate(chunks)
    except Exception as e:
        logger.warning("PyAV 解析失败: {}", e)
    return None


def _convert_to_float32_universal(audio_bytes: bytes) -> Optional[np.ndarray]:
    """
    通用音频字节 -> float32，自动检测格式
    优先用 ffmpeg，降级用 soundfile/librosa
    """
    fmt = _detect_format(audio_bytes)
    logger.debug("检测到音频格式: {}, 大小: {} bytes", fmt, len(audio_bytes))

    # WAV 直接解析
    if fmt == 'wav':
        try:
            from audio.audio_manager import bytes_to_float32
            return bytes_to_float32(audio_bytes, is_wav=True)
        except Exception as e:
            logger.warning("WAV 直接解析失败: {}, 尝试 ffmpeg", e)

    # faster-whisper 已依赖 PyAV，可直接解码浏览器产生的 WebM/Opus。
    # 这条路径不要求用户另行安装系统级 ffmpeg。
    audio = _convert_to_float32_pyav(audio_bytes)
    if audio is not None:
        return audio

    # 非 WAV 或 WAV 解析失败 -> ffmpeg 转换
    wav_bytes = _convert_to_wav_ffmpeg(audio_bytes, fmt=fmt if fmt != 'unknown' else 'webm')
    if wav_bytes:
        try:
            from audio.audio_manager import bytes_to_float32
            return bytes_to_float32(wav_bytes, is_wav=True)
        except Exception as e:
            logger.error("ffmpeg 转换后解析失败: {}", e)
            return None

    # ffmpeg 不可用 -> 尝试 soundfile
    try:
        import soundfile as sf
        audio, sr = sf.read(io.BytesIO(audio_bytes))
        if audio.ndim > 1:
            audio = audio.mean(axis=1)
        audio = audio.astype(np.float32)
        if sr != 16000:
            from audio.audio_manager import resample
            audio = resample(audio, sr, 16000)
        return audio
    except Exception as e:
        logger.warning("soundfile 解析失败: {}", e)

    # 最后降级：尝试 librosa
    try:
        import librosa
        audio, _ = librosa.load(io.BytesIO(audio_bytes), sr=16000, mono=True)
        return audio.astype(np.float32)
    except Exception as e:
        logger.error("librosa 解析失败: {}", e)

    return None


class STTEngine:
    """
    语音识别引擎（faster-whisper 本地推理）
    自动处理 WAV / WebM / OGG 等浏览器常见格式
    """

    def __init__(
        self,
        model_size:   str = "base",
        device:       str = "cpu",
        compute_type: str = "int8",
        language:     str = "zh",
    ) -> None:
        self.language = language
        self.model: Optional[Any] = None

        if not _WHISPER_AVAILABLE:
            logger.warning("faster-whisper 未安装，STT 功能不可用")
            return

        try:
            from faster_whisper import WhisperModel

            self.model = WhisperModel(
                model_size,
                device=device,
                compute_type=compute_type,
            )
            logger.info(
                "STT 模型加载完成: {} / {} / {}",
                model_size, device, compute_type,
            )
        except Exception as e:
            logger.error("STT 模型加载失败: {}", e)

    @property
    def available(self) -> bool:
        return self.model is not None

    # ── 核心接口 ──────────────────────────────────────────────────────────────

    def transcribe_bytes(
        self,
        audio_bytes: bytes,
        is_wav: bool = False,       # 保留参数兼容旧调用，但内部自动检测
        sample_rate: int = 16000,
    ) -> str:
        """
        通用入口：接收任意格式音频字节，返回识别文本
        自动检测格式，无需调用方指定
        """
        if not self.available:
            logger.warning("STT 不可用")
            return ""

        if not audio_bytes:
            return ""

        try:
            audio = _convert_to_float32_universal(audio_bytes)
            if audio is None or len(audio) == 0:
                logger.warning("音频转换失败，无法识别")
                return ""
            return self._run_whisper(audio)
        except Exception as e:
            logger.error("STT transcribe_bytes 失败: {}", e)
            return ""

    def transcribe_wav(self, wav_bytes: bytes) -> str:
        """WAV 格式快捷入口"""
        return self.transcribe_bytes(wav_bytes, is_wav=True)

    def transcribe_pcm(self, pcm_bytes: bytes, sample_rate: int = 16000) -> str:
        """PCM 原始字节快捷入口"""
        if not self.available:
            return ""
        try:
            audio = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32) / 32768.0
            if sample_rate != 16000:
                from audio.audio_manager import resample
                audio = resample(audio, sample_rate, 16000)
            return self._run_whisper(audio)
        except Exception as e:
            logger.error("STT transcribe_pcm 失败: {}", e)
            return ""

    def transcribe_array(self, audio: np.ndarray) -> str:
        """直接接收 float32 numpy 数组"""
        if not self.available:
            return ""
        try:
            return self._run_whisper(audio)
        except Exception as e:
            logger.error("STT transcribe_array 失败: {}", e)
            return ""

    # ── 内部方法 ──────────────────────────────────────────────────────────────

    def _run_whisper(self, audio: np.ndarray) -> str:
        """调用 faster-whisper 推理"""
        if len(audio) < 1600:   # 少于 0.1 秒，跳过
            logger.debug("音频太短，跳过识别")
            return ""

        segments, info = self.model.transcribe(
            audio,
            beam_size=5,
            language=self.language,
            initial_prompt="以下是普通话内容，请使用简体中文进行转写。",
            vad_filter=True,
            vad_parameters={"min_silence_duration_ms": 300},
        )
        text = " ".join(s.text.strip() for s in segments).strip()
        if _T2S_CONVERTER is not None:
            text = _T2S_CONVERTER.convert(text)
        logger.debug(
            "STT 识别完成: lang={} prob={:.2f} text={}",
            info.language,
            info.language_probability,
            text[:80],
        )
        return text


class LazySTTEngine:
    """线程安全的 Whisper 懒加载代理。

    模块导入只创建这个轻量代理；真正的 ``WhisperModel`` 由首次语音请求或
    显式 ``ensure_loaded`` 创建。多个 WebSocket 同时首次使用时只会加载一次。
    """

    def __init__(self, factory: Optional[Callable[[], STTEngine]] = None) -> None:
        self._factory = factory or self._default_factory
        self._engine: Optional[STTEngine] = None
        self._lock = threading.Lock()
        self._status = "unloaded"  # unloaded/loading/ready/unavailable/failed
        self._load_error = ""

    @staticmethod
    def _default_factory() -> STTEngine:
        return STTEngine(
            model_size=settings.stt_model_size,
            device=settings.stt_device,
            compute_type=settings.stt_compute_type,
            language=settings.stt_language,
        )

    @property
    def status(self) -> str:
        return self._status

    @property
    def is_loaded(self) -> bool:
        return self._status in {"ready", "unavailable", "failed"}

    @property
    def available(self) -> bool:
        if self._status == "ready":
            return True
        if self._status in {"unavailable", "failed"}:
            return False
        return _WHISPER_AVAILABLE

    @property
    def load_error(self) -> str:
        return self._load_error

    def ensure_loaded(self) -> Optional[STTEngine]:
        if self._engine is not None:
            return self._engine if self._engine.available else None
        if self._status in {"unavailable", "failed"}:
            return None
        with self._lock:
            if self._engine is not None:
                return self._engine if self._engine.available else None
            if self._status in {"unavailable", "failed"}:
                return None
            self._status = "loading"
            logger.info(
                "首次使用语音识别，开始懒加载 STT 模型: {} / {} / {}",
                settings.stt_model_size,
                settings.stt_device,
                settings.stt_compute_type,
            )
            try:
                engine = self._factory()
                self._engine = engine
                if engine.available:
                    self._status = "ready"
                    return engine
                self._status = "unavailable"
                self._load_error = "STT 模型不可用或加载失败"
            except Exception as exc:
                self._status = "failed"
                self._load_error = str(exc)
                logger.exception("STT 懒加载失败: {}", exc)
            return None

    def transcribe_bytes(
        self,
        audio_bytes: bytes,
        is_wav: bool = False,
        sample_rate: int = 16000,
    ) -> str:
        engine = self.ensure_loaded()
        if engine is None:
            logger.warning("STT 不可用: {}", self._load_error or self._status)
            return ""
        return engine.transcribe_bytes(
            audio_bytes,
            is_wav=is_wav,
            sample_rate=sample_rate,
        )


# 轻量全局代理；不会在 import 阶段加载 WhisperModel。
stt_engine = LazySTTEngine()
