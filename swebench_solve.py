#!/usr/bin/env python3
"""
Mechanic — SWE-bench Verified adapter (the breadth regime).

Runs OUR agents on a LARGE-repo, MULTI-FILE SWE-bench instance inside the instance's
prebuilt Docker environment, behind the SAME Executor seam used by solve.py — only this
eval/grade wrapper is new; the agent loop/tools/prompt are reused unchanged.

It mirrors the LOCAL eval procedure (eval/verify.py + solve.py), one-for-one:

    local (eval/issues*.yaml)                 SWE-bench Verified
    -------------------------------------     ----------------------------------------------
    git clone @ base_commit               ->  prebuilt instance image (repo @ base_commit at
                                              /testbed, deps installed in the `testbed` conda env)
    drop eval/repros/<id>.py              ->  git apply the instance test_patch (adds the REAL
                                              FAIL_TO_PASS test the fix must flip)
    confirm repro is RED on base         ->  confirm FAIL_TO_PASS is red on base
    agent loop (bash/str_replace/submit)  ->  SAME loop, SweBenchExecutor (edits IN the container)
    grade(): pristine repro red->green    ->  official `swebench.harness.run_evaluation`
                                              (FAIL_TO_PASS flips + PASS_TO_PASS holds, in a
                                              fresh container — the authoritative grade)
    (n/a)                                 ->  capture `git diff` = the candidate model_patch

We ADOPT SWE-bench (images + grader); only this thin adapter is new. NOTE: like our local
sets, we GIVE the agent the failing test to iterate against (slightly easier than blind
SWE-bench, but identical for single vs multi — the comparison is what we're measuring).

CAVEAT: these big repos (django/sympy/…) are almost certainly in the model's pretraining
(contamination) — it inflates ABSOLUTE resolution but hits single AND multi equally, so the
single-vs-multi comparison stays valid.

Usage:
  .venv/bin/python swebench_solve.py --instance sphinx-doc__sphinx-10673 --engine solve --verbose
  .venv/bin/python swebench_solve.py --instance sphinx-doc__sphinx-10673 --engine governed --grade
  .venv/bin/python swebench_solve.py --instance django__django-11138 --engine multi --max-iterations 4 --grade
"""
from __future__ import annotations

import argparse
import base64
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "eval"))

from verify import run, tail  # noqa: E402  (subprocess helper, shared with the local harness)
from solve import (  # noqa: E402  — reuse the agent primitives verbatim (loop stays unchanged)
    Executor, TOOLS, dispatch, make_client, load_env, _chat_with_backoff, RUNS_DIR,
    TOOL_OUTPUT_CAP, BASH_TIMEOUT,
)

DATASET = "SWE-bench/SWE-bench_Verified"
GRADE_TEST_TIMEOUT = 1200      # in-container F2P run (emulated; can be slow on big suites)
PULL_TIMEOUT = 2400
CONDA = "source /opt/miniconda3/bin/activate && conda activate testbed"
TESTBED = "/testbed"

_DS_CACHE = None


def load_instance(instance_id: str) -> dict:
    """Fetch one SWE-bench Verified row (dataset cached across calls)."""
    global _DS_CACHE
    if _DS_CACHE is None:
        from datasets import load_dataset
        _DS_CACHE = load_dataset(DATASET, split="test")
    for r in _DS_CACHE:
        if r["instance_id"] == instance_id:
            return dict(r)
    sys.exit(f"instance '{instance_id}' not in {DATASET}")


def test_command(instance: dict) -> str:
    """The repo-correct command to run this instance's FAIL_TO_PASS tests (pytest/django/…),
    straight from swebench's per-repo spec — so the agent runs the REAL failing test."""
    from swebench.harness.constants import MAP_REPO_VERSION_TO_SPECS
    from swebench.harness.test_spec.python import get_test_directives
    spec = MAP_REPO_VERSION_TO_SPECS[instance["repo"]][instance["version"]]
    directives = get_test_directives(instance)
    return f"{spec['test_cmd']} {' '.join(directives)}"


# --------------------------------------------------------------------------- #
# image provisioning (pull the prebuilt x86_64 image; build only as a fallback)
# --------------------------------------------------------------------------- #
def _image_exists(name: str) -> bool:
    code, out, _ = run(["docker", "images", "-q", name], timeout=30)
    return code == 0 and bool(out.strip())


