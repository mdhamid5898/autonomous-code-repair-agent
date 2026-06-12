# arrow-1185.py — span_range() with exact=True and frame='month' creates multi-day gaps
# Bug: when the start date falls on a day-of-month that doesn't exist in a later month
# (e.g. Jan 31 → Feb has no 31st), span_range skips ahead, leaving uncovered days.
import arrow


def test_span_range_month_exact_no_gaps():
    start = arrow.get("2023-01-31T00:00:00+00:00")
    end = arrow.get("2023-04-30T23:59:59+00:00")

    spans = list(arrow.Arrow.span_range("month", start.datetime, end.datetime, exact=True))
    assert len(spans) >= 2, "Expected at least 2 monthly spans"

    for i in range(len(spans) - 1):
        span_end = spans[i][1]
        next_start = spans[i + 1][0]
        # Gap in seconds between consecutive spans
        gap_seconds = (next_start - span_end).total_seconds()
        assert gap_seconds <= 1.0, (
            "Bug: span_range(exact=True) leaves a gap of %.1f seconds (%.1f days) "
            "between span %d (%s) and span %d (%s)" % (
                gap_seconds, gap_seconds / 86400,
                i, span_end.isoformat(),
                i + 1, next_start.isoformat(),
            )
        )
