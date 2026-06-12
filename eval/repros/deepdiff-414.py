# deepdiff-414.py — iterable_compare_func misses value changes when items are moved
# Bug: DeepDiff returns {} (empty) when iterable_compare_func is used and items are
# reordered AND have value changes. Only "iterable_item_moved" appears at verbose_level=2,
# but actual value diffs within moved items are silently dropped.
from deepdiff import DeepDiff
from deepdiff.helper import CannotCompare


def test_iterable_compare_func_reports_value_changes_when_moved():
    t1 = [
        {"id": 1, "value": [1]},
        {"id": 2, "value": [7, 8, 1]},   # id=2 has extra element
        {"id": 3, "value": [7, 8]},
    ]
    t2 = [
        {"id": 2, "value": [7, 8]},       # id=2 moved to index 0, value shrunk
        {"id": 3, "value": [7, 8, 1]},    # id=3 moved to index 1, value grew
        {"id": 1, "value": [1]},
    ]

    def compare_func(x, y, level=None):
        try:
            return x["id"] == y["id"]
        except Exception:
            raise CannotCompare() from None

    diff = DeepDiff(t1, t2, iterable_compare_func=compare_func)
    # Bug: diff is {} — value changes inside moved items are not reported.
    # Expected: reports iterable_item_added/removed for the inner [value] lists.
    assert diff != {}, (
        "Bug: DeepDiff with iterable_compare_func returned empty diff; "
        "value changes inside moved items are dropped. diff=%r" % diff
    )
    # The inner list of id=2 shrank (item 1 removed), id=3 grew (item 1 added).
    assert "iterable_item_removed" in diff or "iterable_item_added" in diff, (
        "Expected iterable_item_added/removed in diff, got: %r" % diff
    )
