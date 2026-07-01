#!/usr/bin/env python3
"""
bestofn_validate.py — TODO #1: validate the parser-fixed best-of-N SELECTOR.

The best-of-N engine (swebench_solve.run_best_of_n) picks a winner using a CHEAP
in-container grade (swebench_solve.SweBenchExecutor.grade_patch: reset to base+test_patch
-> apply the candidate patch fresh -> run the whole touched test file -> parse the log with
swebench's repo parser and require every FAIL_TO_PASS to flip AND every PASS_TO_PASS to hold).
The whole design rests on ONE equivalence:

    in-container grade_patch(patch)["resolved"]  ==  official run_evaluation(patch) resolved

If that holds, selection is a real regression guard with no per-candidate official spin. Two
grading bugs previously broke it (both since fixed): the in-container grade used the bare exit
code, which FALSE-NEGATIVES on django (the eval command runs a whole module that has unrelated
failing tests, so exit!=0 while the F2P/P2P subset passes) -> fixed to parse the gold node ids
(`5eaec28`); and grade_official re-used a per-(engine,instance) run_id, so swebench's cache
returned a STALE verdict when re-grading a DIFFERENT patch -> fixed to content-address by patch
hash (`dceb008`). Live selection evidence was only N=2 and mixed old-grade/stale-cache -> too
thin to trust. This script closes that gap.

GATE (per the two hard instances a single agent missed): for EVERY best-of-N candidate patch,
the in-container verdict must EQUAL a FRESH official grade.
  * false-POSITIVE  = in-container PASS but official not-resolved  -> selector would SHIP a non-fix.
  * false-NEGATIVE  = in-container fail  but official RESOLVED     -> selector would DISCARD a real
                      fix (exactly the django symptom the parser fix targets).
0 false-pos AND 0 false-neg on both instances => the selector is trustworthy at scale (unblocks
TODO #2, the full --engine bestofn sweep).

Two phases, each cached so an 8GB-RAM Docker death is resumable (re-run the same command):
  PRODUCE  run N best-of-N trajectories (mirrors run_best_of_n: escalation ladder + per-attempt
           temperature + seed nudge; --no-early-stop equivalent so we capture ALL N pairs), keep
           each candidate PATCH and its in-container verdict. Plus GOLD and EMPTY oracle grades
           (a guaranteed-resolve and a guaranteed-fail through the same in-container grader).
           -> runs/bestofn_validate_<id>_candidates.json
  GRADE    fresh official grade of every DISTINCT non-empty patch (grade_official is
           content-addressed; one container at a time; verdict cached per patch-hash).
           -> runs/bestofn_validate_<id>_official.json
Then COMPARE every (in-container, official) pair, print the confusion matrix, and emit a GATE
verdict. Full report -> runs/bestofn_validate_<id>_report.json.

Usage:
  .venv/bin/python bestofn_validate.py --instance django__django-11138 --max-steps 60 --escalate --verbose
  .venv/bin/python bestofn_validate.py --instance sympy__sympy-16597  --max-steps 60 --escalate --verbose
  # resume: just re-run the same command (cached candidates + already-graded patches are skipped)
  # re-grade only (agent phase already cached): add --grade-only
  # force fresh trajectories: add --refresh
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "eval"))

from swebench_solve import (  # noqa: E402 — reuse the exact primitives the live engine uses
    ensure_image, load_instance, run_single, grade_official, test_command,
    SweBenchExecutor, RUNS_DIR, _BON_SEEDS,
)
from solve import load_env  # noqa: E402
from router import escalation_ladder  # noqa: E402


def _sha(patch: str) -> str:
    return hashlib.sha1(patch.encode()).hexdigest()[:12]


def _bon_diversity(i: int, base_model: str, escalate: bool, n: int) -> tuple[str, float, str]:
    """The EXACT per-attempt (model, temperature, seed_hint) that run_best_of_n uses, so the
    candidates we validate are representative of the live selector's inputs."""
    models = escalation_ladder(n) if escalate else [base_model] * n
    model = models[i] if i < len(models) else models[-1]
    temp = round(0.0 if i == 0 else min(0.4 + 0.3 * (i - 1), 1.0), 2)
    seed = _BON_SEEDS[i] if i < len(_BON_SEEDS) else _BON_SEEDS[-1]
    return model, temp, seed


