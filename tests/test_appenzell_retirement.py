from webapp import app


def test_default_local_bypass_hosts_have_no_appenzell_dependency():
    assert app.LOCAL_BYPASS_HOSTS == {
        "localhost",
        "127.0.0.1",
        "127.0.1.1",
        "192.168.251.10",
    }
