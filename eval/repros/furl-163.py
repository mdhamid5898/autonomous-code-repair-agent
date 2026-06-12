# furl #163 — netloc with multiple '@' must split on the LAST '@', not the first.
# Bug: furl/furl.py netloc setter uses split('@', 1); should be rsplit('@', 1).
from furl import furl


def test_netloc_splits_on_last_at_sign():
    f = furl("http://user:pass@word@host.com/path")
    # buggy split('@',1) -> host == 'word@host.com'; correct is 'host.com'
    assert f.host == "host.com"
    assert f.username == "user"
    assert f.password == "pass@word"
