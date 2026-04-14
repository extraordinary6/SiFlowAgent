from __future__ import annotations

import os
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv
from openai import AsyncOpenAI
from pydantic import BaseModel, Field

load_dotenv(dotenv_path=Path(__file__).resolve().parent.parent / ".env")


class LLMRequest(BaseModel):
    system_prompt: str = Field(..., description="System prompt for the model")
    messages: list[dict[str, str]] = Field(default_factory=list, description="Conversation history")
    temperature: float = Field(default=0.2, description="Sampling temperature")
    max_tokens: int = Field(default=1024, description="Maximum output tokens")


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


class OpenAICompatibleClient(BaseLLMClient):
    def __init__(self, config: OpenAICompatibleConfig) -> None:
        self.config = config
        self.client = AsyncOpenAI(
            api_key=config.api_key,
            base_url=config.base_url,
            timeout=config.timeout,
        )

    async def generate(self, request: LLMRequest) -> str:
        response = await self.client.chat.completions.create(
            model=self.config.model,
            messages=[
                {"role": "system", "content": _sanitize_text(request.system_prompt)},
                *_sanitize_messages(request.messages),
            ],
            temperature=request.temperature,
            max_tokens=request.max_tokens,
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
        if not content:
            raise ValueError("Messages API returned empty content")
        return content


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
