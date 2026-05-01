# SiFlowAgent Architecture & Roadmap

This document tracks the architectural evolution of SiFlowAgent as a serial sequence of
phases. Each phase captures one self-contained capability that builds on the previous
phases. Phases marked **(planned)** describe future work; everything else is
implemented and lives on `main`.

For installation, configuration, and CLI usage, see [README.md](README.md).

## Mental model

SiFlowAgent is structured as concentric layers around a small set of LLM-driven skills:

- **Skills** — single-call units that wrap one LLM invocation or one deterministic
  transform.
- **Agents** — wrappers that combine one or more skill calls under a persona,
  temperature, and message contract.
- **Orchestration** — the `Orchestrator` registers skills, builds agents, manages
  context, and exposes higher-level entry points.
- **Context** — the `ContextManager` facade over a three-tier memory hierarchy
  (working / session / long-term).
- **Harness** — scenario-driven evaluation (`harness/eval.py`) and deterministic
  regression (`harness/regression_rtl.py`).

### Skill catalog

| Skill | Introduced in | Type |
|-------|---------------|------|
| `hello_siflow` | Phase 0 | demo / smoke test |
| `spec_summary` | Phase 0 | structured spec parsing |
| `verilog_template` | Phase 0 | deterministic RTL skeleton |
| `chat` | Phase 0 | LLM fallback |
| `router` | Phase 1 | LLM-driven single-shot dispatch |
| `planner` | Phase 2 | ReAct planner |
| `rtl_lint` | Phase 3 | deterministic regex lint |
| `rtl_review` | Phase 3 | LLM semantic review |
| `rtl_revise` | Phase 4 | LLM revision |
| `rtl_sim` | Phase 9 | iverilog / Verilator behavioral simulation |
| `cocotb_sim` | Phase 10B | cocotb-driven Python testbench simulation |
| `probe_inject` | Phase 10D | runs an agent-generated cocotb probe coroutine and returns a typed event log |

### Agent catalog

| Agent | Introduced in | Persona |
|-------|---------------|---------|
| `ActorAgent` | Phase 6 | senior RTL engineer (T=0.3) |
| `CriticAgent` | Phase 6 | strict RTL reviewer (T=0.0) |
| `VerifierAgent` | Phase 9 | simulator runner (T=0.0) |

### Reasoning / thinking model compatibility

`LLMRequest` exposes a three-state thinking control. `core/llm_client.py::_resolve_thinking_mode`
reduces the request to one of: `disabled`, `budget=N`, or `default` (let the backend decide).

| Field | Effect | Backend wiring |
|-------|--------|----------------|
| `disable_thinking=True` | Suppress reasoning entirely | OpenAI: `extra_body={reasoning_effort: "minimal"}`. Messages API: `thinking={type:"disabled"}` |
| `thinking_budget=N` (N>0) | Opt in to extended reasoning | OpenAI: `extra_body={reasoning_effort: <bucketed>}` (low <1024, medium <4096, high). Messages API: `thinking={type:"enabled", budget_tokens:N}`, with `max_tokens` auto-bumped by N so reasoning does not starve the answer |
| neither set | Backend default | no thinking-related fields are added to the request |

`max_tokens` always describes the **answer** budget. Thinking budget is added on top by
the client (Messages API only — OpenAI's `reasoning_effort` is a tier, so token-precise
control is not available there).

If both `disable_thinking=True` and `thinking_budget>0` are set, `thinking_budget` wins
and a warning is logged once. They are documented as mutually exclusive.

All five JSON-producing skills (`router`, `planner`, `spec_summary`, `rtl_review`,
`rtl_revise`) opt in to `disable_thinking=True` because their outputs are short
structured JSON; reasoning would only burn tokens. `chat` leaves the field default
so free-form responses can still benefit from reasoning. Future skills that need
extended reasoning (deeper code review, multi-step bug analysis) can pass
`thinking_budget=N` to opt back in.

