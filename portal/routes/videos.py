"""Video lessons: upload, byte-range streaming, server-verified watch tracking, completion reports."""
import json
import math
import os
import shutil
import subprocess

from flask import (Blueprint, abort, flash, g, jsonify, redirect, render_template, request,
                   send_file, url_for)

from .. import config
from ..db import audit, get_db, now
from ..security import login_required, staff_required, student_required
from ..services import apply_heartbeat, completion_threshold, covered_seconds
from ..utils import (can_view, class_clause, csv_response, int_or_none, remove_file, save_upload)

bp = Blueprint("videos", __name__)

MIME = {"mp4": "video/mp4", "m4v": "video/mp4", "webm": "video/webm", "ogv": "video/ogg"}


def _probe_duration(path):
    """Ask ffprobe for the real duration when it is installed (more trustworthy than the browser)."""
    exe = shutil.which("ffprobe")
    if not exe:
        return None
    try:
        out = subprocess.run([exe, "-v", "error", "-show_entries", "format=duration", "-of",
                              "default=nw=1:nk=1", path], capture_output=True, text=True, timeout=20)
        value = float(out.stdout.strip())
        return value if math.isfinite(value) and value > 0 else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def _go(url):
    """The upload form posts via XHR (for a progress bar); give it JSON, plain posts get a redirect."""
    if request.headers.get("X-Requested-With") == "XHR-upload":
        return jsonify(ok=True, redirect=url)
    return redirect(url)


def _video_or_404(db, vid):
    v = db.execute("SELECT * FROM videos WHERE id = ?", (vid,)).fetchone()
    if not v or (g.user["kind"] != "staff" and not can_view(v)):
        abort(404)
    return v


def _progress_view(row, duration):
    watched = row["watched"] if row else 0
    return {"watched": watched, "percent": min(100, round(100 * watched / duration)) if duration else 0,
            "completed": bool(row and row["completed_at"])}


# --------------------------------------------------------------------------- #
#  Library
# --------------------------------------------------------------------------- #
@bp.route("/videos")
@login_required
def library():
    db = get_db()
    clause, params = class_clause("v")
    rows = db.execute(
        f"""SELECT v.*, c.name AS class_name, s.name AS subject_name FROM videos v
            LEFT JOIN classes c ON c.id = v.class_id LEFT JOIN subjects s ON s.id = v.subject_id
            WHERE {clause} ORDER BY v.id DESC""", params).fetchall()
    items = []
    if g.user["kind"] == "student":
        prog = {p["video_id"]: p for p in db.execute("SELECT * FROM video_progress WHERE student_id = ?",
                                                    (g.user["code"],)).fetchall()}
        for v in rows:
            items.append({"v": v, **_progress_view(prog.get(v["id"]), v["duration"])})
    else:
        for v in rows:
            target = (db.execute("SELECT COUNT(*) FROM authorized_students WHERE COALESCE(active,1)=1 AND class_id = ?",
                                 (v["class_id"],)).fetchone()[0] if v["class_id"] else
                      db.execute("SELECT COUNT(*) FROM authorized_students WHERE COALESCE(active,1)=1").fetchone()[0])
            done = db.execute("SELECT COUNT(*) FROM video_progress WHERE video_id = ? AND completed_at IS NOT NULL",
                              (v["id"],)).fetchone()[0]
            items.append({"v": v, "target": target, "done": done})
    classes = db.execute("SELECT * FROM classes ORDER BY name").fetchall()
    subjects = db.execute("SELECT * FROM subjects ORDER BY name").fetchall()
    return render_template("videos.html", items=items, classes=classes, subjects=subjects)


