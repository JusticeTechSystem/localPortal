"""End-to-end tests (Flask test client). Run:  python -m unittest discover -s tests -v"""
import io
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import unittest
import zipfile
from unittest import mock

from tests import helpers as H
from tests.helpers import APP, make_student, one, post, run, staff_client


def uniq(prefix):
    return f"{prefix}-{int(time.time() * 1000) % 10_000_000}"


class AuthTests(unittest.TestCase):
    def test_login_pages_render_clean_and_no_default_hint(self):
        html = APP.test_client().get("/").get_data(as_text=True)
        self.assertNotIn("{%", html)
        self.assertNotIn("{{", html)
        self.assertNotIn("admin123", html)

    def test_staff_login_and_wrong_password(self):
        c = APP.test_client()
        r = post(c, "/", {"role": "staff", "username": "admin", "password": "nope"})
        self.assertEqual(r.status_code, 200)
        self.assertIn("Invalid username or password", r.get_data(as_text=True))
        c = staff_client()
        self.assertEqual(c.get("/dashboard").status_code, 200)

    def test_csrf_required(self):
        c = APP.test_client()
        r = c.post("/", data={"role": "staff", "username": "admin", "password": "Adm1n-Secret!"})
        self.assertEqual(r.status_code, 302)
        self.assertNotIn("/dashboard", r.headers["Location"])

    def test_anonymous_blocked_everywhere(self):
        c = APP.test_client()
        for url in ("/dashboard", "/students", "/exams", "/videos", "/settings", "/audit", "/backups", "/attendance"):
            self.assertEqual(c.get(url).status_code, 302, url)

    def test_throttling(self):
        c = APP.test_client()
        for _ in range(10):
            post(c, "/", {"role": "student", "student_id": "ghost", "pin": "x"})
        self.assertEqual(post(c, "/", {"role": "student", "student_id": "ghost", "pin": "x"}).status_code, 429)
        from portal import security
        security._FAILS.clear()

    def test_student_first_login_flow_forces_pin_change(self):
        admin = staff_client()
        r = post(admin, "/students/add", {"name": "Flow Student", "student_id": uniq("FLOW")})
        temp = H.pin_from(r)
        sid = re.search(r"<td><code>([^<]+)</code></td>", r.get_data(as_text=True)).group(1)
        c = APP.test_client()
        self.assertEqual(post(c, "/", {"role": "student", "student_id": sid, "pin": "000000"}).status_code, 200)
        self.assertEqual(post(c, "/", {"role": "student", "student_id": sid, "pin": temp}).status_code, 302)
        r = c.get("/exams")
        self.assertEqual(r.status_code, 302)
        self.assertIn("/account", r.headers["Location"])
        r = post(c, "/account/password", {"new_password": "ab", "confirm_password": "ab"}, follow=True)
        self.assertIn("at least 4", r.get_data(as_text=True))
        r = post(c, "/account/password", {"new_password": sid, "confirm_password": sid}, follow=True)
        self.assertIn("cannot be the same", r.get_data(as_text=True))
        post(c, "/account/password", {"new_password": "goodpin77", "confirm_password": "goodpin77"})
        self.assertEqual(c.get("/exams").status_code, 200)
        c2 = APP.test_client()
        self.assertEqual(post(c2, "/", {"role": "student", "student_id": sid, "pin": "goodpin77"}).status_code, 302)

    def test_legacy_student_without_pin_can_claim_once(self):
        run("INSERT INTO authorized_students (student_id, name) VALUES ('LEGACY-1', 'Legacy Kid')")
        c = APP.test_client()
        self.assertEqual(post(c, "/", {"role": "student", "student_id": "legacy-1", "pin": ""}).status_code, 302)
        self.assertIn("/account", c.get("/dashboard").headers["Location"])
        post(c, "/account/password", {"new_password": "claimed99", "confirm_password": "claimed99"})
        self.assertEqual(c.get("/dashboard").status_code, 200)
        # the account is now protected
        c2 = APP.test_client()
        self.assertEqual(post(c2, "/", {"role": "student", "student_id": "LEGACY-1", "pin": ""}).status_code, 200)

    def test_staff_roles_and_forced_change_and_deactivation(self):
        admin = staff_client()
        uname = uniq("teach")
        r = post(admin, "/staff/add", {"username": uname, "full_name": "Tee Cher", "role": "teacher"})
        temp = H.pin_from(r)
        t = APP.test_client()
        self.assertEqual(post(t, "/", {"role": "staff", "username": uname, "password": temp}).status_code, 302)
        self.assertIn("/account", t.get("/dashboard").headers["Location"])
        post(t, "/account/password", {"new_password": "TeacherPass1", "confirm_password": "TeacherPass1"})
        self.assertEqual(t.get("/dashboard").status_code, 200)
        self.assertEqual(t.get("/students").status_code, 200)
        for url in ("/settings", "/audit", "/backups", "/staff"):
            self.assertEqual(t.get(url).status_code, 302, f"teacher must not open {url}")
        row = one("SELECT id FROM staff WHERE username = ?", uname)
        post(admin, f"/staff/{row['id']}/update", {"action": "deactivate"})
        self.assertEqual(t.get("/dashboard").status_code, 302)
        me = one("SELECT id FROM staff WHERE username = 'admin'")
        r = post(admin, f"/staff/{me['id']}/update", {"action": "deactivate"}, follow=True)
        self.assertIn("cannot demote or deactivate your own", r.get_data(as_text=True))

    def test_default_password_bootstrap_requires_change(self):
        """Fresh DB without TEACHER_PASSWORD -> admin/admin123 + forced change."""
        code = ("import os,tempfile;os.environ['DATA_DIR']=tempfile.mkdtemp();os.environ.pop('TEACHER_PASSWORD',None);"
                "import sys;sys.path.insert(0,%r);import app as A, sqlite3;"
                "r=sqlite3.connect(A.config.DB_PATH).execute(\"select must_change from staff where username='admin'\").fetchone();"
                "print('MUSTCHANGE', r[0])") % H.ROOT
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=H.ROOT)
        self.assertIn("MUSTCHANGE 1", out.stdout, out.stderr)


