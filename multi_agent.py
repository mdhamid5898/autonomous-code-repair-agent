#!/usr/bin/env python3
"""
Phase 4 — multi-agent split (Planner / Coder / Tester / Reviewer).

The single generalist agent from solve.py/graph_solve.py is decomposed into
specialists coordinated by a LangGraph graph, with a bounded re-plan loop:

    START → Planner → Coder → Tester → Reviewer
            (hypothesis  (localize  (run repro   accept → END (resolved)
             + files +    + fix)     — no LLM)    revise → Coder (with feedback)
             strategy)                            escalate → END (failed)
              ▲
              └──────────── bounded by --max-iterations ────┘  (loop is Coder↔Tester↔Reviewer)

Roles:
  - Planner   : 1 LLM call. issue + test + file list → {hypothesis, candidate_files, strategy}.
  - Coder     : STATEFUL bash + str_replace + run_test loop. Localizes, edits, and runs the repro
                ITSELF (edit→run_test→edit); its message history PERSISTS across re-plan rounds, so
                it never loses context. This mirrors the single agent's tight, full-memory loop.
  - Tester    : DETERMINISTIC (no LLM) — final anti-tamper gate; runs the pristine repro via solve.grade().
  - Reviewer  : 1 LLM call on failure → decide revise (with feedback) or escalate.
                (accept is automatic when the Tester is green.)

NOTE 1 (Phase 4): a separate Localizer node was REMOVED (folded into the Coder) — across every
trace it burned its read-only budget without ever calling report_localization; the Coder
re-localized anyway. Localization was never the bottleneck. It returns retrieval-backed in Phase 5.

NOTE 2 (the rebuild): the first 5-/4-role versions REGRESSED vs single-agent (47% vs 87%) because
the Coder was lobotomized — an 8-step cap, a FRESH context every re-plan round (amnesia), and
forbidden to run the test (edited blind). The single agent, by contrast, ran pytest mid-edit and
kept one continuous memory. Fix: the Coder is now stateful (history persists across rounds) and
runs the test itself via run_test — restoring the edit→test→edit feedback loop. grade() is still
the final authority, so self-testing can't game the verdict.

Reuses solve.py verbatim for the Executor, tools, dispatch, and grader — only the
orchestration is new. Measure against the SINGLE-agent baseline on the SAME model.

Usage:
  .venv/bin/python multi_agent.py --issue furl-163 --verbose [--max-iterations 4] [--sandbox docker]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Optional, TypedDict

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from solve import (  # noqa: E402
    RUNS_DIR, MAX_STEPS, DEFAULT_MODEL,
    LocalExecutor, DockerExecutor, make_client, dispatch, TOOLS,
    issue_brief, prepare_repo, drop_repro, grade, get_issue, load_env,
    _RateLimitError, _APIError,
)
from langgraph.graph import StateGraph, START, END  # noqa: E402

MAX_ITERATIONS = 4        # Coder↔Tester↔Reviewer re-plan rounds
CODER_STEPS = 20          # the Coder's tool budget per round. It now PERSISTS its message history
                          # across rounds (stateful resume) and runs the test itself, so this is a
                          # generous CONTINUOUS budget mirroring the single agent's loop — not the
                          # fragmented per-attempt cap (the old 8-step cap is what starved it).
CODER_ACT_BY_STEP = CODER_STEPS - 8   # backstop: if still no edit by here, nudge it to stop exploring

# reuse solve.py's bash + str_replace tool schemas; specialists add a "finish" tool
_BASH = next(t for t in TOOLS if t["function"]["name"] == "bash")
_STR_REPLACE = next(t for t in TOOLS if t["function"]["name"] == "str_replace")

PLAN_TOOL = {"type": "function", "function": {
    "name": "submit_plan",
    "description": "Record the bug-fix plan.",
    "parameters": {"type": "object", "properties": {
        "hypothesis": {"type": "string", "description": "root-cause hypothesis"},
        "candidate_files": {"type": "array", "items": {"type": "string"},
                            "description": "most likely files to fix (repo-relative)"},
        "strategy": {"type": "string", "description": "how to fix it, briefly"}},
        "required": ["hypothesis", "candidate_files", "strategy"]}}}

EDIT_DONE_TOOL = {"type": "function", "function": {
    "name": "finish_edit",
    "description": "Call once your fix makes run_test PASS (or you've truly exhausted ideas).",
    "parameters": {"type": "object", "properties": {
        "summary": {"type": "string", "description": "1-2 sentences: root cause + the change you made"}},
        "required": ["summary"]}}}

RUN_TEST_TOOL = {"type": "function", "function": {
    "name": "run_test",
    "description": "Run the failing repro and see its output. CHECK your fix with this: "
                   "edit → run_test → read the failure → edit again, until it passes.",
    "parameters": {"type": "object", "properties": {}}}}

REVIEW_TOOL = {"type": "function", "function": {
    "name": "submit_review",
    "description": "Decide what to do after a FAILED test.",
    "parameters": {"type": "object", "properties": {
        "decision": {"type": "string", "enum": ["revise", "escalate"],
                     "description": "revise = try another fix; escalate = give up"},
        "feedback": {"type": "string", "description": "specific guidance for the next Coder attempt"}},
        "required": ["decision", "feedback"]}}}


# --------------------------------------------------------------------------- #
# shared state
# --------------------------------------------------------------------------- #
class MultiAgentState(TypedDict):
    issue: dict
    test_name: str
    plan: Optional[dict]
    coder_messages: list   # the Coder's PERSISTENT history — carried across re-plan rounds (no amnesia)
    last_edit: Optional[str]
    test_result: Optional[dict]
    review: Optional[dict]
    iteration: int
    max_iterations: int
    status: str
    decision_log: list   # [{node, detail}]
    transcript: list      # tool-level trace across all specialists


# --------------------------------------------------------------------------- #
# model-call helpers (own copies because solve._chat_with_backoff hardcodes TOOLS)
# --------------------------------------------------------------------------- #
def _chat(client, model, messages, tools, verbose, tool_choice="auto", attempts=6):
    for i in range(attempts):
        try:
            resp = client.chat.completions.create(
                model=model, messages=messages, tools=tools,
                tool_choice=tool_choice, temperature=0,
            )
            return resp.choices[0].message
        except (_RateLimitError, _APIError) as e:
            wait = min(2 ** i, 30)
            if verbose:
                print(f"  [{type(e).__name__}; sleeping {wait}s ({i + 1}/{attempts})]")
            time.sleep(wait)
    return None


def _single_call(client, model, system, user, tool, verbose):
    """One forced structured call → the tool's args dict (or None on failure)."""
    msg = _chat(client, model,
                [{"role": "system", "content": system}, {"role": "user", "content": user}],
                [tool], verbose, tool_choice="required")
    if msg is None or not msg.tool_calls:
        return None
    try:
        return json.loads(msg.tool_calls[0].function.arguments or "{}")
    except json.JSONDecodeError:
        return {}


