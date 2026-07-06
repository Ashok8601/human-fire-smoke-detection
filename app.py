import os
import cv2
import time
import json
import serial
import sqlite3
import smtplib
import logging
import requests
import subprocess
import threading
from pathlib import Path
from datetime import datetime
from functools import wraps
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.image import MIMEImage

from flask import Flask, render_template, Response, jsonify, request, redirect, url_for, session
from werkzeug.security import generate_password_hash, check_password_hash
from ultralytics import YOLO
from waitress import serve
from dotenv import load_dotenv


# ==========================================================
# ENV + BASIC SETUP
# ==========================================================
load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "system.db"

STATIC_DIR = BASE_DIR / "static"
SNAPSHOT_DIR = STATIC_DIR / "snapshots"

FOLDERS = {
    "fire": SNAPSHOT_DIR / "fire",
    "smoke": SNAPSHOT_DIR / "smoke",
    "human": SNAPSHOT_DIR / "human",
}

for folder in FOLDERS.values():
    folder.mkdir(parents=True, exist_ok=True)

LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    filename=LOG_DIR / "system.log",
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

console = logging.StreamHandler()
console.setLevel(logging.INFO)
logging.getLogger("").addHandler(console)

app = Flask(__name__)
app.secret_key = os.getenv("APP_SECRET_KEY", "CHANGE_THIS_SECRET_KEY_NOW")

cv2.setNumThreads(int(os.getenv("OPENCV_THREADS", "2")))


