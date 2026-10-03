"""
EduPortal — a classroom portal (attendance, CBT exams, assignments, notes).

Runs in two modes from the same code:

  * Locally / LAN :  python app.py          -> prints the exact URL(s) to share
  * Hosted        :  gunicorn app:app       -> any Python host (Render, Railway, VPS ...)

Built by JusticeTech.
"""
import argparse
import hmac
import logging
import os
import re
import secrets
import shutil
import socket
import sqlite3
import sys
import threading
import time
from datetime import datetime
from functools import wraps

from flask import (Flask, abort, flash, g, redirect, render_template, request,
                   send_from_directory, session, url_for)
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.utils import secure_filename

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


# --------------------------------------------------------------------------- #
#  Configuration (environment variables, optionally loaded from a .env file)
# --------------------------------------------------------------------------- #
def _load_dotenv(path):
    """Tiny .env reader so no extra dependency is needed. Real env vars win."""
    if not os.path.isfile(path):
        return
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            value = value.strip().strip('"').strip("'")
            os.environ.setdefault(key.strip(), value)


_load_dotenv(os.path.join(BASE_DIR, ".env"))

DATA_DIR = os.path.abspath(os.environ.get("DATA_DIR") or os.path.join(BASE_DIR, "data"))
DB_PATH = os.path.join(DATA_DIR, "classroom.db")
UPLOAD_ROOT = os.path.join(DATA_DIR, "uploads")
UPLOAD_ASSIGNMENTS = os.path.join(UPLOAD_ROOT, "assignments")
UPLOAD_SUBMISSIONS = os.path.join(UPLOAD_ROOT, "submissions")
UPLOAD_NOTES = os.path.join(UPLOAD_ROOT, "notes")
FOLDERS = {"assignments": UPLOAD_ASSIGNMENTS, "submissions": UPLOAD_SUBMISSIONS, "notes": UPLOAD_NOTES}

for _p in FOLDERS.values():
    os.makedirs(_p, exist_ok=True)

TEACHER_PASSWORD = os.environ.get("TEACHER_PASSWORD") or "admin123"
USING_DEFAULT_PASSWORD = not os.environ.get("TEACHER_PASSWORD")

MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "25"))
ALLOWED_EXTENSIONS = {
    "pdf", "doc", "docx", "odt", "rtf", "txt", "md", "ppt", "pptx", "odp", "xls", "xlsx",
    "ods", "csv", "png", "jpg", "jpeg", "gif", "webp", "zip", "rar", "7z", "py", "java",
    "c", "cpp", "h", "js", "html", "css", "json", "sql", "ipynb",
}

_tz_name = os.environ.get("PORTAL_TIMEZONE", "").strip()
TZ = None
if _tz_name and ZoneInfo:
    try:
        TZ = ZoneInfo(_tz_name)
    except Exception:  # unknown timezone name
        print(f"[!] PORTAL_TIMEZONE '{_tz_name}' is not valid - using the server's local time.", file=sys.stderr)
        _tz_name = ""
TZ_LABEL = _tz_name or "server local time"


def _load_secret_key():
    key = os.environ.get("SECRET_KEY")
    if key:
        return key
    key_file = os.path.join(DATA_DIR, ".secret_key")
    if os.path.isfile(key_file):
        with open(key_file, encoding="utf-8") as fh:
            stored = fh.read().strip()
            if stored:
                return stored
    key = secrets.token_hex(32)
    try:
        with open(key_file, "w", encoding="utf-8") as fh:
            fh.write(key)
        os.chmod(key_file, 0o600)
    except OSError:
        pass
    return key


app = Flask(__name__)
app.secret_key = _load_secret_key()
app.config.update(
    MAX_CONTENT_LENGTH=MAX_UPLOAD_MB * 1024 * 1024,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE") == "1",
    TEMPLATES_AUTO_RELOAD=False,
)
if os.environ.get("TRUST_PROXY") == "1":
    # Hosted behind a reverse proxy (Render, Railway, Nginx ...): trust X-Forwarded-* headers.
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

logging.getLogger("werkzeug").setLevel(logging.ERROR)


# --------------------------------------------------------------------------- #
#  Time helpers (deadlines must use ONE timezone, not whatever the host uses)
# --------------------------------------------------------------------------- #
def now():
    return datetime.now(TZ).replace(tzinfo=None) if TZ else datetime.now()


def parse_dt(value):
    if not value:
        return None
    for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None


def is_closed(due_date):
    due = parse_dt(due_date)
    return bool(due and now() > due)


