# typeguard-489.py — NameError when @typechecked + Literal annotations + wildcard import
# Bug: when the decorated module uses `from typing import *`, typeguard's @typechecked
# raises NameError at call-time when a Literal type annotation is evaluated, because
# typeguard resolves annotations in a context that doesn't find the wildcard-imported name.
from typing import *  # noqa: F401,F403 — this wildcard import is the trigger
from typeguard import typechecked
import pytest


@typechecked
def _typed_with_literal(x: Literal["hello"] | Literal["world"]) -> str:  # noqa: F821
    return x


def test_typechecked_literal_wildcard_no_nameerror():
    """@typechecked should not raise NameError when Literal comes from a wildcard import."""
    try:
        result = _typed_with_literal("hello")
    except NameError as e:
        pytest.fail(
            "Bug: @typechecked raises NameError with Literal + wildcard import: %s" % e
        )
    assert result == "hello"
