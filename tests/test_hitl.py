"""HITL interrupt/resume path — deterministic, no Docker/API.

Scripts the agent (monkeypatched _chat_with_backoff returns canned submit tool-calls) and a
FakeEx (git diff only), then drives the compiled LangGraph graph to assert the human-approval
gate: it PAUSES at a proposed patch (interrupt payload carries the summary + diff), APPROVE
resumes to stop_reason=approved, and REJECT+feedback loops back to the agent for a re-review.
"""
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import hitl_solve as h  # noqa: E402
from langgraph.types import Command  # noqa: E402


class FakeEx:
    def __init__(self, diff="diff --git a/pkg/mod.py b/pkg/mod.py\n--- a/pkg/mod.py\n+++ b/pkg/mod.py\n+    fixed line"):
        self._diff = diff

    def exec_raw(self, cmd):
        return (0, self._diff, "")   # stands in for `git diff`

    def close(self):
        pass


def _msg(content="", tool_calls=None):
    tcs = [types.SimpleNamespace(id=tid, function=types.SimpleNamespace(name=name, arguments=argj))
           for (tid, name, argj) in (tool_calls or [])]
    return types.SimpleNamespace(content=content, tool_calls=tcs or None)


def _scripted_chat(monkeypatch, sequence):
    """Make _chat_with_backoff return sequence[i] on the i-th agent call."""
    it = iter(sequence)
    monkeypatch.setattr(h, "_chat_with_backoff",
                        lambda client, model, messages, verbose, **kw: next(it))


def _init(max_steps=10):
    return {"messages": [{"role": "user", "content": "fix it"}], "steps": 0, "max_steps": max_steps,
            "submitted": None, "stop_reason": "max_steps", "tool_trace": [], "pending_summary": None,
            "proposed_patch": None, "approved": False, "review_log": []}


def _graph(monkeypatch, sequence, diff=None):
    _scripted_chat(monkeypatch, sequence)
    ex = FakeEx(diff) if diff is not None else FakeEx()
    return h.build_hitl_graph(ex, client=None, model="m", verbose=False)


def test_pauses_at_proposed_patch_with_summary_and_diff(monkeypatch):
    g = _graph(monkeypatch, [_msg(tool_calls=[("c1", "submit", '{"summary": "guard against None"}')])])
    cfg = {"configurable": {"thread_id": "t1"}, "recursion_limit": 40}
    res = g.invoke(_init(), cfg)
    assert res.get("__interrupt__"), "graph should pause for human review"
    payload = res["__interrupt__"][0].value
    assert payload["action"] == "approve_patch"
    assert payload["proposed_summary"] == "guard against None"
    assert "pkg/mod.py" in payload["files_changed"]
    assert "fixed line" in payload["diff"]
    # NOT yet accepted while paused
    assert res.get("submitted") is None and res.get("approved") is False


def test_approve_resumes_to_accepted(monkeypatch):
    g = _graph(monkeypatch, [_msg(tool_calls=[("c1", "submit", '{"summary": "the fix"}')])])
    cfg = {"configurable": {"thread_id": "t2"}, "recursion_limit": 40}
    g.invoke(_init(), cfg)
    res = g.invoke(Command(resume={"approved": True}), cfg)
    assert not res.get("__interrupt__")
    assert res["stop_reason"] == "approved"
    assert res["submitted"] == "the fix"
    assert res["approved"] is True
    assert res["review_log"] == [{"step": 1, "approved": True, "feedback": None}]
    assert "fixed line" in (res["proposed_patch"] or "")


def test_reject_with_feedback_loops_back_then_approves(monkeypatch):
    # agent submits on call 1 (rejected) and again on call 2 (approved)
    g = _graph(monkeypatch, [
        _msg(tool_calls=[("c1", "submit", '{"summary": "first try"}')]),
        _msg(tool_calls=[("c2", "submit", '{"summary": "second try"}')]),
    ])
    cfg = {"configurable": {"thread_id": "t3"}, "recursion_limit": 60}
    res = g.invoke(_init(), cfg)
    assert res["__interrupt__"][0].value["proposed_summary"] == "first try"

    # reject with feedback -> should loop back to the agent and re-propose
    res = g.invoke(Command(resume={"approved": False, "feedback": "handle the empty case"}), cfg)
    assert res.get("__interrupt__"), "should pause again after the agent re-proposes"
    assert res["__interrupt__"][0].value["proposed_summary"] == "second try"
    # the reviewer's feedback was injected back into the transcript for the agent
    assert any("CHANGES REQUESTED" in m.get("content", "") and "handle the empty case" in m.get("content", "")
               for m in res["messages"] if isinstance(m, dict))

    # approve the second proposal
    res = g.invoke(Command(resume={"approved": True}), cfg)
    assert res["stop_reason"] == "approved"
    assert res["submitted"] == "second try"
    assert [d["approved"] for d in res["review_log"]] == [False, True]


def test_model_stop_without_submit_never_reaches_review(monkeypatch):
    # agent stops with no tool calls -> no proposal, no interrupt, no approval
    g = _graph(monkeypatch, [_msg(content="I give up", tool_calls=None)])
    cfg = {"configurable": {"thread_id": "t4"}, "recursion_limit": 40}
    res = g.invoke(_init(), cfg)
    assert not res.get("__interrupt__")
    assert res["stop_reason"] == "model_stopped"
    assert res["approved"] is False and res["submitted"] is None
    assert res["review_log"] == []
