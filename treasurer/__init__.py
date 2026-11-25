"""VSU Wesley Foundation treasurer: financial reports from bank statements,
online giving records and receipts."""
import os
import secrets

from flask import Flask, abort, redirect, request, session, url_for

from . import db


def load_env_file(path=".env"):
    """Load KEY=VALUE lines from a .env file (git-ignored) into the environment.
    Real environment variables take precedence."""
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
                value = value[1:-1]
            os.environ.setdefault(key.strip(), value)


def create_app(test_config=None):
    if not (test_config or {}).get("TESTING"):
        load_env_file()
    app = Flask(__name__, instance_relative_config=True)
    data_dir = os.environ.get("TREASURER_DATA_DIR", os.path.join(app.instance_path))
    app.config.update(
        DATABASE=os.path.join(data_dir, "treasurer.sqlite3"),
        RECEIPTS_DIR=os.path.join(data_dir, "receipts"),
        MAX_CONTENT_LENGTH=64 * 1024 * 1024,
        APP_PASSWORD=os.environ.get("APP_PASSWORD"),
    )
    if test_config:
        app.config.update(test_config)
    os.makedirs(os.path.dirname(app.config["DATABASE"]), exist_ok=True)
    os.makedirs(app.config["RECEIPTS_DIR"], exist_ok=True)
    app.secret_key = app.config.get("SECRET_KEY") or os.environ.get("SECRET_KEY") or _persistent_key(data_dir)

    conn = db.connect(app.config["DATABASE"])
    db.init_db(conn)
    conn.close()
    app.teardown_appcontext(db.close_db)

    @app.before_request
    def csrf_protect():
        if request.method == "POST" and not app.config.get("TESTING"):
            token = session.get("csrf")
            if not token or not secrets.compare_digest(token, request.form.get("csrf", "")):
                abort(400, "Form expired. Go back, reload the page and try again.")

    @app.context_processor
    def csrf_token():
        if "csrf" not in session:
            session["csrf"] = secrets.token_hex(16)
        return {"csrf_token": session["csrf"]}

    @app.before_request
    def require_login():
        if not app.config.get("APP_PASSWORD"):
            return None
        if session.get("ok") or request.endpoint in ("main.login", "static"):
            return None
        return redirect(url_for("main.login", next=request.full_path))

    from .routes import bp
    app.register_blueprint(bp)

    # Background email checking (TREASURER_EMAIL_POLL_MINUTES). Skipped in
    # tests, the CLI, and the parent process of Flask's debug reloader.
    if (app.config.get("EMAIL_POLLER", True) and not app.config.get("TESTING")
            and (os.environ.get("FLASK_DEBUG") != "1" or os.environ.get("WERKZEUG_RUN_MAIN") == "true")):
        from .mailbox import start_poller
        start_poller(app)

    @app.template_filter("money")
    def money(v):
        if v is None:
            return "—"
        s = f"${abs(v):,.2f}"
        return f"({s})" if v < 0 else s

    @app.template_filter("month_name")
    def month_name(m):
        from datetime import date
        y, mo = map(int, m.split("-"))
        return date(y, mo, 1).strftime("%B %Y")

    return app


def _persistent_key(data_dir):
    path = os.path.join(data_dir, ".secret_key")
    if not os.path.exists(path):
        with open(path, "w") as f:
            f.write(secrets.token_hex(32))
        os.chmod(path, 0o600)
    with open(path) as f:
        return f.read().strip()
