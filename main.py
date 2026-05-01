from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from context.manager import ContextManager
from core.agent_loop import AgentStep
from core.agents import AgentMessage
from core.llm_client import load_llm_client_from_env
from core.orchestrator import Orchestrator


def _print_router_trace(decision: dict) -> None:
    print("[router]")
    print(f"  skill    : {decision.get('skill')}")
    print(f"  args     : {json.dumps(decision.get('args', {}), ensure_ascii=False)}")
    print(f"  reasoning: {decision.get('reasoning', '')}")


def _print_routed_result(skill: str, result) -> None:
    if skill == "spec_summary" and isinstance(result, dict):
        print(f"SiFlowAgent>\n{result.get('markdown_summary', '')}")
        return
    if skill == "verilog_template" and isinstance(result, dict):
        print(f"SiFlowAgent>\n{result.get('verilog_code', '')}")
        modules = result.get("modules") or []
        if len(modules) > 1:
            print("SiFlowAgent> Additional submodule files ready:")
            for name in modules[1:]:
                print(f"  {name}")
        saved = result.get("saved") or []
        if saved:
            print("SiFlowAgent> Saved files:")
            for path in saved:
                print(f"  {path}")
        return
    if skill == "rtl_review" and isinstance(result, dict):
        print(f"SiFlowAgent>\n{result.get('markdown_report', '')}")
        return
    if skill == "rtl_lint" and isinstance(result, dict):
        print(
            f"SiFlowAgent> Lint: {result.get('findings_count', 0)} findings "
            f"(todos={result.get('todo_count', 0)}, "
            f"reset_missing={result.get('reset_missing_count', 0)}, "
            f"empty_always={result.get('empty_always_count', 0)})"
        )
        for finding in result.get("findings") or []:
            print(f"  - [{finding.get('severity')}] {finding.get('rule')}: {finding.get('message')}")
        return
    if skill == "rtl_revise" and isinstance(result, dict):
        modules = result.get("modules") or []
        print(
            f"SiFlowAgent> Revise #{result.get('revise_iteration', '?')}: "
            f"modules={modules}, addressed={result.get('addressed_issues_count', 0)}, "
            f"unresolved={result.get('unresolved_issues_count', 0)}"
        )
        if result.get("changes_summary"):
            print(f"  changes : {result['changes_summary']}")
        for issue in result.get("addressed_issues") or []:
            print(f"  fixed   - {issue}")
        for issue in result.get("unresolved_issues") or []:
            print(f"  pending - {issue}")
        for path in result.get("saved") or []:
            print(f"  saved   - {path}")
        return
    print(f"SiFlowAgent> {result}")


def _print_agent_step(step: AgentStep) -> None:
    marker = "finish" if step.action == "finish" else ("ok" if step.ok else "err")
    print(f"[step {step.index}] ({marker}) action={step.action}")
    if step.thought:
        print(f"  thought    : {step.thought}")
    if step.args:
        print(f"  args       : {json.dumps(step.args, ensure_ascii=False)}")
    if step.observation:
        obs = step.observation
        if len(obs) > 500:
            obs = obs[:500] + "..."
        print(f"  observation: {obs}")


def _print_agent_message(message: AgentMessage) -> None:
    sender = message.sender
    recipient = message.recipient
    round_tag = f"r{message.round}"
    print(f"[{round_tag}] {sender} -> {recipient} ({message.kind}) {message.summary}")
    if message.content:
        text = message.content if len(message.content) <= 400 else message.content[:400] + "..."
        print(f"    content: {text}")
    if message.sender == "actor":
        for sub in message.payload.get("sub_steps") or []:
            marker = "ok" if sub.get("ok") else "err"
            print(
                f"    sub -> ({marker}) skill={sub.get('skill')} "
                f"args={json.dumps(sub.get('args') or {}, ensure_ascii=False)}"
            )
            obs = sub.get("observation") or ""
            if obs:
                print(f"        observation: {obs[:260]}")
    elif message.sender == "critic":
        payload = message.payload or {}
        verdict = payload.get("verdict")
        priority = payload.get("priority_issues") or []
        print(f"    verdict : {verdict}")
        if priority:
            print("    priority_issues:")
            for issue in priority:
                print(f"      - {issue}")
        lint = payload.get("lint_summary") or {}
        review = payload.get("review_summary") or {}
        if lint:
            print(f"    lint    : {json.dumps(lint, ensure_ascii=False)}")
        if review:
            print(f"    review  : {json.dumps(review, ensure_ascii=False)}")
    elif message.sender == "verifier":
        payload = message.payload or {}
        print(f"    verdict : {payload.get('verdict')}")
        print(
            f"    status  : {payload.get('status')} tool={payload.get('tool')} "
            f"top={payload.get('top_module')} dur={payload.get('duration_seconds')}s"
        )
        print(
            f"    counts  : PASS={payload.get('assertions_passed', 0)}  "
            f"FAIL={payload.get('assertions_failed', 0)}  "
            f"compile_ok={payload.get('compile_ok')}  run_ok={payload.get('run_ok')}"
        )


