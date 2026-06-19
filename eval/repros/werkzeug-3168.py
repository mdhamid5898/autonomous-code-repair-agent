# werkzeug-3168.py — parse_age (and similar) accept non-plain integers like "+1"
#
# Bug: Several Werkzeug HTTP parsing functions use the built-in int() to convert
# integer-looking strings. Python's int() accepts a broader set of inputs than
# a "plain integer" in HTTP headers: "+1", "-1", "0x1", "1_000" are all valid
# Python integer literals but are NOT valid plain HTTP integers.
#
# Affected functions (http.py, serving.py, middleware/shared_data.py,
# debug/__init__.py all use int() directly; the shared helper _plain_int()
# already existed in _internal.py but was not used in these places).
#
# Fix: replace int(value) with _plain_int(value) in the five files listed
# in fix_files, so that e.g. parse_age("+1") returns None instead of
# timedelta(seconds=1).
import pytest
from werkzeug.http import parse_age


def test_parse_age_rejects_plus_prefix():
    """'+1' is not a valid HTTP Age header value.

    Before fix: parse_age('+1') returns timedelta(seconds=1) because int('+1')
    succeeds in Python.
    After fix: _plain_int('+1') raises ValueError, so parse_age returns None.
    """
    result = parse_age("+1")
    assert result is None, (
        f"Bug: parse_age('+1') returned {result!r} instead of None. "
        "Python's int() accepts '+1' but HTTP plain integers must not have a sign."
    )


def test_parse_age_rejects_minus_prefix():
    """-1 is handled by the 'seconds < 0' guard, but that requires int() to succeed.

    The real question is that '+1' should be caught BEFORE the negative-value
    guard. After the fix, _plain_int() rejects '+' prefix strings.
    """
    # '+1' and '1_000' should both be rejected (non-plain integers).
    for bad in ("+1", "1_000"):
        result = parse_age(bad)
        assert result is None, (
            f"Bug: parse_age({bad!r}) returned {result!r} instead of None. "
            "Non-plain integer strings must be rejected."
        )


def test_parse_age_still_accepts_plain_integers():
    """Valid plain integer Age values must still be parsed correctly."""
    from datetime import timedelta

    assert parse_age("0") == timedelta(0)
    assert parse_age("3600") == timedelta(seconds=3600)
    assert parse_age(None) is None
    assert parse_age("") is None
