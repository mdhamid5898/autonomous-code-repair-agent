#!/usr/bin/env python3
"""
Mechanic — eval gate. The eval is the gate: this fails (exit 1) if a sweep report's
resolution count drops below a committed baseline, so CI can BLOCK a PR that regresses the
agent's fix rate. Works for both report shapes (each row carries a `resolved` bool):
  - local sweep.py        -> [{"id": ..., "resolved": bool, ...}, ...]
  - swebench_sweep.py     -> [{"instance_id": ..., "resolved": bool, "official_resolved": ...}, ...]

Baselines live in a committed JSON (default eval/ci_baseline.json), keyed by eval name:
  {"swebench_subset_flash": {"total": 14, "min_resolved": 9, "note": "..."}, ...}

Usage:
  python ci_gate.py --report swebench_report_solve_deepseek-v4-flash.json --key swebench_subset_flash
  python ci_gate.py --report sweep_report_issues_v2_deepseek-chat_solve.json --min-resolved 13
  python ci_gate.py --report <r> --key <k> --update     # accept current as the new floor (intentional change)
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DEFAULT_BASELINE = ROOT / "eval" / "ci_baseline.json"


def count_resolved(report_path: Path) -> tuple[int, int]:
    """Return (resolved, total) from a sweep report (a JSON list of per-instance rows).
    A row counts as resolved if `resolved` (or, for SWE-bench, `official_resolved`) is truthy."""
    rows = json.loads(report_path.read_text())
    if not isinstance(rows, list):
        sys.exit(f"{report_path}: expected a JSON list of rows, got {type(rows).__name__}")
    total = len(rows)
    resolved = sum(1 for r in rows if r.get("resolved") or r.get("official_resolved"))
    return resolved, total


def main() -> None:
    ap = argparse.ArgumentParser(description="Fail if a sweep report regresses below its baseline.")
    ap.add_argument("--report", required=True, type=Path, help="sweep report JSON to gate")
    ap.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE, help="committed baseline JSON")
    ap.add_argument("--key", help="baseline entry name (e.g. swebench_subset_flash)")
    ap.add_argument("--min-resolved", type=int, help="override: required resolved count (skips baseline file)")
    ap.add_argument("--update", action="store_true", help="write the current resolved count back as the new floor")
    args = ap.parse_args()

    if not args.report.exists():
        sys.exit(f"report not found: {args.report}")
    resolved, total = count_resolved(args.report)
    rate = (resolved / total * 100) if total else 0.0

    # determine the floor
    if args.min_resolved is not None:
        floor, note = args.min_resolved, "(from --min-resolved)"
    else:
        if not args.key:
            sys.exit("provide --key (a baseline entry) or --min-resolved")
        baseline = json.loads(args.baseline.read_text()) if args.baseline.exists() else {}
        if args.update:
            baseline[args.key] = {"total": total, "min_resolved": resolved,
                                  "note": baseline.get(args.key, {}).get("note", "")}
            args.baseline.write_text(json.dumps(baseline, indent=2) + "\n")
            print(f"baseline[{args.key}] updated -> {resolved}/{total}  ({args.baseline.name})")
            return
        if args.key not in baseline:
            sys.exit(f"no baseline entry '{args.key}' in {args.baseline} (seed it with --update)")
        floor, note = baseline[args.key]["min_resolved"], baseline[args.key].get("note", "")

    ok = resolved >= floor
    print("=" * 64)
    print(f"  eval gate: {args.report.name}")
    print(f"  resolved : {resolved}/{total}  ({rate:.0f}%)")
    print(f"  floor    : >= {floor}   {note}")
    print(f"  verdict  : {'PASS ✅' if ok else 'FAIL ❌ — resolution regressed below the baseline'}")
    print("=" * 64)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
