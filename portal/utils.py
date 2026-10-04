"""Shared helpers: safe uploads, CSV in/out, SVG charts, template filters, misc."""
import csv
import io
import os
import re
import secrets

from flask import Response, g
from markupsafe import Markup, escape
from werkzeug.utils import secure_filename

from . import config
from .db import parse_dt

# --------------------------------------------------------------------------- #
#  Uploads
# --------------------------------------------------------------------------- #
_PREFIX_RE = re.compile(r"^[0-9a-f]{8}__")


def display_name(stored):
    return _PREFIX_RE.sub("", stored or "")


def sniff_image(head):
    """Return the extension for real PNG/JPEG/WebP bytes, else None (don't trust file names)."""
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if head.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    return None


def save_upload(file, folder, allowed, max_mb=None, image=False):
    """Validate + store an upload. Returns (stored_name, error_message)."""
    original = file.filename or ""
    stem, ext = os.path.splitext(original)
    ext = ext.lower().lstrip(".")
    if ext not in allowed:
        return None, f"File type '.{ext or '?'}' is not allowed."
    if image:
        head = file.stream.read(16)
        file.stream.seek(0)
        real = sniff_image(head)
        if not real:
            return None, "That file is not a valid PNG, JPG or WebP image."
        ext = real
    if max_mb:
        file.stream.seek(0, os.SEEK_END)
        size = file.stream.tell()
        file.stream.seek(0)
        if size > max_mb * 1024 * 1024:
            return None, f"File is too large (limit {max_mb} MB)."
    base = secure_filename(stem) or "file"
    stored = f"{secrets.token_hex(4)}__{base[:80]}.{ext}"
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
#  CSV
# --------------------------------------------------------------------------- #
def _csv_safe(value):
    """Neutralise spreadsheet formula injection (=, +, -, @ at the start of a text cell)."""
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + value
    return value


