"""Exams (timed, shuffled, 3 question types, review), assignments + late policy, submissions,
notes, announcements and protected downloads."""
import os
import random
import sqlite3
import time

from flask import (Blueprint, abort, flash, g, redirect, render_template, request,
                   send_from_directory, url_for)

from .. import config
from ..db import audit, get_db, is_closed, now, parse_dt
from ..security import login_required, staff_required, student_required
from ..services import (attempt_end, exam_total_points, finalize_expired_attempts, ordered_questions,
                        review_allowed, score_exam)
from ..utils import (can_view, class_clause, csv_response, display_name, int_or_none, read_csv_upload,
                     remove_file, save_upload)

bp = Blueprint("academics", __name__)

REVIEW_MODES = (("after_deadline", "After the deadline"), ("after_submit", "Right after submitting"),
                ("off", "Never (hide answers)"))


def form_lists(db):
    return (db.execute("SELECT * FROM classes ORDER BY name").fetchall(),
            db.execute("SELECT * FROM subjects ORDER BY name").fetchall())


def _decorate(rows, key="due_date"):
    out = []
    for r in rows:
        d = dict(r)
        d["closed"] = is_closed(d.get(key))
        out.append(d)
    return out


def _target_fields():
    return int_or_none(request.form.get("class_id")), int_or_none(request.form.get("subject_id"))


# =========================================================================== #
#  EXAMS
# =========================================================================== #
@bp.route("/exams")
@login_required
def exams():
    db = get_db()
    clause, params = class_clause("e")
    sid = g.user["code"] if g.user["kind"] == "student" else None
    finalize_expired_attempts(db, sid)
    rows = _decorate(db.execute(
        f"""SELECT e.*, c.name AS class_name, s.name AS subject_name,
                   (SELECT COUNT(*) FROM exam_questions q WHERE q.exam_id = e.id) AS q_count,
                   (SELECT COUNT(*) FROM exam_results r WHERE r.exam_id = e.id) AS result_count
            FROM exams e LEFT JOIN classes c ON c.id = e.class_id LEFT JOIN subjects s ON s.id = e.subject_id
            WHERE {clause} ORDER BY e.id DESC""", params).fetchall())
    classes, subjects = form_lists(db)
    mine, attempts = {}, {}
    if sid:
        mine = {r["exam_id"]: dict(r) for r in db.execute("SELECT * FROM exam_results WHERE student_id = ?", (sid,))}
        attempts = {a["exam_id"]: a for a in db.execute("SELECT * FROM exam_attempts WHERE student_id = ?", (sid,))}
        for e in rows:
            e["review_ok"] = e["id"] in mine and review_allowed(e)
    return render_template("exams.html", exams=rows, classes=classes, subjects=subjects, mine=mine,
                           attempts=attempts, review_modes=REVIEW_MODES)


def _exam_form_values():
    title = request.form.get("title", "").strip()[:120]
    due = request.form.get("due_date", "").strip()
    duration = max(0, min(600, int_or_none(request.form.get("duration_min")) or 0))
    review = request.form.get("review_mode", "after_deadline")
    if review not in dict(REVIEW_MODES):
        review = "after_deadline"
    shuffle = 1 if request.form.get("shuffle") else 0
    class_id, subject_id = _target_fields()
    return title, due, duration, review, shuffle, class_id, subject_id