@app.template_filter("fmt_dt")
def fmt_dt(value):
    dt = parse_dt(value)
    return dt.strftime("%d %b %Y, %H:%M") if dt else (value or "")


_PREFIX_RE = re.compile(r"^[0-9a-f]{8}__")


@app.template_filter("display_name")
def display_name(stored):
    return _PREFIX_RE.sub("", stored or "")


# --------------------------------------------------------------------------- #
#  Database
# --------------------------------------------------------------------------- #
def _migrate_legacy_data():
    """Older versions kept classroom.db and uploads/ next to app.py - move them over once."""
    legacy_db = os.path.join(BASE_DIR, "classroom.db")
    if os.path.isfile(legacy_db) and not os.path.isfile(DB_PATH) and os.path.abspath(DATA_DIR) != BASE_DIR:
        shutil.copy2(legacy_db, DB_PATH)
    legacy_uploads = os.path.join(BASE_DIR, "uploads")
    if os.path.isdir(legacy_uploads) and os.path.abspath(UPLOAD_ROOT) != os.path.abspath(legacy_uploads):
        for sub in FOLDERS:
            src = os.path.join(legacy_uploads, sub)
            if not os.path.isdir(src):
                continue
            for name in os.listdir(src):
                src_file = os.path.join(src, name)
                dst = os.path.join(FOLDERS[sub], name)
                if os.path.isfile(src_file) and not os.path.exists(dst):
                    try:
                        shutil.copy2(src_file, dst)
                    except OSError:
                        pass  # never let a copy problem stop the portal from starting


def init_db():
    os.makedirs(DATA_DIR, exist_ok=True)
    _migrate_legacy_data()
    conn = sqlite3.connect(DB_PATH, timeout=15)
    c = conn.cursor()
    try:
        c.execute("PRAGMA journal_mode=WAL")
    except sqlite3.DatabaseError:
        pass

    c.execute('''CREATE TABLE IF NOT EXISTS authorized_students (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    student_id TEXT UNIQUE NOT NULL,
                    name TEXT DEFAULT 'Unknown Student')''')
    c.execute('''CREATE TABLE IF NOT EXISTS attendance (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    student_id TEXT, login_date TEXT, login_time TEXT,
                    FOREIGN KEY(student_id) REFERENCES authorized_students(student_id))''')
    c.execute('''CREATE TABLE IF NOT EXISTS exams (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT, due_date TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS exam_questions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, exam_id INTEGER, question TEXT,
                    opt_a TEXT, opt_b TEXT, opt_c TEXT, opt_d TEXT, correct_opt TEXT,
                    FOREIGN KEY(exam_id) REFERENCES exams(id) ON DELETE CASCADE)''')
    c.execute('''CREATE TABLE IF NOT EXISTS exam_results (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, exam_id INTEGER, student_id TEXT,
                    score INTEGER, total INTEGER, submitted_at TEXT,
                    FOREIGN KEY(exam_id) REFERENCES exams(id) ON DELETE CASCADE)''')
    c.execute('''CREATE TABLE IF NOT EXISTS assignments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT, filename TEXT,
                    instructions TEXT, due_date TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS submissions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, assignment_id INTEGER, student_id TEXT,
                    filename TEXT, grade TEXT, feedback TEXT,
                    FOREIGN KEY(assignment_id) REFERENCES assignments(id))''')
    c.execute('''CREATE TABLE IF NOT EXISTS notes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT, filename TEXT)''')

    # Auto-migrations for databases created by older versions
    for table, column, ddl in (
        ("authorized_students", "name", "ALTER TABLE authorized_students ADD COLUMN name TEXT DEFAULT 'Unknown Student'"),
        ("assignments", "due_date", "ALTER TABLE assignments ADD COLUMN due_date TEXT"),
    ):
        cols = [r[1] for r in c.execute(f"PRAGMA table_info({table})").fetchall()]
        if column not in cols:
            c.execute(ddl)

    conn.commit()
    conn.close()


init_db()


def get_db():
    if "db" not in g:
        conn = sqlite3.connect(DB_PATH, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        g.db = conn
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


# --------------------------------------------------------------------------- #
#  Security: CSRF, headers, login throttling, access control
# --------------------------------------------------------------------------- #
def csrf_token():
    if "_csrf" not in session:
        session["_csrf"] = secrets.token_hex(16)
    return session["_csrf"]


app.jinja_env.globals["csrf_token"] = csrf_token


@app.before_request
def csrf_protect():
    if request.method == "POST":
        sent = request.form.get("csrf_token", "")
        expected = session.get("_csrf", "")
        if not expected or not hmac.compare_digest(sent, expected):
            flash("Your session expired. Please try again.", "error")
            return redirect(url_for("dashboard" if "role" in session else "index"))


@app.after_request
def security_headers(resp):
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "same-origin"
    resp.headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
        "form-action 'self'; frame-ancestors 'none'; base-uri 'self'"
    )
    if request.endpoint != "static":
        resp.headers["Cache-Control"] = "no-store"
    return resp