def csv_response(filename, header, rows):
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(header)
    for row in rows:
        writer.writerow([_csv_safe(c) for c in row])
    data = "\ufeff" + buf.getvalue()  # BOM so Excel opens UTF-8 names correctly
    return Response(data, mimetype="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


def read_csv_upload(file, max_rows=1000):
    """Parse an uploaded CSV into a list of dicts with lower-cased, trimmed headers."""
    raw = file.stream.read(2 * 1024 * 1024 + 1)
    if len(raw) > 2 * 1024 * 1024:
        raise ValueError("CSV is too large (max 2 MB).")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("latin-1")
    try:
        dialect = csv.Sniffer().sniff(text[:2048], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    if not reader.fieldnames:
        raise ValueError("The CSV has no header row.")
    rows = []
    for i, row in enumerate(reader):
        if i >= max_rows:
            raise ValueError(f"Too many rows (max {max_rows}).")
        clean = {(k or "").strip().lower().replace(" ", "_"): (v or "").strip() for k, v in row.items()}
        if any(clean.values()):
            rows.append(clean)
    return rows


# --------------------------------------------------------------------------- #
#  IDs and PINs
# --------------------------------------------------------------------------- #
def gen_pin(length=6):
    return "".join(secrets.choice("0123456789") for _ in range(length))


def next_student_id(db, taken=()):
    best = 0
    for r in db.execute("SELECT student_id FROM authorized_students").fetchall():
        m = re.match(r"^STU-(\d+)$", r["student_id"], re.I)
        if m:
            best = max(best, int(m.group(1)))
    for t in taken:
        m = re.match(r"^STU-(\d+)$", t, re.I)
        if m:
            best = max(best, int(m.group(1)))
    return f"STU-{best + 1:03d}"


# --------------------------------------------------------------------------- #
#  Visibility: content may target one class (class_id) or everybody (NULL)
# --------------------------------------------------------------------------- #
def class_clause(alias=""):
    """SQL fragment + params limiting rows to what the current user may see."""
    col = f"{alias}.class_id" if alias else "class_id"
    user = g.user
    if user["kind"] == "staff":
        return "1=1", ()
    if user["class_id"]:
        return f"({col} IS NULL OR {col} = ?)", (user["class_id"],)
    return f"{col} IS NULL", ()


def can_view(row):
    user = g.user
    if user["kind"] == "staff":
        return True
    cid = row["class_id"]
    return cid is None or cid == user["class_id"]


def int_or_none(value):
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- #
#  Presentation filters
# --------------------------------------------------------------------------- #
def fmt_dt(value):
    dt = parse_dt(value)
    return dt.strftime("%d %b %Y, %H:%M") if dt else (value or "")


def fmt_secs(value):
    try:
        total = int(round(float(value or 0)))
    except (TypeError, ValueError):
        return "0:00"
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def fmt_size(value):
    size = float(value or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024


def pct(score, total):
    try:
        return round(100.0 * score / total) if total else 0
    except (TypeError, ZeroDivisionError):
        return 0


def final_grade(grade, penalty):
    """If a grade is numeric and a late penalty applies, return the reduced grade (else None)."""
    try:
        if penalty and float(penalty) > 0:
            value = float(str(grade).strip().rstrip("%"))
            return f"{round(value * (1 - float(penalty) / 100.0), 1):g}"
    except (TypeError, ValueError):
        pass
    return None


def nl2br(value):
    return Markup("<br>").join(escape(line) for line in (value or "").splitlines())


# --------------------------------------------------------------------------- #
#  Server-side SVG chart (no JS, no CDN, CSP-safe: styling is done by CSS classes)
# --------------------------------------------------------------------------- #
def score_chart(points, width=640, height=220):
    """points: list of (label, percent). Returns an inline SVG as Markup."""
    if not points:
        return Markup("")
    pad_l, pad_r, pad_t, pad_b = 36, 14, 14, 42
    inner_w, inner_h = width - pad_l - pad_r, height - pad_t - pad_b
    n = len(points)
    step = inner_w / max(n - 1, 1)
    xy = []
    for i, (_label, value) in enumerate(points):
        x = pad_l + (inner_w / 2 if n == 1 else i * step)
        y = pad_t + inner_h * (1 - max(0, min(100, value)) / 100.0)
        xy.append((x, y))
    parts = [f'<svg class="chart" viewBox="0 0 {width} {height}" role="img" '
             f'aria-label="Exam score history">']
    for g_val in (0, 25, 50, 75, 100):
        y = pad_t + inner_h * (1 - g_val / 100.0)
        parts.append(f'<line class="chart-grid" x1="{pad_l}" y1="{y:.1f}" x2="{width - pad_r}" y2="{y:.1f}"/>')
        parts.append(f'<text class="chart-axis" x="{pad_l - 6}" y="{y + 4:.1f}" text-anchor="end">{g_val}</text>')
    if n > 1:
        pts = " ".join(f"{x:.1f},{y:.1f}" for x, y in xy)
        parts.append(f'<polyline class="chart-line" points="{pts}"/>')
    label_every = max(1, n // 8)
    for i, ((label, value), (x, y)) in enumerate(zip(points, xy)):
        parts.append(f'<circle class="chart-dot" cx="{x:.1f}" cy="{y:.1f}" r="4"><title>'
                     f'{escape(label)}: {value}%</title></circle>')
        if i % label_every == 0:
            short = (label[:14] + "…") if len(label) > 15 else label
            parts.append(f'<text class="chart-axis" x="{x:.1f}" y="{height - pad_b + 16}" '
                         f'text-anchor="middle">{escape(short)}</text>')
    parts.append("</svg>")
    return Markup("".join(parts))


# --------------------------------------------------------------------------- #
#  Misc
# --------------------------------------------------------------------------- #
def page_args(request, default=50):
    try:
        page = max(1, int(request.args.get("page", 1)))
    except ValueError:
        page = 1
    return page, default, (page - 1) * default


def hex_color_ok(value):
    return bool(re.fullmatch(r"#[0-9a-fA-F]{6}", value or ""))


def shade(hex_color, factor):
    """factor < 0 darkens, > 0 lightens (towards white)."""
    r, g_, b = (int(hex_color[i:i + 2], 16) for i in (1, 3, 5))
    if factor < 0:
        r, g_, b = (int(c * (1 + factor)) for c in (r, g_, b))
    else:
        r, g_, b = (int(c + (255 - c) * factor) for c in (r, g_, b))
    return f"#{r:02x}{g_:02x}{b:02x}"


def register_filters(app):
    app.add_template_filter(fmt_dt, "fmt_dt")
    app.add_template_filter(fmt_secs, "fmt_secs")
    app.add_template_filter(fmt_size, "fmt_size")
    app.add_template_filter(display_name, "display_name")
    app.add_template_filter(final_grade, "final_grade")
    app.add_template_filter(nl2br, "nl2br")
    app.add_template_filter(pct, "pct")
    app.add_template_filter(lambda v: (v or "?")[:1].upper(), "initial")