@bp.route("/exams/create", methods=["POST"])
@staff_required
def create_exam():
    title, due, duration, review, shuffle, class_id, subject_id = _exam_form_values()
    if not title or not parse_dt(due):
        flash("Please provide an exam title and a valid deadline.", "error")
        return redirect(url_for("academics.exams"))
    db = get_db()
    cur = db.execute(
        """INSERT INTO exams (title, due_date, duration_min, shuffle, review_mode, class_id, subject_id,
                              created_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (title, due, duration, shuffle, review, class_id, subject_id, g.user["name"], now().strftime("%Y-%m-%d")))
    db.commit()
    audit("exam_created", title)
    flash("Exam created. Now add its questions.", "success")
    return redirect(url_for("academics.exam_questions", eid=cur.lastrowid))


@bp.route("/exams/<int:eid>/edit", methods=["POST"])
@staff_required
def edit_exam(eid):
    title, due, duration, review, shuffle, class_id, subject_id = _exam_form_values()
    db = get_db()
    if not db.execute("SELECT 1 FROM exams WHERE id = ?", (eid,)).fetchone():
        abort(404)
    if not title or not parse_dt(due):
        flash("Please provide an exam title and a valid deadline.", "error")
    else:
        db.execute("""UPDATE exams SET title=?, due_date=?, duration_min=?, review_mode=?, shuffle=?,
                      class_id=?, subject_id=? WHERE id=?""",
                   (title, due, duration, review, shuffle, class_id, subject_id, eid))
        db.commit()
        audit("exam_edited", title)
        flash("Exam updated.", "success")
    return redirect(url_for("academics.exams"))


@bp.route("/exams/<int:eid>/delete", methods=["POST"])
@staff_required
def delete_exam(eid):
    db = get_db()
    exam = db.execute("SELECT title FROM exams WHERE id = ?", (eid,)).fetchone()
    if exam:
        for q in db.execute("SELECT image FROM exam_questions WHERE exam_id = ?", (eid,)).fetchall():
            remove_file(config.FOLDERS["questions"], q["image"])
        db.execute("DELETE FROM exam_answers WHERE result_id IN (SELECT id FROM exam_results WHERE exam_id = ?)", (eid,))
        db.execute("DELETE FROM exams WHERE id = ?", (eid,))
        db.commit()
        audit("exam_deleted", exam["title"])
        flash("Exam deleted.", "success")
    return redirect(url_for("academics.exams"))


# ---- questions ----------------------------------------------------------- #
def _parse_question(form):
    qtype = form.get("qtype", "mcq")
    text = form.get("question", "").strip()[:1000]
    points = max(1, min(100, int_or_none(form.get("points")) or 1))
    if not text:
        return None, "The question text is required."
    data = {"qtype": qtype, "question": text, "points": points,
            "opt_a": "", "opt_b": "", "opt_c": "", "opt_d": "", "correct_opt": ""}
    if qtype == "mcq":
        for k in ("opt_a", "opt_b", "opt_c", "opt_d"):
            data[k] = form.get(k, "").strip()[:300]
        if not all(data[k] for k in ("opt_a", "opt_b", "opt_c", "opt_d")):
            return None, "Multiple-choice questions need all four options."
        if form.get("correct_opt") not in ("A", "B", "C", "D"):
            return None, "Pick the correct option."
        data["correct_opt"] = form["correct_opt"]
    elif qtype == "tf":
        if form.get("correct_tf") not in ("A", "B"):
            return None, "Choose True or False as the correct answer."
        data["opt_a"], data["opt_b"], data["correct_opt"] = "True", "False", form["correct_tf"]
    elif qtype == "short":
        accepted = "|".join(a.strip() for a in form.get("accepted", "").split("|") if a.strip())[:300]
        if not accepted:
            return None, "Enter at least one accepted answer (separate alternatives with |)."
        data["correct_opt"] = accepted
    else:
        return None, "Unknown question type."
    return data, None


def _exam_or_404(db, eid):
    exam = db.execute("SELECT * FROM exams WHERE id = ?", (eid,)).fetchone()
    if not exam:
        abort(404)
    return exam


@bp.route("/exams/<int:eid>/questions")
@staff_required
def exam_questions(eid):
    db = get_db()
    exam = _exam_or_404(db, eid)
    qs = db.execute("SELECT * FROM exam_questions WHERE exam_id = ? ORDER BY id", (eid,)).fetchall()
    return render_template("exam_questions.html", exam=exam, questions=qs, total=exam_total_points(qs))


@bp.route("/exams/<int:eid>/questions/add", methods=["POST"])
@staff_required
def add_question(eid):
    db = get_db()
    _exam_or_404(db, eid)
    data, err = _parse_question(request.form)
    image = None
    if not err:
        file = request.files.get("image")
        if file and file.filename:
            image, err = save_upload(file, config.FOLDERS["questions"], config.IMAGE_EXTENSIONS,
                                     max_mb=config.MAX_IMAGE_MB, image=True)
    if err:
        flash(err, "error")
    else:
        db.execute("""INSERT INTO exam_questions (exam_id, question, opt_a, opt_b, opt_c, opt_d, correct_opt,
                      qtype, image, points) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                   (eid, data["question"], data["opt_a"], data["opt_b"], data["opt_c"], data["opt_d"],
                    data["correct_opt"], data["qtype"], image, data["points"]))
        db.commit()
        audit("question_added", f"exam {eid}")
        flash("Question added.", "success")
    return redirect(url_for("academics.exam_questions", eid=eid))


