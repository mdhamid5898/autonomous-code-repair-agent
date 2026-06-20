#!/usr/bin/env python3
"""
Mechanic — Langfuse observability (opt-in, graceful, bulletproof).

Token/cost/latency tracing for every agent LLM call, via Langfuse's OpenAI drop-in
(`from langfuse.openai import OpenAI`) — it auto-logs each chat.completions.create as a
"generation" (model, prompts, completion, token usage, latency, $ cost) with NO change to the
agent loop. make_client() swaps in this class only when tracing is enabled.

Design rule: tracing must NEVER affect the agent's behavior or break a run. So it is:
  - OPT-IN: on only if MECHANIC_TRACE is truthy AND both Langfuse keys are set.
  - GRACEFUL: any import/runtime problem falls back to the plain openai.OpenAI.
  - ZERO-DEP to import: `import tracing` pulls only stdlib; langfuse is imported lazily.

Enable (in .env or the environment):
  MECHANIC_TRACE=1
  LANGFUSE_PUBLIC_KEY=pk-...
  LANGFUSE_SECRET_KEY=sk-...
  LANGFUSE_HOST=https://cloud.langfuse.com   # or a self-hosted URL
"""
from __future__ import annotations

import os


def _truthy(v) -> bool:
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def is_enabled() -> bool:
    """Tracing is active only when explicitly opted in AND both Langfuse keys are present."""
    if not _truthy(os.environ.get("MECHANIC_TRACE", "")):
        return False
    return bool(os.environ.get("LANGFUSE_PUBLIC_KEY") and os.environ.get("LANGFUSE_SECRET_KEY"))


def openai_class():
    """The OpenAI client CLASS to instantiate: Langfuse's auto-tracing drop-in when tracing is
    enabled and importable, else the plain openai.OpenAI. Never raises — falls back to plain on
    any problem, so a tracing misconfig can never break the agent."""
    if is_enabled():
        try:
            from langfuse.openai import OpenAI  # drop-in subclass: same ctor, auto-traces every call
            return OpenAI
        except Exception:
            pass  # langfuse missing/broken -> plain client, agent unaffected
    from openai import OpenAI
    return OpenAI


def tag_run(**metadata) -> None:
    """Best-effort: attach metadata (e.g. instance_id, engine) to the current Langfuse trace.
    No-op if disabled or unsupported by the installed langfuse version."""
    if not is_enabled():
        return
    try:
        from langfuse import Langfuse
        Langfuse().update_current_trace(metadata=metadata)
    except Exception:
        pass


def flush() -> None:
    """Flush buffered events to Langfuse at the end of a run/sweep. No-op if disabled."""
    if not is_enabled():
        return
    try:
        from langfuse import Langfuse
        Langfuse().flush()
    except Exception:
        pass


if __name__ == "__main__":
    cls = openai_class()
    print(f"MECHANIC_TRACE enabled: {is_enabled()}")
    print(f"client class: {cls.__module__}.{cls.__name__}")
