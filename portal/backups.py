"""Backups: consistent SQLite snapshots (scheduled) and full zip downloads (manual)."""
import glob
import os
import sqlite3
import tempfile
import threading
import time
import zipfile

from . import config
from .db import audit, connect, get_int_setting, now

_lock = threading.Lock()


def snapshot_db(dest_path):
    """Copy the live database safely using SQLite's online backup API."""
    src = sqlite3.connect(config.DB_PATH, timeout=15)
    dst = sqlite3.connect(dest_path)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()


def list_backups():
    items = []
    for path in sorted(glob.glob(os.path.join(config.BACKUP_DIR, "classroom-*.db")), reverse=True):
        st = os.stat(path)
        items.append({"name": os.path.basename(path), "size": st.st_size,
                      "when": time.strftime("%Y-%m-%d %H:%M", time.localtime(st.st_mtime))})
    return items


def create_scheduled_backup(reason="scheduled"):
    os.makedirs(config.BACKUP_DIR, exist_ok=True)
    name = f"classroom-{now().strftime('%Y%m%d-%H%M%S')}.db"
    snapshot_db(os.path.join(config.BACKUP_DIR, name))
    keep = max(1, get_int_setting("backup_keep", 14))
    for old in list_backups()[keep:]:
        try:
            os.remove(os.path.join(config.BACKUP_DIR, old["name"]))
        except OSError:
            pass
    audit("backup_created", f"{name} ({reason})", actor="system", role="system")
    return name


def run_scheduled_if_due():
    if not _lock.acquire(blocking=False):
        return
    try:
        items = list_backups()
        newest = max((os.path.getmtime(os.path.join(config.BACKUP_DIR, i["name"])) for i in items), default=0)
        if time.time() - newest >= config.BACKUP_INTERVAL_HOURS * 3600:
            create_scheduled_backup()
    except Exception:
        pass
    finally:
        _lock.release()


def build_zip(include_videos=False):
    """Full backup (database + uploads) as a temp zip file path. Caller deletes it."""
    fd, zip_path = tempfile.mkstemp(suffix=".zip")
    os.close(fd)
    fd, db_copy = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        snapshot_db(db_copy)
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
            z.write(db_copy, "classroom.db")
            for folder_name, folder in config.FOLDERS.items():
                if folder_name == "videos" and not include_videos:
                    continue
                for root, _dirs, files in os.walk(folder):
                    for f in files:
                        full = os.path.join(root, f)
                        z.write(full, os.path.join("uploads", folder_name, os.path.relpath(full, folder)))
    finally:
        os.remove(db_copy)
    return zip_path
