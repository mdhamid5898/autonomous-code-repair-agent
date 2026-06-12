"""Repro for python-tabulate issue #428.

disable_numparse=True is ignored when maxcolwidths is set, due to a positional
argument mixup in the _type() call inside _wrap_text_to_colwidths. The string
'80,443' gets number-parsed despite disable_numparse=True, raising
ValueError: invalid literal for int() with base 10: '80,443'.

Post-fix: the call succeeds and the value is rendered literally as a string.
"""

from tabulate import tabulate


def test_disable_numparse_respected_with_maxcolwidths():
    # On the buggy code this raises ValueError before returning.
    result = tabulate(
        [["ports", "str", "comma-separated port list", "80,443"]],
        ["name", "type", "desc", "default"],
        tablefmt="grid",
        disable_numparse=True,
        maxcolwidths=40,
    )

    # Number parsing must be disabled: the raw string survives intact.
    assert "80,443" in result
