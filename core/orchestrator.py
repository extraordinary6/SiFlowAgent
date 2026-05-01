from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml
from loguru import logger

from context.manager import ContextManager
from core.agent_loop import AgentLoop, AgentLoopResult, AgentStep, StepCallback
from core.agents import ActorAgent, AgentConfig, AgentMessage, CriticAgent, VerifierAgent
from core.coordinator import Coordinator, CoordinatorResult, MessageCallback
from core.llm_client import BaseLLMClient, LLMRequest
from core.memory import LongTermMemory
from core.session import SessionStore
from skills.chat import ChatSkill
from skills.hello import HelloSiFlowSkill
from skills.planner import PlannerDecision, PlannerSkill
from skills.registry import SkillRegistry
from skills.router import RouterDecision, RouterSkill
from skills.rtl_lint import RtlLintSkill
from skills.rtl_review import RtlReviewSkill
from skills.rtl_revise import RtlReviseSkill
from skills.rtl_sim import RtlSimResult, RtlSimSkill
from skills.cocotb_sim import CocotbSimResult, CocotbSimSkill
from skills.probe_inject import ProbeInjectResult, ProbeInjectSkill
from skills.spec_summary import SpecSummarySkill
from skills.verilog_template import VerilogModuleFile, VerilogTemplateResult, VerilogTemplateSkill

if TYPE_CHECKING:
    pass


