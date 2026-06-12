# arrow-1240.py — humanize() says "a month" for a 16-day difference
# Bug: Arrow's granularity thresholds treat any delta >= 45 days as "a month",
# but 16 days should humanize to "2 weeks" (14–27 days range for weeks).
import arrow


def test_humanize_16_days_is_two_weeks():
    now = arrow.Arrow(2023, 1, 1, tzinfo="UTC")
    then = arrow.Arrow(2023, 1, 17, tzinfo="UTC")  # 16 days later

    result = now.humanize(then)
    assert "week" in result, (
        "Bug: arrow.humanize() for a 16-day difference returned %r; "
        "expected something containing 'week' (e.g. '2 weeks ago'), "
        "not 'a month ago'" % result
    )
