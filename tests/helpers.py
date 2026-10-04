"""Shared test setup: isolated data dir, app import, login helpers."""
import io
import os
import re
import shutil
import sys
import tempfile

TMP = tempfile.mkdtemp(prefix="eduportal-test-")
os.environ["DATA_DIR"] = TMP
os.environ["TEACHER_PASSWORD"] = "Adm1n-Secret!"
os.environ["PORTAL_TIMEZONE"] = "Africa/Lagos"
os.environ.pop("SECRET_KEY", None)
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import app as A  # noqa: E402

A.app.config["TESTING"] = False
APP = A.app


def token(client):
    html = client.get("/offline").get_data(as_text=True)
    return re.search(r'data-csrf="([0-9a-f]+)"', html).group(1)


def post(client, url, data=None, files=None, follow=False, **kw):
    payload = {"csrf_token": token(client)}
    payload.update(data or {})
    if files:
        for k, (content, name) in files.items():
            payload[k] = (io.BytesIO(content), name)
        return client.post(url, data=payload, content_type="multipart/form-data", follow_redirects=follow, **kw)
    return client.post(url, data=payload, follow_redirects=follow, **kw)


def staff_client(username="admin", password="Adm1n-Secret!"):
    c = APP.test_client()
    r = post(c, "/", {"role": "staff", "username": username, "password": password})
    assert r.status_code == 302, r.get_data(as_text=True)[:500]
    return c


def pin_from(response):
    return re.search(r'<code class="pin">([^<]+)</code>', response.get_data(as_text=True)).group(1)


def make_student(admin, name, sid=None, class_id=None, set_pin="mypin1234"):
    """Create via the UI, then sign in as the student and choose their own PIN. Returns (client, student_id)."""
    r = post(admin, "/students/add", {"name": name, "student_id": sid or "", "class_id": class_id or ""})
    assert r.status_code == 200, r.get_data(as_text=True)[:400]
    html = r.get_data(as_text=True)
    temp = pin_from(r)
    m = re.search(r"<code>(STU-[^<]+|[^<]+)</code></td><td>", html)
    student_id = sid or re.search(r"<td><code>([^<]+)</code></td>", html).group(1)
    c = APP.test_client()
    r = post(c, "/", {"role": "student", "student_id": student_id, "pin": temp})
    assert r.status_code == 302, "student login failed"
    r = post(c, "/account/password", {"new_password": set_pin, "confirm_password": set_pin})
    assert r.status_code == 302
    return c, student_id


def db():
    return A.dbm.connect()


def one(sql, *params):
    conn = db()
    row = conn.execute(sql, params).fetchone()
    conn.close()
    return row


def run(sql, *params):
    conn = db()
    conn.execute(sql, params)
    conn.commit()
    conn.close()


def png_bytes(color=(200, 30, 30)):
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (40, 40), color).save(buf, "PNG")
    return buf.getvalue()


def cleanup():
    shutil.rmtree(TMP, ignore_errors=True)
