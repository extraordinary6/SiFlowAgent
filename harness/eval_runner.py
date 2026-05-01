from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from context.manager import ContextManager
from core.llm_client import BaseLLMClient, LLMRequest
from core.orchestrator import Orchestrator
from skills.rtl_lint import RtlLintSkill
from skills.rtl_sim import RtlSimSkill
from skills.cocotb_sim import CocotbSimSkill

from harness.eval_scenarios import Scenario


class CountingLLMClient(BaseLLMClient):
    """Wraps any BaseLLMClient and counts generate calls plus rough I/O volume.

    Used by the eval runner so every scenario's cost can be reported
    alongside its correctness score.
    """

    def __init__(self, inner: BaseLLMClient) -> None:
        self.inner = inner
        self.calls = 0
        self.total_request_chars = 0
        self.total_response_chars = 0

    async def generate(self, request: LLMRequest) -> str:
        self.calls += 1
        self.total_request_chars += len(request.system_prompt or "")
        for message in request.messages:
            self.total_request_chars += len(message.get("content", ""))
        out = await self.inner.generate(request)
        self.total_response_chars += len(out)
        return out


@dataclass
class CheckResult:
    name: str
    passed: bool
    detail: str = ""
    category: str = ""


@dataclass
class JudgeResult:
    score: float
    passed: bool
    rationale: str


@dataclass
class TierRunOutcome:
    tier: str
    scenario_id: str
    final_text: str = ""
    final_rtl: str = ""
    module_files: list[str] = field(default_factory=list)
    llm_calls: int = 0
    tool_llm_calls: int = 0
    judge_llm_calls: int = 0
    wall_seconds: float = 0.0
    steps_used: int = 0
    completed: bool = False
    accepted: bool = False
    error: str = ""
    checks: list[CheckResult] = field(default_factory=list)
    judge: JudgeResult | None = None
    deterministic_passed: bool = False
    overall_passed: bool = False
    sim_status: str = ""
    sim_tool: str = ""
    sim_detail: str = ""
    sim_duration_seconds: float = 0.0
    sim_backend: str = ""


def _strip_json_fences(text: str) -> str:
    stripped = (text or "").strip()
    if not stripped.startswith("```"):
        return stripped
    lines = stripped.splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines).strip()


async def run_scenario_on_tier(
    scenario: Scenario,
    tier: str,
    llm_client: BaseLLMClient,
    project_root: Path,
    judge: bool = False,
) -> TierRunOutcome:
    counting = CountingLLMClient(llm_client)
    context_manager = ContextManager()
    orchestrator = Orchestrator(
        prompt_dir=project_root / "prompts",
        context_manager=context_manager,
        llm_client=counting,
        project_root=project_root,
    )

    outcome = TierRunOutcome(tier=tier, scenario_id=scenario.id)
    start = time.monotonic()

    try:
        preload_text = scenario.preload_spec_text or ""
        if scenario.preload_spec_file:
            preload_path = Path(scenario.preload_spec_file)
            if not preload_path.is_absolute():
                preload_path = project_root / preload_path
            preload_text = preload_path.read_text(encoding="utf-8")
        if preload_text.strip():
            await orchestrator.summarize_spec(preload_text)

        if tier == "router":
            routed = await orchestrator.route_and_execute(scenario.goal)
            outcome.final_text = _stringify_result(routed.get("result"))
            outcome.completed = True
            outcome.steps_used = 1
        elif tier == "agent":
            agent_result = await orchestrator.run_agent_loop(scenario.goal, max_steps=8)
            outcome.final_text = agent_result.final_answer
            outcome.completed = agent_result.completed
            outcome.steps_used = len(agent_result.steps)
        elif tier == "multi":
            multi_result = await orchestrator.run_multi_agent(scenario.goal, max_rounds=3)
            outcome.final_text = ""
            for message in reversed(multi_result.transcript):
                if message.sender == "coordinator" and message.kind == "final":
                    outcome.final_text = message.content
                    break
            outcome.completed = multi_result.accepted
            outcome.accepted = multi_result.accepted
            outcome.steps_used = multi_result.rounds_used
        else:
            raise ValueError(f"unknown tier: {tier}")

        last_template = context_manager.get_state("last_verilog_template") or {}
        outcome.final_rtl = last_template.get("verilog_code", "")
        outcome.module_files = [
            module.get("file_name", "") for module in (last_template.get("modules") or [])
        ]

    except Exception as error:  # noqa: BLE001 - capture for reporting
        outcome.error = str(error)
    finally:
        outcome.wall_seconds = time.monotonic() - start
        outcome.tool_llm_calls = counting.calls

    outcome.checks = await _grade_outcome(scenario, tier, outcome, context_manager, project_root)
    outcome.deterministic_passed = (not outcome.error) and all(check.passed for check in outcome.checks)

    if judge and scenario.judge.enabled and scenario.judge.criteria:
        judge_start_calls = counting.calls
        try:
            outcome.judge = await _run_judge(counting, scenario, outcome)
        except Exception as error:  # noqa: BLE001
            outcome.judge = JudgeResult(score=0.0, passed=False, rationale=f"judge error: {error}")
        outcome.judge_llm_calls = counting.calls - judge_start_calls

    outcome.llm_calls = counting.calls

    overall = outcome.deterministic_passed
    if outcome.judge is not None:
        overall = overall and outcome.judge.passed
    outcome.overall_passed = overall
    return outcome


