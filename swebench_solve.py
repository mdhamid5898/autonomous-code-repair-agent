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
    def reset_clean(self):
        """Reset the repo to the committed base+test_patch state — drop all source edits, untracked
        files, and stale __pycache__ (the state the official grader applies a patch onto)."""
        self._dexec("git reset --hard HEAD -q && git clean -fdq -e .venv && "
                    "find . -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null; true",
                    conda=False, timeout=120)

    def apply_patch(self, patch: str):
        """git-apply a candidate SOURCE patch onto the current (clean) tree. No-op if empty."""
        if patch.strip():
            b = base64.b64encode(patch.encode()).decode()
            self._dexec('printf %s "$D" | base64 -d | git apply --whitespace=nowarn',
                        conda=False, env=[f"D={b}"], timeout=120)

    def _official_verdict(self, blob: str, exit_code: int) -> bool:
        """Resolved verdict that MATCHES the official grader: parse the test log with swebench's repo
        parser and require every FAIL_TO_PASS to pass AND every PASS_TO_PASS to hold — scoring ONLY those
        gold node ids. The bare exit code is NOT equivalent: the eval command runs the whole touched test
        file, which can contain tests outside F2P∪P2P that fail independently (e.g. django runs a module
        with 3 unrelated failures) → exit≠0 while the official grade is RESOLVED. Falls back to exit==0 if
        the parser yields nothing (robustness)."""
        try:
            from swebench.harness.grading import get_eval_tests_report, get_resolution_status
            from swebench.harness.constants import (ResolvedStatus, FAIL_ONLY_REPOS, EvalType,
                                                    FAIL_TO_PASS as K_F2P, PASS_TO_PASS as K_P2P)
            from swebench.harness.log_parsers import MAP_REPO_TO_PARSER
            from swebench.harness.test_spec.test_spec import make_test_spec
            spec = make_test_spec(self.instance)
            status_map = MAP_REPO_TO_PARSER[self.instance["repo"]](blob, spec)
            if not status_map:
                return exit_code == 0
            ids = lambda k: (json.loads(self.instance[k]) if isinstance(self.instance[k], str)
                             else list(self.instance[k]))
            eval_ref = {K_F2P: ids("FAIL_TO_PASS"), K_P2P: ids("PASS_TO_PASS")}
            eval_type = EvalType.FAIL_ONLY if self.instance["repo"] in FAIL_ONLY_REPOS else EvalType.PASS_AND_FAIL
            report = get_eval_tests_report(status_map, eval_ref, eval_type=eval_type)
            return get_resolution_status(report) == ResolvedStatus.FULL.value
        except Exception:
            return exit_code == 0  # parser unavailable/changed -> conservative exit-code proxy

    def _run_test_cmd(self) -> dict:
        """Run THIS instance's test command (== the OFFICIAL eval command: repo test_cmd + the directives
        from the test_patch, i.e. the WHOLE touched test file(s)), then compute an OFFICIAL-EQUIVALENT
        verdict by parsing the log for exactly the FAIL_TO_PASS/PASS_TO_PASS gold node ids — not the bare
        exit code, which false-negatives when the file has unrelated failing tests (the django case). Still
        a real PASS_TO_PASS regression guard (a destructive patch breaks a P2P node -> not resolved)."""
        cmd = test_command(self.instance)
        code, out, err = self._dexec(cmd, timeout=GRADE_TEST_TIMEOUT, conda=True)
        blob = (out or "") + "\n" + (err or "")
        summary = ""
        for line in reversed(blob.splitlines()):
            if any(w in line for w in ("passed", "failed", "error", "PASSED", "FAILED", "OK", "Ran ")):
                summary = line.strip(" =")
                break
        return {"resolved": self._official_verdict(blob, code), "exit": code, "exit_resolved": code == 0,
                "summary": summary or "(no summary)", "log": tail(blob, 30)}

    def grade_patch(self, patch: str) -> dict:
        """Official-EQUIVALENT in-container grade of a SPECIFIC candidate patch: reset to base+test_patch,
        apply the patch FRESH, run the full test file(s), and score exactly the F2P/P2P node ids via
        swebench's parser (see _run_test_cmd). Lets best-of-N select among candidates cheaply (no
        per-candidate official container spin) while matching what the official grader would say."""
        self.reset_clean()
        self.apply_patch(patch)
        return self._run_test_cmd()

    def run_fail_to_pass(self, clean: bool = True) -> dict:
        """Grade THIS instance in-container. clean=True (default) first RESETs to the committed
        base+test_patch and RE-APPLIES the agent's captured live diff — so the verdict matches what the
        OFFICIAL grader sees (a fresh apply), NOT the agent's drifted live tree (which gave false
        negatives, e.g. multi's Tester said FAIL while the captured patch actually resolved). clean=False
        grades the current tree as-is (used for the red-on-base check, where there are no edits yet)."""
        if clean:
            return self.grade_patch(self.git_diff())
        return self._run_test_cmd()

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
def run_single(instance: dict, model: str, max_steps: int, verbose: bool, governed: bool,
               ex: SweBenchExecutor, temperature: float = 0.0, seed_hint: str = ""):
    import os
    os.environ["_MECHANIC_MODEL"] = model
    client = make_client()
    test_cmd = test_command(instance)
    messages = swe_messages(instance, test_cmd)
    if seed_hint:  # best-of-N diversity: nudge this attempt toward a different hypothesis
        messages.append({"role": "user", "content": seed_hint})
    trace, submitted, stop_reason, steps = [], None, "max_steps", 0
    acted = False                    # has the agent edited SOURCE yet? (resolved ⟺ ≥1 edit)
    act_by = max(8, max_steps // 2)  # anti-paralysis backstop (ported from multi's Coder): a reasoning
    while steps < max_steps:         # model explores forever on big repos; nudge it to COMMIT an edit
        steps += 1
        if not acted and steps > act_by:
            messages.append({"role": "user", "content":
                "You have NOT edited any SOURCE yet and you're past halfway on your step budget. STOP "
                "exploring. Apply your single best str_replace fix to the SOURCE now, run the failing "
                "test to verify, then submit once it passes. A tested edit beats more reading."})
        msg = _chat_with_backoff(client, model, messages, verbose, temperature=temperature)
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
            if name == "str_replace":
                acted = True
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
            "edited": acted, "tool_trace": trace, "messages": messages}


