"""Repro for gweis/isodate issue #44.

parse_duration("P3M") returns a Duration whose month component cannot be
mapped to a fixed number of seconds. Calling total_seconds() on it silently
delegates to an empty timedelta and returns 0.0, hiding the 3 months entirely.

Correct post-fix behavior: months/years cannot be converted to a fixed number
of seconds without a reference date, so total_seconds() must NOT silently
return 0.0 -- it should raise (ValueError / ISO8601Error, the latter being a
ValueError subclass).
"""

import pytest

import isodate


def test_total_seconds_on_month_duration_does_not_silently_return_zero():
    duration = isodate.parse_duration("P3M")

    # Sanity: the parsed value really does carry 3 months.
    assert duration.months == 3

    # The bug: total_seconds() returns 0.0, silently dropping the months.
    # Fixed behavior: refuse to convert an ambiguous month/year duration to
    # seconds by raising instead of returning a misleading 0.0.
    with pytest.raises(ValueError):
        duration.total_seconds()
