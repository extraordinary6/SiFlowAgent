from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, Field

from context.manager import ContextManager
from core.llm_client import BaseLLMClient, LLMRequest
from skills.base import BaseSkill


class RouterDecision(BaseModel):
    skill: str = Field(..., description="Name of the chosen skill")
    args: dict[str, Any] = Field(
        default_factory=dict,
        description="Arguments to pass to the chosen skill, matching its parameters_schema",
    )
    reasoning: str = Field(
        default="",
        description="Short natural-language reason explaining this routing decision",
    )


def _strip_json_fences(text: str) -> str:
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    lines = stripped.splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines).strip()


class RouterSkill(BaseSkill):
    def __init__(self, context_manager: ContextManager, llm_client: BaseLLMClient) -> None:
        super().__init__(
            name="router",
            description="Internal skill. Pick the most appropriate downstream skill for a user message by inspecting registered skill metadata.",
            parameters_schema={
                "user_input": {
                    "type": "string",
                    "required": True,
                    "description": "Raw natural-language message from the user.",
                },
                "skill_catalog": {
                    "type": "array",
                    "required": True,
                    "description": "List of available skills with name, description, parameters_schema.",
                },
            },
        )
        self.context_manager = context_manager
        self.llm_client = llm_client

    async def execute(self, **kwargs: Any) -> RouterDecision:
        user_input = str(kwargs.get("user_input", "")).strip()
        skill_catalog = kwargs.get("skill_catalog") or []
        if not user_input:
            raise ValueError("user_input is required")
        if not skill_catalog:
            raise ValueError("skill_catalog is required")

        catalog_text = json.dumps(skill_catalog, ensure_ascii=False, indent=2)
        runtime_state = {
            "last_skill": self.context_manager.get_state("last_skill"),
            "has_spec_summary": self.context_manager.get_state("last_spec_summary") is not None,
        }
        state_text = json.dumps(runtime_state, ensure_ascii=False)

        raw = await self.llm_client.generate(
            LLMRequest(
                system_prompt=(
                    "You are the routing brain of SiFlowAgent. "
                    "Pick exactly ONE skill from the provided catalog that best handles the user's message. "
                    "Return valid JSON only, with no markdown fences and no extra commentary. "
                    'Schema: {"skill": string, "args": object, "reasoning": string}. '
                    "Rules:\n"
                    "- 'skill' MUST be one of the names in the catalog. Never invent a new name.\n"
                    "- 'args' keys MUST come from that skill's parameters_schema. Omit optional args when not needed.\n"
                    "- Prefer 'chat' for greetings, small talk, meta-questions about the agent, or whenever no specialized skill clearly fits.\n"
                    "- For 'spec_summary', pass either spec_text (raw text the user provided inline) or spec_path (a file path the user mentioned).\n"
                    "- For 'verilog_template', only choose it when runtime_state.has_spec_summary is true. Include output_path only if the user clearly asked to save to a file or directory.\n"
                    "- Keep 'reasoning' to one short sentence in the same language the user used."
                ),
                messages=[
                    {
                        "role": "user",
                        "content": (
                            f"Skill catalog:\n{catalog_text}\n\n"
                            f"Runtime state:\n{state_text}\n\n"
                            f"User message:\n{user_input}"
                        ),
                    }
                ],
                temperature=0.0,
                max_tokens=400,
            )
        )

        cleaned = _strip_json_fences(raw)
        data = json.loads(cleaned)
        decision = RouterDecision(**data)
        self.context_manager.set_state("last_router_decision", decision.model_dump())
        return decision
