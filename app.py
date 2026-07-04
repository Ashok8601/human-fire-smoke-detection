import os
import cv2
import time
import serial
import sqlite3
import smtplib
import requests  # WiFi alarm via ESP32 network request ke liye
import subprocess
import threading
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.image import MIMEImage
from flask import Flask, render_template, Response, jsonify, request, redirect, url_for, session
from ultralytics import YOLO

app = Flask(__name__)
app.secret_key = "INDUSTRIAL_SECURITY_SECRET_KEY_A1" # Session auth locker

# Performance Tuning for Pi
cv2.setNumThreads(4)

# ==================== STRICTLY ONCE-DEFINED CONFIGURATIONS ====================
SMTP_SERVER = "smtp.gmail.com"
SMTP_PORT = 587
SENDER_EMAIL = "ashokjuriya3521@gmail.com"  # FIXED: Gmail Sender Account
SENDER_PASSWORD = "zhmq dkug xmmr vegb"      # FIXED: Secure App Password

# ESP32 WiFi Alarm Settings (Dono same WiFi par hone chahiye)
# CRITICAL FIX: "1192.168.1.50" me se extra '1' hata kar sahi format kiya hai
ESP32_IP = "192.168.1.21"  # <-- Yahan apna real ESP32 IP Address dalein

DB_PATH = "system.db"
BASE_SNAPSHOT_DIR = os.path.join('static', 'snapshots')
FOLDERS = {
    'fire': os.path.join(BASE_SNAPSHOT_DIR, 'fire'),
    'smoke': os.path.join(BASE_SNAPSHOT_DIR, 'smoke'),
    'human': os.path.join(BASE_SNAPSHOT_DIR, 'human')
}
for folder_path in FOLDERS.values():
    os.makedirs(folder_path, exist_ok=True)

# Global Resources Lockers
camera = None
serial_port = None
captured_alerts = []

# AI Models Loading Safely
try:
    model_fire = YOLO('best.pt')
    model_base = YOLO('yolov8n.pt')
except Exception as e:
    print(f"❌ YOLO Initialization Failed: {e}")

# ==================== DATABASE INITIALIZATION ====================
def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # 1. User Management Table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            username TEXT PRIMARY KEY,
            password TEXT NOT NULL
        )
    ''')
    
    # 2. System Settings Config Table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    ''')
    
    # Insert Default Admin User safely if missing
    cursor.execute("SELECT * FROM users WHERE username='admin'")
    if not cursor.fetchone():
        cursor.execute("INSERT INTO users VALUES ('admin', 'admin123')")
        
    # Insert Default Configurations if empty
    default_configs = {
        'fire_on': '1', 'smoke_on': '1', 'human_on': '1',
        'fire_thresh': '0.45', 'smoke_thresh': '0.45', 'human_thresh': '0.50',
        'gpio_alarm_fire': '1', 'gpio_alarm_smoke': '0', 'gpio_alarm_human': '0',
        'wifi_alarm_fire': '1', 'wifi_alarm_smoke': '0', 'wifi_alarm_human': '0',
        'email_alert_fire': '1', 'email_alert_smoke': '0', 'email_alert_human': '0',
        'alarm_manual_override': '0',
        'email_recipients': 'security@firm.com, manager@firm.com'
    }
    
    for k, v in default_configs.items():
        cursor.execute("SELECT * FROM settings WHERE key=?", (k,))
        if not cursor.fetchone():
            cursor.execute("INSERT INTO settings VALUES (?, ?)", (k, v))
            
    conn.commit()
    conn.close()

