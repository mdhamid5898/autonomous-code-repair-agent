#!/usr/bin/env python3
"""
Mechanic — SWE-bench cost-tier table: cheap vs strong, head-to-head on the subset.

Reads two sweep reports (default the v4-flash and v4-pro single-agent runs) and prints a
per-instance YES/no table, the headline resolution + cost per tier, the breadth (file-count)
breakdown, and the instances the strong tier LIFTS over cheap (the case for escalate-on-retry).

Cost per run uses documented anchors (router.estimate_cost: flash ~$0.03, pro ~$0.07) since the
harness doesn't meter tokens; wall-clock seconds (from the reports) is the concrete cost proxy.

Usage:
  python swebench_costtier.py
  python swebench_costtier.py --cheap swebench_report_solve_deepseek-v4-flash.json \
                              --strong swebench_report_solve_deepseek-v4-pro.json
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PER_RUN = {"deepseek-v4-flash": 0.03, "deepseek-v4-pro": 0.07}


def load(path: Path) -> dict:
    rows = json.loads(path.read_text()) if path.exists() else []
    return {r["instance_id"]: r for r in rows}


def stats(rows: dict, model: str) -> dict:
    vals = list(rows.values())
    res = sum(1 for r in vals if r.get("resolved"))
    secs = sum((r.get("seconds") or 0) for r in vals)
    per = PER_RUN.get(model, 0.05)
    return {"n": len(vals), "resolved": res, "secs": secs, "per_run": per, "total_cost": round(per * len(vals), 2)}


def main() -> None:
    ap = argparse.ArgumentParser(description="SWE-bench cheap-vs-strong cost-tier table.")
    ap.add_argument("--cheap", type=Path, default=ROOT / "swebench_report_solve_deepseek-v4-flash.json")
    ap.add_argument("--strong", type=Path, default=ROOT / "swebench_report_solve_deepseek-v4-pro.json")
    ap.add_argument("--cheap-model", default="deepseek-v4-flash")
    ap.add_argument("--strong-model", default="deepseek-v4-pro")
    args = ap.parse_args()

    cheap, strong = load(args.cheap), load(args.strong)
    ids = sorted(set(cheap) | set(strong), key=lambda i: (-(cheap.get(i) or strong.get(i) or {}).get("n_src_files", 0), i))
    cs, ss = stats(cheap, args.cheap_model), stats(strong, args.strong_model)

    def mark(rows, i):
        if i not in rows:
            return "  -  "
        return " YES " if rows[i].get("resolved") else " no  "

    print("=" * 78)
    print(f"{'instance':<34}{'#src':<6}{'flash':<7}{'pro':<7}{'lift':<6}")
    print("-" * 78)
    lifts = []
    for i in ids:
        nsrc = (cheap.get(i) or strong.get(i) or {}).get("n_src_files", "")
        c_res = cheap.get(i, {}).get("resolved")
        s_res = strong.get(i, {}).get("resolved")
        both = i in cheap and i in strong  # only compare where both tiers actually ran
        lift = ("↑" if (s_res and not c_res) else ("↓" if (c_res and not s_res) else "")) if both else ""
        if both and s_res and not c_res:
            lifts.append((i, nsrc))
        print(f"{i:<34}{str(nsrc):<6}{mark(cheap, i):<7}{mark(strong, i):<7}{lift:<6}")
    print("=" * 78)

    def line(tag, model, st):
        rate = (st["resolved"] / st["n"] * 100) if st["n"] else 0
        print(f"  {tag:<6} ({model:<18}): {st['resolved']}/{st['n']}  ({rate:.0f}%)  "
              f"@ ~${st['per_run']}/run  ~${st['total_cost']} total  | {round(st['secs']/60)} min wall-clock")

    line("flash", args.cheap_model, cs)
    line("pro", args.strong_model, ss)

    # breadth (file-count) breakdown per tier
    for tag, rows in (("flash", cheap), ("pro", strong)):
        vals = list(rows.values())
        big = [r for r in vals if r.get("n_src_files", 0) >= 4]
        small = [r for r in vals if 0 < r.get("n_src_files", 0) <= 3]
        if vals:
            print(f"  {tag} breadth: <=3 files {sum(r['resolved'] for r in small)}/{len(small)}  |  "
                  f">=4 files {sum(r['resolved'] for r in big)}/{len(big)}")
    if lifts:
        print(f"  pro LIFTS over flash on: {', '.join(f'{i} ({n} files)' for i, n in lifts)}")
    if cs["n"] and ss["n"] and ss["resolved"] > cs["resolved"]:
        dc = round(ss["total_cost"] - cs["total_cost"], 2)
        print(f"  -> pro buys +{ss['resolved'] - cs['resolved']} resolved for +${dc} "
              f"(~{round(ss['per_run']/cs['per_run'],1)}x per-run cost)")


if __name__ == "__main__":
    main()