class PeopleTests(unittest.TestCase):
    def test_classes_students_import_edit_delete(self):
        admin = staff_client()
        post(admin, "/classes/add", {"name": "Imp Class A"})
        csv_bytes = ("full_name,student_id,class\nAda Lovelace,,Imp Class A\nGrace Hopper,IMP-200,Imp Class B\n"
                     "Dup Person,IMP-200,Imp Class B\n,,\nNo Id Kid,,Imp Class A\n").encode()
        r = post(admin, "/students/import", files={"file": (csv_bytes, "s.csv")})
        html = r.get_data(as_text=True)
        self.assertIn("Ada Lovelace", html)
        self.assertIn("IMP-200", html)
        self.assertIn("already exists", html)
        self.assertEqual(one("SELECT COUNT(*) c FROM classes WHERE name LIKE 'Imp Class%'")["c"], 2)
        pins = re.findall(r'<code class="pin">([^<]+)</code>', html)
        self.assertEqual(len(pins), 3)
        r = post(admin, "/students/credentials.csv", {"name": "A", "student_id": "X1", "class_name": "C", "pin": "123456"})
        self.assertIn("text/csv", r.headers["Content-Type"])
        self.assertIn("temporary_pin", r.get_data(as_text=True))
        row = one("SELECT * FROM authorized_students WHERE student_id = 'IMP-200'")
        post(admin, f"/students/{row['id']}/edit", {"name": "Grace M. Hopper", "student_id": "IMP-201", "class_id": ""})
        self.assertIsNotNone(one("SELECT 1 FROM authorized_students WHERE student_id = 'IMP-201'"))
        self.assertEqual(admin.get(f"/students/{row['id']}").status_code, 200)
        post(admin, f"/students/{row['id']}/toggle")
        self.assertEqual(one("SELECT active FROM authorized_students WHERE id = ?", row["id"])["active"], 0)
        c = APP.test_client()
        r = post(c, "/", {"role": "student", "student_id": "IMP-201", "pin": pins[1]})
        self.assertEqual(r.status_code, 200)
        post(admin, f"/students/{row['id']}/delete")
        self.assertIsNone(one("SELECT 1 FROM authorized_students WHERE id = ?", row["id"]))

    def test_id_change_moves_history_and_delete_with_attendance_works(self):
        admin = staff_client()
        c, sid = make_student(admin, "History Kid", uniq("HIS"))
        self.assertEqual(one("SELECT COUNT(*) c FROM attendance WHERE student_id = ?", sid)["c"], 1)
        row = one("SELECT id FROM authorized_students WHERE student_id = ?", sid)
        new = sid + "X"
        post(admin, f"/students/{row['id']}/edit", {"name": "History Kid", "student_id": new, "class_id": ""})
        self.assertEqual(one("SELECT COUNT(*) c FROM attendance WHERE student_id = ?", new)["c"], 1)
        post(admin, f"/students/{row['id']}/delete")
        self.assertEqual(admin.get("/students").status_code, 200)

    def test_photo_validation_and_access(self):
        admin = staff_client()
        a, sid_a = make_student(admin, "Photo A", uniq("PHA"))
        b, sid_b = make_student(admin, "Photo B", uniq("PHB"))
        r = post(a, "/account/photo", files={"photo": (b"this is not an image", "me.png")}, follow=True)
        self.assertIn("not a valid", r.get_data(as_text=True))
        post(a, "/account/photo", files={"photo": (H.png_bytes(), "me.png")})
        row = one("SELECT id, photo FROM authorized_students WHERE student_id = ?", sid_a)
        self.assertTrue(row["photo"])
        self.assertEqual(a.get(f"/photo/{row['id']}").headers["Content-Type"], "image/png")
        self.assertEqual(b.get(f"/photo/{row['id']}").status_code, 403)
        self.assertEqual(admin.get(f"/photo/{row['id']}").status_code, 200)
        r = post(admin, f"/students/{row['id']}/photo", files={"photo": (H.png_bytes((1, 2, 3)), "x.png")})
        self.assertIn(r.status_code, (302,))

    def test_csv_formula_injection_is_neutralised(self):
        admin = staff_client()
        make_student(admin, "=HYPERLINK(\"http://evil\")", uniq("INJ"))
        text = admin.get("/export/students.csv").get_data(as_text=True)
        self.assertIn("'=HYPERLINK", text)


