# Local Assignment Portal

`index.html` sits in the project root so the same page works two ways.

## 1. Local server (real sharing between teacher and students)
    pip install -r requirements.txt
    python app.py
Open http://127.0.0.1:5000. To let students on the same Wi-Fi connect, run with `HOST=0.0.0.0`
(Windows: `set HOST=0.0.0.0` then `python app.py`) and give them `http://<your-laptop-IP>:5000`.
Files are stored in `uploads/assignments` and `uploads/submissions`.

## 2. GitHub Pages (static)
Upload everything to a repo (index.html must stay in the root), then Settings -> Pages ->
Deploy from a branch -> main -> / (root).
GitHub Pages cannot run Python, so in this mode:
- Assignments listed in `assignments.json` (files in `uploads/assignments/`) are visible to everyone.
  To publish a new one: add the file to `uploads/assignments/`, add its name to `assignments.json`, commit.
- Anything uploaded through the page is saved only in that visitor's browser (not shared).
For real file sharing online, use a Python host (PythonAnywhere / Render) or a backend like Firebase/Supabase.
