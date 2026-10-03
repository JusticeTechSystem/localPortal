import os
import sqlite3
import sys
import time
import logging
from datetime import datetime
from flask import Flask, render_template, request, redirect, url_for, session, flash, send_from_directory

# --- MUTE DEFAULT FLASK LOGS FOR CLEAN TERMINAL ---
log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)
cli = sys.modules['flask.cli']
cli.show_server_banner = lambda *x: None

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

app = Flask(__name__, template_folder=BASE_DIR)
app.secret_key = 'super_secret_local_key'

UPLOAD_ASSIGNMENTS = os.path.join(BASE_DIR, 'uploads', 'assignments')
UPLOAD_SUBMISSIONS = os.path.join(BASE_DIR, 'uploads', 'submissions')
UPLOAD_NOTES = os.path.join(BASE_DIR, 'uploads', 'notes')

for path in [UPLOAD_ASSIGNMENTS, UPLOAD_SUBMISSIONS, UPLOAD_NOTES]:
    os.makedirs(path, exist_ok=True)

DB_PATH = os.path.join(BASE_DIR, 'classroom.db')

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    # 1. Authorized Students
    c.execute('''CREATE TABLE IF NOT EXISTS authorized_students (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    student_id TEXT UNIQUE NOT NULL,
                    name TEXT DEFAULT 'Unknown Student'
                )''')
    
    # AUTO-MIGRATIONS FOR OLD DATABASES
    c.execute("PRAGMA table_info(authorized_students)")
    columns = [col[1] for col in c.fetchall()]
    if 'name' not in columns:
        c.execute("ALTER TABLE authorized_students ADD COLUMN name TEXT DEFAULT 'Unknown Student'")
        
    c.execute("PRAGMA table_info(assignments)")
    assign_cols = [col[1] for col in c.fetchall()]
    if 'due_date' not in assign_cols:
        try:
            c.execute("ALTER TABLE assignments ADD COLUMN due_date TEXT")
        except sqlite3.OperationalError:
            pass # Table might not exist yet
    
    # 2. Attendance Tracker
    c.execute('''CREATE TABLE IF NOT EXISTS attendance (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    student_id TEXT,
                    login_date TEXT,
                    login_time TEXT,
                    FOREIGN KEY(student_id) REFERENCES authorized_students(student_id)
                )''')

    # 3. CBT Exams & Tests
    c.execute('''CREATE TABLE IF NOT EXISTS exams (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT,
                    due_date TEXT
                )''')
    c.execute('''CREATE TABLE IF NOT EXISTS exam_questions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    exam_id INTEGER,
                    question TEXT,
                    opt_a TEXT,
                    opt_b TEXT,
                    opt_c TEXT,
                    opt_d TEXT,
                    correct_opt TEXT,
                    FOREIGN KEY(exam_id) REFERENCES exams(id) ON DELETE CASCADE
                )''')
    c.execute('''CREATE TABLE IF NOT EXISTS exam_results (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    exam_id INTEGER,
                    student_id TEXT,
                    score INTEGER,
                    total INTEGER,
                    submitted_at TEXT,
                    FOREIGN KEY(exam_id) REFERENCES exams(id) ON DELETE CASCADE
                )''')

    # 4. Assignments & Notes
    c.execute('''CREATE TABLE IF NOT EXISTS assignments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT,
                    filename TEXT,
                    instructions TEXT,
                    due_date TEXT
                )''')
    c.execute('''CREATE TABLE IF NOT EXISTS submissions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    assignment_id INTEGER,
                    student_id TEXT,
                    filename TEXT,
                    grade TEXT,
                    feedback TEXT,
                    FOREIGN KEY(assignment_id) REFERENCES assignments(id)
                )''')
    c.execute('''CREATE TABLE IF NOT EXISTS notes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT,
                    filename TEXT
                )''')
        
    conn.commit()
    conn.close()

init_db()

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON") 
    return conn

