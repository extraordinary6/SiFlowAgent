from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, Field

from context.manager import ContextManager
from core.llm_client import BaseLLMClient, LLMRequest
from skills.base import BaseSkill


class PlannerDecision(BaseModel):
    thought: str = Field(default="", description="Short reasoning for the next step")
    action: str = Field(..., description="Skill name to invoke, or the literal 'finish'")
    args: dict[str, Any] = Field(default_factory=dict, description="Arguments for the chosen skill")
    final_answer: str = Field(
        default="",
        description="User-facing response, only non-empty when action == 'finish'",
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


class PlannerSkill(BaseSkill):
    """Produce the next step of a ReAct-style agent loop.

    Unlike RouterSkill, PlannerSkill gets a scratchpad of all previous
    (thought, action, args, observation) tuples and decides whether to call
    another skill or finish with a final answer for the user.
    """

    def __init__(self, context_manager: ContextManager, llm_client: BaseLLMClient) -> None:
        super().__init__(
            name="planner",
            description="Internal skill. Multi-step reasoning brain for the agent loop.",
            parameters_schema={
                "goal": {
                    "type": "string",
                    "required": True,
                    "description": "The user's overall goal for this agent run.",
                },
                "skill_catalog": {
                    "type": "array",
                    "required": True,
                    "description": "List of downstream skills the planner may invoke.",
                },
                "scratchpad": {
                    "type": "array",
                    "required": True,
                    "description": "List of previous steps: thought, action, args, observation, ok.",
                },
            },
        )
        self.context_manager = context_manager
        self.llm_client = llm_client

    async def execute(self, **kwargs: Any) -> PlannerDecision:
        goal = str(kwargs.get("goal", "")).strip()
        skill_catalog = kwargs.get("skill_catalog") or []
        scratchpad = kwargs.get("scratchpad") or []
        if not goal:
            raise ValueError("goal is required")
        if not skill_catalog:
            raise ValueError("skill_catalog is required")

        catalog_text = json.dumps(skill_catalog, ensure_ascii=False, indent=2)
        scratchpad_text = json.dumps(scratchpad, ensure_ascii=False, indent=2)
        runtime_state = {
            "last_skill": self.context_manager.get_state("last_skill"),
            "has_spec_summary": self.context_manager.get_state("last_spec_summary") is not None,
            "has_verilog_template": self.context_manager.get_state("last_verilog_template") is not None,
            "has_rtl_review": self.context_manager.get_state("last_rtl_review") is not None,
            "has_rtl_lint": self.context_manager.get_state("last_rtl_lint") is not None,
            "rtl_revise_count": self.context_manager.get_state("rtl_revise_count") or 0,
        }
        state_text = json.dumps(runtime_state, ensure_ascii=False)

        raw = await self.llm_client.generate(
            LLMRequest(
                system_prompt=(
                    "You are the planning brain of SiFlowAgent, running a ReAct loop.\n"
                    "Each turn you see: the user's goal, the skill catalog, the runtime state, "
                    "and a scratchpad of previous (thought, action, args, observation, ok) tuples.\n"
                    "Decide ONLY the next single step. Return valid JSON, no markdown fences.\n"
                    'Schema: {"thought": string, "action": string, "args": object, "final_answer": string}.\n'
                    "Rules:\n"
                    "- 'action' MUST be exactly one of the catalog skill names OR the literal string \"finish\".\n"
                    "- When action=='finish', put the user-facing reply in 'final_answer' and leave 'args' empty.\n"
                    "- When action is a skill name, fill 'args' according to that skill's parameters_schema, and leave 'final_answer' empty.\n"
                    "- Do NOT repeat an action+args combination that already failed (ok==false) in the scratchpad.\n"
                    "- Prefer 'finish' as soon as the goal is satisfied. Do not call extra skills once you can answer.\n"
                    "- For goals that only need conversation (greetings, meta questions), go straight to 'finish' with the reply as final_answer.\n"
                    "- verilog_template requires runtime_state.has_spec_summary == true; if the goal needs RTL and no spec summary exists yet, first call spec_summary.\n"
                    "- After rtl_review or rtl_lint reports issues of severity 'high' or 'medium', you may call rtl_revise to apply fixes, then call rtl_review again on the revised RTL before finishing.\n"
                    "- Do not call rtl_revise more than 2 times in a single run. If issues still remain after 2 revisions, finish and summarize what was fixed and what remains.\n"
                    "- Do not call verilog_template a second time after rtl_revise has run; that would discard the revisions.\n"
                    "- Keep 'thought' to one or two short sentences.\n"
                ),
                messages=[
                    {
                        "role": "user",
                        "content": (
                            f"User goal:\n{goal}\n\n"
                            f"Skill catalog:\n{catalog_text}\n\n"
                            f"Runtime state:\n{state_text}\n\n"
                            f"Scratchpad (previous steps, oldest first):\n{scratchpad_text}"
                        ),
                    }
                ],
                temperature=0.0,
                max_tokens=600,
                disable_thinking=True,
            )
        )

        cleaned = _strip_json_fences(raw)
        data = json.loads(cleaned)
        decision = PlannerDecision(**data)
        self.context_manager.set_state("last_planner_decision", decision.model_dump())
        return decision
