"""
全局配置：通过 pydantic-settings 读取 .env 环境变量
"""
from functools import lru_cache
from typing import Optional

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """应用全局配置（自动从 .env 加载）"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ---- LLM ----
    dashscope_api_key: str = Field(..., description="通义千问 API Key")
    llm_model: str = "qwen-max"
    embedding_model: str = "text-embedding-v3"
    llm_temperature: float = 0.7
    llm_max_tokens: int = 4096
    llm_request_timeout_seconds: float = 45.0

    # ---- MySQL ----
    mysql_host: str = "127.0.0.1"
    mysql_port: int = 3306
    mysql_user: str = "interview_agent"
    mysql_password: str = ""
    mysql_database: str = "interview_agent"

    @property
    def mysql_url(self) -> str:
        return (
            f"mysql+pymysql://{self.mysql_user}:{self.mysql_password}"
            f"@{self.mysql_host}:{self.mysql_port}/{self.mysql_database}"
            "?charset=utf8mb4"
        )

    # ---- Redis ----
    redis_host: str = "127.0.0.1"
    redis_port: int = 6379
    redis_password: Optional[str] = None
    redis_db: int = 0
    redis_short_term_ttl: int = 86400  # 短期记忆 24 小时过期
    redis_reconnect_initial_seconds: float = 1.0
    redis_reconnect_max_seconds: float = 30.0

    # ---- Distributed Tool Runtime state ----
    # memory preserves local development behaviour; redis shares reliability
    # controls across API workers.
    tool_runtime_state_backend: str = "memory"
    tool_runtime_redis_db: int = 3
    tool_runtime_state_prefix: str = "iam:tool-runtime"
    tool_runtime_idempotency_ttl_seconds: int = 3600

    # ---- Session checkpoint ----
    # Legacy MySQL session snapshot. LangGraph is the durable workflow source.
    session_checkpoint_enabled: bool = False

    # ---- Celery ----
    celery_enabled: bool = False
    celery_broker_url: str = "redis://127.0.0.1:6379/1"
    celery_result_backend: str = "redis://127.0.0.1:6379/2"
    celery_task_soft_time_limit_seconds: int = 150
    celery_task_time_limit_seconds: int = 180
    task_event_stream_enabled: bool = True
    task_event_transport: str = "pubsub"
    task_event_redis_db: int = 4
    task_event_stream_prefix: str = "iam:task-events"
    task_event_stream_maxlen: int = 500
    task_event_stream_block_ms: int = 1000

    # ---- Weaviate ----
    weaviate_url: str = "http://localhost:8080"
    weaviate_api_key: Optional[str] = None

    # ---- MCP / GitHub ----
    github_token: Optional[str] = None

    # ---- App ----
    app_host: str = "0.0.0.0"
    app_port: int = 8000
    log_level: str = "INFO"
    public_url: str = "http://localhost:8000"

    # ---- Speech to Text ----
    # 默认按首次语音请求懒加载，避免 Whisper 模型阻塞 API 启动。
    stt_preload: bool = False
    stt_model_size: str = "base"
    stt_device: str = "cpu"
    stt_compute_type: str = "int8"
    stt_language: str = "zh"
    stt_max_audio_bytes: int = 10 * 1024 * 1024
    stt_transcribe_timeout_seconds: float = 90.0
    stt_max_concurrent_jobs: int = 2

    # ---- Agent Runtime / Observability ----
    observability_enabled: bool = True
    trace_jsonl_enabled: bool = True
    trace_jsonl_path: str = "logs/agent_traces.jsonl"
    otel_service_name: str = "interview-agent"
    otel_exporter_otlp_endpoint: Optional[str] = None
    metrics_enabled: bool = True
    # Celery is a separate process, so it exposes its own scrape endpoint.
    # Set to 0 when the worker endpoint is not wanted.
    metrics_worker_port: int = 9109
    health_check_timeout_seconds: float = 2.0
    readiness_cache_seconds: float = 5.0
    agent_max_steps: int = 30
    agent_max_llm_calls: int = 12
    agent_max_tool_calls: int = 6
    agent_max_total_tokens: int = 30000
    agent_deadline_seconds: float = 120.0
    agent_context_max_chars: int = 6000
    agent_context_max_tokens: int = 1800
    agent_context_tokenizer_encoding: str = "cl100k_base"
    agent_planning_max_attempts: int = 2
    agent_planning_max_evidence_actions: int = 2

    # ---- LangGraph checkpoint ----
    langgraph_checkpoint_enabled: bool = True
    # sqlite: local development; mysql: shared multi-instance deployment.
    langgraph_checkpoint_backend: str = "sqlite"
    langgraph_checkpoint_path: str = "data/langgraph_checkpoints.sqlite3"
    # Empty means reuse MYSQL_* settings.
    langgraph_checkpoint_database_url: Optional[str] = None

    # ---- Authentication ----
    jwt_secret: str = "dev-only-change-me"
    jwt_expire_minutes: int = 1440

    # ---- RAG ----
    rag_top_k: int = 5
    rag_bm25_weight: float = 0.4
    rag_vector_weight: float = 0.6
    rag_retrieval_timeout_seconds: float = 8.0
    rag_rerank_enabled: bool = True
    rag_rerank_candidates_multiplier: int = 3
    # Skip expensive LLM reranking only when both retrieval branches strongly
    # agree on the leading evidence.
    rag_adaptive_rerank_enabled: bool = True
    rag_adaptive_rerank_threshold: float = 0.90
    # Do not stack an expensive LLM rerank after retrieval has already consumed
    # most of the request latency budget. Set <= 0 to disable this guard.
    rag_rerank_max_retrieval_latency_ms: float = 3000.0
    # Experimental: keep disabled until the hybrid A/B regression gate passes.
    rag_query_expansion_enabled: bool = False
    rag_metadata_filter_enabled: bool = True

    # ---- Difficulty FSM ----
    difficulty_levels: tuple = ("easy", "medium", "hard")
    consecutive_correct_to_upgrade: int = 2
    consecutive_wrong_to_downgrade: int = 2


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]


settings = get_settings()
