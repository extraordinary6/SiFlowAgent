"""Phase 10B: cocotb-driven behavioral verification skill.

`CocotbSimSkill` mirrors the contract of `RtlSimSkill` but drives the DUT through
a Python coroutine testbench instead of a Verilog testbench. The cocotb runner
hands the DUT to a Python module, where each ``@cocotb.test()`` becomes one
test case; results are parsed from the JUnit-style ``results.xml`` cocotb writes.

Returned ``CocotbSimResult`` keeps the same status vocabulary as ``RtlSimResult``
(``pass`` / ``fail`` / ``compile_error`` / ``no_tool`` / ``timeout`` / ``error``)
so the orchestrator and eval harness can consume either backend interchangeably
once Phase 10C wires the dual-backend `VerifierAgent`.
"""
from __future__ import annotations

import asyncio
import contextlib
import io
import shutil
import tempfile
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from context.manager import ContextManager
from skills.base import BaseSkill


class CocotbCaseResult(BaseModel):
    name: str
    status: str = Field(default="pass", description="pass | fail | error | skipped")
    sim_time_ns: float | None = None
    wall_seconds: float | None = None
    message: str = Field(default="")


class CocotbSimResult(BaseModel):
    tool: str = Field(default="", description="icarus | verilator | ''")
    status: str = Field(
        default="error",
        description="pass | fail | compile_error | no_tool | timeout | error",
    )
    build_ok: bool = Field(default=False)
    run_ok: bool = Field(default=False)
    build_log_tail: str = Field(default="", description="Last 800 chars of build stdout (best effort).")
    run_log_tail: str = Field(default="", description="Last 800 chars of test stdout (best effort).")
    passes: int = Field(default=0)
    fails: int = Field(default=0)
    skipped: int = Field(default=0)
    test_cases: list[CocotbCaseResult] = Field(default_factory=list)
    event_log: list[str] = Field(default_factory=list, description="Per-case status timeline.")
    duration_seconds: float = Field(default=0.0)
    hdl_toplevel: str = Field(default="")
    test_module: str = Field(default="")
    verilog_sources: list[str] = Field(default_factory=list)
    test_dir: str = Field(default="")
    build_dir: str = Field(default="")
    results_xml: str = Field(default="")
    detail: str = Field(default="")


