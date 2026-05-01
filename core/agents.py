from __future__ import annotations

import json
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

from loguru import logger
from pydantic import BaseModel, Field

from core.llm_client import BaseLLMClient, LLMRequest

if TYPE_CHECKING:
    from core.orchestrator import Orchestrator


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


class AgentConfig(BaseModel):
    name: str
    persona: str = Field(default="", description="Short role label for the transcript")
    system_prompt: str
    temperature: float = Field(default=0.2)
    max_tokens: int = Field(default=1000)
    allowed_skills: list[str] = Field(default_factory=list)
    max_substeps: int = Field(
        default=4,
        description="How many skill calls the agent may chain inside a single turn before handing off",
    )


class AgentMessage(BaseModel):
    round: int = Field(default=0)
    sender: str
    recipient: str
    kind: str = Field(..., description="request | action | proposal | verdict | final")
    summary: str = Field(default="", description="One-line label for the transcript")
    content: str = Field(default="", description="Natural-language message body")
    payload: dict[str, Any] = Field(default_factory=dict, description="Structured extras")


class BaseAgent(ABC):
    def __init__(
        self,
        config: AgentConfig,
        llm_client: BaseLLMClient,
        orchestrator: "Orchestrator",
    ) -> None:
        self.config = config
        self.llm_client = llm_client
        self.orchestrator = orchestrator

    @property
    def name(self) -> str:
        return self.config.name

    def allowed_catalog(self) -> list[dict[str, Any]]:
        """Skills this agent is allowed to see and invoke."""
        return [
            entry
            for entry in self.orchestrator.skill_registry.list_skills()
            if entry["name"] in set(self.config.allowed_skills)
        ]

    @abstractmethod
    async def respond(self, transcript: list[AgentMessage], goal: str, round_idx: int) -> AgentMessage:
        raise NotImplementedError


class ActorAgent(BaseAgent):
    """Creative sub-agent that understands the spec, generates RTL, and applies revisions.

    Within a single turn it may run a small chain of skill calls (bounded by
    `config.max_substeps`) before handing off to the critic.
    """

    async def respond(
        self,
        transcript: list[AgentMessage],
        goal: str,
        round_idx: int,
    ) -> AgentMessage:
        sub_steps: list[dict[str, Any]] = []
        last_rationale = ""
        handoff_message = ""

        for _ in range(self.config.max_substeps):
            decision = await self._decide_next(transcript, goal, sub_steps)
            last_rationale = decision.get("rationale", "")
            action = decision.get("action", "handoff")

            if action == "handoff":
                handoff_message = decision.get("handoff_message") or last_rationale
                break

            skill_name = decision.get("skill")
            args = decision.get("args") or {}
            if not skill_name:
                handoff_message = "Actor returned no skill to execute; handing off."
                break

            try:
                result = await self.orchestrator.execute_action(skill_name, dict(args), goal)
                observation = self.orchestrator.make_observation(skill_name, result)
                ok = True
            except Exception as error:  # noqa: BLE001 - feed errors back as observations
                observation = f"ERROR while running {skill_name}: {error}"
                ok = False

            sub_steps.append(
                {
                    "skill": skill_name,
                    "args": dict(args),
                    "observation": observation,
                    "ok": ok,
                }
            )

        return AgentMessage(
            round=round_idx,
            sender=self.config.name,
            recipient="critic",
            kind="proposal",
            summary=f"{len(sub_steps)} step(s); handoff={bool(handoff_message)}",
            content=handoff_message or "Actor turn complete.",
            payload={
                "sub_steps": sub_steps,
                "final_rationale": last_rationale,
            },
        )

    async def _decide_next(
        self,
        transcript: list[AgentMessage],
        goal: str,
        sub_steps: list[dict[str, Any]],
    ) -> dict[str, Any]:
        catalog = self.allowed_catalog()
        runtime_state = self._runtime_state()
        last_critic = self._last_from(transcript, "critic")

        user_content = (
            f"User goal:\n{goal}\n\n"
            f"Allowed skills (call at most one per step):\n"
            f"{json.dumps(catalog, ensure_ascii=False, indent=2)}\n\n"
            f"Runtime state:\n{json.dumps(runtime_state, ensure_ascii=False)}\n\n"
            f"Latest critic feedback (may be empty on first round):\n"
            f"{self._format_message(last_critic)}\n\n"
            f"Your sub-steps this turn so far:\n{json.dumps(sub_steps, ensure_ascii=False, indent=2)}"
        )

        raw = await self.llm_client.generate(
            LLMRequest(
                system_prompt=self.config.system_prompt
                + "\n\nReturn valid JSON only. Schema:\n"
                '{"action": "call_skill" | "handoff", '
                '"skill": string | null, "args": object, '
                '"rationale": string, "handoff_message": string}\n'
                "Set action=handoff when your current produced RTL (or your investigation) is ready for the critic, "
                "when you have nothing more to do, or when you would otherwise repeat a failed action. "
                "Never invent skill names outside the provided catalog.",
                messages=[{"role": "user", "content": user_content}],
                temperature=self.config.temperature,
                max_tokens=self.config.max_tokens,
            )
        )
        try:
            return json.loads(_strip_json_fences(raw))
        except json.JSONDecodeError as error:
            logger.warning("Actor returned non-JSON, handing off: {}", error)
            return {"action": "handoff", "rationale": f"non-JSON response: {raw[:200]}"}

    def _runtime_state(self) -> dict[str, Any]:
        cm = self.orchestrator.context_manager
        return {
            "has_spec_summary": cm.get_state("last_spec_summary") is not None,
            "has_verilog_template": cm.get_state("last_verilog_template") is not None,
            "has_rtl_review": cm.get_state("last_rtl_review") is not None,
            "rtl_revise_count": cm.get_state("rtl_revise_count") or 0,
        }

    @staticmethod
    def _last_from(transcript: list[AgentMessage], sender: str) -> AgentMessage | None:
        for message in reversed(transcript):
            if message.sender == sender:
                return message
        return None

    @staticmethod
    def _format_message(message: AgentMessage | None) -> str:
        if message is None:
            return "(none)"
        return json.dumps(message.model_dump(), ensure_ascii=False, indent=2)


