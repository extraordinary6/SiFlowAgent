"""Phase 10D: probe-injection skill.

The probe-injection mechanism lets the agent generate a small cocotb coroutine
*body* at runtime, run it inside a fresh simulation, and read back a
structured event stream. This is the substrate for "looking at the waveform"
without ever materializing a waveform file: instead of pixels or a binary VCD,
the agent observes a list of typed events emitted by its own probe code.

Mechanism:

1. The caller supplies ``probe_body``: Python source for the coroutine body,
   plus a DUT name and a set of Verilog sources.
2. The skill writes a tempdir test module that:
   - imports cocotb, the clock helper, and a small ``emit(tag, **kv)`` helper
     that prints ``[PROBE] tag=... k=v ...`` lines on a single line each
   - exec's the caller-provided body inside an ``async def probe(dut)`` test
3. The skill drives the test via ``cocotb_tools.runner`` (icarus by default).
4. The skill parses every ``[PROBE]`` line out of stdout into a typed
   ``ProbeEvent`` and returns the list, plus a final pass/fail status.

The skill deliberately keeps the contract narrow: it runs a single probe per
call, returns one structured event log, and lets the caller compose multiple
probes by issuing multiple calls. Phase 10D is the **mechanism**; the agent
loop that drives a probe → critique → next-probe cycle lives one layer up.

Note: ``probe_body`` is exec'd verbatim. Treat it as trusted Python the same
way you treat any agent-generated code you would run locally.
"""
from __future__ import annotations

import asyncio
import os
import re
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from context.manager import ContextManager
from skills.base import BaseSkill


PROBE_EVENT_LINE = re.compile(
    r"^(?P<rest>tag=.+)$"
)
KV_PAIR = re.compile(r"(?P<k>[A-Za-z_][A-Za-z0-9_]*)=(?P<v>\S+)")


class ProbeEvent(BaseModel):
    tag: str = Field(default="", description="Free-form event label set by the probe.")
    sim_time_ns: float | None = Field(default=None, description="Simulation time in ns when emit() was called.")
    data: dict[str, str] = Field(default_factory=dict, description="Caller-provided keyword payload (string-coerced).")


class ProbeInjectResult(BaseModel):
    status: str = Field(default="error", description="pass | fail | compile_error | no_tool | timeout | error")
    tool: str = Field(default="")
    duration_seconds: float = Field(default=0.0)
    events: list[ProbeEvent] = Field(default_factory=list)
    event_count: int = Field(default=0)
    last_signal_values: dict[str, str] = Field(
        default_factory=dict,
        description="Final emit() payload per tag, useful for spot-checks.",
    )
    cocotb_status: str = Field(default="")
    cocotb_passes: int = Field(default=0)
    cocotb_fails: int = Field(default=0)
    hdl_toplevel: str = Field(default="")
    verilog_sources: list[str] = Field(default_factory=list)
    work_dir: str = Field(default="")
    run_log_tail: str = Field(default="")
    detail: str = Field(default="")


# Wrapper template injected around the caller-provided coroutine body. Keeps
# imports + emit() helper consistent across calls so the agent only writes
# the body itself. Events are written to a file (not printed) because
# cocotb runs the simulator in a subprocess whose stdout is not captured by
# the parent Python process; a file is the reliable cross-process channel.
_PROBE_TEMPLATE = '''\
import os
import cocotb
from cocotb.clock import Clock
from cocotb.triggers import (
    ClockCycles,
    Combine,
    Edge,
    FallingEdge,
    First,
    Join,
    NextTimeStep,
    ReadOnly,
    ReadWrite,
    RisingEdge,
    Timer,
)
from cocotb.utils import get_sim_time


_PROBE_EVENT_PATH = os.environ.get("PROBE_EVENT_LOG", "probe_events.log")


def emit(tag, **kv):
    """Append a structured event line to the probe-event log file."""
    parts = ["tag=" + str(tag), "t=%dns" % int(get_sim_time("ns"))]
    for k, v in kv.items():
        try:
            v_str = str(int(v))
        except (TypeError, ValueError):
            v_str = str(v).replace(" ", "_")
        parts.append(k + "=" + v_str)
    with open(_PROBE_EVENT_PATH, "a", encoding="utf-8") as fp:
        fp.write(" ".join(parts) + "\\n")


@cocotb.test()
async def probe(dut):
# __PROBE_BODY__
'''


