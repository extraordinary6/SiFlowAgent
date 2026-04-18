from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field


class ScenarioSimulation(BaseModel):
    """Per-scenario behavioral verification via a real simulator.

    When ``enabled`` is true the eval grader runs ``rtl_sim`` on the RTL
    produced by the tier against the given testbench and turns the simulator's
    pass/fail into a deterministic check. This is the ``text-match -> behavior``
    upgrade path: a scenario whose string checks pass can still fail here if
    the generated design does not actually work.
    """

    enabled: bool = Field(default=False)
    testbench: str = Field(default="", description="Path to the testbench .v file (project-relative OK).")
    top_module: str = Field(default="", description="Top testbench module name. Auto-detected when empty.")
    tool: str = Field(default="auto", description="iverilog | verilator | auto")
    timeout_seconds: float = Field(default=20.0)
    pass_tokens: list[str] = Field(default_factory=list)
    fail_tokens: list[str] = Field(default_factory=list)
    extra_sources: list[str] = Field(default_factory=list)
    require_tool: bool = Field(
        default=True,
        description=(
            "If true (default), a missing iverilog/verilator is an explicit failure. "
            "Set to false to record the missing-tool case as skipped/info so the scenario "
            "still passes on a machine without a simulator installed."
        ),
    )


class ScenarioChecks(BaseModel):
    """Deterministic, reproducible checks applied to a tier's output."""

    require_completed: bool = Field(default=True)
    lint: dict[str, int] = Field(
        default_factory=dict,
        description="Upper/lower bounds on RtlLint counters, e.g. reset_missing_count_max, module_count_min.",
    )
    contains_any: list[str] = Field(default_factory=list, description="Final RTL must contain at least one.")
    contains_all: list[str] = Field(default_factory=list, description="Final RTL must contain every entry.")
    not_contains: list[str] = Field(default_factory=list, description="Final RTL must contain none of these.")
    max_llm_calls: dict[str, int] = Field(
        default_factory=dict,
        description="Per-tier soft budget for total LLM.generate calls, e.g. {'router': 3, 'agent': 10}",
    )
    min_modules: int = Field(default=0, description="Minimum number of generated Verilog module files.")


class ScenarioJudge(BaseModel):
    enabled: bool = Field(default=False)
    criteria: str = Field(
        default="",
        description="Natural-language success criteria shown to the LLM judge.",
    )
    pass_threshold: float = Field(default=0.7, description="Minimum score the judge must assign to pass.")


class Scenario(BaseModel):
    id: str
    description: str = Field(default="")
    preload_spec_text: str = Field(default="")
    preload_spec_file: str = Field(default="")
    goal: str
    tiers: list[str] = Field(default_factory=lambda: ["router", "agent", "multi"])
    checks: ScenarioChecks = Field(default_factory=ScenarioChecks)
    judge: ScenarioJudge = Field(default_factory=ScenarioJudge)
    simulation: ScenarioSimulation = Field(default_factory=ScenarioSimulation)


def load_scenarios(scenarios_dir: Path, only_ids: list[str] | None = None) -> list[Scenario]:
    scenarios: list[Scenario] = []
    for path in sorted(scenarios_dir.glob("*.yaml")):
        raw: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        scenario = Scenario(**raw)
        if only_ids and scenario.id not in only_ids:
            continue
        scenarios.append(scenario)
    return scenarios