_FAILS = {}
_FAILS_LOCK = threading.Lock()
MAX_FAILS, LOCK_SECONDS = 8, 300


def _client_key():
    return request.remote_addr or "unknown"


def login_locked():
    with _FAILS_LOCK:
        recent = [t for t in _FAILS.get(_client_key(), []) if time.time() - t < LOCK_SECONDS]
        _FAILS[_client_key()] = recent
        return len(recent) >= MAX_FAILS


def record_fail():
    with _FAILS_LOCK:
        _FAILS.setdefault(_client_key(), []).append(time.time())


def clear_fails():
    with _FAILS_LOCK:
        _FAILS.pop(_client_key(), None)


def login_required(view):
    @wraps(view)
    def wrapped(*a, **kw):
        if "role" not in session:
            return redirect(url_for("index"))
        return view(*a, **kw)
    return wrapped


def role_required(role):
    def deco(view):
        @wraps(view)
        def wrapped(*a, **kw):
            if "role" not in session:
                return redirect(url_for("index"))
            if session["role"] != role:
                flash("You do not have permission to do that.", "error")
                return redirect(url_for("dashboard"))
            return view(*a, **kw)
        return wrapped
    return deco


# --------------------------------------------------------------------------- #
#  File helpers
# --------------------------------------------------------------------------- #
def save_upload(file, folder):
    """Validate + store an upload. Returns (stored_name, error_message)."""
    original = file.filename or ""
    stem, ext = os.path.splitext(original)
    ext = ext.lower().lstrip(".")
    if ext not in ALLOWED_EXTENSIONS:
        return None, f"File type '.{ext or '?'}' is not allowed."
    base = secure_filename(stem) or "file"
    stored = f"{secrets.token_hex(4)}__{base}.{ext}"
    file.save(os.path.join(folder, stored))
    return stored, None


def remove_file(folder, stored):
    if stored:
        path = os.path.join(folder, stored)
        if os.path.isfile(path):
            try:
                os.remove(path)
            except OSError:
                pass


# --------------------------------------------------------------------------- #
#  Routes: health, auth
# --------------------------------------------------------------------------- #
@app.route("/healthz")
def healthz():
    return "ok", 200, {"Content-Type": "text/plain"}


@app.route("/", methods=["GET", "POST"])
def index():
    if request.method == "GET" and "role" in session:
        return redirect(url_for("dashboard"))

    selected_role = "student"
    if request.method == "POST":
        role = request.form.get("role", "student")
        selected_role = "teacher" if role == "teacher" else "student"

        if login_locked():
            flash("Too many failed attempts. Please wait a few minutes and try again.", "error")
            return render_template("login.html", selected_role=selected_role), 429

        if role == "teacher":
            password = request.form.get("password", "")
            if hmac.compare_digest(password.encode(), TEACHER_PASSWORD.encode()):
                clear_fails()
                session.clear()
                session.update(user_id="Teacher", name="Instructor", role="teacher")
                return redirect(url_for("dashboard"))
            record_fail()
            flash("Invalid teacher password.", "error")
        elif role == "student":
            student_id = request.form.get("student_id", "").strip()
            db = get_db()
            student = db.execute(
                "SELECT * FROM authorized_students WHERE student_id = ? COLLATE NOCASE", (student_id,)
            ).fetchone() if student_id else None
            if student:
                clear_fails()
                session.clear()
                session.update(user_id=student["student_id"], name=student["name"], role="student")
                today = now().strftime("%Y-%m-%d")
                if not db.execute("SELECT id FROM attendance WHERE student_id = ? AND login_date = ?",
                                  (student["student_id"], today)).fetchone():
                    db.execute("INSERT INTO attendance (student_id, login_date, login_time) VALUES (?, ?, ?)",
                               (student["student_id"], today, now().strftime("%H:%M:%S")))
                    db.commit()
                return redirect(url_for("dashboard"))
            record_fail()
            flash("Unauthorized Student ID. Contact your teacher for an issued ID.", "error")
        else:
            flash("Please choose how you want to log in.", "error")
    return render_template("login.html", selected_role=selected_role)


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("index"))


