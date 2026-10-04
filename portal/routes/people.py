"""Students (issue/import/edit/PIN reset/photo), classes & subjects, staff accounts, student profile."""
import re

from flask import (Blueprint, Response, abort, flash, g, redirect, render_template, request, url_for)

from .. import config
from ..db import audit, get_db, now
from ..security import admin_required, hash_secret, login_required, staff_required
from ..services import attendance_summary
from ..utils import (csv_response, gen_pin, int_or_none, next_student_id, read_csv_upload,
                     remove_file, save_upload, score_chart)

bp = Blueprint("people", __name__)


def _class_id_for(db, name):
    """Find or create a class by name (used by CSV import)."""
    name = (name or "").strip()[:60]
    if not name:
        return None
    row = db.execute("SELECT id FROM classes WHERE name = ?", (name,)).fetchone()
    if row:
        return row["id"]
    cur = db.execute("INSERT INTO classes (name, created_at) VALUES (?, ?)", (name, now().strftime("%Y-%m-%d")))
    return cur.lastrowid


# --------------------------------------------------------------------------- #
#  Students
# --------------------------------------------------------------------------- #
@bp.route("/students")
@staff_required
def students():
    db = get_db()
    class_filter = int_or_none(request.args.get("class_id"))
    sql = """SELECT s.*, c.name AS class_name FROM authorized_students s
             LEFT JOIN classes c ON c.id = s.class_id"""
    params = ()
    if class_filter:
        sql += " WHERE s.class_id = ?"
        params = (class_filter,)
    rows = db.execute(sql + " ORDER BY c.name, s.name", params).fetchall()
    classes = db.execute("SELECT * FROM classes ORDER BY name").fetchall()
    return render_template("students.html", students=rows, classes=classes, class_filter=class_filter)


@bp.route("/students/add", methods=["POST"])
@staff_required
def add_student():
    name = request.form.get("name", "").strip()[:80]
    student_id = request.form.get("student_id", "").strip()[:40]
    class_id = int_or_none(request.form.get("class_id"))
    db = get_db()
    if not name:
        flash("The student's full name is required.", "error")
        return redirect(url_for("people.students"))
    if not student_id:
        student_id = next_student_id(db)
    if db.execute("SELECT 1 FROM authorized_students WHERE student_id = ? COLLATE NOCASE", (student_id,)).fetchone():
        flash("That Student ID already exists.", "error")
        return redirect(url_for("people.students"))
    pin = gen_pin()
    db.execute("INSERT INTO authorized_students (student_id, name, class_id, pin_hash, must_change_pin, active, created_at) "
               "VALUES (?, ?, ?, ?, 1, 1, ?)",
               (student_id, name, class_id, hash_secret(pin), now().strftime("%Y-%m-%d")))
    db.commit()
    audit("student_added", f"{student_id} {name}")
    return render_template("credentials.html", rows=[{"name": name, "student_id": student_id, "pin": pin,
                                                      "class_name": _class_name(db, class_id)}], title="Student added")


def _class_name(db, class_id):
    if not class_id:
        return ""
    row = db.execute("SELECT name FROM classes WHERE id = ?", (class_id,)).fetchone()
    return row["name"] if row else ""