async def _grade_outcome(
    scenario: Scenario,
    tier: str,
    outcome: TierRunOutcome,
    context_manager: ContextManager,
    project_root: Path,
) -> list[CheckResult]:
    checks_spec = scenario.checks
    results: list[CheckResult] = []

    if outcome.error:
        results.append(
            CheckResult("no_runtime_error", False, outcome.error, "error")
        )

    if checks_spec.require_completed:
        if tier == "multi":
            results.append(
                CheckResult(
                    "multi_accepted",
                    outcome.accepted,
                    f"accepted={outcome.accepted}",
                    "completion",
                )
            )
        elif tier == "agent":
            results.append(
                CheckResult(
                    "agent_completed",
                    outcome.completed,
                    f"completed={outcome.completed}",
                    "completion",
                )
            )
        else:
            results.append(
                CheckResult(
                    "router_completed",
                    outcome.completed,
                    "single-shot",
                    "completion",
                )
            )

    if tier in checks_spec.max_llm_calls:
        budget = checks_spec.max_llm_calls[tier]
        passed = outcome.tool_llm_calls <= budget
        results.append(
            CheckResult(
                f"llm_calls_budget[{tier}]",
                passed,
                f"{outcome.tool_llm_calls} <= {budget}",
                "budget",
            )
        )

    last_template = context_manager.get_state("last_verilog_template") or {}
    aggregated_code = _aggregate_code(last_template)

    if checks_spec.min_modules:
        module_count = len(last_template.get("modules") or [])
        passed = module_count >= checks_spec.min_modules
        results.append(
            CheckResult(
                "min_modules",
                passed,
                f"{module_count} >= {checks_spec.min_modules}",
                "rtl",
            )
        )

    if aggregated_code and checks_spec.lint:
        lint_cm = ContextManager()
        lint_cm.set_state("last_verilog_template", last_template)
        lint_result = await RtlLintSkill(lint_cm).execute()
        lint_counters = {
            "reset_missing_count": lint_result.reset_missing_count,
            "empty_always_count": lint_result.empty_always_count,
            "todo_count": lint_result.todo_count,
            "module_count": lint_result.module_count,
            "always_posedge_count": lint_result.always_posedge_count,
        }
        for key, bound in checks_spec.lint.items():
            if key.endswith("_max"):
                counter = key[: -len("_max")]
                value = lint_counters.get(counter)
                passed = value is not None and value <= bound
                results.append(
                    CheckResult(
                        f"lint.{counter}<={bound}",
                        passed,
                        f"observed={value}",
                        "lint",
                    )
                )
            elif key.endswith("_min"):
                counter = key[: -len("_min")]
                value = lint_counters.get(counter)
                passed = value is not None and value >= bound
                results.append(
                    CheckResult(
                        f"lint.{counter}>={bound}",
                        passed,
                        f"observed={value}",
                        "lint",
                    )
                )

    if checks_spec.contains_all:
        for needle in checks_spec.contains_all:
            passed = needle in aggregated_code
            results.append(
                CheckResult(f"contains_all: {needle!r}", passed, "", "content")
            )
    if checks_spec.contains_any:
        passed = any(needle in aggregated_code for needle in checks_spec.contains_any)
        results.append(
            CheckResult(
                "contains_any",
                passed,
                f"any_of {checks_spec.contains_any}",
                "content",
            )
        )
    if checks_spec.not_contains:
        for needle in checks_spec.not_contains:
            passed = needle not in aggregated_code
            results.append(
                CheckResult(f"not_contains: {needle!r}", passed, "", "content")
            )

    if scenario.simulation.enabled:
        sim_checks = await _run_simulation_checks(
            scenario, outcome, context_manager, project_root
        )
        results.extend(sim_checks)

    if scenario.cocotb.enabled:
        cocotb_checks = await _run_cocotb_checks(
            scenario, outcome, context_manager, project_root
        )
        results.extend(cocotb_checks)

    return results