# --------------------------------------------------------------------------- #
#  Dashboard
# --------------------------------------------------------------------------- #
def _decorate(rows):
    out = []
    for r in rows:
        d = dict(r)
        d["closed"] = is_closed(d.get("due_date"))
        out.append(d)
    return out


@app.route("/dashboard")
@login_required
def dashboard():
    db = get_db()
    assignments = _decorate(db.execute(
        """SELECT a.*, (SELECT COUNT(*) FROM submissions s WHERE s.assignment_id = a.id) AS sub_count
           FROM assignments a ORDER BY a.id DESC""").fetchall())
    notes = db.execute("SELECT * FROM notes ORDER BY id DESC").fetchall()
    exams = _decorate(db.execute(
        """SELECT e.*, (SELECT COUNT(*) FROM exam_questions q WHERE q.exam_id = e.id) AS q_count,
                  (SELECT COUNT(*) FROM exam_results r WHERE r.exam_id = e.id) AS result_count
           FROM exams e ORDER BY e.id DESC""").fetchall())

    if session["role"] == "teacher":
        students = db.execute("SELECT * FROM authorized_students ORDER BY id DESC").fetchall()
        submissions = db.execute(
            """SELECT s.*, a.title AS assignment_title, st.name AS student_name
               FROM submissions s
               LEFT JOIN assignments a ON s.assignment_id = a.id
               LEFT JOIN authorized_students st ON s.student_id = st.student_id
               ORDER BY s.id DESC""").fetchall()
        attendance = db.execute(
            """SELECT a.login_date, a.login_time, a.student_id, s.name
               FROM attendance a LEFT JOIN authorized_students s ON a.student_id = s.student_id
               ORDER BY a.login_date DESC, a.login_time DESC LIMIT 300""").fetchall()
        results = db.execute(
            """SELECT r.*, e.title, s.name FROM exam_results r
               JOIN exams e ON r.exam_id = e.id
               LEFT JOIN authorized_students s ON r.student_id = s.student_id
               ORDER BY r.id DESC""").fetchall()
        stats = {
            "students": len(students),
            "exams": len(exams),
            "assignments": len(assignments),
            "pending": sum(1 for s in submissions if s["grade"] == "Pending"),
        }
        return render_template(
            "dashboard.html", assignments=assignments, notes=notes, exams=exams, students=students,
            submissions=submissions, attendance=attendance, results=results, stats=stats,
            default_password=USING_DEFAULT_PASSWORD, tz_label=TZ_LABEL)

    uid = session["user_id"]
    submissions = db.execute(
        """SELECT s.*, a.title AS assignment_title FROM submissions s
           LEFT JOIN assignments a ON s.assignment_id = a.id
           WHERE s.student_id = ? ORDER BY s.id DESC""", (uid,)).fetchall()
    taken = {r["exam_id"]: r for r in db.execute(
        "SELECT * FROM exam_results WHERE student_id = ?", (uid,)).fetchall()}
    stats = {
        "open_exams": sum(1 for e in exams if not e["closed"] and e["id"] not in taken and e["q_count"]),
        "open_assignments": sum(1 for a in assignments if not a["closed"]),
        "submitted": len(submissions),
    }
    return render_template(
        "dashboard.html", assignments=assignments, notes=notes, exams=exams, submissions=submissions,
        taken=taken, stats=stats, tz_label=TZ_LABEL)


# --------------------------------------------------------------------------- #
#  Exams / CBT
# --------------------------------------------------------------------------- #
@app.route("/create_exam", methods=["POST"])
@role_required("teacher")
def create_exam():
    title = request.form.get("title", "").strip()
    due = request.form.get("due_date", "").strip()
    if not title or not parse_dt(due):
        flash("Please provide an exam title and a valid deadline.", "error")
        return redirect(url_for("dashboard") + "#exams")
    db = get_db()
    db.execute("INSERT INTO exams (title, due_date) VALUES (?, ?)", (title, due))
    db.commit()
    flash("Exam created. Add questions with the “Add question” button.", "success")
    return redirect(url_for("dashboard") + "#exams")


