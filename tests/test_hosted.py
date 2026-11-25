import pytest


@pytest.fixture
def hosted_app(tmp_path, monkeypatch):
    monkeypatch.setenv("TREASURER_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("RENDER", "true")

    def make(password):
        if password:
            monkeypatch.setenv("APP_PASSWORD", password)
        else:
            monkeypatch.delenv("APP_PASSWORD", raising=False)
        from treasurer import create_app
        return create_app({"SECRET_KEY": "x", "EMAIL_POLLER": False})
    return make


def test_hosted_without_password_refuses(hosted_app):
    c = hosted_app(None).test_client()
    r = c.get("/")
    assert r.status_code == 503 and b"APP_PASSWORD" in r.data
    assert c.get("/healthz").data == b"ok"


def test_hosted_cookie_is_https_only(hosted_app):
    app = hosted_app("pw")
    assert app.config["SESSION_COOKIE_SECURE"] is True
    c = app.test_client()
    r = c.get("/login", base_url="https://example.onrender.com")
    assert "Secure" in r.headers.get("Set-Cookie", "")
    assert c.get("/", base_url="https://example.onrender.com").status_code == 302


def test_login_lockout(hosted_app):
    from treasurer import routes
    routes._failed_logins.clear()
    app = hosted_app("right")
    c = app.test_client()
    base = "https://example.onrender.com"
    c.get("/login", base_url=base)
    with c.session_transaction(base_url=base) as s:
        token = s["csrf"]
    for _ in range(routes.LOGIN_MAX_FAILURES):
        assert c.post("/login", data={"password": "wrong", "csrf": token}, base_url=base).status_code == 200
    r = c.post("/login", data={"password": "right", "csrf": token}, base_url=base)
    assert r.status_code == 429  # locked out even with the right password
    routes._failed_logins.clear()
    assert c.post("/login", data={"password": "right", "csrf": token}, base_url=base).status_code == 302
