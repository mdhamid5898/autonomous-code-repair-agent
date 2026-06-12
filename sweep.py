#!/usr/bin/env python3
"""
Mechanic — run the whole eval set through solve.py's agent loop and report X/15.

Sequential on purpose: the OpenAI org is on a low TPM tier, so concurrent runs
would just rate-limit each other. Each issue resets its own clone first
(solve.run_agent -> prepare_repo), so runs are independent.

Writes eval results incrementally to sweep_report.json (survives a crash) and
prints a final resolution-rate table. pass@1 — a single shot per issue. NOTE:
GPT-4o is non-deterministic (we saw freezegun-553 thrash on one run, nail it on
another), so one sweep is a NOISY point estimate; pass@k is the honest follow-up.

Usage:
  .venv/bin/python sweep.py                       # all 15
  .venv/bin/python sweep.py --only furl-163 isodate-44
  .venv/bin/python sweep.py --model gpt-4o --max-steps 30
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "eval"))

from solve import run_agent, load_env, RUNS_DIR  # noqa: E402
from verify import load_manifest  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description="Run the full eval set and report X/15.")
    ap.add_argument("--model", default="gpt-4o")
    ap.add_argument("--max-steps", type=int, default=30)
    ap.add_argument("--only", nargs="*", help="subset of issue ids")
    args = ap.parse_args()

    load_env()
    doc, _, _ = load_manifest()
    issues = doc["issues"]
    if args.only:
        issues = [i for i in issues if i["id"] in args.only]

    RUNS_DIR.mkdir(exist_ok=True)
    report_path = ROOT / "sweep_report.json"
    results = []
    t_start = time.time()
    print(f"SWEEP: {len(issues)} issues | model={args.model} | max_steps={args.max_steps}\n", flush=True)

    for n, issue in enumerate(issues, 1):
        iid = issue["id"]
        print(f"[{n}/{len(issues)}] {iid} ...", flush=True)
        t0 = time.time()
        try:
            rec = run_agent(issue, args.model, args.max_steps, verbose=False)
            v = rec["verdict"]
            row = {"id": iid, "resolved": v["resolved"], "exit": v["exit"],
                   "steps": rec["steps"], "stop_reason": rec["stop_reason"],
                   "repro": v["summary"], "submitted": rec["submitted_summary"],
                   "seconds": round(time.time() - t0, 1)}
            (RUNS_DIR / f"{iid}_{int(t0)}.json").write_text(json.dumps(rec, indent=2, default=str))
        except (Exception, SystemExit) as e:  # one issue must never kill the sweep
            row = {"id": iid, "resolved": False, "exit": None, "steps": None,
                   "stop_reason": "EXCEPTION", "repro": f"{type(e).__name__}: {e}",
                   "submitted": None, "seconds": round(time.time() - t0, 1)}
        results.append(row)
        mark = "RESOLVED ✅" if row["resolved"] else "-- not resolved"
        print(f"      {mark}  steps={row['steps']} stop={row['stop_reason']} ({row['seconds']}s)", flush=True)
        report_path.write_text(json.dumps(results, indent=2))  # incremental save

    n_res = sum(1 for r in results if r["resolved"])
    print("\n" + "=" * 72)
    print(f"{'ISSUE':<24}{'RESOLVED':<11}{'STEPS':<7}{'STOP':<14}")
    print("-" * 72)
    for r in results:
        print(f"{r['id']:<24}{('YES' if r['resolved'] else 'no'):<11}{str(r['steps']):<7}{r['stop_reason']:<14}")
    print("=" * 72)
    rate = round(100 * n_res / len(results)) if results else 0
    print(f"  RESOLUTION RATE: {n_res}/{len(results)}  ({rate}%)   |   {round(time.time() - t_start)}s total")
    print(f"  PRD v1 target: >=25%  |  report -> {report_path.name}")


if __name__ == "__main__":
    main()
