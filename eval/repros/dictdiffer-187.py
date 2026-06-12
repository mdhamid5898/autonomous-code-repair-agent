"""Repro for inveniosoftware/dictdiffer#187.

`ignore={'b.bb'}` is honored on a `change` between two dicts, but is NOT
consulted when a nested dict is removed/replaced (the change-from-dict path).
The ignored key `b.bb` must be stripped from the reported old value.
"""

from dictdiffer import diff


def test_ignore_honored_on_remove_path():
    a = dict(a="a", b=dict(bb="bb", cc="cc"))
    c = dict(a="A", b=None)

    result = list(diff(a, c, ignore={'b.bb'}))

    # Post-fix expected behavior straight from the issue body: the ignored
    # nested key 'b.bb' must not appear in the diff output even though the
    # whole 'b' subtree is being replaced (dict -> None).
    expected = [
        ('change', 'a', ('a', 'A')),
        ('change', 'b', ({'cc': 'cc'}, None)),
    ]
    assert result == expected
