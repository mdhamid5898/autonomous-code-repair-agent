import furl

def test_furl_parses_userinfo_with_at_sign():
    f = furl.furl('http://user:pass@word@host.com/path')
    assert f.host == 'host.com'
    assert f.username == 'user'
    assert f.password == 'pass@word'
