#!/usr/bin/env python3
"""
TODO #4 — Human-in-the-loop (HITL) approval gate, via LangGraph interrupt/checkpoint.

The same single-agent repair loop as graph_solve.py, but BEFORE a proposed fix is accepted,
the graph PAUSES and asks a human to approve it. This is the production "agent proposes, human
approves, then it proceeds" pattern — the guardrail you want when an autonomous agent edits real
code. Built on LangGraph's dynamic `interrupt()` + a checkpointer (so the paused run is durable
and resumable with `Command(resume=...)`), which is the idiomatic way to do HITL in LangGraph.

Flow (adds a `human_review` node + checkpointer to the graph_solve graph):

    START → agent ─(tool calls?)─→ tools ─(submit proposed?)─→ human_review ──approve──→ END → grade
                 └─(no calls)──→ END        └─(other tools)──→ agent         └──reject(feedback)──→ agent

Key mechanics:
  * When the agent calls `submit`, tools_node does NOT finalize — it stashes the summary as
    `pending_summary` and routes to human_review (instead of ending like graph_solve does).
  * `human_review` captures the proposed SOURCE diff (`git diff`) and calls `interrupt(payload)`,
    which pauses the graph and hands the payload (summary + diff) back to the caller. The caller
    (a human CLI, or a scripted approver) inspects it and resumes with `Command(resume=decision)`.
  * APPROVE → the patch is accepted, graph ends, driver grades it (red→green). REJECT → the human's
    feedback is injected as a user message and the agent keeps editing, then re-proposes (re-review).
  * `interrupt()` requires a checkpointer, so we compile with MemorySaver and run under a thread_id.
    NOTE: on resume, the interrupted node RE-RUNS from its top (LangGraph semantics), so everything
    before `interrupt()` must be idempotent — capturing a `git diff` is a pure read, so it is.

Everything that isn't control flow (tools, Executor, grader, prompt) is imported verbatim from
solve.py, exactly like graph_solve.py — so the agent behaves identically; only the gate is new.

Usage:
    .venv/bin/python hitl_solve.py --issue furl-163 --verbose            # interactive: prompts you y/n
    .venv/bin/python hitl_solve.py --issue furl-163 --auto-approve       # scripted: auto-approve (demo)
    .venv/bin/python hitl_solve.py --issue furl-163 --reject-first       # demo reject→feedback→re-review→approve
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Callable, Optional, TypedDict

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from solve import (  # noqa: E402  — reuse everything that is NOT control flow (identical behavior)
    RUNS_DIR, MAX_STEPS, DEFAULT_MODEL,
    LocalExecutor, DockerExecutor, make_client,
    dispatch, SYSTEM_PROMPT, issue_brief,
    prepare_repo, drop_repro, grade, get_issue, load_env, _chat_with_backoff,
)
from langgraph.graph import StateGraph, START, END  # noqa: E402
from langgraph.checkpoint.memory import MemorySaver  # noqa: E402
from langgraph.types import interrupt, Command  # noqa: E402


class HitlState(TypedDict):
    messages: list
    steps: int
    max_steps: int
    submitted: Optional[str]      # set ONLY after human approval
    stop_reason: str              # approved | model_stopped | max_steps | api_error
    tool_trace: list
    pending_summary: Optional[str]  # a submit awaiting review (set by tools, consumed by human_review)
    proposed_patch: Optional[str]   # the git diff shown to the reviewer (kept for the run record)
    approved: bool
    review_log: list                # [{step, approved, feedback}] — the audit trail of decisions


def build_hitl_graph(ex, client, model: str, verbose: bool):
    def agent_node(state: HitlState) -> dict:
        steps = state["steps"] + 1
        msg = _chat_with_backoff(client, model, state["messages"], verbose)
        if msg is None:
            return {"steps": steps, "stop_reason": "api_error"}
        a = {"role": "assistant", "content": msg.content or ""}
        if msg.tool_calls:
            a["tool_calls"] = [{"id": tc.id, "type": "function",
                                "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                               for tc in msg.tool_calls]
        out = {"messages": state["messages"] + [a], "steps": steps}
        if not msg.tool_calls:
            out["stop_reason"] = "model_stopped"
        return out

    def tools_node(state: HitlState) -> dict:
        """Run each tool call. A `submit` is NOT final here — it's stashed as pending_summary and
        the graph routes to human_review; every other tool behaves exactly as in graph_solve."""
        last = state["messages"][-1]
        new_msgs, trace = [], list(state["tool_trace"])
        pending = state.get("pending_summary")
        for tc in last.get("tool_calls", []):
            name = tc["function"]["name"]
            try:
                args = json.loads(tc["function"]["arguments"] or "{}")
            except json.JSONDecodeError:
                args = {}
            result = dispatch(ex, name, args)
            if name == "submit":
                pending = args.get("summary", "")
                result = "PROPOSED — sent to a human reviewer for approval before it is accepted."
            if verbose:
                preview = (args.get("cmd") or args.get("path") or args.get("summary") or "")
                head = result.splitlines()[0][:80] if result else ""
                print(f"[{state['steps']}] {name}({str(preview)[:80]}) -> {head}")
            trace.append({"step": state["steps"], "tool": name, "args": args, "result": result})
            new_msgs.append({"role": "tool", "tool_call_id": tc["id"], "content": result})
        return {"messages": state["messages"] + new_msgs, "tool_trace": trace, "pending_summary": pending}

    def human_review_node(state: HitlState) -> dict:
        """Pause for human approval of the proposed patch. `interrupt()` suspends the graph and
        returns the payload to the caller; resuming with Command(resume={approved, feedback})
        re-enters this node and `interrupt()` returns that decision."""
        code, diff, _ = ex.exec_raw("git diff")   # pure read → safe to re-run on resume
        diff = diff or ""
        files = [ln.split(" b/")[-1] for ln in diff.splitlines() if ln.startswith("diff --git")]
        decision = interrupt({
            "action": "approve_patch",
            "issue": state.get("stop_reason"),  # placeholder-safe; real id is in the run record
            "proposed_summary": state.get("pending_summary"),
            "steps": state["steps"],
            "files_changed": files,
            "diff": diff[:4000],
            "diff_truncated": len(diff) > 4000,
        })
        decision = decision or {}
        approved = bool(decision.get("approved"))
        log = list(state["review_log"]) + [{"step": state["steps"], "approved": approved,
                                             "feedback": decision.get("feedback")}]
        if approved:
            return {"approved": True, "submitted": state.get("pending_summary"),
                    "stop_reason": "approved", "pending_summary": None,
                    "proposed_patch": diff, "review_log": log}
        # rejected → feed the reviewer's redirect back to the agent and keep working
        fb = decision.get("feedback") or "Not approved. Reconsider the fix and revise before resubmitting."
        return {"approved": False, "pending_summary": None, "review_log": log,
                "messages": state["messages"] + [{"role": "user", "content":
                    f"HUMAN REVIEW — CHANGES REQUESTED (patch NOT approved): {fb}\n"
                    "Make further source edits to address this, then call submit again."}]}

    def route_after_agent(state: HitlState) -> str:
        last = state["messages"][-1]
        return "tools" if (last.get("role") == "assistant" and last.get("tool_calls")) else "end"

    def route_after_tools(state: HitlState) -> str:
        if state.get("pending_summary"):
            return "review"                                   # a submit is awaiting human approval
        return "end" if state["steps"] >= state["max_steps"] else "agent"

    def route_after_review(state: HitlState) -> str:
        return "end" if state.get("approved") else "agent"    # approved → finish; rejected → keep editing

    g = StateGraph(HitlState)
    g.add_node("agent", agent_node)
    g.add_node("tools", tools_node)
    g.add_node("human_review", human_review_node)
    g.add_edge(START, "agent")
    g.add_conditional_edges("agent", route_after_agent, {"tools": "tools", "end": END})
    g.add_conditional_edges("tools", route_after_tools,
                            {"review": "human_review", "agent": "agent", "end": END})
    g.add_conditional_edges("human_review", route_after_review, {"agent": "agent", "end": END})
    return g.compile(checkpointer=MemorySaver())


# --------------------------------------------------------------------------- #
# Approvers — the human side of the gate. A driver drains interrupts by calling one
# of these and resuming. (The unit tests inject their own scripted approver.)
# --------------------------------------------------------------------------- #
def cli_approver(payload: dict) -> dict:
    """Interactive: show the proposed patch, ask the human to approve or request changes."""
    print("\n" + "=" * 70)
    print("HUMAN REVIEW REQUESTED — the agent proposes this fix:")
    print(f"  summary: {payload.get('proposed_summary')}")
    print(f"  files:   {payload.get('files_changed')}  (after {payload.get('steps')} steps)")
    print("-" * 70)
    print(payload.get("diff") or "(no diff)")
    if payload.get("diff_truncated"):
        print("...[diff truncated]...")
    print("=" * 70)
    ans = input("Approve this patch? [y = approve / anything else = request changes]: ").strip().lower()
    if ans in ("y", "yes"):
        return {"approved": True}
    return {"approved": False, "feedback": input("Feedback for the agent: ").strip()}


def auto_approver(payload: dict) -> dict:
    print(f"  [auto-approve] proposed: {payload.get('proposed_summary')} "
          f"({payload.get('files_changed')})")
    return {"approved": True}


def make_reject_then_approve(feedback: str = "Please double-check the edge case before resubmitting."):
    """Demo approver: reject the FIRST proposal (with feedback), approve the next — exercises the
    full reject→feedback→re-review→approve loop end-to-end."""
    state = {"rejected": False}

    def approver(payload: dict) -> dict:
        if not state["rejected"]:
            state["rejected"] = True
            print(f"  [demo] rejecting first proposal with feedback: {feedback!r}")
            return {"approved": False, "feedback": feedback}
        print("  [demo] approving second proposal")
        return {"approved": True}

    return approver


# --------------------------------------------------------------------------- #
# Driver — run the graph, DRAIN interrupts by calling the approver + resuming, then grade.
# --------------------------------------------------------------------------- #
def run_hitl(issue: dict, model: str, max_steps: int, verbose: bool,
             approver: Callable[[dict], dict], sandbox: str = "local",
             thread_id: str = "hitl-run") -> dict:
    import os
    os.environ["_MECHANIC_MODEL"] = model

    iid = issue["id"]
    repo_dir = prepare_repo(issue, reset=True)
    test_name = drop_repro(issue, repo_dir)
    ex = DockerExecutor(repo_dir) if sandbox == "docker" else LocalExecutor(repo_dir)
    client = make_client()
    graph = build_hitl_graph(ex, client, model, verbose)

    init: HitlState = {
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT.format(test_name=test_name)},
            {"role": "user", "content":
                f"Fix this bug:\n\n{issue_brief(issue)}\n\n"
                f"fix_hint (from the issue triage, may be imperfect): {issue.get('fix_hint', '(none)')}\n\n"
                f"The failing test is {test_name}. Start by running it. When you believe the fix is "
                f"complete, call submit — a human will review your patch before it is accepted."},
        ],
        "steps": 0, "max_steps": max_steps, "submitted": None, "stop_reason": "max_steps",
        "tool_trace": [], "pending_summary": None, "proposed_patch": None,
        "approved": False, "review_log": [],
    }
    config = {"configurable": {"thread_id": thread_id}, "recursion_limit": max_steps * 3 + 10}
    n_reviews = 0
    try:
        result = graph.invoke(init, config)
        # DRAIN: each interrupt is a review request — approve/reject and resume until the graph ends.
        while result.get("__interrupt__"):
            payload = result["__interrupt__"][0].value
            n_reviews += 1
            decision = approver(payload)
            result = graph.invoke(Command(resume=decision), config)
        verdict = grade(issue, repo_dir, ex)
    finally:
        ex.close()

    return {
        "issue": iid, "model": model, "engine": "hitl", "sandbox": sandbox,
        "steps": result.get("steps"), "max_steps": max_steps,
        "stop_reason": result.get("stop_reason"), "submitted_summary": result.get("submitted"),
        "approved": result.get("approved"), "n_reviews": n_reviews,
        "review_log": result.get("review_log"), "proposed_patch": result.get("proposed_patch"),
        "verdict": verdict, "tool_trace": result.get("tool_trace"), "messages": result.get("messages"),
    }


def main():
    ap = argparse.ArgumentParser(description="TODO #4: fix one eval issue with a HITL approval gate.")
    ap.add_argument("--issue", required=True)
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--max-steps", type=int, default=MAX_STEPS)
    ap.add_argument("--sandbox", choices=["local", "docker"], default="local")
    ap.add_argument("--auto-approve", action="store_true", help="scripted: approve every proposal (demo)")
    ap.add_argument("--reject-first", action="store_true",
                    help="scripted: reject the first proposal w/ feedback, approve the next (demo the loop)")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    load_env()
    issue = get_issue(args.issue)
    RUNS_DIR.mkdir(exist_ok=True)
    approver = (make_reject_then_approve() if args.reject_first
                else auto_approver if args.auto_approve else cli_approver)
    print(f"[hitl] {issue['id']} | {args.model} | [{args.sandbox}] | <= {args.max_steps} steps | "
          f"approver={approver.__name__ if hasattr(approver, '__name__') else 'reject_then_approve'}\n")

    rec = run_hitl(issue, args.model, args.max_steps, args.verbose, approver, args.sandbox)

    out = RUNS_DIR / f"{issue['id']}_hitl_{int(time.time())}.json"
    out.write_text(json.dumps(rec, indent=2, default=str))
    v = rec["verdict"]
    status = "RESOLVED ✅" if v["resolved"] else "NOT_RESOLVED ❌"
    print("\n" + "=" * 60)
    print(f"{issue['id']} [hitl]: {status}  ({rec['steps']} steps, {rec['n_reviews']} review(s), "
          f"stop={rec['stop_reason']}, approved={rec['approved']})")
    if rec["submitted_summary"]:
        print(f"  approved fix: {rec['submitted_summary']}")
    print(f"  review log: {rec['review_log']}")
    print(f"  repro: {v['summary']}")
    print(f"  trace: {out.relative_to(ROOT)}")
    print("=" * 60)
    sys.exit(0 if v["resolved"] else 2)


if __name__ == "__main__":
    main()
