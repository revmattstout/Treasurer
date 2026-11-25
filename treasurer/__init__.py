"""VSU Wesley Foundation treasurer: financial reports from bank statements,
online giving records and receipts."""
import os
import secrets

from flask import Flask, abort, redirect, request, session, url_for

from . import db


def create_app(test_config=None):
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