@bp.route("/questions/<int:qid>/edit", methods=["POST"])
@staff_required
def edit_question(qid):
    db = get_db()
    q = db.execute("SELECT * FROM exam_questions WHERE id = ?", (qid,)).fetchone()
    if not q:
        abort(404)
    data, err = _parse_question(request.form)
    new_image = q["image"]
    if not err:
        if request.form.get("remove_image"):
            remove_file(config.FOLDERS["questions"], q["image"])
            new_image = None
        file = request.files.get("image")
        if file and file.filename:
            stored, err = save_upload(file, config.FOLDERS["questions"], config.IMAGE_EXTENSIONS,
                                      max_mb=config.MAX_IMAGE_MB, image=True)
            if not err:
                remove_file(config.FOLDERS["questions"], new_image)
                new_image = stored
    if err:
        flash(err, "error")
    else:
        db.execute("""UPDATE exam_questions SET question=?, opt_a=?, opt_b=?, opt_c=?, opt_d=?, correct_opt=?,
                      qtype=?, image=?, points=? WHERE id=?""",
                   (data["question"], data["opt_a"], data["opt_b"], data["opt_c"], data["opt_d"],
                    data["correct_opt"], data["qtype"], new_image, data["points"], qid))
        db.commit()
        audit("question_edited", f"question {qid}")
        flash("Question updated. (Scores already recorded are not recalculated.)", "success")
    return redirect(url_for("academics.exam_questions", eid=q["exam_id"]))


@bp.route("/questions/<int:qid>/delete", methods=["POST"])
@staff_required
def delete_question(qid):
    db = get_db()
    q = db.execute("SELECT * FROM exam_questions WHERE id = ?", (qid,)).fetchone()
    if q:
        remove_file(config.FOLDERS["questions"], q["image"])
        db.execute("DELETE FROM exam_questions WHERE id = ?", (qid,))
        db.commit()
        audit("question_deleted", f"question {qid} of exam {q['exam_id']}")
        flash("Question deleted.", "success")
        return redirect(url_for("academics.exam_questions", eid=q["exam_id"]))
    return redirect(url_for("academics.exams"))