async def main() -> None:
    project_root = Path(__file__).resolve().parent
    context_manager = ContextManager()

    llm_client = load_llm_client_from_env()

    orchestrator = Orchestrator(
        prompt_dir=project_root / "prompts",
        context_manager=context_manager,
        llm_client=llm_client,
        project_root=project_root,
    )

    if llm_client is None:
        result = await orchestrator.hello_siflow()
        print(result)
        print("LLM client not configured. Set SIFLOW_LLM_BASE_URL, SIFLOW_LLM_API_KEY, and SIFLOW_LLM_MODEL to enable real model calls.")
        return

    print(
        "SiFlowAgent CLI ready.\n"
        "  natural language       -> single-shot skill router (step 1)\n"
        "  '/agent <goal>'        -> single-agent multi-step loop (step 2)\n"
        "  '/multi <goal>'        -> actor-critic multi-agent coordinator (step 6B)\n"
        "  '/verify <tb.v> [top]' -> run VerifierAgent (iverilog/verilator) on current RTL\n"
        "  '/verify cocotb <test_module> <hdl_top> [test_dir]' -> run cocotb-backed verification\n"
        "  '/memory'              -> show tiered memory summary (working/session/long_term)\n"
        "  '/chat <msg>'          -> force plain chat skill\n"
        "  '/spec' | '/spec <path>'       -> manual spec summary (control group)\n"
        "  '/rtl'  | '/rtl <file.v|dir>'  -> manual RTL generation (control group)\n"
        "  '/session save [id]'   -> save current session to data/sessions/<id>.json\n"
        "  '/session load <id>'   -> restore a saved session (replaces current context)\n"
        "  '/session list'        -> list saved sessions\n"
        "  '/session info'        -> show current session summary\n"
        "  '/history'             -> show revise history timeline\n"
        "  '/history rollback <N>'-> restore RTL to revise iteration N (drops later ones)\n"
        "  'skills'               -> list available skills and schemas\n"
        "  'clear'                -> reset context\n"
        "  'exit' | 'quit'        -> leave"
    )

    while True:
        user_input = input("You> ").strip()
        if not user_input:
            continue
        if user_input.lower() in {"exit", "quit"}:
            print("Bye.")
            break
        if user_input.lower() == "clear":
            context_manager.clear()
            print("Context cleared.")
            continue
        if user_input.lower() == "skills":
            catalog = [
                entry
                for entry in orchestrator.skill_registry.list_skills()
                if entry["name"] not in {"router", "planner"}
            ]
            print(json.dumps(catalog, ensure_ascii=False, indent=2))
            continue
        if user_input.startswith("/session"):
            parts = user_input.split()
            sub = parts[1] if len(parts) >= 2 else "info"
            try:
                if sub == "save":
                    target_id = parts[2] if len(parts) >= 3 else None
                    saved_id = orchestrator.save_session(target_id)
                    path = orchestrator.session_store.path_for(saved_id)
                    print(f"SiFlowAgent> Saved session '{saved_id}' -> {path}")
                elif sub == "load":
                    if len(parts) < 3:
                        print("Usage: /session load <id>")
                        continue
                    orchestrator.load_session(parts[2])
                    info = orchestrator.session_info()
                    print(f"SiFlowAgent> Loaded session '{parts[2]}'. info={json.dumps(info, ensure_ascii=False)}")
                elif sub == "list":
                    rows = orchestrator.list_sessions()
                    if not rows:
                        print("SiFlowAgent> No saved sessions.")
                    else:
                        for row in rows:
                            print(
                                f"  {row['session_id']}  updated={row['updated_at']}  "
                                f"spec={row['has_spec_summary']}  rtl={row['has_verilog_template']}  "
                                f"review={row['has_rtl_review']}  revise_count={row['rtl_revise_count']}  "
                                f"history_len={row['revise_history_len']}  last_skill={row['last_skill']}"
                            )
                elif sub == "info":
                    info = orchestrator.session_info()
                    print(json.dumps(info, ensure_ascii=False, indent=2))
                else:
                    print("Usage: /session save [id] | load <id> | list | info")
            except Exception as error:
                print(f"SiFlowAgent> session error: {error}")
            continue
        if user_input.startswith("/history"):
            parts = user_input.split()
            sub = parts[1] if len(parts) >= 2 else "show"
            try:
                if sub == "rollback":
                    if len(parts) < 3:
                        print("Usage: /history rollback <iteration>")
                        continue
                    iteration = int(parts[2])
                    result = orchestrator.rollback_revise(iteration)
                    print(f"SiFlowAgent> Rolled back to iteration {iteration}. {result}")
                else:
                    history = orchestrator.get_revise_history()
                    if not history:
                        print("SiFlowAgent> revise history is empty.")
                    else:
                        for entry in history:
                            marker = "* current" if entry.get("current") else ""
                            print(
                                f"  [{entry['iteration']}] created_at={entry['created_at']}  "
                                f"files={entry['module_files']}  "
                                f"triggered_by(review_issues={entry['source_review_issues']}, "
                                f"lint_findings={entry['source_lint_findings']})  {marker}"
                            )
                            preview = (entry.get("verilog_preview") or "").replace("\n", " ")
                            if preview:
                                print(f"       preview: {preview[:120]}...")
            except Exception as error:
                print(f"SiFlowAgent> history error: {error}")
            continue
        if user_input.startswith("/agent"):
            parts = user_input.split(maxsplit=1)
            if len(parts) != 2 or not parts[1].strip():
                print("Usage: /agent <goal>")
                continue
            goal = parts[1].strip()
            try:
                result = await orchestrator.run_agent_loop(
                    goal=goal,
                    max_steps=6,
                    on_step=_print_agent_step,
                )
            except Exception as error:
                print(f"SiFlowAgent> Agent loop error: {error}")
                continue
            print(f"[agent] stopped_reason={result.stopped_reason} completed={result.completed}")
            print(f"SiFlowAgent> {result.final_answer or '(no final answer produced)'}")
            continue
        if user_input.startswith("/multi"):
            parts = user_input.split(maxsplit=1)
            if len(parts) != 2 or not parts[1].strip():
                print("Usage: /multi <goal>")
                continue
            goal = parts[1].strip()
            try:
                result = await orchestrator.run_multi_agent(
                    goal=goal,
                    max_rounds=3,
                    on_message=_print_agent_message,
                )
            except Exception as error:
                print(f"SiFlowAgent> Multi-agent error: {error}")
                continue
            print(
                f"[multi] accepted={result.accepted} rounds_used={result.rounds_used} "
                f"stopped_reason={result.stopped_reason}"
            )
            continue
        if user_input.startswith("/verify"):
            parts = user_input.split()
            if len(parts) < 2:
                print(
                    "Usage:\n"
                    "  /verify <testbench.v> [top_module]                    # rtl_sim backend\n"
                    "  /verify cocotb <test_module> <hdl_toplevel> [test_dir]  # cocotb backend"
                )
                continue
            try:
                if parts[1] == "cocotb":
                    if len(parts) < 4:
                        print("Usage: /verify cocotb <test_module> <hdl_toplevel> [test_dir]")
                        continue
                    cocotb_kwargs: dict[str, Any] = {
                        "cocotb_test_module": parts[2],
                        "cocotb_hdl_toplevel": parts[3],
                    }
                    if len(parts) >= 5:
                        cocotb_kwargs["cocotb_test_dir"] = parts[4]
                    message = await orchestrator.verify_rtl(
                        on_message=_print_agent_message,
                        **cocotb_kwargs,
                    )
                else:
                    testbench = parts[1]
                    top_module = parts[2] if len(parts) >= 3 else None
                    message = await orchestrator.verify_rtl(
                        testbench_path=testbench,
                        top_module=top_module,
                        on_message=_print_agent_message,
                    )
            except Exception as error:
                print(f"SiFlowAgent> Verify error: {error}")
                continue
            payload = message.payload or {}
            backend = payload.get("backend", "rtl_sim")
            if backend == "cocotb":
                total = (
                    payload.get("passes", 0)
                    + payload.get("fails", 0)
                    + payload.get("skipped", 0)
                )
                print(
                    f"[verify] backend=cocotb verdict={payload.get('verdict')} "
                    f"status={payload.get('status')} tool={payload.get('tool')} "
                    f"cases={payload.get('passes', 0)}/{total} pass "
                    f"({payload.get('fails', 0)} fail, {payload.get('skipped', 0)} skip) "
                    f"duration={payload.get('duration_seconds', 0)}s"
                )
                for ev in (payload.get("event_log") or [])[:10]:
                    print(f"  {ev}")
            else:
                print(
                    f"[verify] backend=rtl_sim verdict={payload.get('verdict')} "
                    f"status={payload.get('status')} tool={payload.get('tool')} "
                    f"assertions={payload.get('assertions_passed', 0)}/"
                    f"{payload.get('assertions_passed', 0) + payload.get('assertions_failed', 0)} "
                    f"duration={payload.get('duration_seconds', 0)}s"
                )
                if payload.get("run_log_tail"):
                    print("--- run log tail ---")
                    print(payload["run_log_tail"])
            continue
        if user_input.lower() == "/memory":
            summary = context_manager.memory_summary()
            print(json.dumps(summary, ensure_ascii=False, indent=2))
            continue
        if user_input.startswith("/chat"):
            parts = user_input.split(maxsplit=1)
            if len(parts) != 2 or not parts[1].strip():
                print("Usage: /chat <message>")
                continue
            response = await orchestrator.chat(parts[1].strip())
            print(f"SiFlowAgent> {response}")
            continue
        if user_input.startswith("/rtl"):
            parts = user_input.split(maxsplit=1)
            try:
                template_result = await orchestrator.build_verilog_template()
            except RuntimeError as error:
                print(f"SiFlowAgent> {error}")
                continue

            if len(parts) == 2:
                output_path = Path(parts[1]).expanduser()
                if not output_path.is_absolute():
                    output_path = project_root / output_path

                if output_path.suffix.lower() == ".v":
                    output_path.parent.mkdir(parents=True, exist_ok=True)
                    output_path.write_text(template_result.verilog_code + "\n", encoding="utf-8")
                    print(f"SiFlowAgent> Verilog saved to {output_path}")
                else:
                    output_path.mkdir(parents=True, exist_ok=True)
                    written_files: list[str] = []
                    for module in template_result.modules:
                        module_path = output_path / module.file_name
                        module_path.write_text(module.verilog_code + "\n", encoding="utf-8")
                        written_files.append(str(module_path))
                    print("SiFlowAgent> Generated modules:")
                    for file_name in written_files:
                        print(file_name)
            else:
                print(f"SiFlowAgent>\n{template_result.verilog_code}")
                if len(template_result.modules) > 1:
                    print("SiFlowAgent> Additional submodule files ready:")
                    for module in template_result.modules[1:]:
                        print(module.file_name)
            continue
        if user_input.startswith("/spec"):
            parts = user_input.split(maxsplit=1)
            if len(parts) == 2:
                spec_path = Path(parts[1]).expanduser()
                if not spec_path.is_absolute():
                    spec_path = project_root / spec_path
                if not spec_path.exists() or not spec_path.is_file():
                    print(f"Spec file not found: {spec_path}")
                    continue
                spec_text = spec_path.read_text(encoding="utf-8")
            else:
                print("Paste spec text. End with a single line containing END.")
                lines: list[str] = []
                while True:
                    line = input()
                    if line.strip() == "END":
                        break
                    lines.append(line)
                spec_text = "\n".join(lines).strip()

            if not spec_text:
                print("No spec text provided.")
                continue
            response = await orchestrator.summarize_spec(spec_text)
            print(f"SiFlowAgent> {response}")
            continue

        try:
            routed = await orchestrator.route_and_execute(user_input)
        except Exception as error:
            print(f"SiFlowAgent> Router error: {error}")
            continue

        decision = routed["decision"]
        _print_router_trace(decision)
        _print_routed_result(decision.get("skill", ""), routed["result"])


if __name__ == "__main__":
    asyncio.run(main())
