# parse-223.py — microsecond precision loss in timestamp parsing
# Bug: the date_convert helper computes microseconds as int(float("." + u) * 1e6),
# which loses precision for most 6-digit microsecond values due to floating-point
# rounding (e.g. "501902" → float("0.501902") * 1e6 = 501901.999... → 501901).
import parse


def test_parse_microsecond_precision():
    # 0.501902 * 1e6 rounds down to 501901 with float arithmetic
    r = parse.parse("{:ti}", "2023-10-14T15:09:08.501902Z")
    assert r is not None, "parse() returned None — format or test string may be wrong"
    assert r[0].microsecond == 501902, (
        "Bug: microsecond precision loss: got %d, expected 501902. "
        "Cause: int(float('.501902') * 1e6) = %d" % (
            r[0].microsecond,
            int(float(".501902") * 1_000_000),
        )
    )


def test_parse_microsecond_short_fraction():
    # 3-digit fraction should be zero-padded to 6 digits: .562 → 562000
    r = parse.parse("{:ti}", "2023-10-14T15:09:08.562Z")
    assert r is not None
    assert r[0].microsecond == 562000, (
        "Expected 562000 microseconds, got %d" % r[0].microsecond
    )
