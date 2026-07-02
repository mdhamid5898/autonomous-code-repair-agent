"""CI feedback flywheel control flow — deterministic, no API/Docker.

Injects a fake repro-generator + a scripted red-on-base verifier (so no LLM/verify/clone runs) and
points the module at a tmp manifest, then asserts: accept on READY, retry-with-feedback until READY,
reject+rollback after max attempts, and that materialize/remove actually write/clean the manifest+repro.
"""
import sys
from pathlib import Path

import pytest  # noqa: F401

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import eval_flywheel as fw  # noqa: E402


@pytest.fixture
def tmp_manifest(tmp_path, monkeypatch):
    monkeypatch.setattr(fw, "FLYWHEEL_MANIFEST", tmp_path / "issues_flywheel.yaml")
    monkeypatch.setattr(fw, "REPROS_DIR", tmp_path / "repros")
    return tmp_path


CTX = {"id": "demo-fw", "repo": "acme/widget", "clone": "https://x/widget.git",
       "base_commit": "abc123", "pkg": "widget", "import_name": "widget",
       "problem_statement": "widget.f() returns None instead of 0", "error_tail": ""}


def _rec(verdict, status=None, log=""):
    return {"verdict": verdict, "repro": {"status": status, "log": log}}


def test_accept_on_ready(tmp_manifest):
    gen = lambda ctx, prior: "def test_x():\n    assert True\n"
    result = fw.run_flywheel(CTX, client=None, model="m", gen=gen,
                             verifier=lambda issue: _rec("READY", "FAIL"), verbose=False)
    assert result["accepted"] is True
    assert result["case_id"] == "demo-fw"
    assert [a["verdict"] for a in result["attempts"]] == ["READY"]
    # persisted + verified in the manifest, repro file written
    import yaml
    doc = yaml.safe_load(fw.FLYWHEEL_MANIFEST.read_text())
    entry = doc["issues"][0]
    assert entry["id"] == "demo-fw" and entry["repro_verified"] is True
    assert (fw.REPROS_DIR / "demo-fw.py").exists()


def test_retry_with_feedback_until_ready(tmp_manifest):
    seq = iter([_rec("REPRO_ERROR", "ERROR", "ImportError"), _rec("BUG_ABSENT", "PASS"), _rec("READY", "FAIL")])
    seen_priors = []

    def gen(ctx, prior):
        seen_priors.append(prior)
        return f"def test_x():\n    assert {len(seen_priors)}\n"

    result = fw.run_flywheel(CTX, client=None, model="m", gen=gen,
                             verifier=lambda issue: next(seq), verbose=False)
    assert result["accepted"] is True
    assert [a["verdict"] for a in result["attempts"]] == ["REPRO_ERROR", "BUG_ABSENT", "READY"]
    # first attempt has no prior; retries carry the previous verdict as feedback
    assert seen_priors[0] is None
    assert seen_priors[1]["verdict"] == "REPRO_ERROR"
    assert seen_priors[2]["verdict"] == "BUG_ABSENT"


def test_reject_after_max_attempts_rolls_back(tmp_manifest):
    gen = lambda ctx, prior: "def test_x():\n    raise ImportError\n"
    result = fw.run_flywheel(CTX, client=None, model="m", gen=gen,
                             verifier=lambda issue: _rec("REPRO_ERROR", "ERROR"),
                             max_attempts=2, verbose=False)
    assert result["accepted"] is False
    assert len(result["attempts"]) == 2
    # rolled back: no manifest entry and no repro file left behind
    assert not (fw.REPROS_DIR / "demo-fw.py").exists()
    if fw.FLYWHEEL_MANIFEST.exists():
        import yaml
        doc = yaml.safe_load(fw.FLYWHEEL_MANIFEST.read_text()) or {"issues": []}
        assert all(i["id"] != "demo-fw" for i in doc.get("issues", []))


def test_bug_absent_is_not_accepted(tmp_manifest):
    # a repro that PASSES on the buggy base does not reproduce the bug -> must be rejected
    gen = lambda ctx, prior: "def test_x():\n    assert True\n"
    result = fw.run_flywheel(CTX, client=None, model="m", gen=gen,
                             verifier=lambda issue: _rec("BUG_ABSENT", "PASS"),
                             max_attempts=1, verbose=False)
    assert result["accepted"] is False


def test_materialize_and_remove(tmp_manifest):
    issue = fw.materialize_case("demo-fw", CTX, "def test_x():\n    assert 1\n", repro_verified=True)
    assert issue["repro_verified"] is True and issue["tier"] == "flywheel"
    assert "auto-generated" in issue["flags"]
    assert (fw.REPROS_DIR / "demo-fw.py").read_text().startswith("def test_x")
    fw.remove_case("demo-fw")
    assert not (fw.REPROS_DIR / "demo-fw.py").exists()
    import yaml
    doc = yaml.safe_load(fw.FLYWHEEL_MANIFEST.read_text()) or {"issues": []}
    assert all(i["id"] != "demo-fw" for i in doc.get("issues", []))


def test_strip_fences():
    assert fw._strip_fences("```python\nx=1\n```").strip() == "x=1"
    assert fw._strip_fences("x=2").strip() == "x=2"