# --------------------------------------------------------------------------- #
# PRODUCE — run N trajectories, capture each patch + its in-container verdict,
# plus gold/empty oracle grades. All in ONE container; cached to disk.
# --------------------------------------------------------------------------- #
def produce(instance: dict, base_model: str, max_steps: int, n: int, escalate: bool,
            verbose: bool, cache_path: Path, refresh: bool) -> dict:
    iid = instance["instance_id"]
    # Resume support: the cache is written INCREMENTALLY (after every candidate AND the oracle), so a
    # mid-PRODUCE death — an 8GB-Docker OOM, or a session teardown that kills THIS process while the
    # container keeps running (both hit us) — only loses the in-flight trajectory. Re-running the same
    # command resumes from the next candidate. Each attempt is independent (reset_clean between them),
    # so a partial candidate list is always safe to continue.
    prior = {} if (refresh or not cache_path.exists()) else json.loads(cache_path.read_text())
    candidates = prior.get("candidates", [])
    oracle = prior.get("oracle", {})
    if len(candidates) >= n and oracle:
        print(f"  [PRODUCE cached ({len(candidates)}/{n} candidates + oracle) -> {cache_path.name}]")
        return prior

    def save(red_resolved: bool, t0: float) -> dict:
        result = {"instance_id": iid, "repo": instance["repo"], "base_model": base_model,
                  "max_steps": max_steps, "n": n, "escalate": escalate,
                  "red_on_base_resolved": red_resolved,
                  "produce_seconds": round(round(time.time() - t0, 1) + prior.get("produce_seconds", 0), 1),
                  "candidates": candidates, "oracle": oracle}
        cache_path.write_text(json.dumps(result, indent=2, default=str))
        return result

    image = ensure_image(instance, verbose)
    ex = SweBenchExecutor(instance, image, verbose)
    t0 = time.time()
    result = prior
    try:
        if "red_on_base_resolved" in prior:  # skip the slow red re-check on resume (already confirmed)
            red_resolved = prior["red_on_base_resolved"]
            print(f"  [resuming: {len(candidates)}/{n} candidates cached; red-on-base={red_resolved}]")
        else:
            red = ex.run_fail_to_pass()  # clean=True: reset+test on base+test_patch (no edits yet)
            red_resolved = bool(red["resolved"])
            print(f"  [red-on-base: {'RED (correct)' if not red_resolved else 'GREEN (bug absent?!)'} "
                  f"exit={red['exit']} {red['summary']}]")

        for i in range(len(candidates), n):
            ex.reset_clean()  # clean base+test_patch per attempt (idempotent & safe, incl. i==0)
            model, temp, seed = _bon_diversity(i, base_model, escalate, n)
            print(f"  [candidate {i}: model={model} T={temp}] running agent ({max_steps} steps)...")
            meta = run_single(instance, model, max_steps, verbose, governed=False, ex=ex,
                              temperature=temp, seed_hint=seed)
            patch = ex.git_diff()
            gv = (ex.grade_patch(patch) if patch.strip()
                  else {"resolved": False, "summary": "empty patch", "exit": -1})
            candidates.append({
                "i": i, "model": model, "temperature": temp,
                "patch": patch, "patch_sha": _sha(patch), "patch_empty": not patch.strip(),
                "patch_len": len(patch),
                "incontainer_pass": bool(gv["resolved"]), "incontainer_summary": gv.get("summary"),
                "incontainer_exit": gv.get("exit"),
                "steps": meta.get("steps"), "stop_reason": meta.get("stop_reason"),
                "submitted_summary": meta.get("submitted_summary"),
            })
            print(f"    -> in-container {'PASS' if gv['resolved'] else 'fail'} "
                  f"({'empty' if not patch.strip() else str(len(patch)) + 'ch'}, "
                  f"{meta.get('steps')} steps, {gv.get('summary')})")
            result = save(red_resolved, t0)  # incremental checkpoint after EACH candidate

        if not oracle:
            # ORACLE: a guaranteed-resolve (gold) and a guaranteed-fail (empty) through the SAME
            # in-container grader, so we validate BOTH directions even if the stochastic candidates
            # happen to land on one verdict. gold is source-only -> applies onto base+test_patch.
            gold = instance["patch"]
            gold_gv = ex.grade_patch(gold)
            empty_gv = ex.grade_patch("")
            oracle = {"gold_patch": gold, "gold_patch_sha": _sha(gold),
                      "gold_incontainer_pass": bool(gold_gv["resolved"]),
                      "gold_incontainer_summary": gold_gv.get("summary"),
                      "empty_incontainer_pass": bool(empty_gv["resolved"]),
                      "empty_incontainer_summary": empty_gv.get("summary")}
            print(f"  [oracle: gold in-container {'PASS' if gold_gv['resolved'] else 'FAIL (!!)'} "
                  f"| empty in-container {'fail (correct)' if not empty_gv['resolved'] else 'PASS (!!)'}]")
            result = save(red_resolved, t0)
    finally:
        ex.close()

    print(f"  [PRODUCE done ({len(candidates)}/{n} candidates + oracle) -> {cache_path.name}]")
    return result


