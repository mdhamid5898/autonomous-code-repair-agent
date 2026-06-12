# boltons-393.py — research() skips branches that share an identical (interned) object
# Bug: research() uses boltons.iterutils.remap internally with a cache keyed on
# object identity (id()). CPython interns small tuples, so two dictionary values
# that are equal *and* identical (same id()) cause remap to skip the second branch
# entirely — research() never visits it and never yields its paths.
from boltons.iterutils import research


def test_research_finds_all_branches_with_identical_tuple_values():
    # CPython interns small string tuples: both values are the *same* object.
    tree = {
        "branch_a": ("hello",),
        "branch_b": ("hello",),  # identical object to branch_a's value on CPython
    }
    # Confirm the interning (this is the precondition for the bug)
    assert id(tree["branch_a"]) == id(tree["branch_b"]), (
        "Precondition failed: tuples are not interned — bug may not manifest"
    )

    results = list(research(tree))
    paths = [path for path, val in results]

    branch_a_found = any("branch_a" in str(p) for p in paths)
    branch_b_found = any("branch_b" in str(p) for p in paths)

    assert branch_a_found, "branch_a not found in research results: %r" % paths
    assert branch_b_found, (
        "Bug: research() skipped branch_b because its value is object-identical to "
        "branch_a's value (CPython tuple interning). paths=%r" % paths
    )