class ProbeInjectSkill(BaseSkill):
    """Run an agent-generated cocotb probe and return its structured event log."""

    def __init__(
        self,
        context_manager: ContextManager,
        project_root: str | Path | None = None,
        work_dir: str | Path | None = None,
    ) -> None:
        super().__init__(
            name="probe_inject",
            description=(
                "Inject and run an agent-generated cocotb probe coroutine against the most "
                "recent generated RTL. The probe samples DUT signals via cocotb's live "
                "Python handles and emits typed events through the emit(tag, **kv) helper. "
                "The skill returns a structured ProbeInjectResult with a parsed event list — "
                "the agent uses this to 'look at the waveform' without a waveform file."
            ),
            parameters_schema={
                "probe_body": {
                    "type": "string",
                    "required": True,
                    "description": (
                        "Python source for the probe coroutine body. Will be inserted into "
                        "`async def probe(dut):` so it must be valid indented coroutine code "
                        "that calls `await ...` and `emit(tag, **kv)` to produce events. "
                        "Has access to: cocotb, Clock, RisingEdge/FallingEdge/Edge/Timer/"
                        "ClockCycles/ReadOnly/ReadWrite/NextTimeStep/Combine/Join/First, "
                        "and emit()."
                    ),
                },
                "hdl_toplevel": {
                    "type": "string",
                    "required": True,
                    "description": "Top-level Verilog DUT module name.",
                },
                "verilog_sources": {
                    "type": "array",
                    "required": False,
                    "description": "Paths to DUT .v files. Defaults to last_verilog_template.",
                },
                "extra_sources": {
                    "type": "array",
                    "required": False,
                    "description": "Extra Verilog sources to compile alongside the DUT.",
                },
                "tool": {
                    "type": "string",
                    "required": False,
                    "description": "icarus | verilator | auto (default: auto, prefers icarus).",
                },
                "timeout_seconds": {
                    "type": "number",
                    "required": False,
                    "description": "Hard timeout for build + probe run (default 30s).",
                },
                "verilog_code": {
                    "type": "string",
                    "required": False,
                    "description": "Optional raw Verilog override instead of last_verilog_template.",
                },
            },
        )
        self.context_manager = context_manager
        self.project_root = Path(project_root) if project_root else Path.cwd()
        self.work_dir = Path(work_dir) if work_dir else self.project_root / "data" / "probe_runs"

    async def execute(self, **kwargs: Any) -> ProbeInjectResult:
        probe_body = str(kwargs.get("probe_body") or "").strip("\n")
        if not probe_body:
            raise ValueError("probe_inject requires probe_body")
        hdl_toplevel = str(kwargs.get("hdl_toplevel") or "").strip()
        if not hdl_toplevel:
            raise ValueError("probe_inject requires hdl_toplevel")

        # Local import keeps the skill importable without cocotb installed; we
        # surface the absence as ``no_tool`` further down.
        try:
            from skills.cocotb_sim import CocotbSimSkill, CocotbSimResult  # noqa: F401
        except ImportError:
            return ProbeInjectResult(
                status="no_tool",
                hdl_toplevel=hdl_toplevel,
                detail=(
                    "cocotb_sim skill is unavailable; install cocotb (`pip install cocotb`) "
                    "and ensure skills.cocotb_sim imports cleanly."
                ),
            )

        result = ProbeInjectResult(hdl_toplevel=hdl_toplevel)

        self.work_dir.mkdir(parents=True, exist_ok=True)
        run_dir = Path(tempfile.mkdtemp(prefix="probe_", dir=self.work_dir))
        result.work_dir = str(run_dir)

        event_log_path = run_dir / "probe_events.log"
        # Tell the wrapped emit() where to write — survives the child sim process.
        os.environ["PROBE_EVENT_LOG"] = str(event_log_path)

        # Write the wrapped probe module.
        body_indented = "\n".join(
            ("    " + line) if line else line for line in probe_body.splitlines()
        )
        # Guard against an entirely-empty body so the test still parses.
        if not body_indented.strip():
            body_indented = "    pass"
        test_source = _PROBE_TEMPLATE.replace("# __PROBE_BODY__", body_indented)
        test_module_name = "probe_module"
        (run_dir / f"{test_module_name}.py").write_text(test_source, encoding="utf-8")

        # Delegate the build + run to CocotbSimSkill — it already handles tool
        # detection, tempdir-per-source, results.xml parsing, and timeouts.
        sim_skill = CocotbSimSkill(
            context_manager=self.context_manager,
            project_root=self.project_root,
        )

        sim_args: dict[str, Any] = {
            "test_module": test_module_name,
            "hdl_toplevel": hdl_toplevel,
            "test_dir": str(run_dir),
            "timeout_seconds": float(kwargs.get("timeout_seconds") or 30.0),
        }
        if kwargs.get("verilog_sources"):
            sim_args["verilog_sources"] = kwargs["verilog_sources"]
        if kwargs.get("verilog_code"):
            sim_args["verilog_code"] = kwargs["verilog_code"]
        if kwargs.get("extra_sources"):
            sim_args["extra_sources"] = kwargs["extra_sources"]
        if kwargs.get("tool"):
            sim_args["tool"] = kwargs["tool"]

        start = time.monotonic()
        try:
            sim_result = await sim_skill.execute(**sim_args)
        except Exception as exc:  # noqa: BLE001 - surface any cocotb error structurally
            result.status = "error"
            result.detail = str(exc)[:300]
            result.duration_seconds = time.monotonic() - start
            self._persist(result)
            return result

        result.duration_seconds = time.monotonic() - start
        result.tool = sim_result.tool
        result.cocotb_status = sim_result.status
        result.cocotb_passes = sim_result.passes
        result.cocotb_fails = sim_result.fails
        result.run_log_tail = sim_result.run_log_tail
        result.verilog_sources = sim_result.verilog_sources

        if sim_result.status == "no_tool":
            result.status = "no_tool"
            result.detail = sim_result.detail
            self._persist(result)
            return result

        # Parse PROBE events out of the dedicated event log file. The cocotb
        # simulator runs in a child process whose stdout never reaches our
        # captured tail, so the event log is the reliable channel.
        result.events = self._parse_events(event_log_path)
        result.event_count = len(result.events)
        for event in result.events:
            if event.tag:
                result.last_signal_values[event.tag] = ", ".join(
                    f"{k}={v}" for k, v in event.data.items()
                )

        # Map cocotb status → probe status. compile_error / timeout pass through;
        # cocotb pass/fail folds into probe pass/fail.
        if sim_result.status == "pass":
            result.status = "pass"
            result.detail = (
                f"probe ran cleanly, {result.event_count} events captured "
                f"in {result.duration_seconds:.2f}s"
            )
        elif sim_result.status == "fail":
            result.status = "fail"
            result.detail = sim_result.detail or "probe coroutine failed"
        else:
            result.status = sim_result.status  # compile_error / timeout / error
            result.detail = sim_result.detail

        self._persist(result)
        return result

    @staticmethod
    def _parse_events(log_path: Path) -> list[ProbeEvent]:
        events: list[ProbeEvent] = []
        if not log_path.exists():
            return events
        for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            data: dict[str, str] = {}
            tag = ""
            sim_time_ns: float | None = None
            for kv in KV_PAIR.finditer(line):
                key = kv.group("k")
                value = kv.group("v")
                if key == "tag":
                    tag = value
                elif key == "t":
                    cleaned = value.rstrip("ns")
                    try:
                        sim_time_ns = float(cleaned)
                    except ValueError:
                        sim_time_ns = None
                else:
                    data[key] = value
            events.append(ProbeEvent(tag=tag, sim_time_ns=sim_time_ns, data=data))
        return events

    def _persist(self, result: ProbeInjectResult) -> None:
        self.context_manager.set_state("last_skill", self.name)
        self.context_manager.set_state(
            "last_probe_inject",
            {
                "status": result.status,
                "event_count": result.event_count,
                "tool": result.tool,
                "duration_seconds": round(result.duration_seconds, 3),
                "hdl_toplevel": result.hdl_toplevel,
                "events": [event.model_dump() for event in result.events[-50:]],
                "detail": result.detail,
            },
        )
