"""EduPortal application factory."""
import logging
import threading
import time

from flask import Flask, flash, g, redirect, render_template, request, session, url_for
from werkzeug.middleware.proxy_fix import ProxyFix

from . import config, db, security, utils

_last_backup_check = {"t": 0.0}


def create_app():
    config.ensure_dirs()
    db.init_db()

    app = Flask(__name__, root_path=config.BASE_DIR)
    app.secret_key = config.load_secret_key()
    app.config.update(
        MAX_CONTENT_LENGTH=(config.MAX_VIDEO_MB + 10) * 1024 * 1024,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=config.COOKIE_SECURE,
        JSON_SORT_KEYS=False,
    )
    if config.TRUST_PROXY:
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
    logging.getLogger("werkzeug").setLevel(logging.ERROR)

    utils.register_filters(app)
    app.jinja_env.globals["csrf_token"] = security.csrf_token
    app.teardown_appcontext(db.close_db)
    app.before_request(_limit_request_size)      # must run first: reject oversize bodies before parsing them
    app.before_request(security.before_request)
    app.before_request(_maybe_scheduled_backup)
    app.after_request(security.after_request)

    @app.context_processor
    def inject_globals():
        return {
            "me": g.get("user"),
            "is_staff": security.is_staff(),
            "is_admin": security.is_admin(),
            "school": {
                "name": db.get_setting("school_name"),
                "tagline": db.get_setting("tagline"),
                "has_logo": bool(db.get_setting("logo")),
            },
            "tz_label": db.tz_label(),
            "max_video_mb": config.MAX_VIDEO_MB,
            "max_upload_mb": config.MAX_UPLOAD_MB,
        }

    from .routes import academics, admin, auth, main, people, reports, videos
    for module in (auth, main, people, academics, videos, reports, admin):
        app.register_blueprint(module.bp)

    _register_errors(app)
    return app


def _limit_request_size():
    """Videos may be big; everything else keeps the small default limit."""
    if request.method != "POST" or request.content_length is None:
        return None
    big = request.endpoint in ("videos.upload", "videos.edit")
    limit_mb = config.MAX_VIDEO_MB if big else config.MAX_UPLOAD_MB
    if request.content_length > (limit_mb + 2) * 1024 * 1024:
        flash(f"That upload is too large (limit {limit_mb} MB).", "error")
        target = request.referrer or url_for("main.dashboard")
        if request.headers.get("X-Requested-With") == "XHR-upload":
            from flask import jsonify
            return jsonify(ok=False, redirect=target)
        return redirect(target)
    return None


def _maybe_scheduled_backup():
    """Daily DB snapshot, triggered lazily by traffic (works on every host, no scheduler needed)."""
    if time.time() - _last_backup_check["t"] < 600:
        return None
    _last_backup_check["t"] = time.time()
    if db.get_setting("backup_enabled") != "1":
        return None
    from . import backups
    threading.Thread(target=backups.run_scheduled_if_due, daemon=True).start()
    return None


ERRORS = {
    400: ("Bad request", "The request could not be understood."),
    403: ("Access denied", "You are not allowed to view this."),
    404: ("Page not found", "The page or file you asked for does not exist."),
    405: ("Method not allowed", "That action is not available this way."),
    500: ("Something went wrong", "An unexpected error occurred. Please try again."),
}


def _register_errors(app):
    def render_error(code):
        title, text = ERRORS.get(code, ("Error", "Something went wrong."))
        return render_template("error.html", code=code, title=title, text=text), code

    for code in ERRORS:
        app.register_error_handler(code, lambda e, c=code: render_error(c))

    @app.errorhandler(413)
    def too_large(_e):
        flash("That upload is too large.", "error")
        return redirect(url_for("main.dashboard") if session.get("role") else url_for("auth.login"))
