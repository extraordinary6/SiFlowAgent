from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, Field

from context.manager import ContextManager
from core.llm_client import BaseLLMClient, LLMRequest
from skills.base import BaseSkill


class SignalSummary(BaseModel):
    name: str = Field(..., description="Signal name")
    direction: str = Field(..., description="input, output, inout, or unknown")
    width: str = Field(default="1", description="Bit width or range description")
    description: str = Field(default="", description="Signal purpose or notes")


class SubmoduleSummary(BaseModel):
    name: str = Field(..., description="Submodule name")
    role: str = Field(default="", description="Submodule responsibility")


class SpecSummaryResult(BaseModel):
    module_name: str | None = Field(default=None)
    overview: str = Field(default="")
    interfaces: list[SignalSummary] = Field(default_factory=list)
    functional_behavior: list[str] = Field(default_factory=list)
    timing_and_control: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)
    submodules: list[SubmoduleSummary] = Field(default_factory=list)
    interconnects: list[str] = Field(default_factory=list)
    markdown_summary: str = Field(default="")


class SpecSummarySkill(BaseSkill):
    def __init__(self, context_manager: ContextManager, llm_client: BaseLLMClient) -> None:
        super().__init__(
            name="spec_summary",
            description="Summarize a hardware spec into interfaces, behavior, timing, and open questions",
        )
        self.context_manager = context_manager
        self.llm_client = llm_client

    async def execute(self, **kwargs: Any) -> SpecSummaryResult:
        spec_text = str(kwargs.get("spec_text", "")).strip()
        if not spec_text:
            raise ValueError("spec_text is required")

        raw_json = await self.llm_client.generate(
            LLMRequest(
                system_prompt=(
                    "You are an expert hardware architect. Extract a structured summary from the given hardware spec. "
                    "Return valid JSON only, with no markdown fences and no extra text. "
                    "Use this exact schema: "
                    '{"module_name": string|null, "overview": string, "interfaces": '
                    '[{"name": string, "direction": string, "width": string, "description": string}], '
                    '"functional_behavior": [string], "timing_and_control": [string], '
                    '"constraints": [string], "open_questions": [string], '
                    '"submodules": [{"name": string, "role": string}], "interconnects": [string]}. '
                    "If the spec implies a multi-module architecture, list the likely submodules and high-level interconnect notes. "
                    "If it does not, return empty arrays for submodules and interconnects. "
                    "Do not invent facts. If a field is unknown, use null, an empty string, or an empty list."
                ),
                messages=[
                    {
                        "role": "user",
                        "content": f"Extract a structured summary from this hardware spec:\n\n{spec_text}",
                    }
                ],
                temperature=0.1,
                max_tokens=1800,
            )
        )

        data = json.loads(raw_json)
        result = SpecSummaryResult(**data)
        result.markdown_summary = self._to_markdown(result)

        self.context_manager.add_message("assistant", result.markdown_summary)
        self.context_manager.set_state("last_skill", self.name)
        self.context_manager.set_state("last_spec_summary", result.model_dump())
        return result

    def _to_markdown(self, result: SpecSummaryResult) -> str:
        lines: list[str] = []
        lines.append("## Overview")
        lines.append(result.overview or "Not specified.")
        lines.append("")

        lines.append("## Interfaces")
        if result.interfaces:
            for signal in result.interfaces:
                description = f" - {signal.description}" if signal.description else ""
                lines.append(f"- `{signal.direction}` `{signal.name}` [{signal.width}]{description}")
        else:
            lines.append("Not specified.")
        lines.append("")

        lines.append("## Functional Behavior")
        if result.functional_behavior:
            lines.extend(f"- {item}" for item in result.functional_behavior)
        else:
            lines.append("- Not specified.")
        lines.append("")

        lines.append("## Timing and Control")
        if result.timing_and_control:
            lines.extend(f"- {item}" for item in result.timing_and_control)
        else:
            lines.append("- Not specified.")
        lines.append("")

        lines.append("## Constraints")
        if result.constraints:
            lines.extend(f"- {item}" for item in result.constraints)
        else:
            lines.append("- Not specified.")
        lines.append("")

        lines.append("## Submodules")
        if result.submodules:
            for submodule in result.submodules:
                role = f" - {submodule.role}" if submodule.role else ""
                lines.append(f"- `{submodule.name}`{role}")
        else:
            lines.append("- None.")
        lines.append("")

        lines.append("## Interconnects")
        if result.interconnects:
            lines.extend(f"- {item}" for item in result.interconnects)
        else:
            lines.append("- Not specified.")
        lines.append("")

        lines.append("## Open Questions")
        if result.open_questions:
            lines.extend(f"- {item}" for item in result.open_questions)
        else:
            lines.append("- None.")

        return "\n".join(lines).strip()
