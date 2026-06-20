#!/usr/bin/env python3
"""
Mechanic — SWE-bench Verified breadth experiment: single vs governed vs multi.

Runs an engine over the large-repo, multi-file subset (eval/swebench_subset.json), grading
each instance OFFICIALLY (swebench.harness.run_evaluation). HYPOTHESIS: unlike v3 (mid-size,
single-context repos where multi only TIED single — see [[mechanic-multiagent-v2-comparison]]),
here multi's edge should GROW with repo size / file-count, because the task finally exceeds a
single context window.

Per-instance + incremental (writes after each instance) so it is RESUMABLE — SWE-bench Docker
runs are slow under emulation, so a sweep can take hours; killing/resuming is expected. Already-
graded instances in the report are skipped on re-run.

Reports: swebench_report_<engine>_<model>.json. Cost proxy = wall-clock seconds + agent
step/iteration count (token accounting is Phase-6 observability, not wired here).

Usage:
  .venv/bin/python swebench_sweep.py --engine solve                 # all subset instances
  .venv/bin/python swebench_sweep.py --engine multi --only sympy__sympy-16597 sphinx-doc__sphinx-10673
  .venv/bin/python swebench_sweep.py --engine governed --max-steps 40
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from solve import load_env  # noqa: E402
from swebench_solve import load_instance, solve_instance, grade_official  # noqa: E402

SUBSET = ROOT / "eval" / "swebench_subset.json"


def main():
    ap = argparse.ArgumentParser(description="Run one engine over the SWE-bench subset, officially graded.")
    ap.add_argument("--engine", choices=["solve", "governed", "multi", "bestofn"], default="solve")
    ap.add_argument("--model", default="deepseek-chat")
    ap.add_argument("--max-steps", type=int, default=40)
    ap.add_argument("--max-iterations", type=int, default=4)
    ap.add_argument("--best-of-n", type=int, default=3, help="candidate trajectories per instance for engine=bestofn")
    ap.add_argument("--no-early-stop", action="store_true", help="for bestofn: always sample all N candidates")
    ap.add_argument("--escalate", action="store_true",
                    help="for bestofn: router cheap->strong ladder (v4-flash, then v4-pro on retry)")
    ap.add_argument("--only", nargs="*", help="subset of instance_ids")
    ap.add_argument("--no-grade", action="store_true", help="run agents but skip official grading")
    args = ap.parse_args()

    load_env()
    subset = json.loads(SUBSET.read_text())["instances"]
    if args.only:
        subset = [c for c in subset if c["instance_id"] in args.only]
    budget = args.max_iterations if args.engine == "multi" else args.max_steps
    report_path = ROOT / f"swebench_report_{args.engine}_{args.model}.json"
    results = json.loads(report_path.read_text()) if report_path.exists() else []
    done = {r["instance_id"] for r in results}

    print(f"SWE-BENCH SWEEP | engine={args.engine} | model={args.model} | budget={budget} | "
          f"{len(subset)} instances ({len(done)} already done)\n", flush=True)

    for n, c in enumerate(subset, 1):
        iid = c["instance_id"]
        if iid in done:
            print(f"[{n}/{len(subset)}] {iid} — skip (already in report)", flush=True)
            continue
        print(f"[{n}/{len(subset)}] {iid} ({c['label']}, {c['n_src_files']} src files) ...", flush=True)
        t0 = time.time()
        try:
            instance = load_instance(iid)
            rec = solve_instance(instance, args.engine, args.model, budget, verbose=False,
                                 n=args.best_of_n, early_stop=not args.no_early_stop, escalate=args.escalate)
            resolved = None
            if not args.no_grade and not rec["patch_empty"]:
                model_name = f"mechanic-{args.engine}-{args.model}"
                run_id = f"swe_{args.engine}_{iid.replace('__', '_')}"
                resolved = grade_official({iid: rec["patch"]}, run_id, model_name, verbose=False).get(iid)
            row = {"instance_id": iid, "repo": c["label"], "n_src_files": c["n_src_files"],
                   "engine": args.engine, "resolved": bool(resolved), "official_resolved": resolved,
                   "incontainer_f2p_pass": rec["incontainer_f2p_pass"], "patch_empty": rec["patch_empty"],
                   "steps": rec.get("steps"), "stop_reason": rec.get("stop_reason"),
                   "n_candidates": rec.get("n_candidates"), "n_passing": rec.get("n_passing"),
                   "winner_index": rec.get("winner_index"), "seconds": round(time.time() - t0, 1)}
        except Exception as e:  # one instance must never kill the sweep
            row = {"instance_id": iid, "repo": c["label"], "n_src_files": c["n_src_files"],
                   "engine": args.engine, "resolved": False, "official_resolved": None,
                   "error": f"{type(e).__name__}: {e}", "seconds": round(time.time() - t0, 1)}
        results.append(row)
        report_path.write_text(json.dumps(results, indent=2))  # incremental save (resumable)
        mark = "RESOLVED ✅" if row["resolved"] else ("ERROR" if row.get("error") else "-- not resolved")
        print(f"      {mark}  steps={row.get('steps')} ({row['seconds']}s)"
              + (f"  [{row['error']}]" if row.get("error") else ""), flush=True)

    graded = [r for r in results if r["instance_id"] in {c["instance_id"] for c in subset}]
    n_res = sum(1 for r in graded if r["resolved"])
    print("\n" + "=" * 70)
    print(f"{'instance':<34}{'repo':<11}{'#src':<6}{'resolved':<9}")
    print("-" * 70)
    for r in sorted(graded, key=lambda r: (r["repo"], -r.get("n_src_files", 0))):
        print(f"{r['instance_id']:<34}{r['repo']:<11}{r.get('n_src_files',''):<6}"
              f"{('YES' if r['resolved'] else 'no'):<9}")
    print("=" * 70)
    print(f"  {args.engine}: {n_res}/{len(graded)} resolved  |  report -> {report_path.name}")
    # file-count signal: does resolution track file-count? (the breadth hypothesis)
    if graded:
        big = [r for r in graded if r.get("n_src_files", 0) >= 3]
        small = [r for r in graded if r.get("n_src_files", 0) == 2]
        print(f"  by breadth: >=3 src files {sum(r['resolved'] for r in big)}/{len(big)}  |  "
              f"==2 src files {sum(r['resolved'] for r in small)}/{len(small)}")
    try:
        from tracing import flush; flush()  # send any Langfuse traces (no-op unless enabled)
    except Exception:
        pass


if __name__ == "__main__":
    main()
