

from database import init_db, get_db_connection, log_activity
from werkzeug.security import generate_password_hash, check_password_hash
from functools import wraps
import os
import json
import sqlite3
from pyngrok import ngrok

app = Flask(__name__, static_folder='static', template_folder='templates')
app.secret_key = os.urandom(24) 

# Database setup check
init_db()
os.makedirs("static/snapshots", exist_ok=True)

# Camera initialized
cam = VideoStreamYOLO(conf_threshold=0.35).start()

# --- AUTHENTICATION DECORATOR ---
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'username' not in session:
            return redirect(url_for('login'))
        
        conn = get_db_connection()
        user = conn.execute("SELECT is_blocked FROM users WHERE username = ?", (session['username'],)).fetchone()
        conn.close()
        
        if user and user['is_blocked'] == 1:
            session.clear()
            return "Aapka account block kar diya gaya hai. Admin se sampark karein.", 403
            
        return f(*args, **kwargs)
    return decorated_function

# --- ROUTES ---

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username').strip()
        password = request.form.get('password')
        
        conn = get_db_connection()
        user = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        conn.close()
        
        if user and check_password_hash(user['password_hash'], password):
            if user['is_blocked'] == 1:
                return render_template('login.html', error="Aapka account blocked hai!")
                
            session['username'] = user['username']
            session['role'] = user['role']
            log_activity(user['username'], "LOGIN", "User logged in successfully")
            return redirect(url_for('index'))
        else:
            return render_template('login.html', error="Galat Username ya Password!")
            
    return render_template('login.html')

@app.route('/logout')
def logout():
    if 'username' in session:
        log_activity(session['username'], "LOGOUT", "User logged out")
    session.clear()
    return redirect(url_for('login'))

@app.route('/')
@login_required
def index():
    return render_template('index.html', username=session['username'], role=session['role'])

@app.route('/roi')
@login_required
def roi_page():
    return render_template('roi.html')

def gen_frames(camera):
    while True:
        frame = camera.get_frame()
        if frame is None: continue
        yield (b'--frame\r\n'
               b'Content-Type: image/jpeg\r\n\r\n' + frame + b'\r\n\r\n')

@app.route('/video_feed')
@login_required
def video_feed():
    return Response(gen_frames(cam), mimetype='multipart/x-mixed-replace; boundary=frame')

@app.route('/system_status')
@login_required
def system_status():
    status = {
        "camera_connected": cam.stream.isOpened(),
        "ai_engine_running": cam.started,
        "latest_incident": cam.latest_incident,
        "manual_alarm_active": cam.manual_alarm_active
    }
    cam.latest_incident = None 
    return jsonify(status)

# --- NEW: MANUAL ALARM TESTING ROUTE ---
@app.route('/trigger_manual_alarm', methods=['POST'])
@login_required
def trigger_manual_alarm():
    data = request.get_json()
    action = data.get('action') # 'ON' ya 'OFF'
    
    if action == 'ON':
        cam.manual_alarm_active = True
        cam.set_alarm_state(True)
        log_activity(session['username'], "ALARM_TEST", "Manual Alarm Turned ON")
        return jsonify({"status": "success", "message": "Manual Alarm Activated (HIGH)"})
    elif action == 'OFF':
        cam.manual_alarm_active = False
        cam.set_alarm_state(False)
        log_activity(session['username'], "ALARM_TEST", "Manual Alarm Turned OFF")
        return jsonify({"status": "success", "message": "Manual Alarm Deactivated (LOW)"})
    
    return jsonify({"status": "error", "message": "Invalid Action"}), 400


@app.route('/save_roi', methods=['POST'])
@login_required
def save_roi():
    data = request.get_json()
    points = data.get('points', [])
    with open("roi_coordinates.txt", "w") as f:
        json.dump(points, f)
    cam.load_roi()
    log_activity(session['username'], "UPDATE_ROI", f"New coordinates: {points}")
    return jsonify({"status": "success", "message": "ROI Saved Successfully!"})

@app.route('/get_roi', methods=['GET'])
@login_required
def get_roi():
    return jsonify({"points": cam.roi_points})

# --- USER MANAGEMENT API ROUTES ---
@app.route('/user_management')
@login_required
def user_management_page():
    if session.get('role') != 'admin':
        return "Access Denied: Is page ke liye Admin privileges chahiye.", 403
        
    conn = get_db_connection()
    users = conn.execute("SELECT id, username, role, is_blocked, is_flagged, created_at FROM users WHERE username != 'admin'").fetchall()
    logs = conn.execute("SELECT * FROM activity_logs ORDER BY timestamp DESC LIMIT 100").fetchall()
    conn.close()
    return render_template('users.html', users=users, logs=logs)

@app.route('/add_user', methods=['POST'])
@login_required
def add_user():
    if session.get('role') != 'admin': return jsonify({"status": "error", "msg": "Unauthorized"}), 403
    
    username = request.form.get('username').strip()
    password = request.form.get('password')
    role = request.form.get('role', 'operator')
    
    if not username or not password:
        return jsonify({"status": "error", "msg": "Fields khali nahi chhod sakte!"})
        
    hashed_pw = generate_password_hash(password)
    try:
        conn = get_db_connection()
        conn.execute("INSERT INTO users (username, password_hash, role) VALUES (?, ?, ?)", (username, hashed_pw, role))
        conn.commit()
        conn.close()
        log_activity(session['username'], "ADD_USER", f"Added user: {username} with role: {role}")
        return jsonify({"status": "success", "msg": "User successfully add ho gaya!"})
    except sqlite3.IntegrityError:
        return jsonify({"status": "error", "msg": "Username pehle se hi exist karta hai!"})

@app.route('/update_user_status/<int:user_id>/<string:action>', methods=['POST'])
@login_required
def update_user_status(user_id, action):
    if session.get('role') != 'admin': return jsonify({"status": "error", "msg": "Unauthorized"}), 403
    
    conn = get_db_connection()
    user = conn.execute("SELECT username FROM users WHERE id = ?", (user_id,)).fetchone()
    if not user:
        conn.close()
        return jsonify({"status": "error", "msg": "User nahi mila!"})
        
    target_username = user['username']
    
    if action == "block":
        conn.execute("UPDATE users SET is_blocked = 1 WHERE id = ?", (user_id,))
        log_activity(session['username'], "BLOCK_USER", f"Blocked user: {target_username}")
    elif action == "unblock":
        conn.execute("UPDATE users SET is_blocked = 0 WHERE id = ?", (user_id,))
        log_activity(session['username'], "UNBLOCK_USER", f"Unblocked user: {target_username}")
    elif action == "flag":
        conn.execute("UPDATE users SET is_flagged = 1 WHERE id = ?", (user_id,))
        log_activity(session['username'], "FLAG_USER", f"Flagged user: {target_username}")
    elif action == "unflag":
        conn.execute("UPDATE users SET is_flagged = 0 WHERE id = ?", (user_id,))
        log_activity(session['username'], "UNFLAG_USER", f"Unflagged user: {target_username}")
    elif action == "delete":
        conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
        log_activity(session['username'], "DELETE_USER", f"Deleted user account: {target_username}")
        
    conn.commit()
    conn.close()
    return jsonify({"status": "success", "msg": f"Action '{action}' successfully completed."})

@app.route('/incident_logs')
@login_required
def incident_logs_page():
    conn = get_db_connection()
    incidents = conn.execute("SELECT * FROM incident_logs ORDER BY id DESC").fetchall()
    conn.close()
    return render_template('log.html', incidents=incidents)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False, threaded=True, use_reloader=False)
