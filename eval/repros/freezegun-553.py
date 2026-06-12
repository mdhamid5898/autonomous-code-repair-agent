"""Repro for freezegun issue #553:

tz_offset incorrectly shifts tz-aware datetime.now(tz=...).

When freeze_time is used with a tz_offset, calling datetime.now(tz=...) returns
a timestamp shifted by tz_offset relative to a naive datetime.now(). Since both
refer to the same frozen instant, their .timestamp() values must be identical.
The tz_offset must only affect naive/local time, not the absolute instant of a
tz-aware now().
"""
from datetime import datetime, timezone, timedelta

from freezegun import freeze_time


def test_tz_offset_does_not_shift_tzaware_now_timestamp():
    with freeze_time("2022-08-09 11:26:00.000", tz_offset=-9):
        naive_ts = datetime.now().timestamp()
        aware_ts = datetime.now(tz=timezone(timedelta(hours=0))).timestamp()

    # Both refer to the exact same frozen instant; tz_offset must not shift the
    # absolute timestamp of a timezone-aware now().
    assert aware_ts == naive_ts
