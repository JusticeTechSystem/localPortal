"""Local Assignment Portal - Flask backend.

index.html lives in the project root so the same file also works on GitHub Pages.
Run:  python app.py   ->  http://127.0.0.1:5000
"""
import os
from datetime import datetime

from flask import Flask, abort, jsonify, request, send_from_directory
from werkzeug.utils import secure_filename

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FOLDERS = {
    "assignments": os.path.join(BASE_DIR, "uploads", "assignments"),
    "submissions": os.path.join(BASE_DIR, "uploads", "submissions"),
}
for path in FOLDERS.values():
    os.makedirs(path, exist_ok=True)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024  # 50 MB


def list_files(kind):
    folder = FOLDERS[kind]
    return sorted(f for f in os.listdir(folder)
                  if os.path.isfile(os.path.join(folder, f)) and not f.startswith("."))


@app.route("/")
def index():
    return send_from_directory(BASE_DIR, "index.html")


@app.route("/api/list")
def api_list():
    return jsonify(assignments=list_files("assignments"), submissions=list_files("submissions"))


@app.route("/api/upload/<kind>", methods=["POST"])
def api_upload(kind):
    if kind not in FOLDERS:
        abort(404)
    file = request.files.get("file")
    if not file or not file.filename:
        return jsonify(error="No file selected."), 400
    name = secure_filename(file.filename)
    if not name:
        return jsonify(error="Invalid file name."), 400
    # Never overwrite an existing file: add a timestamp instead.
    if os.path.exists(os.path.join(FOLDERS[kind], name)):
        stem, ext = os.path.splitext(name)
        name = f"{stem}_{datetime.now().strftime('%Y%m%d%H%M%S')}{ext}"
    file.save(os.path.join(FOLDERS[kind], name))
    return jsonify(ok=True, name=name)


@app.route("/download/<kind>/<path:filename>")
def download_file(kind, filename):
    if kind not in FOLDERS:
        abort(404)
    return send_from_directory(FOLDERS[kind], filename, as_attachment=True)


@app.errorhandler(413)
def too_large(_e):
    return jsonify(error="File too large (max 50 MB)."), 413


if __name__ == "__main__":
    host = os.environ.get("HOST", "127.0.0.1")   # set HOST=0.0.0.0 to let other devices connect
    port = int(os.environ.get("PORT", 5000))
    app.run(host=host, port=port, debug=False)
