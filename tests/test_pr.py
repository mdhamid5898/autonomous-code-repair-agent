"""PR-open plan + execution — deterministic, no git/gh/network.

Exercises build_pr_plan (pure) and open_pr (through an injected recorder runner): branch/title/
body, exclusion of the injected repro from the commit, the exact git/gh command sequence, the
dry-run-executes-nothing guarantee, and PR-URL parsing from gh output.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pr_open as p  # noqa: E402

ISSUE = {"id": "furl-163", "repo": "gruns/furl", "url": "https://github.com/gruns/furl/issues/163"}
REPRO = "test_mechanic_furl_163.py"
DIFF = ("diff --git a/furl/furl.py b/furl/furl.py\n"
        "--- a/furl/furl.py\n+++ b/furl/furl.py\n"
        "-        userpass, netloc = netloc.split('@', 1)\n"
        "+        userpass, netloc = netloc.rsplit('@', 1)\n")


class Recorder:
    """Injected run_cmd: replays canned git-diff reads, records everything, fakes gh output."""
    def __init__(self, changed, diff=DIFF, gh_url="https://github.com/gruns/furl/pull/42"):
        self.changed, self.diff, self.gh_url = changed, diff, gh_url
        self.calls = []

    def __call__(self, cmd):
        self.calls.append(cmd)
        if cmd[:3] == ["git", "diff", "--name-only"]:
            return (0, "\n".join(self.changed) + "\n", "")
        if cmd[:2] == ["git", "diff"]:
            return (0, self.diff, "")
        if cmd[:3] == ["gh", "pr", "create"]:
            return (0, f"{self.gh_url}\n", "")
        return (0, "", "")


def test_plan_excludes_repro_and_builds_commands():
    plan = p.build_pr_plan(ISSUE, "use rsplit so userinfo '@' is kept", ["furl/furl.py", REPRO], DIFF,
                           base="master")
    assert plan["ok"] is True
    assert plan["source_files"] == ["furl/furl.py"]
    assert plan["excluded"] == [REPRO]                      # harness scaffolding not committed
    assert plan["branch"] == "mechanic/fix-furl-163"
    assert plan["title"].startswith("fix(furl): ")
    # command sequence: branch -> add(source only) -> commit -> push -> gh pr create
    verbs = [c[:2] for c in plan["commands"]]
    assert verbs == [["git", "checkout"], ["git", "add"], ["git", "commit"],
                     ["git", "push"], ["gh", "pr"]]
    add_cmd = next(c for c in plan["commands"] if c[:2] == ["git", "add"])
    assert REPRO not in add_cmd and "furl/furl.py" in add_cmd
    gh_cmd = next(c for c in plan["commands"] if c[:2] == ["gh", "pr"])
    assert "--base" in gh_cmd and "master" in gh_cmd and "--head" in gh_cmd
    # body carries the evidence a reviewer needs
    assert "deterministic" in plan["body"] and "furl/furl.py" in plan["body"] and "rsplit" in plan["body"]


def test_plan_no_source_change_is_not_ok():
    plan = p.build_pr_plan(ISSUE, "noop", [REPRO], DIFF)     # only the repro changed
    assert plan["ok"] is False
    assert "no source" in plan["reason"]
    assert plan["commands"] == []


def test_dry_run_executes_nothing_but_the_reads():
    rec = Recorder(changed=["furl/furl.py", REPRO])
    res = p.open_pr(Path("/tmp/x"), ISSUE, "fix it", run_cmd=rec, dry_run=True)
    assert res["ok"] is True and res["dry_run"] is True and res["pr_url"] is None
    # ONLY the two read-only `git diff` probes ran; no checkout/commit/push/gh
    assert rec.calls == [["git", "diff", "--name-only"], ["git", "diff"]]
    assert res["steps"] == []


def test_execute_runs_sequence_and_parses_pr_url():
    rec = Recorder(changed=["furl/furl.py", REPRO])
    res = p.open_pr(Path("/tmp/x"), ISSUE, "fix it", run_cmd=rec, base="master", dry_run=False)
    assert res["ok"] is True and res["dry_run"] is False
    assert res["pr_url"] == "https://github.com/gruns/furl/pull/42"
    ran = [c[:2] for c in rec.calls]
    assert ["git", "checkout"] in ran and ["git", "push"] in ran and ["gh", "pr"] in ran


def test_execute_stops_and_reports_on_failed_command():
    class FailPush(Recorder):
        def __call__(self, cmd):
            out = super().__call__(cmd)
            if cmd[:2] == ["git", "push"]:
                return (1, "", "remote: Permission denied")
            return out
    rec = FailPush(changed=["furl/furl.py"])
    res = p.open_pr(Path("/tmp/x"), ISSUE, "fix it", run_cmd=rec, dry_run=False)
    assert res["ok"] is False
    assert "push" in res["reason"] and "Permission denied" in res["reason"]
    assert res["pr_url"] is None                              # never reached gh pr create
    assert ["gh", "pr"] not in [c[:2] for c in rec.calls]


def test_parse_pr_url():
    assert p._parse_pr_url("Creating pull request\nhttps://github.com/o/r/pull/7\n") == \
        "https://github.com/o/r/pull/7"
    assert p._parse_pr_url("no url here") is None
