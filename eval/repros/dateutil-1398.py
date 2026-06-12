# dateutil-1398.py — rrulestr FREQ=WEEKLY with WKST + BYSETPOS + out-of-order BYDAY
# Bug: when BYDAY lists days in non-natural calendar order and WKST is set,
# BYSETPOS indexes into the wrong ordering of days, producing incorrect dates.
# With WKST=WE (week starts Wednesday), BYDAY=MO,TU,WE, BYSETPOS=1 should
# select the FIRST day of each week (Wednesday), but instead returns Monday
# (which belongs to the PREVIOUS week under WKST=WE).
import datetime
from dateutil.rrule import rrulestr


def test_rrulestr_bysetpos_with_wkst_and_nonstandard_byday():
    rule_str = (
        "DTSTART:2024-11-10\n"
        "RRULE:FREQ=WEEKLY;WKST=WE;BYSETPOS=1;BYDAY=MO,TU,WE;COUNT=3"
    )
    dates = [d.strftime("%a %d %b %Y") for d in rrulestr(rule_str)]

    # With WKST=WE, the week starting 2024-11-13 (Wed) contains WE=Nov13, MO=Nov18(?).
    # Actually: week [Wed Nov 13 .. Tue Nov 19]. Days present: WE(13), MO(18), TU(19).
    # BYSETPOS=1 → first in week order = WED Nov 13.
    # Bug: first result is "Mon 11 Nov 2024" (previous week's Monday, wrong week boundary)
    # Expected: first result is "Wed 13 Nov 2024"
    assert dates[0] == "Wed 13 Nov 2024", (
        "Bug: BYSETPOS=1 with WKST=WE+BYDAY=MO,TU,WE returned %r as first date; "
        "expected 'Wed 13 Nov 2024' (first day of week starting Wednesday)" % dates[0]
    )
    assert dates == ["Wed 13 Nov 2024", "Wed 20 Nov 2024", "Wed 27 Nov 2024"], (
        "Bug: full result %r does not match expected weekly Wednesdays" % dates
    )
