# SiFlowAgent

SiFlowAgent is a modular AI agent framework for hardware and chip design workflows.
It implements the core pieces of an agent system — prompt engineering, context
management, skill / tool execution, multi-agent coordination, and evaluation
harnesses — and exercises them on RTL design tasks.

The framework currently covers structured spec parsing, RTL skeleton generation,
multi-step planning, self-review and revision, multi-agent actor / critic
coordination, scenario-driven evaluation, tiered memory, and behavioral verification
via real Verilog simulators.

For a phase-by-phase breakdown of what is built and what is planned, see
[ARCHITECTURE.md](ARCHITECTURE.md).

## Requirements

- Python 3.11+
- (Optional, for behavioral verification) `iverilog` or `verilator` available on `PATH`

Install dependencies:

```bash
python -m pip install -r requirements.txt
```

## Configuration

Copy the example file and fill in the required values:

```bash
cp .env.example .env
```

Required environment variables:

- `SIFLOW_LLM_PROVIDER`
- `SIFLOW_LLM_BASE_URL`
- `SIFLOW_LLM_API_KEY`
- `SIFLOW_LLM_MODEL`

The project currently defaults to a `messages_api` style backend when loading the
client from environment.

## Running the CLI

```bash
python main.py
```

### Smart commands (skill- and agent-aware)

| Command | Description |
|---------|-------------|
| normal chat input | single-shot router picks one skill and runs it |
| `/agent <goal>` | single-agent ReAct loop (think → act → observe) |
| `/multi <goal>` | actor / critic multi-agent coordinator |
| `/verify <tb.v> [top_module]` | run `VerifierAgent` (iverilog / Verilator) on the current RTL |
| `/memory` | show tiered memory summary (working / session / long-term) |

### Control-group commands (manual dispatch)

These bypass the router and call one skill directly. Kept as a baseline for comparing
human dispatch vs. agent dispatch on the same task.

| Command | Description |
|---------|-------------|
| `/spec` | paste a hardware spec manually |
| `/spec <path>` | summarize a spec file from disk |
| `/chat <message>` | force the plain chat skill |
| `/rtl` | print the top-level generated Verilog |
| `/rtl <file.v>` | save a single Verilog file |
| `/rtl <dir>` | save all generated Verilog files for a multi-module design |
| `skills` | list registered skills with their parameter schemas |

### Session and history

| Command | Description |
|---------|-------------|
| `/session save [id]` | save session (history + state + revise history) to `data/sessions/<id>.json` |
| `/session load <id>` | restore a previously saved session into the current context |
| `/session list` | list saved sessions with a one-line summary each |
| `/session info` | print a compact summary of the current session state |
| `/history` | show the revise-history timeline (every past RTL snapshot + current HEAD) |
| `/history rollback <N>` | restore RTL to revise iteration N, invalidate stale review / lint, branch off history |

### Misc

| Command | Description |
|---------|-------------|
| `clear` | clear the current in-memory session context |
| `exit` / `quit` | leave the CLI |

## Examples

Summarize a spec from file:

```text
/spec data/sample_spec.txt
```

Generate RTL and save it:

```text
/rtl data/generated_packet_counter.v
```

Generate a multi-module RTL directory:

```text
/spec data/sample_multi_module_spec.txt
/rtl rtl/
```

Run behavioral verification on the latest RTL:

```text
/verify harness/tb/counter_tb.v counter_tb
```

## Test harnesses

### Deterministic RTL regression

```bash
python harness/regression_rtl.py
```

Builds a fixed structured multi-module summary locally, generates
`system_top / controller / datapath / fifo / arbiter` Verilog files, writes them to
`data/harness_out/`, and checks for expected filenames and key RTL fragments.

### Scenario-driven evaluation

```bash
# every scenario across every tier, no judge
python harness/eval.py

# one scenario, single-agent tier only
python harness/eval.py --scenario counter_basic --tier agent

# enable LLM-as-judge and write the full report
python harness/eval.py --judge --report data/eval_report.json
```

The harness runs the same goal through every agent tier (`router`, `agent`, `multi`)
and produces a side-by-side cost / quality report that combines deterministic checks,
an optional LLM-as-judge score, and (when configured) real simulator pass / fail.
Exit code is non-zero when any scenario fails, suitable for CI.

See [ARCHITECTURE.md § Phase 7](ARCHITECTURE.md#phase-7--scenario-driven-evaluation-harness)
for the full design.

### cocotb tests

```bash
python harness/cocotb/run_counter.py
```

Builds and runs the cocotb counter test against `harness/cocotb/counter.v` using the
Icarus Verilog backend. Mirrors the three checks of the original Verilog testbench
(`reset_zero`, `increment_three`, `still_incrementing`). Build artifacts land in
`data/cocotb_build/` (gitignored). See [ARCHITECTURE.md § Phase 10](ARCHITECTURE.md#phase-10--cocotb-based-verification-10a-done-10bd-planned)
for the wider plan.

## Project layout

```text
SiFlowAgent/
├── ARCHITECTURE.md  # phase-by-phase architecture and roadmap
├── README.md        # this file
├── context/         # ContextManager facade over the tiered memory system
├── core/            # orchestrator, LLM client, memory tiers, agents, coordinator
├── data/            # sample specs, reference inputs, sessions, memory, sim_runs
├── harness/         # evaluation scripts, scenario YAMLs, testbenches
├── prompts/         # YAML system prompts
├── skills/          # skill definitions and registry (incl. rtl_sim)
├── .env.example     # example runtime configuration
├── main.py          # interactive CLI entrypoint
└── requirements.txt
```

## Security

- Do not commit your real `.env`
- Keep API keys local only
- `.env.example` is intentionally redacted and safe to share

## License

See [LICENSE](LICENSE).
