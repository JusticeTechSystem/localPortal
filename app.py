from flask import Flask, render_template, request, send_from_directory, redirect, url_for
import os

app = Flask(__name__)

# Configure upload paths
ASSIGNMENT_FOLDER = os.path.join(os.path.dirname(__file__), 'uploads', 'assignments')
SUBMISSION_FOLDER = os.path.join(os.path.dirname(__file__), 'uploads', 'submissions')

os.makedirs(ASSIGNMENT_FOLDER, exist_ok=True)
os.makedirs(SUBMISSION_FOLDER, exist_ok=True)

@app.route('/')
def index():
    assignments = os.listdir(ASSIGNMENT_FOLDER)
    submissions = os.listdir(SUBMISSION_FOLDER)
    return render_template('index.html', assignments=assignments, submissions=submissions)

@app.route('/upload_assignment', methods=['POST'])
def upload_assignment():
    file = request.files.get('file')
    if file and file.filename:
        file.save(os.path.join(ASSIGNMENT_FOLDER, file.filename))
    return redirect(url_for('index'))

@app.route('/upload_submission', methods=['POST'])
def upload_submission():
    file = request.files.get('file')
    if file and file.filename:
        file.save(os.path.join(SUBMISSION_FOLDER, file.filename))
    return redirect(url_for('index'))

@app.route('/download/<folder_type>/<filename>')
def download_file(folder_type, filename):
    folder = ASSIGNMENT_FOLDER if folder_type == 'assignments' else SUBMISSION_FOLDER
    return send_from_directory(folder, filename, as_attachment=True)

if __name__ == '__main__':
    app.run(host='127.0.0.1', port=5000, debug=True)