"""Repro for inveniosoftware/dictdiffer issue #182.

Deleting the first item of a list must be reported as a single 'remove'
operation, NOT as a cascade of 'change' operations (one per shifted index)
followed by a trailing 'remove' of the last index.

Buggy (pre-fix) output for {'targ1': ['one','two','three']} ->
{'targ1': ['two','three']}:

    [('change', ['targ1', 0], ('one', 'two')),
     ('change', ['targ1', 1], ('two', 'three')),
     ('remove', 'targ1', [(2, 'three')])]

Correct (post-fix) behavior: no spurious 'change' ops; the single head
deletion is captured purely as removal(s).
"""

from dictdiffer import diff, patch


def test_list_head_delete_is_single_remove_not_change_cascade():
    first = {'targ1': ['one', 'two', 'three']}
    second = {'targ1': ['two', 'three']}

    result = list(diff(first, second))

    actions = [item[0] for item in result]

    # The only difference between first and second is that the head element
    # 'one' was removed. The diff must therefore contain no 'change' ops --
    # the buggy code emits ('change', ..., ('one','two')) and
    # ('change', ..., ('two','three')) because of off-by-one list alignment.
    assert 'change' not in actions, (
        "head-of-list deletion was mis-reported as 'change' operations: "
        "%r" % (result,)
    )

    # There must be at least one removal describing the deleted item.
    assert 'remove' in actions, (
        "expected a 'remove' op for the deleted head item, got: %r" % (result,)
    )

    # Sanity invariant: applying the diff to `first` must reconstruct `second`.
    assert patch(result, first) == second
