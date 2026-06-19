# jinja2-1921.py — namespace tuple assignment raises TemplateSyntaxError
#
# Bug: in a Jinja2 {% set %} statement, tuple assignment to namespace
# attributes (e.g. "{% set ns.x, ns.y = 1, 2 %}") fails because the
# parser's namespace-dotted-reference path was a special early-exit that
# skipped the general tuple-parsing logic entirely.
#
# Fix: parser.py is refactored so namespace tokens fall through to the
# regular tuple / primary-expression handling; compiler.py is updated so
# the namespace isinstance guard is emitted once at the Assign level
# instead of once per NSRef visit, removing the duplication that caused
# double-emission when multiple NSRefs appeared in a single assignment.
import pytest
from jinja2 import Environment, TemplateSyntaxError


def test_namespace_tuple_assignment_renders_correctly():
    """{% set ns.x, ns.y = 1, 2 %} must assign both values simultaneously.

    Before the fix: the template raises TemplateSyntaxError because the
    parser's with_namespace branch exits before it can see the comma that
    starts the tuple, so it never builds a Tuple node. The error looks like
    'unexpected ","' or similar.

    After the fix: both namespace attributes receive their values and the
    template renders '1,2'.
    """
    env = Environment()

    try:
        tmpl = env.from_string(
            "{% set ns = namespace(x=0, y=0) %}"
            "{% set ns.x, ns.y = 1, 2 %}"
            "{{ ns.x }},{{ ns.y }}"
        )
        result = tmpl.render()
    except TemplateSyntaxError as exc:
        pytest.fail(
            f"Bug: '% set ns.x, ns.y = 1, 2 %' raised TemplateSyntaxError: {exc}"
        )

    assert result == "1,2", (
        f"Bug: namespace tuple assignment produced wrong output. "
        f"Got {result!r}, expected '1,2'."
    )


def test_namespace_single_assignment_unaffected():
    """Regular single-attr namespace assignment must still work after the fix."""
    env = Environment()
    tmpl = env.from_string(
        "{% set ns = namespace(x=0) %}{% set ns.x = 42 %}{{ ns.x }}"
    )
    assert tmpl.render() == "42"
