"""Tracing graceful-degradation — the critical property: it must never break a run.
No langfuse install / no keys needed (we only test the DISABLED paths)."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import tracing  # noqa: E402  (stdlib-only import; must always succeed)


def test_disabled_by_default(monkeypatch):
    monkeypatch.delenv("MECHANIC_TRACE", raising=False)
    assert tracing.is_enabled() is False


def test_requires_keys_even_when_opted_in(monkeypatch):
    monkeypatch.setenv("MECHANIC_TRACE", "1")
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    assert tracing.is_enabled() is False


def test_openai_class_is_plain_when_disabled(monkeypatch):
    monkeypatch.delenv("MECHANIC_TRACE", raising=False)
    cls = tracing.openai_class()
    assert cls.__module__.startswith("openai")  # the plain client, not the langfuse drop-in


def test_flush_and_tag_are_noop_when_disabled(monkeypatch):
    monkeypatch.delenv("MECHANIC_TRACE", raising=False)
    tracing.flush()            # must not raise
    tracing.tag_run(x="y")     # must not raise
