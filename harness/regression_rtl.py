from __future__ import annotations

import shutil
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from context.manager import ContextManager
from skills.spec_summary import SignalSummary, SpecSummaryResult, SubmoduleSummary
from skills.verilog_template import VerilogTemplateSkill


def build_sample_multi_module_summary() -> SpecSummaryResult:
    return SpecSummaryResult(
        module_name="system_top",
        overview="Multi-module example with controller, datapath, fifo, and arbiter.",
        interfaces=[
            SignalSummary(name="clk", direction="input", width="1", description="clock"),
            SignalSummary(name="rst_n", direction="input", width="1", description="active low reset"),
            SignalSummary(name="start", direction="input", width="1", description="start pulse"),
            SignalSummary(name="data_in", direction="input", width="32", description="input data bus"),
            SignalSummary(name="req", direction="input", width="4", description="request bus"),
            SignalSummary(name="data_out", direction="output", width="32", description="datapath result"),
            SignalSummary(name="busy", direction="output", width="1", description="controller busy flag"),
            SignalSummary(name="grant", direction="output", width="4", description="arbiter grant bus"),
        ],
        functional_behavior=[
            "controller drives ctrl_enable and ctrl_valid",
            "datapath computes data_out from data_in when enabled",
            "fifo buffers datapath results between stages",
            "arbiter grants one requester from req",
        ],
        timing_and_control=[
            "sample inputs on rising edge of clk",
            "rst_n is active low",
            "arbiter updates grant on clock boundaries",
        ],
        constraints=["keep one output signal per always block unless the conditions are exactly identical"],
        submodules=[
            SubmoduleSummary(name="controller", role="control path"),
            SubmoduleSummary(name="datapath", role="data path"),
            SubmoduleSummary(name="fifo", role="buffer queue"),
            SubmoduleSummary(name="arbiter", role="round-robin arbitration"),
        ],
        interconnects=[
            "controller drives ctrl_enable and ctrl_valid into datapath",
            "datapath output can feed fifo din",
            "fifo dout can feed later processing",
            "arbiter grant feeds controller request handling",
        ],
    )


def assert_contains(text: str, needle: str, label: str) -> None:
    if needle not in text:
        raise AssertionError(f"Missing {label}: {needle}")


def main() -> None:
    output_dir = PROJECT_ROOT / "data" / "harness_out"

    if output_dir.exists():
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    summary = build_sample_multi_module_summary()
    skill = VerilogTemplateSkill(ContextManager())
    result = skill._build_template(summary)

    for module in result.modules:
        (output_dir / module.file_name).write_text(module.verilog_code + "\n", encoding="utf-8")

    expected_files = {
        "system_top.v",
        "controller.v",
        "datapath.v",
        "fifo.v",
        "arbiter.v",
    }
    actual_files = {path.name for path in output_dir.glob("*.v")}
    if expected_files != actual_files:
        raise AssertionError(f"Generated files mismatch. expected={expected_files} actual={actual_files}")

    top_text = (output_dir / "system_top.v").read_text(encoding="utf-8")
    controller_text = (output_dir / "controller.v").read_text(encoding="utf-8")
    datapath_text = (output_dir / "datapath.v").read_text(encoding="utf-8")
    fifo_text = (output_dir / "fifo.v").read_text(encoding="utf-8")
    arbiter_text = (output_dir / "arbiter.v").read_text(encoding="utf-8")

    assert_contains(top_text, "controller u_controller", "controller instance")
    assert_contains(top_text, "datapath u_datapath", "datapath instance")
    assert_contains(top_text, "fifo u_fifo", "fifo instance")
    assert_contains(top_text, "arbiter u_arbiter", "arbiter instance")
    assert_contains(top_text, "wire ctrl_enable;", "top control wire")
    assert_contains(controller_text, "localparam ISSUE = 2'd1;", "controller FSM state")
    assert_contains(controller_text, "reg [1:0] state;", "controller state register")
    assert_contains(datapath_text, "ctrl_enable", "datapath control port")
    assert_contains(datapath_text, "data_out = data_in;", "datapath update expression")
    assert_contains(fifo_text, "localparam DEPTH = 16;", "fifo depth")
    assert_contains(fifo_text, "reg [31:0] mem [0:DEPTH-1];", "fifo memory")
    assert_contains(arbiter_text, "reg [3:0] mask;", "arbiter mask register")
    assert_contains(arbiter_text, "grant_next", "arbiter next grant")

    print("Harness regression passed.")
    for file_name in sorted(actual_files):
        print(file_name)


if __name__ == "__main__":
    main()
