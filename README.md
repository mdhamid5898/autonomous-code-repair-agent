# Autonomous Code-Repair Agent

Given a failing GitHub issue, an LLM agent localizes the bug, edits the source in a
sandbox, and runs the test suite until it goes from **red to green** — gated by a
deterministic test-pass check, not the model's say-so.

> **Baseline: 9 / 15 (60%)** issues resolved with GPT-4o on a curated benchmark of
> real GitHub bugs (single shot per issue).

## How it works

A single agentic loop — deliberately no framework yet:

```
issue + failing test
      │
      ▼
┌─────────────────────────────────────────────┐
│  agent (GPT-4o, tool-calling)                │
│   tools: bash · str_replace · submit         │
└───────────────┬─────────────────────────────┘
                │ runs commands through an Executor
      ┌─────────┴──────────┐
 LocalExecutor        DockerExecutor
 (repo's venv)     (isolated container)
                │
                ▼
      pytest red → green   ← the only definition of "resolved"
```

The agent explores with `grep`/`cat`, edits with a precise `str_replace`, and re-runs
the test itself until it passes. *Where* commands run is swappable behind one
`exec_raw` seam: a fast local venv, or an isolated Docker container (`--sandbox docker`).

## The benchmark (`eval/`)

15 real, open GitHub issues across 9 small/medium Python libraries. Each is:

- **pinned** to a `base_commit` where the bug is present, and
- **repro-verified red-on-base** — a dropped-in test that *fails* on the buggy commit
  and must *pass* after the fix.

`verify.py` enforces this: an issue only counts if its repro fails on base (which
catches "open but already patched upstream" traps). Resolution = the repro flips
red→green without the model touching the test.

## Quickstart

```bash
uv venv .venv
uv pip install --python .venv/bin/python -r eval/requirements-dev.txt openai

# rebuild + verify the benchmark (clones repos, pins commits, confirms red-on-base)
.venv/bin/python eval/verify.py                        # -> READY:15

# fix one issue end-to-end (needs OPENAI_API_KEY in a .env file at the repo root)
.venv/bin/python solve.py --issue furl-163 --verbose

# build the sandbox image once, then run the agent inside a container
docker build -t mechanic-sandbox -f docker/sandbox.Dockerfile docker/
.venv/bin/python solve.py --issue furl-163 --sandbox docker

# run the whole benchmark and report the resolution rate
.venv/bin/python sweep.py
```

## Layout

```
solve.py     # the agent: loop + tools + executors (local / docker) + grading
sweep.py     # run all 15 issues and report the resolution rate
eval/        # benchmark: issues.yaml (manifest), repros/, verify.py harness
docker/      # sandbox image definition
```

## Status / roadmap

Working single-agent loop with a verified benchmark and a Docker sandbox. Next:
port the loop to **LangGraph**, split into specialist agents (planner / coder /
tester / reviewer), add **retrieval** for localization, and wire **observability** +
a cost-aware model **router**.
