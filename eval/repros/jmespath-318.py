# jmespath-318.py — literal list/object expressions share state across calls via AST cache
# Bug: jmespath compiles and caches the AST for an expression. A literal value node
# (e.g. `[]`) stores the actual Python object in the AST. When two calls use the same
# compiled expression, they get back references to the *same* list object, so mutations
# by one caller silently corrupt subsequent callers' results.
import jmespath


def test_literal_list_not_shared_across_calls():
    expr = "`[]`"

    result1 = jmespath.search(expr, {})
    result1.extend([1, 2, 3])

    result2 = jmespath.search(expr, {})
    # Bug: result2 is [1, 2, 3] because it is the same object as result1
    assert result2 == [], (
        "Bug: jmespath literal `[]` shares state across calls. "
        "After mutating result1, result2=%r (expected [])" % result2
    )


def test_literal_object_not_shared_across_calls():
    expr = '`{"key": "value"}`'

    result1 = jmespath.search(expr, {})
    result1["extra"] = "injected"

    result2 = jmespath.search(expr, {})
    assert "extra" not in result2, (
        "Bug: jmespath literal object shares state across calls. "
        "After mutating result1, result2=%r" % result2
    )
