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
├── context/        # ContextManager facade over the tiered memory system
├── core/           # orchestrator, LLM client, memory tiers, agents, coordinator
├── data/           # sample specs, reference inputs, sessions, memory, sim_runs
├── harness/        # evaluation scripts, scenario YAMLs, testbenches
├── prompts/        # YAML system prompts
├── skills/         # skill definitions and registry (incl. rtl_sim)
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

- normal chat input — **single-shot routing (step 1)**: the router picks one skill and runs it
- `/agent <goal>` — **single-agent multi-step loop (step 2)**: one planner drives think-act-observe
- `/multi <goal>` — **actor-critic multi-agent coordinator (step 6B)**: two specialized LLM personas exchange structured messages over multiple rounds
- `/verify <testbench.v> [top_module]` — **behavioral verification (step 7)**: run VerifierAgent (iverilog/Verilator) on the current RTL
- `/memory` — show tiered memory summary (working / session / long-term namespaces)
- `/chat <message>` — force the plain chat skill and bypass the router
- `skills` — list registered skills with their parameter schemas (what the router/planner see)
- `clear` to clear the current in-memory session context
- `/spec` to paste a hardware spec manually (control group, hand-dispatched)
- `/spec <path>` to summarize a spec file from disk (control group)
- `/rtl` to print the top-level generated Verilog (control group)
- `/rtl <file.v>` to save a single Verilog file (control group)
- `/rtl <dir>` to save all generated Verilog files for a multi-module design (control group)
- `/session save [id]` — save the current session (history + state + revise history) to `data/sessions/<id>.json`
- `/session load <id>` — restore a previously saved session into the current context
- `/session list` — list saved sessions with a one-line summary each
- `/session info` — print a compact summary of the current session state
- `/history` — show the revise-history timeline (every past RTL snapshot + current HEAD)
- `/history rollback <N>` — restore the RTL to revise iteration N, invalidate stale review/lint, branch off history
- `exit` or `quit` to leave the CLI

The `/spec` and `/rtl` commands are kept on purpose as a control group so you can directly compare
"human dispatch" vs "single-shot agent dispatch" vs "multi-step agent loop" on the same task.

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

## Evaluation harness (agent step 7)

`harness/eval.py` is a scenario-driven evaluation harness that runs the **same
goal** through all three agent tiers (`router`, `agent`, `multi`) and produces
a side-by-side report. It is the foundation for A/B-testing agent changes:
before/after any prompt tweak, skill addition, or architecture change, run
`python harness/eval.py` and compare the numbers.

```bash
# run every scenario across every tier, no judge
python harness/eval.py

# run just one scenario, only the single-agent tier
python harness/eval.py --scenario counter_basic --tier agent

# enable the LLM-as-judge pass, write the full report
python harness/eval.py --judge --report data/eval_report.json
```

Core pieces:

- **`harness/scenarios/*.yaml`** — one file per test case. A scenario declares
  the goal, optional spec preload, which tiers to run, deterministic checks
  (lint bounds, `contains_all`/`contains_any`/`not_contains`, per-tier LLM
  budget, minimum modules), an optional LLM-as-judge block with its own
  criteria + pass threshold, and an optional `simulation:` block that runs
  the testbench via `rtl_sim` and turns pass/fail into a real behavioral check.
- **`harness/eval_scenarios.py`** — Pydantic `Scenario` model + YAML loader.
- **`harness/eval_runner.py`** — core runner logic:
  - `CountingLLMClient` wraps any LLM client and counts every `generate` call
    (plus rough request/response byte totals) so every scenario ships with a
    concrete cost number.
  - `run_scenario_on_tier` builds a fresh `Orchestrator` (fresh context) per
    scenario×tier, optionally preloads a spec summary, then runs the tier and
    grades the output.
  - Grading combines **deterministic checks** (always runs) with an **optional
    LLM-as-judge** pass (requires `--judge` flag and `judge.enabled: true` in
    the scenario).