def _run_test_str(issue: dict, ex, grade_fn=None) -> str:
    """Run the pristine repro (re-dropped each call = anti-tamper) and return a compact
    PASS/FAIL + output string into the Coder's context — the test-feedback loop the single
    agent gets from running pytest itself, which the old blind Coder structurally lacked.
    grade_fn(issue, ex)->verdict lets the SWE-bench adapter inject its in-container grade
    (default = the local pristine-repro grade)."""
    gf = grade_fn or (lambda i, e: grade(i, e.repo_dir, e))
    v = gf(issue, ex)
    if v["resolved"]:
        return f"PASS ✅ — the repro now passes ({v['summary']}). Call finish_edit."
    return f"FAIL — still red ({v['summary']}).\n{v['log']}"


def _run_specialist(client, model, messages, tools, finish_name, ex, verbose, label, transcript,
                    max_steps, act_tool=None, act_by_step=None, extra=None):
    """A bounded tool-loop over a CALLER-OWNED `messages` list (so the Coder can be resumed
    across re-plan rounds with full memory). Returns (finish_args_or_None, messages).
    `extra` maps tool-name → handler(args)->str for tools beyond bash/str_replace (e.g. run_test).

    act-by-step-N nudge (optional): if `act_tool` still hasn't been called by `act_by_step`,
    an escalating message is injected each remaining step — a backstop against the
    explore-without-editing paralysis the v2 sweep exposed (resolved ⟺ ≥1 edit)."""
    extra = extra or {}
    result = None
    acted = False
    for step in range(1, max_steps + 1):
        if act_tool and act_by_step and step > act_by_step and not acted:
            messages.append({"role": "user", "content":
                f"You have NOT applied a `{act_tool}` fix yet and you're low on steps. STOP exploring. "
                f"Apply your single best `{act_tool}` edit to the SOURCE now, run_test to check it, "
                f"then call {finish_name}."})
        msg = _chat(client, model, messages, tools, verbose)
        if msg is None:
            return {"_error": "api_error"}, messages
        a = {"role": "assistant", "content": msg.content or ""}
        if msg.tool_calls:
            a["tool_calls"] = [
                {"id": tc.id, "type": "function",
                 "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                for tc in msg.tool_calls]
        messages.append(a)
        if not msg.tool_calls:
            break
        for tc in msg.tool_calls:
            name = tc.function.name
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            if name == finish_name:
                result, out = args, "ok"
            elif name in extra:
                out = extra[name](args)
            else:
                out = dispatch(ex, name, args)
            if name == act_tool:
                acted = True
            if verbose:
                prev = args.get("cmd") or args.get("path") or args.get("summary") or ""
                print(f"  [{label} {step}] {name}({str(prev)[:60]})")
            transcript.append({"agent": label, "step": step, "tool": name, "args": args,
                               "result": out if isinstance(out, str) else "ok"})
            messages.append({"role": "tool", "tool_call_id": tc.id,
                             "content": out if isinstance(out, str) else "ok"})
        if result is not None:
            break
    return result, messages


# --------------------------------------------------------------------------- #
# the graph
# --------------------------------------------------------------------------- #
def build_graph(ex, client, model: str, verbose: bool, grade_fn=None):
    # grade_fn(issue, ex)->verdict — default is the local pristine-repro grade; the SWE-bench
    # adapter injects an in-container FAIL_TO_PASS grade. The Tester stays deterministic either way.
    grade_fn = grade_fn or (lambda issue, e: grade(issue, e.repo_dir, e))

    def log(state, node, detail):
        return state["decision_log"] + [{"node": node, "detail": detail}]

    def planner_node(state: MultiAgentState) -> dict:
        _, files, _ = ex.exec_raw("git ls-files | head -200")
        user = (f"{issue_brief(state['issue'])}\n\n"
                f"fix_hint (may be imperfect): {state['issue'].get('fix_hint', '(none)')}\n\n"
                f"Failing test: {state['test_name']}\n\nRepo files:\n{files}\n\n"
                "Produce the plan. Do not write code.")
        plan = _single_call(client, model,
                            "You are the Planner in a bug-fix pipeline. Given a bug report, a "
                            "failing test, and the repo's files, produce a concise plan: a "
                            "root-cause hypothesis, the most likely file(s) to fix, and a "
                            "strategy. Call submit_plan.", user, PLAN_TOOL, verbose) or {}
        if verbose:
            print(f"[Planner] files={plan.get('candidate_files')} :: {str(plan.get('hypothesis'))[:80]}")
        return {"plan": plan, "status": "coding",
                "decision_log": log(state, "planner", plan.get("hypothesis", ""))}

    def coder_node(state: MultiAgentState) -> dict:
        plan = state["plan"] or {}
        messages = state.get("coder_messages") or []
        if not messages:
            # first round: build the Coder's standing context
            system = ("You are the Coder — you fix the bug end-to-end in this one session. Localize with "
                      "bash from the Planner's candidate files, apply a minimal source fix with str_replace, "
                      "then call run_test to check yourself. Iterate edit → run_test → edit until run_test "
                      "PASSES, then call finish_edit. Commands run from the repo root (repo-relative paths, "
                      "never cd). NEVER edit the test file — make the SOURCE pass it.")
            user = (f"{issue_brief(state['issue'])}\n\nHypothesis: {plan.get('hypothesis')}\n"
                    f"Strategy: {plan.get('strategy')}\n"
                    f"Candidate files (start here, confirm with bash):\n{plan.get('candidate_files')}\n\n"
                    "Localize, fix the SOURCE with str_replace, and use run_test to verify — keep iterating "
                    "until run_test passes, then finish_edit. Don't just explore: edit and test.")
            messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        else:
            # resume with FULL memory of prior rounds; add the grader verdict + Reviewer guidance
            tr = state["test_result"] or {}
            review = state["review"] or {}
            messages.append({"role": "user", "content":
                f"The independent grader still reports FAILURE: {tr.get('summary')}\n{tr.get('log')}\n"
                f"Reviewer guidance: {review.get('feedback')}\n\n"
                "You still have full context above. Adjust your fix, run_test to check, finish_edit when it passes."})
        res, messages = _run_specialist(
            client, model, messages, [_BASH, _STR_REPLACE, RUN_TEST_TOOL, EDIT_DONE_TOOL],
            "finish_edit", ex, verbose, "Coder", state["transcript"], CODER_STEPS,
            act_tool="str_replace", act_by_step=CODER_ACT_BY_STEP,
            extra={"run_test": lambda args: _run_test_str(state["issue"], ex, grade_fn)})
        summary = (res or {}).get("summary", "(no finish_edit)")
        if verbose:
            print(f"[Coder] {summary[:90]}")
        return {"coder_messages": messages, "last_edit": summary, "status": "testing",
                "decision_log": log(state, "coder", summary)}

    def tester_node(state: MultiAgentState) -> dict:
        # deterministic: re-drop the pristine repro and run it (anti-tamper grade)
        v = grade_fn(state["issue"], ex)
        if verbose:
            print(f"[Tester] {'PASS ✅' if v['resolved'] else 'FAIL'} — {v['summary']}")
        return {"test_result": v, "status": "reviewing",
                "decision_log": log(state, "tester", v["summary"])}

    def reviewer_node(state: MultiAgentState) -> dict:
        tr = state["test_result"] or {}
        if tr.get("resolved"):
            return {"review": {"decision": "accept", "feedback": "tests pass"}, "status": "resolved",
                    "decision_log": log(state, "reviewer", "accept")}
        if state["iteration"] >= state["max_iterations"]:
            return {"review": {"decision": "escalate", "feedback": "out of iterations"}, "status": "failed",
                    "decision_log": log(state, "reviewer", "escalate (budget)")}
        user = (f"{issue_brief(state['issue'])}\n\nPlan: {(state['plan'] or {}).get('strategy')}\n"
                f"Coder's edit: {state['last_edit']}\nTest result: {tr.get('summary')}\n"
                f"Test output:\n{tr.get('log')}\n\nThe test still FAILS. Decide revise or escalate.")
        review = _single_call(client, model,
                              "You are the Reviewer. The fix failed the test. Decide 'revise' (with "
                              "concrete feedback for the next attempt) or 'escalate' (give up). "
                              "Call submit_review.", user, REVIEW_TOOL, verbose) or \
            {"decision": "revise", "feedback": tr.get("summary", "test failed")}
        if verbose:
            print(f"[Reviewer] {review['decision']}: {review['feedback'][:80]}")
        upd = {"review": review, "decision_log": log(state, "reviewer", review["decision"])}
        if review["decision"] == "revise":
            upd["iteration"] = state["iteration"] + 1
            upd["status"] = "coding"
        else:
            upd["status"] = "failed"
        return upd

    def route_after_reviewer(state: MultiAgentState) -> str:
        return {"accept": "end", "escalate": "end"}.get(state["review"]["decision"], "coder")

    g = StateGraph(MultiAgentState)
    for name, fn in [("planner", planner_node), ("coder", coder_node),
                     ("tester", tester_node), ("reviewer", reviewer_node)]:
        g.add_node(name, fn)
    g.add_edge(START, "planner")
    g.add_edge("planner", "coder")
    g.add_edge("coder", "tester")
    g.add_edge("tester", "reviewer")
    g.add_conditional_edges("reviewer", route_after_reviewer, {"coder": "coder", "end": END})
    return g.compile()


# --------------------------------------------------------------------------- #
# driver
# --------------------------------------------------------------------------- #
def run_multi_agent(issue: dict, model: str, max_iterations: int, verbose: bool,
                    sandbox: str = "local", ex=None, grade_fn=None, test_name=None) -> dict:
    """Local path (ex is None): prepare_repo + drop_repro + Local/DockerExecutor, as before.
    Injected path (ex given — e.g. the SWE-bench adapter): use the caller's executor, grade_fn,
    and test_name; the CALLER owns the executor's lifecycle (we don't close it)."""
    os.environ["_MECHANIC_MODEL"] = model
    iid = issue.get("id") or issue.get("instance_id")
    own_ex = ex is None
    if own_ex:
        repo_dir = prepare_repo(issue, reset=True)
        test_name = drop_repro(issue, repo_dir)
        ex = DockerExecutor(repo_dir) if sandbox == "docker" else LocalExecutor(repo_dir)
    client = make_client()
    graph = build_graph(ex, client, model, verbose, grade_fn=grade_fn)

    init: MultiAgentState = {
        "issue": issue, "test_name": test_name or "(the failing test)", "plan": None, "coder_messages": [],
        "last_edit": None, "test_result": None, "review": None, "iteration": 1,
        "max_iterations": max_iterations, "status": "planning", "decision_log": [], "transcript": [],
    }
    try:
        final = graph.invoke(init, {"recursion_limit": max_iterations * 4 + 20})
    finally:
        if own_ex:
            ex.close()

    v = final.get("test_result") or {"resolved": False, "exit": None, "summary": "no test run", "log": ""}
    return {
        "issue": iid, "model": model, "engine": "multi-agent", "sandbox": sandbox,
        "iterations": final["iteration"], "status": final["status"], "verdict": v,
        "plan": final["plan"],
        "decision_log": final["decision_log"], "tool_trace": final["transcript"],
    }


def main():
    ap = argparse.ArgumentParser(description="Phase 4: fix one eval issue with a multi-agent LangGraph pipeline.")
    ap.add_argument("--issue", required=True)
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--max-iterations", type=int, default=MAX_ITERATIONS)
    ap.add_argument("--sandbox", choices=["local", "docker"], default="local")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    load_env()
    issue = get_issue(args.issue)
    RUNS_DIR.mkdir(exist_ok=True)
    print(f"[multi-agent] Solving {issue['id']} with {args.model} in [{args.sandbox}] "
          f"(<= {args.max_iterations} revise rounds)...\n")
    rec = run_multi_agent(issue, args.model, args.max_iterations, args.verbose, args.sandbox)

    out = RUNS_DIR / f"{issue['id']}_multi_{int(time.time())}.json"
    out.write_text(json.dumps(rec, indent=2, default=str))
    v = rec["verdict"]
    status = "RESOLVED ✅" if v["resolved"] else "NOT_RESOLVED ❌"
    print("\n" + "=" * 64)
    print(f"{issue['id']} [multi-agent]: {status}   ({rec['iterations']} round(s), {rec['sandbox']}, "
          f"status={rec['status']}, exit {v['exit']})")
    print(f"  repro: {v['summary']}")
    print(f"  path : " + " → ".join(d["node"] for d in rec["decision_log"]))
    print(f"  trace: {out.relative_to(ROOT)}")
    print("=" * 64)
    sys.exit(0 if v["resolved"] else 2)


if __name__ == "__main__":
    main()