# --- AUTHENTICATION, ATTENDANCE & LANDING PAGE ---

@app.route('/', methods=['GET', 'POST'])
def index():
    if request.method == 'POST':
        role = request.form.get('role')
        if role == 'teacher':
            password = request.form.get('password')
            if password == 'admin123':
                session['user_id'] = 'Teacher'
                session['name'] = 'Instructor'
                session['role'] = 'teacher'
                return redirect(url_for('dashboard'))
            else:
                flash('Invalid teacher password! Default is admin123')
        elif role == 'student':
            student_id = request.form.get('student_id', '').strip()
            db = get_db()
            valid_student = db.execute('SELECT * FROM authorized_students WHERE student_id = ?', (student_id,)).fetchone()
            
            if valid_student:
                session['user_id'] = student_id
                session['name'] = valid_student['name']
                session['role'] = 'student'
                
                today = datetime.now().strftime('%Y-%m-%d')
                now_time = datetime.now().strftime('%H:%M:%S')
                existing = db.execute('SELECT id FROM attendance WHERE student_id = ? AND login_date = ?', (student_id, today)).fetchone()
                if not existing:
                    db.execute('INSERT INTO attendance (student_id, login_date, login_time) VALUES (?, ?, ?)', (student_id, today, now_time))
                    db.commit()
                db.close()
                return redirect(url_for('dashboard'))
            else:
                db.close()
                flash('Unauthorized Student ID! Contact your teacher for an issued ID.')
    return render_template('index.html')

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('index'))

# --- DASHBOARD DATA LOADING ---

@app.route('/dashboard')
def dashboard():
    if 'role' not in session:
        return redirect(url_for('index'))
    
    db = get_db()
    assignments = db.execute('SELECT * FROM assignments ORDER BY id DESC').fetchall()
    notes = db.execute('SELECT * FROM notes ORDER BY id DESC').fetchall()
    exams = db.execute('SELECT * FROM exams ORDER BY id DESC').fetchall()
    current_time = datetime.now().strftime('%Y-%m-%dT%H:%M')

    if session['role'] == 'teacher':
        authorized_students = db.execute('SELECT * FROM authorized_students ORDER BY id DESC').fetchall()
        submissions = db.execute('''SELECT s.*, a.title as assignment_title FROM submissions s LEFT JOIN assignments a ON s.assignment_id = a.id ORDER BY s.id DESC''').fetchall()
        attendance = db.execute('''SELECT a.login_date, a.login_time, a.student_id, s.name FROM attendance a JOIN authorized_students s ON a.student_id = s.student_id ORDER BY a.login_date DESC, a.login_time DESC''').fetchall()
        exam_questions = db.execute('SELECT * FROM exam_questions').fetchall()
        all_exam_results = db.execute('''SELECT r.*, e.title, s.name FROM exam_results r JOIN exams e ON r.exam_id = e.id JOIN authorized_students s ON r.student_id = s.student_id ORDER BY r.id DESC''').fetchall()
        db.close()
        return render_template('dashboard.html', assignments=assignments, notes=notes, submissions=submissions, 
                               authorized_students=authorized_students, attendance=attendance, exams=exams, 
                               exam_questions=exam_questions, all_exam_results=all_exam_results, current_time=current_time)
    else:
        submissions = db.execute('''SELECT s.*, a.title as assignment_title FROM submissions s LEFT JOIN assignments a ON s.assignment_id = a.id WHERE s.student_id = ? ORDER BY s.id DESC''', (session['user_id'],)).fetchall()
        my_results_raw = db.execute('SELECT * FROM exam_results WHERE student_id = ?', (session['user_id'],)).fetchall()
        taken_exams = {r['exam_id']: r for r in my_results_raw}
        db.close()
        return render_template('dashboard.html', assignments=assignments, notes=notes, submissions=submissions, 
                               exams=exams, taken_exams=taken_exams, current_time=current_time)

# --- EXAMS & CBT LOGIC ---

