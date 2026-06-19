# httpx-3373.py — QueryParams encodes spaces as %20 instead of +
#
# Bug: httpx used a custom urlencode() in _urlparse.py that encoded spaces as
# %20 and '/' as a safe character. The standard application/x-www-form-urlencoded
# encoding (RFC 1866 / HTML5) requires + for spaces, matching how browsers and
# the requests library work.
#
# Fix: remove urlencode() from _urlparse.py and switch _urls.py to use
# urllib.parse.urlencode() (which uses + for spaces), also switching to the
# WHATWG query percent-encode set for other characters.
import pytest
import httpx


def test_query_params_encode_spaces_as_plus():
    """Query parameters must encode spaces as '+', not '%20'.

    Before fix: httpx's custom urlencode uses %20 for spaces.
    After fix: httpx uses urllib.parse.urlencode which uses + for spaces.
    """
    params = httpx.QueryParams({"q": "hello world"})
    encoded = str(params)

    assert "hello+world" in encoded, (
        f"Bug: spaces in query params encoded as '%20' instead of '+'. "
        f"Got: {encoded!r}. Expected 'hello+world' (standard form encoding)."
    )
    assert "hello%20world" not in encoded, (
        f"Bug: httpx is still using %20 for spaces in query params. "
        f"Got: {encoded!r}"
    )


def test_url_with_spaces_in_params_uses_plus():
    """httpx.URL constructed with space-containing params uses + encoding."""
    url = httpx.URL("http://example.com/", params={"search": "foo bar"})
    query = url.query.decode()

    assert "foo+bar" in query, (
        f"Bug: URL query string encodes spaces as %20 instead of +. "
        f"query={query!r}"
    )