def load_system_configs():
    """Helper method to load database context instantly into memory matrix"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM settings")
    rows = cursor.fetchall()
    conn.close()
    
    mem_config = {}
    for row in rows:
        key, val = row[0], row[1]
        if key in ['email_recipients']:
            mem_config[key] = str(val)
        elif 'thresh' in key:
            mem_config[key] = float(val)
        else:
            mem_config[key] = True if val == '1' else False
    return mem_config

# ==================== HARDWARE SYSTEMS BACKPLANE ====================
def start_hardware_interfaces():
    global camera, serial_port
    
    # LINUX FIX: CAP_DSHOW hata kar strictly standard V4L2 backend open kiya
    camera = cv2.VideoCapture(0, cv2.CAP_V4L2) 
    
    # IMX577 OPTIMIZATION: MJPEG stream trigger kiya taaki system freeze na ho
    camera.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*'MJPG'))
    camera.set(cv2.CAP_PROP_FRAME_WIDTH, 640)   # Balanced Resolution for AI
    camera.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
    camera.set(cv2.CAP_PROP_BUFFERSIZE, 1)      # Zero latency buffer

    if not camera.isOpened():
        print("❌ OpenCV could not hook IMX577 at index 0!")
    else:
        print("✅ Camera IMX577 operational in fast MJPEG mode.")
    
    # LINUX FIX: Raspberry Pi standard tty mappings
    try:
        serial_port = serial.Serial("/dev/ttyACM0", 115200, timeout=0.1)
    except Exception:
        try:
            serial_port = serial.Serial("/dev/ttyUSB0", 115200, timeout=0.1)
        except Exception:
            pass
            
    try:
        subprocess.run(["pinctrl", "set", "18", "op"], check=True)
    except Exception:
        pass

# ==================== FULLY UPDATED WIFI ALARM WORKER ====================
def trigger_wifi_alarm_worker(state):
    """Network requests are channeled here to eliminate vision pipeline latency"""
    base_url = f"http://{ESP32_IP.strip()}"
    try:
        if state:
            # Jab threat detected ho -> ESP32 Server Route: /led/on
            requests.get(f"{base_url}/led/on", timeout=1.0)
        else:
            # Jab clear ho state -> ESP32 Server Route: /led/off
            requests.get(f"{base_url}/led/off", timeout=1.0)
    except Exception:
        pass  # Network anomalies drops ko drop hone dein background me

def trigger_hardware_actuators(gpio_state, wifi_state):
    global serial_port
    
    # Local Pin Trigger
    cmd = "dh" if gpio_state else "dl"
    try:
        subprocess.run(["pinctrl", "set", "18", cmd], check=True)
    except Exception:
        pass

    # Serial Backup Trigger
    if serial_port and serial_port.is_open:
        try:
            signal = b'1' if wifi_state else b'0'
            serial_port.write(signal)
        except Exception:
            pass
            
    # WiFi Async Trigger to ESP32 over network (Matches your tested web server setup)
    threading.Thread(target=trigger_wifi_alarm_worker, args=(wifi_state,)).start()

# ==================== SECURE ASYNCHRONOUS NOTIFICATION WORKER ====================
def send_email_worker(subject, body, img_path, recipients_str):
    if not SENDER_EMAIL or "ashokjuriya" not in SENDER_EMAIL:
        return
    recipients = [r.strip() for r in recipients_str.split(',') if r.strip()]
    if not recipients:
        return
        
    try:
        msg = MIMEMultipart()
        msg['From'] = SENDER_EMAIL
        msg['To'] = ", ".join(recipients)
        msg['Subject'] = subject
        msg.attach(MIMEText(body, 'html'))

        if img_path and os.path.exists(img_path):
            with open(img_path, 'rb') as f:
                img_data = f.read()
            image = MIMEImage(img_data, name=os.path.basename(img_path))
            msg.attach(image)

        server = smtplib.SMTP(SMTP_SERVER, SMTP_PORT)
        server.starttls()
        server.login(SENDER_EMAIL, SENDER_PASSWORD)
        server.sendmail(SENDER_EMAIL, recipients, msg.as_string())
        server.quit()
        print("🚨 Email alert dispatched using fixed SMTP constants config!")
    except Exception as e:
        print(f"❌ Notification worker encountered an anomaly: {e}")

# ==================== MACHINE VISION VIDEO STREAM ENGINE ====================
def generate_frames():
    global captured_alerts, camera
    last_save = {'fire': 0, 'smoke': 0, 'human': 0}
    
    while True:
        if camera is None or not camera.isOpened():
            time.sleep(0.1)
            continue
            
        success, frame = camera.read()
        if not success or frame is None:
            time.sleep(0.01)
            continue
            
        # PERFORMANCE FIX: Purane frames queue ko flush karo taaki delay ya lag na aaye
        for _ in range(2):
            camera.grab()
        
        sys_conf = load_system_configs()
        annotated_frame = frame.copy()
        detect_flags = {'fire': False, 'smoke': False, 'human': False}
        alert_details = []

        # Fire / Smoke Pipeline
        if sys_conf['fire_on'] or sys_conf['smoke_on']:
            results_fire = model_fire(frame, verbose=False)
            for r in results_fire:
                for box in r.boxes:
                    conf = float(box.conf[0])
                    cls_id = int(box.cls[0])
                    label = model_fire.names[cls_id].lower()
                    
                    if 'fire' in label and sys_conf['fire_on'] and conf >= sys_conf['fire_thresh']:
                        detect_flags['fire'] = True
                        x1, y1, x2, y2 = map(int, box.xyxy[0])
                        cv2.rectangle(annotated_frame, (x1, y1), (x2, y2), (0, 0, 255), 3)
                        cv2.putText(annotated_frame, f"CRITICAL: FIRE {conf*100:.1f}%", (x1, y1-12), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 255), 2)
                        alert_details.append(('fire', conf))
                        
                    elif 'smoke' in label and sys_conf['smoke_on'] and conf >= sys_conf['smoke_thresh']:
                        detect_flags['smoke'] = True
                        x1, y1, x2, y2 = map(int, box.xyxy[0])
                        cv2.rectangle(annotated_frame, (x1, y1), (x2, y2), (90, 90, 90), 3)
                        cv2.putText(annotated_frame, f"WARNING: SMOKE {conf*100:.1f}%", (x1, y1-12), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (140, 140, 140), 2)
                        alert_details.append(('smoke', conf))

        # Human Tracking Pipeline
        if sys_conf['human_on']:
            results_base = model_base(frame, classes=[0], verbose=False) 
            for r in results_base:
                for box in r.boxes:
                    conf = float(box.conf[0])
                    if conf >= sys_conf['human_thresh']:
                        detect_flags['human'] = True
                        x1, y1, x2, y2 = map(int, box.xyxy[0])
                        cv2.rectangle(annotated_frame, (x1, y1), (x2, y2), (0, 255, 0), 3)
                        cv2.putText(annotated_frame, f"INTRUDER DETECTED {conf*100:.1f}%", (x1, y1-12), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 0), 2)
                        alert_details.append(('human', conf))

        # Core Snapshot Processor & Automation Trigger
        current_time = time.time()
        for det_type, detected in detect_flags.items():
            if detected and (current_time - last_save[det_type] > 8): 
                timestamp_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                file_timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                
                img_name = f"{det_type}_{file_timestamp}.jpg"
                save_path = os.path.join(FOLDERS[det_type], img_name)
                cv2.imwrite(save_path, annotated_frame)
                last_save[det_type] = current_time
                
                matching_conf = next((c for t, c in alert_details if t == det_type), 0.50)
                accuracy_pct = f"{matching_conf * 100:.1f}%"
                
                new_alert = {
                    'id': file_timestamp + "_" + det_type,
                    'type': det_type.upper(),
                    'accuracy': accuracy_pct,
                    'time': timestamp_str,
                    'url': f"/static/snapshots/{det_type}/{img_name}"
                }
                captured_alerts.insert(0, new_alert)
                captured_alerts = captured_alerts[:20]

                if sys_conf[f'email_alert_{det_type}']:
                    subject = f"🚨 AI SYSTEM BREAKOUT TELEMETRY: {det_type.upper()}"
                    html_body = f"<h3>Industrial Safety Automated Report</h3><p><b>Threat Vector:</b> {det_type.upper()}</p><p><b>Accuracy Score:</b> {accuracy_pct}</p><p><b>Timestamp:</b> {timestamp_str}</p>"
                    threading.Thread(target=send_email_worker, args=(subject, html_body, save_path, sys_conf['email_recipients'])).start()

        # Actuators Status Calculation
        gpio_trigger = sys_conf['alarm_manual_override'] or (detect_flags['fire'] and sys_conf['gpio_alarm_fire']) or (detect_flags['smoke'] and sys_conf['gpio_alarm_smoke']) or (detect_flags['human'] and sys_conf['gpio_alarm_human'])
        wifi_trigger = sys_conf['alarm_manual_override'] or (detect_flags['fire'] and sys_conf['wifi_alarm_fire']) or (detect_flags['smoke'] and sys_conf['wifi_alarm_smoke']) or (detect_flags['human'] and sys_conf['wifi_alarm_human'])
        
        # This calls our async threading function that updates the ESP32 pin state instantly
        trigger_hardware_actuators(gpio_trigger, wifi_trigger)

        ret, buffer = cv2.imencode('.jpg', annotated_frame)
        if ret:
            yield (b'--frame\r\nContent-Type: image/jpeg\r\n\r\n' + buffer.tobytes() + b'\r\n')

# ==================== FLASK SECURITY AUTH AND ROUTING CORE ====================
def login_required(f):
    from functools import wraps
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if "user" not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM users WHERE username=? AND password=?", (username, password))
        user_match = cursor.fetchone()
        conn.close()
        
        if user_match:
            session['user'] = username
            return redirect(url_for('index'))
        else:
            return render_template('login.html', error="Invalid Operator Credentials")
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.pop('user', None)
    return redirect(url_for('login'))

@app.route('/')
@login_required
def index():
    return render_template('index.html')

@app.route('/settings')
@login_required
def settings():
    return render_template('settings.html', config=load_system_configs())

@app.route('/users')
@login_required
def users_management():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT username FROM users")
    all_users = [row[0] for row in cursor.fetchall()]
    conn.close()
    return render_template('users.html', users=all_users)

# ==================== REST APIS FOR DASHBOARD MANAGEMENT ====================
@app.route('/video_feed')
def video_feed():
    return Response(generate_frames(), mimetype='multipart/x-mixed-replace; boundary=frame')

@app.route('/get_alerts')
def get_alerts():
    return jsonify(captured_alerts)

@app.route('/commit_settings', methods=['POST'])
@login_required
def commit_settings():
    data = request.json
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    try:
        for key, value in data.items():
            if isinstance(value, bool):
                db_val = '1' if value else '0'
            else:
                db_val = str(value)
            cursor.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, db_val))
        conn.commit()
        status = "success"
    except Exception as e:
        conn.rollback()
        status = f"Database Commit Failure: {e}"
    finally:
        conn.close()
        
    return jsonify({"status": status, "refreshed_config": load_system_configs()})

@app.route('/manage_user_action', methods=['POST'])
@login_required
def manage_user_action():
    data = request.json
    action = data.get('action')
    username = data.get('username')
    password = data.get('password')
    
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    msg = "Action Acknowledged"
    
    if action == 'add' and username and password:
        try:
            cursor.execute("INSERT INTO users VALUES (?, ?)", (username, password))
            conn.commit()
            msg = "User Registered Successfully"
        except sqlite3.IntegrityError:
            msg = "Username identity collisions exist."
    elif action == 'delete' and username:
        if username == 'admin':
            msg = "Cannot liquidate terminal superuser master root admin account profile."
        else:
            cursor.execute("DELETE FROM users WHERE username=?", (username,))
            conn.commit()
            msg = "Identity successfully wiped out"
            
    conn.close()
    return jsonify({"message": msg})
@app.route('/logs')
def system_logs():
    # Maan lo aapka alerts ka data kisi list ya function se aata hai (jaise /get_alerts me use ho raha hai)
    # Agar aapke paas 'all_alerts' naam ki list hai, toh hum use template me bhejenge
    try:
        # Agar aapke backend me alerts ki koi global list hai (jaise: global_alerts_list)
        # Toh use yahan pass kar do. Abhi ke liye hum dummy fallback ke saath de rahe hain:
        detected_logs = [
            {"time": "2026-07-04 15:02:11", "type": "FIRE", "severity": "Critical", "msg": "Threat signature matched in Sector B-4 (Confidence: 94.2%)"},
            {"time": "2026-07-04 14:55:03", "type": "SMOKE", "severity": "Warning", "msg": "Dense particles registered on primary optical lens"},
            {"time": "2026-07-04 14:30:00", "type": "HUMAN", "severity": "Info", "msg": "Human detected near perimeter gate 2"},
        ]
        return render_template('logs.html', logs=detected_logs)
    except Exception as e:
        return f"Log system error: {str(e)}", 500

if __name__ == '__main__':
    start_hardware_interfaces()
    from waitress import serve
    print("🚀 Waitress production server running on port 8000...")
    serve(app, host='0.0.0.0', port=8000, threads=4)
'''if __name__ == '__main__':
    init_db()
    start_hardware_interfaces()
    app.run(debug=False, host='0.0.0.0', port=5000, threaded=True)'''