class CriticAgent(BaseAgent):
    """Strict reviewer sub-agent.

    Always runs rtl_lint first (deterministic), then rtl_review (LLM), then
    produces a structured verdict via one more LLM call.
    """

    async def respond(
        self,
        transcript: list[AgentMessage],
        goal: str,
        round_idx: int,
    ) -> AgentMessage:
        lint_result: dict[str, Any] | None = None
        review_result: dict[str, Any] | None = None
        error_notes: list[str] = []

        try:
            lint_result = await self.orchestrator.execute_action("rtl_lint", {}, goal)
        except Exception as error:  # noqa: BLE001
            error_notes.append(f"rtl_lint failed: {error}")

        try:
            review_result = await self.orchestrator.execute_action("rtl_review", {}, goal)
        except Exception as error:  # noqa: BLE001
            error_notes.append(f"rtl_review failed: {error}")

        verdict = await self._decide_verdict(goal, lint_result, review_result, error_notes)

        next_recipient = "actor" if verdict.get("verdict") == "revise" else "user"
        return AgentMessage(
            round=round_idx,
            sender=self.config.name,
            recipient=next_recipient,
            kind="verdict",
            summary=(
                f"verdict={verdict.get('verdict')}, "
                f"review_quality={(review_result or {}).get('overall_quality', '?')}"
            ),
            content=verdict.get("rationale", ""),
            payload={
                "verdict": verdict.get("verdict", "revise"),
                "priority_issues": verdict.get("priority_issues", []),
                "recommendation": verdict.get("recommendation", ""),
                "lint_summary": self._lint_summary(lint_result),
                "review_summary": self._review_summary(review_result),
                "errors": error_notes,
            },
        )

    async def _decide_verdict(
        self,
        goal: str,
        lint_result: dict[str, Any] | None,
        review_result: dict[str, Any] | None,
        error_notes: list[str],
    ) -> dict[str, Any]:
        user_content = (
            f"User goal:\n{goal}\n\n"
            f"rtl_lint result:\n{json.dumps(lint_result, ensure_ascii=False, indent=2)}\n\n"
            f"rtl_review result:\n{json.dumps(review_result, ensure_ascii=False, indent=2)}\n\n"
            f"Tool errors (if any):\n{json.dumps(error_notes, ensure_ascii=False)}"
        )

        raw = await self.llm_client.generate(
            LLMRequest(
                system_prompt=self.config.system_prompt
                + "\n\nReturn valid JSON only. Schema:\n"
                '{"verdict": "accept" | "revise", '
                '"priority_issues": [string], '
                '"rationale": string, "recommendation": string}\n'
                "Choose 'accept' only if the RTL is reasonably clean, has no high-severity issues, "
                "and every medium issue is either minor or already documented. "
                "Otherwise choose 'revise' and list the priority issues the actor must fix.",
                messages=[{"role": "user", "content": user_content}],
                temperature=self.config.temperature,
                max_tokens=self.config.max_tokens,
            )
        )
        try:
            return json.loads(_strip_json_fences(raw))
        except json.JSONDecodeError as error:
            logger.warning("Critic returned non-JSON, defaulting to revise: {}", error)
            return {
                "verdict": "revise",
                "priority_issues": [],
                "rationale": f"non-JSON critic response: {raw[:200]}",
                "recommendation": "",
            }

    @staticmethod
    def _lint_summary(result: dict[str, Any] | None) -> dict[str, Any]:
        if not result:
            return {}
        return {
            "findings_count": result.get("findings_count"),
            "todo_count": result.get("todo_count"),
            "reset_missing_count": result.get("reset_missing_count"),
            "empty_always_count": result.get("empty_always_count"),
        }

    @staticmethod
    def _review_summary(result: dict[str, Any] | None) -> dict[str, Any]:
        if not result:
            return {}
        return {
            "overall_quality": result.get("overall_quality"),
            "issues_count": result.get("issues_count"),
            "severity_counts": result.get("severity_counts"),
        }