- **`harness/eval.py`** — CLI that loops scenarios × tiers, prints a
  side-by-side table, and writes a JSON report on `--report`.

Example output shape:

```text
Eval run 20260418_190000  (scenarios=1, tiers=['router', 'agent', 'multi'], judge=True)

  scenario           | tier   | res   | chk   | judge  | cost
  ------------------ | ------ | ----- | ----- | ------ | ----
  counter_basic      | router | pass  | 9/9   | 0.85+  | llm=3 steps=1 wall=1.1s
  counter_basic      | agent  | pass  | 9/9   | 0.90+  | llm=4 steps=1 wall=1.4s
  counter_basic      | multi  | pass  | 9/9   | 0.90+  | llm=6 steps=1 wall=2.0s

Summary by tier:
  router : 1/1 passed (100%)  avg_llm=3.0  avg_wall=1.1s  avg_judge=0.85
  agent  : 1/1 passed (100%)  avg_llm=4.0  avg_wall=1.4s  avg_judge=0.90
  multi  : 1/1 passed (100%)  avg_llm=6.0  avg_wall=2.0s  avg_judge=0.90
```

This exposes the **cost / quality tradeoff** explicitly: router is cheapest
but can't handle goals that need multi-step reasoning; multi is the most
expensive but gives you an independent critic for free. Adding a new scenario
is one YAML file, and the exit code of `harness/eval.py` is non-zero when any
tier/scenario fails, so it plugs cleanly into CI.

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
- `rtl_lint` — deterministic local lint of generated RTL (no LLM call)
- `rtl_review` — LLM-based semantic review of generated RTL with structured issues and severities
- `rtl_revise` — LLM-based revision that applies review/lint feedback to produce fixed RTL
- `rtl_sim` — real Verilog simulation (iverilog preferred, Verilator fallback); the **only** behavioral signal in the stack
- `chat` — free-form conversation, used as the fallback route
- `router` — internal skill that picks the right downstream skill for a natural-language request (single-shot)
- `planner` — internal skill that drives the multi-step ReAct-style agent loop

### Skill routing (agent step 1)

Every skill now carries a `parameters_schema` describing what arguments it accepts.
The `RouterSkill` feeds the full skill catalog plus a minimal runtime-state snapshot
into the LLM and asks it to return a structured decision:

```json
{"skill": "spec_summary", "args": {"spec_path": "data/sample_spec.txt"}, "reasoning": "..."}
```

`Orchestrator.route_and_execute` then dispatches to the chosen skill and handles any
I/O side-effects (reading spec files, writing generated Verilog). The CLI prints the
router's decision before the result, so you can clearly see when the agent picks
`chat` vs. `spec_summary` vs. `verilog_template`.

### Agent loop (agent step 2)

`PlannerSkill` + `core/agent_loop.py` implement a classic ReAct-style loop:

1. **Think** — the planner sees the user goal, skill catalog, runtime state, and a
   scratchpad of all previous steps, then returns JSON:

   ```json
   {"thought": "...", "action": "spec_summary" | "verilog_template" | ... | "finish",
    "args": {...}, "final_answer": "..."}
   ```

2. **Act** — `Orchestrator.execute_action` runs the chosen skill. Any exception is
   caught and turned into an `observation` beginning with `ERROR`, so the planner can
   pivot on the next turn.
3. **Observe** — `Orchestrator.make_observation` compresses the skill result into a
   short factual string (not the full output), keeping the scratchpad small.
4. Repeat until the planner emits `action: "finish"` (with `final_answer`) or the
   loop hits `max_steps` (default 6).

Use `/agent <goal>` from the CLI to trigger it. Each step is printed live:

```
[step 1] (ok) action=spec_summary
  thought    : Need to summarize spec first.
  args       : {"spec_path": "data/sample_spec.txt"}
  observation: Produced spec summary. module_name=counter, interfaces=3, submodules=[none].
[step 2] (ok) action=verilog_template
  ...
[step 3] (finish) action=finish
  observation: Done. Summary produced and RTL saved to ...
```