async def _run_simulation_checks(
    scenario: Scenario,
    outcome: TierRunOutcome,
    context_manager: ContextManager,
    project_root: Path,
) -> list[CheckResult]:
    """Run a real simulator against the tier's final RTL and turn the verdict
    into one or more deterministic checks.

    Failure here is meaningful: it means lint/text checks agreed but the
    design does not actually behave correctly under the testbench. This is
    the upgrade from string matching to behavior verification.
    """
    sim_conf = scenario.simulation
    checks: list[CheckResult] = []

    last_template = context_manager.get_state("last_verilog_template") or {}
    aggregated_code = _aggregate_code(last_template)
    if not aggregated_code:
        checks.append(
            CheckResult(
                "sim.no_rtl",
                False,
                "tier produced no RTL for the simulator to run",
                "sim",
            )
        )
        return checks

    if not sim_conf.testbench:
        checks.append(
            CheckResult(
                "sim.no_testbench",
                False,
                "simulation.enabled=true but simulation.testbench is empty",
                "sim",
            )
        )
        return checks

    skill = RtlSimSkill(
        context_manager=context_manager,
        project_root=project_root,
    )
    sim_args: dict[str, Any] = {
        "testbench_path": sim_conf.testbench,
        "tool": sim_conf.tool or "auto",
        "timeout_seconds": sim_conf.timeout_seconds,
    }
    if sim_conf.top_module:
        sim_args["top_module"] = sim_conf.top_module
    if sim_conf.pass_tokens:
        sim_args["pass_tokens"] = sim_conf.pass_tokens
    if sim_conf.fail_tokens:
        sim_args["fail_tokens"] = sim_conf.fail_tokens
    if sim_conf.extra_sources:
        sim_args["extra_sources"] = sim_conf.extra_sources

    try:
        sim_result = await skill.execute(**sim_args)
    except Exception as error:  # noqa: BLE001
        checks.append(
            CheckResult(
                "sim.run",
                False,
                f"simulator skill raised: {error}",
                "sim",
            )
        )
        return checks

    outcome.sim_status = sim_result.status
    outcome.sim_tool = sim_result.tool
    outcome.sim_detail = sim_result.detail
    outcome.sim_duration_seconds = round(sim_result.duration_seconds, 3)
    outcome.sim_backend = "rtl_sim"

    if sim_result.status == "no_tool":
        if sim_conf.require_tool:
            checks.append(
                CheckResult(
                    "sim.tool_available",
                    False,
                    sim_result.detail or "no simulator on PATH",
                    "sim",
                )
            )
        else:
            checks.append(
                CheckResult(
                    "sim.tool_available[skipped]",
                    True,
                    "require_tool=false; sim skipped",
                    "sim",
                )
            )
        return checks

    checks.append(
        CheckResult(
            f"sim.compile[{sim_result.tool}]",
            sim_result.compile_ok,
            (sim_result.compile_log or "")[-200:],
            "sim",
        )
    )
    if not sim_result.compile_ok:
        return checks

    passed = sim_result.status == "pass"
    detail = (
        f"status={sim_result.status} "
        f"assertions={sim_result.assertions_passed}/"
        f"{sim_result.assertions_passed + sim_result.assertions_failed} "
        f"pass_marker={sim_result.pass_marker_seen} "
        f"fail_marker={sim_result.fail_marker_seen} "
        f"in {sim_result.duration_seconds:.1f}s"
    )
    checks.append(
        CheckResult(
            f"sim.behavior[{sim_result.tool}]",
            passed,
            detail,
            "sim",
        )
    )
    return checks