# --------------------------------------------------------------------------- #
# best-of-N: sample N independent repair trajectories, keep the first that the
# in-container grade accepts (F2P flips AND P2P holds). This is the residual lever the
# iso-control + breadth experiments pointed to — django-11138 only resolved via multi's
# FOUR re-plan rounds (≈ four fix attempts), not decomposition; best-of-N captures that
# "more attempts" win for the SINGLE agent, while the in-container grade guards against a
# destructive candidate (the sympy patch that broke 74 PASS_TO_PASS) being accepted.
# --------------------------------------------------------------------------- #
# Per-attempt seed nudges (i>0): cheap trajectory diversity that does NOT depend on the
# model honoring `temperature` (reasoning tiers like v4-pro may ignore it).
_BON_SEEDS = [
    "",  # attempt 0 = the canonical run (temperature 0, no nudge)
    "(Solution attempt #2. If the obvious fix is at the call site, consider instead that the "
    "ROOT CAUSE may live deeper — in the underlying function/class this path delegates to.)",
    "(Solution attempt #3. Re-examine your localization from scratch: grep for OTHER code paths "
    "that produce the same symptom; the responsible spot may be a different file than you'd assume.)",
    "(Solution attempt #4. Prefer the smallest, most targeted change that makes the test pass "
    "WITHOUT altering unrelated behavior — a narrow guard/branch rather than a broad rewrite.)",
]


