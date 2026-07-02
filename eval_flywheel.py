#!/usr/bin/env python3
"""
TODO #5 — CI feedback flywheel: a failed trace becomes a new, VERIFIED eval case.

The eval set should LEARN from failures: when the agent (or production) hits a bug the benchmark
doesn't cover, capture it as a permanent regression case so every future agent version is tested
against it. This is the "+ feedback" half of the CI story.

The loop, and its guardrail:

    failed trace / bug report
        │  extract (repo, clone, base_commit, problem statement, error tail)
        ▼
    LLM drafts a minimal repro test  ──────────────┐  (the "auto-generated" part)
        │  materialize: eval/repros/<id>.py         │
        │  + an entry in eval/issues_flywheel.yaml   │ retry (feed the verdict back)
        ▼                                            │
    verify.py RED-ON-BASE gate ─(not READY)──────────┘
        │  (READY ⟺ the repro FAILS at base_commit = the bug is genuinely reproduced;
        │   REPRO_ERROR/BUG_ABSENT are rejected — a broken or non-reproducing test never gets in)
        ▼
    ACCEPT → the case is now a permanent part of the benchmark.

The LLM PROPOSES the repro; verify.py's deterministic red-on-base check DISPOSES — exactly the
"no model self-certification" discipline used everywhere else in this project. A drafted test that
imports-errors or that passes on the buggy code is rejected, and the flywheel retries with that
feedback, so only a test that truly reproduces the bug is admitted.

Usage:
  .venv/bin/python eval_flywheel.py --demo            # run the built-in furl multi-'@' failure end-to-end
  .venv/bin/python eval_flywheel.py --from-trace runs/<id>.json   # seed from a real failed run trace
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Callable, Optional

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "eval"))

import yaml  # noqa: E402
from solve import make_client, load_env, DEFAULT_MODEL  # noqa: E402
import verify  # noqa: E402  (the eval harness: process() + REPOS_DIR/REPROS_DIR + red-on-base gate)


def _plain_completion(client, model: str, messages: list, temperature: float = 0.3, attempts: int = 5) -> str:
    """A PLAIN chat completion (no tools). solve._chat_with_backoff always attaches the agent's
    bash/str_replace/submit tools with tool_choice=auto, so for a text task the model returns an
    empty message + a tool call — useless here. This asks for content directly, with simple backoff."""
    import time as _t
    last = None
    for i in range(attempts):
        try:
            resp = client.chat.completions.create(model=model, messages=messages, temperature=temperature)
            return resp.choices[0].message.content or ""
        except Exception as e:  # rate limit / transient API error → back off and retry
            last = e
            _t.sleep(min(2 ** i, 20))
    raise RuntimeError(f"model call failed while generating the repro: {last}")

FLYWHEEL_MANIFEST = ROOT / "eval" / "issues_flywheel.yaml"
REPROS_DIR = ROOT / "eval" / "repros"

GEN_SYSTEM = """You extend a bug-regression benchmark. Given a bug report for a Python package at a \
specific commit, you write a MINIMAL, standalone pytest file that REPRODUCES the bug: it must FAIL with \
an AssertionError against the buggy code and PASS once the bug is fixed. You assert the CORRECT expected \
behavior directly. Output ONLY python code — no prose, no markdown fences."""

GEN_USER = """Package: {pkg}   (import as `{import_name}`)
Repo: {repo}   Commit (bug present): {base_commit}

Bug report:
{problem_statement}
{error_block}
Write the repro test file now. Requirements:
- `import {import_name}` (or `from {import_name} import ...`).
- EXACTLY ONE test function named `test_...`.
- Assert the CORRECT behavior with plain `assert` (so it FAILS while the bug is present, PASSES once fixed).
- Do NOT use `pytest.raises` unless the bug itself is a wrong/raised exception.
- No network, no file I/O, no custom fixtures or plugins. Self-contained.
- Output ONLY the code."""


def _strip_fences(text: str) -> str:
    """LLMs often wrap code in ```python fences despite instructions — strip them."""
    t = (text or "").strip()
    if t.startswith("```"):
        lines = t.splitlines()
        lines = lines[1:]                       # drop opening ```lang
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        t = "\n".join(lines)
    return t.strip() + "\n"


def generate_repro(context: dict, client, model: str, prior_failure: Optional[dict] = None) -> str:
    """Ask the model to draft a repro test. On retry, prior_failure carries the last verdict/log so
    it can fix a repro that didn't reproduce (e.g. import error, or the assertion passed on base)."""
    err = context.get("error_tail") or ""
    error_block = (f"\nObserved error/output when the bug triggers:\n{err}\n" if err else "\n")
    user = GEN_USER.format(pkg=context["pkg"], import_name=context["import_name"],
                           repo=context["repo"], base_commit=context["base_commit"],
                           problem_statement=context["problem_statement"], error_block=error_block)
    msgs = [{"role": "system", "content": GEN_SYSTEM}, {"role": "user", "content": user}]
    if prior_failure:
        msgs.append({"role": "user", "content":
            f"Your previous repro was REJECTED by the red-on-base gate (verdict={prior_failure['verdict']}). "
            f"It must FAIL with an AssertionError at the buggy commit (not error out, not pass). "
            f"Fix it. Gate log:\n{prior_failure.get('log','')[:1200]}\n\nHere was your previous attempt:\n"
            f"{prior_failure.get('repro','')}"})
    # retries nudge temperature up a touch for diversity when the first draft didn't reproduce
    temp = 0.3 + (0.2 if prior_failure else 0.0)
    return _strip_fences(_plain_completion(client, model, msgs, temperature=temp))


