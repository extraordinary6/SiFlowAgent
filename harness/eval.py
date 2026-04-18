from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.llm_client import load_llm_client_from_env

from harness.eval_runner import TierRunOutcome, outcome_to_dict, run_scenario_on_tier
from harness.eval_scenarios import Scenario, load_scenarios

SCENARIOS_DIR = PROJECT_ROOT / "harness" / "scenarios"
DEFAULT_TIERS = ["router", "agent", "multi"]


def _fmt_pass(flag: bool) -> str:
    return "pass" if flag else "FAIL"


def _print_row(scenario_id: str, outcome: TierRunOutcome) -> None:
    passed_count = sum(1 for check in outcome.checks if check.passed)
    total_checks = len(outcome.checks)
    judge_cell = "-"
    if outcome.judge is not None:
        judge_cell = f"{outcome.judge.score:.2f}{'+' if outcome.judge.passed else '-'}"
    sim_cell = "-"
    if outcome.sim_status:
        marker = {"pass": "+", "fail": "-", "no_tool": "?", "compile_error": "x", "timeout": "T"}.get(
            outcome.sim_status, "?"
        )
        sim_cell = f"{outcome.sim_status}{marker}"
    overall = _fmt_pass(outcome.overall_passed)
    print(
        f"  {scenario_id:<18} | {outcome.tier:<6} | {overall:<5} | "
        f"{passed_count}/{total_checks:<3} | {judge_cell:<6} | {sim_cell:<14} | "
        f"llm={outcome.llm_calls:<3} steps={outcome.steps_used:<2} wall={outcome.wall_seconds:.1f}s"
    )


def _print_summary(summary: dict[str, dict[str, float]]) -> None:
    print()
    print("Summary by tier:")
    for tier, stats in summary.items():
        if stats["total"] == 0:
            continue
        pass_rate = stats["passed"] / stats["total"] * 100
        print(
            f"  {tier:<6} : {stats['passed']}/{stats['total']} passed ({pass_rate:.0f}%)  "
            f"avg_llm={stats['avg_llm_calls']:.1f}  avg_wall={stats['avg_wall']:.1f}s  "
            f"avg_judge={stats['avg_judge_score']:.2f}"
        )


async def _run_all(
    scenarios: list[Scenario],
    tiers: list[str],
    judge: bool,
) -> dict:
    llm_client = load_llm_client_from_env()
    if llm_client is None:
        raise RuntimeError(
            "No LLM client configured. Set SIFLOW_LLM_* env vars before running eval."
        )

    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    report = {
        "run_id": run_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "tiers_run": tiers,
        "judge_enabled": judge,
        "scenarios": [],
    }
    summary: dict[str, dict[str, float]] = {
        tier: {
            "total": 0,
            "passed": 0,
            "avg_llm_calls": 0.0,
            "avg_wall": 0.0,
            "avg_judge_score": 0.0,
            "_judge_total": 0.0,
            "_judge_count": 0,
        }
        for tier in tiers
    }

    print(
        f"\nEval run {run_id}  (scenarios={len(scenarios)}, tiers={tiers}, judge={judge})\n"
    )
    header = (
        f"  {'scenario':<18} | {'tier':<6} | {'res':<5} | {'chk':<5} | {'judge':<6} | {'sim':<14} | cost"
    )
    print(header)
    print("  " + "-" * (len(header) - 2))

    for scenario in scenarios:
        scenario_entry = {
            "id": scenario.id,
            "description": scenario.description,
            "tiers": {},
        }
        for tier in tiers:
            if tier not in scenario.tiers:
                continue
            try:
                outcome = await run_scenario_on_tier(
                    scenario=scenario,
                    tier=tier,
                    llm_client=llm_client,
                    project_root=PROJECT_ROOT,
                    judge=judge,
                )
            except Exception as error:  # noqa: BLE001 - keep going on per-case failure
                outcome = TierRunOutcome(tier=tier, scenario_id=scenario.id, error=str(error))

            _print_row(scenario.id, outcome)
            scenario_entry["tiers"][tier] = outcome_to_dict(outcome)

            stats = summary[tier]
            stats["total"] += 1
            if outcome.overall_passed:
                stats["passed"] += 1
            stats["avg_llm_calls"] += outcome.llm_calls
            stats["avg_wall"] += outcome.wall_seconds
            if outcome.judge is not None:
                stats["_judge_total"] += outcome.judge.score
                stats["_judge_count"] += 1

        report["scenarios"].append(scenario_entry)

    for tier, stats in summary.items():
        count = stats["total"]
        if count > 0:
            stats["avg_llm_calls"] = stats["avg_llm_calls"] / count
            stats["avg_wall"] = stats["avg_wall"] / count
        if stats["_judge_count"] > 0:
            stats["avg_judge_score"] = stats["_judge_total"] / stats["_judge_count"]
        stats.pop("_judge_total", None)
        stats.pop("_judge_count", None)
    report["summary"] = summary

    _print_summary(summary)
    return report


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run SiFlowAgent evaluation harness.")
    parser.add_argument(
        "--scenario",
        action="append",
        default=None,
        help="Scenario id to run (repeatable). Default: run all scenarios in harness/scenarios/.",
    )
    parser.add_argument(
        "--tier",
        action="append",
        default=None,
        help="Tier to run (repeatable). One of: router, agent, multi. Default: all three.",
    )
    parser.add_argument(
        "--judge",
        action="store_true",
        help="Run the LLM-as-judge pass for scenarios that enable it.",
    )
    parser.add_argument(
        "--report",
        default=None,
        help="Optional path to write the JSON report (default: no file, stdout only).",
    )
    parser.add_argument(
        "--scenarios-dir",
        default=str(SCENARIOS_DIR),
        help="Directory that holds scenario YAML files.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    scenarios_dir = Path(args.scenarios_dir)
    scenarios = load_scenarios(scenarios_dir, only_ids=args.scenario)
    if not scenarios:
        print(f"No scenarios found in {scenarios_dir}")
        return 2
    tiers = args.tier or DEFAULT_TIERS

    report = asyncio.run(_run_all(scenarios, tiers, args.judge))

    if args.report:
        out_path = Path(args.report)
        if not out_path.is_absolute():
            out_path = PROJECT_ROOT / out_path
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nWrote report to {out_path}")

    any_failed = any(
        not entry.get("tiers", {}).get(tier, {}).get("overall_passed", False)
        for entry in report["scenarios"]
        for tier in tiers
        if tier in entry["tiers"]
    )
    return 1 if any_failed else 0


if __name__ == "__main__":
    sys.exit(main())
