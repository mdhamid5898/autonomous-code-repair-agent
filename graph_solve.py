#!/usr/bin/env python3
"""
Phase 3 — the SAME repair loop as solve.py, re-expressed as a LangGraph graph.

This is a single-agent, ZERO-behavior-change port. The point is to learn LangGraph
(typed state, nodes, conditional edges) on a loop we already understand — not to add
capability. Everything that isn't control flow (tools, Executor, grader, prompt) is
imported verbatim from solve.py, so the agent behaves identically; only the
hand-rolled `while` loop becomes a graph:

    START → agent ─(tool calls?)─→ tools ─(loop?)─→ agent ...   (bounded by max_steps)
                 └─(no calls / api error)─┐         └─(submit / max_steps)─┐
                                          └────────────→ grade → END ←─────┘

Mapping from solve.py's run_agent():
    loop-local variables   →  AgentState (a TypedDict, the graph's shared memory)
    "call the model" body  →  agent node
    "run each tool" body   →  tools node
    `if tool_calls:`       →  conditional edge after agent
    `if submitted / cap`   →  conditional edge after tools

Usage mirrors solve.py:
    .venv/bin/python graph_solve.py --issue furl-163 --verbose [--sandbox local|docker]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Optional, TypedDict

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

# Reuse everything that is NOT control flow from solve.py — identical behavior.
from solve import (  # noqa: E402
    RUNS_DIR, MAX_STEPS, DEFAULT_MODEL,
    LocalExecutor, DockerExecutor, make_client,
    TOOLS, dispatch, SYSTEM_PROMPT, issue_brief,
    prepare_repo, drop_repro, grade, get_issue, load_env, _chat_with_backoff,
)
from langgraph.graph import StateGraph, START, END  # noqa: E402


# --------------------------------------------------------------------------- #
# The shared state — what was loop-local in solve.py is now one typed object that
# every node reads and writes. (Each node returns a partial dict; LangGraph merges
# it in. We return the full `messages` list each time = replace semantics.)
# --------------------------------------------------------------------------- #
class AgentState(TypedDict):
    messages: list           # the running OpenAI chat transcript
    steps: int               # model calls so far
    max_steps: int           # bound
    submitted: Optional[str]  # the agent's final summary, once it calls submit
    stop_reason: str         # submitted | model_stopped | max_steps | api_error
    tool_trace: list          # [{step, tool, args, result}] for the run record


# --------------------------------------------------------------------------- #
# Nodes. ex/client/model/verbose are captured via closure so the node signature
# stays (state) -> partial-state, which is what LangGraph expects.
# --------------------------------------------------------------------------- #
def build_graph(ex, client, model: str, verbose: bool):

    def agent_node(state: AgentState) -> dict:
        """One model call. Mirrors the top of solve.py's while body."""
        steps = state["steps"] + 1
        msg = _chat_with_backoff(client, model, state["messages"], verbose)
        if msg is None:
            return {"steps": steps, "stop_reason": "api_error"}
        a = {"role": "assistant", "content": msg.content or ""}
        if msg.tool_calls:
            a["tool_calls"] = [
                {"id": tc.id, "type": "function",
                 "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                for tc in msg.tool_calls
            ]
        out = {"messages": state["messages"] + [a], "steps": steps}
        if not msg.tool_calls:
            out["stop_reason"] = "model_stopped"
        return out

    def tools_node(state: AgentState) -> dict:
        """Execute every tool call in the last assistant turn. Mirrors solve.py's
        `for tc in msg.tool_calls` body."""
        last = state["messages"][-1]
        new_msgs, trace = [], list(state["tool_trace"])
        submitted = state["submitted"]
        for tc in last.get("tool_calls", []):
            name = tc["function"]["name"]
            try:
                args = json.loads(tc["function"]["arguments"] or "{}")
            except json.JSONDecodeError:
                args = {}
            result = dispatch(ex, name, args)
            if verbose:
                preview = (args.get("cmd") or args.get("path") or args.get("summary") or "")
                head = result.splitlines()[0][:80] if result else ""
                print(f"[{state['steps']}] {name}({str(preview)[:80]}) -> {head}")
            trace.append({"step": state["steps"], "tool": name, "args": args, "result": result})
            new_msgs.append({"role": "tool", "tool_call_id": tc["id"], "content": result})
            if name == "submit":
                submitted = args.get("summary", "")
        out = {"messages": state["messages"] + new_msgs, "tool_trace": trace}
        if submitted is not None:
            out["submitted"] = submitted
            out["stop_reason"] = "submitted"
        return out

    def route_after_agent(state: AgentState) -> str:
        """tool calls → run them; otherwise (model stopped / api error) → finish."""
        last = state["messages"][-1]
        return "tools" if (last.get("role") == "assistant" and last.get("tool_calls")) else "end"

    def route_after_tools(state: AgentState) -> str:
        """submitted or step budget exhausted → finish; else loop back to the model."""
        if state["submitted"] is not None or state["steps"] >= state["max_steps"]:
            return "end"
        return "agent"

    g = StateGraph(AgentState)
    g.add_node("agent", agent_node)
    g.add_node("tools", tools_node)
    g.add_edge(START, "agent")
    g.add_conditional_edges("agent", route_after_agent, {"tools": "tools", "end": END})
    g.add_conditional_edges("tools", route_after_tools, {"agent": "agent", "end": END})
    return g.compile()


# --------------------------------------------------------------------------- #
# Driver — mirrors solve.run_agent: prep repo, run the graph, grade, tear down.
# --------------------------------------------------------------------------- #
def run_agent_graph(issue: dict, model: str, max_steps: int, verbose: bool,
                    sandbox: str = "local") -> dict:
    import os
    os.environ["_MECHANIC_MODEL"] = model  # route make_client (deepseek* -> DeepSeek base_url)

    iid = issue["id"]
    repo_dir = prepare_repo(issue, reset=True)
    test_name = drop_repro(issue, repo_dir)
    ex = DockerExecutor(repo_dir) if sandbox == "docker" else LocalExecutor(repo_dir)
    client = make_client()
    graph = build_graph(ex, client, model, verbose)

    init: AgentState = {
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT.format(test_name=test_name)},
            {"role": "user", "content":
                f"Fix this bug:\n\n{issue_brief(issue)}\n\n"
                f"fix_hint (from the issue triage, may be imperfect): {issue.get('fix_hint', '(none)')}\n\n"
                f"The failing test is {test_name}. Start by running it."},
        ],
        "steps": 0, "max_steps": max_steps, "submitted": None,
        "stop_reason": "max_steps", "tool_trace": [],
    }
    try:
        # recursion_limit counts super-steps; each agent+tools cycle is ~2, so give room.
        final = graph.invoke(init, {"recursion_limit": max_steps * 2 + 10})
        verdict = grade(issue, repo_dir, ex)
    finally:
        ex.close()

    return {
        "issue": iid, "model": model, "engine": "langgraph",
        "steps": final["steps"], "max_steps": max_steps, "sandbox": sandbox,
        "stop_reason": final["stop_reason"], "submitted_summary": final["submitted"],
        "verdict": verdict, "messages": final["messages"], "tool_trace": final["tool_trace"],
    }


