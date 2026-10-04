"""Attendance reports (date range), printable PDF report cards and CSV exports."""
import io
import os
from datetime import timedelta

from flask import (Blueprint, Response, abort, g, render_template, request, send_file, url_for)

from .. import config
from ..db import audit, get_db, get_setting, now, parse_dt
from ..security import login_required, staff_required
from ..services import attendance_summary
from ..utils import csv_response, final_grade, fmt_dt, fmt_secs, int_or_none
from .people import student_summary

bp = Blueprint("reports", __name__)


# --------------------------------------------------------------------------- #
#  Attendance
# --------------------------------------------------------------------------- #
def _range_args():
    today = now().date()
    start = request.args.get("from") or (today - timedelta(days=29)).isoformat()
    end = request.args.get("to") or today.isoformat()
    if not (parse_dt(start + "T00:00") and parse_dt(end + "T00:00")):
        start, end = (today - timedelta(days=29)).isoformat(), today.isoformat()
    if start > end:
        start, end = end, start
    return start, end, int_or_none(request.args.get("class_id"))


@bp.route("/attendance")
@staff_required
def attendance():
    db = get_db()
    start, end, class_id = _range_args()
    days, summary = attendance_summary(db, start, end, class_id)
    sql = """SELECT a.login_date, a.login_time, a.student_id, s.name, s.id AS srow, c.name AS class_name
             FROM attendance a LEFT JOIN authorized_students s ON s.student_id = a.student_id
             LEFT JOIN classes c ON c.id = s.class_id WHERE a.login_date BETWEEN ? AND ?"""
    params = [start, end]
    if class_id:
        sql += " AND s.class_id = ?"
        params.append(class_id)
    log = db.execute(sql + " ORDER BY a.login_date DESC, a.login_time DESC LIMIT 500", params).fetchall()
    classes = db.execute("SELECT * FROM classes ORDER BY name").fetchall()
    return render_template("attendance.html", start=start, end=end, class_id=class_id, days=days,
                           summary=summary, log=log, classes=classes)


@bp.route("/attendance.csv")
@staff_required
def attendance_csv():
    db = get_db()
    start, end, class_id = _range_args()
    audit("export", f"attendance {start}..{end}")
    if request.args.get("kind") == "log":
        sql = """SELECT a.login_date, a.login_time, a.student_id, s.name, c.name FROM attendance a
                 LEFT JOIN authorized_students s ON s.student_id = a.student_id
                 LEFT JOIN classes c ON c.id = s.class_id WHERE a.login_date BETWEEN ? AND ?"""
        params = [start, end]
        if class_id:
            sql += " AND s.class_id = ?"
            params.append(class_id)
        rows = [[r[0], r[1], r[2], r[3] or "", r[4] or ""] for r in
                db.execute(sql + " ORDER BY a.login_date, a.login_time", params)]
        return csv_response(f"attendance-log-{start}-to-{end}.csv",
                            ["date", "time", "student_id", "full_name", "class"], rows)
    _days, summary = attendance_summary(db, start, end, class_id)
    return csv_response(f"attendance-summary-{start}-to-{end}.csv",
                        ["full_name", "student_id", "class", "days_present", "school_days", "percent"],
                        [[r["name"], r["student_id"], r["class_name"], r["present"], r["total"], r["percent"]]
                         for r in summary])


# --------------------------------------------------------------------------- #
#  Exports
# --------------------------------------------------------------------------- #
@bp.route("/export/students.csv")
@staff_required
def export_students():
    rows = get_db().execute(
        """SELECT s.name, s.student_id, c.name, COALESCE(s.active, 1), s.created_at FROM authorized_students s
           LEFT JOIN classes c ON c.id = s.class_id ORDER BY c.name, s.name""").fetchall()
    audit("export", "students")
    return csv_response("students.csv", ["full_name", "student_id", "class", "active", "created"],
                        [[r[0], r[1], r[2] or "", "yes" if r[3] else "no", r[4] or ""] for r in rows])