class Orchestrator:
    def __init__(
        self,
        prompt_dir: str | Path,
        context_manager: ContextManager | None = None,
        llm_client: BaseLLMClient | None = None,
        project_root: str | Path | None = None,
        sessions_dir: str | Path | None = None,
    ) -> None:
        self.prompt_dir = Path(prompt_dir)
        self.project_root = Path(project_root) if project_root else self.prompt_dir.parent
        self.context_manager = context_manager or ContextManager()
        if self.context_manager.long_term is None:
            self.context_manager.attach_long_term(LongTermMemory.default_for(self.project_root))
        self.llm_client = llm_client
        self.skill_registry = SkillRegistry()
        self._default_system_prompt = self._load_default_system_prompt()
        self._register_default_skills()
        self.session_store = SessionStore(
            Path(sessions_dir) if sessions_dir else self.project_root / "data" / "sessions"
        )
        self.session_id: str | None = None

    def _load_default_system_prompt(self) -> str:
        try:
            prompt = self.load_prompt("default")
            return prompt.get("content", "You are SiFlowAgent.")
        except FileNotFoundError:
            return "You are SiFlowAgent."

    def _register_default_skills(self) -> None:
        self.skill_registry.register(HelloSiFlowSkill(context_manager=self.context_manager))
        self.skill_registry.register(VerilogTemplateSkill(context_manager=self.context_manager))
        self.skill_registry.register(RtlLintSkill(context_manager=self.context_manager))
        self.skill_registry.register(
            RtlSimSkill(
                context_manager=self.context_manager,
                project_root=self.project_root,
            )
        )
        self.skill_registry.register(
            CocotbSimSkill(
                context_manager=self.context_manager,
                project_root=self.project_root,
            )
        )
        self.skill_registry.register(
            ProbeInjectSkill(
                context_manager=self.context_manager,
                project_root=self.project_root,
            )
        )
        if self.llm_client is not None:
            self.skill_registry.register(
                SpecSummarySkill(context_manager=self.context_manager, llm_client=self.llm_client)
            )
            self.skill_registry.register(
                ChatSkill(
                    context_manager=self.context_manager,
                    llm_client=self.llm_client,
                    system_prompt=self._default_system_prompt,
                )
            )
            self.skill_registry.register(
                RtlReviewSkill(context_manager=self.context_manager, llm_client=self.llm_client)
            )
            self.skill_registry.register(
                RtlReviseSkill(context_manager=self.context_manager, llm_client=self.llm_client)
            )
            self.skill_registry.register(
                RouterSkill(context_manager=self.context_manager, llm_client=self.llm_client)
            )
            self.skill_registry.register(
                PlannerSkill(context_manager=self.context_manager, llm_client=self.llm_client)
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
        system_content = self._default_system_prompt
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

    async def build_verilog_template(self) -> VerilogTemplateResult:
        spec_summary = self.context_manager.get_state("last_spec_summary")
        if not spec_summary:
            raise RuntimeError("No spec summary available. Run /spec first.")

        self.context_manager.set_state("last_task", "verilog_template")
        result = await self.skill_registry.execute("verilog_template", spec_summary=spec_summary)
        logger.info("Executed verilog_template skill")
        return result

    async def generate_verilog_template(self) -> str:
        result = await self.build_verilog_template()
        return result.verilog_code

    async def generate_verilog_modules(self) -> list[VerilogModuleFile]:
        result = await self.build_verilog_template()
        return result.modules or [
            VerilogModuleFile(
                module_name=result.module_name,
                file_name=f"{result.module_name}.v",
                verilog_code=result.verilog_code,
            )
        ]

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

    # ---------------- Step 1: single-shot router ----------------

    async def route_and_execute(self, user_input: str) -> dict[str, Any]:
        if self.llm_client is None:
            raise RuntimeError("LLM client is not configured")

        catalog = self._public_skill_catalog()
        decision: RouterDecision = await self.skill_registry.execute(
            "router",
            user_input=user_input,
            skill_catalog=catalog,
        )
        logger.info(
            "Router chose skill={} args={} reason={}",
            decision.skill,
            decision.args,
            decision.reasoning,
        )
        result = await self.execute_action(decision.skill, dict(decision.args), user_input)
        self.context_manager.set_state("last_task", f"routed:{decision.skill}")
        return {"decision": decision.model_dump(), "result": result}

    # ---------------- Step 2: multi-step agent loop ----------------

    async def plan_next_step(self, goal: str, steps: list[AgentStep]) -> PlannerDecision:
        if self.llm_client is None:
            raise RuntimeError("LLM client is not configured")

        catalog = self._planner_skill_catalog()
        scratchpad = [asdict(step) for step in steps]
        decision: PlannerDecision = await self.skill_registry.execute(
            "planner",
            goal=goal,
            skill_catalog=catalog,
            scratchpad=scratchpad,
        )
        logger.info(
            "Planner chose action={} args={} thought={}",
            decision.action,
            decision.args,
            decision.thought,
        )
        return decision

    async def run_agent_loop(
        self,
        goal: str,
        max_steps: int = 6,
        on_step: StepCallback | None = None,
    ) -> AgentLoopResult:
        loop = AgentLoop(orchestrator=self, max_steps=max_steps, on_step=on_step)
        result = await loop.run(goal)
        self.context_manager.set_state(
            "last_agent_loop",
            {"goal": goal, "stopped_reason": result.stopped_reason, "completed": result.completed},
        )
        return result

    # ---------------- Shared action execution & observation ----------------

    async def execute_action(
        self,
        skill_name: str,
        args: dict[str, Any],
        user_input: str,
    ) -> Any:
        if not self.skill_registry.has(skill_name):
            raise RuntimeError(f"Unknown skill: {skill_name}")

        if skill_name == "chat":
            message = args.get("message") or user_input
            return await self.skill_registry.execute("chat", message=message)

        if skill_name == "hello_siflow":
            return await self.skill_registry.execute("hello_siflow")

        if skill_name == "spec_summary":
            spec_text = args.get("spec_text")
            spec_path = args.get("spec_path")
            if spec_path and not spec_text:
                resolved = Path(spec_path).expanduser()
                if not resolved.is_absolute():
                    resolved = self.project_root / resolved
                if not resolved.exists() or not resolved.is_file():
                    raise RuntimeError(f"Spec file not found: {resolved}")
                spec_text = resolved.read_text(encoding="utf-8")
            if not spec_text or not str(spec_text).strip():
                raise RuntimeError(
                    "spec_summary requires spec_text or a valid spec_path"
                )
            summary = await self.skill_registry.execute("spec_summary", spec_text=spec_text)
            return {
                "markdown_summary": summary.markdown_summary,
                "module_name": summary.module_name,
                "interfaces_count": len(summary.interfaces),
                "submodules": [sub.name for sub in summary.submodules],
            }

        if skill_name == "verilog_template":
            template = await self.build_verilog_template()
            output_path = args.get("output_path")
            saved: list[str] = []
            if output_path:
                saved = self._persist_verilog(template, output_path)
            return {
                "verilog_code": template.verilog_code,
                "modules": [module.file_name for module in template.modules],
                "saved": saved,
            }

        if skill_name == "rtl_lint":
            lint_args: dict[str, Any] = {}
            if args.get("verilog_code"):
                lint_args["verilog_code"] = args["verilog_code"]
            lint = await self.skill_registry.execute("rtl_lint", **lint_args)
            return {
                "findings_count": len(lint.findings),
                "todo_count": lint.todo_count,
                "module_count": lint.module_count,
                "always_posedge_count": lint.always_posedge_count,
                "reset_missing_count": lint.reset_missing_count,
                "empty_always_count": lint.empty_always_count,
                "findings": [finding.model_dump() for finding in lint.findings],
            }

        if skill_name == "rtl_review":
            review_args: dict[str, Any] = {}
            if args.get("verilog_code"):
                review_args["verilog_code"] = args["verilog_code"]
            review = await self.skill_registry.execute("rtl_review", **review_args)
            severity_counts = {"high": 0, "medium": 0, "low": 0}
            for issue in review.issues:
                if issue.severity in severity_counts:
                    severity_counts[issue.severity] += 1
            return {
                "overall_quality": review.overall_quality,
                "summary": review.summary,
                "issues_count": len(review.issues),
                "severity_counts": severity_counts,
                "recommendations": review.recommendations,
                "markdown_report": review.markdown_report,
            }

        if skill_name == "rtl_revise":
            revise_args: dict[str, Any] = {}
            if args.get("verilog_code"):
                revise_args["verilog_code"] = args["verilog_code"]
            revised = await self.skill_registry.execute("rtl_revise", **revise_args)
            current_count = self.context_manager.get_state("rtl_revise_count") or 0
            self.context_manager.set_state("rtl_revise_count", current_count + 1)

            output_path = args.get("output_path")
            saved: list[str] = []
            if output_path and revised.modules:
                target = Path(output_path).expanduser()
                if not target.is_absolute():
                    target = self.project_root / target
                if target.suffix.lower() == ".v":
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(revised.modules[0].verilog_code + "\n", encoding="utf-8")
                    saved.append(str(target))
                else:
                    target.mkdir(parents=True, exist_ok=True)
                    for module in revised.modules:
                        module_path = target / module.file_name
                        module_path.write_text(module.verilog_code + "\n", encoding="utf-8")
                        saved.append(str(module_path))
            return {
                "modules": [module.file_name for module in revised.modules],
                "changes_summary": revised.changes_summary,
                "addressed_issues_count": len(revised.addressed_issues),
                "unresolved_issues_count": len(revised.unresolved_issues),
                "addressed_issues": revised.addressed_issues,
                "unresolved_issues": revised.unresolved_issues,
                "saved": saved,
                "revise_iteration": current_count + 1,
            }

        if skill_name == "rtl_sim":
            sim_args: dict[str, Any] = {}
            for key in (
                "testbench_path",
                "top_module",
                "tool",
                "timeout_seconds",
                "pass_tokens",
                "fail_tokens",
                "extra_sources",
                "verilog_code",
            ):
                if args.get(key) is not None:
                    sim_args[key] = args[key]
            sim: RtlSimResult = await self.skill_registry.execute("rtl_sim", **sim_args)
            return {
                "tool": sim.tool,
                "status": sim.status,
                "compile_ok": sim.compile_ok,
                "run_ok": sim.run_ok,
                "pass_marker_seen": sim.pass_marker_seen,
                "fail_marker_seen": sim.fail_marker_seen,
                "assertions_passed": sim.assertions_passed,
                "assertions_failed": sim.assertions_failed,
                "duration_seconds": round(sim.duration_seconds, 3),
                "top_module": sim.top_module,
                "module_files": sim.module_files,
                "testbench": sim.testbench,
                "detail": sim.detail,
                "compile_log_tail": (sim.compile_log or "")[-800:],
                "run_log_tail": (sim.run_log or "")[-800:],
            }

        if skill_name == "cocotb_sim":
            sim_args: dict[str, Any] = {}
            for key in (
                "test_module",
                "hdl_toplevel",
                "test_dir",
                "verilog_sources",
                "extra_sources",
                "tool",
                "timeout_seconds",
                "testcase",
                "verilog_code",
            ):
                if args.get(key) is not None:
                    sim_args[key] = args[key]
            cocotb_sim: CocotbSimResult = await self.skill_registry.execute("cocotb_sim", **sim_args)
            return {
                "backend": "cocotb",
                "tool": cocotb_sim.tool,
                "status": cocotb_sim.status,
                "build_ok": cocotb_sim.build_ok,
                "run_ok": cocotb_sim.run_ok,
                "passes": cocotb_sim.passes,
                "fails": cocotb_sim.fails,
                "skipped": cocotb_sim.skipped,
                "duration_seconds": round(cocotb_sim.duration_seconds, 3),
                "hdl_toplevel": cocotb_sim.hdl_toplevel,
                "test_module": cocotb_sim.test_module,
                "verilog_sources": cocotb_sim.verilog_sources,
                "test_cases": [case.model_dump() for case in cocotb_sim.test_cases],
                "event_log": cocotb_sim.event_log,
                "detail": cocotb_sim.detail,
                "build_log_tail": cocotb_sim.build_log_tail,
                "run_log_tail": cocotb_sim.run_log_tail,
            }

        raise RuntimeError(f"No dispatcher implemented for skill: {skill_name}")

    def make_observation(self, skill_name: str, result: Any) -> str:
        if skill_name == "chat":
            text = str(result)
            return text if len(text) <= 600 else text[:600] + "..."

        if skill_name == "hello_siflow":
            return f"Greeted user: {result}"

        if skill_name == "spec_summary" and isinstance(result, dict):
            subs = result.get("submodules") or []
            sub_text = ", ".join(subs) if subs else "none"
            return (
                f"Produced spec summary. module_name={result.get('module_name')}, "
                f"interfaces={result.get('interfaces_count')}, submodules=[{sub_text}]."
            )

        if skill_name == "verilog_template" and isinstance(result, dict):
            modules = result.get("modules") or []
            saved = result.get("saved") or []
            base = f"Generated {len(modules)} Verilog module file(s): {modules}."
            if saved:
                base += f" Saved to: {saved}."
            return base

        if skill_name == "rtl_lint" and isinstance(result, dict):
            return (
                f"Lint: {result.get('findings_count', 0)} findings "
                f"(todo={result.get('todo_count', 0)}, "
                f"reset_missing={result.get('reset_missing_count', 0)}, "
                f"empty_always={result.get('empty_always_count', 0)}, "
                f"posedge_blocks={result.get('always_posedge_count', 0)})."
            )

        if skill_name == "rtl_review" and isinstance(result, dict):
            counts = result.get("severity_counts") or {}
            return (
                f"Review: quality={result.get('overall_quality')}, "
                f"issues={result.get('issues_count', 0)} "
                f"(high={counts.get('high', 0)}, medium={counts.get('medium', 0)}, low={counts.get('low', 0)}). "
                f"summary={(result.get('summary') or '')[:200]}"
            )

        if skill_name == "rtl_revise" and isinstance(result, dict):
            saved_suffix = f" Saved to: {result.get('saved')}." if result.get("saved") else ""
            return (
                f"Revise #{result.get('revise_iteration', '?')}: "
                f"addressed={result.get('addressed_issues_count', 0)}, "
                f"unresolved={result.get('unresolved_issues_count', 0)}, "
                f"modules={result.get('modules', [])}. "
                f"changes={(result.get('changes_summary') or '')[:200]}.{saved_suffix}"
            )

        if skill_name == "rtl_sim" and isinstance(result, dict):
            status = result.get("status", "error")
            tool = result.get("tool", "")
            return (
                f"Simulation {status} via {tool or 'no_tool'}: "
                f"compile_ok={result.get('compile_ok')}, run_ok={result.get('run_ok')}, "
                f"pass_marker={result.get('pass_marker_seen')}, "
                f"assertions={result.get('assertions_passed', 0)}/"
                f"{result.get('assertions_passed', 0) + result.get('assertions_failed', 0)} "
                f"({result.get('duration_seconds', 0)}s). "
                f"detail={(result.get('detail') or '')[:200]}"
            )

        if skill_name == "cocotb_sim" and isinstance(result, dict):
            status = result.get("status", "error")
            tool = result.get("tool", "")
            passes = result.get("passes", 0)
            fails = result.get("fails", 0)
            skipped = result.get("skipped", 0)
            total = passes + fails + skipped
            return (
                f"cocotb {status} via {tool or 'no_tool'}: "
                f"build_ok={result.get('build_ok')}, run_ok={result.get('run_ok')}, "
                f"cases={passes}/{total} pass ({fails} fail, {skipped} skip) "
                f"on {result.get('hdl_toplevel', '?')} "
                f"({result.get('duration_seconds', 0)}s). "
                f"detail={(result.get('detail') or '')[:200]}"
            )

        return str(result)[:600]

    # ---------------- Internal helpers ----------------

    def _public_skill_catalog(self) -> list[dict[str, Any]]:
        return [
            entry
            for entry in self.skill_registry.list_skills()
            if entry["name"] not in {"router", "planner"}
        ]

    def _planner_skill_catalog(self) -> list[dict[str, Any]]:
        # Planner produces final_answer directly via the "finish" action, so chat is
        # not needed as a downstream skill inside the loop.
        return [
            entry
            for entry in self.skill_registry.list_skills()
            if entry["name"] not in {"router", "planner", "chat"}
        ]

    def _persist_verilog(
        self,
        template: VerilogTemplateResult,
        output_path: str,
    ) -> list[str]:
        target = Path(output_path).expanduser()
        if not target.is_absolute():
            target = self.project_root / target

        saved: list[str] = []
        if target.suffix.lower() == ".v":
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(template.verilog_code + "\n", encoding="utf-8")
            saved.append(str(target))
        else:
            target.mkdir(parents=True, exist_ok=True)
            for module in template.modules:
                module_path = target / module.file_name
                module_path.write_text(module.verilog_code + "\n", encoding="utf-8")
                saved.append(str(module_path))
        return saved

    # ---------------- Step 5: session persistence & revise history ----------------

    def save_session(self, session_id: str | None = None) -> str:
        target_id = session_id or self.session_id or self.session_store.generate_id()
        self.session_store.save(target_id, self.context_manager)
        self.session_id = target_id
        logger.info("Saved session to {}", self.session_store.path_for(target_id))
        return target_id

    def load_session(self, session_id: str) -> dict[str, Any]:
        data = self.session_store.load(session_id, self.context_manager)
        self.session_id = session_id
        logger.info("Loaded session from {}", self.session_store.path_for(session_id))
        return data

    def list_sessions(self) -> list[dict[str, Any]]:
        return self.session_store.list_sessions()

    def session_info(self) -> dict[str, Any]:
        state = self.context_manager.state
        last_spec = state.get("last_spec_summary") or {}
        last_template = state.get("last_verilog_template") or {}
        last_review = state.get("last_rtl_review") or {}
        return {
            "session_id": self.session_id,
            "messages": len(self.context_manager.get_history()),
            "last_skill": state.get("last_skill"),
            "last_task": state.get("last_task"),
            "spec_module_name": last_spec.get("module_name"),
            "rtl_module_name": last_template.get("module_name"),
            "rtl_file_count": len(last_template.get("modules") or []),
            "review_issues": len(last_review.get("issues") or []),
            "review_quality": last_review.get("overall_quality"),
            "rtl_revise_count": state.get("rtl_revise_count") or 0,
            "revise_history_len": len(state.get("revise_history") or []),
        }

    def get_revise_history(self) -> list[dict[str, Any]]:
        history = list(self.context_manager.get_state("revise_history") or [])
        current = self.context_manager.get_state("last_verilog_template")
        summaries: list[dict[str, Any]] = []
        for entry in history:
            summaries.append(
                {
                    "iteration": entry.get("iteration"),
                    "created_at": entry.get("created_at"),
                    "module_files": [m.get("file_name") for m in (entry.get("modules") or [])],
                    "source_review_issues": len((entry.get("source_review") or {}).get("issues") or []),
                    "source_lint_findings": len((entry.get("source_lint") or {}).get("findings") or []),
                    "verilog_preview": (entry.get("verilog_code") or "")[:160],
                }
            )
        # append the currently-live version as a virtual "head"
        if current:
            summaries.append(
                {
                    "iteration": len(history),
                    "created_at": "(current)",
                    "module_files": [m.get("file_name") for m in (current.get("modules") or [])],
                    "source_review_issues": None,
                    "source_lint_findings": None,
                    "verilog_preview": (current.get("verilog_code") or "")[:160],
                    "current": True,
                }
            )
        return summaries

    def rollback_revise(self, iteration: int) -> dict[str, Any]:
        history = list(self.context_manager.get_state("revise_history") or [])
        if iteration < 0 or iteration >= len(history):
            raise ValueError(
                f"iteration {iteration} out of range (valid: 0..{len(history) - 1 if history else -1})"
            )
        snapshot = history[iteration]
        modules = snapshot.get("modules") or []
        top_module_name = modules[0].get("module_name", "") if modules else ""
        restored = {
            "module_name": top_module_name,
            "port_declarations": [],
            "body_lines": [],
            "verilog_code": snapshot.get("verilog_code", ""),
            "modules": modules,
        }
        # Branch off iteration N: keep history[:iteration], discard everything after.
        self.context_manager.set_state("last_verilog_template", restored)
        self.context_manager.set_state("revise_history", history[:iteration])
        self.context_manager.set_state("rtl_revise_count", iteration)
        # Invalidate stale review/lint since we just swapped the RTL underneath.
        self.context_manager.set_state("last_rtl_review", None)
        self.context_manager.set_state("last_rtl_lint", None)
        logger.info("Rolled back revise_history to iteration {}", iteration)
        return {
            "restored_iteration": iteration,
            "module_files": [m.get("file_name") for m in modules],
            "remaining_history_len": iteration,
        }

    # ---------------- Step 6-B: multi-agent actor-critic coordinator ----------------

    def build_actor_agent(
        self,
        system_prompt: str | None = None,
        temperature: float = 0.3,
        max_substeps: int = 4,
        llm_client: BaseLLMClient | None = None,
    ) -> ActorAgent:
        if self.llm_client is None and llm_client is None:
            raise RuntimeError("Actor agent requires an LLM client")
        config = AgentConfig(
            name="actor",
            persona="senior RTL engineer",
            system_prompt=(
                system_prompt
                or (
                    "You are the ActorAgent of SiFlowAgent: a senior RTL engineer who turns hardware specs "
                    "into Verilog and applies critic feedback. You may call at most one skill per sub-step, "
                    "chaining sub-steps until the RTL is ready for the critic. Prefer spec_summary first when "
                    "no structured spec exists, verilog_template to generate, and rtl_revise to apply critic "
                    "feedback (never regenerate from scratch after a revise). Hand off as soon as the latest "
                    "generated or revised RTL is ready for review."
                )
            ),
            temperature=temperature,
            max_tokens=700,
            allowed_skills=["spec_summary", "verilog_template", "rtl_revise"],
            max_substeps=max_substeps,
        )
        return ActorAgent(config=config, llm_client=llm_client or self.llm_client, orchestrator=self)

    def build_critic_agent(
        self,
        system_prompt: str | None = None,
        temperature: float = 0.0,
        llm_client: BaseLLMClient | None = None,
    ) -> CriticAgent:
        if self.llm_client is None and llm_client is None:
            raise RuntimeError("Critic agent requires an LLM client")
        config = AgentConfig(
            name="critic",
            persona="strict RTL reviewer",
            system_prompt=(
                system_prompt
                or (
                    "You are the CriticAgent of SiFlowAgent: a strict and fair RTL reviewer. Your only job is "
                    "to judge the most recent RTL produced by the actor. You see rtl_lint and rtl_review "
                    "outputs and must return a verdict. Reject 'accept' if any high-severity issue exists or "
                    "if more than one medium issue remains. Be concise and concrete."
                )
            ),
            temperature=temperature,
            max_tokens=500,
            allowed_skills=["rtl_lint", "rtl_review"],
        )
        return CriticAgent(config=config, llm_client=llm_client or self.llm_client, orchestrator=self)

    async def run_multi_agent(
        self,
        goal: str,
        max_rounds: int = 3,
        on_message: MessageCallback | None = None,
        actor: ActorAgent | None = None,
        critic: CriticAgent | None = None,
    ) -> CoordinatorResult:
        if self.llm_client is None:
            raise RuntimeError("Multi-agent mode requires an LLM client")
        actor = actor or self.build_actor_agent()
        critic = critic or self.build_critic_agent()
        coordinator = Coordinator(
            orchestrator=self,
            actor=actor,
            critic=critic,
            max_rounds=max_rounds,
            on_message=on_message,
        )
        result = await coordinator.run(goal)
        self.context_manager.set_state(
            "last_multi_agent_run",
            {
                "goal": goal,
                "accepted": result.accepted,
                "rounds_used": result.rounds_used,
                "stopped_reason": result.stopped_reason,
            },
        )
        return result

    # ---------------- Step 7: behavioral verification (VerifierAgent) ----------------

    def build_verifier_agent(
        self,
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
        llm_client: BaseLLMClient | None = None,
    ) -> VerifierAgent:
        """Build a VerifierAgent. The agent supports two simulator backends —
        ``rtl_sim`` (Verilog testbench) and ``cocotb_sim`` (Python testbench).
        Pass cocotb_* arguments to select the cocotb backend; otherwise the
        Verilog-testbench path is used.

        An LLM client is not strictly required because the agent only
        orchestrates simulator skills, but the signature stays uniform with
        the actor/critic builders in case future extensions add LLM-driven
        retry reasoning.
        """
        backend_skills = ["rtl_sim", "cocotb_sim"]
        config = AgentConfig(
            name="verifier",
            persona="verification engineer",
            system_prompt=(
                "You are the VerifierAgent of SiFlowAgent: you run a real simulator on the "
                "most recent RTL produced by the actor and report a behavioral verdict. "
                "You do not reason about style or quality — only whether the design passes the testbench."
            ),
            temperature=0.0,
            max_tokens=200,
            allowed_skills=backend_skills,
        )
        return VerifierAgent(
            config=config,
            llm_client=llm_client or self.llm_client,  # may be None; unused by the current impl
            orchestrator=self,
            testbench_path=testbench_path,
            top_module=top_module,
            timeout_seconds=timeout_seconds,
            pass_tokens=pass_tokens,
            fail_tokens=fail_tokens,
            extra_sources=extra_sources,
            cocotb_test_module=cocotb_test_module,
            cocotb_hdl_toplevel=cocotb_hdl_toplevel,
            cocotb_test_dir=cocotb_test_dir,
            cocotb_verilog_sources=cocotb_verilog_sources,
            cocotb_testcase=cocotb_testcase,
            tool=tool,
        )

    async def verify_rtl(
        self,
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
        on_message: MessageCallback | None = None,
    ) -> AgentMessage:
        """Single-shot verification: run VerifierAgent once against the current RTL.

        Intended to be called after ``run_agent_loop`` or ``run_multi_agent``
        so the freshly-produced RTL is already stored in
        ``last_verilog_template``. The returned AgentMessage contains the
        structured verdict under ``payload``.

        Pass cocotb_* arguments to use the cocotb backend; otherwise the
        Verilog testbench (``testbench_path``) is used.
        """
        verifier = self.build_verifier_agent(
            testbench_path=testbench_path,
            top_module=top_module,
            timeout_seconds=timeout_seconds,
            pass_tokens=pass_tokens,
            fail_tokens=fail_tokens,
            extra_sources=extra_sources,
            cocotb_test_module=cocotb_test_module,
            cocotb_hdl_toplevel=cocotb_hdl_toplevel,
            cocotb_test_dir=cocotb_test_dir,
            cocotb_verilog_sources=cocotb_verilog_sources,
            cocotb_testcase=cocotb_testcase,
            tool=tool,
        )
        message = await verifier.respond(transcript=[], goal="verify", round_idx=1)
        payload = message.payload or {}
        self.context_manager.set_state(
            "last_verification",
            {
                "verdict": payload.get("verdict"),
                "status": payload.get("status"),
                "backend": payload.get("backend"),
                "testbench": testbench_path,
                "top_module": top_module,
                "cocotb_test_module": cocotb_test_module,
                "cocotb_hdl_toplevel": cocotb_hdl_toplevel,
            },
        )
        if on_message is not None:
            outcome = on_message(message)
            if hasattr(outcome, "__await__"):
                await outcome  # type: ignore[func-returns-value]
        return message