def materialize_case(case_id: str, context: dict, repro_src: str, repro_verified: bool = False) -> dict:
    """Write eval/repros/<id>.py and upsert an entry into the flywheel manifest. Returns the issue dict."""
    REPROS_DIR.mkdir(parents=True, exist_ok=True)
    (REPROS_DIR / f"{case_id}.py").write_text(repro_src)
    issue = {
        "id": case_id, "repo": context["repo"], "url": context.get("url", ""),
        "issue": context.get("issue", 0), "clone": context["clone"],
        "base_commit": context["base_commit"], "repro_verified": repro_verified,
        "bug_type": context.get("bug_type", "regression"),
        "bug_summary": context.get("problem_statement", "")[:200],
        "repro": "generated", "fix_hint": context.get("fix_hint", "(auto-generated by the flywheel)"),
        "fix_size": "unknown", "tier": "flywheel", "test_framework": "pytest",
        "install": context.get("install", "pip install -e ."),
        "test_cmd": context.get("test_cmd", "pytest -q"), "flags": ["auto-generated"],
        "source_trace": context.get("source_trace"),
    }
    doc = {"issues": []}
    if FLYWHEEL_MANIFEST.exists():
        doc = yaml.safe_load(FLYWHEEL_MANIFEST.read_text()) or {"issues": []}
    doc["issues"] = [i for i in doc.get("issues", []) if i["id"] != case_id] + [issue]
    FLYWHEEL_MANIFEST.write_text(yaml.safe_dump(doc, sort_keys=False))
    return issue


def verify_case(issue: dict, fresh: bool = False) -> dict:
    """Run the eval harness's red-on-base check on this one case (clone @ base_commit, install, drop the
    repro, run it, EXPECT it to fail). Returns the verify record; rec['verdict']=='READY' == genuinely
    reproduces. Reuses an existing clone+venv when present (fast self-check)."""
    verify.MANIFEST = FLYWHEEL_MANIFEST
    repo_dir = verify.REPOS_DIR / issue["id"]
    have_repo = repo_dir.exists() and verify.venv_paths(repo_dir)[0].exists()
    args = SimpleNamespace(only=issue["id"], manifest=str(FLYWHEEL_MANIFEST), no_install=False,
                           no_repro=False, repro_only=(have_repo and not fresh), fresh=fresh)
    return verify.process(issue, args)


def remove_case(case_id: str) -> None:
    """Roll back a rejected case: drop its repro file + manifest entry (keep the benchmark clean)."""
    (REPROS_DIR / f"{case_id}.py").unlink(missing_ok=True)
    if FLYWHEEL_MANIFEST.exists():
        doc = yaml.safe_load(FLYWHEEL_MANIFEST.read_text()) or {"issues": []}
        doc["issues"] = [i for i in doc.get("issues", []) if i["id"] != case_id]
        FLYWHEEL_MANIFEST.write_text(yaml.safe_dump(doc, sort_keys=False))


def run_flywheel(context: dict, client, model: str, max_attempts: int = 3,
                 gen: Optional[Callable] = None, verifier: Optional[Callable] = None,
                 verbose: bool = True) -> dict:
    """failed trace -> generate repro -> RED-ON-BASE gate -> accept (or retry, then reject).
    `gen`/`verifier` are injectable (defaults call the real LLM + verify.py) so the control flow is
    unit-testable without an API or Docker."""
    gen = gen or (lambda ctx, pf: generate_repro(ctx, client, model, prior_failure=pf))
    verifier = verifier or (lambda issue: verify_case(issue))
    case_id = context["id"]
    attempts, prior = [], None
    for k in range(1, max_attempts + 1):
        repro_src = gen(context, prior)
        issue = materialize_case(case_id, context, repro_src)
        rec = verifier(issue)
        verdict = rec.get("verdict")
        status = (rec.get("repro") or {}).get("status")
        log = (rec.get("repro") or {}).get("log") or (rec.get("install") or {}).get("log") or ""
        if verbose:
            print(f"  [attempt {k}/{max_attempts}] verdict={verdict} repro_status={status}")
        attempts.append({"attempt": k, "verdict": verdict, "repro_status": status})
        if verdict == "READY":                          # red-on-base confirmed → accept
            materialize_case(case_id, context, repro_src, repro_verified=True)  # persist the verified flag
            return {"accepted": True, "case_id": case_id, "attempts": attempts,
                    "manifest": str(FLYWHEEL_MANIFEST), "repro_path": f"eval/repros/{case_id}.py",
                    "verify_record": rec}
        prior = {"verdict": verdict, "log": log, "repro": repro_src}
    remove_case(case_id)                                 # nothing reproduced → don't pollute the set
    return {"accepted": False, "case_id": case_id, "attempts": attempts,
            "reason": f"no drafted repro reproduced the bug in {max_attempts} attempts"}


