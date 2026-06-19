# flask-5898.py — flask.redirect() defaults to 302, should default to 303
#
# Bug: Both flask.redirect() in helpers.py and App.redirect() in sansio/app.py
# default to status code 302 (Found). RFC 9110 recommends 303 (See Other)
# after a POST request so that a browser reload doesn't re-submit the form.
# The correct default is 303, not 302.
#
# Fix: change the default `code` parameter from 302 to 303 in both
# helpers.py::redirect() and sansio/app.py::App.redirect().
import pytest
import flask


def test_redirect_defaults_to_303():
    """flask.redirect(location) must use status 303 by default.

    Before fix: flask.redirect('/target').status_code == 302
    After fix:  flask.redirect('/target').status_code == 303
    """
    app = flask.Flask(__name__)
    with app.app_context():
        response = flask.redirect("/target")

    assert response.status_code == 303, (
        f"Bug: flask.redirect() defaults to status {response.status_code}, "
        "expected 303 (See Other). The default code in helpers.py (and "
        "sansio/app.py) is still 302 (Found)."
    )


def test_app_redirect_defaults_to_303():
    """app.redirect() must also default to 303 (sansio/app.py path)."""
    app = flask.Flask(__name__)
    with app.app_context():
        response = flask.current_app.redirect("/target")

    assert response.status_code == 303, (
        f"Bug: app.redirect() defaults to status {response.status_code}, "
        "expected 303 (See Other). The default in sansio/app.py::App.redirect() "
        "is still 302."
    )