@bp.route("/export/results.csv")
@staff_required
def export_results():
    rows = get_db().execute(
        """SELECT s.name, r.student_id, c.name, e.title, r.score, r.total, r.status, r.submitted_at
           FROM exam_results r JOIN exams e ON e.id = r.exam_id
           LEFT JOIN authorized_students s ON s.student_id = r.student_id
           LEFT JOIN classes c ON c.id = s.class_id ORDER BY e.id, c.name, s.name""").fetchall()
    audit("export", "all exam results")
    return csv_response("exam-results.csv",
                        ["full_name", "student_id", "class", "exam", "score", "total", "percent", "status", "submitted_at"],
                        [[r[0] or "", r[1], r[2] or "", r[3], r[4], r[5], round(100 * r[4] / r[5]) if r[5] else 0, r[6], r[7]]
                         for r in rows])


@bp.route("/export/grades.csv")
@staff_required
def export_grades():
    rows = get_db().execute(
        """SELECT s.name, x.student_id, c.name, a.title, x.grade, x.penalty_pct, x.late, x.feedback, x.submitted_at
           FROM submissions x LEFT JOIN assignments a ON a.id = x.assignment_id
           LEFT JOIN authorized_students s ON s.student_id = x.student_id
           LEFT JOIN classes c ON c.id = s.class_id ORDER BY a.id, c.name, s.name""").fetchall()
    audit("export", "assignment grades")
    return csv_response("assignment-grades.csv",
                        ["full_name", "student_id", "class", "assignment", "grade", "late_penalty_pct",
                         "final_grade_after_penalty", "late", "feedback", "submitted_at"],
                        [[r[0] or "", r[1], r[2] or "", r[3] or "", r[4], r[5] or 0, final_grade(r[4], r[5]) or "",
                          "yes" if r[6] else "no", r[7], r[8] or ""] for r in rows])


# --------------------------------------------------------------------------- #
#  Report card
# --------------------------------------------------------------------------- #
def _load_student(db, sid):
    if g.user["kind"] == "student" and g.user["id"] != sid:
        abort(403)
    row = db.execute("""SELECT s.*, c.name AS class_name FROM authorized_students s
                        LEFT JOIN classes c ON c.id = s.class_id WHERE s.id = ?""", (sid,)).fetchone()
    if not row:
        abort(404)
    return row


@bp.route("/report-card/<int:sid>")
@login_required
def report_card(sid):
    db = get_db()
    row = _load_student(db, sid)
    summary = student_summary(db, row)
    if request.args.get("format") != "html":
        pdf = _build_pdf(row, summary)
        if pdf:
            return send_file(io.BytesIO(pdf), mimetype="application/pdf", as_attachment=True,
                             download_name=f"report-card-{row['student_id']}.pdf")
    return render_template("report_card.html", s=row, summary=summary, today=now().strftime("%d %B %Y"))


def _register_fonts():
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    fonts = os.path.join(config.BASE_DIR, "static", "fonts")
    if "DejaVu" not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont("DejaVu", os.path.join(fonts, "DejaVuSans.ttf")))
        pdfmetrics.registerFont(TTFont("DejaVu-Bold", os.path.join(fonts, "DejaVuSans-Bold.ttf")))
        pdfmetrics.registerFontFamily("DejaVu", normal="DejaVu", bold="DejaVu-Bold")