async def _run_cocotb_checks(
    scenario: Scenario,
    outcome: TierRunOutcome,
    context_manager: ContextManager,
    project_root: Path,
) -> list[CheckResult]:
    """cocotb counterpart of `_run_simulation_checks`.

    Emits ``cocotb.build[<tool>]`` and ``cocotb.cases[<tool>]`` checks: the
    first asserts the design elaborates against the cocotb runner, the second
    asserts every test case in the Python testbench passes.
    """
    cocotb_conf = scenario.cocotb
    checks: list[CheckResult] = []

    last_template = context_manager.get_state("last_verilog_template") or {}
    aggregated_code = _aggregate_code(last_template)
    if not aggregated_code and not cocotb_conf.verilog_sources:
        checks.append(
            CheckResult(
                "cocotb.no_rtl",
                False,
                "tier produced no RTL and scenario.cocotb.verilog_sources is empty",
                "sim",
            )
        )
        return checks

    if not cocotb_conf.test_module or not cocotb_conf.hdl_toplevel:
        checks.append(
            CheckResult(
                "cocotb.no_test",
                False,
                "scenario.cocotb.enabled=true but test_module / hdl_toplevel missing",
                "sim",
            )
        )
        return checks

    skill = CocotbSimSkill(
        context_manager=context_manager,
        project_root=project_root,
    )
    sim_args: dict[str, Any] = {
        "test_module": cocotb_conf.test_module,
        "hdl_toplevel": cocotb_conf.hdl_toplevel,
        "tool": cocotb_conf.tool or "auto",
        "timeout_seconds": cocotb_conf.timeout_seconds,
    }
    if cocotb_conf.test_dir:
        sim_args["test_dir"] = cocotb_conf.test_dir
    if cocotb_conf.verilog_sources:
        sim_args["verilog_sources"] = cocotb_conf.verilog_sources
    if cocotb_conf.extra_sources:
        sim_args["extra_sources"] = cocotb_conf.extra_sources
    if cocotb_conf.testcase:
        sim_args["testcase"] = cocotb_conf.testcase

    try:
        sim_result = await skill.execute(**sim_args)
    except Exception as error:  # noqa: BLE001
        checks.append(
            CheckResult(
                "cocotb.run",
                False,
                f"cocotb_sim raised: {error}",
                "sim",
            )
        )
        return checks

    outcome.sim_status = sim_result.status
    outcome.sim_tool = sim_result.tool
    outcome.sim_detail = sim_result.detail
    outcome.sim_duration_seconds = round(sim_result.duration_seconds, 3)
    outcome.sim_backend = "cocotb"

    if sim_result.status == "no_tool":
        if cocotb_conf.require_tool:
            checks.append(
                CheckResult(
                    "cocotb.tool_available",
                    False,
                    sim_result.detail or "no simulator on PATH",
                    "sim",
                )
            )
        else:
            checks.append(
                CheckResult(
                    "cocotb.tool_available[skipped]",
                    True,
                    "require_tool=false; cocotb skipped",
                    "sim",
                )
            )
        return checks

    checks.append(
        CheckResult(
            f"cocotb.build[{sim_result.tool}]",
            sim_result.build_ok,
            (sim_result.build_log_tail or "")[-200:],
            "sim",
        )
    )
    if not sim_result.build_ok:
        return checks

    total = sim_result.passes + sim_result.fails + sim_result.skipped
    detail = (
        f"status={sim_result.status} cases={sim_result.passes}/{total} pass "
        f"({sim_result.fails} fail, {sim_result.skipped} skip) in {sim_result.duration_seconds:.1f}s"
    )
    checks.append(
        CheckResult(
            f"cocotb.cases[{sim_result.tool}]",
            sim_result.status == "pass",
            detail,
            "sim",
        )
    )
    return checks