@app.route("/add_question", methods=["POST"])
@role_required("teacher")
def add_question():
    f = request.form
    exam_id = f.get("exam_id", type=int)
    fields = [f.get(k, "").strip() for k in ("question", "opt_a", "opt_b", "opt_c", "opt_d")]
    correct = f.get("correct_opt", "")
    db = get_db()
    if (not exam_id or not all(fields) or correct not in ("A", "B", "C", "D")
            or not db.execute("SELECT 1 FROM exams WHERE id = ?", (exam_id,)).fetchone()):
        flash("Please fill in the question, all four options and the correct answer.", "error")
        return redirect(url_for("dashboard") + "#exams")
    db.execute(
        "INSERT INTO exam_questions (exam_id, question, opt_a, opt_b, opt_c, opt_d, correct_opt) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)", (exam_id, *fields, correct))
    db.commit()
    flash("Question added successfully.", "success")
    return redirect(url_for("dashboard") + "#exams")


@app.route("/delete_exam/<int:exam_id>", methods=["POST"])
@role_required("teacher")
def delete_exam(exam_id):
    db = get_db()
    db.execute("DELETE FROM exams WHERE id = ?", (exam_id,))
    db.commit()
    flash("Exam deleted.", "success")
    return redirect(url_for("dashboard") + "#exams")


def _exam_gate(db, exam_id):
    """Return (exam, redirect_response). Handles missing / closed / already-taken exams."""
    exam = db.execute("SELECT * FROM exams WHERE id = ?", (exam_id,)).fetchone()
    if not exam:
        flash("That exam does not exist.", "error")
        return None, redirect(url_for("dashboard") + "#exams")
    if db.execute("SELECT 1 FROM exam_results WHERE exam_id = ? AND student_id = ?",
                  (exam_id, session["user_id"])).fetchone():
        flash("You have already taken this exam.", "info")
        return None, redirect(url_for("dashboard") + "#exams")
    if is_closed(exam["due_date"]):
        flash("This exam has closed.", "error")
        return None, redirect(url_for("dashboard") + "#exams")
    return exam, None


@app.route("/take_exam/<int:exam_id>")
@role_required("student")
def take_exam(exam_id):
    db = get_db()
    exam, bounce = _exam_gate(db, exam_id)
    if bounce:
        return bounce
    questions = db.execute("SELECT * FROM exam_questions WHERE exam_id = ? ORDER BY id", (exam_id,)).fetchall()
    return render_template("exam.html", exam=exam, questions=questions, tz_label=TZ_LABEL)


@app.route("/submit_exam/<int:exam_id>", methods=["POST"])
@role_required("student")
def submit_exam(exam_id):
    db = get_db()
    exam, bounce = _exam_gate(db, exam_id)
    if bounce:
        return bounce
    questions = db.execute("SELECT id, correct_opt FROM exam_questions WHERE exam_id = ?", (exam_id,)).fetchall()
    score = sum(1 for q in questions if request.form.get(f"q_{q['id']}") == q["correct_opt"])
    db.execute(
        "INSERT INTO exam_results (exam_id, student_id, score, total, submitted_at) VALUES (?, ?, ?, ?, ?)",
        (exam_id, session["user_id"], score, len(questions), now().strftime("%Y-%m-%d %H:%M:%S")))
    db.commit()
    flash(f"Exam submitted! You scored {score}/{len(questions)}.", "success")
    return redirect(url_for("dashboard") + "#exams")


# --------------------------------------------------------------------------- #
#  Student IDs (teacher)
# --------------------------------------------------------------------------- #
@app.route("/add_student_id", methods=["POST"])
@role_required("teacher")
def add_student_id():
    student_id = request.form.get("student_id", "").strip()[:40]
    name = request.form.get("name", "").strip()[:80]
    if not student_id or not name:
        flash("Student name and ID are both required.", "error")
        return redirect(url_for("dashboard") + "#students")
    db = get_db()
    if db.execute("SELECT 1 FROM authorized_students WHERE student_id = ? COLLATE NOCASE", (student_id,)).fetchone():
        flash("That Student ID already exists.", "error")
    else:
        db.execute("INSERT INTO authorized_students (student_id, name) VALUES (?, ?)", (student_id, name))
        db.commit()
        flash(f"ID {student_id} issued to {name}.", "success")
    return redirect(url_for("dashboard") + "#students")


@app.route("/delete_student_id/<int:row_id>", methods=["POST"])
@role_required("teacher")
def delete_student_id(row_id):
    db = get_db()
    row = db.execute("SELECT student_id FROM authorized_students WHERE id = ?", (row_id,)).fetchone()
    if row:
        # attendance has a foreign key to the student, so its rows must go first
        db.execute("DELETE FROM attendance WHERE student_id = ?", (row["student_id"],))
        db.execute("DELETE FROM authorized_students WHERE id = ?", (row_id,))
        db.commit()
        flash("Student ID revoked.", "success")
    return redirect(url_for("dashboard") + "#students")