def main():
    ap = argparse.ArgumentParser(description="Phase 3: fix one eval issue via a LangGraph graph.")
    ap.add_argument("--issue", required=True, help="issue id, e.g. furl-163")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--max-steps", type=int, default=MAX_STEPS)
    ap.add_argument("--sandbox", choices=["local", "docker"], default="local")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    load_env()
    issue = get_issue(args.issue)
    RUNS_DIR.mkdir(exist_ok=True)
    print(f"[graph] Solving {issue['id']} with {args.model} in [{args.sandbox}] (<= {args.max_steps} steps)...\n")
    rec = run_agent_graph(issue, args.model, args.max_steps, args.verbose, args.sandbox)

    out = RUNS_DIR / f"{issue['id']}_graph_{int(time.time())}.json"
    out.write_text(json.dumps(rec, indent=2, default=str))
    v = rec["verdict"]
    status = "RESOLVED ✅" if v["resolved"] else "NOT_RESOLVED ❌"
    print("\n" + "=" * 60)
    print(f"{issue['id']} [langgraph]: {status}   ({rec['steps']} steps, {rec['sandbox']}, stop={rec['stop_reason']}, exit {v['exit']})")
    if rec["submitted_summary"]:
        print(f"  agent: {rec['submitted_summary']}")
    print(f"  repro: {v['summary']}")
    print(f"  trace: {out.relative_to(ROOT)}")
    print("=" * 60)
    sys.exit(0 if v["resolved"] else 2)


if __name__ == "__main__":
    main()
