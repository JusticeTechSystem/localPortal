"""Configuration: environment variables (optionally from a local .env file) and paths."""
import os
import secrets

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOTENV_USED = False


def _load_dotenv(path):
    """Tiny .env reader so no extra dependency is needed. Real environment variables win."""
    global DOTENV_USED
    if not os.path.isfile(path):
        return
    DOTENV_USED = True
    with open(path, encoding="utf-8-sig") as fh:  # utf-8-sig tolerates Notepad's BOM
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv(os.path.join(BASE_DIR, ".env"))


def _int_env(name, default):
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


DATA_DIR = os.path.abspath(os.environ.get("DATA_DIR") or os.path.join(BASE_DIR, "data"))
DB_PATH = os.path.join(DATA_DIR, "classroom.db")
UPLOAD_ROOT = os.path.join(DATA_DIR, "uploads")
BACKUP_DIR = os.path.join(DATA_DIR, "backups")
FOLDERS = {
    name: os.path.join(UPLOAD_ROOT, name)
    for name in ("assignments", "submissions", "notes", "videos", "questions", "photos", "branding")
}

# The bootstrap administrator password. Only used the FIRST time the database is created
# (afterwards passwords are managed inside the portal).
ADMIN_PASSWORD_ENV = os.environ.get("TEACHER_PASSWORD") or os.environ.get("ADMIN_PASSWORD") or ""
USING_DEFAULT_PASSWORD = not ADMIN_PASSWORD_ENV
DEFAULT_ADMIN_PASSWORD = "admin123"

MAX_UPLOAD_MB = _int_env("MAX_UPLOAD_MB", 25)
MAX_VIDEO_MB = _int_env("MAX_VIDEO_MB", 500)
MAX_IMAGE_MB = 3

DOC_EXTENSIONS = {
    "pdf", "doc", "docx", "odt", "rtf", "txt", "md", "ppt", "pptx", "odp", "xls", "xlsx",
    "ods", "csv", "png", "jpg", "jpeg", "gif", "webp", "zip", "rar", "7z", "py", "java",
    "c", "cpp", "h", "js", "html", "css", "json", "sql", "ipynb",
}
VIDEO_EXTENSIONS = {"mp4", "m4v", "webm", "ogv"}
IMAGE_EXTENSIONS = {"png", "jpg", "jpeg", "webp"}

EXAM_GRACE_SECONDS = 30      # tolerance for network delay when a timed exam auto-submits
BACKUP_INTERVAL_HOURS = 24

TRUST_PROXY = os.environ.get("TRUST_PROXY") == "1"
COOKIE_SECURE = os.environ.get("COOKIE_SECURE") == "1"


def ensure_dirs():
    for path in (DATA_DIR, UPLOAD_ROOT, BACKUP_DIR, *FOLDERS.values()):
        os.makedirs(path, exist_ok=True)


def load_secret_key():
    key = os.environ.get("SECRET_KEY")
    if key:
        return key
    ensure_dirs()
    key_file = os.path.join(DATA_DIR, ".secret_key")
    if os.path.isfile(key_file):
        with open(key_file, encoding="utf-8") as fh:
            stored = fh.read().strip()
            if stored:
                return stored
    key = secrets.token_hex(32)
    try:
        with open(key_file, "w", encoding="utf-8") as fh:
            fh.write(key)
        os.chmod(key_file, 0o600)
    except OSError:
        pass
    return key