class ContentTargetingTests(unittest.TestCase):
    def test_class_targeting(self):
        admin = staff_client()
        post(admin, "/classes/add", {"name": "Target A"})
        post(admin, "/classes/add", {"name": "Target B"})
        ca = one("SELECT id FROM classes WHERE name = 'Target A'")["id"]
        cb = one("SELECT id FROM classes WHERE name = 'Target B'")["id"]
        sa, _ = make_student(admin, "Target Student A", uniq("TA"), ca)
        sb, _ = make_student(admin, "Target Student B", uniq("TB"), cb)
        post(admin, "/exams/create", {"title": "Only A exam", "due_date": "2099-01-01T10:00", "class_id": ca})
        post(admin, "/announcements/create", {"title": "Only B notice", "body": "hello B", "class_id": cb})
        post(admin, "/notes/upload", {"title": "Only A note", "class_id": ca}, files={"file": (b"x", "n.txt")})
        self.assertIn("Only A exam", sa.get("/exams").get_data(as_text=True))
        self.assertNotIn("Only A exam", sb.get("/exams").get_data(as_text=True))
        self.assertIn("Only B notice", sb.get("/announcements").get_data(as_text=True))
        self.assertNotIn("Only B notice", sa.get("/announcements").get_data(as_text=True))
        self.assertNotIn("Only A note", sb.get("/notes").get_data(as_text=True))
        note = one("SELECT filename FROM notes WHERE title = 'Only A note'")
        self.assertEqual(sa.get(f"/download/notes/{note['filename']}").status_code, 200)
        self.assertEqual(sb.get(f"/download/notes/{note['filename']}").status_code, 404)
        exam = one("SELECT id FROM exams WHERE title = 'Only A exam'")
        self.assertEqual(sb.get(f"/exams/{exam['id']}/take").status_code, 404)
        post(admin, f"/classes/{ca}/delete")
        self.assertIsNone(one("SELECT class_id FROM exams WHERE title = 'Only A exam'")["class_id"])


class ExamTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.admin = staff_client()
        r = post(cls.admin, "/exams/create", {"title": "Mixed Exam", "due_date": "2099-01-01T10:00", "duration_min": "30",
                                              "review_mode": "after_submit", "shuffle": "1"})
        cls.eid = int(re.search(r"/exams/(\d+)/questions", r.headers["Location"]).group(1))
        base = f"/exams/{cls.eid}/questions/add"
        post(cls.admin, base, {"qtype": "mcq", "question": "What is 2+2?", "points": "2", "opt_a": "3", "opt_b": "4",
                               "opt_c": "5", "opt_d": "6", "correct_opt": "B"})
        post(cls.admin, base, {"qtype": "tf", "question": "The sun is a star.", "correct_tf": "A"})
        post(cls.admin, base, {"qtype": "short", "question": "Capital of France?", "accepted": "Paris|paris city", "points": "3"},
             files={"image": (H.png_bytes(), "pic.png")})
        cls.q = {q["qtype"]: q for q in H.db().execute("SELECT * FROM exam_questions WHERE exam_id = ?", (cls.eid,)).fetchall()}

    def test_question_validation(self):
        r = post(self.admin, f"/exams/{self.eid}/questions/add", {"qtype": "mcq", "question": "x", "opt_a": "1"}, follow=True)
        self.assertIn("all four options", r.get_data(as_text=True))
        r = post(self.admin, f"/exams/{self.eid}/questions/add", {"qtype": "short", "question": "x"}, follow=True)
        self.assertIn("accepted answer", r.get_data(as_text=True))

    def test_timed_shuffled_exam_scores_correctly_and_review(self):
        c, sid = make_student(self.admin, "Exam Taker", uniq("EX"))
        page = c.get(f"/exams/{self.eid}/take").get_data(as_text=True)
        self.assertIn('id="timer"', page)
        order1 = re.findall(r'class="q-num">(\d+)</span> ([^<]+?) (?:<span|</legend)', page)
        again = c.get(f"/exams/{self.eid}/take").get_data(as_text=True)
        self.assertEqual(order1, re.findall(r'class="q-num">(\d+)</span> ([^<]+?) (?:<span|</legend)', again), "stable per attempt")
        four = re.search(r'name="q_(\d+)" value="([A-D])"><span class="choice-letter">[A-D]</span><span>4</span>', page)
        self.assertEqual(int(four.group(1)), self.q["mcq"]["id"])
        form = {f"q_{self.q['mcq']['id']}": four.group(2), f"q_{self.q['tf']['id']}": "B",
                f"q_{self.q['short']['id']}": "  PARIS  "}
        r = post(c, f"/exams/{self.eid}/submit", form, follow=True)
        self.assertIn("scored 5/6", r.get_data(as_text=True))          # 2 (mcq) + 0 (tf wrong) + 3 (short)
        self.assertIn("your answer", r.get_data(as_text=True))          # review shown straight away
        res = one("SELECT * FROM exam_results WHERE student_id = ?", sid)
        self.assertEqual((res["score"], res["total"]), (5, 6))
        self.assertEqual(one("SELECT COUNT(*) c FROM exam_answers WHERE result_id = ?", res["id"])["c"], 3)
        r = post(c, f"/exams/{self.eid}/submit", form, follow=True)
        self.assertIn("already taken", r.get_data(as_text=True))
        r = self.admin.get(f"/results/{res['id']}/review")
        self.assertIn("Exam Taker", r.get_data(as_text=True))
        self.assertEqual(self.admin.get(f"/exams/{self.eid}/results.csv").status_code, 200)
        # another student cannot read this review
        other, _ = make_student(self.admin, "Nosy Kid", uniq("NO"))
        self.assertEqual(other.get(f"/results/{res['id']}/review").status_code, 403)
        # teacher can allow a retake
        post(self.admin, f"/results/{res['id']}/reset")
        self.assertEqual(c.get(f"/exams/{self.eid}/take").status_code, 200)

    def test_time_limit_enforced_server_side(self):
        c, sid = make_student(self.admin, "Slow Poke", uniq("SP"))
        c.get(f"/exams/{self.eid}/take")
        run("UPDATE exam_attempts SET started_at = ? WHERE student_id = ?", time.time() - 3600, sid)
        r = post(c, f"/exams/{self.eid}/submit", {f"q_{self.q['tf']['id']}": "A"}, follow=True)
        self.assertIn("Time ran out", r.get_data(as_text=True))
        res = one("SELECT * FROM exam_results WHERE student_id = ?", sid)
        self.assertEqual((res["score"], res["status"]), (0, "timeout"))
        r = c.get(f"/exams/{self.eid}/take", follow_redirects=True)
        self.assertIn("already taken", r.get_data(as_text=True))

    def test_abandoned_attempt_is_finalised_as_zero(self):
        c, sid = make_student(self.admin, "Walked Away", uniq("WA"))
        c.get(f"/exams/{self.eid}/take")
        run("UPDATE exam_attempts SET started_at = ? WHERE student_id = ?", time.time() - 3600, sid)
        c.get("/exams")
        self.assertEqual(one("SELECT status FROM exam_results WHERE student_id = ?", sid)["status"], "timeout")

    def test_closed_exam_cannot_be_started_and_review_after_deadline(self):
        r = post(self.admin, "/exams/create", {"title": "Past Exam", "due_date": "2000-01-01T10:00", "review_mode": "after_deadline"})
        eid = int(re.search(r"/exams/(\d+)/questions", r.headers["Location"]).group(1))
        post(self.admin, f"/exams/{eid}/questions/add", {"qtype": "tf", "question": "q", "correct_tf": "A"})
        c, sid = make_student(self.admin, "Late Student", uniq("LT"))
        r = c.get(f"/exams/{eid}/take", follow_redirects=True)
        self.assertIn("closed", r.get_data(as_text=True))
        # review gating on a live exam
        r = post(self.admin, "/exams/create", {"title": "Gated", "due_date": "2099-01-01T10:00", "review_mode": "after_deadline"})
        gid = int(re.search(r"/exams/(\d+)/questions", r.headers["Location"]).group(1))
        post(self.admin, f"/exams/{gid}/questions/add", {"qtype": "tf", "question": "q", "correct_tf": "A"})
        c.get(f"/exams/{gid}/take")
        post(c, f"/exams/{gid}/submit", {})
        rid = one("SELECT id FROM exam_results WHERE exam_id = ? AND student_id = ?", gid, sid)["id"]
        self.assertIn("not released", c.get(f"/results/{rid}/review", follow_redirects=True).get_data(as_text=True))
        run("UPDATE exams SET due_date = '2001-01-01T00:00' WHERE id = ?", gid)
        self.assertEqual(c.get(f"/results/{rid}/review").status_code, 200)

    def test_question_edit_delete_and_csv_import(self):
        q = self.q["tf"]
        post(self.admin, f"/questions/{q['id']}/edit", {"qtype": "tf", "question": "The sun is a star (edited).", "correct_tf": "A", "points": "1"})
        self.assertIn("edited", one("SELECT question FROM exam_questions WHERE id = ?", q["id"])["question"])
        csv_bytes = ("type,question,a,b,c,d,answer,points\nmcq,CSV Q1,a,b,c,d,C,1\ntf,CSV Q2,,,,,false,1\nshort,CSV Q3,,,,,x|y,2\nmcq,bad,,,,,Z,1\n").encode()
        r = post(self.admin, f"/exams/{self.eid}/questions/import", files={"file": (csv_bytes, "q.csv")}, follow=True)
        self.assertIn("Imported 3", r.get_data(as_text=True))
        qid = one("SELECT id FROM exam_questions WHERE question = 'CSV Q1'")["id"]
        post(self.admin, f"/questions/{qid}/delete")
        self.assertIsNone(one("SELECT 1 FROM exam_questions WHERE id = ?", qid))
        for q_ in H.db().execute("SELECT id FROM exam_questions WHERE question LIKE 'CSV Q%'").fetchall():
            run("DELETE FROM exam_questions WHERE id = ?", q_["id"])

    def test_question_image_access(self):
        qid = self.q["short"]["id"]
        self.assertEqual(self.admin.get(f"/questions/{qid}/image").status_code, 200)
        self.assertEqual(APP.test_client().get(f"/questions/{qid}/image").status_code, 302)


