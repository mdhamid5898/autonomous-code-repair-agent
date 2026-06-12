# humanize-174.py — naturaldelta() rounds down instead of to nearest unit
# Bug: humanize.naturaldelta(timedelta(seconds=90)) returns "a minute" instead of
# "2 minutes".  The threshold for "a minute" should be < 90 s, but the buggy
# implementation uses floor division so 89 s == 1 min → "a minute" even at 90 s.
import datetime
import humanize


def test_naturaldelta_rounds_to_nearest_minutes():
    # 90 seconds is halfway between 1 and 2 minutes → should round to "2 minutes"
    delta = datetime.timedelta(seconds=90)
    result = humanize.naturaldelta(delta)
    assert result != "a minute", (
        "Bug: humanize.naturaldelta(90s) returned %r; "
        "expected '2 minutes' (rounds to nearest, not down)" % result
    )
    assert "2" in result and "minute" in result, (
        "Expected '2 minutes', got %r" % result
    )


def test_naturaldelta_89_seconds_is_a_minute():
    # 89 seconds < 90 s threshold → still "a minute"
    delta = datetime.timedelta(seconds=89)
    result = humanize.naturaldelta(delta)
    assert "minute" in result, (
        "Expected a minute for 89s, got %r" % result
    )