@bp.route("/exams/<int:eid>/questions/import", methods=["POST"])
@staff_required
def import_questions(eid):
    db = get_db()
    _exam_or_404(db, eid)
    file = request.files.get("file")
    if not file or not file.filename:
        flash("Choose a CSV file first.", "error")
        return redirect(url_for("academics.exam_questions", eid=eid))
    try:
        rows = read_csv_upload(file, max_rows=500)
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(url_for("academics.exam_questions", eid=eid))
    added, errors = 0, []
    for i, row in enumerate(rows, start=2):
        qtype = (row.get("type") or "mcq").lower()
        if qtype in ("true/false", "true_false", "truefalse"):
            qtype = "tf"
        answer = (row.get("answer") or "").strip()
        form = {"qtype": qtype, "question": row.get("question", ""), "points": row.get("points") or "1",
                "opt_a": row.get("a") or row.get("option_a") or "", "opt_b": row.get("b") or row.get("option_b") or "",
                "opt_c": row.get("c") or row.get("option_c") or "", "opt_d": row.get("d") or row.get("option_d") or ""}
        if qtype == "mcq":
            form["correct_opt"] = answer.upper()[:1]
        elif qtype == "tf":
            form["correct_tf"] = "A" if answer.lower() in ("true", "t", "a", "yes") else (
                "B" if answer.lower() in ("false", "f", "b", "no") else "")
        else:
            form["accepted"] = answer
        data, err = _parse_question(form)
        if err:
            errors.append(f"Row {i}: {err}")
            continue
        db.execute("""INSERT INTO exam_questions (exam_id, question, opt_a, opt_b, opt_c, opt_d, correct_opt,
                      qtype, points) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                   (eid, data["question"], data["opt_a"], data["opt_b"], data["opt_c"], data["opt_d"],
                    data["correct_opt"], data["qtype"], data["points"]))
        added += 1
    db.commit()
    audit("questions_imported", f"exam {eid}: {added} added")
    flash(f"Imported {added} question(s)." + (f" {len(errors)} row(s) skipped: " + "; ".join(errors[:5]) if errors else ""),
          "success" if added else "error")
    return redirect(url_for("academics.exam_questions", eid=eid))


@bp.route("/questions/template.csv")
@staff_required
def questions_template():
    return csv_response("questions-template.csv", ["type", "question", "a", "b", "c", "d", "answer", "points"], [
        ["mcq", "What is 2+2?", "3", "4", "5", "6", "B", "1"],
        ["tf", "The sun is a star.", "", "", "", "", "true", "1"],
        ["short", "Capital of France?", "", "", "", "", "Paris|paris", "2"]])


@bp.route("/questions/<int:qid>/image")
@login_required
def question_image(qid):
    db = get_db()
    row = db.execute("""SELECT q.image, e.class_id FROM exam_questions q JOIN exams e ON e.id = q.exam_id
                        WHERE q.id = ?""", (qid,)).fetchone()
    if not row or not row["image"] or not can_view(row):
        abort(404)
    resp = send_from_directory(config.FOLDERS["questions"], row["image"], max_age=3600)
    resp.headers["Cache-Control"] = "private, max-age=3600"
    return resp


# ---- taking an exam ------------------------------------------------------ #
def _visible_exam(db, eid):
    exam = _exam_or_404(db, eid)
    if not can_view(exam):
        abort(404)
    return exam


@bp.route("/exams/<int:eid>/take")
@student_required
def take_exam(eid):
    db = get_db()
    exam = _visible_exam(db, eid)
    sid = g.user["code"]
    finalize_expired_attempts(db, sid)
    if db.execute("SELECT 1 FROM exam_results WHERE exam_id = ? AND student_id = ?", (eid, sid)).fetchone():
        flash("You have already taken this exam.", "info")
        return redirect(url_for("academics.exams"))
    attempt = db.execute("SELECT * FROM exam_attempts WHERE exam_id = ? AND student_id = ?", (eid, sid)).fetchone()
    if not attempt and is_closed(exam["due_date"]):
        flash("This exam has closed.", "error")
        return redirect(url_for("academics.exams"))
    questions = db.execute("SELECT * FROM exam_questions WHERE exam_id = ? ORDER BY id", (eid,)).fetchall()
    if not questions:
        flash("No questions have been added to this exam yet.", "info")
        return redirect(url_for("academics.exams"))
    if not attempt:
        try:
            db.execute("INSERT INTO exam_attempts (exam_id, student_id, started_at, seed) VALUES (?, ?, ?, ?)",
                       (eid, sid, time.time(), random.randint(1, 2 ** 31)))
            db.commit()
        except sqlite3.IntegrityError:
            pass
        attempt = db.execute("SELECT * FROM exam_attempts WHERE exam_id = ? AND student_id = ?", (eid, sid)).fetchone()
        audit("exam_started", exam["title"])
    end = attempt_end(exam, attempt["started_at"])
    remaining = None if end is None else max(0, int(end - time.time()))
    return render_template("exam_take.html", exam=exam, questions=ordered_questions(questions, exam, attempt["seed"]),
                           remaining=remaining, timed=bool(exam["duration_min"]))


@bp.route("/exams/<int:eid>/submit", methods=["POST"])
@student_required
def submit_exam(eid):
    db = get_db()
    exam = _visible_exam(db, eid)
    sid = g.user["code"]
    if db.execute("SELECT 1 FROM exam_results WHERE exam_id = ? AND student_id = ?", (eid, sid)).fetchone():
        flash("You have already taken this exam.", "info")
        return redirect(url_for("academics.exams"))
    attempt = db.execute("SELECT * FROM exam_attempts WHERE exam_id = ? AND student_id = ?", (eid, sid)).fetchone()
    if not attempt:
        flash("Please start the exam before submitting.", "error")
        return redirect(url_for("academics.exams"))
    questions = db.execute("SELECT * FROM exam_questions WHERE exam_id = ?", (eid,)).fetchall()
    end = attempt_end(exam, attempt["started_at"])
    stamp = now().strftime("%Y-%m-%d %H:%M:%S")
    if end is not None and time.time() > end + config.EXAM_GRACE_SECONDS:
        db.execute("INSERT OR IGNORE INTO exam_results (exam_id, student_id, score, total, submitted_at, status) "
                   "VALUES (?, ?, 0, ?, ?, 'timeout')", (eid, sid, exam_total_points(questions), stamp))
        db.commit()
        flash("Time ran out before your answers were received, so the exam was recorded as not submitted.", "error")
        return redirect(url_for("academics.exams"))
    answers = {q["id"]: request.form.get(f"q_{q['id']}", "") for q in questions}
    score, total, rows = score_exam(questions, answers)
    try:
        cur = db.execute("INSERT INTO exam_results (exam_id, student_id, score, total, submitted_at, status) "
                         "VALUES (?, ?, ?, ?, ?, 'submitted')", (eid, sid, score, total, stamp))
    except sqlite3.IntegrityError:
        return redirect(url_for("academics.exams"))
    db.executemany("INSERT INTO exam_answers (result_id, question_id, answer, is_correct, points) VALUES (?, ?, ?, ?, ?)",
                   [(cur.lastrowid, *r) for r in rows])
    db.commit()
    audit("exam_submitted", f"{exam['title']}: {score}/{total}")
    flash(f"Exam submitted! You scored {score}/{total}.", "success")
    if review_allowed(exam):
        return redirect(url_for("academics.review", rid=cur.lastrowid))
    return redirect(url_for("academics.exams"))


# ---- results, review, reset ---------------------------------------------- #
@bp.route("/exams/<int:eid>/results")
@staff_required
def exam_results(eid):
    db = get_db()
    exam = _exam_or_404(db, eid)
    finalize_expired_attempts(db)
    rows = db.execute(
        """SELECT r.*, s.name, s.id AS srow, c.name AS class_name FROM exam_results r
           LEFT JOIN authorized_students s ON s.student_id = r.student_id
           LEFT JOIN classes c ON c.id = s.class_id WHERE r.exam_id = ? ORDER BY c.name, s.name""", (eid,)).fetchall()
    return render_template("exam_results.html", exam=exam, results=rows)


@bp.route("/exams/<int:eid>/results.csv")
@staff_required
def exam_results_csv(eid):
    db = get_db()
    exam = _exam_or_404(db, eid)
    rows = db.execute(
        """SELECT s.name, r.student_id, c.name, r.score, r.total, r.status, r.submitted_at FROM exam_results r
           LEFT JOIN authorized_students s ON s.student_id = r.student_id
           LEFT JOIN classes c ON c.id = s.class_id WHERE r.exam_id = ? ORDER BY c.name, s.name""", (eid,)).fetchall()
    audit("export", f"exam results: {exam['title']}")
    return csv_response(f"exam-{eid}-results.csv",
                        ["full_name", "student_id", "class", "score", "total", "percent", "status", "submitted_at"],
                        [[r[0] or "", r[1], r[2] or "", r[3], r[4], round(100 * r[3] / r[4]) if r[4] else 0, r[5], r[6]]
                         for r in rows])


@bp.route("/results/<int:rid>/review")
@login_required
def review(rid):
    db = get_db()
    result = db.execute("SELECT * FROM exam_results WHERE id = ?", (rid,)).fetchone()
    if not result:
        abort(404)
    exam = _exam_or_404(db, result["exam_id"])
    if g.user["kind"] == "student":
        if result["student_id"] != g.user["code"]:
            abort(403)
        if not review_allowed(exam):
            flash("The teacher has not released the answers for this exam yet.", "info")
            return redirect(url_for("academics.exams"))
    stu = db.execute("SELECT * FROM authorized_students WHERE student_id = ?", (result["student_id"],)).fetchone()
    rows = db.execute(
        """SELECT q.*, a.answer, a.is_correct, a.points AS awarded FROM exam_questions q
           LEFT JOIN exam_answers a ON a.question_id = q.id AND a.result_id = ?
           WHERE q.exam_id = ? ORDER BY q.id""", (rid, exam["id"])).fetchall()
    has_detail = db.execute("SELECT 1 FROM exam_answers WHERE result_id = ?", (rid,)).fetchone() is not None
    return render_template("exam_review.html", exam=exam, result=result, rows=rows, student=stu, has_detail=has_detail)


@bp.route("/results/<int:rid>/reset", methods=["POST"])
@staff_required
def reset_result(rid):
    """Let a student retake: removes their result, answers and attempt."""
    db = get_db()
    r = db.execute("SELECT * FROM exam_results WHERE id = ?", (rid,)).fetchone()
    if r:
        db.execute("DELETE FROM exam_answers WHERE result_id = ?", (rid,))
        db.execute("DELETE FROM exam_results WHERE id = ?", (rid,))
        db.execute("DELETE FROM exam_attempts WHERE exam_id = ? AND student_id = ?", (r["exam_id"], r["student_id"]))
        db.commit()
        audit("exam_result_reset", f"exam {r['exam_id']} student {r['student_id']}")
        flash("Result removed - the student can retake the exam.", "success")
        return redirect(url_for("academics.exam_results", eid=r["exam_id"]))
    return redirect(url_for("academics.exams"))


# =========================================================================== #
#  ASSIGNMENTS
# =========================================================================== #
LATE_POLICIES = (("reject", "Reject late work"), ("allow", "Accept late work (flagged)"),
                 ("penalty", "Accept late work with a penalty"))


def _assignment_form():
    title = request.form.get("title", "").strip()[:120]
    instructions = request.form.get("instructions", "").strip()[:2000]
    due = request.form.get("due_date", "").strip()
    policy = request.form.get("late_policy", "reject")
    if policy not in dict(LATE_POLICIES):
        policy = "reject"
    penalty = max(0, min(100, int_or_none(request.form.get("late_penalty")) or 0)) if policy == "penalty" else 0
    class_id, subject_id = _target_fields()
    return title, instructions, due, policy, penalty, class_id, subject_id


@bp.route("/assignments")
@login_required
def assignments():
    db = get_db()
    clause, params = class_clause("a")
    rows = _decorate(db.execute(
        f"""SELECT a.*, c.name AS class_name, s.name AS subject_name,
                   (SELECT COUNT(*) FROM submissions x WHERE x.assignment_id = a.id) AS sub_count
            FROM assignments a LEFT JOIN classes c ON c.id = a.class_id LEFT JOIN subjects s ON s.id = a.subject_id
            WHERE {clause} ORDER BY a.id DESC""", params).fetchall())
    mine = {}
    if g.user["kind"] == "student":
        for r in db.execute("SELECT * FROM submissions WHERE student_id = ? ORDER BY id", (g.user["code"],)):
            mine[r["assignment_id"]] = dict(r)
    classes, subjects = form_lists(db)
    return render_template("assignments.html", assignments=rows, classes=classes, subjects=subjects,
                           mine=mine, late_policies=LATE_POLICIES)


@bp.route("/assignments/create", methods=["POST"])
@staff_required
def create_assignment():
    title, instructions, due, policy, penalty, class_id, subject_id = _assignment_form()
    if not title or not parse_dt(due):
        flash("Please provide a title and a valid due date.", "error")
        return redirect(url_for("academics.assignments"))
    stored = ""
    file = request.files.get("file")
    if file and file.filename:
        stored, err = save_upload(file, config.FOLDERS["assignments"], config.DOC_EXTENSIONS, config.MAX_UPLOAD_MB)
        if err:
            flash(err, "error")
            return redirect(url_for("academics.assignments"))
    db = get_db()
    db.execute("""INSERT INTO assignments (title, filename, instructions, due_date, late_policy, late_penalty,
                  class_id, subject_id, created_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
               (title, stored, instructions, due, policy, penalty, class_id, subject_id, g.user["name"],
                now().strftime("%Y-%m-%d")))
    db.commit()
    audit("assignment_created", title)
    flash("Assignment created.", "success")
    return redirect(url_for("academics.assignments"))


@bp.route("/assignments/<int:aid>/edit", methods=["POST"])
@staff_required
def edit_assignment(aid):
    title, instructions, due, policy, penalty, class_id, subject_id = _assignment_form()
    db = get_db()
    current = db.execute("SELECT filename FROM assignments WHERE id = ?", (aid,)).fetchone()
    if not current:
        abort(404)
    if not title or not parse_dt(due):
        flash("Please provide a title and a valid due date.", "error")
        return redirect(url_for("academics.assignments"))
    stored = current["filename"]
    file = request.files.get("file")
    if file and file.filename:
        new, err = save_upload(file, config.FOLDERS["assignments"], config.DOC_EXTENSIONS, config.MAX_UPLOAD_MB)
        if err:
            flash(err, "error")
            return redirect(url_for("academics.assignments"))
        remove_file(config.FOLDERS["assignments"], stored)
        stored = new
    db.execute("""UPDATE assignments SET title=?, instructions=?, due_date=?, late_policy=?, late_penalty=?,
                  class_id=?, subject_id=?, filename=? WHERE id=?""",
               (title, instructions, due, policy, penalty, class_id, subject_id, stored, aid))
    db.commit()
    audit("assignment_edited", title)
    flash("Assignment updated.", "success")
    return redirect(url_for("academics.assignments"))


@bp.route("/assignments/<int:aid>/delete", methods=["POST"])
@staff_required
def delete_assignment(aid):
    db = get_db()
    a = db.execute("SELECT * FROM assignments WHERE id = ?", (aid,)).fetchone()
    if a:
        remove_file(config.FOLDERS["assignments"], a["filename"])
        for s in db.execute("SELECT filename FROM submissions WHERE assignment_id = ?", (aid,)).fetchall():
            remove_file(config.FOLDERS["submissions"], s["filename"])
        db.execute("DELETE FROM submissions WHERE assignment_id = ?", (aid,))
        db.execute("DELETE FROM assignments WHERE id = ?", (aid,))
        db.commit()
        audit("assignment_deleted", a["title"])
        flash("Assignment and its submissions deleted.", "success")
    return redirect(url_for("academics.assignments"))


@bp.route("/assignments/<int:aid>/submit", methods=["POST"])
@student_required
def submit_work(aid):
    db = get_db()
    a = db.execute("SELECT * FROM assignments WHERE id = ?", (aid,)).fetchone()
    if not a or not can_view(a):
        abort(404)
    closed = is_closed(a["due_date"])
    if closed and a["late_policy"] == "reject":
        flash("The deadline has passed. Submission rejected.", "error")
        return redirect(url_for("academics.assignments"))
    file = request.files.get("file")
    if not file or not file.filename:
        flash("Please choose a file to submit.", "error")
        return redirect(url_for("academics.assignments"))
    stored, err = save_upload(file, config.FOLDERS["submissions"], config.DOC_EXTENSIONS, config.MAX_UPLOAD_MB)
    if err:
        flash(err, "error")
        return redirect(url_for("academics.assignments"))
    penalty = a["late_penalty"] if closed and a["late_policy"] == "penalty" else 0
    db.execute("""INSERT INTO submissions (assignment_id, student_id, filename, grade, feedback, late, penalty_pct,
                  submitted_at) VALUES (?, ?, ?, 'Pending', 'No feedback yet', ?, ?, ?)""",
               (aid, g.user["code"], stored, 1 if closed else 0, penalty, now().strftime("%Y-%m-%d %H:%M:%S")))
    db.commit()
    audit("work_submitted", f"{a['title']}{' (LATE)' if closed else ''}")
    flash("Work submitted successfully." + (" It was marked as late." if closed else ""), "success")
    return redirect(url_for("academics.submissions"))


@bp.route("/submissions")
@login_required
def submissions():
    db = get_db()
    if g.user["kind"] == "student":
        rows = db.execute(
            """SELECT s.*, a.title AS assignment_title FROM submissions s
               LEFT JOIN assignments a ON a.id = s.assignment_id
               WHERE s.student_id = ? ORDER BY s.id DESC""", (g.user["code"],)).fetchall()
        return render_template("submissions.html", submissions=rows, assignments=[], classes=[], filters={})
    aid = int_or_none(request.args.get("assignment_id"))
    cid = int_or_none(request.args.get("class_id"))
    status = request.args.get("status", "")
    sql = """SELECT s.*, a.title AS assignment_title, st.name AS student_name, st.id AS srow,
                    c.name AS class_name FROM submissions s
             LEFT JOIN assignments a ON a.id = s.assignment_id
             LEFT JOIN authorized_students st ON st.student_id = s.student_id
             LEFT JOIN classes c ON c.id = st.class_id WHERE 1=1"""
    params = []
    if aid:
        sql += " AND s.assignment_id = ?"
        params.append(aid)
    if cid:
        sql += " AND st.class_id = ?"
        params.append(cid)
    if status == "pending":
        sql += " AND s.grade = 'Pending'"
    elif status == "graded":
        sql += " AND s.grade != 'Pending'"
    elif status == "late":
        sql += " AND s.late = 1"
    rows = db.execute(sql + " ORDER BY s.id DESC LIMIT 500", params).fetchall()
    assignments_ = db.execute("SELECT id, title FROM assignments ORDER BY id DESC").fetchall()
    classes, _subjects = form_lists(db)
    return render_template("submissions.html", submissions=rows, assignments=assignments_, classes=classes,
                           filters={"assignment_id": aid, "class_id": cid, "status": status})


@bp.route("/submissions/<int:sid>/grade", methods=["POST"])
@staff_required
def grade_submission(sid):
    grade = request.form.get("grade", "").strip()[:20]
    feedback = request.form.get("feedback", "").strip()[:500] or "No feedback yet"
    if not grade:
        flash("Enter a grade first.", "error")
    else:
        db = get_db()
        db.execute("UPDATE submissions SET grade = ?, feedback = ? WHERE id = ?", (grade, feedback, sid))
        db.commit()
        audit("graded", f"submission {sid}: {grade}")
        flash("Grade saved.", "success")
    return redirect(request.referrer or url_for("academics.submissions"))


# =========================================================================== #
#  NOTES
# =========================================================================== #
@bp.route("/notes")
@login_required
def notes():
    db = get_db()
    clause, params = class_clause("n")
    rows = db.execute(
        f"""SELECT n.*, c.name AS class_name, s.name AS subject_name FROM notes n
            LEFT JOIN classes c ON c.id = n.class_id LEFT JOIN subjects s ON s.id = n.subject_id
            WHERE {clause} ORDER BY n.id DESC""", params).fetchall()
    classes, subjects = form_lists(db)
    return render_template("notes.html", notes=rows, classes=classes, subjects=subjects)


@bp.route("/notes/upload", methods=["POST"])
@staff_required
def upload_note():
    title = request.form.get("title", "").strip()[:120]
    file = request.files.get("file")
    class_id, subject_id = _target_fields()
    if not title or not file or not file.filename:
        flash("Please provide a title and choose a file.", "error")
        return redirect(url_for("academics.notes"))
    stored, err = save_upload(file, config.FOLDERS["notes"], config.DOC_EXTENSIONS, config.MAX_UPLOAD_MB)
    if err:
        flash(err, "error")
        return redirect(url_for("academics.notes"))
    db = get_db()
    db.execute("INSERT INTO notes (title, filename, class_id, subject_id, created_by, created_at) VALUES (?, ?, ?, ?, ?, ?)",
               (title, stored, class_id, subject_id, g.user["name"], now().strftime("%Y-%m-%d")))
    db.commit()
    audit("note_uploaded", title)
    flash("Note uploaded.", "success")
    return redirect(url_for("academics.notes"))


@bp.route("/notes/<int:nid>/delete", methods=["POST"])
@staff_required
def delete_note(nid):
    db = get_db()
    n = db.execute("SELECT * FROM notes WHERE id = ?", (nid,)).fetchone()
    if n:
        remove_file(config.FOLDERS["notes"], n["filename"])
        db.execute("DELETE FROM notes WHERE id = ?", (nid,))
        db.commit()
        audit("note_deleted", n["title"])
        flash("Note deleted.", "success")
    return redirect(url_for("academics.notes"))


# =========================================================================== #
#  ANNOUNCEMENTS
# =========================================================================== #
@bp.route("/announcements")
@login_required
def announcements():
    db = get_db()
    clause, params = class_clause("a")
    rows = db.execute(
        f"""SELECT a.*, c.name AS class_name FROM announcements a LEFT JOIN classes c ON c.id = a.class_id
            WHERE {clause} ORDER BY a.pinned DESC, a.id DESC LIMIT 100""", params).fetchall()
    classes, _s = form_lists(db)
    return render_template("announcements.html", items=rows, classes=classes)


@bp.route("/announcements/create", methods=["POST"])
@staff_required
def create_announcement():
    title = request.form.get("title", "").strip()[:120]
    body = request.form.get("body", "").strip()[:3000]
    class_id = int_or_none(request.form.get("class_id"))
    if not title:
        flash("An announcement needs a title.", "error")
    else:
        db = get_db()
        db.execute("INSERT INTO announcements (title, body, class_id, pinned, created_by, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                   (title, body, class_id, 1 if request.form.get("pinned") else 0, g.user["name"],
                    now().strftime("%Y-%m-%d %H:%M")))
        db.commit()
        audit("announcement_posted", title)
        flash("Announcement posted.", "success")
    return redirect(url_for("academics.announcements"))