class AssignmentTests(unittest.TestCase):
    def test_late_policies(self):
        admin = staff_client()
        ids = {}
        for policy, extra in (("reject", {}), ("allow", {}), ("penalty", {"late_penalty": "20"})):
            post(admin, "/assignments/create", {"title": f"HW {policy}", "due_date": "2000-01-01T10:00", "late_policy": policy, **extra})
            ids[policy] = one("SELECT id FROM assignments WHERE title = ?", f"HW {policy}")["id"]
        c, sid = make_student(admin, "Late Worker", uniq("LW"))
        f = {"file": (b"work", "my work.txt")}
        r = post(c, f"/assignments/{ids['reject']}/submit", files=f, follow=True)
        self.assertIn("deadline has passed", r.get_data(as_text=True))
        r = post(c, f"/assignments/{ids['allow']}/submit", files={"file": (b"w", "a.txt")}, follow=True)
        self.assertIn("marked as late", r.get_data(as_text=True))
        post(c, f"/assignments/{ids['penalty']}/submit", files={"file": (b"w", "b.txt")})
        sub = one("SELECT * FROM submissions WHERE assignment_id = ? AND student_id = ?", ids["penalty"], sid)
        self.assertEqual((sub["late"], sub["penalty_pct"]), (1, 20))
        post(admin, f"/submissions/{sub['id']}/grade", {"grade": "80", "feedback": "ok"})
        self.assertIn("→ <strong>64</strong>", admin.get("/submissions").get_data(as_text=True))
        self.assertIn("64", c.get("/submissions").get_data(as_text=True))
        self.assertIn("HW penalty", admin.get("/submissions?status=late").get_data(as_text=True))
        self.assertEqual(admin.get("/export/grades.csv").status_code, 200)
        # delete with submissions works; files removed
        stored = sub["filename"]
        post(admin, f"/assignments/{ids['penalty']}/delete")
        self.assertFalse(os.path.exists(os.path.join(H.A.config.FOLDERS["submissions"], stored)))

    def test_upload_hardening(self):
        admin = staff_client()
        post(admin, "/assignments/create", {"title": "Trav", "due_date": "2099-01-01T10:00"}, files={"file": (b"x", "../../etc/passwd.txt")})
        stored = one("SELECT filename FROM assignments WHERE title = 'Trav'")["filename"]
        self.assertNotIn("/", stored)
        self.assertNotIn("..", stored)
        r = post(admin, "/assignments/create", {"title": "Exe", "due_date": "2099-01-01T10:00"}, files={"file": (b"MZ", "evil.exe")}, follow=True)
        self.assertIn("not allowed", r.get_data(as_text=True))
        self.assertEqual(admin.get("/download/notes/..%2f..%2fapp.py").status_code, 404)
        self.assertEqual(admin.get("/download/secrets/x").status_code, 404)


class VideoTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.admin = staff_client()
        cls.mp4 = os.path.join(H.TMP, "lesson.mp4")
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=duration=10:size=320x240:rate=15",
                        "-f", "lavfi", "-i", "sine=duration=10", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
                        "-movflags", "+faststart", cls.mp4], check=True)
        post(cls.admin, "/classes/add", {"name": "Video Class"})
        cls.cid = one("SELECT id FROM classes WHERE name = 'Video Class'")["id"]
        with open(cls.mp4, "rb") as fh:
            data = fh.read()
        r = post(cls.admin, "/videos/upload", {"title": "Lesson One", "description": "intro", "class_id": cls.cid, "duration": "10"},
                 files={"file": (data, "lesson.mp4")})
        cls.vid = int(re.search(r"/videos/(\d+)/report", r.headers["Location"]).group(1))

    def beat(self, client, position, state="playing"):
        r = client.post(f"/videos/{self.vid}/progress", json={"position": position, "state": state},
                        headers={"X-CSRF-Token": H.token(client)})
        return r.get_json()

    def test_upload_probe_duration_and_streaming_ranges(self):
        v = one("SELECT * FROM videos WHERE id = ?", self.vid)
        self.assertAlmostEqual(v["duration"], 10.0, delta=0.6)
        full = self.admin.get(f"/videos/{self.vid}/stream")
        self.assertEqual(full.status_code, 200)
        self.assertEqual(full.headers["Accept-Ranges"], "bytes")
        part = self.admin.get(f"/videos/{self.vid}/stream", headers={"Range": "bytes=0-99"})
        self.assertEqual(part.status_code, 206)
        self.assertEqual(len(part.data), 100)
        self.assertIn("bytes 0-99/", part.headers["Content-Range"])
        self.assertEqual(part.headers["Content-Type"], "video/mp4")
        self.assertEqual(APP.test_client().get(f"/videos/{self.vid}/stream").status_code, 302)

    def test_bad_video_rejected(self):
        r = post(self.admin, "/videos/upload", {"title": "Fake", "duration": "0"}, files={"file": (b"not a video", "x.mp4")}, follow=True)
        self.assertIn("Could not read the video", r.get_data(as_text=True))
        r = post(self.admin, "/videos/upload", {"title": "Fake", "duration": "5"}, files={"file": (b"x", "x.exe")}, follow=True)
        self.assertIn("not allowed", r.get_data(as_text=True))

    def test_honest_viewing_completes_and_appears_in_report(self):
        c, sid = make_student(self.admin, "Full Watcher", uniq("FW"), self.cid)
        clock = {"off": 0.0}
        real = time.time
        with mock.patch("time.time", lambda: real() + clock["off"]):
            self.beat(c, 0)
            res = None
            for pos in (3, 6, 9):
                clock["off"] += 3
                res = self.beat(c, pos)
            self.assertFalse(res["completed"] and res["percent"] < 90)
            clock["off"] += 1.2
            res = self.beat(c, 10, "ended")
        self.assertTrue(res["completed"], res)
        self.assertGreaterEqual(res["percent"], 95)
        html = self.admin.get(f"/videos/{self.vid}/report").get_data(as_text=True)
        self.assertIn("Full Watcher", html)
        self.assertIn(sid, html)
        self.assertIn("Video Class", html)
        self.assertRegex(html, r'data-status="Completed"')
        csv_text = self.admin.get(f"/videos/{self.vid}/report.csv").get_data(as_text=True)
        self.assertIn("Full Watcher", csv_text)
        self.assertIn("Completed", csv_text)
        self.assertIn("Completed", c.get("/videos").get_data(as_text=True))

    def test_skipping_and_speeding_earn_no_credit(self):
        c, sid = make_student(self.admin, "Skipper", uniq("SK"), self.cid)
        clock = {"off": 0.0}
        real = time.time
        with mock.patch("time.time", lambda: real() + clock["off"]):
            self.beat(c, 0)
            clock["off"] += 3
            res = self.beat(c, 9.5)                    # jumped 9.5s in 3 real seconds
            self.assertEqual(res["watched"], 0)
            clock["off"] += 3
            res = self.beat(c, 10, "ended")
            self.assertLess(res["percent"], 20)
            self.assertFalse(res["completed"])
            clock["off"] += 60                          # long gap (tab asleep) -> anchor resets, no credit
            res = self.beat(c, 10)
            self.assertFalse(res["completed"])
        self.assertIn("Not started", self.admin.get(f"/videos/{self.vid}/report").get_data(as_text=True))

    def test_pause_does_not_credit_and_bad_input_rejected(self):
        c, _ = make_student(self.admin, "Pauser", uniq("PA"), self.cid)
        self.assertEqual(c.post(f"/videos/{self.vid}/progress", json={"position": "abc", "state": "playing"},
                                headers={"X-CSRF-Token": H.token(c)}).status_code, 400)
        self.assertEqual(c.post(f"/videos/{self.vid}/progress", json={"position": 1, "state": "hax"},
                                headers={"X-CSRF-Token": H.token(c)}).status_code, 400)
        self.assertEqual(c.post(f"/videos/{self.vid}/progress", json={"position": 1, "state": "playing"}).status_code, 400)  # no CSRF
        self.assertEqual(self.admin.post(f"/videos/{self.vid}/progress", json={"position": 1, "state": "playing"},
                                         headers={"X-CSRF-Token": H.token(self.admin)}).status_code, 403)   # staff can't record viewing

    def test_other_class_cannot_watch(self):
        c, _ = make_student(self.admin, "Outsider", uniq("OUT"))
        self.assertEqual(c.get(f"/videos/{self.vid}").status_code, 404)
        self.assertEqual(c.get(f"/videos/{self.vid}/stream").status_code, 404)
        self.assertEqual(c.get(f"/videos/{self.vid}/report").status_code, 302)    # staff only
        self.assertNotIn("Lesson One", c.get("/videos").get_data(as_text=True))

    def test_watch_page_and_resume(self):
        c, _ = make_student(self.admin, "Resumer", uniq("RS"), self.cid)
        page = c.get(f"/videos/{self.vid}").get_data(as_text=True)
        self.assertIn('data-tracked="1"', page)
        self.assertIn('data-progress-url="/videos/%d/progress"' % self.vid, page)
        self.assertIn('data-tracked="0"', self.admin.get(f"/videos/{self.vid}").get_data(as_text=True))

    def test_xhr_upload_returns_json(self):
        with open(self.mp4, "rb") as fh:
            data = fh.read()
        r = post(self.admin, "/videos/upload", {"title": "XHR", "duration": "10"}, files={"file": (data, "x.mp4")},
                 headers={"X-Requested-With": "XHR-upload"})
        self.assertTrue(r.get_json()["ok"])
        self.assertIn("/videos/", r.get_json()["redirect"])

    def test_delete_video_removes_file(self):
        with open(self.mp4, "rb") as fh:
            data = fh.read()
        post(self.admin, "/videos/upload", {"title": "Temp vid", "duration": "10"}, files={"file": (data, "t.mp4")})
        v = one("SELECT * FROM videos WHERE title = 'Temp vid'")
        post(self.admin, f"/videos/{v['id']}/delete")
        self.assertFalse(os.path.exists(os.path.join(H.A.config.FOLDERS["videos"], v["filename"])))

    def test_heartbeat_unit_logic(self):
        from portal import services
        row = {"covered": "[]", "last_pos": 0.0, "last_ts": 100.0}
        ranges, pos, ts, credited = services.apply_heartbeat(row, 3.0, 10.0, 103.0, "playing")
        self.assertEqual((ranges, credited), ([[0.0, 3.0]], 3.0))
        row = {"covered": json.dumps(ranges), "last_pos": 3.0, "last_ts": 103.0}
        _r, _p, _t, credited = services.apply_heartbeat(row, 1.0, 10.0, 106.0, "playing")     # rewound
        self.assertEqual(credited, 0.0)
        _r, _p, _t, credited = services.apply_heartbeat(row, 6.0, 10.0, 104.0, "playing")     # 3s of video in 1s
        self.assertEqual(credited, 0.0)
        merged = services.merge_ranges([[0, 3], [3.2, 6], [8, 9]])
        self.assertEqual(merged, [[0, 6], [8, 9]])


