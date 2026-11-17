"""Run ngspice in batch mode and parse the DC operating point."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

_LINE = re.compile(r"^\s*(?:v\()?([\w.\-#]+)\)?\s*=\s*([-+0-9.eE]+)\s*$")


def run_op(cir: Path) -> dict[str, float]:
    proc = subprocess.run(["ngspice", "-b", str(cir)], capture_output=True, text=True, timeout=120)
    if proc.returncode != 0 or "error" in proc.stderr.lower():
        raise RuntimeError(f"ngspice failed ({proc.returncode}):\n{proc.stdout}\n{proc.stderr}")
    log = proc.stdout + proc.stderr
    if "source stepping failed" in log.lower():  # every convergence aid failed: the numbers are junk
        hints = sorted(set(re.findall(r"check node (\S+)", log)))
        raise RuntimeError(f"ngspice found no operating point (singular nodes: {hints})")
    return parse_print_all(proc.stdout)


def parse_print_all(text: str) -> dict[str, float]:
    out: dict[str, float] = {}
    for line in text.splitlines():
        m = _LINE.match(line)
        if m:
            out[m.group(1).lower()] = float(m.group(2))
    return out
