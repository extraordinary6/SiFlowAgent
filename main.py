from __future__ import annotations

import asyncio
from pathlib import Path

from context.manager import ContextManager
from core.llm_client import load_llm_client_from_env
from core.orchestrator import Orchestrator


async def main() -> None:
    project_root = Path(__file__).resolve().parent
    context_manager = ContextManager()

    llm_client = load_llm_client_from_env()

    orchestrator = Orchestrator(
        prompt_dir=project_root / "prompts",
        context_manager=context_manager,
        llm_client=llm_client,
    )

    if llm_client is None:
        result = await orchestrator.hello_siflow()
        print(result)
        print("LLM client not configured. Set SIFLOW_LLM_BASE_URL, SIFLOW_LLM_API_KEY, and SIFLOW_LLM_MODEL to enable real model calls.")
        return

    print("SiFlowAgent CLI ready. Type 'clear' to reset context, '/spec' or '/spec <path>' to summarize a spec, 'exit' or 'quit' to stop.")

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

        response = await orchestrator.chat(user_input)
        print(f"SiFlowAgent> {response}")


if __name__ == "__main__":
    asyncio.run(main())
