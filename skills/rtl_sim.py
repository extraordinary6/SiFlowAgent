from __future__ import annotations

import asyncio
import re
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from context.manager import ContextManager
from skills.base import BaseSkill


class RtlSimResult(BaseModel):
    tool: str = Field(default="", description="Simulator used: iverilog | verilator | ''")
    status: str = Field(
        default="error",
        description="pass | fail | compile_error | no_tool | timeout | error",
    )
    compile_ok: bool = Field(default=False)
    run_ok: bool = Field(default=False)
    compile_log: str = Field(default="")
    run_log: str = Field(default="")
    assertions_passed: int = Field(default=0, description="Count of 'PASS' occurrences in stdout")
    assertions_failed: int = Field(default=0, description="Count of 'FAIL' occurrences in stdout")
    pass_marker_seen: bool = Field(default=False)
    fail_marker_seen: bool = Field(default=False)
    duration_seconds: float = Field(default=0.0)
    top_module: str = Field(default="")
    module_files: list[str] = Field(default_factory=list)
    testbench: str = Field(default="")
    work_dir: str = Field(default="")
    detail: str = Field(default="")


class RtlSimSkill(BaseSkill):
    """Run a real Verilog simulator on the most recent generated RTL.

    This is the only skill in the stack that produces a **behavioral** pass/fail
    signal rather than a text match or LLM judgement. Tries iverilog first
    (fast, widely available), falls back to verilator (--binary), and surfaces
    a clear ``no_tool`` status if neither is installed.
    """

    DEFAULT_PASS_TOKENS: tuple[str, ...] = ("TEST_PASS", "ALL_TESTS_PASSED")
    DEFAULT_FAIL_TOKENS: tuple[str, ...] = ("TEST_FAIL", "TEST_FAILED", "ASSERTION_FAILED")

    def __init__(
        self,
        context_manager: ContextManager,
        project_root: str | Path | None = None,
        work_dir: str | Path | None = None,
    ) -> None:
        super().__init__(
            name="rtl_sim",
            description=(
                "Run a real Verilog simulator (iverilog preferred, verilator as fallback) "
                "on the most recent generated RTL against a provided testbench. Returns a "
                "true behavioral pass/fail signal rather than a text match. Writes the "
                "verdict to context as last_rtl_sim and appends a record to long-term "
                "memory (sim_history). Use this to verify RTL behavior, not just style."
            ),
            parameters_schema={
                "testbench_path": {
                    "type": "string",
                    "required": True,
                    "description": "Absolute or project-relative path to a Verilog testbench file.",
                },
                "top_module": {
                    "type": "string",
                    "required": False,
                    "description": "Top-level testbench module name. If omitted, auto-detected from the TB file.",
                },
                "tool": {
                    "type": "string",
                    "required": False,
                    "description": "iverilog | verilator | auto (default: auto, prefers iverilog).",
                },
                "timeout_seconds": {
                    "type": "number",
                    "required": False,
                    "description": "Hard timeout for each compile/run subprocess (default 20s).",
                },
                "pass_tokens": {
                    "type": "array",
                    "required": False,
                    "description": "Strings that mark a pass when present in stdout (default: TEST_PASS, ALL_TESTS_PASSED).",
                },
                "fail_tokens": {
                    "type": "array",
                    "required": False,
                    "description": "Strings that mark a fail when present in stdout (default: TEST_FAIL, ASSERTION_FAILED).",
                },
                "extra_sources": {
                    "type": "array",
                    "required": False,
                    "description": "Extra Verilog source file paths to compile alongside the generated RTL.",
                },
                "verilog_code": {
                    "type": "string",
                    "required": False,
                    "description": "Optional raw Verilog to simulate instead of the RTL stored in context.",
                },
            },
        )
        self.context_manager = context_manager
        self.project_root = Path(project_root) if project_root else Path.cwd()
        self.work_dir = Path(work_dir) if work_dir else self.project_root / "data" / "sim_runs"
        self._last_run_dir: Path | None = None

    async def execute(self, **kwargs: Any) -> RtlSimResult:
        testbench_arg = kwargs.get("testbench_path")
        if not testbench_arg:
            raise ValueError("rtl_sim requires testbench_path")

        testbench = Path(testbench_arg).expanduser()
        if not testbench.is_absolute():
            testbench = self.project_root / testbench
        if not testbench.exists() or not testbench.is_file():
            raise FileNotFoundError(f"testbench not found: {testbench}")

        top_module = str(kwargs.get("top_module") or "").strip() or self._detect_top_module(testbench)
        tool_pref = str(kwargs.get("tool") or "auto").lower()
        timeout = float(kwargs.get("timeout_seconds") or 20.0)
        pass_tokens = tuple(kwargs.get("pass_tokens") or self.DEFAULT_PASS_TOKENS)
        fail_tokens = tuple(kwargs.get("fail_tokens") or self.DEFAULT_FAIL_TOKENS)
        extra_sources = [
            self._resolve_extra_source(path)
            for path in (kwargs.get("extra_sources") or [])
        ]

        rtl_files = self._write_rtl_to_tempdir(kwargs.get("verilog_code"))
        if not rtl_files:
            raise ValueError(
                "rtl_sim requires a prior verilog_template/rtl_revise result, or an explicit verilog_code argument"
            )

        tool = self._pick_tool(tool_pref)
        result = RtlSimResult(
            tool=tool or "",
            top_module=top_module,
            module_files=[path.name for path in rtl_files],
            testbench=str(testbench),
            work_dir=str(self._last_run_dir) if self._last_run_dir else "",
        )

        if not tool:
            result.status = "no_tool"
            result.detail = (
                "Neither iverilog nor verilator found on PATH. "
                "Install iverilog (e.g. `choco install iverilog` on Windows, "
                "`apt install iverilog` on Debian) or verilator to enable real simulation."
            )
            self._persist(result)
            return result

        start = time.monotonic()
        try:
            if tool == "iverilog":
                await self._run_iverilog(rtl_files, extra_sources, testbench, top_module, timeout, result)
            else:
                await self._run_verilator(rtl_files, extra_sources, testbench, top_module, timeout, result)
        finally:
            result.duration_seconds = time.monotonic() - start

        self._classify(result, pass_tokens, fail_tokens)
        self._persist(result)
        return result

    # ---- tool discovery ----

    def _pick_tool(self, preference: str) -> str:
        iverilog = shutil.which("iverilog") or shutil.which("iverilog.exe")
        verilator = shutil.which("verilator") or shutil.which("verilator.exe")
        if preference == "iverilog":
            return "iverilog" if iverilog else ""
        if preference == "verilator":
            return "verilator" if verilator else ""
        if iverilog:
            return "iverilog"
        if verilator:
            return "verilator"
        return ""

    # ---- RTL & path preparation ----

    def _resolve_extra_source(self, raw_path: str) -> Path:
        path = Path(raw_path).expanduser()
        if not path.is_absolute():
            path = self.project_root / path
        if not path.exists():
            raise FileNotFoundError(f"extra_sources entry not found: {path}")
        return path

    def _detect_top_module(self, testbench: Path) -> str:
        try:
            text = testbench.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""
        match = re.search(r"^\s*module\s+(\w+)", text, flags=re.MULTILINE)
        return match.group(1) if match else ""

    def _write_rtl_to_tempdir(self, override_code: str | None) -> list[Path]:
        self.work_dir.mkdir(parents=True, exist_ok=True)
        run_dir = Path(tempfile.mkdtemp(prefix="rtlsim_", dir=self.work_dir))
        self._last_run_dir = run_dir

        if override_code:
            out_path = run_dir / "override.v"
            out_path.write_text(override_code, encoding="utf-8")
            return [out_path]

        last_template = self.context_manager.get_state("last_verilog_template") or {}
        modules = last_template.get("modules") or []
        files: list[Path] = []
        seen_code: set[str] = set()
        for module in modules:
            code = module.get("verilog_code") or ""
            if not code or code in seen_code:
                continue
            seen_code.add(code)
            file_name = module.get("file_name") or f"{module.get('module_name', 'module')}.v"
            out_path = run_dir / file_name
            out_path.write_text(code, encoding="utf-8")
            files.append(out_path)

        if not files:
            top_code = last_template.get("verilog_code", "")
            if top_code:
                out_path = run_dir / f"{last_template.get('module_name') or 'top'}.v"
                out_path.write_text(top_code, encoding="utf-8")
                files.append(out_path)

        return files

    # ---- simulator drivers ----

    async def _run_iverilog(
        self,
        rtl_files: list[Path],
        extra_sources: list[Path],
        testbench: Path,
        top_module: str,
        timeout: float,
        result: RtlSimResult,
    ) -> None:
        run_dir = self._last_run_dir
        assert run_dir is not None
        sim_out = run_dir / "sim.vvp"

        compile_cmd = ["iverilog", "-g2012", "-o", str(sim_out)]
        if top_module:
            compile_cmd.extend(["-s", top_module])
        compile_cmd.append(str(testbench))
        compile_cmd.extend(str(path) for path in rtl_files)
        compile_cmd.extend(str(path) for path in extra_sources)

        compile_rc, compile_out = await self._run_cmd(compile_cmd, timeout, cwd=run_dir)
        result.compile_log = compile_out
        result.compile_ok = compile_rc == 0 and sim_out.exists()
        if not result.compile_ok:
            result.status = "compile_error"
            result.detail = f"iverilog exit={compile_rc}"
            return

        run_rc, run_out = await self._run_cmd(["vvp", str(sim_out)], timeout, cwd=run_dir)
        result.run_log = run_out
        result.run_ok = run_rc == 0
        if run_rc == 124:
            result.status = "timeout"
            result.detail = f"vvp timed out after {timeout:.1f}s"

    async def _run_verilator(
        self,
        rtl_files: list[Path],
        extra_sources: list[Path],
        testbench: Path,
        top_module: str,
        timeout: float,
        result: RtlSimResult,
    ) -> None:
        run_dir = self._last_run_dir
        assert run_dir is not None

        cmd = ["verilator", "--binary", "--timing", "-Wno-fatal"]
        if top_module:
            cmd.extend(["--top-module", top_module])
        cmd.append(str(testbench))
        cmd.extend(str(path) for path in rtl_files)
        cmd.extend(str(path) for path in extra_sources)

        build_rc, build_out = await self._run_cmd(cmd, timeout, cwd=run_dir)
        result.compile_log = build_out
        result.compile_ok = build_rc == 0
        if not result.compile_ok:
            result.status = "compile_error"
            result.detail = f"verilator exit={build_rc}"
            return

        binary: Path | None = None
        obj_dir = run_dir / "obj_dir"
        if obj_dir.exists():
            for candidate_name in (f"V{top_module}", f"V{top_module}.exe"):
                candidate = obj_dir / candidate_name
                if candidate.exists():
                    binary = candidate
                    break
        if binary is None:
            result.status = "error"
            result.detail = "verilator did not produce an expected binary"
            return

        run_rc, run_out = await self._run_cmd([str(binary)], timeout, cwd=run_dir)
        result.run_log = run_out
        result.run_ok = run_rc == 0
        if run_rc == 124:
            result.status = "timeout"
            result.detail = f"{binary.name} timed out after {timeout:.1f}s"

    async def _run_cmd(
        self,
        cmd: list[str],
        timeout: float,
        cwd: Path,
    ) -> tuple[int, str]:
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=str(cwd),
            )
        except FileNotFoundError as error:
            return 127, f"[{cmd[0]} not found: {error}]"

        try:
            stdout_bytes, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            try:
                await proc.wait()
            except Exception:  # noqa: BLE001
                pass
            return 124, f"[TIMEOUT after {timeout:.1f}s running: {' '.join(cmd)}]"

        text = stdout_bytes.decode("utf-8", errors="replace")
        return proc.returncode or 0, text

    # ---- verdict classification ----

    def _classify(
        self,
        result: RtlSimResult,
        pass_tokens: tuple[str, ...],
        fail_tokens: tuple[str, ...],
    ) -> None:
        # Only short-circuit on terminal states the runner already set itself.
        # ``error`` is the field default and gets reclassified below.
        if result.status in {"compile_error", "no_tool", "timeout"}:
            return
        log = result.run_log or ""
        result.pass_marker_seen = any(tok and tok in log for tok in pass_tokens)
        result.fail_marker_seen = any(tok and tok in log for tok in fail_tokens)
        result.assertions_passed = len(re.findall(r"\bPASS\b", log))
        result.assertions_failed = len(re.findall(r"\bFAIL\b", log))

        if result.fail_marker_seen or result.assertions_failed > 0 or not result.run_ok:
            result.status = "fail"
            if not result.detail:
                result.detail = (
                    f"assertions_failed={result.assertions_failed}, run_ok={result.run_ok}"
                )
            return
        if result.pass_marker_seen or result.assertions_passed > 0:
            result.status = "pass"
            if not result.detail:
                result.detail = (
                    f"pass_marker={result.pass_marker_seen}, "
                    f"assertions_passed={result.assertions_passed}"
                )
            return
        result.status = "fail"
        result.detail = "no pass marker found in simulation output"

    # ---- persistence ----

    def _persist(self, result: RtlSimResult) -> None:
        self.context_manager.set_state("last_skill", self.name)
        self.context_manager.set_state("last_rtl_sim", result.model_dump())
        long_term = getattr(self.context_manager, "long_term", None)
        if long_term is None:
            return
        try:
            long_term.append(
                "sim_history",
                {
                    "tool": result.tool,
                    "status": result.status,
                    "top_module": result.top_module,
                    "module_files": result.module_files,
                    "testbench": result.testbench,
                    "duration_seconds": round(result.duration_seconds, 3),
                    "assertions_passed": result.assertions_passed,
                    "assertions_failed": result.assertions_failed,
                    "compile_ok": result.compile_ok,
                    "run_ok": result.run_ok,
                    "detail": result.detail,
                },
            )
        except Exception:  # noqa: BLE001 - long-term write must never break the sim call
            pass