class ReportTests(unittest.TestCase):
    def test_attendance_range_and_exports(self):
        admin = staff_client()
        post(admin, "/classes/add", {"name": "Att Class"})
        cid = one("SELECT id FROM classes WHERE name = 'Att Class'")["id"]
        c, sid = make_student(admin, "Att Student", uniq("AT"), cid)
        today = one("SELECT date('now','localtime') d")["d"]
        html = admin.get(f"/attendance?from=2000-01-01&to=2099-12-31&class_id={cid}").get_data(as_text=True)
        self.assertIn("Att Student", html)
        self.assertIn("100%", html)
        csv_text = admin.get(f"/attendance.csv?from=2000-01-01&to=2099-12-31&class_id={cid}").get_data(as_text=True)
        self.assertIn("Att Student", csv_text)
        log = admin.get("/attendance.csv?from=2000-01-01&to=2099-12-31&kind=log").get_data(as_text=True)
        self.assertIn(sid, log)
        self.assertEqual(admin.get("/attendance?from=garbage&to=junk").status_code, 200)
        empty = admin.get("/attendance?from=1999-01-01&to=1999-01-31").get_data(as_text=True)
        self.assertIn("0</strong> school day", empty)

    def test_profile_report_card_pdf_and_permissions(self):
        admin = staff_client()
        a, sid_a = make_student(admin, "Ọlábísí Àdùnní", uniq("RC"))
        b, _ = make_student(admin, "Other Kid", uniq("RD"))
        row = one("SELECT id FROM authorized_students WHERE student_id = ?", sid_a)
        r = post(admin, "/exams/create", {"title": "RC Exam", "due_date": "2099-01-01T10:00"})
        eid = int(re.search(r"/exams/(\d+)/questions", r.headers["Location"]).group(1))
        post(admin, f"/exams/{eid}/questions/add", {"qtype": "tf", "question": "q", "correct_tf": "A"})
        a.get(f"/exams/{eid}/take")
        qid = one("SELECT id FROM exam_questions WHERE exam_id = ?", eid)["id"]
        post(a, f"/exams/{eid}/submit", {f"q_{qid}": "A"})
        post(a, "/account/photo", files={"photo": (H.png_bytes(), "p.png")})
        pdf = a.get(f"/report-card/{row['id']}")
        self.assertEqual(pdf.status_code, 200)
        self.assertEqual(pdf.headers["Content-Type"], "application/pdf")
        self.assertTrue(pdf.data.startswith(b"%PDF"))
        self.assertGreater(len(pdf.data), 2000)
        self.assertEqual(admin.get(f"/report-card/{row['id']}").status_code, 200)
        self.assertEqual(b.get(f"/report-card/{row['id']}").status_code, 403)
        html = admin.get(f"/report-card/{row['id']}?format=html").get_data(as_text=True)
        self.assertIn("Ọlábísí Àdùnní", html)
        self.assertIn("<svg", a.get(f"/students/{row['id']}").get_data(as_text=True))
        self.assertEqual(b.get(f"/students/{row['id']}").status_code, 403)
        self.assertEqual(a.get("/my-results").status_code, 302)

    def test_exports(self):
        admin = staff_client()
        for url in ("/export/students.csv", "/export/results.csv", "/export/grades.csv"):
            r = admin.get(url)
            self.assertEqual(r.status_code, 200, url)
            self.assertTrue(r.get_data(as_text=True).startswith("\ufeff"))