End-to-end verified against GLM-5.1: in `disabled` mode the response is a single
text block (~200 output tokens for a small JSON); in `budget=1024` mode the response
contains both a `type:"thinking"` block and a `type:"text"` block, the client takes
the text block and discards the thinking trace.

`MessagesAPIClient` additionally falls back to reading `type:"thinking"` blocks if a
text block is missing — defensive code that lets degenerate responses surface
diagnostically instead of crashing on an empty payload.

A second class of failure — JSON inside markdown fences — is handled by per-skill
`_strip_json_fences` helpers (one copy in each of the five JSON skills). Folding
these into a shared utility is on the [Backlog](#backlog-unscheduled).

---

## Phase 0 — foundation

Establishes the building blocks every later phase reuses.

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

(In Phase 8 this is rewired as a facade over a tiered memory system, but the public
API remains stable.)

### Skills and registry

The skill system uses a registry pattern:

- `BaseSkill` defines the shared contract
- `SkillRegistry` stores available skills by name
- the orchestrator calls skills through the registry rather than directly embedding
  task logic

### Structured spec output

`SpecSummarySkill` produces a structured internal result using Pydantic models,
including:

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

---

## Phase 1 — single-shot routing

Every skill carries a `parameters_schema` describing what arguments it accepts. The
`RouterSkill` feeds the full skill catalog plus a minimal runtime-state snapshot into
the LLM and asks it to return a structured decision:

```json
{"skill": "spec_summary", "args": {"spec_path": "data/sample_spec.txt"}, "reasoning": "..."}
```

`Orchestrator.route_and_execute` then dispatches to the chosen skill and handles any
I/O side-effects (reading spec files, writing generated Verilog). The CLI prints the
router's decision before the result, making the routing choice (for example `chat` vs.
`spec_summary` vs. `verilog_template`) visible in the trace.

CLI entry point: normal chat input.

---

## Phase 2 — single-agent ReAct loop

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
4. Repeat until the planner emits `action: "finish"` (with `final_answer`) or the loop
   hits `max_steps` (default 6).

CLI entry point: `/agent <goal>`. Each step is printed live:

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

---

## Phase 3 — self-reflection

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

---

## Phase 4 — reflexion loop

`rtl_revise` closes the reflexion loop so the agent can not only **find** issues but
also **fix** them:

1. `verilog_template` — deterministic skeleton from the structured spec
2. `rtl_review` / `rtl_lint` — issues collected into `last_rtl_review` / `last_rtl_lint`
3. `rtl_revise` — LLM reads both the current RTL (from `last_verilog_template`) and the
   aggregated issues, produces revised module files, and **replaces**
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

---

## Phase 5 — session persistence & revise history

`core/session.py` exposes a `SessionStore` that serializes the `ContextManager` history
and runtime state (including `revise_history`) to `data/sessions/<id>.json`. Loading
reads the JSON back into a fresh context, so later CLI runs can pick up exactly where
a previous one left off.

Key additions:

1. **Snapshot on revise.** Before `rtl_revise` overwrites `last_verilog_template`, it
   appends the baseline code + the review/lint that triggered the revision to
   `revise_history`. Every revision becomes a replayable audit-trail entry with its
   cause attached.
2. **`rollback_revise(N)`.** Restores `last_verilog_template` to the Nth snapshot,
   truncates `revise_history` beyond N (true branching, not undo-log), resets
   `rtl_revise_count` to N, and invalidates `last_rtl_review` / `last_rtl_lint`
   because the RTL underneath just changed.
