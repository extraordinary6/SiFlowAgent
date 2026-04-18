from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from core.orchestrator import Orchestrator


@dataclass
class AgentStep:
    index: int
    thought: str
    action: str
    args: dict[str, Any] = field(default_factory=dict)
    observation: str = ""
    ok: bool = True


class AgentLoopResult(BaseModel):
    goal: str
    steps: list[dict[str, Any]] = Field(default_factory=list)
    final_answer: str = Field(default="")
    completed: bool = Field(default=False)
    stopped_reason: str = Field(default="")


StepCallback = Callable[[AgentStep], Awaitable[None] | None]


class AgentLoop:
    """ReAct-style think/act/observe loop built on top of PlannerSkill."""

    def __init__(
        self,
        orchestrator: "Orchestrator",
        max_steps: int = 6,
        on_step: StepCallback | None = None,
    ) -> None:
        self.orchestrator = orchestrator
        self.max_steps = max_steps
        self.on_step = on_step

    async def run(self, goal: str) -> AgentLoopResult:
        steps: list[AgentStep] = []
        final_answer = ""
        stopped_reason = "max_steps"
        completed = False

        for index in range(self.max_steps):
            decision = await self.orchestrator.plan_next_step(goal, steps)

            if decision.action == "finish":
                final_answer = decision.final_answer
                completed = True
                stopped_reason = "finish"
                # Emit a synthetic final step so the trace shows the finishing thought.
                finish_step = AgentStep(
                    index=index + 1,
                    thought=decision.thought,
                    action="finish",
                    args={},
                    observation=final_answer,
                    ok=True,
                )
                await self._emit(finish_step)
                break

            try:
                result = await self.orchestrator.execute_action(
                    decision.action,
                    dict(decision.args),
                    goal,
                )
                observation = self.orchestrator.make_observation(decision.action, result)
                ok = True
            except Exception as error:  # noqa: BLE001 - capture to feed back to planner
                observation = f"ERROR while running {decision.action}: {error}"
                ok = False

            step = AgentStep(
                index=index + 1,
                thought=decision.thought,
                action=decision.action,
                args=dict(decision.args),
                observation=observation,
                ok=ok,
            )
            steps.append(step)
            await self._emit(step)

        return AgentLoopResult(
            goal=goal,
            steps=[asdict(step) for step in steps],
            final_answer=final_answer,
            completed=completed,
            stopped_reason=stopped_reason,
        )

    async def _emit(self, step: AgentStep) -> None:
        if self.on_step is None:
            return
        outcome = self.on_step(step)
        if hasattr(outcome, "__await__"):
            await outcome  # type: ignore[func-returns-value]