@app.route('/create_exam', methods=['POST'])
def create_exam():
    if session.get('role') != 'teacher': return redirect(url_for('dashboard'))
    title = request.form.get('title')
    due_date = request.form.get('due_date')
    db = get_db()
    db.execute('INSERT INTO exams (title, due_date) VALUES (?, ?)', (title, due_date))
    db.commit()
    db.close()
    return redirect(url_for('dashboard'))

@app.route('/add_question', methods=['POST'])
def add_question():
    if session.get('role') != 'teacher': return redirect(url_for('dashboard'))
    exam_id = request.form.get('exam_id')
    question = request.form.get('question')
    a, b, c, d = request.form.get('opt_a'), request.form.get('opt_b'), request.form.get('opt_c'), request.form.get('opt_d')
    ans = request.form.get('correct_opt')
    db = get_db()
    db.execute('INSERT INTO exam_questions (exam_id, question, opt_a, opt_b, opt_c, opt_d, correct_opt) VALUES (?, ?, ?, ?, ?, ?, ?)',
               (exam_id, question, a, b, c, d, ans))
    db.commit()
    db.close()
    flash('Question added to exam successfully!')
    return redirect(url_for('dashboard'))

@app.route('/delete_exam/<int:id>')
def delete_exam(id):
    if session.get('role') != 'teacher': return redirect(url_for('dashboard'))
    db = get_db()
    db.execute('DELETE FROM exams WHERE id = ?', (id,))
    db.commit()
    db.close()
    return redirect(url_for('dashboard'))

@app.route('/take_exam/<int:exam_id>')
def take_exam(exam_id):
    if session.get('role') != 'student': return redirect(url_for('dashboard'))
    db = get_db()
    exam = db.execute('SELECT * FROM exams WHERE id = ?', (exam_id,)).fetchone()
    questions = db.execute('SELECT * FROM exam_questions WHERE exam_id = ?', (exam_id,)).fetchall()
    db.close()
    
    if exam['due_date'] and datetime.now() > datetime.strptime(exam['due_date'], '%Y-%m-%dT%H:%M'):
        flash('This exam has closed.')
        return redirect(url_for('dashboard'))
        
    return render_template('dashboard.html', taking_exam=True, exam=exam, questions=questions)

@app.route('/submit_exam/<int:exam_id>', methods=['POST'])
def submit_exam(exam_id):
    if session.get('role') != 'student': return redirect(url_for('dashboard'))
    db = get_db()
    exam = db.execute('SELECT due_date FROM exams WHERE id = ?', (exam_id,)).fetchone()
    
    if exam['due_date'] and datetime.now() > datetime.strptime(exam['due_date'], '%Y-%m-%dT%H:%M'):
        flash('Deadline passed. Exam not accepted.')
        db.close()
        return redirect(url_for('dashboard'))

    questions = db.execute('SELECT id, correct_opt FROM exam_questions WHERE exam_id = ?', (exam_id,)).fetchall()
    score = 0
    total = len(questions)
    
    for q in questions:
        user_ans = request.form.get(f"q_{q['id']}")
        if user_ans == q['correct_opt']: score += 1
            
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    db.execute('INSERT INTO exam_results (exam_id, student_id, score, total, submitted_at) VALUES (?, ?, ?, ?, ?)',
               (exam_id, session['user_id'], score, total, now))
    db.commit()
    db.close()
    flash(f'Exam submitted! You scored {score}/{total}.')
    return redirect(url_for('dashboard'))

# --- STUDENT ID MANAGEMENT (TEACHER) ---

@app.route('/add_student_id', methods=['POST'])
def add_student_id():
    if session.get('role') != 'teacher': return redirect(url_for('dashboard'))
    student_id = request.form.get('student_id', '').strip()
    name = request.form.get('name', '').strip()
    if student_id and name:
        db = get_db()
        try:
            db.execute('INSERT INTO authorized_students (student_id, name) VALUES (?, ?)', (student_id, name))
            db.commit()
        except sqlite3.IntegrityError:
            flash('Student ID already exists.')
        db.close()
    return redirect(url_for('dashboard'))