### Self-reflection (agent step 3)

Two complementary review skills let the agent audit its own output before finishing:

- `rtl_lint` — deterministic, local, no LLM. Parses the most recent generated RTL with
  regex-based rules (module/endmodule balance, posedge-clocked always blocks without
  reset reference, empty always blocks, TODO placeholder count). Useful for fast
  structural sanity checks inside the agent loop and inside regression harnesses.
- `rtl_review` — LLM-based semantic review. Returns structured JSON with an
  `overall_quality` grade, a list of `issues` each carrying `severity`
  (`high`/`medium`/`low`), `category`, `location`, `description`, and `suggestion`,
  plus a list of `recommendations`. A markdown report is also rendered for CLI output.

Both skills read their input from the most recent `verilog_template` output stored in
context, or from a `verilog_code` argument if the planner chooses to pass one
explicitly. The planner typically runs them after `verilog_template` when the user's
goal mentions lint, review, validation, audit, or quality. They also run inside the
deterministic regression harness (`harness/regression_rtl.py`) so the lint never
silently drifts.

### Reflexion loop (agent step 4)

`rtl_revise` closes the reflexion loop so the agent can not only **find** issues
but also **fix** them:

1. `verilog_template` — deterministic skeleton from the structured spec
2. `rtl_review` / `rtl_lint` — issues collected into `last_rtl_review` / `last_rtl_lint`
3. `rtl_revise` — LLM reads both the current RTL (from `last_verilog_template`) and
   the aggregated issues, produces revised module files, and **replaces**
   `last_verilog_template` in context
4. `rtl_review` — run again on the revised RTL to verify the fix
5. `finish` — report which issues were addressed and which remain

The planner prompt enforces three soft guardrails to keep the loop sane:

- After `rtl_review` reports medium/high issues, consider `rtl_revise` then re-review
- Do not call `rtl_revise` more than 2 times per run
- Do not call `verilog_template` again after `rtl_revise` (that would discard fixes)

A dedicated `rtl_revise_count` is tracked in runtime state so the planner can see how
many revision cycles have already happened. Every revision returns both
`addressed_issues` and `unresolved_issues`, so the final answer can be honest about
what was actually fixed.

### Session persistence & revise history (agent step 5)

`core/session.py` exposes a `SessionStore` that serializes the `ContextManager`
history and runtime state (including `revise_history`) to
`data/sessions/<id>.json`. Loading reads the JSON back into a fresh context, so
later CLI runs can pick up exactly where a previous one left off.

Key additions in this step:

1. **Snapshot on revise.** Before `rtl_revise` overwrites `last_verilog_template`
   it appends the baseline code + the review/lint that triggered the revision to
   `revise_history`. Every revision becomes a replayable audit-trail entry with
   its cause attached.
2. **`rollback_revise(N)`.** Restores `last_verilog_template` to the Nth
   snapshot, truncates `revise_history` beyond N (true branching, not undo-log),
   resets `rtl_revise_count` to N, and invalidates `last_rtl_review` /
   `last_rtl_lint` because the RTL underneath just changed.
3. **Session file layout.**

   ```json
   {
     "session_id": "20260418_001530_a1b2",
     "created_at": "...",
     "updated_at": "...",
     "history": [{"role": "user", "content": "..."}, ...],
     "state": {
       "last_spec_summary": {...},
       "last_verilog_template": {...},
       "last_rtl_review": {...},
       "last_rtl_revise": {...},
       "revise_history": [
         {"iteration": 0, "created_at": "...", "verilog_code": "...",
          "modules": [...], "source_review": {...}, "source_lint": {...}}
       ],
       "rtl_revise_count": 1
     }
   }
   ```

4. **CLI surface.** `/session save|load|list|info` and `/history [rollback <N>]`
   expose the store and the history timeline directly. `data/sessions/` is
   gitignored so your workspace cache stays local.

### Multi-agent actor-critic (agent step 6B)