def ensure_image(instance: dict, verbose: bool) -> str:
    """Return a usable local image tag for the instance. Prefer an already-cached image,
    else pull the prebuilt x86_64 image from the swebench namespace (no local build)."""
    iid = instance["instance_id"]
    escaped = iid.replace("__", "_1776_")
    hub = f"swebench/sweb.eval.x86_64.{escaped}:latest"
    local = f"sweb.eval.x86_64.{iid}:latest"
    for cand in (hub, local):
        if _image_exists(cand):
            if verbose:
                print(f"  [image cached: {cand}]")
            return cand
    if verbose:
        print(f"  [pulling {hub} (prebuilt, ~2-4GB) ...]")
    code, out, err = run(["docker", "pull", "--platform", "linux/amd64", hub], timeout=PULL_TIMEOUT)
    if code == 0:
        return hub
    sys.exit(f"could not provision image for {iid}: {tail(err or out)}\n"
             f"(fallback: build it via `python -m swebench.harness.run_evaluation "
             f"--predictions_path gold --instance_ids {iid} --run_id build_{escaped} --cache_level instance`)")


# --------------------------------------------------------------------------- #
# the SWE-bench executor — same seam as DockerExecutor, but the repo lives INSIDE
# the instance image at /testbed (not bind-mounted), so edits happen in-container.
# --------------------------------------------------------------------------- #
class SweBenchExecutor(Executor):
    def __init__(self, instance: dict, image: str, verbose: bool = False):
        self.instance = instance
        self.image = image
        self.repo_dir = Path(TESTBED)           # nominal; edits go through docker exec
        self.name = f"mechanic_swe_{instance['instance_id'].replace('__', '_')}"
        self.verbose = verbose
        run(["docker", "rm", "-f", self.name], timeout=60)
        code, out, err = run(
            ["docker", "run", "-d", "--platform", "linux/amd64", "--name", self.name,
             "-w", TESTBED, image, "sleep", "infinity"], timeout=180)
        if code != 0:
            raise RuntimeError(f"docker run failed: {tail(err or out)}")
        self._prepare()

    def _dexec(self, cmd: str, timeout: int = BASH_TIMEOUT, conda: bool = True, env: list | None = None):
        full = f"{CONDA} && {cmd}" if conda else cmd
        argv = ["docker", "exec"]
        for e in (env or []):
            argv += ["-e", e]
        argv += ["-w", TESTBED, self.name, "bash", "-lc", full]
        return run(argv, timeout=timeout)

    def _prepare(self):
        """Reset the repo to a clean base, then apply the test_patch so the FAIL_TO_PASS test
        exists, and COMMIT it — so the agent's later `git diff HEAD` is SOURCE-only."""
        self._dexec("git config user.email m@x.io && git config user.name mechanic "
                    "&& git checkout -- . && git clean -fdq", conda=False)
        tp_b64 = base64.b64encode(self.instance["test_patch"].encode()).decode()
        code, out, err = self._dexec(
            "printf %s \"$MECH_TP\" | base64 -d | git apply -v",
            conda=False, env=[f"MECH_TP={tp_b64}"], timeout=120)
        if code != 0:
            raise RuntimeError(f"applying test_patch failed: {tail(err or out)}")
        self._dexec("git add -A && git commit -q -m 'swebench test_patch'", conda=False)

    # --- agent-facing seam ---
    def exec_raw(self, cmd: str):
        return self._dexec(cmd, timeout=BASH_TIMEOUT, conda=True)

    def str_replace(self, path: str, old_str: str, new_str: str) -> str:
        """Unique-match source edit INSIDE the container (mirrors solve.Executor.str_replace)."""
        prog = (
            'import os,base64,sys\n'
            'p=os.environ["MECH_P"]\n'
            'o=base64.b64decode(os.environ["MECH_O"]).decode()\n'
            'n=base64.b64decode(os.environ["MECH_N"]).decode()\n'
            'import pathlib\n'
            'if not pathlib.Path(p).exists(): sys.exit(4)\n'
            't=open(p,encoding="utf-8").read(); c=t.count(o)\n'
            'if c==0: sys.exit(2)\n'
            'if c>1: sys.exit(3)\n'
            'open(p,"w",encoding="utf-8").write(t.replace(o,n,1))\n')
        env = [f"MECH_P={path}",
               f"MECH_O={base64.b64encode(old_str.encode()).decode()}",
               f"MECH_N={base64.b64encode(new_str.encode()).decode()}",
               f"MECH_PROG={base64.b64encode(prog.encode()).decode()}"]
        code, out, err = self._dexec('printf %s "$MECH_PROG" | base64 -d | python', env=env, timeout=60)
        return {0: f"OK: edited {path}", 2: f"ERROR: old_str not found in {path} (must match exactly)",
                3: f"ERROR: old_str occurs >1x in {path}; add context to make it unique",
                4: f"ERROR: {path} does not exist"}.get(code, f"ERROR: edit failed: {tail(err or out)}")

    # --- grading / capture ---
    def run_fail_to_pass(self) -> dict:
        """Run THIS instance's FAIL_TO_PASS tests in-container. exit 0 => they pass.
        Compact verdict for the agent's run_test + the governed gate (the official
        run_evaluation is the authoritative grade, incl. PASS_TO_PASS)."""
        cmd = test_command(self.instance)
        code, out, err = self._dexec(cmd, timeout=GRADE_TEST_TIMEOUT, conda=True)
        blob = (out or "") + "\n" + (err or "")
        summary = ""
        for line in reversed(blob.splitlines()):
            if any(w in line for w in ("passed", "failed", "error", "PASSED", "FAILED", "OK", "Ran ")):
                summary = line.strip(" =")
                break
        return {"resolved": code == 0, "exit": code, "summary": summary or "(no summary)",
                "log": tail(blob, 30)}

    def git_diff(self) -> str:
        """The agent's candidate patch: SOURCE diff vs the test_patch commit."""
        code, out, err = self._dexec("git add -A && git diff --cached HEAD", conda=False, timeout=60)
        return out or ""

    def close(self):
        run(["docker", "rm", "-f", self.name], timeout=60)


