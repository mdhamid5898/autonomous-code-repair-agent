# flask-5928.py — teardown callbacks silently skipped when one raises
#
# Bug: do_teardown_appcontext (and do_teardown_request) iterate the registered
# callbacks with a bare call — no try/except.  The first callback that raises
# aborts the loop; all subsequent callbacks are never invoked.
#
# Fix: introduce _CollectErrors (helpers.py) and wrap every call in both
# app.py (do_teardown_request / do_teardown_appcontext) and ctx.py (pop()) so
# every callback runs regardless of errors; exceptions are collected and
# re-raised together at the end.
import pytest
import flask


def test_all_teardown_appcontext_called_despite_errors():
    """Register two teardown_appcontext callbacks that both raise.
    After the fix ALL callbacks must be called (count == 2).
    Before the fix the loop stops at the first error → count == 1.
    Flask calls them in reversed-registration order, so 'second' runs first.
    """
    app = flask.Flask(__name__)
    count = 0

    @app.teardown_appcontext
    def first(exc):
        nonlocal count
        count += 1
        raise ValueError("error in first teardown")

    @app.teardown_appcontext
    def second(exc):
        nonlocal count
        count += 1
        raise ValueError("error in second teardown")

    # Both callbacks raise, so an exception will escape the context manager.
    try:
        with app.app_context():
            pass
    except Exception:
        pass  # expected — teardown errors propagate out

    assert count == 2, (
        f"Bug: only {count}/2 teardown callbacks were called. "
        "Callbacks stop executing after the first raises."
    )


def test_first_teardown_called_even_when_second_raises():
    """The callback registered FIRST runs LAST (LIFO order).
    Before the fix, if the second (i.e. first-to-run) callback raises,
    the first-registered callback is never reached.
    """
    app = flask.Flask(__name__)
    called = []

    @app.teardown_appcontext
    def first_registered(exc):
        called.append("first_registered")

    @app.teardown_appcontext
    def second_registered(exc):
        called.append("second_registered")
        raise ValueError("second_registered raises")

    # reversed order: second_registered runs first → raises → first_registered skipped (bug)
    try:
        with app.app_context():
            pass
    except Exception:
        pass

    assert "first_registered" in called, (
        f"Bug: 'first_registered' teardown was never called. called={called}"
    )
