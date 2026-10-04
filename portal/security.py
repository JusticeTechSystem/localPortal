"""CSRF protection, session -> user loading, role decorators, login throttling, security headers."""
import hmac
import secrets
import threading
import time
from functools import wraps

from flask import flash, g, jsonify, redirect, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from .db import get_db

STAFF_ROLES = ("admin", "teacher")

# Pages a user who still must choose a new password/PIN is allowed to reach.
ALLOWED_WHEN_LOCKED = {
    "auth.account", "auth.change_password", "auth.logout", "static", "main.healthz",
    "main.theme_css", "main.branding_logo", "main.manifest", "main.service_worker", "main.offline",
}


# --------------------------------------------------------------------------- #
#  Passwords
# --------------------------------------------------------------------------- #
def hash_secret(value):
    return generate_password_hash(value)


def check_secret(stored_hash, value):
    if not stored_hash:
        return False
    try:
        return check_password_hash(stored_hash, value)
    except ValueError:
        return False


# --------------------------------------------------------------------------- #
#  CSRF
# --------------------------------------------------------------------------- #
def csrf_token():
    if "_csrf" not in session:
        session["_csrf"] = secrets.token_hex(16)
    return session["_csrf"]


def _csrf_ok():
    sent = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token", "")
    expected = session.get("_csrf", "")
    return bool(expected) and hmac.compare_digest(sent, expected)


# --------------------------------------------------------------------------- #
#  Login throttling (in-memory; fine for a single-process classroom deployment)
# --------------------------------------------------------------------------- #
_FAILS = {}
_LOCK = threading.Lock()


def throttled(key, limit=8, window=300):
    with _LOCK:
        recent = [t for t in _FAILS.get(key, []) if time.time() - t < window]
        _FAILS[key] = recent
        return len(recent) >= limit


def record_fail(key):
    with _LOCK:
        _FAILS.setdefault(key, []).append(time.time())


def clear_fails(key):
    with _LOCK:
        _FAILS.pop(key, None)


def client_ip():
    return request.remote_addr or "unknown"


# --------------------------------------------------------------------------- #
#  Current user
# --------------------------------------------------------------------------- #
def load_user():
    g.user = None
    role, uid = session.get("role"), session.get("uid")
    if not role or uid is None:
        return
    db = get_db()
    if role in STAFF_ROLES:
        row = db.execute("SELECT * FROM staff WHERE id = ? AND active = 1", (uid,)).fetchone()
        if row:
            g.user = {"kind": "staff", "id": row["id"], "name": row["full_name"], "role": row["role"],
                      "code": row["username"], "class_id": None, "photo": None,
                      "must_change": bool(row["must_change"])}
    elif role == "student":
        row = db.execute("SELECT * FROM authorized_students WHERE id = ? AND COALESCE(active, 1) = 1",
                         (uid,)).fetchone()
        if row:
            g.user = {"kind": "student", "id": row["id"], "name": row["name"], "role": "student",
                      "code": row["student_id"], "class_id": row["class_id"], "photo": row["photo"],
                      "must_change": bool(row["must_change_pin"]) or not row["pin_hash"]}
    if g.user is None:
        session.clear()


def login_user(kind_role, uid):
    csrf = session.get("_csrf")
    session.clear()
    session["role"], session["uid"] = kind_role, uid
    session["_csrf"] = csrf or secrets.token_hex(16)
    session.permanent = False


def is_staff():
    return bool(g.get("user")) and g.user["role"] in STAFF_ROLES


def is_admin():
    return bool(g.get("user")) and g.user["role"] == "admin"


def is_student():
    return bool(g.get("user")) and g.user["role"] == "student"


def _deny(message, endpoint="main.dashboard"):
    if request.headers.get("X-CSRF-Token") and request.is_json:
        return jsonify(ok=False, error=message), 403
    flash(message, "error")
    return redirect(url_for(endpoint if g.get("user") else "auth.login"))


def login_required(view):
    @wraps(view)
    def wrapped(*a, **kw):
        if not g.get("user"):
            return redirect(url_for("auth.login"))
        return view(*a, **kw)
    return wrapped


def staff_required(view):
    @wraps(view)
    def wrapped(*a, **kw):
        if not g.get("user"):
            return redirect(url_for("auth.login"))
        if not is_staff():
            return _deny("You do not have permission to do that.")
        return view(*a, **kw)
    return wrapped


def admin_required(view):
    @wraps(view)
    def wrapped(*a, **kw):
        if not g.get("user"):
            return redirect(url_for("auth.login"))
        if not is_admin():
            return _deny("Only administrators can do that.")
        return view(*a, **kw)
    return wrapped


def student_required(view):
    @wraps(view)
    def wrapped(*a, **kw):
        if not g.get("user"):
            return redirect(url_for("auth.login"))
        if not is_student():
            return _deny("That action is for students.")
        return view(*a, **kw)
    return wrapped


# --------------------------------------------------------------------------- #
#  Hooks
# --------------------------------------------------------------------------- #
def before_request():
    if request.endpoint == "static":
        return None
    load_user()
    if request.method == "POST" and not _csrf_ok():
        if request.is_json or request.headers.get("X-CSRF-Token"):
            return jsonify(ok=False, error="Session expired - reload the page."), 400
        flash("Your session expired. Please try again.", "error")
        return redirect(url_for("main.dashboard" if g.user else "auth.login"))
    user = g.user
    if user and user["must_change"] and request.endpoint not in ALLOWED_WHEN_LOCKED:
        if request.endpoint:
            flash("Please choose a new password / PIN to continue.", "info")
        return redirect(url_for("auth.account"))
    return None


CSP = ("default-src 'self'; img-src 'self' data: blob:; media-src 'self' blob:; style-src 'self'; "
       "script-src 'self'; worker-src 'self'; manifest-src 'self'; connect-src 'self'; "
       "form-action 'self'; frame-ancestors 'none'; base-uri 'self'; object-src 'none'")


def after_request(resp):
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "same-origin"
    resp.headers["Content-Security-Policy"] = CSP
    if request.endpoint != "static" and "Cache-Control" not in resp.headers:
        resp.headers["Cache-Control"] = "no-store"
    return resp
