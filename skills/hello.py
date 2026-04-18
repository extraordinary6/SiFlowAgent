from __future__ import annotations

from typing import Any

from context.manager import ContextManager
from skills.base import BaseSkill


class HelloSiFlowSkill(BaseSkill):
    def __init__(self, context_manager: ContextManager) -> None:
        super().__init__(
            name="hello_siflow",
            description="Return a built-in greeting for the SiFlowAgent framework. Use when the user asks what this agent is or requests a hello.",
            parameters_schema={},
        )
        self.context_manager = context_manager

    async def execute(self, **kwargs: Any) -> str:
        greeting = kwargs.get("greeting", "Hello SiFlow")
        self.context_manager.add_message("assistant", greeting)
        self.context_manager.set_state("last_skill", self.name)
        return greeting
