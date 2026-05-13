#!/usr/bin/env python3
"""IR analysis

Read a kpex/magic PEX'd SPICE netlist, replace MOSFETs with hardcoded
current sources, .op via ngspice, find all relevant nodes, and print the largest-magnitude
voltage drops first.

Usage:
    ir_check.py <pex.spice> <subckt_name>
                [--vdd-port VDD]
                [--vdd 1.5] [--ion 50e-6]
                [--out-dir ./ir_out]
"""

import argparse
import subprocess
import sys
from collections import defaultdict
from pathlib import Path
from typing import List


def join_continuations(text):
    """SPICE logical lines: '+' continues previous; '*' is a comment."""
    buf = ""
    for raw in text.splitlines():
        s = raw.rstrip()
        if not s or s.lstrip().startswith("*"):
            if buf:
                yield buf
                buf = ""
            continue
        if s.lstrip().startswith("+"):
            buf += " " + s.lstrip()[1:].strip()
        else:
            if buf:
                yield buf
            buf = s
    if buf:
        yield buf

class DeviceInstance:
    def __init__(self, name, nodes, model):
        self.name = name
        self.nodes = nodes
        self.model = model
    
    def __repr__(self):
        return f"DeviceInstance(name={self.name}, nodes={self.nodes}, model={self.model})"

class SubcircuitWithParasitics:
    def __init__(self, name):
        self.name = name
        self.ports = []
        self.resistors = []
        self.devices = []
    
    def __repr__(self):
        return f"SubcircuitWithParasitics(name={self.name}, ports={self.ports}, Rs={self.resistors}, devices={self.devices})"

def parse_subckt(subckt_name, spice_path) -> SubcircuitWithParasitics:
    """Return a SubcircuitWithParasitics for the given subckt in the given SPICE netlist"""
    text = Path(spice_path).read_text()
    subckt = SubcircuitWithParasitics(subckt_name)
    in_sub = False
    for line in join_continuations(text):
        low = line.lower()
        if low.startswith(".subckt"):
            parts = line.split()
            if parts[1].lower() == subckt_name.lower():
                subckt.ports = parts[2:]
                in_sub = True
            continue
        if low.startswith(".ends"):
            in_sub = False
            continue
        if not in_sub:
            continue
        parts = line.split()
        if low.startswith("r") and len(parts) >= 4:
            subckt.resistors.append((parts[0], parts[1], parts[2], parts[3]))
        elif low.startswith("x"):
            name = parts[0]
            tokens = parts[1:]
            model_idx = next((i for i, t in enumerate(tokens) if "=" in t), len(tokens))
            model_idx -= 1
            nodes = tokens[:model_idx]
            model = tokens[model_idx]
            subckt.devices.append(DeviceInstance(name, nodes, model))
    return subckt


def nodes_for_op_analysis(subckt: SubcircuitWithParasitics):
    """Nodes that actually end up in the generated SPICE deck. After we
    replace each MOSFET with a current source between its drain and source,
    the gate and bulk nets disappear from the deck"""
    nodes = set(subckt.ports)
    for _, n1, n2, _ in subckt.resistors:
        nodes.add(n1)
        nodes.add(n2)
    for d in subckt.devices:
        if len(d.nodes) < 4:
            continue
        m = d.model.lower()
        if "pmos" in m or "nmos" in m:
            drain, _gate, source, _bulk = d.nodes[:4]
            nodes.add(drain)
            nodes.add(source)
    return sorted(nodes)