# --------------------------------------------------------------------------- #
# prompts (close to solve.SYSTEM_PROMPT, adapted: large repo, real test command)
# --------------------------------------------------------------------------- #
SWE_SYSTEM_PROMPT = """You are an autonomous software engineer fixing a real bug in a large Python repository.

The repo is checked out at a commit that EXHIBITS the bug. A failing test has been added that \
encodes the required behavior. Make that test pass by editing the SOURCE code only. NEVER edit a test file.

Tools: `bash` (explore + run tests), `str_replace` (precise source edits), `submit` (finish).
Run the failing test with EXACTLY this command:
    {test_cmd}
Commands run from the repo root ({testbed}); use repo-relative paths, never `cd /`, and don't search the whole FS.

Method:
1. Run the failing test to see the error.
2. Localize: this fix likely spans MORE THAN ONE source file — grep/read to find every responsible spot.
3. Make minimal, correct source edits (prefer str_replace). Re-run the test. Iterate until it PASSES.
4. Call `submit` with a one-line root-cause + fix summary.
Keep changes targeted. Do not install packages."""


def swe_messages(instance: dict, test_cmd: str) -> list:
    brief = (f"repo: {instance['repo']}\ninstance: {instance['instance_id']}\n\n"
             f"Problem statement (the GitHub issue):\n{instance['problem_statement']}")
    return [
        {"role": "system", "content": SWE_SYSTEM_PROMPT.format(test_cmd=test_cmd, testbed=TESTBED)},
        {"role": "user", "content": f"Fix this bug:\n\n{brief}\n\nStart by running the failing test."},
    ]


# --------------------------------------------------------------------------- #
# the single / governed agent loop (thin; reuses solve's dispatch/TOOLS/backoff)
# --------------------------------------------------------------------------- #
def run_single(instance: dict, model: str, max_steps: int, verbose: bool, governed: bool, ex: SweBenchExecutor):
    import os
    os.environ["_MECHANIC_MODEL"] = model
    client = make_client()
    test_cmd = test_command(instance)
    messages = swe_messages(instance, test_cmd)
    trace, submitted, stop_reason, steps = [], None, "max_steps", 0
    while steps < max_steps:
        steps += 1
        msg = _chat_with_backoff(client, model, messages, verbose)
        if msg is None:
            stop_reason = "api_error"; break
        a = {"role": "assistant", "content": msg.content or ""}
        if msg.tool_calls:
            a["tool_calls"] = [{"id": tc.id, "type": "function",
                                "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                               for tc in msg.tool_calls]
        messages.append(a)
        if not msg.tool_calls:
            stop_reason = "model_stopped"; break
        for tc in msg.tool_calls:
            name = tc.function.name
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            if governed and name == "submit":
                gv = ex.run_fail_to_pass()
                if gv["resolved"]:
                    result, submitted = "ACCEPTED — the tests pass. Done.", args.get("summary", "")
                else:
                    result = (f"REJECTED — FAIL_TO_PASS still fails, you are not done:\n{gv['log']}\n"
                              "Keep editing the SOURCE and re-run the test; submit only once it passes.")
            else:
                result = dispatch(ex, name, args)
                if name == "submit":
                    submitted = args.get("summary", "")
            if verbose:
                prev = args.get("cmd") or args.get("path") or args.get("summary") or ""
                print(f"[{steps}] {name}({str(prev)[:70]}) -> {result.splitlines()[0][:70] if result else ''}")
            trace.append({"step": steps, "tool": name, "args": args, "result": result})
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})
        if submitted is not None:
            stop_reason = "submitted"; break
    return {"submitted_summary": submitted, "stop_reason": stop_reason, "steps": steps,
            "tool_trace": trace, "messages": messages}