@bp.route("/announcements/<int:aid>/pin", methods=["POST"])
@staff_required
def pin_announcement(aid):
    db = get_db()
    db.execute("UPDATE announcements SET pinned = 1 - COALESCE(pinned, 0) WHERE id = ?", (aid,))
    db.commit()
    return redirect(url_for("academics.announcements"))


@bp.route("/announcements/<int:aid>/delete", methods=["POST"])
@staff_required
def delete_announcement(aid):
    db = get_db()
    row = db.execute("SELECT title FROM announcements WHERE id = ?", (aid,)).fetchone()
    if row:
        db.execute("DELETE FROM announcements WHERE id = ?", (aid,))
        db.commit()
        audit("announcement_deleted", row["title"])
        flash("Announcement deleted.", "success")
    return redirect(url_for("academics.announcements"))


# =========================================================================== #
#  DOWNLOADS (always as attachments, access-checked)
# =========================================================================== #
@bp.route("/download/<folder_type>/<path:filename>")
@login_required
def download_file(folder_type, filename):
    if folder_type not in ("assignments", "submissions", "notes"):
        abort(404)
    db = get_db()
    row = db.execute(f"SELECT * FROM {folder_type} WHERE filename = ?", (filename,)).fetchone()
    if not row:
        abort(404)
    if folder_type == "submissions":
        if g.user["kind"] != "staff" and row["student_id"] != g.user["code"]:
            abort(403)
    elif not can_view(row):
        abort(404)
    return send_from_directory(config.FOLDERS[folder_type], filename, as_attachment=True,
                               download_name=display_name(filename))