def generate_spice_op_analysis(subckt: SubcircuitWithParasitics, vdd_port, vdd_v, ion, data_path):
    """Generate a SPICE deck that will be used to compute node voltages:
    R network + I sources for each MOSFET + V supplies + .op, with results
    written to `data_path` as an ngspice wrdata table."""
    out = [
        "* IR analysis (auto-generated)",
        f"* VDD={vdd_v}V Ion={ion}A per MOSFET",
        "",
        "* Parasitic R network",
    ]
    for name, n1, n2, val in subckt.resistors:
        out.append(f"{name} {n1} {n2} {val}")
    out += ["", "* Devices -> fixed current sources"]
    counts = {"pmos": 0, "nmos": 0, "other": 0, "skipped": 0}
    for device in subckt.devices:
        if len(device.nodes) < 4:
            out.append(f"* {device.name}: skip (fewer than 4 nodes)")
            counts["skipped"] += 1
            continue
        d, g, s, b = device.nodes[:4]
        m = device.model.lower()
        if "pmos" in m:
            out.append(f"I_{device.name} {s} {d} DC {ion}")
            counts["pmos"] += 1
        elif "nmos" in m:
            out.append(f"I_{device.name} {d} {s} DC {ion}")
            counts["nmos"] += 1
        else:
            out.append(f"* {device.name}: unknown model '{device.model}', skipped")
            counts["other"] += 1
    out += [
        "",
        "* Supplies",
        f"V_VDD_SRC {vdd_port} 0 {vdd_v}",
        "",
        "* Solve operating point and dump every node voltage to a wrdata table.",
        ".control",
        "set wr_vecnames",         # 1st line of the data file: column names
        "set wr_singlescale",      # one X-axis column shared across all vars
        "op",
        # Explicit v(...) list — wrdata doesn't accept v(*) wildcards, and
        # only nodes that actually appear in the deck are valid vectors.
        "wrdata " + str(data_path) + " " + " ".join(f"v({n})" for n in nodes_for_op_analysis(subckt)),
        "quit",
        ".endc",
        ".end",
        "",
    ]
    return "\n".join(out), counts


def run_ngspice(deck_path):
    r = subprocess.run(
        ["ngspice", "-b", str(deck_path)],
        capture_output=True, text=True, check=False,
    )
    return r.stdout, r.stderr, r.returncode


def parse_wrdata(data_path):
    """Parse ngspice wrdata output (single .op row, with set wr_vecnames + set wr_singlescale).
    Returns {node_name: voltage}.

    Format:
        <x_label> v(node1) v(node2) ...
        <x_value> <val1>   <val2>  ...

    The first column is always the X-axis (point index, value 0 for .op);
    skip it. Voltage columns wear a v(...) wrapper that we strip.
    """
    lines = Path(data_path).read_text().strip().splitlines()
    if len(lines) < 2:
        raise ValueError(f"{data_path}: expected header + ≥1 data row, got {len(lines)}")
    header = lines[0].split()
    values = [float(x) for x in lines[1].split()]
    if len(header) != len(values):
        raise ValueError(
            f"{data_path}: header has {len(header)} cols, data row has {len(values)}"
        )
    voltages = {}
    # Skip the X-axis column (header[0]); decode v(...) wrappers on the rest.
    for col, val in zip(header[1:], values[1:]):
        if col.startswith("v(") and col.endswith(")"):
            voltages[col[2:-1]] = val
        else:
            voltages[col] = val
    return voltages


def find_terminal_nodes(subckt, start):
    """Find all terminal nodes connected to the power net `start`."""
    # We want to find all leaf nodes reachable from the supply port
    # So DFS and add the last node in the path to the output

    adj = defaultdict(set)
    for _, n1, n2, _ in subckt.resistors:
        adj[n1].add(n2)
        adj[n2].add(n1)
        
    visited = set()
    def dfs(n, path):
        visited.add(n)
        for nbr in adj[n]:
            if nbr not in visited:
                dfs(nbr, path + [nbr])

    dfs(start, [start])

    # visited now has all nodes reachable from the supply port.
    # of these nodes, we are only interested in the ones that appear
    # as device terminals (drain/source of MOSFETs, or any terminal of unmodelled devices).
    terminal_nodes = set()
    for d in subckt.devices:
        if len(d.nodes) < 4:
            # Unmodelled device: all its nodes are terminals of interest.
            for n in d.nodes:
                if n in visited:
                    terminal_nodes.add(n)
        else:
            m = d.model.lower()
            if "pmos" in m or "nmos" in m:
                drain, _gate, source, _bulk = d.nodes[:4]
                for n in (drain, source):
                    if n in visited:
                        terminal_nodes.add(n)
    return sorted(terminal_nodes)

