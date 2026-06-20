#!/usr/bin/env python3
"""
Mechanic — model router: cheap by default, escalate to a stronger tier on retry.

The experiments earned this: deepseek-v4-pro cracked a 6-file sympy bug the cheap tier could
NOT (capability is a real lever), and django-11138 only resolved across multiple fix attempts
(more-attempts is a real lever). So the cost-smart policy is: try the cheap model first, and
spend the ~3x-pricier strong model only when the cheap one fails. Two composable pieces:

  1. escalation_ladder(n)  — a pure list of model ids for best-of-N's N attempts:
     attempt 0 = cheap (v4-flash), retries = strong (v4-pro). No deps; this is the
     "v4-flash default -> escalate v4-pro on retry" behavior the single agent consumes.

  2. MechanicRouter        — a litellm.Router with cheap + strong deployments and an automatic
     cheap->strong FALLBACK (litellm retries the strong model when the cheap call errors/times
     out), behind one provider-agnostic completion() call. litellm is imported LAZILY so
     `import router` (hence the escalation ladder) never requires litellm to be installed.

Cost anchors (per agent run on the SWE-bench subset): v4-flash ~= $0.03, v4-pro ~= $0.07.

Usage:
  python router.py                       # print the escalation ladder
  python router.py --demo                # one cheap completion through the litellm router (needs key)
"""
from __future__ import annotations

import argparse
import os

# logical tier -> concrete model id. make_client()/litellm both route "deepseek*" to DeepSeek.
TIERS = {"cheap": "deepseek-v4-flash", "strong": "deepseek-v4-pro"}
# best-of-N tiering: first attempt cheap, every retry escalates to strong.
DEFAULT_LADDER = ["cheap", "strong"]


def escalation_ladder(n: int, ladder: list[str] | None = None, tiers: dict | None = None) -> list[str]:
    """Concrete model id for each of n best-of-N attempts. `ladder` is a list of tier names;
    once exhausted its last tier repeats (so the default ['cheap','strong'] => cheap, then strong
    for all retries). Unknown tier names are treated as literal model ids (lets callers pass
    explicit models)."""
    ladder = ladder or DEFAULT_LADDER
    tiers = tiers or TIERS
    out = []
    for i in range(max(0, n)):
        tier = ladder[i] if i < len(ladder) else ladder[-1]
        out.append(tiers.get(tier, tier))
    return out


def estimate_cost(models: list[str], per_run: dict | None = None) -> float:
    """Rough $ estimate for a sequence of attempt-models (cost anchors are ~per-run averages)."""
    per_run = per_run or {"deepseek-v4-flash": 0.03, "deepseek-v4-pro": 0.07}
    return round(sum(per_run.get(m, 0.05) for m in models), 4)


# --------------------------------------------------------------------------- #
# litellm.Router — unified completion + automatic cheap->strong fallback.
# Imported lazily so the escalation ladder above works with zero extra deps.
# --------------------------------------------------------------------------- #
class MechanicRouter:
    """Thin wrapper over litellm.Router. Deployments 'cheap'/'strong' map to the DeepSeek tiers;
    a cheap-call failure automatically falls back to strong. completion() is OpenAI-shaped."""

    def __init__(self, tiers: dict | None = None, num_retries: int = 2, timeout: int = 600):
        try:
            from litellm import Router
        except ImportError as e:  # pragma: no cover - exercised only without litellm installed
            raise RuntimeError("litellm not installed. `uv pip install --python .venv/bin/python litellm`") from e
        if not os.environ.get("DEEPSEEK_API_KEY"):
            raise RuntimeError("DEEPSEEK_API_KEY not set (load_env() or export it).")
        self.tiers = tiers or TIERS
        model_list = [
            {"model_name": name,
             # litellm addresses DeepSeek as "deepseek/<model>" and reads DEEPSEEK_API_KEY.
             "litellm_params": {"model": f"deepseek/{self.tiers[name]}", "timeout": timeout}}
            for name in ("cheap", "strong")
        ]
        self.router = Router(
            model_list=model_list,
            fallbacks=[{"cheap": ["strong"]}],   # escalate cheap -> strong on error/timeout
            num_retries=num_retries,
        )

    def completion(self, messages, tools=None, tier: str = "cheap", temperature: float = 0.0):
        kwargs = {"model": tier, "messages": messages, "temperature": temperature}
        if tools:
            kwargs.update(tools=tools, tool_choice="auto")
        return self.router.completion(**kwargs)


def main() -> None:
    ap = argparse.ArgumentParser(description="Mechanic model router.")
    ap.add_argument("-n", type=int, default=3, help="best-of-N attempt count to show the ladder for")
    ap.add_argument("--ladder", nargs="*", help="tier names, e.g. cheap cheap strong")
    ap.add_argument("--demo", action="store_true", help="one cheap completion through the litellm router")
    args = ap.parse_args()

    models = escalation_ladder(args.n, ladder=args.ladder)
    print(f"escalation ladder (n={args.n}, ladder={args.ladder or DEFAULT_LADDER}):")
    for i, m in enumerate(models):
        print(f"  attempt {i}: {m}{'   <- default' if i == 0 else '   <- escalate'}")
    print(f"  est. cost if all attempts run: ${estimate_cost(models)}")

    if args.demo:
        try:
            from solve import load_env
            load_env()
        except Exception:
            pass
        r = MechanicRouter()
        resp = r.completion([{"role": "user", "content": "Reply with exactly: PONG"}], tier="cheap")
        print("router demo (cheap tier):", repr(resp.choices[0].message.content))


if __name__ == "__main__":
    main()
