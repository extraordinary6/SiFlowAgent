from __future__ import annotations

import os
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv
from loguru import logger
from openai import AsyncOpenAI
from pydantic import BaseModel, Field

load_dotenv(dotenv_path=Path(__file__).resolve().parent.parent / ".env")


class LLMRequest(BaseModel):
    system_prompt: str = Field(..., description="System prompt for the model")
    messages: list[dict[str, str]] = Field(default_factory=list, description="Conversation history")
    temperature: float = Field(default=0.2, description="Sampling temperature")
    max_tokens: int = Field(default=1024, description="Answer-token budget. Thinking budget (when enabled) is added on top.")
    disable_thinking: bool = Field(
        default=False,
        description=(
            "Suppress extended reasoning. OpenAI-compatible: passes "
            "reasoning_effort=minimal via extra_body. Messages API: passes "
            "thinking={type:'disabled'} in the payload. Has no effect on "
            "non-thinking models. Mutually exclusive with thinking_budget — "
            "if both are set, thinking_budget wins."
        ),
    )
    thinking_budget: int | None = Field(
        default=None,
        description=(
            "Opt in to extended reasoning with this many tokens of budget. "
            "OpenAI-compatible: maps to reasoning_effort low/medium/high "
            "(API does not expose token-precise control). Messages API: "
            "passes thinking={type:'enabled', budget_tokens:N} and bumps "
            "max_tokens by N so the answer is not starved. None means the "
            "backend default."
        ),
    )


class OpenAICompatibleConfig(BaseModel):
    base_url: str = Field(..., description="OpenAI-compatible base URL")
    api_key: str = Field(..., description="API key for the relay service")
    model: str = Field(..., description="Model name exposed by the relay")
    timeout: float = Field(default=60.0, description="Request timeout in seconds")


class MessagesAPIConfig(BaseModel):
    base_url: str = Field(..., description="Base URL for the messages API relay")
    api_key: str = Field(..., description="API key for the relay service")
    model: str = Field(..., description="Model name exposed by the relay")
    timeout: float = Field(default=60.0, description="Request timeout in seconds")
    anthropic_version: str = Field(default="2023-06-01", description="Anthropic messages API version")


class BaseLLMClient(ABC):
    @abstractmethod
    async def generate(self, request: LLMRequest) -> str:
        raise NotImplementedError


def _sanitize_text(value: str) -> str:
    return value.encode("utf-8", errors="replace").decode("utf-8")


def _sanitize_messages(messages: list[dict[str, str]]) -> list[dict[str, str]]:
    return [
        {
            "role": _sanitize_text(message["role"]),
            "content": _sanitize_text(message["content"]),
        }
        for message in messages
    ]


def _budget_to_effort(budget: int) -> str:
    """Map a thinking-token budget to OpenAI's reasoning_effort tier.

    OpenAI's API exposes only four discrete tiers, so token-precise control
    is not possible on that backend; the bands below are a coarse heuristic.
    """
    if budget <= 0:
        return "minimal"
    if budget < 1024:
        return "low"
    if budget < 4096:
        return "medium"
    return "high"


def _resolve_thinking_mode(request: LLMRequest) -> tuple[str, int]:
    """Reduce the (disable_thinking, thinking_budget) pair to one decision.

    Returns ``("budget", N)``, ``("disabled", 0)``, or ``("default", 0)``.
    When both fields are set, ``thinking_budget`` wins and the conflict is
    logged once so callers notice. Mutual exclusivity is documented on the
    ``LLMRequest`` fields.
    """
    if request.thinking_budget is not None and request.thinking_budget > 0:
        if request.disable_thinking:
            logger.warning(
                "LLMRequest: disable_thinking=True ignored because thinking_budget={} is set",
                request.thinking_budget,
            )
        return "budget", int(request.thinking_budget)
    if request.disable_thinking:
        return "disabled", 0
    return "default", 0


class OpenAICompatibleClient(BaseLLMClient):
    def __init__(self, config: OpenAICompatibleConfig) -> None:
        self.config = config
        self.client = AsyncOpenAI(
            api_key=config.api_key,
            base_url=config.base_url,
            timeout=config.timeout,
        )

    async def generate(self, request: LLMRequest) -> str:
        kwargs: dict[str, Any] = {}
        mode, budget = _resolve_thinking_mode(request)
        if mode == "disabled":
            kwargs["extra_body"] = {"reasoning_effort": "minimal"}
        elif mode == "budget":
            # OpenAI's reasoning_effort is a tier, not a token count, so the
            # budget gets bucketed. Token-level precision is only available on
            # the messages API path below.
            kwargs["extra_body"] = {"reasoning_effort": _budget_to_effort(budget)}

        response = await self.client.chat.completions.create(
            model=self.config.model,
            messages=[
                {"role": "system", "content": _sanitize_text(request.system_prompt)},
                *_sanitize_messages(request.messages),
            ],
            temperature=request.temperature,
            max_tokens=request.max_tokens,
            **kwargs,
        )

        content = response.choices[0].message.content
        if not content:
            raise ValueError("Model returned empty content")
        return content