def run_best_of_n(instance: dict, model: str, max_steps: int, verbose: bool,
                  ex: SweBenchExecutor, n: int = 3, early_stop: bool = True, models: list | None = None):
    """Run the single agent up to N times (varied temperature + per-attempt seed nudge), grade each
    captured patch in-container (reset+apply+full-file test == the official eval command), and KEEP
    the first candidate that passes (F2P flips AND P2P holds). Leaves the winning patch applied so the
    caller's git_diff()/official grade see it. Falls back to the largest non-empty patch if none pass.
    `models` (optional, from the router's escalation ladder) sets a per-attempt model id — e.g.
    [v4-flash, v4-pro, ...] = cheap first, escalate to strong on retry; if None, all attempts use `model`."""
    candidates = []
    for i in range(n):
        if i > 0:
            ex.reset_clean()  # independent fresh start per attempt
        attempt_model = models[i] if (models and i < len(models)) else model
        temp = round(0.0 if i == 0 else min(0.4 + 0.3 * (i - 1), 1.0), 2)
        seed = _BON_SEEDS[i] if i < len(_BON_SEEDS) else _BON_SEEDS[-1]
        meta = run_single(instance, attempt_model, max_steps, verbose, governed=False, ex=ex,
                          temperature=temp, seed_hint=seed)
        patch = ex.git_diff()
        gv = (ex.grade_patch(patch) if patch.strip()
              else {"resolved": False, "exit": -1, "summary": "empty patch"})
        candidates.append({"i": i, "model": attempt_model, "temperature": temp, "patch": patch,
                           "patch_empty": not patch.strip(),
                           "incontainer_pass": bool(gv["resolved"]), "grade_summary": gv.get("summary"),
                           "steps": meta.get("steps"), "stop_reason": meta.get("stop_reason"),
                           "submitted_summary": meta.get("submitted_summary")})
        if verbose:
            print(f"  [best-of-N cand {i} ({attempt_model} @T={temp}): "
                  f"{'PASS ✅' if gv['resolved'] else 'fail'} "
                  f"({'empty' if not patch.strip() else str(len(patch)) + 'ch'}, {meta.get('steps')} steps, "
                  f"{gv.get('summary')})]")
        if gv["resolved"] and early_stop:
            break  # first passing candidate wins — stop sampling (the cost-efficient default)

    winner = next((c for c in candidates if c["incontainer_pass"]), None)
    if winner is None:  # nothing passed — submit the most substantive attempt for the official grade
        non_empty = [c for c in candidates if not c["patch_empty"]]
        winner = max(non_empty, key=lambda c: len(c["patch"])) if non_empty else candidates[-1]
    ex.reset_clean()
    ex.apply_patch(winner["patch"])  # leave winner applied for capture + official grade
    return {"submitted_summary": winner.get("submitted_summary"),
            "stop_reason": f"bestofn_{'pass' if winner['incontainer_pass'] else 'nopass'}",
            "steps": sum((c.get("steps") or 0) for c in candidates),  # total agent steps across attempts
            "edited": any(not c["patch_empty"] for c in candidates),
            "n_candidates": len(candidates), "winner_index": winner["i"],
            "n_passing": sum(c["incontainer_pass"] for c in candidates),
            "candidates": [{k: v for k, v in c.items() if k != "patch"} for c in candidates],
            "tool_trace": [], "messages": []}


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
def solve_instance(instance: dict, engine: str, model: str, budget: int, verbose: bool,
                   n: int = 3, early_stop: bool = True, escalate: bool = False):
    """Provision image, run the agent, capture the patch, return a run record (ungraded).
    `n`/`early_stop`/`escalate` apply only to engine="bestofn": number of sampled candidates and
    whether to use the router's cheap->strong escalation ladder across attempts."""
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
        elif engine == "bestofn":
            models = None
            if escalate:  # cheap-first, escalate to strong on retry (the router's ladder)
                from router import escalation_ladder
                models = escalation_ladder(n)
                if verbose:
                    print(f"  [best-of-N escalation ladder: {models}]")
            run_meta = run_best_of_n(instance, model, budget, verbose, ex=ex, n=n,
                                     early_stop=early_stop, models=models)
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
    ap.add_argument("--engine", choices=["solve", "governed", "multi", "bestofn"], default="solve")
    ap.add_argument("--model", default="deepseek-chat")
    ap.add_argument("--max-steps", type=int, default=40, help="tool-step cap for solve/governed/bestofn (per attempt)")
    ap.add_argument("--max-iterations", type=int, default=4, help="re-plan rounds for multi")
    ap.add_argument("--best-of-n", type=int, default=3, help="candidate trajectories to sample for engine=bestofn")
    ap.add_argument("--no-early-stop", action="store_true",
                    help="for bestofn: sample ALL N candidates even after one passes (characterize pass rate)")
    ap.add_argument("--escalate", action="store_true",
                    help="for bestofn: use the router's cheap->strong ladder (v4-flash, then v4-pro on retry)")
    ap.add_argument("--grade", action="store_true", help="also run the official swebench grader")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    load_env()
    instance = load_instance(args.instance)
    budget = args.max_iterations if args.engine == "multi" else args.max_steps
    RUNS_DIR.mkdir(exist_ok=True)
    print(f"[swe-bench] {args.instance} | engine={args.engine} | model={args.model} | budget={budget}"
          + (f" | best-of-{args.best_of_n}{' (no early-stop)' if args.no_early_stop else ''}"
             if args.engine == "bestofn" else ""))

    rec = solve_instance(instance, args.engine, args.model, budget, args.verbose,
                         n=args.best_of_n, early_stop=not args.no_early_stop, escalate=args.escalate)
    print(f"  agent done: steps={rec.get('steps')} stop={rec.get('stop_reason')} "
          + (f"candidates={rec.get('n_candidates')} passing={rec.get('n_passing')} winner=#{rec.get('winner_index')} "
             if args.engine == "bestofn" else "")
          + f"in-container F2P {'PASS' if rec['incontainer_f2p_pass'] else 'fail'} "
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
    try:
        from tracing import flush; flush()  # send any Langfuse traces (no-op unless enabled)
    except Exception:
        pass


if __name__ == "__main__":
    main()