class VerifierAgent(BaseAgent):
    """Verification sub-agent that runs a real simulator on the latest RTL.

    The VerifierAgent dispatches one simulator skill per turn and turns the
    pass/fail into a structured verdict. Two backends are supported:

    - ``rtl_sim``  — Verilog testbench via iverilog / verilator
    - ``cocotb_sim`` — Python coroutine testbench via cocotb runner

    The backend is picked from the constructor arguments: a non-empty
    ``cocotb_test_module`` selects cocotb; otherwise ``testbench_path`` selects
    rtl_sim. The verdict vocabulary is shared between both backends so callers
    can treat them interchangeably.

    Verdict vocabulary:
    - ``verified``   : simulator passed (pass marker / non-zero PASS / all cases pass)
    - ``sim_failed`` : simulator ran but the design does not match the TB
    - ``compile_error`` : RTL does not compile against the TB
    - ``no_tool``    : neither iverilog nor verilator available on PATH
    - ``skipped``    : no testbench available
    - ``error``      : unexpected exception from the simulator skill
    """

    def __init__(
        self,
        config: AgentConfig,
        llm_client: BaseLLMClient,
        orchestrator: "Orchestrator",
        testbench_path: str | None = None,
        top_module: str | None = None,
        timeout_seconds: float | None = None,
        pass_tokens: list[str] | None = None,
        fail_tokens: list[str] | None = None,
        extra_sources: list[str] | None = None,
        cocotb_test_module: str | None = None,
        cocotb_hdl_toplevel: str | None = None,
        cocotb_test_dir: str | None = None,
        cocotb_verilog_sources: list[str] | None = None,
        cocotb_testcase: str | None = None,
        tool: str | None = None,
    ) -> None:
        super().__init__(config=config, llm_client=llm_client, orchestrator=orchestrator)
        self.testbench_path = testbench_path
        self.top_module = top_module
        self.timeout_seconds = timeout_seconds
        self.pass_tokens = list(pass_tokens) if pass_tokens else None
        self.fail_tokens = list(fail_tokens) if fail_tokens else None
        self.extra_sources = list(extra_sources) if extra_sources else None
        self.cocotb_test_module = cocotb_test_module
        self.cocotb_hdl_toplevel = cocotb_hdl_toplevel
        self.cocotb_test_dir = cocotb_test_dir
        self.cocotb_verilog_sources = list(cocotb_verilog_sources) if cocotb_verilog_sources else None
        self.cocotb_testcase = cocotb_testcase
        self.tool = tool

    async def respond(
        self,
        transcript: list[AgentMessage],
        goal: str,
        round_idx: int,
    ) -> AgentMessage:
        backend = self._select_backend()
        if backend == "cocotb":
            return await self._respond_cocotb(round_idx)
        if backend == "rtl_sim":
            return await self._respond_rtl_sim(round_idx, goal)
        return AgentMessage(
            round=round_idx,
            sender=self.config.name,
            recipient="user",
            kind="verification",
            summary="verdict=skipped (no testbench)",
            content=(
                "VerifierAgent skipped simulation: neither a Verilog testbench "
                "nor a cocotb test module was supplied."
            ),
            payload={"verdict": "skipped", "status": "no_testbench", "backend": ""},
        )

    def _select_backend(self) -> str:
        if self.cocotb_test_module and self.cocotb_hdl_toplevel:
            return "cocotb"
        if self._resolve_testbench():
            return "rtl_sim"
        return ""

    async def _respond_rtl_sim(self, round_idx: int, goal: str) -> AgentMessage:
        testbench = self._resolve_testbench()
        sim_args: dict[str, Any] = {"testbench_path": testbench}
        if self.top_module:
            sim_args["top_module"] = self.top_module
        if self.timeout_seconds is not None:
            sim_args["timeout_seconds"] = self.timeout_seconds
        if self.pass_tokens:
            sim_args["pass_tokens"] = self.pass_tokens
        if self.fail_tokens:
            sim_args["fail_tokens"] = self.fail_tokens
        if self.extra_sources:
            sim_args["extra_sources"] = self.extra_sources
        if self.tool:
            sim_args["tool"] = self.tool

        try:
            sim_result = await self.orchestrator.execute_action("rtl_sim", sim_args, goal)
        except Exception as error:  # noqa: BLE001
            return AgentMessage(
                round=round_idx,
                sender=self.config.name,
                recipient="user",
                kind="verification",
                summary="verdict=error",
                content=f"rtl_sim raised: {error}",
                payload={"verdict": "error", "status": "error", "backend": "rtl_sim", "error": str(error)},
            )

        status = sim_result.get("status", "error")
        verdict = self._status_to_verdict(status)
        summary = (
            f"verdict={verdict}, backend=rtl_sim, tool={sim_result.get('tool', '')}, "
            f"assertions={sim_result.get('assertions_passed', 0)}/"
            f"{sim_result.get('assertions_passed', 0) + sim_result.get('assertions_failed', 0)}"
        )
        return AgentMessage(
            round=round_idx,
            sender=self.config.name,
            recipient="user" if verdict == "verified" else "actor",
            kind="verification",
            summary=summary,
            content=sim_result.get("detail") or "",
            payload={
                "verdict": verdict,
                "status": status,
                "backend": "rtl_sim",
                "tool": sim_result.get("tool"),
                "top_module": sim_result.get("top_module"),
                "testbench": sim_result.get("testbench"),
                "assertions_passed": sim_result.get("assertions_passed", 0),
                "assertions_failed": sim_result.get("assertions_failed", 0),
                "compile_ok": sim_result.get("compile_ok", False),
                "run_ok": sim_result.get("run_ok", False),
                "duration_seconds": sim_result.get("duration_seconds", 0),
                "run_log_tail": sim_result.get("run_log_tail", ""),
                "compile_log_tail": sim_result.get("compile_log_tail", ""),
            },
        )

    async def _respond_cocotb(self, round_idx: int) -> AgentMessage:
        sim_args: dict[str, Any] = {
            "test_module": self.cocotb_test_module,
            "hdl_toplevel": self.cocotb_hdl_toplevel,
        }
        if self.cocotb_test_dir:
            sim_args["test_dir"] = self.cocotb_test_dir
        if self.cocotb_verilog_sources:
            sim_args["verilog_sources"] = self.cocotb_verilog_sources
        if self.extra_sources:
            sim_args["extra_sources"] = self.extra_sources
        if self.timeout_seconds is not None:
            sim_args["timeout_seconds"] = self.timeout_seconds
        if self.cocotb_testcase:
            sim_args["testcase"] = self.cocotb_testcase
        if self.tool:
            sim_args["tool"] = self.tool

        try:
            sim_result = await self.orchestrator.execute_action("cocotb_sim", sim_args, "")
        except Exception as error:  # noqa: BLE001
            return AgentMessage(
                round=round_idx,
                sender=self.config.name,
                recipient="user",
                kind="verification",
                summary="verdict=error",
                content=f"cocotb_sim raised: {error}",
                payload={"verdict": "error", "status": "error", "backend": "cocotb", "error": str(error)},
            )

        status = sim_result.get("status", "error")
        verdict = self._status_to_verdict(status)
        passes = sim_result.get("passes", 0)
        fails = sim_result.get("fails", 0)
        skipped = sim_result.get("skipped", 0)
        total = passes + fails + skipped
        summary = (
            f"verdict={verdict}, backend=cocotb, tool={sim_result.get('tool', '')}, "
            f"cases={passes}/{total} pass ({fails} fail, {skipped} skip)"
        )
        return AgentMessage(
            round=round_idx,
            sender=self.config.name,
            recipient="user" if verdict == "verified" else "actor",
            kind="verification",
            summary=summary,
            content=sim_result.get("detail") or "",
            payload={
                "verdict": verdict,
                "status": status,
                "backend": "cocotb",
                "tool": sim_result.get("tool"),
                "hdl_toplevel": sim_result.get("hdl_toplevel"),
                "test_module": sim_result.get("test_module"),
                "passes": passes,
                "fails": fails,
                "skipped": skipped,
                "test_cases": sim_result.get("test_cases", []),
                "event_log": sim_result.get("event_log", []),
                "compile_ok": sim_result.get("build_ok", False),
                "run_ok": sim_result.get("run_ok", False),
                "duration_seconds": sim_result.get("duration_seconds", 0),
                "run_log_tail": sim_result.get("run_log_tail", ""),
                "compile_log_tail": sim_result.get("build_log_tail", ""),
            },
        )

    def _resolve_testbench(self) -> str:
        if self.testbench_path:
            return self.testbench_path
        stored = self.orchestrator.context_manager.get_state("verifier_testbench")
        return str(stored) if stored else ""

    @staticmethod
    def _status_to_verdict(status: str) -> str:
        return {
            "pass": "verified",
            "fail": "sim_failed",
            "compile_error": "compile_error",
            "no_tool": "no_tool",
            "timeout": "sim_failed",
            "error": "error",
        }.get(status, "error")
