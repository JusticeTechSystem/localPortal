"""Login / logout, own-account settings (password or PIN, photo) and photo serving."""
import os

from flask import (Blueprint, abort, flash, g, redirect, render_template, request,
                   send_from_directory, url_for)

from .. import config
from ..db import audit, get_db, now
from ..security import (check_secret, clear_fails, client_ip, hash_secret, is_student,
                        login_required, login_user, record_fail, throttled)
from ..utils import remove_file, save_upload

bp = Blueprint("auth", __name__)


@bp.route("/", methods=["GET", "POST"])
def login():
    if request.method == "GET" and g.user:
        return redirect(url_for("main.dashboard"))
    selected = "student"
    if request.method == "POST":
        selected = "staff" if request.form.get("role") == "staff" else "student"
        ip_key = f"ip:{client_ip()}"
        if throttled(ip_key, 10, 300):
            flash("Too many failed attempts. Please wait a few minutes and try again.", "error")
            return render_template("login.html", selected=selected), 429
        handler = _login_staff if selected == "staff" else _login_student
        response = handler(ip_key)
        if response:
            return response
    return render_template("login.html", selected=selected)


def _login_staff(ip_key):
    username = request.form.get("username", "").strip()
    password = request.form.get("password", "")
    acct_key = f"staff:{username.lower()}"
    if throttled(acct_key, 15, 900):
        flash("This account is temporarily locked after repeated failures. Try again later.", "error")
        return None
    db = get_db()
    row = db.execute("SELECT * FROM staff WHERE username = ? AND active = 1", (username,)).fetchone() if username else None
    if row and check_secret(row["password_hash"], password):
        clear_fails(ip_key), clear_fails(acct_key)
        login_user(row["role"], row["id"])
        db.execute("UPDATE staff SET last_login = ? WHERE id = ?", (now().strftime("%Y-%m-%d %H:%M:%S"), row["id"]))
        db.commit()
        audit("login", f"staff {row['username']}", actor=row["full_name"], role=row["role"])
        return redirect(url_for("main.dashboard"))
    record_fail(ip_key), record_fail(acct_key)
    audit("login_failed", f"staff username '{username[:40]}'", actor="anonymous", role="none")
    flash("Invalid username or password.", "error")
    return None


def _login_student(ip_key):
    student_id = request.form.get("student_id", "").strip()
    pin = request.form.get("pin", "")
    acct_key = f"stu:{student_id.lower()}"
    if throttled(acct_key, 15, 900):
        flash("This ID is temporarily locked after repeated failures. Try again later.", "error")
        return None
    db = get_db()
    row = db.execute(
        "SELECT * FROM authorized_students WHERE student_id = ? COLLATE NOCASE AND COALESCE(active, 1) = 1",
        (student_id,)).fetchone() if student_id else None
    # A legacy ID that has never had a PIN can be "claimed" once: the student must then choose one.
    ok = bool(row) and (not row["pin_hash"] or check_secret(row["pin_hash"], pin))
    if ok:
        clear_fails(ip_key), clear_fails(acct_key)
        login_user("student", row["id"])
        today = now().strftime("%Y-%m-%d")
        if not db.execute("SELECT 1 FROM attendance WHERE student_id = ? AND login_date = ?",
                          (row["student_id"], today)).fetchone():
            db.execute("INSERT INTO attendance (student_id, login_date, login_time) VALUES (?, ?, ?)",
                       (row["student_id"], today, now().strftime("%H:%M:%S")))
            db.commit()
        audit("login", f"student {row['student_id']}", actor=row["name"], role="student")
        return redirect(url_for("main.dashboard"))
    record_fail(ip_key), record_fail(acct_key)
    flash("Invalid Student ID or PIN. Ask your teacher if you have forgotten it.", "error")
    return None


@bp.route("/logout", methods=["POST"])
def logout():
    from flask import session
    session.clear()
    return redirect(url_for("auth.login"))


# --------------------------------------------------------------------------- #
#  Own account
# --------------------------------------------------------------------------- #
@bp.route("/account")
@login_required
def account():
    return render_template("account.html")


@bp.route("/account/password", methods=["POST"])
@login_required
def change_password():
    db = get_db()
    staff = g.user["kind"] == "staff"
    table, key = ("staff", "id") if staff else ("authorized_students", "id")
    row = db.execute(f"SELECT * FROM {table} WHERE {key} = ?", (g.user["id"],)).fetchone()
    stored = row["password_hash"] if staff else row["pin_hash"]
    new, confirm = request.form.get("new_password", ""), request.form.get("confirm_password", "")
    current = request.form.get("current_password", "")
    forced = g.user["must_change"]

    success = False
    label = "password" if staff else "PIN"
    min_len = 8 if staff else 4
    if stored and not forced and not check_secret(stored, current):
        flash(f"Your current {label} is incorrect.", "error")
    elif len(new) < min_len or len(new) > 72:
        flash(f"Your new {label} must be at least {min_len} characters.", "error")
    elif new != confirm:
        flash(f"The two new {label}s do not match.", "error")
    elif staff and new == config.DEFAULT_ADMIN_PASSWORD:
        flash("Please choose something other than the default password.", "error")
    elif not staff and new.lower() == row["student_id"].lower():
        flash("Your PIN cannot be the same as your Student ID.", "error")
    else:
        if staff:
            db.execute("UPDATE staff SET password_hash = ?, must_change = 0 WHERE id = ?", (hash_secret(new), row["id"]))
        else:
            db.execute("UPDATE authorized_students SET pin_hash = ?, must_change_pin = 0 WHERE id = ?",
                       (hash_secret(new), row["id"]))
        db.commit()
        audit("password_changed", f"{'staff' if staff else 'student'} {g.user['code']}")
        flash(f"Your {label} was updated.", "success")
        success = True
    return redirect(url_for("main.dashboard") if success else url_for("auth.account"))


@bp.route("/account/photo", methods=["POST"])
@login_required
def upload_photo():
    if not is_student():
        flash("Profile photos are for students.", "error")
        return redirect(url_for("auth.account"))
    file = request.files.get("photo")
    if not file or not file.filename:
        flash("Choose an image first.", "error")
        return redirect(url_for("auth.account"))
    stored, err = save_upload(file, config.FOLDERS["photos"], config.IMAGE_EXTENSIONS,
                              max_mb=config.MAX_IMAGE_MB, image=True)
    if err:
        flash(err, "error")
        return redirect(url_for("auth.account"))
    db = get_db()
    old = db.execute("SELECT photo FROM authorized_students WHERE id = ?", (g.user["id"],)).fetchone()["photo"]
    db.execute("UPDATE authorized_students SET photo = ? WHERE id = ?", (stored, g.user["id"]))
    db.commit()
    remove_file(config.FOLDERS["photos"], old)
    flash("Profile photo updated.", "success")
    return redirect(url_for("auth.account"))


@bp.route("/photo/<int:sid>")
@login_required
def photo(sid):
    if g.user["kind"] != "staff" and g.user["id"] != sid:
        abort(403)
    row = get_db().execute("SELECT photo FROM authorized_students WHERE id = ?", (sid,)).fetchone()
    if not row or not row["photo"] or not os.path.isfile(os.path.join(config.FOLDERS["photos"], row["photo"])):
        abort(404)
    resp = send_from_directory(config.FOLDERS["photos"], row["photo"], max_age=3600)
    resp.headers["Cache-Control"] = "private, max-age=3600"
    return resp