3. **Session file layout.**

   ```json
   {
     "session_id": "<session_id>",
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
   expose the store and the history timeline directly. `data/sessions/` is gitignored
   so the workspace cache stays local.

---

## Phase 6 — multi-agent actor / critic

Where Phase 2's single `PlannerSkill` reasons about everything, Phase 6 splits the
reasoning across **two LLM personas with different system prompts and temperatures**,
connected by a Python-level `Coordinator`:

- **ActorAgent** (`core/agents.py`)
  - Persona: senior RTL engineer, temperature 0.3
  - Allowed skills: `spec_summary`, `verilog_template`, `rtl_revise`
  - Runs a small chain of skill calls per turn (capped by `max_substeps=4`) and hands
    off to the critic when the current RTL is ready for review
- **CriticAgent**
  - Persona: strict RTL reviewer, temperature 0.0
  - Allowed skills: `rtl_lint`, `rtl_review`
  - Always runs lint first, then review, then one LLM call to produce a structured
    verdict (`accept` or `revise` + priority issues)
- **Coordinator** (`core/coordinator.py`)
  - Not an LLM itself — it alternates actor and critic, appends each turn to a shared
    `AgentMessage` transcript, and stops when the critic accepts or when `max_rounds`
    is hit

Messages between agents are structured (`AgentMessage` Pydantic model). Each turn
carries a `payload` dict: actor attaches `sub_steps`, critic attaches `verdict`,
`priority_issues`, `lint_summary`, `review_summary`. The CLI `/multi <goal>` streams
messages live, making the actor / critic dialogue observable in real time.

Contrast with Phase 2:

| Layer | Phase 2 `/agent` | Phase 6 `/multi` |
|-------|------------------|------------------|
| Reasoning brains | 1 (PlannerSkill) | 2 (Actor + Critic) |
| System prompts | 1 shared | 2 distinct personas |
| Temperatures | 1 shared | 2 (0.3 vs 0.0) |
| Skill access | All skills | Per-agent whitelist |
| Inter-turn data | Flat scratchpad | Structured `AgentMessage` transcript |
| Stop signal | Planner emits `finish` | Critic emits `accept` verdict |

---

## Phase 7 — scenario-driven evaluation harness

`harness/eval.py` is a scenario-driven evaluation harness that runs the **same goal**
through all three agent tiers (`router`, `agent`, `multi`) and produces a side-by-side
report. It forms the foundation for A/B testing agent changes: before and after any
prompt tweak, skill addition, or architecture change, running `python harness/eval.py`
yields a comparable set of numbers.

```bash
# every scenario across every tier, no judge
python harness/eval.py

# one scenario, single-agent tier only
python harness/eval.py --scenario counter_basic --tier agent