# --------------------------------------------------------------------------- #
#  Staff: upload / edit / delete
# --------------------------------------------------------------------------- #
@bp.route("/videos/upload", methods=["POST"])
@staff_required
def upload():
    title = request.form.get("title", "").strip()[:120]
    description = request.form.get("description", "").strip()[:2000]
    class_id, subject_id = int_or_none(request.form.get("class_id")), int_or_none(request.form.get("subject_id"))
    file = request.files.get("file")
    if not title or not file or not file.filename:
        flash("Please give the video a title and choose a file.", "error")
        return _go(url_for("videos.library"))
    stored, err = save_upload(file, config.FOLDERS["videos"], config.VIDEO_EXTENSIONS, max_mb=config.MAX_VIDEO_MB)
    if err:
        flash(err, "error")
        return _go(url_for("videos.library"))
    path = os.path.join(config.FOLDERS["videos"], stored)
    try:
        client_duration = float(request.form.get("duration", "0"))
    except ValueError:
        client_duration = 0.0
    duration = _probe_duration(path) or (client_duration if math.isfinite(client_duration) else 0.0)
    if duration <= 0:
        remove_file(config.FOLDERS["videos"], stored)
        flash("Could not read the video's length. Use an MP4 (H.264/AAC) or WebM file.", "error")
        return _go(url_for("videos.library"))
    db = get_db()
    cur = db.execute("""INSERT INTO videos (title, description, filename, duration, size, class_id, subject_id,
                        created_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                     (title, description, stored, round(duration, 2), os.path.getsize(path), class_id, subject_id,
                      g.user["name"], now().strftime("%Y-%m-%d %H:%M")))
    db.commit()
    audit("video_uploaded", f"{title} ({round(duration)}s)")
    flash("Video uploaded. Students can stream it now.", "success")
    return _go(url_for("videos.report", vid=cur.lastrowid))


@bp.route("/videos/<int:vid>/edit", methods=["POST"])
@staff_required
def edit(vid):
    db = get_db()
    if not db.execute("SELECT 1 FROM videos WHERE id = ?", (vid,)).fetchone():
        abort(404)
    title = request.form.get("title", "").strip()[:120]
    if not title:
        flash("A video needs a title.", "error")
    else:
        db.execute("UPDATE videos SET title=?, description=?, class_id=?, subject_id=? WHERE id=?",
                   (title, request.form.get("description", "").strip()[:2000],
                    int_or_none(request.form.get("class_id")), int_or_none(request.form.get("subject_id")), vid))
        db.commit()
        audit("video_edited", title)
        flash("Video updated.", "success")
    return redirect(url_for("videos.library"))


@bp.route("/videos/<int:vid>/delete", methods=["POST"])
@staff_required
def delete(vid):
    db = get_db()
    v = db.execute("SELECT * FROM videos WHERE id = ?", (vid,)).fetchone()
    if v:
        remove_file(config.FOLDERS["videos"], v["filename"])
        db.execute("DELETE FROM video_progress WHERE video_id = ?", (vid,))
        db.execute("DELETE FROM videos WHERE id = ?", (vid,))
        db.commit()
        audit("video_deleted", v["title"])
        flash("Video and its viewing records deleted.", "success")
    return redirect(url_for("videos.library"))


# --------------------------------------------------------------------------- #
#  Watching
# --------------------------------------------------------------------------- #
@bp.route("/videos/<int:vid>")
@login_required
def watch(vid):
    db = get_db()
    v = _video_or_404(db, vid)
    tracked = g.user["kind"] == "student"
    prog = None
    resume, furthest = 0.0, 0.0
    if tracked:
        prog = db.execute("SELECT * FROM video_progress WHERE video_id = ? AND student_id = ?",
                          (vid, g.user["code"])).fetchone()
        if prog:
            ranges = json.loads(prog["covered"] or "[]")
            furthest = max((r[1] for r in ranges), default=0.0)
            resume = 0.0 if prog["completed_at"] else min(prog["last_pos"] or 0.0, furthest)
    ext = v["filename"].rsplit(".", 1)[-1].lower()
    return render_template("video_watch.html", v=v, tracked=tracked, resume=resume, furthest=furthest,
                           mime=MIME.get(ext, "video/mp4"), progress=_progress_view(prog, v["duration"]),
                           threshold=round(completion_threshold(v["duration"])))


@bp.route("/videos/<int:vid>/stream")
@login_required
def stream(vid):
    db = get_db()
    v = _video_or_404(db, vid)
    path = os.path.join(config.FOLDERS["videos"], v["filename"])
    if not os.path.isfile(path):
        abort(404)
    ext = v["filename"].rsplit(".", 1)[-1].lower()
    # conditional=True gives HTTP Range support (206 Partial Content), required for seeking/streaming.
    resp = send_file(path, mimetype=MIME.get(ext, "video/mp4"), conditional=True, max_age=0)
    resp.headers["Cache-Control"] = "private, no-store"
    return resp


@bp.route("/videos/<int:vid>/progress", methods=["POST"])
@student_required
def progress(vid):
    db = get_db()
    v = _video_or_404(db, vid)
    data = request.get_json(silent=True) or {}
    try:
        position = float(data.get("position", 0))
    except (TypeError, ValueError):
        return jsonify(ok=False, error="bad position"), 400
    state = data.get("state")
    if not math.isfinite(position) or state not in ("playing", "paused", "ended"):
        return jsonify(ok=False, error="bad request"), 400

    sid = g.user["code"]
    duration = float(v["duration"] or 0)
    row = db.execute("SELECT * FROM video_progress WHERE video_id = ? AND student_id = ?", (vid, sid)).fetchone()
    stamp = now().strftime("%Y-%m-%d %H:%M:%S")
    if not row:
        db.execute("INSERT OR IGNORE INTO video_progress (video_id, student_id, first_seen, last_seen) VALUES (?, ?, ?, ?)",
                   (vid, sid, stamp, stamp))
        row = db.execute("SELECT * FROM video_progress WHERE video_id = ? AND student_id = ?", (vid, sid)).fetchone()

    import time as _time
    ranges, last_pos, last_ts, _credited = apply_heartbeat(
        row, position, duration, _time.time(), "playing" if state in ("playing", "ended") else "paused")
    watched = covered_seconds(ranges)
    completed_at = row["completed_at"]
    if not completed_at and watched >= completion_threshold(duration):
        completed_at = stamp
        audit("video_completed", v["title"])
    db.execute("""UPDATE video_progress SET covered = ?, watched = ?, completed_at = ?, last_seen = ?,
                  last_pos = ?, last_ts = ? WHERE id = ?""",
               (json.dumps([[round(a, 2), round(b, 2)] for a, b in ranges]), round(watched, 2), completed_at,
                stamp, last_pos, last_ts, row["id"]))
    db.commit()
    return jsonify(ok=True, watched=round(watched, 1), duration=duration,
                   percent=min(100, round(100 * watched / duration)) if duration else 0,
                   completed=bool(completed_at))


# --------------------------------------------------------------------------- #
#  Who watched? (teacher)
# --------------------------------------------------------------------------- #
def _report_rows(db, v, class_filter=None):
    sql = """SELECT s.id AS srow, s.student_id, s.name, c.name AS class_name, p.watched, p.completed_at,
                    p.first_seen, p.last_seen
             FROM authorized_students s LEFT JOIN classes c ON c.id = s.class_id
             LEFT JOIN video_progress p ON p.video_id = ? AND p.student_id = s.student_id
             WHERE COALESCE(s.active, 1) = 1"""
    params = [v["id"]]
    if v["class_id"]:
        sql += " AND s.class_id = ?"
        params.append(v["class_id"])
    elif class_filter:
        sql += " AND s.class_id = ?"
        params.append(class_filter)
    rows = []
    for r in db.execute(sql + " ORDER BY c.name, s.name", params).fetchall():
        d = dict(r)
        d["percent"] = min(100, round(100 * (r["watched"] or 0) / v["duration"])) if v["duration"] else 0
        d["status"] = "Completed" if r["completed_at"] else ("In progress" if (r["watched"] or 0) > 0 else "Not started")
        rows.append(d)
    return rows


@bp.route("/videos/<int:vid>/report")
@staff_required
def report(vid):
    db = get_db()
    v = db.execute("""SELECT v.*, c.name AS class_name FROM videos v LEFT JOIN classes c ON c.id = v.class_id
                      WHERE v.id = ?""", (vid,)).fetchone()
    if not v:
        abort(404)
    class_filter = int_or_none(request.args.get("class_id"))
    rows = _report_rows(db, v, class_filter)
    summary = {"total": len(rows), "completed": sum(1 for r in rows if r["status"] == "Completed"),
               "progress": sum(1 for r in rows if r["status"] == "In progress"),
               "not_started": sum(1 for r in rows if r["status"] == "Not started")}
    classes = db.execute("SELECT * FROM classes ORDER BY name").fetchall()
    from ..db import get_int_setting
    return render_template("video_report.html", v=v, rows=rows, summary=summary, classes=classes,
                           class_filter=class_filter, completion_pct=get_int_setting("video_completion_pct", 95))


@bp.route("/videos/<int:vid>/report.csv")
@staff_required
def report_csv(vid):
    db = get_db()
    v = db.execute("SELECT * FROM videos WHERE id = ?", (vid,)).fetchone()
    if not v:
        abort(404)
    rows = _report_rows(db, v, int_or_none(request.args.get("class_id")))
    audit("export", f"video report: {v['title']}")
    return csv_response(f"video-{vid}-viewing-report.csv",
                        ["full_name", "student_id", "class", "status", "percent_watched", "seconds_watched",
                         "completed_at", "first_seen", "last_seen"],
                        [[r["name"], r["student_id"], r["class_name"] or "", r["status"], r["percent"],
                          round(r["watched"] or 0), r["completed_at"] or "", r["first_seen"] or "",
                          r["last_seen"] or ""] for r in rows])
