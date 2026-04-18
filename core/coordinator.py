from __future__ import annotations

from typing import TYPE_CHECKING, Awaitable, Callable

from loguru import logger
from pydantic import BaseModel, Field

from core.agents import ActorAgent, AgentMessage, CriticAgent

if TYPE_CHECKING:
    from core.orchestrator import Orchestrator


MessageCallback = Callable[[AgentMessage], Awaitable[None] | None]


class CoordinatorResult(BaseModel):
    goal: str
    transcript: list[AgentMessage] = Field(default_factory=list)
    accepted: bool = Field(default=False)
    rounds_used: int = Field(default=0)
    stopped_reason: str = Field(default="")


class Coordinator:
    """Non-LLM orchestrator that drives an actor-critic multi-agent conversation.

    The coordinator itself does no LLM reasoning; it simply alternates between
    the actor and the critic, captures their structured messages into a shared
    transcript, and stops when the critic accepts or when the round cap is hit.
    """

    def __init__(
        self,
        orchestrator: "Orchestrator",
        actor: ActorAgent,
        critic: CriticAgent,
        max_rounds: int = 3,
        on_message: MessageCallback | None = None,
    ) -> None:
        self.orchestrator = orchestrator
        self.actor = actor
        self.critic = critic
        self.max_rounds = max_rounds
        self.on_message = on_message

    async def run(self, goal: str) -> CoordinatorResult:
        transcript: list[AgentMessage] = []

        seed = AgentMessage(
            round=0,
            sender="user",
            recipient=self.actor.name,
            kind="request",
            summary="user goal",
            content=goal,
            payload={},
        )
        transcript.append(seed)
        await self._emit(seed)

        accepted = False
        rounds_used = 0
        stopped_reason = "max_rounds"

        for round_idx in range(1, self.max_rounds + 1):
            rounds_used = round_idx

            actor_msg = await self.actor.respond(transcript, goal, round_idx)
            transcript.append(actor_msg)
            await self._emit(actor_msg)

            critic_msg = await self.critic.respond(transcript, goal, round_idx)
            transcript.append(critic_msg)
            await self._emit(critic_msg)

            verdict = (critic_msg.payload or {}).get("verdict")
            logger.info("Coordinator round {} verdict={}", round_idx, verdict)

            if verdict == "accept":
                accepted = True
                stopped_reason = "accept"
                break

        final = AgentMessage(
            round=rounds_used,
            sender="coordinator",
            recipient="user",
            kind="final",
            summary=f"accepted={accepted}, rounds={rounds_used}",
            content=(
                "Actor-critic loop accepted the RTL."
                if accepted
                else f"Stopped after {rounds_used} round(s) without acceptance."
            ),
            payload={"accepted": accepted, "rounds_used": rounds_used, "stopped_reason": stopped_reason},
        )
        transcript.append(final)
        await self._emit(final)

        return CoordinatorResult(
            goal=goal,
            transcript=transcript,
            accepted=accepted,
            rounds_used=rounds_used,
            stopped_reason=stopped_reason,
        )

    async def _emit(self, message: AgentMessage) -> None:
        if self.on_message is None:
            return
        outcome = self.on_message(message)
        if hasattr(outcome, "__await__"):
            await outcome  # type: ignore[func-returns-value]
