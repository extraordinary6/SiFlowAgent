from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field

from context.manager import ContextManager
from core.llm_client import BaseLLMClient, LLMRequest
from skills.base import BaseSkill


class RevisedModule(BaseModel):
    file_name: str = Field(..., description="Output Verilog file name")
    module_name: str = Field(..., description="Verilog module name inside the file")
    verilog_code: str = Field(..., description="Full revised Verilog source for this module")


class RtlReviseResult(BaseModel):
    modules: list[RevisedModule] = Field(default_factory=list)
    changes_summary: str = Field(default="")
    addressed_issues: list[str] = Field(default_factory=list)
    unresolved_issues: list[str] = Field(default_factory=list)


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


class RtlReviseSkill(BaseSkill):
    """LLM-powered revision of Verilog RTL using prior review / lint findings.

    After a successful revision, ``last_verilog_template`` in context is replaced
    with the revised modules so that subsequent rtl_review / rtl_lint / /rtl saves
    operate on the new code.
    """

    def __init__(self, context_manager: ContextManager, llm_client: BaseLLMClient) -> None:
        super().__init__(
            name="rtl_revise",
            description=(
                "Apply review and lint feedback to revise previously generated Verilog RTL. "
                "Reads rtl_review issues and rtl_lint findings from context, and the current baseline RTL "
                "from the most recent verilog_template or rtl_revise output. Produces revised code that "
                "preserves module names, ports, and architecture while fixing issues. Replaces the stored "
                "RTL in context so subsequent review / lint / save operate on the new code. Use when "
                "review or lint reports issues of severity medium or higher."
            ),
            parameters_schema={
                "verilog_code": {
                    "type": "string",
                    "required": False,
                    "description": "Optional override for the baseline RTL. If omitted, the most recent verilog_template/rtl_revise output from context is used.",
                },
                "output_path": {
                    "type": "string",
                    "required": False,
                    "description": "Optional filesystem path. If it ends with .v, the top revised module is saved; otherwise treated as a directory and all revised modules are written into it.",
                },
            },
        )
        self.context_manager = context_manager
        self.llm_client = llm_client

    async def execute(self, **kwargs: Any) -> RtlReviseResult:
        explicit_code = kwargs.get("verilog_code")
        baseline_modules = self._current_modules(explicit_code)
        if not baseline_modules:
            raise ValueError(
                "rtl_revise requires verilog_code or a prior verilog_template/rtl_revise result in context"
            )

        review = self.context_manager.get_state("last_rtl_review") or {}
        lint = self.context_manager.get_state("last_rtl_lint") or {}
        if not (review.get("issues") or lint.get("findings")):
            raise ValueError(
                "rtl_revise requires a prior rtl_review or rtl_lint with findings to act on"
            )

        modules_text = self._format_modules_for_prompt(baseline_modules)
        issues_text = self._format_issues_for_prompt(review, lint)

        raw = await self.llm_client.generate(
            LLMRequest(
                system_prompt=(
                    "You are an expert Verilog/RTL engineer. Apply the given review feedback to revise the provided Verilog code.\n"
                    "Preserve module names, file names, port lists and widths, and overall architecture unless an issue explicitly demands otherwise.\n"
                    "Only change what is needed to fix the reported issues. Keep style consistent (one output per always block when possible).\n"
                    "Every module appearing in the input MUST appear in the output, even if unchanged. Emit synthesizable Verilog.\n"
                    "Return valid JSON only (no markdown fences) matching this schema:\n"
                    '{"modules": [{"file_name": string, "module_name": string, "verilog_code": string}], '
                    '"changes_summary": string, "addressed_issues": [string], "unresolved_issues": [string]}'
                ),
                messages=[
                    {
                        "role": "user",
                        "content": (
                            f"Current modules:\n{modules_text}\n\n"
                            f"Issues to address:\n{issues_text}\n\n"
                            "Produce revised modules as JSON."
                        ),
                    }
                ],
                temperature=0.1,
                max_tokens=3000,
                disable_thinking=True,
            )
        )

        data = json.loads(_strip_json_fences(raw))
        result = RtlReviseResult(**data)

        modules_list = [
            {
                "module_name": module.module_name,
                "file_name": module.file_name,
                "verilog_code": module.verilog_code,
            }
            for module in result.modules
        ]
        top = modules_list[0] if modules_list else None

        # Snapshot the baseline RTL into revise_history BEFORE overwriting
        # last_verilog_template, together with the review/lint that triggered
        # this revision. This gives the agent a real audit trail and enables
        # rollback to any earlier iteration.
        baseline_state = self.context_manager.get_state("last_verilog_template") or {}
        history = list(self.context_manager.get_state("revise_history") or [])
        history.append(
            {
                "iteration": len(history),
                "created_at": datetime.now(timezone.utc).isoformat(),
                "verilog_code": baseline_state.get("verilog_code", ""),
                "modules": baseline_state.get("modules") or [],
                "source_review": review,
                "source_lint": lint,
            }
        )
        self.context_manager.set_state("revise_history", history)

        self.context_manager.set_state(
            "last_verilog_template",
            {
                "module_name": top["module_name"] if top else "",
                "port_declarations": [],
                "body_lines": [],
                "verilog_code": top["verilog_code"] if top else "",
                "modules": modules_list,
            },
        )
        self.context_manager.set_state("last_skill", self.name)
        self.context_manager.set_state("last_rtl_revise", result.model_dump())
        return result

    def _current_modules(self, explicit_code: str | None) -> list[dict[str, Any]]:
        if explicit_code:
            return [
                {
                    "module_name": "revised_top",
                    "file_name": "revised_top.v",
                    "verilog_code": explicit_code,
                }
            ]
        last = self.context_manager.get_state("last_verilog_template")
        if not last:
            return []
        modules = last.get("modules") or []
        if modules:
            return modules
        top_code = last.get("verilog_code")
        if top_code:
            return [
                {
                    "module_name": last.get("module_name", "top"),
                    "file_name": "top.v",
                    "verilog_code": top_code,
                }
            ]
        return []

    def _format_modules_for_prompt(self, modules: list[dict[str, Any]]) -> str:
        parts: list[str] = []
        seen: set[str] = set()
        for module in modules:
            code = module.get("verilog_code", "")
            if not code or code in seen:
                continue
            seen.add(code)
            parts.append(f"// ---- {module.get('file_name', 'module.v')} ----\n{code}")
        return "\n\n".join(parts)

    def _format_issues_for_prompt(self, review: dict[str, Any], lint: dict[str, Any]) -> str:
        lines: list[str] = []
        issues = review.get("issues") or []
        if issues:
            severity_rank = {"high": 0, "medium": 1, "low": 2}
            sorted_issues = sorted(issues, key=lambda entry: severity_rank.get(entry.get("severity"), 3))
            lines.append("Review issues (sorted by severity):")
            for issue in sorted_issues:
                sev = issue.get("severity", "?")
                cat = issue.get("category", "")
                loc = issue.get("location", "")
                desc = issue.get("description", "")
                sug = issue.get("suggestion", "")
                loc_part = f" @ {loc}" if loc else ""
                sug_part = f" | suggestion: {sug}" if sug else ""
                lines.append(f"- [{sev}] {cat}{loc_part}: {desc}{sug_part}")
        findings = lint.get("findings") or []
        if findings:
            lines.append("Lint findings:")
            for finding in findings:
                sev = finding.get("severity", "?")
                rule = finding.get("rule", "")
                msg = finding.get("message", "")
                lines.append(f"- [{sev}] {rule}: {msg}")
        if not lines:
            lines.append("(no specific issues recorded; general quality improvement)")
        return "\n".join(lines)