# --------------------------------------------------------------------------- #
# GRADE — fresh official grade of every distinct non-empty patch (candidates +
# gold). One container at a time; verdict cached per patch-sha so a RAM death only
# loses the in-flight grade.
# --------------------------------------------------------------------------- #
def grade_all(instance: dict, prod: dict, verbose: bool, results_path: Path) -> dict:
    iid = instance["instance_id"]
    short = iid.replace("__", "_")

    # distinct non-empty patches: candidates first (the actual gate), gold last (oracle/plumbing).
    to_grade: dict[str, str] = {}
    for c in prod["candidates"]:
        if not c["patch_empty"]:
            to_grade.setdefault(c["patch_sha"], c["patch"])
    if prod["oracle"].get("gold_patch"):
        to_grade.setdefault(prod["oracle"]["gold_patch_sha"], prod["oracle"]["gold_patch"])

    results = json.loads(results_path.read_text()) if results_path.exists() else {}
    for sha, patch in to_grade.items():
        if sha in results and results[sha] is not None:
            print(f"  [official {sha}: cached -> {'RESOLVED' if results[sha] else 'not resolved'}]")
            continue
        print(f"  [official grade {sha} ({len(patch)}ch) — fresh container, one at a time ...]")
        t0 = time.time()
        verdict = grade_official({iid: patch}, f"validate_{short}",
                                 f"validate-bestofn-{short}", verbose)
        v = verdict.get(iid)
        results[sha] = v  # may be None if the grader crashed/report missing -> retried on resume
        results_path.write_text(json.dumps(results, indent=2))
        print(f"    -> official {'RESOLVED' if v else ('not resolved' if v is not None else 'ERROR (no report; will retry on resume)')} "
              f"({round(time.time() - t0, 1)}s)")
    return results


# --------------------------------------------------------------------------- #
# COMPARE — per-pair confusion matrix + gate verdict.
# --------------------------------------------------------------------------- #
def compare(prod: dict, official_by_sha: dict) -> dict:
    rows, fp, fn, unknown = [], 0, 0, 0

    def classify(label, incontainer, official):
        nonlocal fp, fn, unknown
        if official is None:
            kind, unknown_inc = "UNKNOWN", 1
        else:
            unknown_inc = 0
            if incontainer and not official:
                kind = "FALSE-POS"
            elif not incontainer and official:
                kind = "FALSE-NEG"
            else:
                kind = "agree"
        return {"item": label, "incontainer": incontainer, "official": official, "kind": kind}, unknown_inc

    for c in prod["candidates"]:
        if c["patch_empty"]:
            # empty patch never resolves officially (nothing applied) -> treat official=False (known).
            row, u = classify(f"cand{c['i']} ({c['model']}, empty)", c["incontainer_pass"], False)
        else:
            official = official_by_sha.get(c["patch_sha"])
            row, u = classify(f"cand{c['i']} ({c['model']}, {c['patch_len']}ch)",
                              c["incontainer_pass"], official)
        row.update(model=c["model"], patch_sha=c["patch_sha"], patch_empty=c["patch_empty"])
        rows.append(row)
        unknown += u
        fp += row["kind"] == "FALSE-POS"
        fn += row["kind"] == "FALSE-NEG"

    orc = prod["oracle"]
    if orc:
        gold_off = official_by_sha.get(orc.get("gold_patch_sha"))
        r, u = classify("GOLD (oracle)", orc["gold_incontainer_pass"], gold_off)
        rows.append(r); unknown += u
        fp += r["kind"] == "FALSE-POS"; fn += r["kind"] == "FALSE-NEG"
        # empty oracle: official not graded (empty never resolves) -> official=False known.
        r2, _ = classify("EMPTY (oracle)", orc["empty_incontainer_pass"], False)
        rows.append(r2)
        fp += r2["kind"] == "FALSE-POS"; fn += r2["kind"] == "FALSE-NEG"

    gate = (fp == 0 and fn == 0 and unknown == 0)
    return {"rows": rows, "false_pos": fp, "false_neg": fn, "unknown": unknown, "gate_pass": gate}