@app.route('/delete_student_id/<int:id>')
def delete_student_id(id):
    if session.get('role') != 'teacher': return redirect(url_for('dashboard'))
    db = get_db()
    db.execute('DELETE FROM authorized_students WHERE id = ?', (id,))
    db.commit()
    db.close()
    return redirect(url_for('dashboard'))

# --- ASSIGNMENTS & NOTES & GRADING ---

@app.route('/create_assignment', methods=['POST'])
def create_assignment():
    if session.get('role') != 'teacher': return redirect(url_for('dashboard'))
    title, instructions, due_date = request.form.get('title'), request.form.get('instructions'), request.form.get('due_date')
    file = request.files.get('file')
    filename = file.filename if file else ''
    if file and filename: file.save(os.path.join(UPLOAD_ASSIGNMENTS, filename))
    db = get_db()
    db.execute('INSERT INTO assignments (title, filename, instructions, due_date) VALUES (?, ?, ?, ?)', (title, filename, instructions, due_date))
    db.commit()
    db.close()
    return redirect(url_for('dashboard'))

@app.route('/edit_assignment/<int:id>', methods=['POST'])
def edit_assignment(id):
    if session.get('role') != 'teacher': return redirect(url_for('dashboard'))
    title, instructions, due_date = request.form.get('title'), request.form.get('instructions'), request.form.get('due_date')
    file = request.files.get('file')
    db = get_db()
    if file and file.filename:
        filename = file.filename
        file.save(os.path.join(UPLOAD_ASSIGNMENTS, filename))
        db.execute('UPDATE assignments SET title=?, instructions=?, due_date=?, filename=? WHERE id=?', (title, instructions, due_date, filename, id))
    else:
        db.execute('UPDATE assignments SET title=?, instructions=?, due_date=? WHERE id=?', (title, instructions, due_date, id))
    db.commit()
    db.close()
    return redirect(url_for('dashboard'))

@app.route('/delete_assignment/<int:id>')
def delete_assignment(id):
    if session.get('role') != 'teacher': return redirect(url_for('dashboard'))
    db = get_db()
    assignment = db.execute('SELECT filename FROM assignments WHERE id = ?', (id,)).fetchone()
    if assignment and assignment['filename']:
        file_path = os.path.join(UPLOAD_ASSIGNMENTS, assignment['filename'])
        if os.path.exists(file_path): os.remove(file_path)
    db.execute('DELETE FROM assignments WHERE id = ?', (id,))
    db.commit()
    db.close()
    return redirect(url_for('dashboard'))

@app.route('/upload_note', methods=['POST'])
def upload_note():
    if session.get('role') != 'teacher': return redirect(url_for('dashboard'))
    title = request.form.get('title')
    file = request.files.get('file')
    if file and file.filename:
        filename = file.filename
        file.save(os.path.join(UPLOAD_NOTES, filename))
        db = get_db()
        db.execute('INSERT INTO notes (title, filename) VALUES (?, ?)', (title, filename))
        db.commit()
        db.close()
    return redirect(url_for('dashboard'))

@app.route('/delete_note/<int:id>')
def delete_note(id):
    if session.get('role') != 'teacher': return redirect(url_for('dashboard'))
    db = get_db()
    note = db.execute('SELECT filename FROM notes WHERE id = ?', (id,)).fetchone()
    if note and note['filename']:
        file_path = os.path.join(UPLOAD_NOTES, note['filename'])
        if os.path.exists(file_path): os.remove(file_path)
    db.execute('DELETE FROM notes WHERE id = ?', (id,))
    db.commit()
    db.close()
    return redirect(url_for('dashboard'))