class AdminTests(unittest.TestCase):
    def test_settings_branding_and_validation(self):
        admin = staff_client()
        r = post(admin, "/settings", {"school_name": "Bright Future Academy", "tagline": "Learn more", "accent": "#0f766e",
                                      "timezone": "Mars/Phobos", "video_completion_pct": "90", "backup_enabled": "1", "backup_keep": "5"}, follow=True)
        self.assertIn("not a valid timezone", r.get_data(as_text=True))
        r = post(admin, "/settings", {"school_name": "Bright Future Academy", "tagline": "Learn more", "accent": "red",
                                      "timezone": "", "video_completion_pct": "90"}, follow=True)
        self.assertIn("valid accent", r.get_data(as_text=True))
        r = post(admin, "/settings", {"school_name": "Bright Future Academy", "tagline": "Learn more", "accent": "#0f766e",
                                      "timezone": "Africa/Lagos", "video_completion_pct": "90", "backup_enabled": "1", "backup_keep": "5"},
                 files={"logo": (H.png_bytes((10, 100, 100)), "logo.png")}, follow=True)
        self.assertIn("Settings saved", r.get_data(as_text=True))
        self.assertIn("Bright Future Academy", APP.test_client().get("/").get_data(as_text=True))
        self.assertIn("#0f766e", APP.test_client().get("/theme.css").get_data(as_text=True))
        self.assertEqual(APP.test_client().get("/branding/logo").headers["Content-Type"], "image/png")
        r = post(admin, "/settings", {"school_name": "X", "accent": "#0f766e", "video_completion_pct": "90"},
                 files={"logo": (b"<svg onload=alert(1)>", "logo.png")}, follow=True)
        self.assertIn("not a valid", r.get_data(as_text=True))
        post(admin, "/settings", {"school_name": "EduPortal", "tagline": "t", "accent": "#4338ca", "timezone": "Africa/Lagos",
                                  "video_completion_pct": "95", "backup_enabled": "1", "backup_keep": "14", "remove_logo": "1"})
        self.assertEqual(APP.test_client().get("/branding/logo").status_code, 404)

    def test_audit_log(self):
        admin = staff_client()
        post(admin, "/classes/add", {"name": "Audited Class"})
        html = admin.get("/audit?q=class_added").get_data(as_text=True)
        self.assertIn("Audited Class", html)
        self.assertIn("login", admin.get("/audit").get_data(as_text=True))
        csv_text = admin.get("/audit.csv?q=class_added").get_data(as_text=True)
        self.assertIn("class_added", csv_text)

    def test_backups(self):
        admin = staff_client()
        post(admin, "/backups/create")
        snaps = [f for f in os.listdir(H.A.config.BACKUP_DIR) if f.startswith("classroom-")]
        self.assertTrue(snaps)
        conn = sqlite3.connect(os.path.join(H.A.config.BACKUP_DIR, snaps[0]))
        self.assertGreater(conn.execute("SELECT COUNT(*) FROM staff").fetchone()[0], 0)
        conn.close()
        r = post(admin, "/backups/download", {})
        self.assertEqual(r.headers["Content-Type"], "application/zip")
        with zipfile.ZipFile(io.BytesIO(r.data)) as z:
            self.assertIn("classroom.db", z.namelist())
            self.assertFalse([n for n in z.namelist() if n.startswith("uploads/videos")])
        with open(os.path.join(H.A.config.FOLDERS["videos"], "backup_probe.mp4"), "wb") as fh:
            fh.write(b"0" * 100)
        r = post(admin, "/backups/download", {"include_videos": "1"})
        with zipfile.ZipFile(io.BytesIO(r.data)) as z:
            self.assertTrue([n for n in z.namelist() if n.startswith("uploads/videos")])
        self.assertEqual(admin.get(f"/backups/file/{snaps[0]}").status_code, 200)
        self.assertEqual(admin.get("/backups/file/..%2fclassroom.db").status_code, 404)
        from portal import backups
        before = len(backups.list_backups())
        backups.run_scheduled_if_due()
        self.assertEqual(len(backups.list_backups()), before, "recent snapshot exists -> nothing new")

    def test_pwa_endpoints(self):
        c = APP.test_client()
        m = c.get("/manifest.webmanifest")
        self.assertEqual(m.headers["Content-Type"], "application/manifest+json")
        self.assertEqual(len(m.get_json()["icons"]), 3)
        for icon in m.get_json()["icons"]:
            self.assertEqual(c.get(icon["src"]).status_code, 200)
        sw = c.get("/sw.js")
        self.assertEqual(sw.headers["Service-Worker-Allowed"], "/")
        self.assertIn("javascript", sw.headers["Content-Type"])
        self.assertEqual(c.get("/offline").status_code, 200)
        self.assertEqual(c.get("/healthz").data, b"ok")
        headers = c.get("/").headers
        self.assertIn("Content-Security-Policy", headers)
        self.assertIn("media-src 'self' blob:", headers["Content-Security-Policy"])

    def test_oversize_upload_rejected_before_parsing(self):
        admin = staff_client()
        r = admin.post("/notes/upload", data={"csrf_token": H.token(admin), "title": "big"},
                       environ_overrides={"CONTENT_LENGTH": str((H.A.config.MAX_UPLOAD_MB + 5) * 1024 * 1024)})
        self.assertEqual(r.status_code, 302)
        self.assertIn("too large", admin.get("/dashboard").get_data(as_text=True))
        self.assertIsNone(one("SELECT 1 FROM notes WHERE title = 'big'"))

    def test_error_pages(self):
        c = APP.test_client()
        self.assertIn("Page not found", c.get("/nope").get_data(as_text=True))
        self.assertEqual(c.get("/exams/create").status_code, 405)
        admin = staff_client()
        r = admin.get("/exams/create")
        self.assertEqual(r.status_code, 405)
        self.assertIn("Method not allowed", r.get_data(as_text=True))


class MigrationTests(unittest.TestCase):
    def test_upgrade_from_original_database(self):
        legacy = os.path.join(H.ROOT, "tests", "legacy_v1.db")
        self.assertTrue(os.path.exists(legacy), "fixture missing")
        code = r'''
import os, shutil, sys, tempfile
d = tempfile.mkdtemp(); shutil.copy(%r, os.path.join(d, "classroom.db"))
os.environ["DATA_DIR"] = d; os.environ["TEACHER_PASSWORD"] = "Adm1n-Secret!"
sys.path.insert(0, %r)
import app as A, sqlite3, re
c = A.app.test_client()
tok = lambda: re.search(r'data-csrf="([0-9a-f]+)"', c.get("/offline").get_data(as_text=True)).group(1)
db = sqlite3.connect(A.config.DB_PATH)
print("STUDENTS", db.execute("select count(*) from authorized_students").fetchone()[0])
print("OLDRESULT", db.execute("select count(*) from exam_results").fetchone()[0])
print("ATT", db.execute("select count(*) from attendance").fetchone()[0])
print("PIN", db.execute("select pin_hash from authorized_students where student_id='STU-101'").fetchone()[0])
r = c.post("/", data={"csrf_token": tok(), "role": "student", "student_id": "STU-101", "pin": ""})
print("CLAIM", r.status_code, c.get("/dashboard").headers.get("Location"))
r = c.post("/account/password", data={"csrf_token": tok(), "new_password": "newpin99", "confirm_password": "newpin99"})
print("DASH", c.get("/dashboard").status_code)
''' % (legacy, H.ROOT)
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
        self.assertIn("STUDENTS 1", out.stdout, out.stderr)
        self.assertIn("OLDRESULT 1", out.stdout)
        self.assertIn("ATT 1", out.stdout)
        self.assertIn("PIN None", out.stdout)
        self.assertIn("CLAIM 302 /account", out.stdout)
        self.assertIn("DASH 200", out.stdout)


def tearDownModule():
    H.cleanup()


if __name__ == "__main__":
    unittest.main()
