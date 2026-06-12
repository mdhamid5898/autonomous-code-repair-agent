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
  - Coder     : short bash + str_replace loop → localizes (from the Planner's candidate
                files) AND applies the fix, returns a summary.
  - Tester    : DETERMINISTIC (no LLM) — runs the pristine repro via solve.grade().
  - Reviewer  : 1 LLM call on failure → decide revise (with feedback) or escalate.
                (accept is automatic when the Tester is green.)

NOTE: a separate Localizer node was REMOVED (folded into the Coder). The v2 head-to-head
showed multi-agent REGRESSED vs single-agent (47% vs 87%), and across every Phase-4 trace
the Localizer burned its full read-only budget without ever calling report_localization —
the Coder re-localized anyway. Localization was never the bottleneck (committing an edit
was). It returns as a real, retrieval-backed node in Phase 5.

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

MAX_INNER_STEPS = 8       # per-specialist tool-loop cap (the Coder)
MAX_ITERATIONS = 4        # Coder↔Tester↔Reviewer re-plan rounds
CODER_ACT_BY_STEP = MAX_INNER_STEPS - 3   # no edit by here → nudge the Coder to commit a fix now

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
    "description": "Call once the fix has been applied via str_replace.",
    "parameters": {"type": "object", "properties": {
        "summary": {"type": "string", "description": "1-2 sentences: root cause + the change you made"}},
        "required": ["summary"]}}}

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


def _run_specialist(client, model, system, user, tools, finish_name, ex, verbose, label, transcript,
                    act_tool=None, act_by_step=None):
    """A bounded bash/str_replace tool-loop for a specialist; returns the finish tool's
    args (or None if it never finished). Appends tool calls to `transcript`.

    act-by-step-N nudge (optional): if `act_tool` (e.g. "str_replace") still hasn't been
    called by step `act_by_step`, an escalating user message is injected each remaining
    step pushing the model to COMMIT its best action now. Counters the analysis-paralysis
    the v2 sweep exposed — every multi-agent failure was the Coder exploring its whole
    budget and never editing (resolved ⟺ ≥1 edit)."""
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    result = None
    acted = False
    for step in range(1, MAX_INNER_STEPS + 1):
        if act_tool and act_by_step and step > act_by_step and not acted:
            left = MAX_INNER_STEPS - step + 1
            messages.append({"role": "user", "content":
                f"You have {left} step(s) left and have NOT applied a `{act_tool}` fix yet. "
                f"STOP exploring. Apply your single best `{act_tool}` edit to the SOURCE now "
                f"(cat the file first only if you still need exact strings), then call "
                f"{finish_name}. A best-guess fix the Tester can check beats no fix at all."})
        msg = _chat(client, model, messages, tools, verbose)
        if msg is None:
            return {"_error": "api_error"}
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
            else:
                out = dispatch(ex, name, args)
            if name == act_tool:
                acted = True
            if verbose:
                prev = args.get("cmd") or args.get("path") or args.get("summary") or args.get("target_file") or ""
                print(f"  [{label} {step}] {name}({str(prev)[:60]})")
            transcript.append({"agent": label, "step": step, "tool": name, "args": args,
                               "result": out if isinstance(out, str) else "ok"})
            messages.append({"role": "tool", "tool_call_id": tc.id,
                             "content": out if isinstance(out, str) else "ok"})
        if result is not None:
            break
    return result


# --------------------------------------------------------------------------- #
# the graph
# --------------------------------------------------------------------------- #
def build_graph(ex, client, model: str, verbose: bool):

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
        feedback = ""
        if state["review"] and state["review"].get("decision") == "revise":
            tr = state["test_result"] or {}
            feedback = (f"\n\nThis is retry #{state['iteration']}. Your previous fix FAILED the test:\n"
                        f"{tr.get('summary')}\nReviewer feedback: {state['review'].get('feedback')}\n"
                        "Try a different fix.")
        user = (f"{issue_brief(state['issue'])}\n\nHypothesis: {plan.get('hypothesis')}\n"
                f"Strategy: {plan.get('strategy')}\n"
                f"Candidate files (from the Planner — confirm with bash before editing):\n"
                f"{plan.get('candidate_files')}{feedback}\n\n"
                "Work fast: spend only a few steps localizing (grep/cat from the candidate files), then "
                "you MUST apply a fix. Make the smallest correct edit to the SOURCE with str_replace (cat "
                "the file first for exact strings) — don't over-explore; a best-guess fix the Tester can "
                "check beats endless reading. NEVER edit the test. Do NOT run the test. Finish with finish_edit.")
        res = _run_specialist(client, model,
                              "You are the Coder. Localize quickly with read-only bash from the Planner's "
                              "candidate files, then COMMIT a minimal source fix via str_replace — bias "
                              "toward editing early over exploring exhaustively. Commands run from the repo "
                              "root (repo-relative paths, never cd). Never touch the test file; don't run "
                              "tests (the Tester does). Finish with finish_edit.",
                              user, [_BASH, _STR_REPLACE, EDIT_DONE_TOOL], "finish_edit", ex, verbose,
                              "Coder", state["transcript"],
                              act_tool="str_replace", act_by_step=CODER_ACT_BY_STEP) or {}
        summary = res.get("summary", "(no edit reported)")
        if verbose:
            print(f"[Coder] {summary[:90]}")
        return {"last_edit": summary, "status": "testing",
                "decision_log": log(state, "coder", summary)}

    def tester_node(state: MultiAgentState) -> dict:
        # deterministic: re-drop the pristine repro and run it (anti-tamper grade)
        v = grade(state["issue"], ex.repo_dir, ex)
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
                    sandbox: str = "local") -> dict:
    os.environ["_MECHANIC_MODEL"] = model
    iid = issue["id"]
    repo_dir = prepare_repo(issue, reset=True)
    test_name = drop_repro(issue, repo_dir)
    ex = DockerExecutor(repo_dir) if sandbox == "docker" else LocalExecutor(repo_dir)
    client = make_client()
    graph = build_graph(ex, client, model, verbose)

    init: MultiAgentState = {
        "issue": issue, "test_name": test_name, "plan": None,
        "last_edit": None, "test_result": None, "review": None, "iteration": 1,
        "max_iterations": max_iterations, "status": "planning", "decision_log": [], "transcript": [],
    }
    try:
        final = graph.invoke(init, {"recursion_limit": max_iterations * 4 + 20})
    finally:
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
