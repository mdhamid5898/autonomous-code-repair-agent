#!/usr/bin/env python3
"""
Mechanic — HTTP service (FastAPI) that wraps the repair agent.

The same `solve.run_agent` loop, exposed as a small web service so it can be DEPLOYED and
triggered remotely — e.g. from a GitHub Action on issue-open (see .github/workflows/repair.yml),
turning the agent into a CI/CD step instead of a laptop script.

Endpoints:
  GET  /health              → liveness probe (no auth) → {"status": "ok", ...}
  POST /solve               → run the agent on one benchmark issue; optionally open a PR.
                              Body: {issue, model?, max_steps?, sandbox?, open_pr?, base?, remote?}
                              Returns: {issue, resolved, summary, steps, stop_reason, pr?}

Auth: if MECHANIC_SERVICE_TOKEN is set, POST /solve requires a matching `X-Mechanic-Token`
header (the Action holds it as a secret). If unset, auth is disabled (local dev) — the service
logs that at startup so it is never a silent hole.

The agent and PR steps are reached through module-level names (`run_agent`, `open_pr`) so the
unit tests monkeypatch them and exercise the whole HTTP surface — auth, validation, PR wiring —
with zero API calls, containers, or network.

Run it:
    .venv/bin/python -m uvicorn service:app --host 0.0.0.0 --port 8080
    curl -sS localhost:8080/health
    curl -sS -X POST localhost:8080/solve -H 'content-type: application/json' \
         -H "X-Mechanic-Token: $MECHANIC_SERVICE_TOKEN" -d '{"issue":"furl-163","open_pr":true}'
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from solve import run_agent, get_issue, load_env, prepare_repo, DEFAULT_MODEL, MAX_STEPS  # noqa: E402
from pr_open import open_pr  # noqa: E402

load_env()  # make OPENAI_/DEEPSEEK_ keys available to the agent when the service boots

app = FastAPI(title="Mechanic", version="1.0",
              description="Autonomous code-repair agent as a service.")


class SolveRequest(BaseModel):
    issue: str = Field(..., description="benchmark issue id, e.g. furl-163")
    model: str = DEFAULT_MODEL
    max_steps: int = MAX_STEPS
    sandbox: str = Field("local", pattern="^(local|docker)$")
    open_pr: bool = False
    base: Optional[str] = None
    remote: str = "origin"
    execute_pr: bool = Field(False, description="actually push + open the PR (default: dry-run plan only)")


def _check_token(token: Optional[str]) -> None:
    expected = os.environ.get("MECHANIC_SERVICE_TOKEN")
    if expected and token != expected:
        raise HTTPException(status_code=401, detail="invalid or missing X-Mechanic-Token")


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "service": "mechanic", "auth": bool(os.environ.get("MECHANIC_SERVICE_TOKEN"))}


@app.post("/solve")
def solve(req: SolveRequest, x_mechanic_token: Optional[str] = Header(None)) -> dict:
    _check_token(x_mechanic_token)
    try:
        issue = get_issue(req.issue)            # sys.exit on unknown id → mapped to 404 below
    except SystemExit:
        raise HTTPException(status_code=404, detail=f"unknown issue '{req.issue}'")

    try:
        rec = run_agent(issue, req.model, req.max_steps, verbose=False, sandbox=req.sandbox)
    except Exception as e:  # a live-run failure must be a 500, not a stack trace to the caller
        raise HTTPException(status_code=500, detail=f"agent run failed: {e}")

    verdict = rec.get("verdict") or {}
    resp = {
        "issue": req.issue,
        "resolved": bool(verdict.get("resolved")),
        "summary": rec.get("submitted_summary"),
        "steps": rec.get("steps"),
        "stop_reason": rec.get("stop_reason"),
        "repro": verdict.get("summary"),
    }

    if req.open_pr and resp["resolved"]:
        repo_dir = prepare_repo(issue, reset=False)  # PR the agent's edits as-is; never reset here
        pr = open_pr(repo_dir, issue, rec.get("submitted_summary") or "", base=req.base,
                     remote=req.remote, dry_run=not req.execute_pr)
        resp["pr"] = {"ok": pr.get("ok"), "dry_run": pr.get("dry_run"), "branch": pr.get("branch"),
                      "title": pr.get("title"), "source_files": pr.get("source_files"),
                      "url": pr.get("pr_url"), "reason": pr.get("reason")}
    elif req.open_pr:
        resp["pr"] = {"ok": False, "reason": "not resolved — no PR opened"}
    return resp


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=os.environ.get("HOST", "0.0.0.0"), port=int(os.environ.get("PORT", "8080")))
