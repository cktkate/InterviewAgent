"""
Agent 基类：所有专职 Agent 的统一接口与公共能力
- 统一 run() 入口
- 统一 LLM 调用封装（基于 LangChain ChatTongyi）
- 统一日志 / 错误重试
"""
from __future__ import annotations

import json
import time
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional, Type

from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from langchain_core.prompts import ChatPromptTemplate
from langchain_openai import ChatOpenAI
from loguru import logger
from opentelemetry.trace import Status, StatusCode
from pydantic import BaseModel, ValidationError
from tenacity import retry, retry_if_not_exception_type, stop_after_attempt, wait_exponential

from config import settings
from observability.tracing import get_tracer
from observability.metrics import record_llm_call
from orchestration.budget import (
    AgentBudgetExceeded,
    before_llm_call,
    record_llm_tokens,
    record_structured_retry,
)


class BaseAgent(ABC):
    """所有 Agent 的抽象基类"""

    name: str = "base"
    description: str = ""

    def __init__(self) -> None:
        # 兼容 OpenAI 接口调用通义千问（通过 DashScope 兼容模式）
        self.llm = ChatOpenAI(
            model=settings.llm_model,
            temperature=settings.llm_temperature,
            max_tokens=settings.llm_max_tokens,
            api_key=settings.dashscope_api_key,
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
            timeout=settings.llm_request_timeout_seconds,
        )
        self.logger = logger.bind(agent=self.name)

    # ---------- 公共 LLM 调用（带重试） ----------
    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=8),
        retry=retry_if_not_exception_type(AgentBudgetExceeded),
        reraise=True,
    )
    def invoke_llm(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        temperature: Optional[float] = None,
        response_format: Optional[Dict[str, Any]] = None,
    ) -> str:
        """调用 LLM 并返回文本，自动重试"""
        before_llm_call()
        messages: List[BaseMessage] = [
            SystemMessage(content=system_prompt),
            HumanMessage(content=user_prompt),
        ]
        llm = self.llm
        if temperature is not None:
            llm = self.llm.bind(temperature=temperature)
        if response_format:
            llm = llm.bind(response_format=response_format)  # type: ignore[attr-defined]
        tracer = get_tracer()
        started = time.perf_counter()
        metric_recorded = False
        with tracer.start_as_current_span("llm.invoke") as span:
            span.set_attribute("llm.agent.name", self.name)
            span.set_attribute("llm.model", settings.llm_model)
            span.set_attribute("llm.system_prompt_chars", len(system_prompt))
            span.set_attribute("llm.user_prompt_chars", len(user_prompt))
            try:
                resp = llm.invoke(messages)
                content = resp.content or ""
                span.set_attribute("llm.response_chars", len(str(content)))
                usage = getattr(resp, "usage_metadata", None) or {}
                input_tokens = int(usage.get("input_tokens") or 0)
                output_tokens = int(usage.get("output_tokens") or 0)
                # Provider call succeeded even if the Harness rejects the run
                # after accounting pushes it over its token budget.
                record_llm_call(
                    settings.llm_model,
                    "success",
                    time.perf_counter() - started,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                )
                metric_recorded = True
                record_llm_tokens(input_tokens, output_tokens)
                for source_key, attribute_key in (
                    ("input_tokens", "llm.tokens.input"),
                    ("output_tokens", "llm.tokens.output"),
                    ("total_tokens", "llm.tokens.total"),
                ):
                    value = usage.get(source_key)
                    if isinstance(value, int):
                        span.set_attribute(attribute_key, value)
                span.set_attribute("llm.status", "success")
                span.set_status(Status(StatusCode.OK))
                return content
            except Exception as exc:
                span.set_attribute("llm.status", "failed")
                span.set_attribute("error.type", type(exc).__name__)
                span.record_exception(exc)
                span.set_status(Status(StatusCode.ERROR, str(exc)[:300]))
                if not metric_recorded:
                    record_llm_call(
                        settings.llm_model,
                        "failure",
                        time.perf_counter() - started,
                    )
                raise

    @staticmethod
    def _parse_json_object(raw: str) -> Dict[str, Any]:
        """Parse a JSON object, tolerating surrounding prose or code fences."""
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            start, end = raw.find("{"), raw.rfind("}") + 1
            if start < 0 or end <= start:
                raise
            parsed = json.loads(raw[start:end])
        if not isinstance(parsed, dict):
            raise ValueError("LLM output must be a JSON object")
        return parsed

    def invoke_llm_json(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        schema: Optional[Type[BaseModel]] = None,
        max_output_attempts: int = 3,
    ) -> Dict[str, Any]:
        """Generate, parse and optionally validate structured LLM output.

        Parsing or schema failures are fed back to the model for a bounded
        repair attempt. Transport failures remain covered by invoke_llm's
        exponential-backoff retry.
        """
        if max_output_attempts < 1:
            raise ValueError("max_output_attempts must be at least 1")

        schema_text = ""
        if schema is not None:
            schema_text = json.dumps(
                schema.model_json_schema(), ensure_ascii=False
            )

        base_system_prompt = (
            system_prompt
            + "\n\n请严格以合法 JSON 对象输出，不要使用 Markdown，不要输出任何多余文字。"
        )
        if schema_text:
            base_system_prompt += f"\n输出必须符合以下 JSON Schema：\n{schema_text}"

        current_user_prompt = user_prompt
        last_error: Optional[Exception] = None
        last_raw = ""

        for attempt in range(1, max_output_attempts + 1):
            last_raw = self.invoke_llm(
                base_system_prompt,
                current_user_prompt,
                temperature=0.2,
                response_format={"type": "json_object"},
            )
            try:
                parsed = self._parse_json_object(last_raw)
                if schema is None:
                    return parsed
                validated = schema.model_validate(parsed)
                return validated.model_dump(mode="json")
            except (json.JSONDecodeError, ValidationError, ValueError) as exc:
                last_error = exc
                record_structured_retry()
                current_span = get_tracer().start_span("llm.structured_output.retry")
                current_span.set_attribute("llm.agent.name", self.name)
                current_span.set_attribute("llm.retry.attempt", attempt)
                current_span.set_attribute("llm.retry.max_attempts", max_output_attempts)
                current_span.set_attribute("error.type", type(exc).__name__)
                current_span.add_event("structured_output_validation_failed")
                current_span.end()
                self.logger.warning(
                    "结构化输出第 {}/{} 次校验失败：{}",
                    attempt,
                    max_output_attempts,
                    str(exc)[:500],
                )
                if attempt == max_output_attempts:
                    break
                current_user_prompt = (
                    f"{user_prompt}\n\n"
                    "你上一次的输出未通过校验。请根据错误修正后，重新输出完整 JSON 对象。\n"
                    f"校验错误：{str(exc)[:1500]}\n"
                    f"上一次输出：{last_raw[:3000]}"
                )

        raise ValueError(
            f"LLM structured output failed after {max_output_attempts} attempts: "
            f"{last_error}; last_output={last_raw[:500]!r}"
        ) from last_error

    def build_prompt(self, template: str, **kwargs: Any) -> str:
        return ChatPromptTemplate.from_template(template).format(**kwargs)

    # ---------- 统一入口 ----------
    @abstractmethod
    def run(self, state: Dict[str, Any]) -> Dict[str, Any]:
        """LangGraph 节点签名：state -> state"""
        ...

    def __repr__(self) -> str:
        return f"<Agent name={self.name}>"
