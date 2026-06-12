# yarl-984.py — trailing slash dropped when dividing URL by empty string
# Bug: yarl.URL("http://localhost") / "path" / "" should produce a URL whose
# string representation ends with "/" (i.e. "http://localhost/path/"), but the
# empty-string segment is silently dropped, yielding "http://localhost/path".
import yarl


def test_url_div_empty_string_preserves_trailing_slash():
    url = yarl.URL("http://localhost") / "path" / ""
    result = str(url)
    assert result == "http://localhost/path/", (
        "Bug: URL / 'path' / '' dropped the trailing slash. "
        "Got %r, expected 'http://localhost/path/'" % result
    )


def test_url_div_preserves_trailing_slash_explicit():
    base = yarl.URL("http://example.com")
    url = base / "api" / "v1" / ""
    assert str(url).endswith("/"), (
        "Bug: dividing by '' did not add trailing slash. Got %r" % str(url)
    )
