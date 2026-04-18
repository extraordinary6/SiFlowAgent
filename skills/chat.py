from __future__ import annotations

from typing import Any

from context.manager import ContextManager
from core.llm_client import BaseLLMClient, LLMRequest
from skills.base import BaseSkill


class ChatSkill(BaseSkill):
    def __init__(
        self,
        context_manager: ContextManager,
        llm_client: BaseLLMClient,
        system_prompt: str,
    ) -> None:
        super().__init__(
            name="chat",
            description="Hold a free-form conversation with the user. Use this for greetings, small talk, clarifying questions, or any request that does not clearly match a specialized skill.",
            parameters_schema={
                "message": {
                    "type": "string",
                    "required": True,
                    "description": "The user's message to respond to.",
                },
            },
        )
        self.context_manager = context_manager
        self.llm_client = llm_client
        self.system_prompt = system_prompt

    async def execute(self, **kwargs: Any) -> str:
        message = str(kwargs.get("message", "")).strip()
        if not message:
            raise ValueError("message is required")

        self.context_manager.add_message("user", message)
        response = await self.llm_client.generate(
            LLMRequest(
                system_prompt=self.system_prompt,
                messages=self.context_manager.get_messages_for_llm(),
            )
        )
        self.context_manager.add_message("assistant", response)
        self.context_manager.set_state("last_skill", self.name)
        return response
