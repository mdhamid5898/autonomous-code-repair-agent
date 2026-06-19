#!/usr/bin/env python3
"""
Mechanic — head-to-head engine comparison from sweep reports (the iso-control view).

Reads the per-engine `sweep_report_<tag>_<model>_<engine>[_<variant>].json` files
that sweep.py writes and prints, per manifest:
  1. a per-issue matrix (resolved + a compact stop-reason code) with one column per engine,
  2. totals per engine,
  3. a "disagreements" section — issues where the engines differ, annotated with each
     engine's (resolved, stop_reason, steps) so you can read the MECHANISM, not just totals.

This is the apparatus for the iso-control experiment: single (`solve`) vs multi (`multi`)
vs governed-single (`governed` = single agent + test-gated submit). If governed ≈ multi,
multi's edge is the verify-and-retry CONTROL LOOP, not the decomposition.

Usage:
  .venv/bin/python compare.py                      # v2 and v3, deepseek-chat
  .venv/bin/python compare.py --tag issues_v2
  .venv/bin/python compare.py --model deepseek-chat --tag issues_v3
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# (column label, engine, variant) — variant maps to the `_<variant>` filename suffix.
# Missing files are skipped silently, so this works incrementally as sweeps land.
COLUMNS = [
    ("single", "solve", None),
    ("single@60", "solve", "60steps"),
    ("multi", "multi", None),
    ("governed", "governed", None),
    ("gov@60", "governed", "60steps"),
]

# compact stop_reason codes for the matrix
STOP_CODE = {
    "submitted": "sub", "max_steps": "max", "model_stopped": "stp",
    "api_error": "api", "EXCEPTION": "exc", "resolved": "res", None: "?",
}


def report_path(tag: str, model: str, engine: str, variant: str | None) -> Path:
    suffix = f"_{variant}" if variant else ""
    return ROOT / f"sweep_report_{tag}_{model}_{engine}{suffix}.json"


def load(tag: str, model: str):
    """Return (present_columns, {issue_id: {col_label: row}}) for the columns that exist."""
    present, by_issue = [], {}
    order = []
    for label, engine, variant in COLUMNS:
        p = report_path(tag, model, engine, variant)
        if not p.exists():
            continue
        present.append(label)
        for row in json.loads(p.read_text()):
            iid = row["id"]
            if iid not in by_issue:
                by_issue[iid] = {}
                order.append(iid)
            by_issue[iid][label] = row
    return present, by_issue, order


def cell(row: dict | None) -> str:
    if row is None:
        return "  -  "
    mark = "Y" if row.get("resolved") else "·"
    code = STOP_CODE.get(row.get("stop_reason"), "?")
    return f"{mark}/{code}"


def show(tag: str, model: str):
    present, by_issue, order = load(tag, model)
    if not present:
        print(f"[{tag}] no reports found for model={model}\n")
        return
    print("=" * (26 + 8 * len(present)))
    print(f"{tag}  (model={model})   Y=resolved · ·=not   codes: sub/max/stp/res/api/exc")
    print("-" * (26 + 8 * len(present)))
    header = f"{'ISSUE':<24}" + "".join(f"{c:<8}" for c in present)
    print(header)
    for iid in order:
        line = f"{iid:<24}" + "".join(f"{cell(by_issue[iid].get(c)):<8}" for c in present)
        print(line)
    print("-" * (26 + 8 * len(present)))
    totals = f"{'RESOLVED':<24}"
    n = len(order)
    for c in present:
        k = sum(1 for iid in order if by_issue[iid].get(c, {}).get("resolved"))
        totals += f"{f'{k}/{n}':<8}"
    print(totals)
    print("=" * (26 + 8 * len(present)))

    # disagreements: where the engines differ — the head-to-head signal
    disagree = [iid for iid in order
                if len({bool(by_issue[iid].get(c, {}).get("resolved"))
                        for c in present if c in by_issue[iid]}) > 1]
    if disagree:
        print("\nDISAGREEMENTS (mechanism — resolved, stop_reason, steps):")
        for iid in disagree:
            print(f"  {iid}")
            for c in present:
                r = by_issue[iid].get(c)
                if r is None:
                    continue
                res = "RESOLVED" if r.get("resolved") else "miss    "
                print(f"      {c:<11} {res}  stop={r.get('stop_reason'):<11} steps={r.get('steps')}")
    else:
        print("\n(no disagreements — all present engines agree on every issue)")

    # set deltas: who recovers what relative to single
    if "single" in present:
        base = {iid for iid in order if by_issue[iid].get("single", {}).get("resolved")}
        for c in present:
            if c == "single":
                continue
            got = {iid for iid in order if by_issue[iid].get(c, {}).get("resolved")}
            gained, lost = sorted(got - base), sorted(base - got)
            if gained or lost:
                print(f"\n{c} vs single:  +{len(gained)} recovered {gained}   -{len(lost)} lost {lost}")
    print()


def main():
    ap = argparse.ArgumentParser(description="Head-to-head engine comparison from sweep reports.")
    ap.add_argument("--model", default="deepseek-chat")
    ap.add_argument("--tag", default=None, help="manifest stem, e.g. issues_v2 (default: v2 then v3)")
    args = ap.parse_args()
    tags = [args.tag] if args.tag else ["issues_v2", "issues_v3"]
    for t in tags:
        show(t, args.model)


if __name__ == "__main__":
    main()
