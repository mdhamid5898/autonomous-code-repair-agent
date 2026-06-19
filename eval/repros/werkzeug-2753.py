# werkzeug-2753.py — parse_accept_header drops q-values that are plain 0 or 1
#
# Bug: RFC 9110 says the decimal point in a q-value is optional, so both
# "q=1" and "q=1.0" are equivalent, as are "q=0" and "q=0.0". Werkzeug's
# internal regex for q-values required the decimal point, so "en;q=1" and
# "en;q=0" were silently dropped from the parsed result entirely.
#
# Fix: update the q-value regex in _internal.py to accept plain integers (no
# decimal point) and update parse_accept_header in http.py to use it.
import pytest
from werkzeug.http import parse_accept_header


def test_accept_header_q_value_plain_1():
    """'en;q=1' must be parsed; q=1 is a valid plain integer per RFC 9110."""
    result = parse_accept_header("en;q=1")
    values = list(result.values())

    assert "en" in values, (
        f"Bug: parse_accept_header('en;q=1') returned empty list. "
        f"Got: {values!r}. RFC 9110 says q=1 (no decimal point) is valid."
    )


def test_accept_header_q_value_plain_0():
    """'en;q=0' means the client explicitly rejects 'en'; it must still appear
    in the parsed header (with quality 0) so callers can see the rejection.
    """
    result = parse_accept_header("text/html,application/json;q=0")
    values = list(result.values())

    assert "application/json" in values, (
        f"Bug: parse_accept_header dropped 'application/json;q=0'. "
        f"Got: {values!r}. A zero q-value is still a valid directive."
    )


def test_accept_header_with_decimal_still_works():
    """q-values WITH decimal points must continue to be parsed correctly."""
    result = parse_accept_header("en;q=0.9,fr;q=0.8")
    values = list(result.values())

    assert "en" in values and "fr" in values, (
        f"Bug: decimal q-values also broken. Got: {values!r}"
    )
