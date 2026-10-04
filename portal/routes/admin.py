"""Administrator tools: school settings (name/logo/colour/timezone), audit log, backups."""
import os
import re

from flask import (Blueprint, abort, flash, redirect, render_template, request, send_file,
                   send_from_directory, url_for)

from .. import backups, config
from ..db import audit, get_db, get_int_setting, get_setting, set_setting, valid_timezone
from ..security import admin_required
from ..utils import csv_response, hex_color_ok, page_args, remove_file, save_upload

bp = Blueprint("admin", __name__)


# --------------------------------------------------------------------------- #
#  Settings
# --------------------------------------------------------------------------- #
@bp.route("/settings", methods=["GET", "POST"])
@admin_required
def settings():
    if request.method == "POST":
        f = request.form
        school_name = f.get("school_name", "").strip()[:60]
        tagline = f.get("tagline", "").strip()[:120]
        accent = f.get("accent", "").strip()
        timezone = f.get("timezone", "").strip()
        problems = []
        if not school_name:
            problems.append("The school / class name is required.")
        if not hex_color_ok(accent):
            problems.append("Pick a valid accent colour.")
        if timezone and not valid_timezone(timezone):
            problems.append(f"'{timezone}' is not a valid timezone (use names like Africa/Lagos or Europe/London).")
        pct = get_int_setting("video_completion_pct", 95)
        try:
            pct = min(100, max(50, int(f.get("video_completion_pct", pct))))
        except ValueError:
            problems.append("Video completion must be a number between 50 and 100.")
        keep = min(60, max(1, int(f.get("backup_keep") or 14))) if (f.get("backup_keep") or "14").isdigit() else 14

        logo_name = None
        file = request.files.get("logo")
        if file and file.filename and not problems:
            logo_name, err = save_upload(file, config.FOLDERS["branding"], config.IMAGE_EXTENSIONS,
                                         max_mb=config.MAX_IMAGE_MB, image=True)
            if err:
                problems.append(err)
        if problems:
            for p in problems:
                flash(p, "error")
            if logo_name:
                remove_file(config.FOLDERS["branding"], logo_name)
            return redirect(url_for("admin.settings"))

        db = get_db()
        set_setting("school_name", school_name, db)
        set_setting("tagline", tagline, db)
        set_setting("accent", accent, db)
        set_setting("timezone", timezone, db)
        set_setting("video_completion_pct", pct, db)
        set_setting("backup_enabled", "1" if f.get("backup_enabled") else "0", db)
        set_setting("backup_keep", keep, db)
        if logo_name:
            remove_file(config.FOLDERS["branding"], get_setting("logo"))
            set_setting("logo", logo_name, db)
        elif f.get("remove_logo"):
            remove_file(config.FOLDERS["branding"], get_setting("logo"))
            set_setting("logo", "", db)
        audit("settings_changed", f"name='{school_name}', timezone='{timezone or 'server'}', video_pct={pct}")
        flash("Settings saved.", "success")
        return redirect(url_for("admin.settings"))
    return render_template("settings.html", s={
        "school_name": get_setting("school_name"), "tagline": get_setting("tagline"),
        "accent": get_setting("accent"), "timezone": get_setting("timezone"),
        "video_completion_pct": get_int_setting("video_completion_pct", 95),
        "backup_enabled": get_setting("backup_enabled") == "1", "backup_keep": get_int_setting("backup_keep", 14),
        "has_logo": bool(get_setting("logo"))})


# --------------------------------------------------------------------------- #
#  Audit log
# --------------------------------------------------------------------------- #
def _audit_query():
    q = request.args.get("q", "").strip()
    sql, params = " FROM audit_log WHERE 1=1", []
    if q:
        sql += " AND (action LIKE ? OR detail LIKE ? OR actor LIKE ?)"
        params += [f"%{q}%"] * 3
    return q, sql, params


@bp.route("/audit")
@admin_required
def audit_log():
    db = get_db()
    q, sql, params = _audit_query()
    page, per, offset = page_args(request, 50)
    total = db.execute("SELECT COUNT(*)" + sql, params).fetchone()[0]
    rows = db.execute("SELECT *" + sql + " ORDER BY id DESC LIMIT ? OFFSET ?", params + [per, offset]).fetchall()
    return render_template("audit.html", rows=rows, q=q, page=page, pages=max(1, -(-total // per)), total=total)


@bp.route("/audit.csv")
@admin_required
def audit_csv():
    q, sql, params = _audit_query()
    rows = get_db().execute("SELECT ts, actor, role, action, detail, ip" + sql + " ORDER BY id DESC LIMIT 20000", params)
    return csv_response("audit-log.csv", ["time", "actor", "role", "action", "detail", "ip"], [list(r) for r in rows])


# --------------------------------------------------------------------------- #
#  Backups
# --------------------------------------------------------------------------- #
@bp.route("/backups")
@admin_required
def backups_page():
    return render_template("backups.html", items=backups.list_backups(),
                           enabled=get_setting("backup_enabled") == "1")


@bp.route("/backups/create", methods=["POST"])
@admin_required
def backup_create():
    name = backups.create_scheduled_backup(reason="manual")
    flash(f"Snapshot saved on the server: {name}", "success")
    return redirect(url_for("admin.backups_page"))


@bp.route("/backups/download", methods=["POST"])
@admin_required
def backup_download():
    include_videos = bool(request.form.get("include_videos"))
    path = backups.build_zip(include_videos)
    audit("backup_downloaded", "with videos" if include_videos else "without videos")
    stamp = re.sub(r"[^0-9]", "", get_setting("school_name") or "") or "portal"
    resp = send_file(path, mimetype="application/zip", as_attachment=True,
                     download_name=f"eduportal-backup-{__import__('time').strftime('%Y%m%d-%H%M')}.zip")
    resp.call_on_close(lambda: os.path.exists(path) and os.remove(path))
    return resp


@bp.route("/backups/file/<name>")
@admin_required
def backup_file(name):
    if not re.fullmatch(r"classroom-\d{8}-\d{6}\.db", name):
        abort(404)
    audit("backup_downloaded", name)
    return send_from_directory(config.BACKUP_DIR, name, as_attachment=True)
