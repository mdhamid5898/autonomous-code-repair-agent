#!/usr/bin/env python3
"""
Mechanic — open a pull request for an accepted fix.

Once the deterministic gate has taken a repro red→green, the agent's edits live in the
checked-out repo as an uncommitted `git diff`. This module turns that diff into a real
pull request: it branches, commits ONLY the source changes, pushes, and calls `gh pr
create` with a body that carries the root-cause summary, the file list, the diff, and the
"verified red→green by the deterministic gate" evidence.

Design (mirrors the rest of the project):
  * The PLAN is pure and unit-tested — `build_pr_plan()` computes the branch, title, body,
    and the exact command sequence from (issue, summary, changed files). No side effects.
  * EXECUTION goes through an injected `run_cmd(cmd) -> (code, out, err)` runner, so tests
    assert the plan + command sequence with a recorder and never touch git/gh/network.
  * DRY-RUN IS THE DEFAULT. `open_pr(..., dry_run=True)` returns the plan and the commands
    that WOULD run, executing nothing — so importing/CLI-ing this can never push by accident.
    You opt in to a real PR with `--execute` (and a remote you can actually push to).

The injected repro test (`test_mechanic_<id>.py`) is HARNESS scaffolding, not part of the
fix, so it is excluded from the commit by default — the PR contains only the source change.

Usage:
    .venv/bin/python pr_open.py --issue furl-163                       # dry-run: print the plan
    .venv/bin/python pr_open.py --issue furl-163 --execute \
        --remote origin --base master                                 # real PR (needs a pushable remote + gh auth)
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "eval"))

from verify import run  # noqa: E402  — the same non-raising subprocess wrapper used everywhere
from solve import repro_filename, get_issue, load_env, prepare_repo  # noqa: E402

BODY_DIFF_CAP = 6000  # chars of diff embedded in the PR body (full diff is in the branch)


# --------------------------------------------------------------------------- #
# pure planning — no side effects, fully unit-testable
# --------------------------------------------------------------------------- #
def _short_repo(issue: dict) -> str:
    """A short repo name for the commit scope: 'gruns/furl' -> 'furl'."""
    repo = issue.get("repo") or issue.get("id") or "repo"
    return repo.rstrip("/").split("/")[-1]


def _title(issue: dict, summary: str) -> str:
    first = (summary or "").strip().splitlines()[0] if summary else ""
    first = first[:72] or f"fix {issue.get('id')}"
    return f"fix({_short_repo(issue)}): {first}"


def _body(issue: dict, summary: str, source_files: list[str], diff: str) -> str:
    ref = issue.get("url") or issue.get("instance_id") or issue.get("id")
    files_md = "\n".join(f"- `{f}`" for f in source_files) or "- (none)"
    diff_block = diff[:BODY_DIFF_CAP] + ("\n… (diff truncated; full change is on the branch)"
                                         if len(diff) > BODY_DIFF_CAP else "")
    return (
        f"## What\n"
        f"Automated fix for **{issue.get('id')}** ({ref}).\n\n"
        f"**Root cause & fix (agent summary):**\n> {summary or '(none provided)'}\n\n"
        f"## Files changed\n{files_md}\n\n"
        f"## Verification\n"
        f"Accepted by Mechanic's **deterministic test gate**: a pristine repro that was RED on the "
        f"base commit is re-run after the edit and must go **GREEN** — the fix is never accepted on "
        f"the model's say-so.\n\n"
        f"<details><summary>Diff</summary>\n\n```diff\n{diff_block}\n```\n</details>\n\n"
        f"---\n_Opened by [Mechanic](https://github.com/mdhamid5898/autonomous-code-repair-agent), "
        f"an autonomous code-repair agent._"
    )


def build_pr_plan(issue: dict, summary: str, changed_files: list[str], diff: str, *,
                  branch: str | None = None, base: str | None = None,
                  remote: str = "origin", exclude_repro: bool = True) -> dict:
    """Compute branch, title, body, and the exact git/gh command sequence. Pure.

    Returns {ok, source_files, excluded, branch, title, body, base, remote, commands, reason}.
    `ok` is False (with a reason) when there is no SOURCE change to open a PR for."""
    repro = repro_filename(issue["id"])
    excluded = [f for f in changed_files if exclude_repro and f == repro]
    source_files = [f for f in changed_files if f not in excluded]
    branch = branch or f"mechanic/fix-{issue['id']}"

    if not source_files:
        return {"ok": False, "reason": "no source files changed (nothing but the repro/harness to commit)",
                "source_files": [], "excluded": excluded, "branch": branch,
                "base": base, "remote": remote, "commands": []}

    title = _title(issue, summary)
    body = _body(issue, summary, source_files, diff)
    commit_msg = f"{title}\n\n{(summary or '').strip()}".strip()

    commands: list[list[str]] = [
        ["git", "checkout", "-B", branch],
        ["git", "add", *source_files],
        ["git", "commit", "-m", commit_msg],
        ["git", "push", "-u", remote, branch],
        ["gh", "pr", "create", "--title", title, "--body", body]
        + (["--base", base] if base else []) + ["--head", branch],
    ]
    return {"ok": True, "reason": None, "source_files": source_files, "excluded": excluded,
            "branch": branch, "title": title, "body": body, "base": base, "remote": remote,
            "commands": commands}


def _parse_pr_url(text: str) -> str | None:
    """gh pr create prints the PR URL on stdout; pull the first github.com/.../pull/N out."""
    for tok in (text or "").split():
        if "github.com/" in tok and "/pull/" in tok:
            return tok.strip()
    return None


# --------------------------------------------------------------------------- #
# execution — through an injected runner; dry-run by default
# --------------------------------------------------------------------------- #
def open_pr(repo_dir: Path, issue: dict, summary: str, *, run_cmd=None,
            branch: str | None = None, base: str | None = None, remote: str = "origin",
            exclude_repro: bool = True, dry_run: bool = True) -> dict:
    """Open a PR for the edits currently in `repo_dir`. `run_cmd(cmd) -> (code, out, err)`
    defaults to a real subprocess in repo_dir; tests inject a recorder. Returns the plan
    plus (real mode) the executed steps and the parsed pr_url."""
    if run_cmd is None:
        def run_cmd(cmd):
            return run(cmd, cwd=repo_dir)

    code, names, _ = run_cmd(["git", "diff", "--name-only"])
    changed = [ln.strip() for ln in (names or "").splitlines() if ln.strip()]
    _, diff, _ = run_cmd(["git", "diff"])

    plan = build_pr_plan(issue, summary, changed, diff or "", branch=branch, base=base,
                         remote=remote, exclude_repro=exclude_repro)
    plan["dry_run"] = dry_run
    plan["pr_url"] = None
    plan["steps"] = []
    if not plan["ok"] or dry_run:
        return plan

    for cmd in plan["commands"]:
        c, out, err = run_cmd(cmd)
        plan["steps"].append({"cmd": cmd[:2], "code": c, "out": ((out or "") + (err or ""))[-400:]})
        if c != 0:
            plan["ok"] = False
            plan["reason"] = f"`{' '.join(cmd[:2])}` failed (exit {c}): {(err or out or '').strip()[-300:]}"
            return plan
        if cmd[:3] == ["gh", "pr", "create"]:
            plan["pr_url"] = _parse_pr_url(out)
    return plan


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(description="Open a PR for an accepted fix (dry-run by default).")
    ap.add_argument("--issue", required=True, help="issue id, e.g. furl-163")
    ap.add_argument("--summary", default="", help="root-cause/fix summary for the PR (else read from the repo state)")
    ap.add_argument("--branch", default=None, help="branch name (default mechanic/fix-<id>)")
    ap.add_argument("--base", default=None, help="base branch for the PR (e.g. main/master)")
    ap.add_argument("--remote", default="origin", help="git remote to push to (default origin)")
    ap.add_argument("--execute", action="store_true",
                    help="actually branch/commit/push and open the PR (default is dry-run: print the plan)")
    ap.add_argument("--include-repro", action="store_true",
                    help="also commit the injected repro test (default: exclude harness scaffolding)")
    args = ap.parse_args()

    load_env()
    issue = get_issue(args.issue)
    repo_dir = prepare_repo(issue, reset=False)  # use the tree AS-IS (the agent's edits), never reset here

    res = open_pr(repo_dir, issue, args.summary, branch=args.branch, base=args.base,
                  remote=args.remote, exclude_repro=not args.include_repro, dry_run=not args.execute)

    print("=" * 66)
    if not res["ok"]:
        print(f"PR NOT opened for {issue['id']}: {res['reason']}")
        sys.exit(1)
    print(f"{'DRY-RUN — would open' if res['dry_run'] else 'Opened'} a PR for {issue['id']}")
    print(f"  branch: {res['branch']}  ->  base: {res['base'] or '(default)'}  (remote {res['remote']})")
    print(f"  title:  {res['title']}")
    print(f"  files:  {res['source_files']}" + (f"  (excluded: {res['excluded']})" if res['excluded'] else ""))
    if res["dry_run"]:
        print("  commands that WOULD run (nothing executed):")
        for c in res["commands"]:
            shown = " ".join(c[:6]) + (" …" if len(c) > 6 else "")
            print(f"    $ {shown}")
        print("  → re-run with --execute (and a pushable --remote) to open it for real.")
    elif res["pr_url"]:
        print(f"  PR: {res['pr_url']}")
    print("=" * 66)
    sys.exit(0)


if __name__ == "__main__":
    main()
