#!/usr/bin/env python3
"""
Mechanic — week-1 single-script core loop (proof of life).

The thinnest honest end-to-end repair agent:

    OpenAI (tool-calling) <-> bash + str_replace in the repo's venv <-> pytest

For one eval issue it:
  1. PREP   resets eval/.repos/<id> to its pinned base_commit (clean tree),
            reusing the per-repo .venv that verify.py already built.
  2. DROP   copies eval/repros/<id>.py into the repo as test_mechanic_<id>.py
            (the red->green contract the agent works against).
  3. LOOP   the model drives bash/str_replace to localize and fix the bug,
            running the repro itself to check progress (<= --max-steps turns).
  4. GRADE  re-copies a PRISTINE repro (anti-tamper) and runs it once more.
            exit 0 => RESOLVED (red->green). Anything else => NOT_RESOLVED.
  5. TRACE  writes the full transcript + verdict to runs/<id>_<ts>.json.

Execution backend is `LocalExecutor` — bash in the repo's venv. The agent code
never names Docker; swapping in a DockerExecutor later is a backend change at the
one `_exec` seam, not a rewrite. This is the deliberate week-1 ordering: prove the
loop works against a known substrate first, harden isolation second.

Usage:
  python solve.py --issue furl-163                 # live run (needs OPENAI_API_KEY)
  python solve.py --issue furl-163 --self-check    # no API: prove the plumbing (expect RED)
  python solve.py --issue furl-163 --model gpt-4o --max-steps 30 --verbose

Run it with the harness venv interpreter:  .venv/bin/python solve.py ...
(OPENAI_API_KEY is read from the environment or a gitignored .env at the repo root.)
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
EVAL_DIR = ROOT / "eval"
REPOS_DIR = EVAL_DIR / ".repos"
REPROS_DIR = EVAL_DIR / "repros"
RUNS_DIR = ROOT / "runs"

# reuse verify.py's clone/venv/run plumbing rather than reimplementing it
sys.path.insert(0, str(EVAL_DIR))
from verify import run, venv_paths, venv_env, tail, load_manifest  # noqa: E402

# OpenAI exception types for backoff (dummy fallbacks if SDK absent; run_agent guards too)
try:
    from openai import RateLimitError as _RateLimitError, APIError as _APIError  # noqa: E402
except Exception:  # pragma: no cover
    class _RateLimitError(Exception):
        pass

    class _APIError(Exception):
        pass

DEFAULT_MODEL = "deepseek-v4-flash"  # cheap default ($0.14/$0.28 per M); escalate to deepseek-v4-pro
# for hard fixes via --model. (The legacy "deepseek-chat"/"deepseek-reasoner" aliases are deprecated
# 2026-07-24 and now map to v4-flash anyway; make_client routes any "deepseek*" to the DeepSeek base URL.)
MAX_STEPS = 30
BASH_TIMEOUT = 90        # seconds per agent command
GRADE_TIMEOUT = 120
INSTALL_TIMEOUT = 600    # seconds for in-container `pip install -e .`
TOOL_OUTPUT_CAP = 8000   # chars of command output fed back to the model


# --------------------------------------------------------------------------- #
# config / manifest
# --------------------------------------------------------------------------- #
def load_env() -> None:
    """Load KEY=VALUE lines from a gitignored .env (does not override real env)."""
    envfile = ROOT / ".env"
    if not envfile.exists():
        return
    for line in envfile.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip().strip('"').strip("'")
        os.environ.setdefault(k, v)


def make_client():
    """Return an OpenAI-compatible client routed by DEFAULT_MODEL / --model flag.
    DeepSeek uses its own base URL and DEEPSEEK_API_KEY; everything else uses OPENAI_API_KEY."""
    from openai import OpenAI
    model = os.environ.get("_MECHANIC_MODEL", DEFAULT_MODEL)
    if model.startswith("deepseek"):
        key = os.environ.get("DEEPSEEK_API_KEY")
        if not key:
            sys.exit("DEEPSEEK_API_KEY not set. Add it to your .env file.")
        return OpenAI(api_key=key, base_url="https://api.deepseek.com", max_retries=6)
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        sys.exit("OPENAI_API_KEY not set. Put it in a .env file at the repo root.")
    return OpenAI(api_key=key, max_retries=6)


def get_issue(iid: str) -> dict:
    doc, _, _ = load_manifest()
    for issue in doc["issues"]:
        if issue["id"] == iid:
            return issue
    sys.exit(f"No issue with id '{iid}' in {EVAL_DIR / 'issues.yaml'}.")


def issue_brief(issue: str) -> str:
    """Compact human-readable summary of the issue for the model prompt."""
    return "\n".join(
        f"{k}: {issue[k]}"
        for k in ("id", "instance_id", "repo", "url", "bug_type", "bug_summary", "problem_statement")
        if issue.get(k)
    )


# --------------------------------------------------------------------------- #
# repo prep / grading (the deterministic guardrail)
# --------------------------------------------------------------------------- #
def repro_filename(iid: str) -> str:
    return f"test_mechanic_{iid.replace('-', '_')}.py"


def prepare_repo(issue: dict, *, reset: bool) -> Path:
    """Ensure the clone exists at its pinned base_commit with a clean tree.
    Reuses the per-repo .venv built by verify.py (never deletes it)."""
    iid = issue["id"]
    repo_dir = REPOS_DIR / iid
    if not repo_dir.exists():
        sys.exit(f"{repo_dir} missing — run `python eval/verify.py --only {iid}` first.")
    venv, _ = venv_paths(repo_dir)
    if not venv.exists():
        sys.exit(f"{venv} missing — run `python eval/verify.py --only {iid}` first.")

    if reset:
        base = issue.get("base_commit")
        cur = run(["git", "rev-parse", "HEAD"], cwd=repo_dir)[1].strip()
        if base and cur != base:
            run(["git", "checkout", "--quiet", base], cwd=repo_dir)
        # revert tracked edits + drop untracked junk, but KEEP the venv
        run(["git", "checkout", "--quiet", "--", "."], cwd=repo_dir)
        run(["git", "clean", "-fdq", "-e", ".venv"], cwd=repo_dir)
    return repo_dir


def drop_repro(issue: dict, repo_dir: Path) -> str:
    """Copy the repro into the repo; return its filename."""
    iid = issue["id"]
    dst = repo_dir / repro_filename(iid)
    shutil.copyfile(REPROS_DIR / f"{iid}.py", dst)
    return dst.name


def grade(issue: dict, repo_dir: Path, ex: "Executor") -> dict:
    """Re-copy a PRISTINE repro (so test-tampering can't pass) and run it once,
    wherever the executor runs commands (host venv or container). exit 0 => RESOLVED.
    drop_repro writes host-side; under Docker that path is bind-mounted into /repo."""
    test_name = drop_repro(issue, repo_dir)  # overwrite with clean copy
    code, out, err = ex.exec_raw(
        f"python -m pytest {test_name} -q -rN --no-header -p no:cacheprovider -o addopts="
    )
    summary = ""
    for line in reversed(((out or "") + "\n" + (err or "")).splitlines()):
        if any(w in line for w in ("passed", "failed", "error")):
            summary = line.strip(" =")
            break
    return {"resolved": code == 0, "exit": code, "summary": summary,
            "log": tail((out or "") + (err or ""), 30)}


# --------------------------------------------------------------------------- #
# execution backend — the Docker-relevant seam.
#
#   Executor       : bash() + str_replace() + grading all share this base; the
#                    ONLY thing a backend overrides is exec_raw() (how a command
#                    actually runs).
#   LocalExecutor  : exec_raw -> subprocess in the repo's venv (fast, no isolation).
#   DockerExecutor : exec_raw -> `docker exec` into a per-issue container (isolated).
#
# str_replace edits the repo dir directly. Under Docker that dir is bind-mounted
# into the container, so host-side edits are instantly visible to the in-container
# pytest — only command execution changes, never editing.
# --------------------------------------------------------------------------- #
class Executor:
    def __init__(self, repo_dir: Path):
        self.repo_dir = repo_dir

    def exec_raw(self, cmd: str):
        """Run a shell command; return (exit_code, stdout, stderr). Backend-specific."""
        raise NotImplementedError

    def close(self):
        """Tear down backing resources (e.g. containers). No-op by default."""

    def _safe(self, rel: str) -> Path:
        """Resolve a repo-relative path, refusing escapes outside the repo."""
        p = (self.repo_dir / rel).resolve()
        if not str(p).startswith(str(self.repo_dir.resolve())):
            raise ValueError(f"path escapes repo: {rel}")
        return p

    def bash(self, cmd: str) -> str:
        code, out, err = self.exec_raw(cmd)
        body = (out or "") + (("\n[stderr]\n" + err) if err else "")
        if len(body) > TOOL_OUTPUT_CAP:
            head, taillen = 1500, TOOL_OUTPUT_CAP - 1500
            body = (body[:head] + f"\n...[truncated {len(body) - TOOL_OUTPUT_CAP} chars]...\n"
                    + body[-taillen:])
        return f"(exit {code})\n{body}".rstrip()

    def str_replace(self, path: str, old_str: str, new_str: str) -> str:
        try:
            f = self._safe(path)
        except ValueError as e:
            return f"ERROR: {e}"
        if not f.exists():
            return f"ERROR: {path} does not exist"
        text = f.read_text()
        n = text.count(old_str)
        if n == 0:
            return f"ERROR: old_str not found in {path} (it must match exactly, incl. whitespace)"
        if n > 1:
            return f"ERROR: old_str occurs {n} times in {path}; make it unique with more context"
        f.write_text(text.replace(old_str, new_str, 1))
        return f"OK: edited {path}"


class LocalExecutor(Executor):
    """bash + pytest run in the repo's host venv (built by verify.py). Fast, no isolation."""

    def __init__(self, repo_dir: Path):
        super().__init__(repo_dir)
        _, bindir = venv_paths(repo_dir)
        self.env = venv_env(bindir)

    def exec_raw(self, cmd: str):
        return run(cmd, cwd=self.repo_dir, env=self.env, timeout=BASH_TIMEOUT, shell=True)


class DockerExecutor(Executor):
    """bash + pytest run inside a per-issue container with the repo bind-mounted at
    /repo — isolated from the host filesystem (outside the mount) and processes.
    Network is currently ON so `pip install -e .` can reach PyPI; locking it down
    (--network none + pre-provisioned deps) is a later hardening sub-step."""

    IMAGE = "mechanic-sandbox"

    def __init__(self, repo_dir: Path):
        super().__init__(repo_dir)
        self.name = f"mechanic_{repo_dir.name}"
        run(["docker", "rm", "-f", self.name], timeout=60)  # clear any stale container
        code, out, err = run(
            ["docker", "run", "-d", "--name", self.name,
             "-v", f"{repo_dir}:/repo", "-w", "/repo",
             self.IMAGE, "sleep", "infinity"],
            timeout=120,
        )
        if code != 0:
            raise RuntimeError(f"docker run failed: {tail(err or out)}")
        # install the target repo editable inside the container (pytest is baked in)
        code, out, err = run(
            ["docker", "exec", "-w", "/repo", self.name, "bash", "-lc", "pip install -e ."],
            timeout=INSTALL_TIMEOUT,
        )
        if code != 0:
            self.close()
            raise RuntimeError(f"in-container `pip install -e .` failed: {tail(err or out)}")

    def exec_raw(self, cmd: str):
        return run(["docker", "exec", "-w", "/repo", self.name, "bash", "-lc", cmd],
                   timeout=BASH_TIMEOUT)

    def close(self):
        run(["docker", "rm", "-f", self.name], timeout=60)


# --------------------------------------------------------------------------- #
# tools (OpenAI function schema) + dispatch
# --------------------------------------------------------------------------- #
TOOLS = [
    {"type": "function", "function": {
        "name": "bash",
        "description": ("Run a shell command from the repository root inside its venv. "
                        "Use for exploring (grep -rn, find, cat -n, ls) and for running the "
                        "repro test. Returns the exit code plus combined stdout/stderr."),
        "parameters": {"type": "object", "properties": {
            "cmd": {"type": "string", "description": "the shell command to run"}},
            "required": ["cmd"]}}},
    {"type": "function", "function": {
        "name": "str_replace",
        "description": ("Replace an exact substring in a SOURCE file (never the test). "
                        "old_str must appear exactly once — include enough surrounding "
                        "context to be unique. Preferred over sed for precise edits."),
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "repo-relative path to the file"},
            "old_str": {"type": "string", "description": "exact text to replace (unique in file)"},
            "new_str": {"type": "string", "description": "replacement text"}},
            "required": ["path", "old_str", "new_str"]}}},
    {"type": "function", "function": {
        "name": "submit",
        "description": "Call when you believe the bug is fixed and the repro test passes.",
        "parameters": {"type": "object", "properties": {
            "summary": {"type": "string", "description": "1-2 sentences: the root cause and your fix"}},
            "required": ["summary"]}}},
]


