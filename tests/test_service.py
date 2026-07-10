"""FastAPI service surface — deterministic, no API/Docker/network.

Uses TestClient (in-process) and monkeypatches the agent + PR functions on the service module,
so it exercises the real HTTP contract — health, token auth, validation, 404/500 mapping, and
the open_pr wiring — without ever calling a model, container, or git.
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import service as s  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

client = TestClient(s.app)

FAKE_ISSUE = {"id": "furl-163", "repo": "gruns/furl", "url": "u"}


def _resolved_record(summary="use rsplit"):
    return {"issue": "furl-163", "steps": 7, "stop_reason": "submitted",
            "submitted_summary": summary, "verdict": {"resolved": True, "summary": "1 passed"}}


@pytest.fixture(autouse=True)
def _patch_agent(monkeypatch):
    """Default: known issue + a resolved run. Individual tests override as needed."""
    monkeypatch.setattr(s, "get_issue", lambda iid: dict(FAKE_ISSUE, id=iid))
    monkeypatch.setattr(s, "run_agent", lambda *a, **k: _resolved_record())
    monkeypatch.setattr(s, "prepare_repo", lambda issue, reset=False: Path("/tmp/repo"))


def test_health_needs_no_auth():
    r = client.get("/health")
    assert r.status_code == 200 and r.json()["status"] == "ok"


def test_solve_resolved_happy_path(monkeypatch):
    monkeypatch.delenv("MECHANIC_SERVICE_TOKEN", raising=False)  # auth disabled → no header needed
    r = client.post("/solve", json={"issue": "furl-163"})
    assert r.status_code == 200
    body = r.json()
    assert body["resolved"] is True
    assert body["summary"] == "use rsplit" and body["steps"] == 7
    assert "pr" not in body                                   # open_pr defaulted false


def test_solve_requires_token_when_configured(monkeypatch):
    monkeypatch.setenv("MECHANIC_SERVICE_TOKEN", "s3cret")
    assert client.post("/solve", json={"issue": "furl-163"}).status_code == 401          # missing
    assert client.post("/solve", json={"issue": "furl-163"},
                       headers={"X-Mechanic-Token": "wrong"}).status_code == 401          # wrong
    ok = client.post("/solve", json={"issue": "furl-163"}, headers={"X-Mechanic-Token": "s3cret"})
    assert ok.status_code == 200 and ok.json()["resolved"] is True                        # correct


def test_solve_unknown_issue_is_404(monkeypatch):
    monkeypatch.delenv("MECHANIC_SERVICE_TOKEN", raising=False)
    def _boom(iid):
        raise SystemExit(f"no issue {iid}")
    monkeypatch.setattr(s, "get_issue", _boom)
    r = client.post("/solve", json={"issue": "nope-999"})
    assert r.status_code == 404


def test_solve_agent_crash_is_500(monkeypatch):
    monkeypatch.delenv("MECHANIC_SERVICE_TOKEN", raising=False)
    def _crash(*a, **k):
        raise RuntimeError("docker exploded")
    monkeypatch.setattr(s, "run_agent", _crash)
    r = client.post("/solve", json={"issue": "furl-163"})
    assert r.status_code == 500 and "agent run failed" in r.json()["detail"]


def test_solve_open_pr_when_resolved(monkeypatch):
    monkeypatch.delenv("MECHANIC_SERVICE_TOKEN", raising=False)
    captured = {}
    def _fake_pr(repo_dir, issue, summary, **kw):
        captured.update(kw)
        return {"ok": True, "dry_run": kw.get("dry_run"), "branch": "mechanic/fix-furl-163",
                "title": "fix(furl): use rsplit", "source_files": ["furl/furl.py"],
                "pr_url": "https://github.com/gruns/furl/pull/42", "reason": None}
    monkeypatch.setattr(s, "open_pr", _fake_pr)
    r = client.post("/solve", json={"issue": "furl-163", "open_pr": True, "execute_pr": True})
    assert r.status_code == 200
    pr = r.json()["pr"]
    assert pr["ok"] is True and pr["url"].endswith("/pull/42")
    assert captured["dry_run"] is False                      # execute_pr=True → real PR


def test_solve_open_pr_skipped_when_unresolved(monkeypatch):
    monkeypatch.delenv("MECHANIC_SERVICE_TOKEN", raising=False)
    monkeypatch.setattr(s, "run_agent", lambda *a, **k: {
        "steps": 30, "stop_reason": "max_steps", "submitted_summary": None,
        "verdict": {"resolved": False, "summary": "1 failed"}})
    called = {"pr": False}
    monkeypatch.setattr(s, "open_pr", lambda *a, **k: called.update(pr=True))
    r = client.post("/solve", json={"issue": "furl-163", "open_pr": True})
    body = r.json()
    assert body["resolved"] is False
    assert body["pr"]["ok"] is False and "not resolved" in body["pr"]["reason"]
    assert called["pr"] is False                             # open_pr never invoked on a failed fix


def test_solve_rejects_bad_sandbox(monkeypatch):
    monkeypatch.delenv("MECHANIC_SERVICE_TOKEN", raising=False)
    r = client.post("/solve", json={"issue": "furl-163", "sandbox": "vm"})
    assert r.status_code == 422                               # pydantic pattern validation