# ==========================================================
# CONFIG FROM ENV
# ==========================================================
SMTP_SERVER = os.getenv("SMTP_SERVER", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SENDER_EMAIL = os.getenv("SENDER_EMAIL", "")
SENDER_PASSWORD = os.getenv("SENDER_PASSWORD", "")

ESP32_IP = os.getenv("ESP32_IP", "192.168.1.21")

CAMERA_INDEX = int(os.getenv("CAMERA_INDEX", "0"))
CAMERA_WIDTH = int(os.getenv("CAMERA_WIDTH", "640"))
CAMERA_HEIGHT = int(os.getenv("CAMERA_HEIGHT", "480"))
CAMERA_FPS = int(os.getenv("CAMERA_FPS", "15"))

FIRE_MODEL_PATH = os.getenv("FIRE_MODEL_PATH", "best.pt")
HUMAN_MODEL_PATH = os.getenv("HUMAN_MODEL_PATH", "yolov8n.pt")

GPIO_PIN = os.getenv("GPIO_PIN", "18")
SERVER_HOST = os.getenv("SERVER_HOST", "0.0.0.0")
SERVER_PORT = int(os.getenv("SERVER_PORT", "8000"))

AI_LOOP_SLEEP = float(os.getenv("AI_LOOP_SLEEP", "0.08"))
ALERT_SAVE_COOLDOWN = int(os.getenv("ALERT_SAVE_COOLDOWN", "10"))
ACTUATOR_MIN_INTERVAL = float(os.getenv("ACTUATOR_MIN_INTERVAL", "1.5"))


# ==========================================================
# GLOBAL STATE
# ==========================================================
camera = None
serial_port = None

latest_frame = None
output_frame = None

frame_lock = threading.Lock()
config_lock = threading.Lock()
state_lock = threading.Lock()

sys_conf = {}
system_state = {
    "camera_ok": False,
    "fire_model_ok": False,
    "human_model_ok": False,
    "last_camera_frame": None,
    "last_error": "",
    "last_alert": None,
}

model_fire = None
model_base = None

last_actuator_state = {
    "gpio": None,
    "wifi": None,
    "last_time": 0
}


# ==========================================================
# DATABASE
# ==========================================================
def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            username TEXT PRIMARY KEY,
            password_hash TEXT NOT NULL,
            role TEXT DEFAULT 'operator',
            created_at TEXT NOT NULL
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            alert_type TEXT NOT NULL,
            accuracy TEXT NOT NULL,
            image_url TEXT,
            created_at TEXT NOT NULL
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS audit_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event TEXT NOT NULL,
            detail TEXT,
            created_at TEXT NOT NULL
        )
    """)

    cur.execute("SELECT username FROM users WHERE username='admin'")
    if not cur.fetchone():
        admin_password = os.getenv("ADMIN_DEFAULT_PASSWORD", "admin123")
        cur.execute(
            "INSERT INTO users(username, password_hash, role, created_at) VALUES (?, ?, ?, ?)",
            ("admin", generate_password_hash(admin_password), "admin", datetime.now().isoformat())
        )

    default_configs = {
        "fire_on": "1",
        "smoke_on": "1",
        "human_on": "1",

        "fire_thresh": "0.45",
        "smoke_thresh": "0.45",
        "human_thresh": "0.50",

        "gpio_alarm_fire": "1",
        "gpio_alarm_smoke": "0",
        "gpio_alarm_human": "0",

        "wifi_alarm_fire": "1",
        "wifi_alarm_smoke": "0",
        "wifi_alarm_human": "0",

        "email_alert_fire": "1",
        "email_alert_smoke": "0",
        "email_alert_human": "0",

        "alarm_manual_override": "0",
        "email_recipients": "security@firm.com,manager@firm.com",
    }

    for key, value in default_configs.items():
        cur.execute("SELECT key FROM settings WHERE key=?", (key,))
        if not cur.fetchone():
            cur.execute("INSERT INTO settings(key, value) VALUES (?, ?)", (key, value))

    conn.commit()
    conn.close()
    update_config_cache()
    add_audit_log("SYSTEM_START", "Database initialized")


def add_audit_log(event, detail=""):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO audit_logs(event, detail, created_at) VALUES (?, ?, ?)",
            (event, detail, datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logging.error(f"Audit log failed: {e}")


def update_config_cache():
    global sys_conf

    conn = get_db()
    rows = conn.execute("SELECT key, value FROM settings").fetchall()
    conn.close()

    mem = {}
    for row in rows:
        key = row["key"]
        value = row["value"]

        if key == "email_recipients":
            mem[key] = str(value)
        elif "thresh" in key:
            try:
                mem[key] = float(value)
            except ValueError:
                mem[key] = 0.5
        else:
            mem[key] = value == "1"

    with config_lock:
        sys_conf = mem


def get_config():
    with config_lock:
        return dict(sys_conf)


def save_alert_to_db(alert_type, accuracy, image_url):
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO alerts(alert_type, accuracy, image_url, created_at) VALUES (?, ?, ?, ?)",
        (alert_type.upper(), accuracy, image_url, datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    )
    conn.commit()
    conn.close()


# ==========================================================
# MODEL LOADING
# ==========================================================
def load_models():
    global model_fire, model_base

    try:
        model_fire = YOLO(FIRE_MODEL_PATH)
        system_state["fire_model_ok"] = True
        logging.info("Fire/smoke model loaded")
    except Exception as e:
        system_state["fire_model_ok"] = False
        system_state["last_error"] = f"Fire model failed: {e}"
        logging.error(system_state["last_error"])

    try:
        model_base = YOLO(HUMAN_MODEL_PATH)
        system_state["human_model_ok"] = True
        logging.info("Human model loaded")
    except Exception as e:
        system_state["human_model_ok"] = False
        system_state["last_error"] = f"Human model failed: {e}"
        logging.error(system_state["last_error"])


# ==========================================================
# CAMERA + HARDWARE
# ==========================================================
def open_camera():
    cam = cv2.VideoCapture(CAMERA_INDEX, cv2.CAP_V4L2)

    cam.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
    cam.set(cv2.CAP_PROP_FRAME_WIDTH, CAMERA_WIDTH)
    cam.set(cv2.CAP_PROP_FRAME_HEIGHT, CAMERA_HEIGHT)
    cam.set(cv2.CAP_PROP_FPS, CAMERA_FPS)
    cam.set(cv2.CAP_PROP_BUFFERSIZE, 1)

    return cam


def camera_capture_worker():
    global camera, latest_frame

    while True:
        try:
            if camera is None or not camera.isOpened():
                system_state["camera_ok"] = False
                logging.warning("Camera not opened. Reconnecting...")
                camera = open_camera()
                time.sleep(2)
                continue

            success, frame = camera.read()

            if not success or frame is None:
                system_state["camera_ok"] = False
                system_state["last_error"] = "Camera frame read failed"
                logging.warning("Camera frame read failed. Reopening camera.")
                try:
                    camera.release()
                except Exception:
                    pass
                camera = None
                time.sleep(1)
                continue

            with frame_lock:
                latest_frame = frame.copy()

            system_state["camera_ok"] = True
            system_state["last_camera_frame"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        except Exception as e:
            system_state["camera_ok"] = False
            system_state["last_error"] = f"Camera worker error: {e}"
            logging.error(system_state["last_error"])
            time.sleep(2)

        time.sleep(0.01)


def setup_gpio():
    try:
        subprocess.run(["pinctrl", "set", GPIO_PIN, "op"], check=True)
        logging.info(f"GPIO {GPIO_PIN} configured as output")
    except Exception as e:
        logging.warning(f"GPIO setup skipped/failed: {e}")


def setup_serial():
    global serial_port

    for port in ["/dev/ttyACM0", "/dev/ttyUSB0"]:
        try:
            serial_port = serial.Serial(port, 115200, timeout=0.1)
            logging.info(f"Serial connected: {port}")
            return
        except Exception:
            pass

    logging.warning("Serial device not found")


def start_hardware_interfaces():
    setup_gpio()
    setup_serial()
    threading.Thread(target=camera_capture_worker, daemon=True).start()


# ==========================================================
# ACTUATORS
# ==========================================================
def trigger_wifi_alarm_worker(state):
    try:
        endpoint = "on" if state else "off"
        requests.get(f"http://{ESP32_IP.strip()}/led/{endpoint}", timeout=0.5)
    except Exception as e:
        logging.warning(f"ESP32 WiFi alarm failed: {e}")


def set_gpio(state):
    cmd = "dh" if state else "dl"
    try:
        subprocess.run(["pinctrl", "set", GPIO_PIN, cmd], check=True)
    except Exception as e:
        logging.warning(f"GPIO trigger failed: {e}")


def set_serial(state):
    global serial_port
    if serial_port and serial_port.is_open:
        try:
            serial_port.write(b"1" if state else b"0")
        except Exception as e:
            logging.warning(f"Serial trigger failed: {e}")


def trigger_hardware_actuators(gpio_state, wifi_state):
    now = time.time()

    with state_lock:
        same_state = (
            last_actuator_state["gpio"] == gpio_state
            and last_actuator_state["wifi"] == wifi_state
        )

        too_fast = now - last_actuator_state["last_time"] < ACTUATOR_MIN_INTERVAL

        if same_state and too_fast:
            return

        last_actuator_state["gpio"] = gpio_state
        last_actuator_state["wifi"] = wifi_state
        last_actuator_state["last_time"] = now

    set_gpio(gpio_state)
    set_serial(gpio_state)
    threading.Thread(target=trigger_wifi_alarm_worker, args=(wifi_state,), daemon=True).start()


# ==========================================================
# EMAIL ALERT
# ==========================================================
def send_email_worker(subject, body, img_path, recipients_str):
    if not SENDER_EMAIL or not SENDER_PASSWORD:
        logging.warning("Email skipped: SMTP credentials missing")
        return

    recipients = [r.strip() for r in recipients_str.split(",") if r.strip()]
    if not recipients:
        logging.warning("Email skipped: no recipients")
        return

    try:
        msg = MIMEMultipart()
        msg["From"] = SENDER_EMAIL
        msg["To"] = ", ".join(recipients)
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "html"))

        if img_path and os.path.exists(img_path):
            with open(img_path, "rb") as f:
                img_data = f.read()
            msg.attach(MIMEImage(img_data, name=os.path.basename(img_path)))

        server = smtplib.SMTP(SMTP_SERVER, SMTP_PORT, timeout=10)
        server.starttls()
        server.login(SENDER_EMAIL, SENDER_PASSWORD)
        server.sendmail(SENDER_EMAIL, recipients, msg.as_string())
        server.quit()

        logging.info("Email alert sent")

    except Exception as e:
        logging.error(f"Email alert failed: {e}")


# ==========================================================
# AI ENGINE
# ==========================================================
def draw_label(frame, text, x, y, color):
    cv2.putText(
        frame,
        text,
        (x, max(25, y - 12)),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.6,
        color,
        2
    )


def ai_inference_engine():
    global output_frame

    last_save = {
        "fire": 0,
        "smoke": 0,
        "human": 0,
    }

    while True:
        try:
            with frame_lock:
                if latest_frame is None:
                    frame = None
                else:
                    frame = latest_frame.copy()

            if frame is None:
                time.sleep(0.05)
                continue

            conf = get_config()
            annotated = frame.copy()

            detect_flags = {
                "fire": False,
                "smoke": False,
                "human": False,
            }

            alert_details = []

            # ---------------- Fire / Smoke ----------------
            if model_fire is not None and (conf.get("fire_on") or conf.get("smoke_on")):
                try:
                    results_fire = model_fire(frame, verbose=False, imgsz=640)

                    for r in results_fire:
                        for box in r.boxes:
                            score = float(box.conf[0])
                            cls_id = int(box.cls[0])
                            label = str(model_fire.names[cls_id]).lower()

                            x1, y1, x2, y2 = map(int, box.xyxy[0])

                            if "fire" in label and conf.get("fire_on") and score >= conf.get("fire_thresh", 0.45):
                                detect_flags["fire"] = True
                                alert_details.append(("fire", score))
                                cv2.rectangle(annotated, (x1, y1), (x2, y2), (0, 0, 255), 3)
                                draw_label(annotated, f"FIRE {score*100:.1f}%", x1, y1, (0, 0, 255))

                            elif "smoke" in label and conf.get("smoke_on") and score >= conf.get("smoke_thresh", 0.45):
                                detect_flags["smoke"] = True
                                alert_details.append(("smoke", score))
                                cv2.rectangle(annotated, (x1, y1), (x2, y2), (120, 120, 120), 3)
                                draw_label(annotated, f"SMOKE {score*100:.1f}%", x1, y1, (120, 120, 120))

                except Exception as e:
                    system_state["fire_model_ok"] = False
                    system_state["last_error"] = f"Fire/smoke inference failed: {e}"
                    logging.error(system_state["last_error"])

            # ---------------- Human ----------------
            if model_base is not None and conf.get("human_on"):
                try:
                    results_human = model_base(frame, classes=[0], verbose=False, imgsz=640)

                    for r in results_human:
                        for box in r.boxes:
                            score = float(box.conf[0])

                            if score >= conf.get("human_thresh", 0.50):
                                detect_flags["human"] = True
                                alert_details.append(("human", score))

                                x1, y1, x2, y2 = map(int, box.xyxy[0])
                                cv2.rectangle(annotated, (x1, y1), (x2, y2), (0, 255, 0), 3)
                                draw_label(annotated, f"HUMAN {score*100:.1f}%", x1, y1, (0, 255, 0))

                except Exception as e:
                    system_state["human_model_ok"] = False
                    system_state["last_error"] = f"Human inference failed: {e}"
                    logging.error(system_state["last_error"])

            # ---------------- Save alerts ----------------
            now = time.time()

            for det_type, detected in detect_flags.items():
                if detected and now - last_save[det_type] >= ALERT_SAVE_COOLDOWN:
                    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                    readable_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

                    img_name = f"{det_type}_{timestamp}.jpg"
                    save_path = FOLDERS[det_type] / img_name
                    cv2.imwrite(str(save_path), annotated)

                    last_save[det_type] = now

                    score = next((s for t, s in alert_details if t == det_type), 0.0)
                    accuracy = f"{score * 100:.1f}%"
                    image_url = f"/static/snapshots/{det_type}/{img_name}"

                    save_alert_to_db(det_type, accuracy, image_url)
                    add_audit_log("ALERT", f"{det_type.upper()} detected with {accuracy}")

                    system_state["last_alert"] = {
                        "type": det_type.upper(),
                        "accuracy": accuracy,
                        "time": readable_time,
                        "url": image_url
                    }

                    if conf.get(f"email_alert_{det_type}"):
                        subject = f"AI Safety Alert: {det_type.upper()}"
                        body = f"""
                        <h3>Industrial Safety Alert</h3>
                        <p><b>Detection:</b> {det_type.upper()}</p>
                        <p><b>Confidence:</b> {accuracy}</p>
                        <p><b>Time:</b> {readable_time}</p>
                        """
                        threading.Thread(
                            target=send_email_worker,
                            args=(subject, body, str(save_path), conf.get("email_recipients", "")),
                            daemon=True
                        ).start()

            # ---------------- Actuator decision ----------------
            gpio_trigger = (
                conf.get("alarm_manual_override")
                or (detect_flags["fire"] and conf.get("gpio_alarm_fire"))
                or (detect_flags["smoke"] and conf.get("gpio_alarm_smoke"))
                or (detect_flags["human"] and conf.get("gpio_alarm_human"))
            )

            wifi_trigger = (
                conf.get("alarm_manual_override")
                or (detect_flags["fire"] and conf.get("wifi_alarm_fire"))
                or (detect_flags["smoke"] and conf.get("wifi_alarm_smoke"))
                or (detect_flags["human"] and conf.get("wifi_alarm_human"))
            )

            trigger_hardware_actuators(bool(gpio_trigger), bool(wifi_trigger))

            with frame_lock:
                output_frame = annotated.copy()

        except Exception as e:
            system_state["last_error"] = f"AI engine error: {e}"
            logging.error(system_state["last_error"])

        time.sleep(AI_LOOP_SLEEP)


# ==========================================================
# STREAM
# ==========================================================
def generate_frames():
    while True:
        with frame_lock:
            frame = None if output_frame is None else output_frame.copy()

        if frame is None:
            time.sleep(0.05)
            continue

        ok, buffer = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 75])
        if not ok:
            continue

        yield (
            b"--frame\r\n"
            b"Content-Type: image/jpeg\r\n\r\n"
            + buffer.tobytes()
            + b"\r\n"
        )

        time.sleep(1 / max(CAMERA_FPS, 1))


# ==========================================================
# AUTH
# ==========================================================
def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if "user" not in session:
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return wrapper


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        conn = get_db()
        user = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        conn.close()

        if user and check_password_hash(user["password_hash"], password):
            session["user"] = username
            add_audit_log("LOGIN", f"{username} logged in")
            return redirect(url_for("index"))

        return render_template("login.html", error="Invalid Operator Credentials")

    return render_template("login.html")


@app.route("/logout")
def logout():
    user = session.get("user", "unknown")
    session.pop("user", None)
    add_audit_log("LOGOUT", f"{user} logged out")
    return redirect(url_for("login"))


# ==========================================================
# ROUTES
# ==========================================================
@app.route("/")
@login_required
def index():
    return render_template("index.html")


@app.route("/settings")
@login_required
def settings():
    return render_template("settings.html", config=get_config())


@app.route("/users")
@login_required
def users_management():
    conn = get_db()
    users = conn.execute("SELECT username, role, created_at FROM users ORDER BY created_at DESC").fetchall()
    conn.close()
    return render_template("users.html", users=users)


@app.route("/logs")
@login_required
def system_logs():
    conn = get_db()
    logs = conn.execute("SELECT * FROM audit_logs ORDER BY id DESC LIMIT 100").fetchall()
    conn.close()
    return render_template("logs.html", logs=logs)


@app.route("/video_feed")
@login_required
def video_feed():
    return Response(generate_frames(), mimetype="multipart/x-mixed-replace; boundary=frame")


@app.route("/health")
def health():
    return jsonify(system_state)


@app.route("/get_alerts")
@login_required
def get_alerts():
    conn = get_db()
    rows = conn.execute(
        "SELECT alert_type, accuracy, image_url, created_at FROM alerts ORDER BY id DESC LIMIT 20"
    ).fetchall()
    conn.close()

    alerts = []
    for row in rows:
        alerts.append({
            "type": row["alert_type"],
            "accuracy": row["accuracy"],
            "time": row["created_at"],
            "url": row["image_url"]
        })

    return jsonify(alerts)


@app.route("/commit_settings", methods=["POST"])
@login_required
def commit_settings():
    data = request.json or {}

    allowed_keys = {
        "fire_on", "smoke_on", "human_on",
        "fire_thresh", "smoke_thresh", "human_thresh",
        "gpio_alarm_fire", "gpio_alarm_smoke", "gpio_alarm_human",
        "wifi_alarm_fire", "wifi_alarm_smoke", "wifi_alarm_human",
        "email_alert_fire", "email_alert_smoke", "email_alert_human",
        "alarm_manual_override",
        "email_recipients"
    }

    conn = get_db()
    cur = conn.cursor()

    try:
        for key, value in data.items():
            if key not in allowed_keys:
                continue

            if isinstance(value, bool):
                db_value = "1" if value else "0"
            else:
                db_value = str(value)

            cur.execute(
                "INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)",
                (key, db_value)
            )

        conn.commit()
        update_config_cache()
        add_audit_log("SETTINGS_UPDATE", json.dumps(data))
        return jsonify({"status": "success", "config": get_config()})

    except Exception as e:
        conn.rollback()
        logging.error(f"Settings update failed: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

    finally:
        conn.close()


@app.route("/manage_user_action", methods=["POST"])
@login_required
def manage_user_action():
    data = request.json or {}
    action = data.get("action")
    username = data.get("username", "").strip()
    password = data.get("password", "")

    if not username:
        return jsonify({"message": "Username required"}), 400

    conn = get_db()
    cur = conn.cursor()

    try:
        if action == "add":
            if not password:
                return jsonify({"message": "Password required"}), 400

            cur.execute(
                "INSERT INTO users(username, password_hash, role, created_at) VALUES (?, ?, ?, ?)",
                (username, generate_password_hash(password), "operator", datetime.now().isoformat())
            )
            conn.commit()
            add_audit_log("USER_ADD", f"{username} added")
            return jsonify({"message": "User created successfully"})

        if action == "delete":
            if username == "admin":
                return jsonify({"message": "Admin user cannot be deleted"}), 400

            cur.execute("DELETE FROM users WHERE username=?", (username,))
            conn.commit()
            add_audit_log("USER_DELETE", f"{username} deleted")
            return jsonify({"message": "User deleted successfully"})

        return jsonify({"message": "Invalid action"}), 400

    except sqlite3.IntegrityError:
        return jsonify({"message": "Username already exists"}), 409

    finally:
        conn.close()


# ==========================================================
# MAIN
# ==========================================================
if __name__ == "__main__":
    init_db()
    load_models()
    start_hardware_interfaces()

    threading.Thread(target=ai_inference_engine, daemon=True).start()

    logging.info(f"Server running on {SERVER_HOST}:{SERVER_PORT}")
    serve(app, host=SERVER_HOST, port=SERVER_PORT, threads=4)