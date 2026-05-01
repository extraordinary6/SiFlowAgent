// Reference 8-bit synchronous up-counter with active-low reset.
// Used as the DUT for the cocotb counter test (Phase 10A).
// Mirrors the contract that harness/tb/counter_tb.v exercises.
module counter (
    input  wire       clk,
    input  wire       rst_n,
    output reg  [7:0] cnt
);

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n)
            cnt <= 8'd0;
        else
            cnt <= cnt + 8'd1;
    end

endmodule