def dispatch(ex: LocalExecutor, name: str, args: dict) -> str:
    if name == "bash":
        return ex.bash(args.get("cmd", ""))
    if name == "str_replace":
        return ex.str_replace(args.get("path", ""), args.get("old_str", ""), args.get("new_str", ""))
    if name == "submit":
        return "submitted"
    return f"ERROR: unknown tool {name}"


SYSTEM_PROMPT = """You are an autonomous software engineer fixing a bug in a Python repository.

You are in a checked-out repo at a commit that EXHIBITS the bug. A failing test \
named {test_name} has been added — it encodes the behavior your fix must produce. \
Your job: make that test pass by editing the SOURCE code. NEVER edit the test file.

Tools: `bash` (explore + run tests), `str_replace` (precise source edits), `submit` (finish).
Every `bash` command already runs from the repository root: use repo-relative paths, never `cd`, and never search outside it (no `find /`).

Method:
1. Run the test to see the failure:  python -m pytest {test_name} -q -o addopts=
2. Localize: grep/cat the source to find the responsible function. Read it carefully.
3. Make the smallest correct change to the source. Prefer str_replace.
4. Re-run the test. Iterate until it PASSES.
5. Call `submit` with a one-line root-cause + fix summary.

Keep changes minimal and targeted. Do not install packages or touch unrelated files."""


