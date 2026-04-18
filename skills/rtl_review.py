from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, Field

from context.manager import ContextManager
from core.llm_client import BaseLLMClient, LLMRequest
from skills.base import BaseSkill


class RtlIssue(BaseModel):
    severity: str = Field(..., description="high | medium | low")
    category: str = Field(default="", description="reset | sensitivity | width | todo | style | logic | other")
    location: str = Field(default="", description="Module name or code hint where the issue lives")
    description: str = Field(..., description="What is wrong")
    suggestion: str = Field(default="", description="How to fix it")


class RtlReviewResult(BaseModel):
    overall_quality: str = Field(default="fair", description="good | fair | poor")
    summary: str = Field(default="")
    issues: list[RtlIssue] = Field(default_factory=list)
    recommendations: list[str] = Field(default_factory=list)
    markdown_report: str = Field(default="")


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


class RtlReviewSkill(BaseSkill):
    """LLM-powered semantic review of generated Verilog RTL."""

    def __init__(self, context_manager: ContextManager, llm_client: BaseLLMClient) -> None:
        super().__init__(
            name="rtl_review",
            description=(
                "Perform a semantic review of generated Verilog RTL using the LLM. Identifies issues such as "
                "missing reset branches, suspicious port widths, unreviewed TODO placeholders, possible multiple "
                "drivers, combinational loops, and style violations. Produces structured issues with severities "
                "(high/medium/low) and fix suggestions. Uses the most recent verilog_template output when "
                "verilog_code is not provided. Use after verilog_template when the user asks for review, audit, "
                "validation, or quality check."
            ),
            parameters_schema={
                "verilog_code": {
                    "type": "string",
                    "required": False,
                    "description": "Optional raw Verilog to review. If omitted, the most recent verilog_template result is used.",
                },
            },
        )
        self.context_manager = context_manager
        self.llm_client = llm_client

    async def execute(self, **kwargs: Any) -> RtlReviewResult:
        code = kwargs.get("verilog_code") or self._code_from_context()
        if not code:
            raise ValueError("rtl_review requires verilog_code or a prior verilog_template result")

        raw = await self.llm_client.generate(
            LLMRequest(
                system_prompt=(
                    "You are an expert Verilog/RTL reviewer. Audit the given Verilog for real issues visible in the code. "
                    "Do not invent issues that are not present. Return valid JSON only, no markdown fences. "
                    'Schema: {"overall_quality": "good"|"fair"|"poor", "summary": string, '
                    '"issues": [{"severity": "high"|"medium"|"low", "category": string, '
                    '"location": string, "description": string, "suggestion": string}], '
                    '"recommendations": [string]}. '
                    "Check for: missing reset branch in posedge-clocked always blocks, missing default in case, "
                    "unreviewed TODO placeholders, inconsistent port widths, possible multiple drivers, "
                    "combinational loops, empty always blocks, missing endmodule, sensitivity list issues, "
                    "declared but unused signals, and any other correctness or style problems. "
                    "Keep descriptions concise. Sort issues by severity (high first)."
                ),
                messages=[
                    {
                        "role": "user",
                        "content": f"Review this Verilog code:\n\n{code}",
                    }
                ],
                temperature=0.1,
                max_tokens=1400,
            )
        )

        data = json.loads(_strip_json_fences(raw))
        result = RtlReviewResult(**data)
        result.markdown_report = self._to_markdown(result)

        self.context_manager.set_state("last_skill", self.name)
        self.context_manager.set_state("last_rtl_review", result.model_dump())
        return result

    def _code_from_context(self) -> str:
        last = self.context_manager.get_state("last_verilog_template")
        if not last:
            return ""
        pieces: list[str] = []
        top_code = last.get("verilog_code", "")
        if top_code:
            pieces.append(top_code)
        seen = {top_code} if top_code else set()
        for module in last.get("modules", []) or []:
            module_code = module.get("verilog_code", "")
            if module_code and module_code not in seen:
                pieces.append(f"// ---- {module.get('file_name', 'submodule')} ----\n{module_code}")
                seen.add(module_code)
        return "\n\n".join(pieces)

    def _to_markdown(self, result: RtlReviewResult) -> str:
        lines = ["## RTL Review", f"- overall_quality: **{result.overall_quality}**"]
        if result.summary:
            lines.append(f"- summary: {result.summary}")
        lines.append("")
        lines.append("### Issues")
        if result.issues:
            for issue in result.issues:
                location = f" @ {issue.location}" if issue.location else ""
                lines.append(f"- [**{issue.severity}**] {issue.category}{location}: {issue.description}")
                if issue.suggestion:
                    lines.append(f"  - suggestion: {issue.suggestion}")
        else:
            lines.append("- None.")
        lines.append("")
        lines.append("### Recommendations")
        if result.recommendations:
            for recommendation in result.recommendations:
                lines.append(f"- {recommendation}")
        else:
            lines.append("- None.")
        return "\n".join(lines).strip()
