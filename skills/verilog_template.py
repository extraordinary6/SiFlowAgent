from __future__ import annotations

from pydantic import BaseModel, Field

from context.manager import ContextManager
from skills.base import BaseSkill
from skills.spec_summary import SignalSummary, SpecSummaryResult, SubmoduleSummary


class VerilogModuleFile(BaseModel):
    module_name: str = Field(...)
    file_name: str = Field(...)
    verilog_code: str = Field(default="")


class VerilogTemplateResult(BaseModel):
    module_name: str = Field(...)
    port_declarations: list[str] = Field(default_factory=list)
    body_lines: list[str] = Field(default_factory=list)
    verilog_code: str = Field(default="")
    modules: list[VerilogModuleFile] = Field(default_factory=list)


class VerilogTemplateSkill(BaseSkill):
    def __init__(self, context_manager: ContextManager) -> None:
        super().__init__(
            name="verilog_template",
            description="Generate a Verilog module skeleton from a structured spec summary",
        )
        self.context_manager = context_manager

    async def execute(self, **kwargs) -> VerilogTemplateResult:
        spec_summary = kwargs.get("spec_summary")
        if not spec_summary:
            raise ValueError("spec_summary is required")

        result = self._build_template(SpecSummaryResult(**spec_summary))
        self.context_manager.add_message("assistant", result.verilog_code)
        self.context_manager.set_state("last_skill", self.name)
        self.context_manager.set_state("last_verilog_template", result.model_dump())
        return result

    def _build_template(self, summary: SpecSummaryResult) -> VerilogTemplateResult:
        top_module = self._build_single_module(summary)
        modules = [
            VerilogModuleFile(
                module_name=top_module.module_name,
                file_name=f"{top_module.module_name}.v",
                verilog_code=top_module.verilog_code,
            )
        ]

        if summary.submodules:
            top_module = self._build_top_module_with_submodules(summary)
            modules = [
                VerilogModuleFile(
                    module_name=top_module.module_name,
                    file_name=f"{top_module.module_name}.v",
                    verilog_code=top_module.verilog_code,
                )
            ]
            modules.extend(self._build_submodule_files(summary))

        return VerilogTemplateResult(
            module_name=top_module.module_name,
            port_declarations=top_module.port_declarations,
            body_lines=top_module.body_lines,
            verilog_code=top_module.verilog_code,
            modules=modules,
        )

    def _build_single_module(self, summary: SpecSummaryResult) -> VerilogTemplateResult:
        module_name = summary.module_name or "unnamed_module"
        output_kinds = self._infer_output_kinds(summary)
        port_declarations = [
            self._format_port(signal, output_kinds.get(signal.name, "comb")) for signal in summary.interfaces
        ] or ["input wire clk"]
        body_lines = self._infer_body(summary, output_kinds)
        verilog_code = self._render_module(module_name, port_declarations, body_lines)
        return VerilogTemplateResult(
            module_name=module_name,
            port_declarations=port_declarations,
            body_lines=body_lines,
            verilog_code=verilog_code,
        )

    def _build_top_module_with_submodules(self, summary: SpecSummaryResult) -> VerilogTemplateResult:
        module_name = summary.module_name or "top"
        port_declarations = [self._format_top_port(signal) for signal in summary.interfaces] or ["input wire clk"]
        body_lines = self._infer_top_body(summary)
        verilog_code = self._render_module(module_name, port_declarations, body_lines)
        return VerilogTemplateResult(
            module_name=module_name,
            port_declarations=port_declarations,
            body_lines=body_lines,
            verilog_code=verilog_code,
        )

    def _render_module(self, module_name: str, port_declarations: list[str], body_lines: list[str]) -> str:
        verilog_lines = [f"module {module_name} ("]
        for index, declaration in enumerate(port_declarations):
            suffix = "," if index < len(port_declarations) - 1 else ""
            verilog_lines.append(f"    {declaration}{suffix}")
        verilog_lines.append(");")
        verilog_lines.append("")
        verilog_lines.extend(f"    {line}" if line else "" for line in body_lines)
        verilog_lines.append("")
        verilog_lines.append("endmodule")
        return "\n".join(verilog_lines).strip()

    def _build_submodule_files(self, summary: SpecSummaryResult) -> list[VerilogModuleFile]:
        files: list[VerilogModuleFile] = []
        for submodule in summary.submodules:
            module_name = submodule.name.strip() or "unnamed_submodule"
            ports = self._infer_submodule_ports(submodule, summary)
            output_kinds = self._infer_signal_kinds_from_ports(ports)
            port_declarations = [
                self._format_port(signal, output_kinds.get(signal.name, "comb")) for signal in ports
            ]
            body_lines = self._infer_submodule_body(submodule, ports)
            verilog_code = self._render_module(module_name, port_declarations, body_lines)
            files.append(
                VerilogModuleFile(
                    module_name=module_name,
                    file_name=f"{module_name}.v",
                    verilog_code=verilog_code,
                )
            )
        return files

    def _infer_top_body(self, summary: SpecSummaryResult) -> list[str]:
        top_inputs = {signal.name: signal for signal in summary.interfaces if signal.direction == "input"}
        top_outputs = {signal.name: signal for signal in summary.interfaces if signal.direction == "output"}
        top_signals = {signal.name: signal for signal in summary.interfaces}
        wire_map: dict[str, str] = {}
        instance_blocks: list[list[str]] = []

        for submodule in summary.submodules:
            ports = self._infer_submodule_ports(submodule, summary)
            bindings = self._build_submodule_bindings(submodule, ports, top_inputs, top_outputs)
            for signal in ports:
                bound_name = bindings[signal.name]
                if bound_name not in top_signals and signal.direction in {"input", "output", "inout"}:
                    wire_map.setdefault(bound_name, signal.width)
            instance_blocks.append(self._build_submodule_instance(submodule, ports, bindings))

        lines: list[str] = []
        if wire_map:
            lines.append("// Internal interconnects")
            for name in sorted(wire_map):
                width_part = self._normalize_width(wire_map[name])
                lines.append(f"wire{width_part} {name};")
            lines.append("")

        if summary.interconnects:
            lines.append("// Interconnect intent from spec")
            for item in summary.interconnects:
                lines.append(f"// {item}")
            lines.append("")

        for index, block in enumerate(instance_blocks):
            lines.extend(block)
            if index < len(instance_blocks) - 1:
                lines.append("")

        if lines:
            lines.append("")
            lines.append("// TODO: refine top-level wiring and module partitioning")

        while lines and not lines[-1]:
            lines.pop()
        return lines or ["// TODO: implement top-level integration logic"]

    def _build_submodule_instance(
        self,
        submodule: SubmoduleSummary,
        ports: list[SignalSummary],
        bindings: dict[str, str],
    ) -> list[str]:
        instance_name = f"u_{submodule.name}"
        lines = [f"{submodule.name} {instance_name} ("]
        for index, port in enumerate(ports):
            suffix = "," if index < len(ports) - 1 else ""
            lines.append(f"    .{port.name}({bindings[port.name]}){suffix}")
        lines.append(");")
        return lines

    def _build_submodule_bindings(
        self,
        submodule: SubmoduleSummary,
        ports: list[SignalSummary],
        top_inputs: dict[str, SignalSummary],
        top_outputs: dict[str, SignalSummary],
    ) -> dict[str, str]:
        role = self._role_key(submodule)
        bindings: dict[str, str] = {}
        for signal in ports:
            name = signal.name
            if name in top_inputs or name in top_outputs:
                bindings[name] = name
                continue
            if signal.direction == "output" and name in top_outputs:
                bindings[name] = name
                continue
            if role == "controller" and name == "ctrl_enable":
                bindings[name] = "ctrl_enable"
                continue
            if role == "controller" and name == "ctrl_valid":
                bindings[name] = "ctrl_valid"
                continue
            if role == "datapath" and name in {"ctrl_enable", "ctrl_valid"}:
                bindings[name] = name
                continue
            if role == "arbiter" and name == "grant":
                bindings[name] = "grant"
                continue
            bindings[name] = f"{submodule.name}_{name}"
        return bindings

    def _infer_submodule_ports(self, submodule: SubmoduleSummary, summary: SpecSummaryResult) -> list[SignalSummary]:
        role = self._role_key(submodule)
        top_inputs = [signal for signal in summary.interfaces if signal.direction == "input"]
        top_outputs = [signal for signal in summary.interfaces if signal.direction == "output"]
        clock_signal = self._pick_signal(top_inputs, ["clk", "clock"])
        reset_signal = self._pick_signal(top_inputs, ["rst_n", "reset_n", "rst", "reset"])

        ports: list[SignalSummary] = []
        if clock_signal:
            ports.append(self._clone_signal(clock_signal))
        if reset_signal:
            ports.append(self._clone_signal(reset_signal))

        if role == "controller":
            ports.extend(self._pick_existing_signals(top_inputs, ["start", "valid_in", "enable", "req"]))
            if any(signal.name == "busy" for signal in top_outputs):
                ports.append(self._clone_signal(top_outputs[[signal.name for signal in top_outputs].index("busy")]))
            else:
                ports.append(self._make_signal("busy", "output", "1", "controller busy indication"))
            ports.append(self._make_signal("ctrl_enable", "output", "1", "datapath enable control"))
            ports.append(self._make_signal("ctrl_valid", "output", "1", "control valid handshake"))
        elif role == "datapath":
            data_inputs = [
                self._clone_signal(signal)
                for signal in top_inputs
                if signal.name not in self._names_of(ports)
            ]
            ports.extend(data_inputs)
            ports.append(self._make_signal("ctrl_enable", "input", "1", "controller enable input"))
            ports.append(self._make_signal("ctrl_valid", "input", "1", "controller valid input"))
            datapath_outputs = [
                self._clone_signal(signal)
                for signal in top_outputs
                if signal.name not in {"busy", "done", "error", "ready", "valid"}
            ]
            ports.extend(datapath_outputs or [self._make_signal("data_out", "output", "32", "datapath result")])
        elif role == "fifo":
            fifo_ports = [
                self._make_signal("wr_en", "input", "1", "fifo write enable"),
                self._make_signal("rd_en", "input", "1", "fifo read enable"),
                self._make_signal("din", "input", self._guess_data_width(summary), "fifo write data"),
                self._make_signal("dout", "output", self._guess_data_width(summary), "fifo read data"),
                self._make_signal("full", "output", "1", "fifo full flag"),
                self._make_signal("empty", "output", "1", "fifo empty flag"),
            ]
            ports.extend(fifo_ports)
        elif role == "arbiter":
            ports.extend(
                [
                    self._make_signal("req", "input", self._guess_request_width(summary), "request bus"),
                    self._make_signal("grant", "output", self._guess_request_width(summary), "grant bus"),
                ]
            )
        else:
            ports.extend(
                [
                    self._clone_signal(signal)
                    for signal in top_inputs + top_outputs
                    if signal.name not in self._names_of(ports)
                ]
            )

        return self._dedupe_ports(ports)

    def _infer_submodule_body(self, submodule: SubmoduleSummary, ports: list[SignalSummary]) -> list[str]:
        role = self._role_key(submodule)
        input_names = {signal.name for signal in ports if signal.direction == "input"}
        output_names = [signal.name for signal in ports if signal.direction == "output"]
        clock_name = self._find_preferred_signal(input_names, ["clk", "clock"])
        reset_name = self._find_preferred_signal(input_names, ["rst_n", "reset_n", "rst", "reset"])
        active_low_reset = bool(reset_name and reset_name.endswith("_n"))

        if role == "controller":
            return self._build_controller_body(submodule, ports, clock_name, reset_name, active_low_reset)
        if role == "datapath":
            return self._build_datapath_body(submodule, ports, clock_name, reset_name, active_low_reset)
        if role == "fifo":
            return self._build_fifo_body(submodule, ports, clock_name, reset_name, active_low_reset)
        if role == "arbiter":
            return self._build_arbiter_body(submodule, ports, clock_name, reset_name, active_low_reset)

        output_kinds = self._infer_signal_kinds_from_ports(ports)
        lines: list[str] = []
        lines.append(f"// Role: {submodule.role}" if submodule.role else f"// TODO: implement {submodule.name}")
        lines.append("// Template: generic submodule")
        lines.append("")

        for index, output_name in enumerate(output_names):
            output_kind = output_kinds.get(output_name, "comb")
            if output_kind == "seq":
                lines.extend(self._build_sequential_block(output_name, clock_name, reset_name, active_low_reset))
            else:
                lines.extend(self._build_combinational_block(output_name))
            if index < len(output_names) - 1:
                lines.append("")

        return lines or ["// TODO: implement submodule logic"]

    def _build_controller_body(
        self,
        submodule: SubmoduleSummary,
        ports: list[SignalSummary],
        clock_name: str | None,
        reset_name: str | None,
        active_low_reset: bool,
    ) -> list[str]:
        output_names = [signal.name for signal in ports if signal.direction == "output"]
        trigger_name = self._find_preferred_signal(
            {signal.name for signal in ports if signal.direction == "input"},
            ["start", "valid_in", "enable", "req"],
        )
        lines = [
            f"// Role: {submodule.role}" if submodule.role else "// Template: controller",
            "localparam IDLE = 2'd0;",
            "localparam ISSUE = 2'd1;",
            "localparam WAIT_DONE = 2'd2;",
            "reg [1:0] state;",
            "reg [1:0] next_state;",
            "",
            "always @(*) begin",
            "    next_state = state;",
            "    case (state)",
            "        IDLE: begin",
            f"            if ({trigger_name or '1\'b0'}) begin",
            "                next_state = ISSUE;",
            "            end",
            "        end",
            "        ISSUE: begin",
            "            next_state = WAIT_DONE;",
            "        end",
            "        WAIT_DONE: begin",
            "            next_state = IDLE;",
            "        end",
            "        default: begin",
            "            next_state = IDLE;",
            "        end",
            "    endcase",
            "end",
            "",
        ]
        lines.extend(self._build_register_block("state", clock_name, reset_name, active_low_reset, "next_state"))

        for output_name in output_names:
            lines.append("")
            if output_name in {"busy", "ctrl_valid", "ctrl_enable"}:
                lines.extend(
                    [
                        "always @(*) begin",
                        f"    {output_name} = 1'b0;",
                        "    case (state)",
                        "        ISSUE: begin",
                        f"            {output_name} = 1'b1;",
                        "        end",
                        "        WAIT_DONE: begin",
                        f"            {output_name} = 1'b1;",
                        "        end",
                        "    endcase",
                        "end",
                    ]
                )
            else:
                lines.extend(self._build_combinational_block(output_name))
        return lines

    def _build_datapath_body(
        self,
        submodule: SubmoduleSummary,
        ports: list[SignalSummary],
        clock_name: str | None,
        reset_name: str | None,
        active_low_reset: bool,
    ) -> list[str]:
        output_names = [signal.name for signal in ports if signal.direction == "output"]
        input_names = {signal.name for signal in ports if signal.direction == "input"}
        enable_name = self._find_preferred_signal(input_names, ["ctrl_enable", "enable", "valid_in", "ctrl_valid"])
        data_input = self._find_preferred_signal(input_names, ["data_in", "packet_len", "din"])
        lines = [
            f"// Role: {submodule.role}" if submodule.role else "// Template: datapath",
            "// Datapath outputs update when control enables a transaction.",
        ]
        for index, output_name in enumerate(output_names):
            lines.append("")
            if self._looks_sequential(output_name.lower(), output_name.lower(), output_name.lower()):
                lines.extend(
                    self._build_update_when_enabled_block(
                        output_name,
                        enable_name,
                        clock_name,
                        reset_name,
                        active_low_reset,
                        update_expr=self._guess_datapath_update_expr(output_name, data_input),
                    )
                )
            else:
                lines.extend(
                    [
                        "always @(*) begin",
                        f"    {output_name} = '0;",
                        f"    if ({enable_name or '1\'b0'}) begin",
                        f"        {output_name} = {self._guess_datapath_comb_expr(output_name, data_input)};",
                        "    end",
                        "end",
                    ]
                )
            if index < len(output_names) - 1:
                lines.append("")
        return lines

    def _build_fifo_body(
        self,
        submodule: SubmoduleSummary,
        ports: list[SignalSummary],
        clock_name: str | None,
        reset_name: str | None,
        active_low_reset: bool,
    ) -> list[str]:
        data_width = self._normalize_width(self._port_width(ports, "din", "32"))
        dout_is_seq = self._port_is_sequential(ports, "dout")
        lines = [
            f"// Role: {submodule.role}" if submodule.role else "// Template: fifo",
            "localparam DEPTH = 16;",
            f"reg{data_width} mem [0:DEPTH-1];",
            "reg [3:0] wr_ptr;",
            "reg [3:0] rd_ptr;",
            "reg [4:0] count;",
            "",
        ]
        lines.extend(
            [
                f"always @({self._build_sensitivity(clock_name, reset_name, active_low_reset)}) begin",
                f"    if ({self._reset_condition(reset_name, active_low_reset)}) begin" if reset_name else "    begin",
                "        wr_ptr <= '0;",
                "        rd_ptr <= '0;",
                "        count <= '0;",
                "    end else begin",
                "        if (wr_en && !full) begin",
                "            mem[wr_ptr] <= din;",
                "            wr_ptr <= wr_ptr + 1'b1;",
                "        end",
                "        if (rd_en && !empty) begin",
                "            rd_ptr <= rd_ptr + 1'b1;",
                "        end",
                "        case ({wr_en && !full, rd_en && !empty})",
                "            2'b10: count <= count + 1'b1;",
                "            2'b01: count <= count - 1'b1;",
                "            default: count <= count;",
                "        endcase",
                "    end",
                "end",
                "",
            ]
        )
        if dout_is_seq and clock_name:
            lines.extend(
                [
                    f"always @({self._build_sensitivity(clock_name, reset_name, active_low_reset)}) begin",
                    f"    if ({self._reset_condition(reset_name, active_low_reset)}) begin" if reset_name else "    begin",
                    "        dout <= '0;",
                    "    end else begin",
                    "        if (rd_en && !empty) begin",
                    "            dout <= mem[rd_ptr];",
                    "        end",
                    "    end",
                    "end",
                    "",
                ]
            )
        else:
            lines.extend(
                [
                    "always @(*) begin",
                    "    dout = mem[rd_ptr];",
                    "end",
                    "",
                ]
            )
        lines.extend(
            [
                "always @(*) begin",
                "    full = (count == DEPTH);",
                "end",
                "",
                "always @(*) begin",
                "    empty = (count == 0);",
                "end",
            ]
        )
        return lines

    def _build_arbiter_body(
        self,
        submodule: SubmoduleSummary,
        ports: list[SignalSummary],
        clock_name: str | None,
        reset_name: str | None,
        active_low_reset: bool,
    ) -> list[str]:
        req_width = self._normalize_width(self._port_width(ports, "req", "4")) or " [3:0]"
        grant_is_seq = self._port_is_sequential(ports, "grant")
        lines = [
            f"// Role: {submodule.role}" if submodule.role else "// Template: arbiter",
            f"reg{req_width} mask;",
            f"reg{req_width} grant_next;" if grant_is_seq else "",
            "",
            "always @(*) begin",
            "    grant_next = '0;" if grant_is_seq else "    grant = '0;",
            "    if ((req & ~mask) != '0) begin",
            "        if ((req & ~mask)[0]) begin",
            "            grant_next[0] = 1'b1;" if grant_is_seq else "            grant[0] = 1'b1;",
            "        end else if ((req & ~mask)[1]) begin",
            "            grant_next[1] = 1'b1;" if grant_is_seq else "            grant[1] = 1'b1;",
            "        end else if ((req & ~mask)[2]) begin",
            "            grant_next[2] = 1'b1;" if grant_is_seq else "            grant[2] = 1'b1;",
            "        end else if ((req & ~mask)[3]) begin",
            "            grant_next[3] = 1'b1;" if grant_is_seq else "            grant[3] = 1'b1;",
            "        end",
            "    end else begin",
            "        if (req[0]) begin",
            "            grant_next[0] = 1'b1;" if grant_is_seq else "            grant[0] = 1'b1;",
            "        end else if (req[1]) begin",
            "            grant_next[1] = 1'b1;" if grant_is_seq else "            grant[1] = 1'b1;",
            "        end else if (req[2]) begin",
            "            grant_next[2] = 1'b1;" if grant_is_seq else "            grant[2] = 1'b1;",
            "        end else if (req[3]) begin",
            "            grant_next[3] = 1'b1;" if grant_is_seq else "            grant[3] = 1'b1;",
            "        end",
            "    end",
            "end",
        ]
        lines = [line for line in lines if line != ""]
        if grant_is_seq and clock_name:
            lines.append("")
            lines.extend(self._build_register_block("grant", clock_name, reset_name, active_low_reset, "grant_next"))
        if clock_name:
            lines.append("")
            lines.extend(self._build_register_block("mask", clock_name, reset_name, active_low_reset, "grant_next" if grant_is_seq else "grant"))
        return lines

    def _build_register_block(
        self,
        reg_name: str,
        clock_name: str | None,
        reset_name: str | None,
        active_low_reset: bool,
        next_value: str,
    ) -> list[str]:
        if not clock_name:
            return [
                "always @(*) begin",
                f"    {reg_name} = {next_value};",
                "end",
            ]
        lines = [f"always @({self._build_sensitivity(clock_name, reset_name, active_low_reset)}) begin"]
        if reset_name:
            lines.append(f"    if ({self._reset_condition(reset_name, active_low_reset)}) begin")
            lines.append(f"        {reg_name} <= '0;")
            lines.append("    end else begin")
            lines.append(f"        {reg_name} <= {next_value};")
            lines.append("    end")
        else:
            lines.append(f"    {reg_name} <= {next_value};")
        lines.append("end")
        return lines

    def _build_update_when_enabled_block(
        self,
        output_name: str,
        enable_name: str | None,
        clock_name: str | None,
        reset_name: str | None,
        active_low_reset: bool,
        update_expr: str | None = None,
    ) -> list[str]:
        if not clock_name:
            return self._build_combinational_block(output_name)
        expr = update_expr or f"// TODO: update {output_name}"
        lines = [f"always @({self._build_sensitivity(clock_name, reset_name, active_low_reset)}) begin"]
        if reset_name:
            lines.append(f"    if ({self._reset_condition(reset_name, active_low_reset)}) begin")
            lines.append(f"        {output_name} <= '0;")
            lines.append("    end else begin")
            lines.append(f"        if ({enable_name or '1\'b0'}) begin")
            if expr.startswith("//"):
                lines.append(f"            {expr}")
            else:
                lines.append(f"            {output_name} <= {expr};")
            lines.append("        end")
            lines.append("    end")
        else:
            lines.append(f"    if ({enable_name or '1\'b0'}) begin")
            if expr.startswith("//"):
                lines.append(f"        {expr}")
            else:
                lines.append(f"        {output_name} <= {expr};")
            lines.append("    end")
        lines.append("end")
        return lines

    def _build_sensitivity(self, clock_name: str | None, reset_name: str | None, active_low_reset: bool) -> str:
        if not clock_name:
            return "*"
        sensitivity = f"posedge {clock_name}"
        if reset_name:
            reset_edge = "negedge" if active_low_reset else "posedge"
            sensitivity = f"{sensitivity} or {reset_edge} {reset_name}"
        return sensitivity

    def _reset_condition(self, reset_name: str | None, active_low_reset: bool) -> str:
        if not reset_name:
            return "1'b0"
        return f"!{reset_name}" if active_low_reset else reset_name

    def _port_width(self, ports: list[SignalSummary], name: str, default: str) -> str:
        for signal in ports:
            if signal.name == name:
                return signal.width
        return default

    def _port_is_sequential(self, ports: list[SignalSummary], name: str) -> bool:
        for signal in ports:
            if signal.name == name:
                signal_text = f"{signal.name.lower()} {signal.description.lower()}"
                return self._looks_sequential(signal_text, signal_text, signal_text)
        return False

    def _guess_datapath_update_expr(self, output_name: str, data_input: str | None) -> str:
        name = output_name.lower()
        source = data_input or output_name
        if "count" in name or "counter" in name:
            return f"{output_name} + 1'b1"
        if "sum" in name or "acc" in name:
            return f"{output_name} + {source}"
        if source != output_name:
            return source
        return f"// TODO: update {output_name}"

    def _guess_datapath_comb_expr(self, output_name: str, data_input: str | None) -> str:
        source = data_input or "'0"
        name = output_name.lower()
        if "valid" in name:
            return "1'b1"
        if "ready" in name:
            return "1'b1"
        return source

    def _format_top_port(self, signal: SignalSummary) -> str:
        direction = signal.direction if signal.direction in {"input", "output", "inout"} else "input"
        width_part = self._normalize_width(signal.width)
        return f"{direction} wire{width_part} {signal.name}"

    def _format_port(self, signal: SignalSummary, output_kind: str) -> str:
        direction = signal.direction if signal.direction in {"input", "output", "inout"} else "input"
        width_part = self._normalize_width(signal.width)
        if direction == "output":
            net_type = "reg" if output_kind == "seq" else "wire"
            return f"{direction} {net_type}{width_part} {signal.name}"
        return f"{direction} wire{width_part} {signal.name}"

    def _normalize_width(self, width: str) -> str:
        normalized = width.strip()
        if not normalized or normalized == "1":
            return ""
        if normalized.startswith("[") and normalized.endswith("]"):
            inner = normalized[1:-1].strip()
            if inner.isdigit():
                value = int(inner)
                return "" if value <= 1 else f" [{value - 1}:0]"
            return f" [{inner}]"
        if normalized.isdigit():
            value = int(normalized)
            return "" if value <= 1 else f" [{value - 1}:0]"
        return f" [{normalized}]"

    def _infer_body(self, summary: SpecSummaryResult, output_kinds: dict[str, str]) -> list[str]:
        behavior_text = " ".join(summary.functional_behavior).lower()
        timing_text = " ".join(summary.timing_and_control).lower()
        input_names = {signal.name for signal in summary.interfaces if signal.direction == "input"}
        output_names = [signal.name for signal in summary.interfaces if signal.direction == "output"]

        if not output_names:
            return ["// TODO: implement module logic"]

        clock_name = self._find_preferred_signal(input_names, ["clk", "clock"])
        reset_name = self._find_preferred_signal(input_names, ["rst_n", "reset_n", "rst", "reset"])
        active_low_reset = bool(reset_name and reset_name.endswith("_n"))
        has_seq_context = bool(
            clock_name
            and (
                "rising edge" in timing_text
                or "falling edge" in timing_text
                or "posedge" in timing_text
                or "negedge" in timing_text
                or "synchronous" in behavior_text
                or "clocked" in behavior_text
                or "register" in behavior_text
            )
        )

        lines: list[str] = []
        for index, output_name in enumerate(output_names):
            output_kind = output_kinds.get(output_name, "seq" if has_seq_context else "comb")
            if output_kind == "seq":
                lines.extend(self._build_sequential_block(output_name, clock_name, reset_name, active_low_reset))
            else:
                lines.extend(self._build_combinational_block(output_name))
            if index < len(output_names) - 1:
                lines.append("")

        return lines or ["// TODO: implement module logic"]

    def _infer_output_kinds(self, summary: SpecSummaryResult) -> dict[str, str]:
        behavior_text = " ".join(summary.functional_behavior).lower()
        timing_text = " ".join(summary.timing_and_control).lower()
        input_names = {signal.name for signal in summary.interfaces if signal.direction == "input"}
        clock_name = self._find_preferred_signal(input_names, ["clk", "clock"])
        has_seq_context = bool(
            clock_name
            and (
                "rising edge" in timing_text
                or "falling edge" in timing_text
                or "posedge" in timing_text
                or "negedge" in timing_text
                or "synchronous" in behavior_text
                or "clocked" in behavior_text
                or "register" in behavior_text
                or "latched" in behavior_text
                or "state" in behavior_text
                or "counter" in behavior_text
            )
        )

        result: dict[str, str] = {}
        for signal in summary.interfaces:
            if signal.direction != "output":
                continue

            signal_text = f"{signal.name.lower()} {signal.description.lower()}"
            if self._looks_sequential(signal_text, behavior_text, timing_text):
                result[signal.name] = "seq"
            elif self._looks_combinational(signal_text):
                result[signal.name] = "comb"
            else:
                result[signal.name] = "seq" if has_seq_context else "comb"

        return result

    def _infer_signal_kinds_from_ports(self, ports: list[SignalSummary]) -> dict[str, str]:
        result: dict[str, str] = {}
        for signal in ports:
            if signal.direction != "output":
                continue
            signal_text = f"{signal.name.lower()} {signal.description.lower()}"
            result[signal.name] = "seq" if self._looks_sequential(signal_text, signal_text, signal_text) else "comb"
        return result

    def _looks_sequential(self, signal_text: str, behavior_text: str, timing_text: str) -> bool:
        seq_keywords = [
            "register",
            "registered",
            "latch",
            "latched",
            "counter",
            "count",
            "state",
            "stored",
            "sticky",
            "pulse",
            "one cycle",
            "next cycle",
            "edge",
            "clock",
            "posedge",
            "negedge",
            "synchronous",
            "busy",
            "grant",
        ]
        text = f"{signal_text} {behavior_text} {timing_text}"
        return any(keyword in text for keyword in seq_keywords)

    def _looks_combinational(self, signal_text: str) -> bool:
        comb_keywords = [
            "combinational",
            "decode",
            "decoded",
            "select",
            "selected",
            "mux",
            "compare",
            "match",
            "direct",
            "immediate",
            "ctrl_",
            "full",
            "empty",
        ]
        return any(keyword in signal_text for keyword in comb_keywords)

    def _build_sequential_block(
        self,
        output_name: str,
        clock_name: str | None,
        reset_name: str | None,
        active_low_reset: bool,
    ) -> list[str]:
        if not clock_name:
            return [
                "always @(*) begin",
                f"    // TODO: {output_name} looks sequential but no clock was identified",
                f"    {output_name} = '0;",
                "end",
            ]

        sensitivity = f"posedge {clock_name}"
        if reset_name:
            reset_edge = "negedge" if active_low_reset else "posedge"
            sensitivity = f"{sensitivity} or {reset_edge} {reset_name}"

        lines = [f"always @({sensitivity}) begin"]
        if reset_name:
            reset_condition = f"!{reset_name}" if active_low_reset else reset_name
            lines.append(f"    if ({reset_condition}) begin")
            lines.append(f"        {output_name} <= '0;")
            lines.append("    end else begin")
            lines.append(f"        // TODO: update {output_name}")
            lines.append("    end")
        else:
            lines.append(f"    // TODO: update {output_name}")
        lines.append("end")
        return lines

    def _build_combinational_block(self, output_name: str) -> list[str]:
        return [
            "always @(*) begin",
            f"    {output_name} = '0;",
            f"    // TODO: drive {output_name} combinationally",
            "end",
        ]

    def _role_key(self, submodule: SubmoduleSummary) -> str:
        text = f"{submodule.name} {submodule.role}".lower()
        if "controller" in text or "control" in text:
            return "controller"
        if "datapath" in text or "data path" in text:
            return "datapath"
        if "fifo" in text or "queue" in text or "buffer" in text:
            return "fifo"
        if "arbiter" in text or "arbitration" in text:
            return "arbiter"
        return "generic"

    def _pick_signal(self, signals: list[SignalSummary], candidates: list[str]) -> SignalSummary | None:
        lower_map = {signal.name.lower(): signal for signal in signals}
        for candidate in candidates:
            if candidate in lower_map:
                return lower_map[candidate]
        return None

    def _pick_existing_signals(self, signals: list[SignalSummary], candidates: list[str]) -> list[SignalSummary]:
        result: list[SignalSummary] = []
        lower_map = {signal.name.lower(): signal for signal in signals}
        for candidate in candidates:
            if candidate in lower_map:
                result.append(self._clone_signal(lower_map[candidate]))
        return result

    def _clone_signal(self, signal: SignalSummary) -> SignalSummary:
        return SignalSummary(
            name=signal.name,
            direction=signal.direction,
            width=signal.width,
            description=signal.description,
        )

    def _make_signal(self, name: str, direction: str, width: str, description: str) -> SignalSummary:
        return SignalSummary(name=name, direction=direction, width=width, description=description)

    def _dedupe_ports(self, ports: list[SignalSummary]) -> list[SignalSummary]:
        result: list[SignalSummary] = []
        seen: set[str] = set()
        for signal in ports:
            if signal.name in seen:
                continue
            seen.add(signal.name)
            result.append(signal)
        return result

    def _names_of(self, ports: list[SignalSummary]) -> set[str]:
        return {signal.name for signal in ports}

    def _guess_data_width(self, summary: SpecSummaryResult) -> str:
        for signal in summary.interfaces:
            if signal.direction == "input" and signal.width.strip() not in {"", "1"}:
                return signal.width
        for signal in summary.interfaces:
            if signal.direction == "output" and signal.width.strip() not in {"", "1"}:
                return signal.width
        return "32"

    def _guess_request_width(self, summary: SpecSummaryResult) -> str:
        for signal in summary.interfaces:
            if "req" in signal.name.lower() and signal.width.strip() not in {"", "1"}:
                return signal.width
        return "4"

    def _find_preferred_signal(self, names: set[str], candidates: list[str]) -> str | None:
        lower_map = {name.lower(): name for name in names}
        for candidate in candidates:
            if candidate in lower_map:
                return lower_map[candidate]
        return None