# --------------------------------------------------------------------------- #
# the agent loop
# --------------------------------------------------------------------------- #
def _chat_with_backoff(client, model, messages, verbose, attempts=6):
    """Call chat.completions, backing off on rate limits / transient API errors.
    Returns the assistant message, or None if the API stays unavailable."""
    for i in range(attempts):
        try:
            resp = client.chat.completions.create(
                model=model, messages=messages, tools=TOOLS,
                tool_choice="auto", temperature=0,
            )
            return resp.choices[0].message
        except (_RateLimitError, _APIError) as e:
            wait = min(2 ** i, 30)
            if verbose:
                print(f"  [{type(e).__name__}; sleeping {wait}s then retrying ({i + 1}/{attempts})]")
            time.sleep(wait)
    return None


def run_agent(issue: dict, model: str, max_steps: int, verbose: bool, sandbox: str = "local",
              governed: bool = False) -> dict:
    """Single-agent repair loop. If governed=True, `submit` is TEST-GATED: a submit is
    accepted only if the pristine repro actually passes; otherwise it's rejected and the
    agent must keep fixing (no premature wrong-submit, forced retry). This ports the
    multi-agent's Tester→Reviewer governance onto the single agent, to isolate whether
    multi's edge is that control loop vs. the decomposition itself."""
    try:
        from openai import OpenAI  # noqa: F401 — make_client imports it
    except ImportError:
        sys.exit("openai SDK not installed. Run: uv pip install --python .venv/bin/python openai")

    os.environ["_MECHANIC_MODEL"] = model
    iid = issue["id"]
    repo_dir = prepare_repo(issue, reset=True)
    test_name = drop_repro(issue, repo_dir)
    ex = DockerExecutor(repo_dir) if sandbox == "docker" else LocalExecutor(repo_dir)
    client = make_client()

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT.format(test_name=test_name)},
        {"role": "user", "content":
            f"Fix this bug:\n\n{issue_brief(issue)}\n\n"
            f"fix_hint (from the issue triage, may be imperfect): {issue.get('fix_hint', '(none)')}\n\n"
            f"The failing test is {test_name}. Start by running it."},
    ]

    trace = []  # list of {step, tool, args, result} for the run record
    submitted = None
    stop_reason = "max_steps"
    steps = 0
    while steps < max_steps:
        steps += 1
        msg = _chat_with_backoff(client, model, messages, verbose)
        if msg is None:                  # API stayed unavailable -> grade what we have
            stop_reason = "api_error"
            break
        # record assistant turn (reconstructed so the API round-trips cleanly)
        a = {"role": "assistant", "content": msg.content or ""}
        if msg.tool_calls:
            a["tool_calls"] = [
                {"id": tc.id, "type": "function",
                 "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                for tc in msg.tool_calls
            ]
        messages.append(a)

        if not msg.tool_calls:
            stop_reason = "model_stopped"
            if verbose and msg.content:
                print(f"[{steps}] (no tool call) {msg.content[:200]}")
            break

        for tc in msg.tool_calls:
            name = tc.function.name
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            if governed and name == "submit":
                # test-gated submit: accept only if the pristine repro actually passes;
                # else reject and force the agent to keep fixing (no premature wrong-submit).
                gv = grade(issue, repo_dir, ex)
                if gv["resolved"]:
                    result = "ACCEPTED — the repro passes. Done."
                    submitted = args.get("summary", "")
                else:
                    result = (f"REJECTED — the repro still FAILS, you are not done:\n{gv['log']}\n"
                              "Keep editing the SOURCE and re-run the test; submit only once it passes.")
            else:
                result = dispatch(ex, name, args)
                if name == "submit":
                    submitted = args.get("summary", "")
            preview = (args.get("cmd") or args.get("path") or args.get("summary") or "")
            if verbose:
                print(f"[{steps}] {name}({str(preview)[:80]}) -> {result.splitlines()[0][:80] if result else ''}")
            trace.append({"step": steps, "tool": name, "args": args, "result": result})
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})
        if submitted is not None:
            stop_reason = "submitted"
            break

    try:
        verdict = grade(issue, repo_dir, ex)
    finally:
        ex.close()  # always tear down the container (stale ones are also cleared on next run)
    return {
        "issue": iid, "model": model, "steps": steps, "max_steps": max_steps,
        "sandbox": sandbox, "stop_reason": stop_reason, "submitted_summary": submitted,
        "verdict": verdict, "messages": messages, "tool_trace": trace,
    }


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(description="Week-1 core loop: fix one eval issue end-to-end.")
    ap.add_argument("--issue", required=True, help="issue id, e.g. furl-163")
    ap.add_argument("--model", default=DEFAULT_MODEL, help=f"OpenAI model (default {DEFAULT_MODEL})")
    ap.add_argument("--max-steps", type=int, default=MAX_STEPS)
    ap.add_argument("--sandbox", choices=["local", "docker"], default="local",
                    help="where the agent runs commands: local venv (default) or docker container")
    ap.add_argument("--self-check", action="store_true",
                    help="no API: reset + drop repro + grade, expecting RED (proves the plumbing)")
    ap.add_argument("--verbose", action="store_true", help="print each tool call")
    args = ap.parse_args()

    load_env()
    issue = get_issue(args.issue)

    if args.self_check:
        repo_dir = prepare_repo(issue, reset=True)
        ex = DockerExecutor(repo_dir) if args.sandbox == "docker" else LocalExecutor(repo_dir)
        try:
            v = grade(issue, repo_dir, ex)  # bug present on clean base => repro must FAIL
        finally:
            ex.close()
        ok = not v["resolved"]
        print(f"\nSELF-CHECK {issue['id']} [{args.sandbox}]: repro "
              f"{'RED (bug present) ✅ plumbing OK' if ok else 'GREEN ⚠️ bug absent on base — unexpected'}")
        print(f"  exit={v['exit']}  {v['summary']}")
        sys.exit(0 if ok else 1)

    RUNS_DIR.mkdir(exist_ok=True)
    print(f"Solving {issue['id']} with {args.model} in [{args.sandbox}] sandbox (<= {args.max_steps} steps)...\n")
    rec = run_agent(issue, args.model, args.max_steps, args.verbose, args.sandbox)

    ts = int(time.time())
    out = RUNS_DIR / f"{issue['id']}_{ts}.json"
    out.write_text(json.dumps(rec, indent=2, default=str))

    v = rec["verdict"]
    status = "RESOLVED ✅" if v["resolved"] else "NOT_RESOLVED ❌"
    print("\n" + "=" * 60)
    print(f"{issue['id']}: {status}   ({rec['steps']} steps, {rec['sandbox']}, stop={rec['stop_reason']}, exit {v['exit']})")
    if rec["submitted_summary"]:
        print(f"  agent: {rec['submitted_summary']}")
    print(f"  repro: {v['summary']}")
    print(f"  trace: {out.relative_to(ROOT)}")
    print("=" * 60)
    sys.exit(0 if v["resolved"] else 2)


if __name__ == "__main__":
    main()