@bp.route("/students/import", methods=["POST"])
@staff_required
def import_students():
    file = request.files.get("file")
    if not file or not file.filename:
        flash("Choose a CSV file first.", "error")
        return redirect(url_for("people.students"))
    try:
        rows = read_csv_upload(file, max_rows=500)
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(url_for("people.students"))
    db = get_db()
    created, errors, taken = [], [], []
    for i, row in enumerate(rows, start=2):
        name = (row.get("full_name") or row.get("name") or "").strip()[:80]
        if not name:
            errors.append(f"Row {i}: missing full_name")
            continue
        sid = (row.get("student_id") or row.get("id") or "").strip()[:40] or next_student_id(db, taken)
        if sid.lower() in (t.lower() for t in taken) or db.execute(
                "SELECT 1 FROM authorized_students WHERE student_id = ? COLLATE NOCASE", (sid,)).fetchone():
            errors.append(f"Row {i}: ID {sid} already exists")
            continue
        class_name = row.get("class") or row.get("class_name") or ""
        class_id = _class_id_for(db, class_name)
        pin = (row.get("pin") or "").strip() or gen_pin()
        if len(pin) < 4:
            pin = gen_pin()
        db.execute("INSERT INTO authorized_students (student_id, name, class_id, pin_hash, must_change_pin, active, created_at) "
                   "VALUES (?, ?, ?, ?, 1, 1, ?)",
                   (sid, name, class_id, hash_secret(pin), now().strftime("%Y-%m-%d")))
        taken.append(sid)
        created.append({"name": name, "student_id": sid, "pin": pin, "class_name": class_name.strip()})
    db.commit()
    audit("students_imported", f"{len(created)} created, {len(errors)} skipped")
    return render_template("credentials.html", rows=created, errors=errors, title="Import complete")


@bp.route("/students/credentials.csv", methods=["POST"])
@staff_required
def credentials_csv():
    """Echo back the credential sheet the teacher is looking at as a CSV (nothing is stored)."""
    ids = request.form.getlist("student_id")
    names = request.form.getlist("name")
    classes = request.form.getlist("class_name")
    pins = request.form.getlist("pin")
    rows = list(zip(names, ids, classes, pins))
    return csv_response("student-credentials.csv", ["full_name", "student_id", "class", "temporary_pin"], rows)


@bp.route("/students/template.csv")
@staff_required
def students_template():
    return csv_response("students-template.csv", ["full_name", "student_id", "class"],
                        [["Ada Lovelace", "", "JSS1 A"], ["Grace Hopper", "STU-200", "JSS1 B"]])


@bp.route("/students/<int:sid>/edit", methods=["POST"])
@staff_required
def edit_student(sid):
    db = get_db()
    row = db.execute("SELECT * FROM authorized_students WHERE id = ?", (sid,)).fetchone()
    if not row:
        abort(404)
    name = request.form.get("name", "").strip()[:80]
    new_id = request.form.get("student_id", "").strip()[:40]
    class_id = int_or_none(request.form.get("class_id"))
    if not name or not new_id:
        flash("Name and Student ID are required.", "error")
        return redirect(url_for("people.students"))
    if new_id.lower() != row["student_id"].lower() and db.execute(
            "SELECT 1 FROM authorized_students WHERE student_id = ? COLLATE NOCASE", (new_id,)).fetchone():
        flash("That Student ID already exists.", "error")
        return redirect(url_for("people.students"))
    if new_id != row["student_id"]:
        # The ID is the key used by attendance/results/submissions, so move them all together.
        db.execute("PRAGMA foreign_keys = OFF")
        for table in ("attendance", "exam_results", "submissions", "exam_attempts", "video_progress"):
            db.execute(f"UPDATE {table} SET student_id = ? WHERE student_id = ?", (new_id, row["student_id"]))
        db.execute("UPDATE authorized_students SET student_id = ? WHERE id = ?", (new_id, sid))
        db.execute("PRAGMA foreign_keys = ON")
    db.execute("UPDATE authorized_students SET name = ?, class_id = ? WHERE id = ?", (name, class_id, sid))
    db.commit()
    audit("student_edited", f"{new_id} {name}")
    flash("Student updated.", "success")
    return redirect(request.referrer or url_for("people.students"))


@bp.route("/students/<int:sid>/reset-pin", methods=["POST"])
@staff_required
def reset_pin(sid):
    db = get_db()
    row = db.execute("SELECT * FROM authorized_students WHERE id = ?", (sid,)).fetchone()
    if not row:
        abort(404)
    pin = gen_pin()
    db.execute("UPDATE authorized_students SET pin_hash = ?, must_change_pin = 1 WHERE id = ?", (hash_secret(pin), sid))
    db.commit()
    audit("pin_reset", row["student_id"])
    return render_template("credentials.html", title="PIN reset", rows=[{
        "name": row["name"], "student_id": row["student_id"], "pin": pin,
        "class_name": _class_name(db, row["class_id"])}])