class CocotbSimSkill(BaseSkill):
    """Run a cocotb test against the most recent generated RTL.

    Tries the ``icarus`` backend first (fast, widely available) and falls back
    to ``verilator``. Returns a structured per-case result plus an aggregated
    pass/fail status mirroring ``RtlSimResult``. Persists ``last_cocotb_sim``
    in session state and appends a compact record to long-term ``sim_history``.
    """

    DEFAULT_TIMESCALE = ("1ns", "1ps")

    def __init__(
        self,
        context_manager: ContextManager,
        project_root: str | Path | None = None,
        work_dir: str | Path | None = None,
    ) -> None:
        super().__init__(
            name="cocotb_sim",
            description=(
                "Run a cocotb Python testbench against the most recent generated RTL. "
                "Drives the DUT via cocotb coroutines, parses results.xml, and returns "
                "structured pass/fail per test case. Use this for behavioral verification "
                "when a Python testbench is available; mirrors the rtl_sim contract for "
                "Verilog testbenches."
            ),
            parameters_schema={
                "test_module": {
                    "type": "string",
                    "required": True,
                    "description": "Dotted module name of the cocotb test (e.g. 'test_counter_cocotb').",
                },
                "hdl_toplevel": {
                    "type": "string",
                    "required": True,
                    "description": "Name of the top-level HDL module (the DUT) the test instantiates.",
                },
                "test_dir": {
                    "type": "string",
                    "required": False,
                    "description": "Directory containing the test_module .py file. Defaults to <project_root>/harness/cocotb.",
                },
                "verilog_sources": {
                    "type": "array",
                    "required": False,
                    "description": "Paths to Verilog source files (DUT). Defaults to writing last_verilog_template modules to a fresh tempdir.",
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
                    "description": "Hard timeout for the build+test combined (default 60s).",
                },
                "testcase": {
                    "type": "string",
                    "required": False,
                    "description": "Run only test cases whose name matches this filter.",
                },
                "verilog_code": {
                    "type": "string",
                    "required": False,
                    "description": "Optional raw Verilog to simulate instead of last_verilog_template.",
                },
            },
        )
        self.context_manager = context_manager
        self.project_root = Path(project_root) if project_root else Path.cwd()
        self.work_dir = Path(work_dir) if work_dir else self.project_root / "data" / "cocotb_runs"
        self._last_run_dir: Path | None = None

    async def execute(self, **kwargs: Any) -> CocotbSimResult:
        test_module = str(kwargs.get("test_module") or "").strip()
        if not test_module:
            raise ValueError("cocotb_sim requires test_module")
        hdl_toplevel = str(kwargs.get("hdl_toplevel") or "").strip()
        if not hdl_toplevel:
            raise ValueError("cocotb_sim requires hdl_toplevel")

        test_dir = self._resolve_test_dir(kwargs.get("test_dir"))
        if not (test_dir / f"{test_module}.py").exists():
            raise FileNotFoundError(
                f"test module not found: {test_dir / (test_module + '.py')}"
            )

        verilog_sources = self._resolve_verilog_sources(kwargs)
        if not verilog_sources:
            raise ValueError(
                "cocotb_sim requires either verilog_sources, verilog_code, or a prior verilog_template result"
            )

        extra_sources = [
            self._resolve_path(path) for path in (kwargs.get("extra_sources") or [])
        ]
        tool = self._pick_tool(str(kwargs.get("tool") or "auto").lower())
        timeout = float(kwargs.get("timeout_seconds") or 60.0)
        testcase_filter = kwargs.get("testcase") or None

        result = CocotbSimResult(
            tool=tool or "",
            hdl_toplevel=hdl_toplevel,
            test_module=test_module,
            test_dir=str(test_dir),
            verilog_sources=[str(p) for p in verilog_sources],
        )

        if not tool:
            result.status = "no_tool"
            result.detail = (
                "Neither iverilog nor verilator found on PATH. "
                "Install iverilog (e.g. `choco install iverilog` on Windows, "
                "`apt install iverilog` on Debian) to enable cocotb simulation."
            )
            self._persist(result)
            return result

        build_dir = self.work_dir / f"cocotb_{int(time.time() * 1000):x}"
        build_dir.mkdir(parents=True, exist_ok=True)
        result.build_dir = str(build_dir)
        self._last_run_dir = build_dir

        all_sources = list(verilog_sources) + list(extra_sources)

        start = time.monotonic()
        try:
            results_xml = await asyncio.wait_for(
                asyncio.to_thread(
                    self._run_cocotb,
                    tool=tool,
                    sources=all_sources,
                    hdl_toplevel=hdl_toplevel,
                    test_module=test_module,
                    test_dir=test_dir,
                    build_dir=build_dir,
                    testcase_filter=testcase_filter,
                    result=result,
                ),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            result.status = "timeout"
            result.detail = f"cocotb run timed out after {timeout:.1f}s"
        except Exception as exc:  # noqa: BLE001 - any cocotb/runner error becomes structured output
            self._classify_runner_exception(result, exc)
        else:
            result.results_xml = str(results_xml) if results_xml else ""
            self._parse_results_xml(result)
        finally:
            result.duration_seconds = time.monotonic() - start

        self._persist(result)
        return result

    # ---- tool discovery ----

    def _pick_tool(self, preference: str) -> str:
        # cocotb's runner key is "icarus", but accept the binary name "iverilog" too.
        icarus_path = shutil.which("iverilog") or shutil.which("iverilog.exe")
        verilator_path = shutil.which("verilator") or shutil.which("verilator.exe")
        if preference in {"icarus", "iverilog"}:
            return "icarus" if icarus_path else ""
        if preference == "verilator":
            return "verilator" if verilator_path else ""
        if icarus_path:
            return "icarus"
        if verilator_path:
            return "verilator"
        return ""

    # ---- input resolution ----

    def _resolve_test_dir(self, raw: str | None) -> Path:
        if raw:
            path = Path(raw).expanduser()
        else:
            path = self.project_root / "harness" / "cocotb"
        if not path.is_absolute():
            path = self.project_root / path
        return path.resolve()

    def _resolve_path(self, raw_path: str) -> Path:
        path = Path(raw_path).expanduser()
        if not path.is_absolute():
            path = self.project_root / path
        if not path.exists():
            raise FileNotFoundError(f"source not found: {path}")
        return path.resolve()

    def _resolve_verilog_sources(self, kwargs: dict[str, Any]) -> list[Path]:
        explicit = kwargs.get("verilog_sources")
        if explicit:
            return [self._resolve_path(p) for p in explicit]

        override_code = kwargs.get("verilog_code")
        if override_code:
            tmp_dir = Path(tempfile.mkdtemp(prefix="cocotbsrc_", dir=self.work_dir))
            self.work_dir.mkdir(parents=True, exist_ok=True)
            out_path = tmp_dir / "override.v"
            out_path.write_text(override_code, encoding="utf-8")
            return [out_path]

        last_template = self.context_manager.get_state("last_verilog_template") or {}
        modules = last_template.get("modules") or []
        if not modules:
            return []

        self.work_dir.mkdir(parents=True, exist_ok=True)
        tmp_dir = Path(tempfile.mkdtemp(prefix="cocotbsrc_", dir=self.work_dir))
        files: list[Path] = []
        seen_code: set[str] = set()
        for module in modules:
            code = module.get("verilog_code") or ""
            if not code or code in seen_code:
                continue
            seen_code.add(code)
            file_name = module.get("file_name") or f"{module.get('module_name', 'module')}.v"
            out_path = tmp_dir / file_name
            out_path.write_text(code, encoding="utf-8")
            files.append(out_path)
        return files

    # ---- runner ----

    def _run_cocotb(
        self,
        *,
        tool: str,
        sources: list[Path],
        hdl_toplevel: str,
        test_module: str,
        test_dir: Path,
        build_dir: Path,
        testcase_filter: str | None,
        result: CocotbSimResult,
    ) -> Path | None:
        # Local import keeps the skill importable even when cocotb is missing —
        # we surface the failure via _classify_runner_exception instead.
        from cocotb_tools.runner import get_runner

        runner = get_runner(tool)

        build_buf = io.StringIO()
        with contextlib.redirect_stdout(build_buf), contextlib.redirect_stderr(build_buf):
            runner.build(
                verilog_sources=[str(s) for s in sources],
                hdl_toplevel=hdl_toplevel,
                build_dir=str(build_dir),
                timescale=self.DEFAULT_TIMESCALE,
                always=True,
            )
        result.build_ok = True
        result.build_log_tail = build_buf.getvalue()[-800:]

        run_buf = io.StringIO()
        test_kwargs: dict[str, Any] = dict(
            hdl_toplevel=hdl_toplevel,
            test_module=test_module,
            test_dir=str(test_dir),
            build_dir=str(build_dir),
            timescale=self.DEFAULT_TIMESCALE,
        )
        if testcase_filter:
            test_kwargs["testcase"] = testcase_filter
        with contextlib.redirect_stdout(run_buf), contextlib.redirect_stderr(run_buf):
            results_xml = runner.test(**test_kwargs)
        result.run_ok = True
        result.run_log_tail = run_buf.getvalue()[-800:]
        return Path(results_xml) if results_xml else None

    def _classify_runner_exception(self, result: CocotbSimResult, exc: Exception) -> None:
        message = str(exc) or exc.__class__.__name__
        lowered = message.lower()
        if isinstance(exc, ModuleNotFoundError) and "cocotb" in lowered:
            result.status = "no_tool"
            result.detail = (
                "cocotb is not installed in this Python environment. "
                "Install with `pip install cocotb`."
            )
            return
        if "no such file" in lowered or "not found" in lowered:
            result.status = "compile_error"
            result.detail = message
            return
        if not result.build_ok:
            result.status = "compile_error"
        else:
            result.status = "error"
        result.detail = message[:400]

    # ---- result parsing ----

    def _parse_results_xml(self, result: CocotbSimResult) -> None:
        if not result.results_xml or not Path(result.results_xml).exists():
            result.status = "error"
            result.detail = result.detail or "cocotb did not produce a results.xml"
            return

        try:
            root = ET.parse(result.results_xml).getroot()
        except ET.ParseError as exc:
            result.status = "error"
            result.detail = f"failed to parse results.xml: {exc}"
            return

        for testcase in root.iter("testcase"):
            name = testcase.attrib.get("name", "?")
            sim_time = testcase.attrib.get("sim_time_ns")
            wall = testcase.attrib.get("time")
            failure = testcase.find("failure")
            error = testcase.find("error")
            skipped = testcase.find("skipped")

            if failure is not None or error is not None:
                node = failure if failure is not None else error
                msg = (node.attrib.get("message") or "").strip() or (node.text or "").strip()
                status = "fail" if failure is not None else "error"
                result.fails += 1
                marker = "FAIL" if status == "fail" else "ERROR"
            elif skipped is not None:
                msg = (skipped.attrib.get("message") or "").strip()
                status = "skipped"
                result.skipped += 1
                marker = "SKIP"
            else:
                msg = ""
                status = "pass"
                result.passes += 1
                marker = "PASS"

            case = CocotbCaseResult(
                name=name,
                status=status,
                sim_time_ns=float(sim_time) if sim_time else None,
                wall_seconds=float(wall) if wall else None,
                message=msg[:400],
            )
            result.test_cases.append(case)
            head = f"[{marker}] {name}"
            if sim_time:
                head += f" sim={sim_time}ns"
            if msg:
                head += f" — {msg[:120]}"
            result.event_log.append(head)

        if not result.test_cases:
            result.status = "error"
            result.detail = result.detail or "no testcases found in results.xml"
            return

        if result.fails > 0:
            result.status = "fail"
            result.detail = result.detail or f"{result.fails}/{len(result.test_cases)} cases failed"
        else:
            result.status = "pass"
            result.detail = result.detail or f"{result.passes}/{len(result.test_cases)} cases passed"

    # ---- persistence ----

    def _persist(self, result: CocotbSimResult) -> None:
        self.context_manager.set_state("last_skill", self.name)
        self.context_manager.set_state("last_cocotb_sim", result.model_dump())
        long_term = getattr(self.context_manager, "long_term", None)
        if long_term is None:
            return
        try:
            long_term.append(
                "sim_history",
                {
                    "backend": "cocotb",
                    "tool": result.tool,
                    "status": result.status,
                    "hdl_toplevel": result.hdl_toplevel,
                    "test_module": result.test_module,
                    "passes": result.passes,
                    "fails": result.fails,
                    "skipped": result.skipped,
                    "duration_seconds": round(result.duration_seconds, 3),
                    "build_ok": result.build_ok,
                    "run_ok": result.run_ok,
                    "detail": result.detail,
                },
            )
        except Exception:  # noqa: BLE001 - long-term write must never break the sim call
            pass