# --------------------------------------------------------------------------- #
#  Assignments, notes, submissions, grading
# --------------------------------------------------------------------------- #
@app.route("/create_assignment", methods=["POST"])
@role_required("teacher")
def create_assignment():
    title = request.form.get("title", "").strip()
    instructions = request.form.get("instructions", "").strip()
    due = request.form.get("due_date", "").strip()
    if not title or not parse_dt(due):
        flash("Please provide a title and a valid due date.", "error")
        return redirect(url_for("dashboard") + "#assignments")
    stored = ""
    file = request.files.get("file")
    if file and file.filename:
        stored, err = save_upload(file, UPLOAD_ASSIGNMENTS)
        if err:
            flash(err, "error")
            return redirect(url_for("dashboard") + "#assignments")
    db = get_db()
    db.execute("INSERT INTO assignments (title, filename, instructions, due_date) VALUES (?, ?, ?, ?)",
               (title, stored, instructions, due))
    db.commit()
    flash("Assignment created.", "success")
    return redirect(url_for("dashboard") + "#assignments")


@app.route("/edit_assignment/<int:aid>", methods=["POST"])
@role_required("teacher")
def edit_assignment(aid):
    title = request.form.get("title", "").strip()
    instructions = request.form.get("instructions", "").strip()
    due = request.form.get("due_date", "").strip()
    db = get_db()
    current = db.execute("SELECT filename FROM assignments WHERE id = ?", (aid,)).fetchone()
    if not current or not title or not parse_dt(due):
        flash("Please provide a title and a valid due date.", "error")
        return redirect(url_for("dashboard") + "#assignments")
    file = request.files.get("file")
    if file and file.filename:
        stored, err = save_upload(file, UPLOAD_ASSIGNMENTS)
        if err:
            flash(err, "error")
            return redirect(url_for("dashboard") + "#assignments")
        remove_file(UPLOAD_ASSIGNMENTS, current["filename"])
        db.execute("UPDATE assignments SET title=?, instructions=?, due_date=?, filename=? WHERE id=?",
                   (title, instructions, due, stored, aid))
    else:
        db.execute("UPDATE assignments SET title=?, instructions=?, due_date=? WHERE id=?",
                   (title, instructions, due, aid))
    db.commit()
    flash("Assignment updated.", "success")
    return redirect(url_for("dashboard") + "#assignments")


@app.route("/delete_assignment/<int:aid>", methods=["POST"])
@role_required("teacher")
def delete_assignment(aid):
    db = get_db()
    a = db.execute("SELECT filename FROM assignments WHERE id = ?", (aid,)).fetchone()
    if a:
        remove_file(UPLOAD_ASSIGNMENTS, a["filename"])
        # submissions reference the assignment, so they are removed with it
        for s in db.execute("SELECT filename FROM submissions WHERE assignment_id = ?", (aid,)).fetchall():
            remove_file(UPLOAD_SUBMISSIONS, s["filename"])
        db.execute("DELETE FROM submissions WHERE assignment_id = ?", (aid,))
        db.execute("DELETE FROM assignments WHERE id = ?", (aid,))
        db.commit()
        flash("Assignment and its submissions deleted.", "success")
    return redirect(url_for("dashboard") + "#assignments")


@app.route("/upload_note", methods=["POST"])
@role_required("teacher")
def upload_note():
    title = request.form.get("title", "").strip()
    file = request.files.get("file")
    if not title or not file or not file.filename:
        flash("Please provide a title and choose a file.", "error")
        return redirect(url_for("dashboard") + "#notes")
    stored, err = save_upload(file, UPLOAD_NOTES)
    if err:
        flash(err, "error")
        return redirect(url_for("dashboard") + "#notes")
    db = get_db()
    db.execute("INSERT INTO notes (title, filename) VALUES (?, ?)", (title, stored))
    db.commit()
    flash("Note uploaded.", "success")
    return redirect(url_for("dashboard") + "#notes")


@app.route("/delete_note/<int:nid>", methods=["POST"])
@role_required("teacher")
def delete_note(nid):
    db = get_db()
    n = db.execute("SELECT filename FROM notes WHERE id = ?", (nid,)).fetchone()
    if n:
        remove_file(UPLOAD_NOTES, n["filename"])
        db.execute("DELETE FROM notes WHERE id = ?", (nid,))
        db.commit()
        flash("Note deleted.", "success")
    return redirect(url_for("dashboard") + "#notes")