def _build_pdf(s, summary):
    """Server-side PDF with reportlab. Returns bytes, or None if reportlab is not installed."""
    try:
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import ParagraphStyle
        from reportlab.lib.units import mm
        from reportlab.platypus import Image, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
        _register_fonts()
    except Exception:
        return None

    accent = colors.HexColor(get_setting("accent") or "#4338ca")
    base = ParagraphStyle("base", fontName="DejaVu", fontSize=9.5, leading=13)
    small = ParagraphStyle("small", parent=base, fontSize=8, textColor=colors.HexColor("#64748b"))
    h1 = ParagraphStyle("h1", parent=base, fontName="DejaVu-Bold", fontSize=20, leading=24, textColor=accent)
    h2 = ParagraphStyle("h2", parent=base, fontName="DejaVu-Bold", fontSize=12, leading=16, spaceBefore=12, spaceAfter=4)
    cell = ParagraphStyle("cell", parent=base, fontSize=8.5, leading=11)

    def p(text, style=cell):
        from xml.sax.saxutils import escape
        return Paragraph(escape(str(text if text is not None else "")), style)

    def table(data, widths):
        t = Table(data, colWidths=widths, repeatRows=1)
        t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), accent), ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTNAME", (0, 0), (-1, 0), "DejaVu-Bold"), ("FONTSIZE", (0, 0), (-1, -1), 8.5),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f4f6fb")]),
            ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#d5dbe6")), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4)]))
        return t

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm, topMargin=16 * mm,
                            bottomMargin=16 * mm, title=f"Report card - {s['name']}", author=get_setting("school_name"))
    story = []

    head_left = [Paragraph(_x(get_setting("school_name")), h1), Paragraph("Student report card", base),
                 Paragraph(f"Generated {now().strftime('%d %B %Y')}", small)]
    photo_path = os.path.join(config.FOLDERS["photos"], s["photo"]) if s["photo"] else None
    if photo_path and os.path.isfile(photo_path):
        try:
            img = Image(photo_path, width=24 * mm, height=28 * mm)
            img.hAlign = "RIGHT"
            head = Table([[head_left, img]], colWidths=[130 * mm, 28 * mm])
        except Exception:
            head = Table([[head_left]], colWidths=[158 * mm])
    else:
        head = Table([[head_left]], colWidths=[158 * mm])
    head.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP")]))
    story += [head, Spacer(1, 6)]

    info = [[p("Name", small), p(s["name"], base), p("Student ID", small), p(s["student_id"], base)],
            [p("Class", small), p(s["class_name"] or "—", base), p("Attendance", small),
             p(f"{summary['present']} of {summary['school_days']} days"
               + (f" ({summary['attendance_pct']}%)" if summary["attendance_pct"] is not None else ""), base)],
            [p("Exam average", small), p(f"{summary['average']}%" if summary["average"] is not None else "—", base),
             p("Videos completed", small),
             p(f"{sum(1 for v in summary['videos'] if v['completed_at'])} of {len(summary['videos'])}", base)]]
    it = Table(info, colWidths=[28 * mm, 52 * mm, 28 * mm, 50 * mm])
    it.setStyle(TableStyle([("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#d5dbe6")),
                            ("INNERGRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#e5e9f1")),
                            ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#f8fafc")),
                            ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4)]))
    story.append(it)

    story.append(Paragraph("CBT exam results", h2))
    if summary["exams"]:
        data = [["Exam", "Score", "Percent", "Date"]] + [
            [p(e["title"]), f"{e['score']} / {e['total']}", f"{round(100 * e['score'] / e['total']) if e['total'] else 0}%",
             (e["submitted_at"] or "")[:10]] for e in summary["exams"]]
        story.append(table(data, [78 * mm, 28 * mm, 24 * mm, 28 * mm]))
    else:
        story.append(Paragraph("No exams taken yet.", small))

    story.append(Paragraph("Assignments", h2))
    if summary["subs"]:
        data = [["Assignment", "Grade", "Note"]]
        for x in summary["subs"]:
            fg = final_grade(x["grade"], x["penalty_pct"])
            grade = f"{x['grade']}" + (f" → {fg} after {x['penalty_pct']}% late penalty" if fg else "")
            data.append([p(x["title"] or "Deleted assignment"), p(grade),
                         p(("Late. " if x["late"] else "") + (x["feedback"] if x["feedback"] != "No feedback yet" else ""))])
        story.append(table(data, [62 * mm, 40 * mm, 56 * mm]))
    else:
        story.append(Paragraph("No work submitted yet.", small))

    story += [Spacer(1, 26), Table([["", ""], ["Teacher's signature", "Date"]], colWidths=[80 * mm, 50 * mm],
                                   style=TableStyle([("LINEABOVE", (0, 1), (0, 1), 0.5, colors.black),
                                                     ("LINEABOVE", (1, 1), (1, 1), 0.5, colors.black),
                                                     ("FONTNAME", (0, 0), (-1, -1), "DejaVu"),
                                                     ("FONTSIZE", (0, 0), (-1, -1), 8),
                                                     ("ROWHEIGHTS", (0, 0), (-1, 0), 18)]))]
    doc.build(story)
    return buf.getvalue()


def _x(text):
    from xml.sax.saxutils import escape
    return escape(text or "")
