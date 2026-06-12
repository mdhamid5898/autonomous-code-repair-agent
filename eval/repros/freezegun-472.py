import datetime

from freezegun import freeze_time


@freeze_time("2000-01-01T00:00:00.000Z", tz_offset=3)
def test_astimezone_respects_tz_offset():
    # Under tz_offset=3, now(tz=utc) reflects the frozen time shifted by the
    # configured offset: 00:00 UTC + 3h offset reported in UTC -> 03:00+00:00.
    now = datetime.datetime.now(tz=datetime.timezone.utc)
    assert now.isoformat() == "2000-01-01T03:00:00+00:00"

    # Calling astimezone() with no argument must convert into freezegun's
    # configured local timezone (tz_offset=3), NOT the system's tzlocal().
    # The instant is unchanged, so the wall-clock time becomes 06:00 with a
    # +03:00 offset. The buggy code uses tzlocal() and ignores tz_offset.
    converted = now.astimezone()

    assert converted.utcoffset() == datetime.timedelta(hours=3)
    assert converted.isoformat() == "2000-01-01T06:00:00+03:00"