# --------------------------------------------------------------------------- #
# official grade
# --------------------------------------------------------------------------- #
def grade_official(predictions: dict, run_id: str, model_name: str, verbose: bool) -> dict:
    """predictions: {instance_id: model_patch}. Runs swebench.harness.run_evaluation in a
    fresh container per instance and returns {instance_id: resolved_bool}."""
    preds_path = ROOT / f"predictions_{run_id}.jsonl"
    preds_path.write_text("\n".join(json.dumps(
        {"instance_id": iid, "model_name_or_path": model_name, "model_patch": patch})
        for iid, patch in predictions.items()))
    cmd = [sys.executable, "-m", "swebench.harness.run_evaluation",
           "--dataset_name", DATASET, "--predictions_path", str(preds_path),
           "--run_id", run_id, "--instance_ids", *list(predictions),
           "--max_workers", "1", "--cache_level", "env", "--timeout", "1800"]
    if verbose:
        print(f"  [official grade: run_evaluation run_id={run_id} ...]")
    run(cmd, timeout=3600 * 3)
    report = ROOT / f"{model_name}.{run_id}.json"
    if not report.exists():
        return {iid: None for iid in predictions}
    d = json.loads(report.read_text())
    resolved = set(d.get("resolved_ids", []))
    return {iid: (iid in resolved) for iid in predictions}


# --------------------------------------------------------------------------- #
# driver
# --------------------------------------------------------------------------- #
def solve_instance(instance: dict, engine: str, model: str, budget: int, verbose: bool):
    """Provision image, run the agent, capture the patch, return a run record (ungraded)."""
    image = ensure_image(instance, verbose)
    ex = SweBenchExecutor(instance, image, verbose)
    t0 = time.time()
    try:
        red = ex.run_fail_to_pass()
        if verbose:
            print(f"  [red-on-base: {'RED ✅' if not red['resolved'] else 'GREEN ⚠️ (bug absent?)'} "
                  f"exit={red['exit']} {red['summary']}]")
        if engine == "multi":
            from multi_agent import run_multi_agent
            rec = run_multi_agent(instance, model, budget, verbose, ex=ex,
                                  grade_fn=lambda inst, e: e.run_fail_to_pass(),
                                  test_name=test_command(instance))
            run_meta = {"steps": rec.get("iterations"), "stop_reason": rec.get("status"),
                        "submitted_summary": None, "tool_trace": rec.get("tool_trace", []),
                        "decision_log": rec.get("decision_log", [])}
        else:
            run_meta = run_single(instance, model, budget, verbose, governed=(engine == "governed"), ex=ex)
        patch = ex.git_diff()
        final = ex.run_fail_to_pass()
    finally:
        ex.close()
    return {"instance_id": instance["instance_id"], "repo": instance["repo"], "engine": engine,
            "model": model, "red_on_base_failed": not red["resolved"],
            "incontainer_f2p_pass": final["resolved"], "patch": patch,
            "patch_empty": not patch.strip(), "seconds": round(time.time() - t0, 1), **run_meta}


def main():
    ap = argparse.ArgumentParser(description="Run a Mechanic engine on one SWE-bench Verified instance.")
    ap.add_argument("--instance", required=True)
    ap.add_argument("--engine", choices=["solve", "governed", "multi"], default="solve")
    ap.add_argument("--model", default="deepseek-chat")
    ap.add_argument("--max-steps", type=int, default=40, help="tool-step cap for solve/governed")
    ap.add_argument("--max-iterations", type=int, default=4, help="re-plan rounds for multi")
    ap.add_argument("--grade", action="store_true", help="also run the official swebench grader")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    load_env()
    instance = load_instance(args.instance)
    budget = args.max_iterations if args.engine == "multi" else args.max_steps
    RUNS_DIR.mkdir(exist_ok=True)
    print(f"[swe-bench] {args.instance} | engine={args.engine} | model={args.model} | budget={budget}")

    rec = solve_instance(instance, args.engine, args.model, budget, args.verbose)
    print(f"  agent done: steps={rec.get('steps')} stop={rec.get('stop_reason')} "
          f"in-container F2P {'PASS' if rec['incontainer_f2p_pass'] else 'fail'} "
          f"patch={'EMPTY' if rec['patch_empty'] else str(len(rec['patch']))+' chars'} ({rec['seconds']}s)")

    if args.grade and not rec["patch_empty"]:
        model_name = f"mechanic-{args.engine}-{args.model}"
        run_id = f"swe_{args.engine}_{args.instance.replace('__', '_')}"
        verdict = grade_official({args.instance: rec["patch"]}, run_id, model_name, args.verbose)
        rec["official_resolved"] = verdict.get(args.instance)
        print(f"  OFFICIAL GRADE: {'RESOLVED ✅' if rec['official_resolved'] else 'not resolved'}")

    out = RUNS_DIR / f"swe_{args.instance.replace('__', '_')}_{args.engine}_{int(time.time())}.json"
    out.write_text(json.dumps(rec, indent=2, default=str))
    print(f"  trace -> {out.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
