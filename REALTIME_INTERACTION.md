# 实时语音交互链路

## STT 生命周期

`audio.stt` 导入时只创建 `LazySTTEngine`，不导入 `faster_whisper` SDK，
也不创建 `WhisperModel`。模型在首次收到语音时由线程安全单例加载，多个并发
首次请求只会执行一次加载。

默认配置：

```env
STT_PRELOAD=false
STT_MODEL_SIZE=base
STT_DEVICE=cpu
STT_COMPUTE_TYPE=int8
STT_LANGUAGE=zh
STT_MAX_AUDIO_BYTES=10485760
STT_TRANSCRIBE_TIMEOUT_SECONDS=90
STT_MAX_CONCURRENT_JOBS=2
```

`STT_PRELOAD=true` 会在 FastAPI startup 完成后提交后台预热任务，不等待模型
加载完成，因此不会阻塞 Uvicorn 监听端口。

## 非阻塞识别

WebSocket 收到浏览器上传的完整音频后：

1. 校验字节大小；
2. 发送 `stt_status=accepted`；
3. 首次使用时发送 `loading_model`；
4. 在受并发信号量约束的 `asyncio.to_thread` 工作线程中完成解码和 Whisper 推理；
5. 超时或异常返回结构化失败；
6. 成功后发送 `completed` 和兼容原前端协议的 `stt_result`。

STT 仅填充前端输入框，不会自动提交正式回答。

## 状态协议

```json
{
  "type": "stt_status",
  "request_id": "...",
  "status": "accepted|loading_model|processing|completed|failed",
  "duration_ms": 1234.5,
  "code": "audio_too_large|stt_unavailable|model_load_failed|transcribe_timeout|transcribe_error"
}
```

前端在加载、排队与识别阶段显示不同提示，并在识别结束前阻止重复录音。
