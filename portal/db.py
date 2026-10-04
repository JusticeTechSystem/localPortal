"""SQLite access, schema + automatic migrations, runtime settings, audit log and time helpers."""
import os
import shutil
import sqlite3
import time
from datetime import datetime
from functools import lru_cache

from flask import g, has_request_context, request
from werkzeug.security import generate_password_hash

from . import config

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None

DEFAULT_SETTINGS = {
    "school_name": "EduPortal",
    "tagline": "Attendance · CBT exams · Assignments · Class notes",
    "accent": "#4338ca",
    "timezone": "",
    "logo": "",
    "video_completion_pct": "95",
    "backup_enabled": "1",
    "backup_keep": "14",
}


# --------------------------------------------------------------------------- #
#  Connections
# --------------------------------------------------------------------------- #
def connect():
    conn = sqlite3.connect(config.DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def get_db():
    if "db" not in g:
        g.db = connect()
    return g.db


def close_db(_exc=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


# --------------------------------------------------------------------------- #
#  Settings (cached for a few seconds so every request does not hit the DB)
# --------------------------------------------------------------------------- #
_cache = {"at": 0.0, "data": dict(DEFAULT_SETTINGS)}


def _load_settings():
    try:
        conn = connect()
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
        conn.close()
        data = dict(DEFAULT_SETTINGS)
        data.update({r["key"]: r["value"] for r in rows})
        _cache.update(at=time.time(), data=data)
    except sqlite3.Error:
        pass


def get_setting(key, default=None):
    if time.time() - _cache["at"] > 15:
        _load_settings()
    return _cache["data"].get(key, DEFAULT_SETTINGS.get(key, default))


def get_int_setting(key, default):
    try:
        return int(get_setting(key, default))
    except (TypeError, ValueError):
        return default


def set_setting(key, value, db=None):
    own = db is None
    db = db or connect()
    db.execute("INSERT INTO settings (key, value) VALUES (?, ?) "
               "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, str(value)))
    db.commit()
    if own:
        db.close()
    _load_settings()


# --------------------------------------------------------------------------- #
#  Time (ONE timezone for deadlines/attendance, whatever the server uses)
# --------------------------------------------------------------------------- #
@lru_cache(maxsize=16)
def _zone(name):
    if name and ZoneInfo:
        try:
            return ZoneInfo(name)
        except Exception:
            return None
    return None


def timezone_name():
    return (get_setting("timezone") or os.environ.get("PORTAL_TIMEZONE", "")).strip()


def valid_timezone(name):
    return bool(name) and _zone(name) is not None


def tz_label():
    name = timezone_name()
    return name if _zone(name) else "server local time"


def now():
    tz = _zone(timezone_name())
    return datetime.now(tz).replace(tzinfo=None) if tz else datetime.now()


def to_epoch(dt):
    """Convert a naive portal-local datetime to a UNIX timestamp."""
    tz = _zone(timezone_name())
    return dt.replace(tzinfo=tz).timestamp() if tz else time.mktime(dt.timetuple())


def parse_dt(value):
    if not value:
        return None
    for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None


def is_closed(due_date):
    due = parse_dt(due_date)
    return bool(due and now() > due)


# --------------------------------------------------------------------------- #
#  Audit log
# --------------------------------------------------------------------------- #
def audit(action, detail="", actor=None, role=None):
    """Record who did what. Never raises: logging must not break the action itself."""
    try:
        user = getattr(g, "user", None) if has_request_context() else None
        actor = actor or (user["name"] if user else "system")
        role = role or (user["role"] if user else "system")
        ip = request.remote_addr if has_request_context() else ""
        db = get_db() if has_request_context() else connect()
        db.execute("INSERT INTO audit_log (ts, actor, role, action, detail, ip) VALUES (?, ?, ?, ?, ?, ?)",
                   (now().strftime("%Y-%m-%d %H:%M:%S"), actor, role, action, str(detail)[:500], ip or ""))
        db.commit()
        if not has_request_context():
            db.close()
    except Exception:
        pass


# --------------------------------------------------------------------------- #
#  Schema + migrations
# --------------------------------------------------------------------------- #
def _add_column(c, table, column, ddl):
    cols = [r[1] for r in c.execute(f"PRAGMA table_info({table})").fetchall()]
    if column not in cols:
        c.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def _migrate_legacy_files():
    """Very old versions kept classroom.db and uploads/ next to app.py - copy them over once."""
    legacy_db = os.path.join(config.BASE_DIR, "classroom.db")
    if (os.path.isfile(legacy_db) and not os.path.isfile(config.DB_PATH)
            and os.path.abspath(config.DATA_DIR) != config.BASE_DIR):
        shutil.copy2(legacy_db, config.DB_PATH)
    legacy_uploads = os.path.join(config.BASE_DIR, "uploads")
    if os.path.isdir(legacy_uploads) and os.path.abspath(config.UPLOAD_ROOT) != os.path.abspath(legacy_uploads):
        for sub, dest in config.FOLDERS.items():
            src = os.path.join(legacy_uploads, sub)
            if not os.path.isdir(src):
                continue
            for name in os.listdir(src):
                src_file, dst = os.path.join(src, name), os.path.join(dest, name)
                if os.path.isfile(src_file) and not os.path.exists(dst):
                    try:
                        shutil.copy2(src_file, dst)
                    except OSError:
                        pass


def init_db():
    config.ensure_dirs()
    _migrate_legacy_files()
    conn = sqlite3.connect(config.DB_PATH, timeout=15)
    c = conn.cursor()
    try:
        c.execute("PRAGMA journal_mode=WAL")
    except sqlite3.DatabaseError:
        pass

    # ---- tables that existed in earlier versions (kept compatible) ----
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

    # ---- new tables ----
    c.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
    c.execute('''CREATE TABLE IF NOT EXISTS staff (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT UNIQUE NOT NULL COLLATE NOCASE,
                    full_name TEXT NOT NULL,
                    password_hash TEXT NOT NULL,
                    role TEXT NOT NULL DEFAULT 'teacher',
                    active INTEGER NOT NULL DEFAULT 1,
                    must_change INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT, last_login TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS classes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT UNIQUE NOT NULL COLLATE NOCASE, created_at TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS subjects (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT UNIQUE NOT NULL COLLATE NOCASE)''')
    c.execute('''CREATE TABLE IF NOT EXISTS exam_attempts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    exam_id INTEGER NOT NULL, student_id TEXT NOT NULL,
                    started_at REAL NOT NULL, seed INTEGER NOT NULL,
                    UNIQUE(exam_id, student_id),
                    FOREIGN KEY(exam_id) REFERENCES exams(id) ON DELETE CASCADE)''')
    c.execute('''CREATE TABLE IF NOT EXISTS exam_answers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    result_id INTEGER NOT NULL, question_id INTEGER NOT NULL,
                    answer TEXT, is_correct INTEGER DEFAULT 0, points INTEGER DEFAULT 0,
                    FOREIGN KEY(result_id) REFERENCES exam_results(id) ON DELETE CASCADE)''')
    c.execute('''CREATE TABLE IF NOT EXISTS announcements (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL, body TEXT, class_id INTEGER,
                    pinned INTEGER DEFAULT 0, created_by TEXT, created_at TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS videos (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL, description TEXT, filename TEXT NOT NULL,
                    duration REAL DEFAULT 0, size INTEGER DEFAULT 0,
                    class_id INTEGER, subject_id INTEGER, created_by TEXT, created_at TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS video_progress (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    video_id INTEGER NOT NULL, student_id TEXT NOT NULL,
                    covered TEXT DEFAULT '[]', watched REAL DEFAULT 0,
                    completed_at TEXT, first_seen TEXT, last_seen TEXT,
                    last_pos REAL DEFAULT 0, last_ts REAL DEFAULT 0,
                    UNIQUE(video_id, student_id),
                    FOREIGN KEY(video_id) REFERENCES videos(id) ON DELETE CASCADE)''')
    c.execute('''CREATE TABLE IF NOT EXISTS audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT, actor TEXT, role TEXT, action TEXT, detail TEXT, ip TEXT)''')

    # ---- columns added to older tables ----
    for col, ddl in (("class_id", "INTEGER"), ("pin_hash", "TEXT"), ("must_change_pin", "INTEGER DEFAULT 0"),
                     ("photo", "TEXT"), ("active", "INTEGER DEFAULT 1"), ("created_at", "TEXT")):
        _add_column(c, "authorized_students", col, ddl)
    for col, ddl in (("class_id", "INTEGER"), ("subject_id", "INTEGER"), ("duration_min", "INTEGER DEFAULT 0"),
                     ("shuffle", "INTEGER DEFAULT 1"), ("review_mode", "TEXT DEFAULT 'after_deadline'"),
                     ("created_by", "TEXT"), ("created_at", "TEXT")):
        _add_column(c, "exams", col, ddl)
    for col, ddl in (("qtype", "TEXT DEFAULT 'mcq'"), ("image", "TEXT"), ("points", "INTEGER DEFAULT 1")):
        _add_column(c, "exam_questions", col, ddl)
    _add_column(c, "exam_results", "status", "TEXT DEFAULT 'submitted'")
    for col, ddl in (("class_id", "INTEGER"), ("subject_id", "INTEGER"), ("late_policy", "TEXT DEFAULT 'reject'"),
                     ("late_penalty", "INTEGER DEFAULT 0"), ("created_by", "TEXT"), ("created_at", "TEXT")):
        _add_column(c, "assignments", col, ddl)
    for col, ddl in (("late", "INTEGER DEFAULT 0"), ("penalty_pct", "INTEGER DEFAULT 0"), ("submitted_at", "TEXT")):
        _add_column(c, "submissions", col, ddl)
    for col, ddl in (("class_id", "INTEGER"), ("subject_id", "INTEGER"), ("created_by", "TEXT"), ("created_at", "TEXT")):
        _add_column(c, "notes", col, ddl)

    # Older versions allowed duplicate results; only enforce uniqueness when the data allows it.
    try:
        c.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_results_exam_student ON exam_results(exam_id, student_id)")
    except sqlite3.IntegrityError:
        pass
    c.execute("CREATE INDEX IF NOT EXISTS ix_attendance_date ON attendance(login_date)")
    c.execute("CREATE INDEX IF NOT EXISTS ix_audit_id ON audit_log(id DESC)")

    # ---- bootstrap the first administrator ----
    if c.execute("SELECT COUNT(*) FROM staff").fetchone()[0] == 0:
        pw = config.ADMIN_PASSWORD_ENV or config.DEFAULT_ADMIN_PASSWORD
        c.execute("INSERT INTO staff (username, full_name, password_hash, role, must_change, created_at) "
                  "VALUES ('admin', 'Administrator', ?, 'admin', ?, ?)",
                  (generate_password_hash(pw), 1 if config.USING_DEFAULT_PASSWORD else 0,
                   datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
    conn.commit()
    conn.close()
    _load_settings()


def reset_admin_password(new_password, username="admin"):
    """Recovery helper used by `python app.py --reset-admin-password`."""
    conn = connect()
    row = conn.execute("SELECT id FROM staff WHERE username = ?", (username,)).fetchone()
    if row:
        conn.execute("UPDATE staff SET password_hash = ?, must_change = 0, active = 1 WHERE id = ?",
                     (generate_password_hash(new_password), row["id"]))
    else:
        conn.execute("INSERT INTO staff (username, full_name, password_hash, role, created_at) "
                     "VALUES (?, 'Administrator', ?, 'admin', ?)",
                     (username, generate_password_hash(new_password), now().strftime("%Y-%m-%d %H:%M:%S")))
    conn.commit()
    conn.close()
    audit("admin_password_reset_cli", f"username={username}", actor="console", role="system")
