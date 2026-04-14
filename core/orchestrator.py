from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from loguru import logger

from context.manager import ContextManager
from core.llm_client import BaseLLMClient, LLMRequest
from skills.hello import HelloSiFlowSkill
from skills.registry import SkillRegistry
from skills.spec_summary import SpecSummarySkill


class Orchestrator:
    def __init__(
        self,
        prompt_dir: str | Path,
        context_manager: ContextManager | None = None,
        llm_client: BaseLLMClient | None = None,
    ) -> None:
        self.prompt_dir = Path(prompt_dir)
        self.context_manager = context_manager or ContextManager()
        self.llm_client = llm_client
        self.skill_registry = SkillRegistry()
        self._register_default_skills()

    def _register_default_skills(self) -> None:
        self.skill_registry.register(HelloSiFlowSkill(context_manager=self.context_manager))
        if self.llm_client is not None:
            self.skill_registry.register(
                SpecSummarySkill(context_manager=self.context_manager, llm_client=self.llm_client)
            )

    def load_prompt(self, prompt_name: str) -> dict[str, Any]:
        prompt_path = self.prompt_dir / f"{prompt_name}.yaml"
        if not prompt_path.exists():
            raise FileNotFoundError(f"Prompt not found: {prompt_path}")

        with prompt_path.open("r", encoding="utf-8") as file:
            data = yaml.safe_load(file) or {}

        logger.info("Loaded prompt from {}", prompt_path)
        return data

    async def hello_siflow(self) -> str:
        prompt = self.load_prompt("default")
        system_content = prompt.get("content", "You are SiFlowAgent.")

        self.context_manager.add_message("system", system_content)
        self.context_manager.set_state("last_task", "hello_siflow")

        result = await self.skill_registry.execute("hello_siflow")
        logger.info("Executed hello_siflow task via registry")
        return result

    async def summarize_spec(self, spec_text: str) -> str:
        if self.llm_client is None:
            raise RuntimeError("LLM client is not configured")

        self.context_manager.set_state("last_task", "spec_summary")
        self.context_manager.add_message("user", f"[spec_summary]\n{spec_text}")
        result = await self.skill_registry.execute("spec_summary", spec_text=spec_text)
        logger.info("Executed spec_summary skill")
        return result.markdown_summary

    async def chat(self, user_input: str, prompt_name: str = "default") -> str:
        if self.llm_client is None:
            raise RuntimeError("LLM client is not configured")

        prompt = self.load_prompt(prompt_name)
        system_content = prompt.get("content", "You are SiFlowAgent.")

        if not self.context_manager.get_history() or self.context_manager.get_history()[0].get("role") != "system":
            self.context_manager.add_message("system", system_content)

        self.context_manager.add_message("user", user_input)
        self.context_manager.set_state("last_task", "chat")

        response = await self.llm_client.generate(
            LLMRequest(
                system_prompt=system_content,
                messages=self.context_manager.get_messages_for_llm(),
            )
        )

        self.context_manager.add_message("assistant", response)
        logger.info("Completed chat turn")
        return response
