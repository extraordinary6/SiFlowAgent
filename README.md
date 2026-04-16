# SiFlowAgent

SiFlowAgent is a modular AI agent framework for hardware and chip design workflows. The project is focused on learning and building the core pieces of an agent system: prompt engineering, context management, skill/tool execution, and evaluation harnesses.

## Current status

The repository currently includes a working project skeleton with:

- `core/` orchestrator and LLM client abstractions
- `context/` conversation state management
- `skills/` base skill interface, registry, and example hardware skills
- `prompts/` YAML-based system prompts
- `harness/` reserved for evaluation and regression scripts
- `data/` sample input documents
- interactive CLI in `main.py`

Implemented features today:

- interactive CLI chat loop
- `.env`-based runtime configuration
- `clear` command to reset in-memory context
- `SpecSummarySkill` for summarizing hardware specs
- `/spec` command with paste mode and file mode
- `VerilogTemplateSkill` for RTL skeleton generation
- `/rtl` to generate Verilog from the latest structured spec summary
- `/rtl <file.v>` to save a single generated module
- `/rtl <dir>` to save multiple generated modules when the spec suggests submodules
- structured spec output internally via Pydantic, with markdown rendering for CLI display
- smarter RTL skeleton inference for likely sequential vs combinational outputs
- top-level plus submodule skeleton generation for multi-module specs
- specialized controller/datapath/fifo/arbiter RTL templates
- local multi-module sample spec for regression-style testing
- harness regression script for deterministic RTL generation checks

## Project layout

```text
SiFlowAgent/
├── context/        # conversation history and runtime state
├── core/           # orchestrator and LLM client layer
├── data/           # sample specs and reference inputs
├── harness/        # evaluation scripts and test harnesses
├── prompts/        # YAML system prompts
├── skills/         # skill definitions and registry
├── .env.example    # example runtime configuration
├── main.py         # interactive CLI entrypoint
└── requirements.txt
```

## Requirements

- Python: use your local Python 3.11+ environment
- Install dependencies:

```bash
python -m pip install -r requirements.txt
```

## Configuration

Copy the example file and fill in your own values:

```bash
cp .env.example .env
```

Required environment variables:

- `SIFLOW_LLM_PROVIDER`
- `SIFLOW_LLM_BASE_URL`
- `SIFLOW_LLM_API_KEY`
- `SIFLOW_LLM_MODEL`

The project currently defaults to a `messages_api` style backend when loading the client from environment.

## Running the CLI

```bash
python main.py
```

You can then use:

- normal chat input for conversational interaction
- `clear` to clear the current in-memory session context
- `/spec` to paste a hardware spec manually
- `/spec <path>` to summarize a spec file from disk
- `/rtl` to print the top-level generated Verilog
- `/rtl <file.v>` to save a single Verilog file
- `/rtl <dir>` to save all generated Verilog files for a multi-module design
- `exit` or `quit` to leave the CLI

## Example: summarize a spec from file

```text
/spec data/sample_spec.txt
```

## Example: generate RTL and save it

```text
/rtl data/generated_packet_counter.v
```

## Example: generate a multi-module RTL directory

```text
/spec data/sample_multi_module_spec.txt
/rtl rtl/
```

## Harness regression test

Run the deterministic RTL regression harness:

```bash
python harness/regression_rtl.py
```

This script:

- builds a fixed structured multi-module summary locally
- generates `system_top/controller/datapath/fifo/arbiter` Verilog files
- writes them to `data/harness_out/`
- checks for expected filenames and key RTL fragments

## Architecture overview

### Orchestrator

`core/orchestrator.py` is the coordination layer. It:

- loads prompts
- manages the current task flow
- dispatches registered skills
- routes chat requests to the configured LLM client

### Context manager

`context/manager.py` keeps short-term conversation state in memory:

- message history
- runtime state values such as the last task and last skill output

### Skills and registry

The skill system uses a registry pattern:

- `BaseSkill` defines the shared contract
- `SkillRegistry` stores available skills by name
- the orchestrator calls skills through the registry rather than directly embedding task logic

Current skills:

- `hello_siflow`
- `spec_summary`
- `verilog_template`

### Structured spec output

`SpecSummarySkill` now produces a structured internal result using Pydantic models, including:

- module name
- interfaces
- behavior summary
- timing/control notes
- constraints
- open questions
- inferred submodules for multi-module specs
- high-level interconnect notes

That structured result is then rendered into markdown for CLI output.

### RTL skeleton generation

`VerilogTemplateSkill` consumes `SpecSummaryResult` and builds Verilog by:

- normalizing port widths into standard Verilog ranges
- inferring likely sequential outputs from timing/behavior text
- inferring likely combinational outputs when descriptions indicate decode/select-style logic
- keeping one output signal per `always` block
- generating a top module plus child module stubs when `submodules` are present
- generating specialized templates for controller/datapath/fifo/arbiter blocks
- producing top-level wire declarations and `.port(signal)` instance skeletons

## Next steps

Suggested next milestones:

- route normal chat into skill selection automatically
- add more regression cases in `harness/`
- persist sessions beyond a single CLI run
- improve RTL skeleton filling beyond TODO placeholders
- infer submodule ports and top-level wiring more precisely

## Security note

- Do not commit your real `.env`
- Keep API keys local only
- `.env.example` is intentionally redacted and safe to share
