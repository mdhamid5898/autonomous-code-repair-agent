import datetime
import json

import jsonpickle


def test_timedelta_unpicklable_false_emits_usable_value():
    # https://github.com/jsonpickle/jsonpickle/issues/444
    # Under unpicklable=False, datetime.timedelta must encode to a usable
    # (lossy) value derived from its __reduce__ args -- NOT null/None.
    # timedelta(days=3).__reduce_ex__(2) == (timedelta, (3, 0, 0)), so with
    # the type metadata stripped the expected output is the args [3, 0, 0].
    td = datetime.timedelta(days=3)

    encoded = jsonpickle.encode(td, unpicklable=False)

    decoded = json.loads(encoded)
    assert decoded is not None, (
        "timedelta encoded to null under unpicklable=False (issue #444)"
    )
    assert decoded == [3, 0, 0]
