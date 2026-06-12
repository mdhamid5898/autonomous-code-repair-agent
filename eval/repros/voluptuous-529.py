# voluptuous-529.py — REMOVE_EXTRA + Required(Any(...)) raises error on extra keys
# Bug: when a schema uses Required(Any("@id", "id")) as a KEY (not a value validator)
# together with extra=REMOVE_EXTRA, extra keys that should be silently dropped instead
# trigger a MultipleInvalid validation error.
import pytest
from voluptuous import REMOVE_EXTRA, Any, Required, Schema


def test_remove_extra_with_required_any_key():
    schema = Schema(
        {Required(Any("@id", "id")): str},
        extra=REMOVE_EXTRA,
    )

    doc = {"@id": "foo", "@version": "1.0"}
    # Bug: raises MultipleInvalid: not a valid value @ data['@version']
    # Expected: extra key '@version' is silently removed; result is {'@id': 'foo'}
    try:
        result = schema(doc)
    except Exception as e:
        pytest.fail(
            "Bug: REMOVE_EXTRA + Required(Any(...)) raised %s: %s; "
            "extra key '@version' should have been removed silently" % (type(e).__name__, e)
        )
    assert "@id" in result, "Expected '@id' key in result, got: %r" % result
    assert "@version" not in result, "Expected '@version' to be removed, got: %r" % result
