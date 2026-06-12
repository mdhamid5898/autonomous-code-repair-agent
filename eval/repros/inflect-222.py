# inflect-222.py — p.singular_noun("pair of scissors") raises TypeError
# Bug: singular_noun() calls a regex match method and passes the result directly
# into subsequent string operations without checking for None, causing a TypeError
# when the word is a "pair of X" phrase where X is an irregular plural noun.
import pytest
import inflect


def test_singular_noun_pair_of_scissors():
    p = inflect.engine()
    try:
        result = p.singular_noun("pair of scissors")
    except TypeError as e:
        pytest.fail(
            "Bug: p.singular_noun('pair of scissors') raised TypeError: %s" % e
        )
    # The singular of "pair of scissors" is "pair of scissor" or False
    # (some inflectors return False for already-singular words).
    # The important thing is no TypeError is raised.
    assert result is not None or result is False, (
        "Unexpected return value: %r" % result
    )


def test_singular_noun_pair_of_trousers():
    p = inflect.engine()
    try:
        result = p.singular_noun("pair of trousers")
    except TypeError as e:
        pytest.fail(
            "Bug: p.singular_noun('pair of trousers') raised TypeError: %s" % e
        )
