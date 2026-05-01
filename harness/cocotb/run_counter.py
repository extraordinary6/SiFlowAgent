"""Standalone runner for the cocotb counter test (Phase 10A).

Builds and runs harness/cocotb/test_counter_cocotb.py against
harness/cocotb/counter.v using the Icarus Verilog backend.

Usage:
    D:/anaconda/envs/pytorch/python.exe harness/cocotb/run_counter.py

Exit code is non-zero if any cocotb test fails or the simulator errors out,
so the script can be wired into CI later.
"""
from __future__ import annotations

import sys
from pathlib import Path

from cocotb_tools.runner import get_runner


HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parent.parent

VERILOG_SOURCES = [HERE / "counter.v"]
HDL_TOPLEVEL = "counter"
TEST_MODULE = "test_counter_cocotb"
SIM = "icarus"


def main() -> int:
    runner = get_runner(SIM)

    runner.build(
        verilog_sources=VERILOG_SOURCES,
        hdl_toplevel=HDL_TOPLEVEL,
        build_dir=str(PROJECT_ROOT / "data" / "cocotb_build"),
        timescale=("1ns", "1ps"),
        always=True,
    )

    runner.test(
        hdl_toplevel=HDL_TOPLEVEL,
        test_module=TEST_MODULE,
        test_dir=str(HERE),
        build_dir=str(PROJECT_ROOT / "data" / "cocotb_build"),
        timescale=("1ns", "1ps"),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
