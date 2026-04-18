`timescale 1ns/1ps

// Testbench for the `counter` scenario. Drives clk + active-low rst_n and
// verifies that cnt resets to 0 and increments by 1 every clock afterward.
// Emits TEST_PASS / TEST_FAIL plus per-check PASS / FAIL tokens so the
// eval harness can classify the verdict.
module counter_tb;
    reg clk;
    reg rst_n;
    wire [7:0] cnt;

    counter dut (
        .clk(clk),
        .rst_n(rst_n),
        .cnt(cnt)
    );

    // 100 MHz clock.
    initial clk = 1'b0;
    always #5 clk = ~clk;

    integer fails;
    integer checks;

    // Safety watchdog in case the DUT never leaves reset.
    initial begin
        #1000;
        $display("TEST_FAIL watchdog timeout, cnt=%0d", cnt);
        $finish;
    end

    initial begin
        fails  = 0;
        checks = 0;

        // Hold reset for several cycles, then release on a clean edge.
        rst_n = 1'b0;
        @(posedge clk);
        @(posedge clk);
        @(posedge clk);
        // Sample during reset: DUT must drive cnt to 0.
        if (cnt === 8'd0) begin
            $display("CHECK reset_zero PASS");
        end else begin
            $display("CHECK reset_zero FAIL got=%0d", cnt);
            fails = fails + 1;
        end
        checks = checks + 1;

        // Deassert reset synchronously with the clock edge.
        @(negedge clk);
        rst_n = 1'b1;

        // After three clock edges the counter should have advanced by 3.
        @(posedge clk);
        @(posedge clk);
        @(posedge clk);
        #1;
        if (cnt === 8'd3) begin
            $display("CHECK increment_three PASS");
        end else begin
            $display("CHECK increment_three FAIL got=%0d expected=3", cnt);
            fails = fails + 1;
        end
        checks = checks + 1;

        // After many more edges cnt should keep climbing (no freeze).
        repeat (10) @(posedge clk);
        #1;
        if (cnt > 8'd3) begin
            $display("CHECK still_incrementing PASS cnt=%0d", cnt);
        end else begin
            $display("CHECK still_incrementing FAIL cnt=%0d", cnt);
            fails = fails + 1;
        end
        checks = checks + 1;

        if (fails == 0) begin
            $display("TEST_PASS (%0d/%0d checks)", checks, checks);
        end else begin
            $display("TEST_FAIL (%0d/%0d checks failed)", fails, checks);
        end
        $finish;
    end
endmodule
