"""Dashboard (overview) plus small infrastructure endpoints (health, PWA manifest, theme, logo)."""
import os

from flask import (Blueprint, Response, abort, g, jsonify, render_template, send_from_directory)

from .. import config
from ..db import get_db, get_setting, is_closed
from ..security import login_required
from ..services import finalize_expired_attempts
from ..utils import class_clause, shade

bp = Blueprint("main", __name__)


@bp.route("/healthz")
def healthz():
    return "ok", 200, {"Content-Type": "text/plain"}


@bp.route("/offline")
def offline():
    return render_template("offline.html")


@bp.route("/dashboard")
@login_required
def dashboard():
    db = get_db()
    clause, params = class_clause("a")
    announcements = db.execute(
        f"""SELECT a.*, c.name AS class_name FROM announcements a LEFT JOIN classes c ON c.id = a.class_id
            WHERE {clause} ORDER BY a.pinned DESC, a.id DESC LIMIT 5""", params).fetchall()

    if g.user["kind"] == "staff":
        pending = db.execute("SELECT COUNT(*) FROM submissions WHERE grade = 'Pending'").fetchone()[0]
        stats = {
            "students": db.execute("SELECT COUNT(*) FROM authorized_students WHERE COALESCE(active,1)=1").fetchone()[0],
            "classes": db.execute("SELECT COUNT(*) FROM classes").fetchone()[0],
            "exams": db.execute("SELECT COUNT(*) FROM exams").fetchone()[0],
            "assignments": db.execute("SELECT COUNT(*) FROM assignments").fetchone()[0],
            "videos": db.execute("SELECT COUNT(*) FROM videos").fetchone()[0],
            "pending": pending,
        }
        finalize_expired_attempts(db)
        recent_subs = db.execute(
            """SELECT s.id, s.grade, a.title, st.name FROM submissions s
               LEFT JOIN assignments a ON a.id = s.assignment_id
               LEFT JOIN authorized_students st ON st.student_id = s.student_id
               ORDER BY s.id DESC LIMIT 5""").fetchall()
        recent_results = db.execute(
            """SELECT r.score, r.total, r.status, e.title, st.name FROM exam_results r
               JOIN exams e ON e.id = r.exam_id
               LEFT JOIN authorized_students st ON st.student_id = r.student_id
               ORDER BY r.id DESC LIMIT 5""").fetchall()
        return render_template("dashboard_staff.html", stats=stats, announcements=announcements,
                               recent_subs=recent_subs, recent_results=recent_results)

    sid = g.user["code"]
    finalize_expired_attempts(db, sid)
    ec, ep = class_clause("e")
    exams = [e for e in db.execute(
        f"""SELECT e.*, (SELECT COUNT(*) FROM exam_questions q WHERE q.exam_id = e.id) AS q_count,
                   (SELECT COUNT(*) FROM exam_results r WHERE r.exam_id = e.id AND r.student_id = ?) AS taken
            FROM exams e WHERE {ec} ORDER BY e.due_date""", (sid,) + ep).fetchall()
        if e["q_count"] and not e["taken"] and not is_closed(e["due_date"])]
    ac, ap = class_clause("a")
    assignments = [a for a in db.execute(
        f"""SELECT a.*, (SELECT COUNT(*) FROM submissions s WHERE s.assignment_id = a.id AND s.student_id = ?) AS mine
            FROM assignments a WHERE {ac} ORDER BY a.due_date""", (sid,) + ap).fetchall()
        if not a["mine"] and (a["late_policy"] != "reject" or not is_closed(a["due_date"]))]
    vc, vp = class_clause("v")
    videos = db.execute(
        f"""SELECT v.* FROM videos v LEFT JOIN video_progress p ON p.video_id = v.id AND p.student_id = ?
            WHERE {vc} AND p.completed_at IS NULL ORDER BY v.id DESC LIMIT 6""", (sid,) + vp).fetchall()
    present = db.execute("SELECT COUNT(DISTINCT login_date) FROM attendance WHERE student_id = ?", (sid,)).fetchone()[0]
    return render_template("dashboard_student.html", announcements=announcements, exams=exams,
                           assignments=assignments, videos=videos, present=present)


# --------------------------------------------------------------------------- #
#  Branding / PWA
# --------------------------------------------------------------------------- #
@bp.route("/theme.css")
def theme_css():
    accent = get_setting("accent") or "#4338ca"
    css = (f":root{{--primary:{accent};--primary-dark:{shade(accent, -0.2)};--primary-soft:{shade(accent, 0.9)};}}\n"
           f"[data-theme=dark]{{--primary:{shade(accent, 0.38)};--primary-dark:{shade(accent, 0.2)};"
           f"--primary-soft:{shade(accent, -0.68)};}}\n")
    return Response(css, mimetype="text/css", headers={"Cache-Control": "no-cache"})


@bp.route("/branding/logo")
def branding_logo():
    name = get_setting("logo")
    if not name or not os.path.isfile(os.path.join(config.FOLDERS["branding"], name)):
        abort(404)
    resp = send_from_directory(config.FOLDERS["branding"], name, max_age=300)
    resp.headers["Cache-Control"] = "public, max-age=300"
    return resp


@bp.route("/manifest.webmanifest")
def manifest():
    name = get_setting("school_name") or "EduPortal"
    accent = get_setting("accent") or "#4338ca"
    data = {
        "name": name, "short_name": name[:12], "description": get_setting("tagline"),
        "start_url": "/dashboard", "scope": "/", "display": "standalone",
        "background_color": "#f4f6fb", "theme_color": accent,
        "icons": [
            {"src": "/static/icons/icon-192.png", "sizes": "192x192", "type": "image/png"},
            {"src": "/static/icons/icon-512.png", "sizes": "512x512", "type": "image/png"},
            {"src": "/static/icons/maskable-512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"},
        ],
    }
    resp = jsonify(data)
    resp.mimetype = "application/manifest+json"
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@bp.route("/sw.js")
def service_worker():
    resp = send_from_directory(os.path.join(config.BASE_DIR, "static"), "sw.js", mimetype="text/javascript")
    resp.headers["Service-Worker-Allowed"] = "/"
    resp.headers["Cache-Control"] = "no-cache"
    return resp
