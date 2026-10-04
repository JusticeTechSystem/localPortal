"""Business logic kept out of the route handlers: exams, video watch tracking, attendance."""
import json
import random
import re
import time

from . import config
from .db import get_int_setting, now, parse_dt, to_epoch

# --------------------------------------------------------------------------- #
#  Exams
# --------------------------------------------------------------------------- #
QTYPES = ("mcq", "tf", "short")


def norm_text(value):
    return re.sub(r"\s+", " ", (value or "").strip().lower())


def is_correct(question, answer):
    qtype = question["qtype"] or "mcq"
    if qtype == "short":
        accepted = [norm_text(a) for a in (question["correct_opt"] or "").split("|") if a.strip()]
        return norm_text(answer) in accepted and norm_text(answer) != ""
    return (answer or "").strip().upper() == (question["correct_opt"] or "").strip().upper()


def exam_total_points(questions):
    return sum(max(1, q["points"] or 1) for q in questions)


def score_exam(questions, answers):
    """answers: {question_id: raw answer}. Returns (score, total, rows) where rows feed exam_answers."""
    score, total, rows = 0, 0, []
    for q in questions:
        pts = max(1, q["points"] or 1)
        total += pts
        ans = (answers.get(q["id"]) or "").strip()
        ok = bool(ans) and is_correct(q, ans)
        awarded = pts if ok else 0
        score += awarded
        rows.append((q["id"], ans[:500], 1 if ok else 0, awarded))
    return score, total, rows


def attempt_end(exam, started_at):
    """Epoch second when an attempt stops accepting answers (timer or exam deadline, whichever first)."""
    ends = []
    if exam["duration_min"]:
        ends.append(started_at + exam["duration_min"] * 60)
    due = parse_dt(exam["due_date"])
    if due:
        ends.append(to_epoch(due))
    return min(ends) if ends else None


def ordered_questions(questions, exam, seed):
    """Per-attempt deterministic shuffle (question order and MCQ option order)."""
    qs = list(questions)
    rng = random.Random(seed)
    if exam["shuffle"]:
        rng.shuffle(qs)
    out = []
    for q in qs:
        d = dict(q)
        if (q["qtype"] or "mcq") == "mcq":
            opts = [("A", q["opt_a"]), ("B", q["opt_b"]), ("C", q["opt_c"]), ("D", q["opt_d"])]
            if exam["shuffle"]:
                rng.shuffle(opts)
            d["options"] = opts
        elif q["qtype"] == "tf":
            d["options"] = [("A", "True"), ("B", "False")]
        out.append(d)
    return out


def finalize_expired_attempts(db, student_id=None):
    """Attempts whose time ran out without a submission become a recorded 0 (so no free retakes)."""
    sql = ("SELECT a.*, e.duration_min, e.due_date FROM exam_attempts a JOIN exams e ON e.id = a.exam_id "
           "WHERE NOT EXISTS (SELECT 1 FROM exam_results r WHERE r.exam_id = a.exam_id AND r.student_id = a.student_id)")
    params = ()
    if student_id:
        sql += " AND a.student_id = ?"
        params = (student_id,)
    changed = False
    for a in db.execute(sql, params).fetchall():
        end = attempt_end(a, a["started_at"])
        if end is not None and time.time() > end + config.EXAM_GRACE_SECONDS:
            qs = db.execute("SELECT * FROM exam_questions WHERE exam_id = ?", (a["exam_id"],)).fetchall()
            db.execute("INSERT OR IGNORE INTO exam_results (exam_id, student_id, score, total, submitted_at, status) "
                       "VALUES (?, ?, 0, ?, ?, 'timeout')",
                       (a["exam_id"], a["student_id"], exam_total_points(qs), now().strftime("%Y-%m-%d %H:%M:%S")))
            changed = True
    if changed:
        db.commit()


def review_allowed(exam):
    mode = exam["review_mode"] or "after_deadline"
    if mode == "after_submit":
        return True
    if mode == "after_deadline":
        due = parse_dt(exam["due_date"])
        return bool(due and now() > due)
    return False


# --------------------------------------------------------------------------- #
#  Video watch tracking
# --------------------------------------------------------------------------- #
def merge_ranges(ranges, gap=0.75):
    ranges = sorted([list(r) for r in ranges if r[1] > r[0]])
    merged = []
    for start, end in ranges:
        if merged and start <= merged[-1][1] + gap:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return merged


def covered_seconds(ranges):
    return sum(e - s for s, e in ranges)


def completion_threshold(duration):
    pct = min(100, max(50, get_int_setting("video_completion_pct", 95)))
    return max(0.0, min(duration * pct / 100.0, duration - 1.0)) if duration > 2 else duration


def apply_heartbeat(progress, position, duration, now_ts, state):
    """
    Credit watch time server-side.

    The browser only reports *where* the playhead is. Time is credited only when the playhead
    moved forward continuously by no more than the real time that elapsed since the previous
    heartbeat (small tolerance), so skipping ahead or faking a high position earns nothing.
    Returns (ranges, last_pos, last_ts, credited_seconds).
    """
    ranges = json.loads(progress["covered"] or "[]")
    last_pos, last_ts = progress["last_pos"] or 0.0, progress["last_ts"] or 0.0
    position = max(0.0, min(float(position), duration if duration else float(position)))
    credited = 0.0
    elapsed = now_ts - last_ts
    if state in ("playing", "ended") and last_ts and 0 < elapsed <= 20 and 0 <= position - last_pos <= elapsed * 1.25 + 1.0:
        ranges.append([last_pos, position])
        credited = position - last_pos
    ranges = merge_ranges(ranges)
    return ranges, position, now_ts, credited


# --------------------------------------------------------------------------- #
#  Attendance
# --------------------------------------------------------------------------- #
def attendance_summary(db, date_from, date_to, class_id=None):
    """Per-student presence between two dates (inclusive), plus the number of school days."""
    days = [r[0] for r in db.execute(
        "SELECT DISTINCT login_date FROM attendance WHERE login_date BETWEEN ? AND ? ORDER BY login_date",
        (date_from, date_to)).fetchall()]
    params = [date_from, date_to]
    class_sql = ""
    if class_id:
        class_sql = " AND s.class_id = ?"
        params.append(class_id)
    rows = db.execute(
        f"""SELECT s.id, s.student_id, s.name, c.name AS class_name,
                   COUNT(DISTINCT CASE WHEN a.login_date BETWEEN ? AND ? THEN a.login_date END) AS present
            FROM authorized_students s
            LEFT JOIN classes c ON c.id = s.class_id
            LEFT JOIN attendance a ON a.student_id = s.student_id
            WHERE COALESCE(s.active, 1) = 1 {class_sql}
            GROUP BY s.id ORDER BY c.name, s.name""", [date_from, date_to] + params[2:]).fetchall()
    total = len(days)
    out = []
    for r in rows:
        out.append({"id": r["id"], "student_id": r["student_id"], "name": r["name"],
                    "class_name": r["class_name"] or "—", "present": r["present"], "total": total,
                    "percent": round(100.0 * r["present"] / total) if total else 0})
    return days, out