@bp.route("/students/<int:sid>/toggle", methods=["POST"])
@staff_required
def toggle_student(sid):
    db = get_db()
    row = db.execute("SELECT * FROM authorized_students WHERE id = ?", (sid,)).fetchone()
    if row:
        active = 0 if (row["active"] is None or row["active"]) else 1
        db.execute("UPDATE authorized_students SET active = ? WHERE id = ?", (active, sid))
        db.commit()
        audit("student_activated" if active else "student_deactivated", row["student_id"])
        flash(f"{row['name']} {'re-activated' if active else 'deactivated (history kept)'}.", "success")
    return redirect(request.referrer or url_for("people.students"))


@bp.route("/students/<int:sid>/delete", methods=["POST"])
@staff_required
def delete_student(sid):
    db = get_db()
    row = db.execute("SELECT * FROM authorized_students WHERE id = ?", (sid,)).fetchone()
    if row:
        # attendance has a foreign key to the student, so its rows must go first
        for table in ("attendance", "exam_attempts", "video_progress"):
            db.execute(f"DELETE FROM {table} WHERE student_id = ?", (row["student_id"],))
        db.execute("DELETE FROM authorized_students WHERE id = ?", (sid,))
        db.commit()
        remove_file(config.FOLDERS["photos"], row["photo"])
        audit("student_deleted", f"{row['student_id']} {row['name']}")
        flash("Student ID revoked.", "success")
    return redirect(url_for("people.students"))


@bp.route("/students/<int:sid>/photo", methods=["POST"])
@staff_required
def student_photo(sid):
    db = get_db()
    row = db.execute("SELECT photo FROM authorized_students WHERE id = ?", (sid,)).fetchone()
    file = request.files.get("photo")
    if not row or not file or not file.filename:
        flash("Choose an image first.", "error")
        return redirect(request.referrer or url_for("people.students"))
    stored, err = save_upload(file, config.FOLDERS["photos"], config.IMAGE_EXTENSIONS,
                              max_mb=config.MAX_IMAGE_MB, image=True)
    if err:
        flash(err, "error")
    else:
        db.execute("UPDATE authorized_students SET photo = ? WHERE id = ?", (stored, sid))
        db.commit()
        remove_file(config.FOLDERS["photos"], row["photo"])
        flash("Photo updated.", "success")
    return redirect(request.referrer or url_for("people.students"))


# --------------------------------------------------------------------------- #
#  Student profile (teacher view, or a student looking at their own results)
# --------------------------------------------------------------------------- #
def student_summary(db, row):
    sid = row["student_id"]
    exams = db.execute(
        """SELECT r.*, e.title, e.due_date FROM exam_results r JOIN exams e ON e.id = r.exam_id
           WHERE r.student_id = ? ORDER BY r.id""", (sid,)).fetchall()
    chart_points = [(e["title"], round(100.0 * e["score"] / e["total"]) if e["total"] else 0) for e in exams]
    subs = db.execute(
        """SELECT s.*, a.title FROM submissions s LEFT JOIN assignments a ON a.id = s.assignment_id
           WHERE s.student_id = ? ORDER BY s.id DESC""", (sid,)).fetchall()
    videos = db.execute(
        """SELECT v.id, v.title, v.duration, p.watched, p.completed_at FROM videos v
           LEFT JOIN video_progress p ON p.video_id = v.id AND p.student_id = ?
           WHERE v.class_id IS NULL OR v.class_id = ? ORDER BY v.id DESC""", (sid, row["class_id"])).fetchall()
    days = db.execute("SELECT COUNT(DISTINCT login_date) FROM attendance WHERE student_id = ?", (sid,)).fetchone()[0]
    school_days = db.execute("SELECT COUNT(DISTINCT login_date) FROM attendance").fetchone()[0]
    avg = round(sum(p for _l, p in chart_points) / len(chart_points)) if chart_points else None
    return {"exams": exams, "chart": score_chart(chart_points), "subs": subs, "videos": videos,
            "present": days, "school_days": school_days, "average": avg,
            "attendance_pct": round(100.0 * days / school_days) if school_days else None}