def print_table(prod: dict, cmp: dict) -> None:
    print("\n" + "=" * 78)
    print(f"SELECTOR VALIDATION — {prod['instance_id']}  "
          f"(base={prod['base_model']}, {prod['max_steps']} steps, "
          f"escalate={prod['escalate']}, N={prod['n']})")
    print("=" * 78)
    print(f"{'item':<34}{'in-container':<14}{'official':<14}{'verdict'}")
    print("-" * 78)
    for r in cmp["rows"]:
        ic = "PASS" if r["incontainer"] else "fail"
        of = ("RESOLVED" if r["official"] else "not-resolved") if r["official"] is not None else "??? (no grade)"
        flag = {"FALSE-POS": "  <-- FALSE POSITIVE", "FALSE-NEG": "  <-- FALSE NEGATIVE",
                "UNKNOWN": "  <-- UNGRADED"}.get(r["kind"], "")
        print(f"{r['item']:<34}{ic:<14}{of:<14}{r['kind']}{flag}")
    print("-" * 78)
    print(f"false-positives: {cmp['false_pos']}   false-negatives: {cmp['false_neg']}   "
          f"ungraded: {cmp['unknown']}")
    print(f"GATE: {'PASS — in-container == official on every pair' if cmp['gate_pass'] else 'FAIL — selector disagrees with the official grader'}")
    print("=" * 78 + "\n")


def main() -> None:
    ap = argparse.ArgumentParser(description="Validate the best-of-N in-container selector vs the official grader.")
    ap.add_argument("--instance", required=True)
    ap.add_argument("--model", default="deepseek-v4-flash", help="base model (attempt 0 when --escalate)")
    ap.add_argument("--max-steps", type=int, default=60)
    ap.add_argument("--best-of-n", type=int, default=3)
    ap.add_argument("--escalate", action="store_true", help="use the router's cheap->strong ladder")
    ap.add_argument("--grade-only", action="store_true", help="skip PRODUCE; require a cached candidates file")
    ap.add_argument("--refresh", action="store_true", help="re-run agent trajectories even if cached")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    load_env()
    instance = load_instance(args.instance)
    RUNS_DIR.mkdir(exist_ok=True)
    short = args.instance.replace("__", "_")
    cand_path = RUNS_DIR / f"bestofn_validate_{short}_candidates.json"
    off_path = RUNS_DIR / f"bestofn_validate_{short}_official.json"
    report_path = RUNS_DIR / f"bestofn_validate_{short}_report.json"

    print(f"[validate] {args.instance} | base={args.model} | {args.max_steps} steps | "
          f"N={args.best_of_n} | escalate={args.escalate}")
    print(f"  test cmd: {test_command(instance)}")

    if args.grade_only and not cand_path.exists():
        sys.exit(f"--grade-only needs a cached candidates file: {cand_path}")

    if args.grade_only:
        prod = json.loads(cand_path.read_text())
        print(f"  [PRODUCE skipped (--grade-only) -> {cand_path.name}]")
    else:
        prod = produce(instance, args.model, args.max_steps, args.best_of_n,
                       args.escalate, args.verbose, cand_path, args.refresh)

    official_by_sha = grade_all(instance, prod, args.verbose, off_path)
    cmp = compare(prod, official_by_sha)
    print_table(prod, cmp)

    report = {"instance_id": args.instance, "produced": {k: v for k, v in prod.items()
                                                          if k not in ("candidates", "oracle")},
              "official_by_sha": official_by_sha, **cmp,
              "candidates": [{k: v for k, v in c.items() if k != "patch"} for c in prod["candidates"]]}
    report_path.write_text(json.dumps(report, indent=2, default=str))
    print(f"  report -> {report_path.relative_to(ROOT)}")
    sys.exit(0 if cmp["gate_pass"] else 1)


if __name__ == "__main__":
    main()