Where step 2's single `PlannerSkill` reasons about everything, step 6B splits
the reasoning across **two LLM personas with different system prompts and
temperatures**, connected by a Python-level `Coordinator`:

- **ActorAgent** (`core/agents.py`)
  - Persona: senior RTL engineer, temperature 0.3
  - Allowed skills: `spec_summary`, `verilog_template`, `rtl_revise`
  - Runs a small chain of skill calls per turn (capped by `max_substeps=4`)
    and hands off to the critic when the current RTL is ready for review
- **CriticAgent**
  - Persona: strict RTL reviewer, temperature 0.0
  - Allowed skills: `rtl_lint`, `rtl_review`
  - Always runs lint first, then review, then one LLM call to produce a
    structured verdict (`accept` or `revise` + priority issues)
- **Coordinator** (`core/coordinator.py`)
  - Not an LLM itself — it alternates actor and critic, appends each turn to a
    shared `AgentMessage` transcript, and stops when the critic accepts or when
    `max_rounds` is hit

Messages between agents are structured (`AgentMessage` Pydantic model). Each
turn carries a `payload` dict: actor attaches `sub_steps`, critic attaches
`verdict`, `priority_issues`, `lint_summary`, `review_summary`. The CLI
`/multi <goal>` streams messages live so you can watch the actor-critic
dialogue in real time.

Contrast with step 2:

| Layer | Step 2 `/agent` | Step 6B `/multi` |
|-------|-----------------|------------------|
| Reasoning brains | 1 (PlannerSkill) | 2 (Actor + Critic) |
| System prompts | 1 shared | 2 distinct personas |
| Temperatures | 1 shared | 2 (0.3 vs 0.0) |
| Skill access | All skills | Per-agent whitelist |
| Inter-turn data | Flat scratchpad | Structured `AgentMessage` transcript |
| Stop signal | Planner emits `finish` | Critic emits `accept` verdict |

### Tiered memory (agent step 7)

Up through step 6 every piece of per-run state lived on a single flat dict
inside `ContextManager`. Step 7 introduces an explicit three-tier memory
hierarchy in `core/memory.py`, with `ContextManager` becoming a thin facade
so every existing call site keeps working unchanged:

| Tier | Class | Scope | Persisted? | Typical contents |
|------|-------|-------|------------|------------------|
| working | `WorkingMemory` | one agent turn / one skill call | never | transient scratch values, in-flight parses |
| session | `SessionMemory` | one CLI run / one eval scenario | via `SessionStore` | message history, `last_spec_summary`, `last_verilog_template`, `revise_history`, `last_rtl_sim`, ... |
| long_term | `LongTermMemory` | across runs and sessions | JSON under `data/memory/` | `sim_history.json`, `patterns.json`, `lessons.json` |

Long-term memory is organized into named JSON stores. Two shapes are
supported:

- **dict-valued** (`put` / `load`): keyed records with automatic `updated_at`
  stamps — useful for named patterns and lessons.
- **list-valued** (`append` / `load` / `recent`): append-only logs — used by
  `rtl_sim` to record every simulator verdict so a later run can mine it.

The orchestrator auto-attaches a default `LongTermMemory.default_for(project_root)`
on startup, so no explicit wiring is needed. From the CLI, `/memory` prints a
quick summary:

```json
{
  "working_keys": 0,
  "session_messages": 12,
  "session_state_keys": 7,
  "long_term": {
    "sim_history": { "type": "list", "count": 4 }
  }
}
```

### Behavioral verification via VerifierAgent (agent step 7)

Until step 7, every "verification" signal in SiFlowAgent was ultimately text
based: lint regexes, review JSON from an LLM, or `contains_all` checks in
the eval harness. Step 7 closes that gap with `VerifierAgent` and the
`rtl_sim` skill — the **only** tool in the stack whose pass/fail is a real
execution result rather than a string match.

`skills/rtl_sim.py::RtlSimSkill`:

