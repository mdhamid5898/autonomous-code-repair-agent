"""Router escalation-ladder policy — pure, no litellm/API needed."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import router  # noqa: E402  (must import even when litellm is absent)


def test_default_ladder_cheap_then_strong():
    assert router.escalation_ladder(1) == ["deepseek-v4-flash"]
    assert router.escalation_ladder(2) == ["deepseek-v4-flash", "deepseek-v4-pro"]
    # last tier repeats: attempt 0 cheap, every retry strong
    assert router.escalation_ladder(3) == ["deepseek-v4-flash", "deepseek-v4-pro", "deepseek-v4-pro"]


def test_custom_ladder():
    assert router.escalation_ladder(3, ladder=["cheap", "cheap", "strong"]) == [
        "deepseek-v4-flash", "deepseek-v4-flash", "deepseek-v4-pro"]


def test_unknown_tier_is_literal_model_id():
    assert router.escalation_ladder(1, ladder=["gpt-4o"]) == ["gpt-4o"]


def test_zero_attempts():
    assert router.escalation_ladder(0) == []


def test_cost_estimate():
    assert router.estimate_cost(["deepseek-v4-flash", "deepseek-v4-pro"]) == 0.10
