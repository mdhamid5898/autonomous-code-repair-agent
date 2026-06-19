#!/usr/bin/env python3
"""
Mechanic — select a LARGE-repo, MULTI-FILE subset of SWE-bench Verified.

The breadth regime our v1/v2/v3 evals couldn't exercise: big repos (too large to hold
in one context) + gold patches that touch >=2 SOURCE files. This is where decomposition
(multi-agent) should finally beat a single agent — the hypothesis v3 could not test
because mid-size repos stay single-context-tractable.

We ADOPT SWE-bench Verified (do not curate): 500 real issues with pinned base_commit,
FAIL_TO_PASS + PASS_TO_PASS test sets, an official grader, and prebuilt per-instance
Docker images. Each instance separates the gold `patch` (SOURCE changes) from
`test_patch` (the tests) — so counting .py files in `patch` IS the source-file count.

Filter:
  (a) large repos only — django, sympy, sphinx, scikit-learn, matplotlib, astropy, xarray
      (NOT requests/flask/seaborn — those are single-context-tractable like our v1/v2/v3);
  (b) gold `patch` touches >= MIN_SRC_FILES distinct .py source files (the multi-file tail —
      ~83% of SWE-bench is single-file, so we explicitly select the minority that isn't);
  (c) spread across repos (<= PER_REPO each), capped at MAX_TOTAL, biggest patches first.

Prints the candidate table and writes the subset to eval/swebench_subset.json (the
"manifest" the adapter consumes — same role as eval/issues*.yaml for the local sets).

Usage:
  .venv/bin/python swebench_subset.py                     # print + write subset
  .venv/bin/python swebench_subset.py --per-repo 2 --max-total 14
  .venv/bin/python swebench_subset.py --min-src-files 3   # only the >=3-file tail
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SUBSET_PATH = ROOT / "eval" / "swebench_subset.json"

# repo (HF `repo` field is "owner/name") -> short label. Large, multi-package repos only.
LARGE_REPOS = {
    "django/django": "django",
    "sympy/sympy": "sympy",
    "sphinx-doc/sphinx": "sphinx",
    "scikit-learn/scikit-learn": "sklearn",
    "matplotlib/matplotlib": "matplotlib",
    "astropy/astropy": "astropy",
    "pydata/xarray": "xarray",
}

_DIFF_GIT = re.compile(r"^diff --git a/(\S+) b/(\S+)", re.MULTILINE)


def patched_files(patch: str) -> list[str]:
    """Distinct file paths a unified diff touches (the b/ side)."""
    return sorted({m.group(2) for m in _DIFF_GIT.finditer(patch or "")})


def src_files(patch: str) -> list[str]:
    """.py files in the gold patch. SWE-bench's `patch` already excludes tests
    (those live in `test_patch`), so every .py here is a source file."""
    return [f for f in patched_files(patch) if f.endswith(".py")]


def load_verified():
    from datasets import load_dataset
    for name in ("SWE-bench/SWE-bench_Verified", "princeton-nlp/SWE-bench_Verified"):
        try:
            return load_dataset(name, split="test")
        except Exception as e:  # noqa: BLE001
            print(f"  [load {name} failed: {type(e).__name__}: {str(e)[:80]}]")
    raise SystemExit("Could not load SWE-bench Verified from HuggingFace.")


def main():
    ap = argparse.ArgumentParser(description="Pick a large-repo multi-file SWE-bench Verified subset.")
    ap.add_argument("--min-src-files", type=int, default=2, help="gold patch must touch >= this many .py files")
    ap.add_argument("--per-repo", type=int, default=2, help="max instances kept per repo (spread)")
    ap.add_argument("--max-total", type=int, default=14, help="cap on total subset size")
    ap.add_argument("--no-write", action="store_true", help="print only; don't write the subset file")
    args = ap.parse_args()

    print("Loading SWE-bench/SWE-bench_Verified (split=test, 500 instances) ...")
    ds = load_verified()

    # build the multi-file, large-repo pool
    pool = []
    for r in ds:
        if r["repo"] not in LARGE_REPOS:
            continue
        sf = src_files(r["patch"])
        if len(sf) < args.min_src_files:
            continue
        pool.append({
            "instance_id": r["instance_id"],
            "repo": r["repo"],
            "label": LARGE_REPOS[r["repo"]],
            "base_commit": r["base_commit"],
            "n_src_files": len(sf),
            "src_files": sf,
            "n_fail_to_pass": len(json.loads(r["FAIL_TO_PASS"])),
            "n_pass_to_pass": len(json.loads(r["PASS_TO_PASS"])),
            "problem_chars": len(r["problem_statement"] or ""),
        })

    # spread: within each repo take the biggest patches first; round-robin across repos
    by_repo: dict[str, list] = {}
    for c in pool:
        by_repo.setdefault(c["label"], []).append(c)
    for lst in by_repo.values():
        lst.sort(key=lambda c: (-c["n_src_files"], c["instance_id"]))

    selected, round_i = [], 0
    while len(selected) < args.max_total:
        added = False
        for label in sorted(by_repo):
            lst = by_repo[label]
            if round_i < min(args.per_repo, len(lst)):
                if round_i < len(lst):
                    selected.append(lst[round_i])
                    added = True
                    if len(selected) >= args.max_total:
                        break
        round_i += 1
        if not added:
            break
    selected.sort(key=lambda c: (c["label"], -c["n_src_files"], c["instance_id"]))

    # ---- print the candidate table (BEFORE doing anything else) ----
    print(f"\nMulti-file (>= {args.min_src_files} .py) pool in large repos: {len(pool)} instances "
          f"across {len(by_repo)} repos")
    counts = {lbl: len(v) for lbl, v in sorted(by_repo.items())}
    print("  pool by repo: " + "  ".join(f"{k}:{v}" for k, v in counts.items()))
    print(f"\nSELECTED SUBSET ({len(selected)}; <= {args.per_repo}/repo, biggest patches first):")
    print("=" * 92)
    print(f"{'instance_id':<34}{'repo':<11}{'#src':<6}{'F2P':<6}{'P2P':<7}{'prob_chars':<11}")
    print("-" * 92)
    for c in selected:
        print(f"{c['instance_id']:<34}{c['label']:<11}{c['n_src_files']:<6}"
              f"{c['n_fail_to_pass']:<6}{c['n_pass_to_pass']:<7}{c['problem_chars']:<11}")
    print("=" * 92)
    print(f"  total: {len(selected)}  |  repos: "
          + ", ".join(f"{lbl}×{sum(1 for c in selected if c['label']==lbl)}"
                      for lbl in sorted({c['label'] for c in selected})))

    if not args.no_write:
        SUBSET_PATH.parent.mkdir(parents=True, exist_ok=True)
        SUBSET_PATH.write_text(json.dumps(
            {"dataset": "SWE-bench/SWE-bench_Verified", "split": "test",
             "filter": {"min_src_files": args.min_src_files, "per_repo": args.per_repo,
                        "max_total": args.max_total, "large_repos": sorted(LARGE_REPOS)},
             "instances": selected}, indent=2))
        print(f"\nSubset written -> {SUBSET_PATH.relative_to(ROOT)}  ({len(selected)} instances)")


if __name__ == "__main__":
    main()