def report_terminal_node_ir_drops(all_voltages, vdd_v, terminal_nodes) -> List[(str, float)]:
    """Print the voltage and drop from Vdd for each terminal node, sorted by largest drop."""
    rows = []
    for n in terminal_nodes:
        v = all_voltages.get(n.lower(), all_voltages.get(n))
        if v is None:
            continue
        drop = vdd_v - v
        rows.append((n, v, drop))
    rows.sort(key=lambda r: -abs(r[2]))
    print(f"\n=== Vdd ===")
    print(f"  {'node':<40} {'V (V)':>10} {'drop (mV)':>12}")
    for n, v, d in rows:
        print(f"  {n:<40} {v:>10.5f} {d*1000:>12.3f}")
    
    return [(n, d) for n, _, d in rows]

def print_report(report_path, node_ir_drops):
    """Write IR Drops to a text file. Format: node_name, drop_from_vdd (in mV)."""
    with report_path.open("w") as f:
        for node, drop in node_ir_drops:
            f.write(f"{node:<40} {drop*1000:>15.3f}\n")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("spice_path")
    ap.add_argument("subckt")
    ap.add_argument("--vdd-port", default="VDD")
    ap.add_argument("--vdd", type=float, default=3.3)
    ap.add_argument("--ion", type=float, default=50e-6)
    ap.add_argument("--out-dir", default="./ir_out")
    args = ap.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"[1/4] parse {args.spice_path} for {args.subckt}")
    subckt = parse_subckt(args.subckt, args.spice_path)
    print(f"      subckt {args.subckt}: {subckt!r}")
    if args.vdd_port not in subckt.ports:
        print(f"ERROR: Provided Vdd-port `{args.vdd_port!r}` is not a port of provided subckt ({subckt!r})", file=sys.stderr)
        sys.exit(1)

    print(f"[2/4] generate deck (VDD={args.vdd} V, Ion={args.ion} A)")
    deck_path = out_dir / "ir_analysis.sp"
    data_path = out_dir / "ir_analysis.data"
    deck, counts = generate_spice_op_analysis(
        subckt, args.vdd_port, args.vdd, args.ion,
        data_path=data_path.resolve(),  # absolute so ngspice's CWD doesn't matter
    )
    deck_path.write_text(deck)
    print(f"      {deck_path}  ({counts['pmos']} PMOS, {counts['nmos']} NMOS, {counts['other']} unknown, {counts['skipped']} skipped)")

    print(f"[3/4] ngspice .op  →  {data_path}")
    stdout, stderr, ngspice_ret = run_ngspice(deck_path)
    (out_dir / "ir_analysis.log").write_text(stdout)
    (out_dir / "ir_analysis.err").write_text(stderr)
    if ngspice_ret != 0:
        print(f"ERROR: ngspice rc={ngspice_ret}", file=sys.stderr)
        print(stderr[:1000], file=sys.stderr)
        sys.exit(2)
    if not data_path.exists():
        print(f"ERROR: ngspice did not produce {data_path}", file=sys.stderr)
        print(stderr[:1000], file=sys.stderr)
        sys.exit(3)
    voltages = parse_wrdata(data_path)
    print(f"      parsed {len(voltages)} node voltages")

    print(f"[4/4] Find terminal nodes from power nets")
    terminal_nodes = find_terminal_nodes(subckt, args.vdd_port)
    node_ir_drops = report_terminal_node_ir_drops(voltages, args.vdd, terminal_nodes)
    print_report(out_dir / "ir_analysis_report.txt", node_ir_drops)
    print()


if __name__ == "__main__":
    main()
