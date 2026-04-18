from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, Field

from context.manager import ContextManager
from skills.base import BaseSkill


class LintFinding(BaseModel):
    severity: str = Field(..., description="error | warning | info")
    rule: str = Field(..., description="Short rule identifier")
    message: str = Field(..., description="Human-readable description")


class RtlLintResult(BaseModel):
    findings: list[LintFinding] = Field(default_factory=list)
    todo_count: int = Field(default=0)
    module_count: int = Field(default=0)
    endmodule_count: int = Field(default=0)
    always_posedge_count: int = Field(default=0)
    reset_missing_count: int = Field(default=0)
    empty_always_count: int = Field(default=0)


class RtlLintSkill(BaseSkill):
    """Deterministic local lint for generated Verilog. No LLM call."""

    def __init__(self, context_manager: ContextManager) -> None:
        super().__init__(
            name="rtl_lint",
            description=(
                "Run deterministic local lint on generated Verilog RTL. Fast, no LLM call. "
                "Counts TODO placeholders, checks posedge-clocked always blocks for reset coverage, "
                "detects empty always blocks, and verifies module/endmodule balance. "
                "Pulls from the most recent verilog_template output when verilog_code is not provided. "
                "Use for quick structural sanity checks after RTL generation."
            ),
            parameters_schema={
                "verilog_code": {
                    "type": "string",
                    "required": False,
                    "description": "Optional raw Verilog to lint. If omitted, uses the most recent verilog_template result.",
                },
            },
        )
        self.context_manager = context_manager

    async def execute(self, **kwargs: Any) -> RtlLintResult:
        code = kwargs.get("verilog_code") or self._code_from_context()
        if not code:
            raise ValueError("rtl_lint requires verilog_code or a prior verilog_template result")

        findings: list[LintFinding] = []

        todo_count = len(re.findall(r"\bTODO\b", code))
        module_count = len(re.findall(r"^\s*module\s+\w+", code, flags=re.MULTILINE))
        endmodule_count = len(re.findall(r"^\s*endmodule\b", code, flags=re.MULTILINE))
        if module_count != endmodule_count:
            findings.append(LintFinding(
                severity="error",
                rule="module_endmodule_mismatch",
                message=f"module count={module_count} but endmodule count={endmodule_count}",
            ))

        always_header = re.compile(r"\balways\s*@\s*\(([^)]*)\)")
        matches = list(always_header.finditer(code))
        always_posedge_count = 0
        reset_missing_count = 0
        for index, match in enumerate(matches):
            sensitivity = match.group(1)
            block_start = match.end()
            block_end = matches[index + 1].start() if index + 1 < len(matches) else len(code)
            block_body = code[block_start:block_end]
            if "posedge" in sensitivity:
                always_posedge_count += 1
                if not re.search(r"\b(?:rst|reset)(?:_n)?\b", sensitivity + block_body):
                    reset_missing_count += 1
                    findings.append(LintFinding(
                        severity="warning",
                        rule="reset_not_found_in_sequential_block",
                        message=f"posedge always block without reset reference: @({sensitivity.strip()})",
                    ))

        empty_always_count = len(re.findall(r"always\s*@\s*\([^)]+\)\s*begin\s*end", code))
        if empty_always_count:
            findings.append(LintFinding(
                severity="warning",
                rule="empty_always",
                message=f"{empty_always_count} empty always block(s) detected",
            ))

        if todo_count:
            findings.append(LintFinding(
                severity="info",
                rule="todo_placeholder",
                message=f"{todo_count} TODO placeholder(s) remain in generated RTL",
            ))

        result = RtlLintResult(
            findings=findings,
            todo_count=todo_count,
            module_count=module_count,
            endmodule_count=endmodule_count,
            always_posedge_count=always_posedge_count,
            reset_missing_count=reset_missing_count,
            empty_always_count=empty_always_count,
        )
        self.context_manager.set_state("last_skill", self.name)
        self.context_manager.set_state("last_rtl_lint", result.model_dump())
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
                pieces.append(module_code)
                seen.add(module_code)
        return "\n\n".join(pieces)
