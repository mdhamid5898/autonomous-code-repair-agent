"""Repro for python-sortedcontainers issue #235.

When a SortedDict (or SortedKeyList) is built with an order-inverting key
function such as ``operator.neg``, ``irange(minimum=, maximum=)`` does not
account for the inverted ordering. Internally it computes
``min_key = key(minimum)`` and ``max_key = key(maximum)`` without swapping, so
for an inverting key ``min_key`` ends up greater than ``max_key`` and the
range comes out empty. Users are forced to swap minimum/maximum to get values.

Post-fix: ``irange(minimum=2, maximum=3)`` should yield exactly the values
whose (real) value lies in the closed interval [2, 3], regardless of the key
function. With keys 1.5, 2.5, 3, 4 and key=neg the stored order is
[4, 3, 2.5, 1.5], so the values in [2, 3] are emitted as [3, 2.5].
"""

from operator import neg

from sortedcontainers import SortedDict


def test_irange_respects_inverting_key():
    d = SortedDict(neg)
    d[1.5] = 5
    d[2.5] = 4
    d[3] = 3
    d[4] = 2

    # Sanity: neg key reverses the stored order of keys.
    assert list(d) == [4, 3, 2.5, 1.5]

    # The natural request: every key v with 2 <= v <= 3.
    # Those keys are 2.5 and 3; in the (reversed) iteration order they come
    # out as [3, 2.5].
    result = list(d.irange(minimum=2, maximum=3))

    # On the buggy code this returns [] because minimum/maximum are not
    # swapped for the inverting key.
    assert result == [3, 2.5]