def _aggregate_code(last_template: dict[str, Any]) -> str:
    parts: list[str] = []
    seen: set[str] = set()
    top_code = last_template.get("verilog_code", "")
    if top_code:
        parts.append(top_code)
        seen.add(top_code)
    for module in last_template.get("modules") or []:
        code = module.get("verilog_code", "")
        if code and code not in seen:
            parts.append(code)
            seen.add(code)
    return "\n\n".join(parts)


async def _run_judge(
    llm_client: BaseLLMClient,
    scenario: Scenario,
    outcome: TierRunOutcome,
) -> JudgeResult:
    system_prompt = (
        "You are a strict but fair evaluator for hardware design agents. "
        "Given a goal, the agent's final artifact, and success criteria, produce a score and a pass decision. "
        "Return valid JSON only, no markdown fences. "
        'Schema: {"score": float in [0.0, 1.0], "passed": bool, "rationale": string}. '
        f"Pass threshold: score >= {scenario.judge.pass_threshold}. "
        "0.0 = total failure, 0.5 = partial with significant gaps, 1.0 = fully satisfies the goal. "
        "Be concise and concrete in the rationale."
    )
    rtl_preview = outcome.final_rtl[:2000]
    text_preview = outcome.final_text[:1500]
    user_content = (
        f"Goal:\n{scenario.goal}\n\n"
        f"Success criteria:\n{scenario.judge.criteria}\n\n"
        f"Tier: {outcome.tier}\n"
        f"Final text from the agent:\n{text_preview or '(empty)'}\n\n"
        f"Final RTL (truncated):\n{rtl_preview or '(no RTL was produced)'}\n\n"
        "Produce the score JSON."
    )

    raw = await llm_client.generate(
        LLMRequest(
            system_prompt=system_prompt,
            messages=[{"role": "user", "content": user_content}],
            temperature=0.0,
            max_tokens=400,
        )
    )
    data = json.loads(_strip_json_fences(raw))
    score = float(data.get("score", 0.0))
    passed = bool(data.get("passed", score >= scenario.judge.pass_threshold))
    rationale = str(data.get("rationale", ""))
    return JudgeResult(score=score, passed=passed, rationale=rationale)


def _stringify_result(result: Any) -> str:
    if result is None:
        return ""
    if isinstance(result, str):
        return result
    if isinstance(result, dict):
        if "markdown_summary" in result:
            return str(result["markdown_summary"])
        if "verilog_code" in result:
            return str(result.get("verilog_code", ""))
    try:
        return json.dumps(result, ensure_ascii=False)[:2000]
    except (TypeError, ValueError):
        return str(result)[:2000]


def outcome_to_dict(outcome: TierRunOutcome) -> dict[str, Any]:
    data = asdict(outcome)
    data["checks"] = [asdict(check) for check in outcome.checks]
    data["judge"] = asdict(outcome.judge) if outcome.judge else None
    data["final_rtl"] = outcome.final_rtl[:4000]
    data["final_text"] = outcome.final_text[:2000]
    return data
