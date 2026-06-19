# werkzeug-3038.py — DuplicateRuleError incorrectly raised for routes with different HTTP methods
#
# Bug: StateMachineMatcher._check_rule uses `rule == existing` to detect
# duplicate routes. Rule.__eq__ only compares _parts (path structure) and
# websocket flag — it does NOT consider the HTTP methods. Two routes with the
# SAME path but DIFFERENT methods (e.g. GET /resource and POST /resource)
# therefore appear identical and raise DuplicateRuleError, even though they
# are perfectly valid co-existing REST routes.
#
# Fix: introduce Rule.is_duplicate(other) in rules.py that adds a method-
# intersection check (excluding HEAD/OPTIONS), and switch matcher.py to use
# it instead of __eq__; also update exceptions.py str representation.
import pytest
from werkzeug.routing import Map, Rule
from werkzeug.routing.exceptions import DuplicateRuleError


def test_get_and_post_same_path_not_duplicate():
    """GET and POST routes to the same path must coexist without error.

    Before the fix: Map([Rule('/r', methods=['GET']), Rule('/r', methods=['POST'])])
    raises DuplicateRuleError because StateMachineMatcher uses Rule.__eq__ which
    only compares _parts (path structure), ignoring HTTP methods entirely.
    After the fix: is_duplicate() checks method overlap and allows the two rules.
    """
    try:
        m = Map([
            Rule("/resource", methods=["GET"], endpoint="get_resource"),
            Rule("/resource", methods=["POST"], endpoint="post_resource"),
        ])
    except DuplicateRuleError as e:
        pytest.fail(
            f"Bug: DuplicateRuleError raised for GET and POST routes to the same path. "
            f"Two routes with different HTTP methods should coexist. Error: {e}"
        )

    # Sanity-check that both routes actually match after the map is built.
    adapter = m.bind("example.com")
    endpoint_get, _ = adapter.match("/resource", method="GET")
    endpoint_post, _ = adapter.match("/resource", method="POST")
    assert endpoint_get == "get_resource"
    assert endpoint_post == "post_resource"


def test_truly_duplicate_routes_still_raise():
    """Routes with identical paths AND identical methods are still duplicates."""
    with pytest.raises(DuplicateRuleError):
        Map([
            Rule("/dup", methods=["GET"], endpoint="dup1"),
            Rule("/dup", methods=["GET"], endpoint="dup2"),
        ])