# enable LLM-as-judge and write the full report
python harness/eval.py --judge --report data/eval_report.json
```

Core pieces:

- **`harness/scenarios/*.yaml`** — one file per test case. A scenario declares the
  goal, optional spec preload, which tiers to run, deterministic checks (lint bounds,
  `contains_all`/`contains_any`/`not_contains`, per-tier LLM budget, minimum modules),
  an optional LLM-as-judge block with its own criteria + pass threshold, and an
  optional `simulation:` block (introduced in Phase 9) that runs the testbench via
  `rtl_sim` and turns pass/fail into a real behavioral check.
- **`harness/eval_scenarios.py`** — Pydantic `Scenario` model + YAML loader.
- **`harness/eval_runner.py`** — core runner logic:
  - `CountingLLMClient` wraps any LLM client and counts every `generate` call (plus
    rough request/response byte totals) so every scenario is accompanied by a concrete
    cost number.
  - `run_scenario_on_tier` builds a fresh `Orchestrator` (fresh context) per
    scenario × tier, optionally preloads a spec summary, then runs the tier and grades
    the output.
  - Grading combines **deterministic checks** (always runs) with an **optional
    LLM-as-judge** pass (requires `--judge` flag and `judge.enabled: true` in the
    scenario).
- **`harness/eval.py`** — CLI that loops scenarios × tiers, prints a side-by-side
  table, and writes a JSON report on `--report`.

This surfaces the **cost / quality tradeoff** explicitly: the router tier is cheapest
but cannot handle goals that need multi-step reasoning; the multi tier is the most
expensive but provides an independent critic. Adding a new scenario is one YAML file,
and the exit code of `harness/eval.py` is non-zero when any tier / scenario fails,
which plugs cleanly into CI.

---

## Phase 8 — tiered memory

Before Phase 8, per-run state lived on a single flat dict inside `ContextManager`.
Phase 8 introduces an explicit three-tier memory hierarchy in `core/memory.py`, with
`ContextManager` becoming a thin facade so every existing call site keeps working
unchanged:

| Tier | Class | Scope | Persisted? | Typical contents |
|------|-------|-------|------------|------------------|
| working | `WorkingMemory` | one agent turn / one skill call | never | transient scratch values, in-flight parses |
| session | `SessionMemory` | one CLI run / one eval scenario | via `SessionStore` | message history, `last_spec_summary`, `last_verilog_template`, `revise_history`, `last_rtl_sim`, ... |
| long_term | `LongTermMemory` | across runs and sessions | JSON under `data/memory/` | `sim_history.json`, `patterns.json`, `lessons.json` |

Long-term memory is organized into named JSON stores. Two shapes are supported:

- **dict-valued** (`put` / `load`): keyed records with automatic `updated_at` stamps —
  useful for named patterns and lessons.
- **list-valued** (`append` / `load` / `recent`): append-only logs — used by `rtl_sim`
  to record every simulator verdict so a later run can mine it.

The orchestrator auto-attaches a default `LongTermMemory.default_for(project_root)` on
startup, so no explicit wiring is needed. From the CLI, `/memory` prints a quick
summary:

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

---

## Phase 9 — behavioral verification

Before Phase 9, every "verification" signal in SiFlowAgent was ultimately text-based:
lint regexes, review JSON from an LLM, or `contains_all` checks in the eval harness.
Phase 9 closes that gap with `VerifierAgent` and the `rtl_sim` skill — the only tool
in the stack whose pass/fail is a real execution result rather than a string match.

### `rtl_sim` skill

`skills/rtl_sim.py::RtlSimSkill`:

- picks `iverilog` first and falls back to `verilator --binary` via `shutil.which`
  (returns an explicit `no_tool` status when neither is installed, so the rest of the
  pipeline never silently skips verification)
- writes every module from `last_verilog_template` into a fresh tempdir under
  `data/sim_runs/` so concurrent runs do not collide
- compiles + executes the testbench, classifies the output by configurable pass/fail
  tokens (default `TEST_PASS` / `TEST_FAIL`) plus `PASS` / `FAIL` word counters
- stores the full verdict in session state under `last_rtl_sim`, appends a compact
  record to long-term `sim_history`, and returns a structured `RtlSimResult` with
  `compile_log` / `run_log` tails

### `VerifierAgent`

`core/agents.py::VerifierAgent` wraps one `rtl_sim` call and emits a structured
`AgentMessage` of kind `verification` with one of six verdicts: `verified`,
`sim_failed`, `compile_error`, `no_tool`, `skipped`, `error`.

Three entry points:

- CLI: `/verify harness/tb/counter_tb.v counter_tb`
- Orchestrator: `await orchestrator.verify_rtl(testbench_path=..., top_module=...)`
- Eval harness: add a `simulation:` block to any scenario YAML

### Simulation-driven evaluation

`harness/eval_scenarios.py::ScenarioSimulation` adds an optional `simulation:` block
to each scenario:

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

When enabled, `harness/eval_runner.py` runs the simulator against the tier's final RTL
after all other checks and adds two deterministic checks:

- `sim.compile[iverilog]` — did the design even elaborate against the TB?
- `sim.behavior[iverilog]` — did the simulation emit a pass marker?

This is the step from text matching to behavioral verification: a scenario whose
`contains_all: ["module counter", "always @"]` checks pass can still fail on
`sim.behavior` if the body is a TODO stub. The eval row now includes a `sim` column,
for example:

```text
  scenario           | tier   | res   | chk   | judge  | sim            | cost
  ------------------ | ------ | ----- | ----- | ------ | -------------- | ----
  counter_basic      | router | FAIL  | 7/8   | 0.35-  | fail-          | llm=3 steps=1 wall=1.1s
  counter_basic      | agent  | pass  | 8/8   | 0.88+  | pass+          | llm=7 steps=3 wall=3.9s
  counter_basic      | multi  | pass  | 8/8   | 0.90+  | pass+          | llm=11 steps=2 wall=5.3s
```

The router tier generally fails `sim.behavior` on a stub, while the agent and multi
tiers only pass once the LLM actually implements the increment, which is the
cost / quality tradeoff the eval harness is designed to surface.

---

## Phase 10 — cocotb-based verification (10A–B done, 10C–D planned)

A Python-driven verification layer using [cocotb](https://www.cocotb.org/) is the next
architectural milestone. It targets a limitation of the current `rtl_sim` skill: the
testbench is fixed Verilog and the agent can only observe `$display` text that is
decided ahead of time. cocotb keeps the DUT signals as live Python objects during
simulation, so an agent can author a short Python probe, sample exactly the signals
and time window it wants, structure the result, and iterate — no waveform file ever
needs to touch disk.

Phased plan:

| Sub-phase | Status | Goal | Notes |
|-----------|--------|------|-------|
| 10A | **done** | Install cocotb and port `counter_tb.v` to a Python testbench; verify the iverilog backend via `cocotb_tools.runner.get_runner("icarus")` | Lives under `harness/cocotb/`. cocotb 2.0 moved the runner to `cocotb_tools.runner`. The runner must pass `timescale=("1ns", "1ps")` or 100 MHz clocks fail with "unable to represent 10ns at precision 1e0" |
| 10B | **done** | New skill `cocotb_sim` returning a `CocotbSimResult(passes, fails, event_log)` | Mirrors the `rtl_sim` contract so the eval harness can consume either backend |
| 10C | planned | `VerifierAgent` dual-backend: use cocotb when a Python test is present, fall back to `rtl_sim` otherwise; add a `cocotb:` block to scenario YAML | Keeps existing scenarios working unchanged |
| 10D | planned | Probe-injection closed loop: the agent generates small cocotb coroutines as probes, observes structured event streams, and feeds them back as critic-style evidence | The core mechanism for agent-driven waveform debugging without multimodal input |

The motivation for sub-phase 10D is the observation that current VLMs do not read
waveform images reliably, whereas targeted text-form signal transitions are both
compact and structured enough for an LLM to reason over. cocotb makes the probe the
unit of "looking at the waveform", keeping the interaction inside the same text
modality that the rest of the stack already uses.

### 10A — what shipped

- `harness/cocotb/counter.v` — reference 8-bit synchronous up-counter DUT
- `harness/cocotb/test_counter_cocotb.py` — three `@cocotb.test()` coroutines that
  mirror the original Verilog TB checks (`reset_zero`, `increment_three`,
  `still_incrementing`)
- `harness/cocotb/run_counter.py` — standalone runner using
  `cocotb_tools.runner.get_runner("icarus")`; build artifacts go to
  `data/cocotb_build/` (gitignored)
- `cocotb>=2.0.0` added to `requirements.txt`

Run it with:

```bash
python harness/cocotb/run_counter.py
```

Expected output ends with `TESTS=3 PASS=3 FAIL=0 SKIP=0`.

### 10B — what shipped

`skills/cocotb_sim.py::CocotbSimSkill` wraps the cocotb runner as a first-class
SiFlow skill. The result schema (`CocotbSimResult`) reuses the status vocabulary of
`RtlSimResult` (`pass` / `fail` / `compile_error` / `no_tool` / `timeout` /
`error`) so downstream code paths can treat the two backends uniformly.

Skill contract:

| Param | Required | Notes |
|-------|----------|-------|
| `test_module` | yes | Dotted name of the cocotb test module (e.g. `test_counter_cocotb`) |
| `hdl_toplevel` | yes | DUT module name |
| `verilog_sources` | no | Paths to RTL files; defaults to writing `last_verilog_template` modules to a fresh tempdir under `data/cocotb_runs/` |
| `test_dir` | no | Directory containing the test module; defaults to `harness/cocotb/` |
| `tool` | no | `icarus` / `verilator` / `auto` (default: auto, prefers icarus) |
| `timeout_seconds` | no | Combined build + test timeout (default 60s) |
| `extra_sources`, `testcase`, `verilog_code` | no | Mirror `rtl_sim` semantics |

The skill drives `cocotb_tools.runner.get_runner(...)` programmatically, captures
stdout/stderr from the build and test phases (best-effort — cocotb spawns
subprocesses), parses the JUnit `results.xml` cocotb writes, and emits a
per-test-case event log such as:

```text
[PASS] reset_zero sim=20.0ns
[PASS] increment_three sim=61.0ns
[PASS] still_incrementing sim=161.0ns
```

The orchestrator dispatches `cocotb_sim` through `execute_action` and renders a
one-line observation matching the `rtl_sim` style:

```text
cocotb pass via icarus: build_ok=True, run_ok=True, cases=3/3 pass (0 fail, 0 skip)
on counter (0.625s). detail=3/3 cases passed
```

Long-term memory records each run under the `sim_history` namespace with a
`backend: "cocotb"` tag so a future Phase 13 mining pass can compare the two
backends side by side.

---

## Phase 11 — broader behavioral coverage (planned)

Expand simulation-driven verification beyond the counter case so the eval harness is
statistically meaningful.

- additional behavioral testbenches for FIFO, arbiter, controller blocks so
  simulation-based verification covers the multi-module scenario
- a "golden" RTL baseline per scenario so string-diff regressions become catchable
- a scenario that intentionally triggers revise-then-review and grades the loop

---

## Phase 12 — VerifierAgent in the multi-agent coordinator (planned)

Make behavioral verification a first-class member of the actor / critic loop.

- wire `VerifierAgent` into `/multi` as an optional third phase that runs after the
  critic emits `accept`
- introduce a retry budget so a `sim_failed` verdict can hand control back to the
  actor for one more revision round
- record verifier verdicts on the shared `AgentMessage` transcript so the critic can
  condition on past simulation results

---

## Phase 13 — long-term memory mining (planned)

Convert the long-term `sim_history` log into actionable conditioning.

- mine `data/memory/sim_history.json` to produce a `lessons.json` store of recurring
  failure patterns
- expose the lessons store to `PlannerSkill` and `CriticAgent` as additional context
  so the planner can avoid repeated mistakes
- add a CLI `/lessons` command for inspection

---

## Phase 14 — model heterogeneity (planned)

Let different agents use different LLM backends and unlock LLM-driven RTL generation.

- swap actor and critic onto different models (for example, a stronger model for the
  critic and a faster model for the actor)
- LLM-backed `verilog_template` that can iterate beyond TODO placeholders
- streaming / partial-result handling inside the agent loop

---

## Backlog (unscheduled)

Small ergonomic improvements that do not change the architecture and are not yet
assigned to a phase.

- auto-save sessions on every agent loop run; expose a `latest` shortcut
- diff view between two revise iterations
- improve RTL skeleton filling beyond TODO placeholders
- infer submodule ports and top-level wiring more precisely
- consolidate the five per-skill `_strip_json_fences` copies into a shared utility
- LLM client retry-on-RemoteProtocolError / empty-body / 504 (the current GLM and
  mimo gateways both lose ~20–30% of long requests; multi-step flows get truncated
  without retry)
