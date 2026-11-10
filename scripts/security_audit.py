#!/usr/bin/env python3
"""Network advisory scan, separate from the offline gate.

Run with the installation's interpreter. Requires uv and npm; audit tooling runs
in an isolated uv environment. Reports preserve findings and skipped packages;
there is no advisory ignore list. All conditional constraints versions are scanned.
"""
from __future__ import annotations

import argparse
import importlib.metadata
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def constraint_batches(path: Path) -> list[list[str]]:
    batches: list[list[str]] = [[]]
    seen: dict[str, set[str]] = {}
    for line in path.read_text().splitlines():
        match = re.match(r"^([A-Za-z0-9_.-]+)==([^ ;]+)", line)
        if not match:
            continue
        name, version = match.groups()
        name = re.sub(r"[-_.]+", "-", name).lower()
        versions = seen.setdefault(name, set())
        if version in versions:
            continue
        index = len(versions)
        versions.add(version)
        if index == len(batches):
            batches.append([])
        batches[index].append(f"{name}=={version}")
    return batches


def run(argv: list[str], report: Path, *, cwd: Path = ROOT) -> int:
    print(f"Scanning {report.stem}", flush=True)
    with report.open("w") as output:
        result = subprocess.run(argv, cwd=cwd, stdout=output, timeout=600)
    return result.returncode


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True,
                        help="directory for JSON results and exact inventories")
    args = parser.parse_args()
    if not shutil.which("uv") or not shutil.which("npm"):
        parser.error("install uv and npm before running advisory scans")
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    inventories = {f"constraints-{i}": rows for i, rows in
                   enumerate(constraint_batches(ROOT / "constraints.txt"))}
    inventories["installed"] = sorted({
        f"{dist.metadata['Name']}=={dist.version}"
        for dist in importlib.metadata.distributions()
        if dist.metadata.get("Name", "").lower() != "yurios"
    })
    statuses = {}
    for scope, rows in inventories.items():
        requirements = output / f"{scope}.txt"
        requirements.write_text("\n".join(rows) + "\n")
        statuses[scope] = run([
            "uv", "tool", "run", "--from", "pip-audit", "pip-audit",
            "--no-deps", "--disable-pip", "--progress-spinner", "off",
            "--format", "json", "-r", str(requirements),
        ], output / f"{scope}.json")
    for scope in ("web", "desktop-shell"):
        statuses[scope] = run(["npm", "audit", "--package-lock-only", "--json"],
                              output / f"{scope}.json", cwd=ROOT / scope)
    (output / "status.json").write_text(json.dumps(statuses, indent=2) + "\n")
    print(f"Reports: {output}. Inspect skipped packages as well as vulnerabilities.")
    return int(any(statuses.values()))


if __name__ == "__main__":
    sys.exit(main())