@app.route('/submit_work', methods=['POST'])
def submit_work():
    if session.get('role') != 'student': return redirect(url_for('dashboard'))
    assignment_id = request.form.get('assignment_id')
    db = get_db()
    assignment = db.execute('SELECT due_date FROM assignments WHERE id = ?', (assignment_id,)).fetchone()
    
    if assignment and assignment['due_date'] and datetime.now() > datetime.strptime(assignment['due_date'], '%Y-%m-%dT%H:%M'):
        flash('Deadline has passed! Submission rejected.')
        db.close()
        return redirect(url_for('dashboard'))

    file = request.files.get('file')
    if file and file.filename:
        filename = f"{session['user_id']}_{file.filename}"
        file.save(os.path.join(UPLOAD_SUBMISSIONS, filename))
        db.execute('INSERT INTO submissions (assignment_id, student_id, filename, grade, feedback) VALUES (?, ?, ?, ?, ?)',
                   (assignment_id, session['user_id'], filename, 'Pending', 'No feedback yet'))
        db.commit()
    db.close()
    return redirect(url_for('dashboard'))

@app.route('/grade_submission/<int:id>', methods=['POST'])
def grade_submission(id):
    if session.get('role') != 'teacher': return redirect(url_for('dashboard'))
    grade, feedback = request.form.get('grade'), request.form.get('feedback')
    db = get_db()
    db.execute('UPDATE submissions SET grade = ?, feedback = ? WHERE id = ?', (grade, feedback, id))
    db.commit()
    db.close()
    return redirect(url_for('dashboard'))

@app.route('/download/<folder_type>/<path:filename>')
def download_file(folder_type, filename):
    folder_map = {'assignments': UPLOAD_ASSIGNMENTS, 'submissions': UPLOAD_SUBMISSIONS, 'notes': UPLOAD_NOTES}
    return send_from_directory(folder_map.get(folder_type), filename, as_attachment=True) if folder_type in folder_map else ("File not found", 404)

# --- JUSTICE TECH TERMINAL ANIMATION & LAUNCHER ---
def print_signature():
    os.system('cls' if os.name == 'nt' else 'clear')
    colors = ['\033[95m', '\033[94m', '\033[96m', '\033[92m', '\033[93m', '\033[91m']
    reset = '\033[0m'
    logo = """
 ███████╗██████╗ ██╗   ██╗██████╗  ██████╗ ██████╗ ████████╗ █████╗ ██╗     
 ██╔════╝██╔══██╗██║   ██║██╔══██╗██╔═══██╗██╔══██╗╚══██╔══╝██╔══██╗██║     
 █████╗  ██║  ██║██║   ██║██████╔╝██║   ██║██████╔╝   ██║   ███████║██║     
 ██╔══╝  ██║  ██║██║   ██║██╔═══╝ ██║   ██║██╔══██╗   ██║   ██╔══██║██║     
 ███████╗██████╔╝╚██████╔╝██║     ╚██████╔╝██║  ██║   ██║   ██║  ██║███████╗
 ╚══════╝╚═════╝  ╚═════╝ ╚═╝      ╚═════╝ ╚═╝  ╚═╝   ╚═╝   ╚═╝  ╚═╝╚══════╝
    """
    sys.stdout.write("\n")
    for i, line in enumerate(logo.strip().split('\n')):
        sys.stdout.write(colors[i % len(colors)] + line + reset + '\n')
        time.sleep(0.08)
    
    signature = "\n>>> Starting EduPortal System by JusticeTech <<<\n"
    for char in signature:
        sys.stdout.write('\033[1;96m' + char + reset)
        sys.stdout.flush()
        time.sleep(0.02)
    
    print("\n\n\033[1;92m[✓]\033[0m Database Migrations & System Initialized Successfully.")
    print("\033[1;93m[i]\033[0m Server running and listening for student connections at: \033[1;97mhttp://0.0.0.0:5000\033[0m\n")

if __name__ == '__main__':
    print_signature()
    # Debug is enabled to show explicit errors, use_reloader=False keeps the animation from printing twice
    app.run(host='0.0.0.0', port=5000, debug=True, use_reloader=False)