@app.route("/submit_work", methods=["POST"])
@role_required("student")
def submit_work():
    aid = request.form.get("assignment_id", type=int)
    db = get_db()
    a = db.execute("SELECT due_date FROM assignments WHERE id = ?", (aid,)).fetchone() if aid else None
    if not a:
        flash("Please select an assignment.", "error")
        return redirect(url_for("dashboard") + "#submissions")
    if is_closed(a["due_date"]):
        flash("The deadline has passed. Submission rejected.", "error")
        return redirect(url_for("dashboard") + "#submissions")
    file = request.files.get("file")
    if not file or not file.filename:
        flash("Please choose a file to submit.", "error")
        return redirect(url_for("dashboard") + "#submissions")
    stored, err = save_upload(file, UPLOAD_SUBMISSIONS)
    if err:
        flash(err, "error")
        return redirect(url_for("dashboard") + "#submissions")
    db.execute("INSERT INTO submissions (assignment_id, student_id, filename, grade, feedback) VALUES (?, ?, ?, ?, ?)",
               (aid, session["user_id"], stored, "Pending", "No feedback yet"))
    db.commit()
    flash("Work submitted successfully.", "success")
    return redirect(url_for("dashboard") + "#submissions")


@app.route("/grade_submission/<int:sid>", methods=["POST"])
@role_required("teacher")
def grade_submission(sid):
    grade = request.form.get("grade", "").strip()[:20]
    feedback = request.form.get("feedback", "").strip()[:500] or "No feedback yet"
    if not grade:
        flash("Enter a grade first.", "error")
        return redirect(url_for("dashboard") + "#submissions")
    db = get_db()
    db.execute("UPDATE submissions SET grade = ?, feedback = ? WHERE id = ?", (grade, feedback, sid))
    db.commit()
    flash("Grade saved.", "success")
    return redirect(url_for("dashboard") + "#submissions")


@app.route("/download/<folder_type>/<path:filename>")
@login_required
def download_file(folder_type, filename):
    if folder_type not in FOLDERS:
        abort(404)
    db = get_db()
    row = db.execute(f"SELECT * FROM {folder_type} WHERE filename = ?", (filename,)).fetchone()
    if not row:
        abort(404)
    if folder_type == "submissions" and session["role"] != "teacher" and row["student_id"] != session["user_id"]:
        abort(403)
    return send_from_directory(FOLDERS[folder_type], filename, as_attachment=True,
                               download_name=display_name(filename))


# --------------------------------------------------------------------------- #
#  Error pages
# --------------------------------------------------------------------------- #
ERRORS = {
    400: ("Bad request", "The request could not be understood."),
    403: ("Access denied", "You are not allowed to view this."),
    404: ("Page not found", "The page or file you asked for does not exist."),
    405: ("Method not allowed", "That action is not available this way."),
    500: ("Something went wrong", "An unexpected error occurred. Please try again."),
}


def _error(code):
    title, text = ERRORS.get(code, ("Error", "Something went wrong."))
    return render_template("error.html", code=code, title=title, text=text), code


for _code in ERRORS:
    app.register_error_handler(_code, lambda e, c=_code: _error(c))


@app.errorhandler(413)
def too_large(_e):
    flash(f"That file is too large (limit {MAX_UPLOAD_MB} MB).", "error")
    return redirect(url_for("dashboard") if "role" in session else url_for("index"))


# --------------------------------------------------------------------------- #
#  Local / LAN launcher
# --------------------------------------------------------------------------- #
def get_lan_ips():
    """Best-effort list of this machine's LAN IPv4 addresses, primary first."""
    ips = []

    def add(ip):
        if ip and not ip.startswith(("127.", "169.254.", "0.")) and ip not in ips:
            ips.append(ip)

    # Connecting a UDP socket sends no packets; it only asks the OS which interface would be used.
    for target in ("8.8.8.8", "10.255.255.255", "192.168.255.255"):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.settimeout(0.3)
            s.connect((target, 1))
            add(s.getsockname()[0])
        except OSError:
            pass
        finally:
            s.close()
    try:
        for ip in socket.gethostbyname_ex(socket.gethostname())[2]:
            add(ip)
    except OSError:
        pass
    return ips


