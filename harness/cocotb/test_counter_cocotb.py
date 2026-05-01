"""Cocotb port of harness/tb/counter_tb.v (Phase 10A).

Same DUT contract as the Verilog TB:
- 8-bit synchronous up-counter
- active-low rst_n
- cnt clears to 0 in reset, increments by 1 every posedge clk afterwards

Three test cases mirror the three checks emitted by counter_tb.v:
- reset_zero
- increment_three
- still_incrementing
"""
from __future__ import annotations

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import FallingEdge, RisingEdge, Timer


CLK_PERIOD_NS = 10  # 100 MHz, matches counter_tb.v


async def _start_clock(dut) -> None:
    cocotb.start_soon(Clock(dut.clk, CLK_PERIOD_NS, unit="ns").start())


async def _hold_reset(dut, cycles: int = 3) -> None:
    dut.rst_n.value = 0
    for _ in range(cycles):
        await RisingEdge(dut.clk)


@cocotb.test()
async def reset_zero(dut) -> None:
    """While rst_n is asserted, cnt must be driven to 0."""
    await _start_clock(dut)
    await _hold_reset(dut)
    assert int(dut.cnt.value) == 0, f"reset_zero: cnt={int(dut.cnt.value)}"


@cocotb.test()
async def increment_three(dut) -> None:
    """After releasing reset, cnt should advance by 1 each rising edge."""
    await _start_clock(dut)
    await _hold_reset(dut)

    # Deassert reset on a falling edge to keep setup/hold clean.
    await FallingEdge(dut.clk)
    dut.rst_n.value = 1

    for _ in range(3):
        await RisingEdge(dut.clk)
    await Timer(1, unit="ns")  # let combinational settle before sampling

    assert int(dut.cnt.value) == 3, f"increment_three: cnt={int(dut.cnt.value)}"


@cocotb.test()
async def still_incrementing(dut) -> None:
    """After many more cycles cnt must keep climbing (no freeze)."""
    await _start_clock(dut)
    await _hold_reset(dut)

    await FallingEdge(dut.clk)
    dut.rst_n.value = 1

    # Three edges to land on cnt==3, then ten more.
    for _ in range(13):
        await RisingEdge(dut.clk)
    await Timer(1, unit="ns")

    cnt = int(dut.cnt.value)
    assert cnt > 3, f"still_incrementing: cnt={cnt} did not advance past 3"