@bp.route("/students/<int:sid>")
@login_required
def profile(sid):
    db = get_db()
    if g.user["kind"] == "student" and g.user["id"] != sid:
        abort(403)
    row = db.execute("""SELECT s.*, c.name AS class_name FROM authorized_students s
                        LEFT JOIN classes c ON c.id = s.class_id WHERE s.id = ?""", (sid,)).fetchone()
    if not row:
        abort(404)
    classes = db.execute("SELECT * FROM classes ORDER BY name").fetchall()
    return render_template("profile.html", s=row, summary=student_summary(db, row), classes=classes)


@bp.route("/my-results")
@login_required
def my_results():
    if g.user["kind"] != "student":
        return redirect(url_for("people.students"))
    return redirect(url_for("people.profile", sid=g.user["id"]))


# --------------------------------------------------------------------------- #
#  Classes & subjects
# --------------------------------------------------------------------------- #
@bp.route("/classes")
@staff_required
def classes():
    db = get_db()
    rows = db.execute("""SELECT c.*, (SELECT COUNT(*) FROM authorized_students s WHERE s.class_id = c.id) AS n
                         FROM classes c ORDER BY c.name""").fetchall()
    subjects = db.execute("SELECT * FROM subjects ORDER BY name").fetchall()
    return render_template("classes.html", classes=rows, subjects=subjects)


@bp.route("/classes/add", methods=["POST"])
@staff_required
def add_class():
    name = request.form.get("name", "").strip()[:60]
    db = get_db()
    if not name:
        flash("Enter a class name.", "error")
    elif db.execute("SELECT 1 FROM classes WHERE name = ?", (name,)).fetchone():
        flash("That class already exists.", "error")
    else:
        db.execute("INSERT INTO classes (name, created_at) VALUES (?, ?)", (name, now().strftime("%Y-%m-%d")))
        db.commit()
        audit("class_added", name)
        flash("Class added.", "success")
    return redirect(url_for("people.classes"))


@bp.route("/classes/<int:cid>/rename", methods=["POST"])
@staff_required
def rename_class(cid):
    name = request.form.get("name", "").strip()[:60]
    db = get_db()
    if name and not db.execute("SELECT 1 FROM classes WHERE name = ? AND id != ?", (name, cid)).fetchone():
        db.execute("UPDATE classes SET name = ? WHERE id = ?", (name, cid))
        db.commit()
        audit("class_renamed", name)
        flash("Class renamed.", "success")
    else:
        flash("Enter a unique class name.", "error")
    return redirect(url_for("people.classes"))


@bp.route("/classes/<int:cid>/delete", methods=["POST"])
@staff_required
def delete_class(cid):
    db = get_db()
    row = db.execute("SELECT name FROM classes WHERE id = ?", (cid,)).fetchone()
    if row:
        # Content that targeted this class becomes visible to everyone; students become unassigned.
        for table in ("authorized_students", "exams", "assignments", "notes", "videos", "announcements"):
            db.execute(f"UPDATE {table} SET class_id = NULL WHERE class_id = ?", (cid,))
        db.execute("DELETE FROM classes WHERE id = ?", (cid,))
        db.commit()
        audit("class_deleted", row["name"])
        flash("Class deleted.", "success")
    return redirect(url_for("people.classes"))


@bp.route("/subjects/add", methods=["POST"])
@staff_required
def add_subject():
    name = request.form.get("name", "").strip()[:60]
    db = get_db()
    if not name:
        flash("Enter a subject name.", "error")
    elif db.execute("SELECT 1 FROM subjects WHERE name = ?", (name,)).fetchone():
        flash("That subject already exists.", "error")
    else:
        db.execute("INSERT INTO subjects (name) VALUES (?)", (name,))
        db.commit()
        audit("subject_added", name)
        flash("Subject added.", "success")
    return redirect(url_for("people.classes"))