class MessagesAPIClient(BaseLLMClient):
    def __init__(self, config: MessagesAPIConfig) -> None:
        self.config = config

    async def generate(self, request: LLMRequest) -> str:
        payload: dict[str, Any] = {
            "model": self.config.model,
            "system": _sanitize_text(request.system_prompt),
            "messages": _sanitize_messages(request.messages),
            "max_tokens": request.max_tokens,
            "temperature": request.temperature,
        }
        mode, budget = _resolve_thinking_mode(request)
        if mode == "disabled":
            # Anthropic's `thinking.type=disabled` switch. Relays that proxy GLM,
            # Kimi-think, etc. usually accept the same key; backends that do not
            # support extended thinking ignore the field harmlessly.
            payload["thinking"] = {"type": "disabled"}
        elif mode == "budget":
            # The caller's max_tokens describes the *answer* size; the thinking
            # budget is added on top so reasoning cannot starve the answer.
            payload["thinking"] = {"type": "enabled", "budget_tokens": budget}
            payload["max_tokens"] = request.max_tokens + budget
        headers = {
            "x-api-key": self.config.api_key,
            "anthropic-version": self.config.anthropic_version,
            "content-type": "application/json",
        }

        async with httpx.AsyncClient(timeout=self.config.timeout) as client:
            response = await client.post(
                f"{self.config.base_url.rstrip('/')}/v1/messages",
                headers=headers,
                json=payload,
            )
            response.raise_for_status()
            data = response.json()

        content_blocks = data.get("content", [])
        text_parts = [block.get("text", "") for block in content_blocks if block.get("type") == "text"]
        content = "\n".join(part for part in text_parts if part).strip()
        if content:
            return content

        # Fallback for thinking-style models (Claude extended thinking, GLM-5.x,
        # Kimi-think, etc.) that may return only ``type: "thinking"`` blocks when
        # the token budget is exhausted by the reasoning trace. Surfacing the
        # thinking text lets downstream parsers fail loudly with diagnosable
        # input instead of crashing on an empty string.
        thinking_parts = [block.get("thinking", "") for block in content_blocks if block.get("type") == "thinking"]
        thinking = "\n".join(part for part in thinking_parts if part).strip()
        if thinking:
            return thinking

        raise ValueError("Messages API returned empty content")


def load_openai_compatible_config_from_env() -> OpenAICompatibleConfig | None:
    base_url = os.getenv("SIFLOW_LLM_BASE_URL")
    api_key = os.getenv("SIFLOW_LLM_API_KEY")
    model = os.getenv("SIFLOW_LLM_MODEL")
    timeout = os.getenv("SIFLOW_LLM_TIMEOUT")

    if not base_url or not api_key or not model:
        return None

    return OpenAICompatibleConfig(
        base_url=base_url,
        api_key=api_key,
        model=model,
        timeout=float(timeout) if timeout else 60.0,
    )


def load_messages_api_config_from_env() -> MessagesAPIConfig | None:
    base_url = os.getenv("SIFLOW_LLM_BASE_URL")
    api_key = os.getenv("SIFLOW_LLM_API_KEY")
    model = os.getenv("SIFLOW_LLM_MODEL")
    timeout = os.getenv("SIFLOW_LLM_TIMEOUT")
    anthropic_version = os.getenv("SIFLOW_LLM_ANTHROPIC_VERSION")

    if not base_url or not api_key or not model:
        return None

    return MessagesAPIConfig(
        base_url=base_url,
        api_key=api_key,
        model=model,
        timeout=float(timeout) if timeout else 60.0,
        anthropic_version=anthropic_version or "2023-06-01",
    )


def load_llm_client_from_env() -> BaseLLMClient | None:
    provider = os.getenv("SIFLOW_LLM_PROVIDER", "messages_api").strip().lower()

    if provider == "openai":
        config = load_openai_compatible_config_from_env()
        return OpenAICompatibleClient(config) if config else None

    if provider == "messages_api":
        config = load_messages_api_config_from_env()
        return MessagesAPIClient(config) if config else None

    raise ValueError(f"Unsupported SIFLOW_LLM_PROVIDER: {provider}")
