"""Repro for freezegun issue #348.

datetime.now(tz=...) must not double-apply tz_offset. With tz_offset set,
datetime.now(timezone.utc) should represent the SAME UTC instant as
datetime.utcnow() -- just made timezone-aware -- not a value shifted by the
tz_offset a second time.
"""
import datetime

from freezegun import freeze_time


def test_now_with_tz_does_not_double_apply_tz_offset():
    frozen = datetime.datetime(2020, 5, 1, 14, 59, 53)

    with freeze_time(frozen, tz_offset=1):
        now_utc = datetime.datetime.now(datetime.timezone.utc)
        utcnow = datetime.datetime.utcnow()

    # now(utc) must be timezone-aware UTC.
    assert now_utc.tzinfo is not None
    assert now_utc.utcoffset() == datetime.timedelta(0)

    # The UTC instant from now(utc) must equal utcnow() -- no double shift.
    assert now_utc.replace(tzinfo=None) == utcnow

    # Concretely: the frozen UTC time is preserved, not bumped by tz_offset.
    assert now_utc == datetime.datetime(
        2020, 5, 1, 14, 59, 53, tzinfo=datetime.timezone.utc
    )