@bp.route("/subjects/<int:sid>/delete", methods=["POST"])
@staff_required
def delete_subject(sid):
    db = get_db()
    row = db.execute("SELECT name FROM subjects WHERE id = ?", (sid,)).fetchone()
    if row:
        for table in ("exams", "assignments", "notes", "videos"):
            db.execute(f"UPDATE {table} SET subject_id = NULL WHERE subject_id = ?", (sid,))
        db.execute("DELETE FROM subjects WHERE id = ?", (sid,))
        db.commit()
        audit("subject_deleted", row["name"])
        flash("Subject deleted.", "success")
    return redirect(url_for("people.classes"))


# --------------------------------------------------------------------------- #
#  Staff accounts (administrators only)
# --------------------------------------------------------------------------- #
USERNAME_RE = re.compile(r"^[A-Za-z0-9._-]{3,32}$")


@bp.route("/staff")
@admin_required
def staff():
    rows = get_db().execute("SELECT * FROM staff ORDER BY role, full_name").fetchall()
    return render_template("staff.html", staff=rows)


@bp.route("/staff/add", methods=["POST"])
@admin_required
def add_staff():
    db = get_db()
    username = request.form.get("username", "").strip()
    full_name = request.form.get("full_name", "").strip()[:80]
    role = "admin" if request.form.get("role") == "admin" else "teacher"
    if not USERNAME_RE.match(username) or not full_name:
        flash("Username must be 3-32 letters/numbers (. _ - allowed) and a full name is required.", "error")
        return redirect(url_for("people.staff"))
    if db.execute("SELECT 1 FROM staff WHERE username = ?", (username,)).fetchone():
        flash("That username is taken.", "error")
        return redirect(url_for("people.staff"))
    password = gen_pin(8)
    db.execute("INSERT INTO staff (username, full_name, password_hash, role, must_change, created_at) "
               "VALUES (?, ?, ?, ?, 1, ?)", (username, full_name, hash_secret(password), role, now().strftime("%Y-%m-%d")))
    db.commit()
    audit("staff_added", f"{username} ({role})")
    return render_template("credentials.html", staff_mode=True, title="Staff account created", rows=[{
        "name": full_name, "student_id": username, "pin": password, "class_name": role}])


@bp.route("/staff/<int:uid>/reset", methods=["POST"])
@admin_required
def reset_staff(uid):
    db = get_db()
    row = db.execute("SELECT * FROM staff WHERE id = ?", (uid,)).fetchone()
    if not row:
        abort(404)
    password = gen_pin(8)
    db.execute("UPDATE staff SET password_hash = ?, must_change = 1 WHERE id = ?", (hash_secret(password), uid))
    db.commit()
    audit("staff_password_reset", row["username"])
    return render_template("credentials.html", staff_mode=True, title="Password reset", rows=[{
        "name": row["full_name"], "student_id": row["username"], "pin": password, "class_name": row["role"]}])


@bp.route("/staff/<int:uid>/update", methods=["POST"])
@admin_required
def update_staff(uid):
    db = get_db()
    row = db.execute("SELECT * FROM staff WHERE id = ?", (uid,)).fetchone()
    if not row:
        abort(404)
    action = request.form.get("action")
    if row["id"] == g.user["id"] and action in ("deactivate", "make_teacher"):
        flash("You cannot demote or deactivate your own account.", "error")
        return redirect(url_for("people.staff"))
    if action == "deactivate":
        db.execute("UPDATE staff SET active = 0 WHERE id = ?", (uid,))
    elif action == "activate":
        db.execute("UPDATE staff SET active = 1 WHERE id = ?", (uid,))
    elif action == "make_admin":
        db.execute("UPDATE staff SET role = 'admin' WHERE id = ?", (uid,))
    elif action == "make_teacher":
        db.execute("UPDATE staff SET role = 'teacher' WHERE id = ?", (uid,))
    else:
        abort(400)
    db.commit()
    audit("staff_updated", f"{row['username']}: {action}")
    flash("Account updated.", "success")
    return redirect(url_for("people.staff"))