def port_is_free(port, host="0.0.0.0"):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind((host, port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def print_banner(host, port, server_name):
    interactive = sys.stdout.isatty()
    if interactive:
        os.system("cls" if os.name == "nt" else "clear")
    colors = ["\033[95m", "\033[94m", "\033[96m", "\033[92m", "\033[93m", "\033[91m"]
    reset, bold = "\033[0m", "\033[1m"
    logo = r"""
 ███████╗██████╗ ██╗   ██╗██████╗  ██████╗ ██████╗ ████████╗ █████╗ ██╗
 ██╔════╝██╔══██╗██║   ██║██╔══██╗██╔═══██╗██╔══██╗╚══██╔══╝██╔══██╗██║
 █████╗  ██║  ██║██║   ██║██████╔╝██║   ██║██████╔╝   ██║   ███████║██║
 ██╔══╝  ██║  ██║██║   ██║██╔═══╝ ██║   ██║██╔══██╗   ██║   ██╔══██║██║
 ███████╗██████╔╝╚██████╔╝██║     ╚██████╔╝██║  ██║   ██║   ██║  ██║███████╗
 ╚══════╝╚═════╝  ╚═════╝ ╚═╝      ╚═════╝ ╚═╝  ╚═╝   ╚═╝   ╚═╝  ╚═╝╚══════╝"""
    try:
        for i, line in enumerate(logo.strip("\n").split("\n")):
            sys.stdout.write(colors[i % len(colors)] + line + reset + "\n")
            if interactive:
                time.sleep(0.06)
    except UnicodeEncodeError:
        print("EDUPORTAL")
    print(f"\n{bold}\033[96m>>> EduPortal System by JusticeTech <<<{reset}\n")
    print(f"\033[1;92m[✓]{reset} Database ready  \033[2m({DB_PATH}){reset}")
    print(f"\033[1;92m[✓]{reset} Server: {server_name}   Timezone: {TZ_LABEL}")
    if USING_DEFAULT_PASSWORD:
        print(f"\033[1;93m[!]{reset} Teacher password is the default (admin123). "
              f"Set TEACHER_PASSWORD (see .env.example) before sharing with students.")
    print()

    if host in ("127.0.0.1", "localhost"):
        print(f"{bold}Running in local-only mode (this computer only):{reset}")
        print(f"   \033[1;97mhttp://localhost:{port}{reset}\n")
        return

    ips = get_lan_ips()
    print(f"{bold}Open on THIS computer:{reset}")
    print(f"   \033[1;97mhttp://localhost:{port}{reset}\n")
    if ips:
        print(f"{bold}Share with phones/laptops on the SAME Wi-Fi / LAN cable:{reset}")
        print(f"   \033[1;92mhttp://{ips[0]}:{port}{reset}   <-- give students this link")
        for extra in ips[1:]:
            print(f"   \033[2mhttp://{extra}:{port}   (other network adapter){reset}")
    else:
        print("\033[1;93m[!]{0} Could not detect a LAN address. Are you connected to Wi-Fi / a network?".format(reset))
    print(f"\n\033[2mIf other devices cannot connect, allow Python through your firewall "
          f"(see README > Troubleshooting).  Press Ctrl+C to stop.{reset}\n")


def main():
    parser = argparse.ArgumentParser(description="Run EduPortal locally / on your LAN.")
    parser.add_argument("--host", default=os.environ.get("HOST"), help="Bind address (default 0.0.0.0 = whole LAN)")
    parser.add_argument("--port", type=int, default=None, help="Port (default 5000, or the next free one)")
    parser.add_argument("--local-only", action="store_true", help="Only allow this computer (127.0.0.1)")
    args = parser.parse_args()

    host = "127.0.0.1" if args.local_only else (args.host or "0.0.0.0")
    explicit_port = args.port or (int(os.environ["PORT"]) if os.environ.get("PORT") else None)

    if explicit_port:
        port = explicit_port
        if not port_is_free(port, host):
            sys.exit(f"[x] Port {port} is already in use. Pick another with --port.")
    else:
        port = next((p for p in range(5000, 5021) if port_is_free(p, host)), None)
        if port is None:
            sys.exit("[x] No free port between 5000-5020. Use --port to choose one.")

    try:
        from waitress import serve
        server_name = "Waitress (production-grade)"
    except ImportError:
        serve = None
        server_name = "Flask built-in (run `pip install -r requirements.txt` for a faster server)"

    print_banner(host, port, server_name)
    try:
        if serve:
            logging.getLogger("waitress").setLevel(logging.ERROR)
            serve(app, host=host, port=port, threads=8)
        else:
            import flask.cli
            flask.cli.show_server_banner = lambda *a, **k: None  # keep the terminal clean
            app.run(host=host, port=port, debug=False, threaded=True, use_reloader=False)
    except KeyboardInterrupt:
        print("\nEduPortal stopped.")


if __name__ == "__main__":
    main()