# --------------------------------------------------------------------------- #
# seeds — turn a failed trace (or a built-in demo) into a flywheel context.
# --------------------------------------------------------------------------- #
def context_from_trace(trace_path: Path) -> dict:
    """Build a flywheel context from a real failed run trace (runs/*.json) + its issue metadata."""
    rec = json.loads(Path(trace_path).read_text())
    iid = rec.get("issue") or rec.get("instance_id")
    issue = next((i for i in yaml.safe_load((ROOT / "eval" / "issues.yaml").read_text())["issues"]
                  if i["id"] == iid), None)
    if not issue:
        sys.exit(f"could not find issue metadata for '{iid}' (trace not from the local eval set)")
    err = ""
    for t in reversed(rec.get("tool_trace", [])):
        if "test" in str(t.get("args", {})).lower() or t.get("tool") == "bash":
            err = str(t.get("result", ""))[:800]; break
    return {"id": f"{iid}-fw", "repo": issue["repo"], "clone": issue["clone"],
            "base_commit": issue["base_commit"], "url": issue.get("url", ""),
            "pkg": issue["repo"].split("/")[-1], "import_name": issue["repo"].split("/")[-1].replace("-", "_"),
            "problem_statement": issue.get("bug_summary", ""), "error_tail": err,
            "install": issue.get("install", "pip install -e ."), "test_cmd": issue.get("test_cmd", "pytest -q"),
            "source_trace": str(trace_path)}


DEMO_CONTEXT = {
    # A realistic "field failure" report — the flywheel is given ONLY a natural-language description
    # (no ready-made repro) and must manufacture a verified regression case from it. furl is tiny and
    # already cloned locally, so the red-on-base gate runs fast.
    "id": "furl-multiat-fw",
    "repo": "gruns/furl", "clone": "https://github.com/gruns/furl.git",
    "base_commit": "46d9ea79c98bb14b970a199fb924705d024f29ad",
    "url": "https://github.com/gruns/furl/issues/163",
    "pkg": "furl", "import_name": "furl", "issue": 163,
    "problem_statement": (
        "furl mis-parses a URL whose userinfo itself contains an '@'. For a URL like "
        "'http://user:pass@word@host.com/path', furl treats 'word@host.com' as the host, i.e. it splits the "
        "netloc on the FIRST '@' instead of the LAST. Expected: host == 'host.com', username == 'user', "
        "password == 'pass@word'. The netloc must be split on the last '@' (userinfo is everything before it)."),
    "error_tail": "", "bug_type": "wrong-value",
    "fix_hint": "netloc setter split('@',1) should be rsplit('@',1)",
    "install": "pip install -e .", "test_cmd": "pytest -q", "source_trace": "(demo)",
}


def main():
    ap = argparse.ArgumentParser(description="CI feedback flywheel: failed trace -> verified new eval case.")
    ap.add_argument("--demo", action="store_true", help="run the built-in furl multi-'@' failure end-to-end")
    ap.add_argument("--from-trace", help="path to a failed run trace (runs/*.json) to seed the case")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--max-attempts", type=int, default=3)
    args = ap.parse_args()
    if not (args.demo or args.from_trace):
        ap.error("pass --demo or --from-trace <path>")

    load_env()
    context = DEMO_CONTEXT if args.demo else context_from_trace(Path(args.from_trace))
    client = make_client()
    print(f"[flywheel] seeding new eval case '{context['id']}' from "
          f"{'the built-in demo failure' if args.demo else args.from_trace}")
    print(f"  bug: {context['problem_statement'][:100]}...")

    t0 = time.time()
    result = run_flywheel(context, client, args.model, max_attempts=args.max_attempts)
    print("\n" + "=" * 66)
    if result["accepted"]:
        print(f"ACCEPTED ✅  new verified eval case: {result['case_id']}")
        print(f"  repro    -> {result['repro_path']}  (RED-ON-BASE confirmed by verify.py)")
        print(f"  manifest -> {Path(result['manifest']).relative_to(ROOT)}")
        print(f"  attempts: {[a['verdict'] for a in result['attempts']]}  ({round(time.time()-t0,1)}s)")
        print("  → the benchmark just grew by one case, learned from a failure.")
    else:
        print(f"REJECTED ❌  {result['reason']}")
        print(f"  attempts: {[a['verdict'] for a in result['attempts']]}")
    print("=" * 66)
    sys.exit(0 if result["accepted"] else 1)


if __name__ == "__main__":
    main()