- picks `iverilog` first and falls back to `verilator --binary` via
  `shutil.which` (returns an explicit `no_tool` status when neither is
  installed, so the rest of the pipeline never silently skips verification)
- writes every module from `last_verilog_template` into a fresh tempdir
  under `data/sim_runs/` so concurrent runs do not collide
- compiles + executes the testbench, classifies the output by configurable
  pass/fail tokens (default `TEST_PASS` / `TEST_FAIL`) plus `PASS`/`FAIL`
  word counters
- stores the full verdict in session state under `last_rtl_sim`, appends a
  compact record to long-term `sim_history`, and returns a structured
  `RtlSimResult` with `compile_log` / `run_log` tails

`core/agents.py::VerifierAgent` wraps one `rtl_sim` call and emits a
structured `AgentMessage` of kind `verification` with one of six verdicts:
`verified`, `sim_failed`, `compile_error`, `no_tool`, `skipped`, `error`.

Three entry points:

- CLI: `/verify harness/tb/counter_tb.v counter_tb`
- Orchestrator: `await orchestrator.verify_rtl(testbench_path=..., top_module=...)`
- Eval harness: add a `simulation:` block to any scenario YAML

### Simulation-driven evaluation

`harness/eval_scenarios.py::ScenarioSimulation` adds an optional
`simulation:` block to each scenario:

```yaml
simulation:
  enabled: true
  testbench: harness/tb/counter_tb.v
  top_module: counter_tb
  tool: auto              # iverilog | verilator | auto
  timeout_seconds: 20
  pass_tokens: [TEST_PASS]
  fail_tokens: [TEST_FAIL]
  require_tool: true      # false => missing simulator = skipped (not fail)
```

When enabled, `harness/eval_runner.py` runs the simulator against the tier's
final RTL after all other checks and adds two deterministic checks:

- `sim.compile[iverilog]` — did the design even elaborate against the TB?
- `sim.behavior[iverilog]` — did the simulation emit a pass marker?

This is the "text-match → behavior" upgrade made explicit: a scenario
whose `contains_all: ["module counter", "always @"]` checks pass can still
fail on `sim.behavior` if the body is a TODO stub. The eval row now
includes a `sim` column, e.g.:

```text
  scenario           | tier   | res   | chk   | judge  | sim            | cost
  ------------------ | ------ | ----- | ----- | ------ | -------------- | ----
  counter_basic      | router | FAIL  | 7/8   | 0.35-  | fail-          | llm=3 steps=1 wall=1.1s
  counter_basic      | agent  | pass  | 8/8   | 0.88+  | pass+          | llm=7 steps=3 wall=3.9s
  counter_basic      | multi  | pass  | 8/8   | 0.90+  | pass+          | llm=11 steps=2 wall=5.3s
```

The router tier reliably fails `sim.behavior` on the stub, while the agent
and multi tiers only pass once the LLM actually implements the increment
— exactly the cost/quality tradeoff the eval harness is meant to expose.

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

- add a scenario that intentionally triggers revise-then-review and grades the loop
- add a "golden" RTL baseline per scenario so string-diff regressions are catchable
- swap actor and critic onto different LLM models (e.g., strong model for critic, faster model for actor)
- wire the VerifierAgent into the `/multi` coordinator as an optional third phase that runs after critic `accept`, with a retry budget for `sim_failed` verdicts
- add more behavioral testbenches (FIFO, arbiter, controller) so simulation-based verification covers the multi-module scenario too
- mine long-term `sim_history` to build a "lessons learned" store the planner can condition on
- stream/partial-result handling inside the agent loop
- swap the deterministic `verilog_template` for an LLM-backed generator that can itself iterate
- auto-save sessions on every agent loop run and expose a "latest" shortcut
- diff view between two revise iterations
- improve RTL skeleton filling beyond TODO placeholders
- infer submodule ports and top-level wiring more precisely

## Security note

- Do not commit your real `.env`
- Keep API keys local only
- `.env.example` is intentionally redacted and safe to share
