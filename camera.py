import os
import cv2
import time
import json
import numpy as np
from threading import Thread, Lock
from datetime import datetime
from urllib.parse import quote
from ultralytics import YOLO
from database import log_incident  

# --- RASPBERRY PI GPIO HARDWARE INTEGRATION ---
try:
    import RPi.GPIO as GPIO
    GPIO_AVAILABLE = True
except ImportError:
    print("[WARNING] RPi.GPIO library nahi mili! Hardware emulation mode me chal raha hai.")
    GPIO_AVAILABLE = False

# Aapki shell script ke anusar PIN 18 select kiya gaya hai
ALARM_PIN = 18  

class VideoStreamYOLO:
    def __init__(self, model_path="yolo11s.pt", conf_threshold=0.35):
        self.username = "admin"
        self.password = quote("Nokia@100a", safe="")
        self.ip = "192.168.1.250"
        self.port = 554
        self.path = "/video/live?channel=1&subtype=0"
        self.rtsp_url = f"rtsp://{self.username}:{self.password}@{self.ip}:{self.port}{self.path}"
        
        print("[INFO] Loading YOLOv11 Model...")
        self.model = YOLO(model_path)
        self.conf_threshold = conf_threshold
        
        self.snapshot_dir = "static/snapshots" 
        if not os.path.exists(self.snapshot_dir):
            os.makedirs(self.snapshot_dir, exist_ok=True)

        self.stream = cv2.VideoCapture(self.rtsp_url)
        self.stream.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        
        (self.grabbed, self.frame) = self.stream.read()
        self.started = False
        self.read_lock = Lock()
        self.latest_incident = None 
        
        # Hardware control properties
        self.manual_alarm_active = False  # Manual override track karne ke liye
        
        # Initialize Raspberry Pi Pins
        self.init_hardware()

        # Load ROI settings
        self.roi_points = []
        self.load_roi()

    def init_hardware(self):
        if GPIO_AVAILABLE:
            try:
                GPIO.setwarnings(False)
                GPIO.setmode(GPIO.BCM)  # Broadcom pin-numbering scheme
                GPIO.setup(ALARM_PIN, GPIO.OUT)
                GPIO.output(ALARM_PIN, GPIO.LOW)  # Shuruat me LOW (0V) rahega
                print(f"[SUCCESS] GPIO Hardware Initialized on PIN: {ALARM_PIN} (Output Mode)")
            except Exception as e:
                print(f"[ERROR] GPIO Initialization Failed: {e}")

    def set_alarm_state(self, state):
        """Manual ya Automatic state change handle karne ke liye utility function"""
        if GPIO_AVAILABLE:
            try:
                if state:
                    GPIO.output(ALARM_PIN, GPIO.HIGH)
                    print(f"[HARDWARE] Pin {ALARM_PIN} -> HIGH (3.3V)")
                else:
                    # Agar manual test active nahi hai tabhi low karein
                    if not self.manual_alarm_active:
                        GPIO.output(ALARM_PIN, GPIO.LOW)
                        print(f"[HARDWARE] Pin {ALARM_PIN} -> LOW (0V)")
            except Exception as e:
                print(f"[ERROR] GPIO control failed: {e}")
        else:
            status_str = "HIGH (3.3V)" if state else "LOW (0V)"
            print(f"[EMULATION] Pin {ALARM_PIN} state: {status_str}")

    def load_roi(self):
        try:
            if os.path.exists("roi_coordinates.txt"):
                with open("roi_coordinates.txt", "r") as f:
                    self.roi_points = json.load(f)
                print(f"[INFO] Loaded ROI coordinates successfully: {self.roi_points}")
            else:
                self.roi_points = []
        except Exception as e:
            print(f"[ERROR] Loading ROI failed: {e}")
            self.roi_points = []

    def start(self):
        if self.started: return self
        self.started = True
        self.thread = Thread(target=self.update, args=())
        self.thread.daemon = True
        self.thread.start()
        return self

    def update(self):
        while self.started:
            (grabbed, frame) = self.stream.read()
            if not grabbed:
                self.stream.open(self.rtsp_url)
                time.sleep(2)
                continue
            with self.read_lock:
                self.frame = frame

    def is_inside_roi(self, px, py):
        if not self.roi_points or len(self.roi_points) < 3:
            return True
        return cv2.pointPolygonTest(np.array(self.roi_points, dtype=np.int32), (px, py), False) >= 0

    def get_frame(self):
        with self.read_lock:
            if self.frame is None: return None
            frame_to_process = self.frame.copy()

        if len(self.roi_points) >= 3:
            cv2.polylines(frame_to_process, [np.array(self.roi_points, dtype=np.int32)], True, (255, 255, 0), 2)

        results = self.model(frame_to_process, verbose=False, conf=self.conf_threshold)[0]
        human_detected = False
        highest_conf = 0.0

        for box in results.boxes:
            class_id = int(box.cls[0])
            conf = float(box.conf[0])
            
            if class_id == 0: # Person only
                x1, y1, x2, y2 = map(int, box.xyxy[0])
                center_x = int((x1 + x2) / 2)
                center_y = int(y2)

                if self.is_inside_roi(center_x, center_y):
                    human_detected = True
                    if conf > highest_conf: highest_conf = conf
                    
                    cv2.rectangle(frame_to_process, (x1, y1), (x2, y2), (0, 0, 255), 3)
                    cv2.circle(frame_to_process, (center_x, center_y), 5, (0, 0, 255), -1)
                    cv2.putText(frame_to_process, f"VIOLATION: {conf:.2%}", (x1, y1 - 10), 
                                cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 255), 2)

        # Agar human detect hua toh Pin 18 HIGH karo, nahi toh LOW karo
        if human_detected:
            self.capture_snapshot(frame_to_process, highest_conf)
            self.set_alarm_state(True)
        else:
            # Automatic system tabhi low karega jab manual test button off ho
            if not self.manual_alarm_active:
                self.set_alarm_state(False)

        ret, jpeg = cv2.imencode('.jpg', frame_to_process)
        return jpeg.tobytes() if ret else None

    def capture_snapshot(self, frame, conf):
        now = datetime.now()
        timestamp_str = now.strftime("%Y-%m-%d %H:%M:%S")
        file_ts = now.strftime("%Y%m%d_%H%M%S_%f")[:-3]
        
        filename = f"HUMAN_INTRUSION_{file_ts}.jpg"
        filepath = os.path.join(self.snapshot_dir, filename)
        
        cv2.imwrite(filepath, frame)
        
        confidence_pct = f"{conf:.2%}"
        image_route_url = f"/static/snapshots/{filename}"
        alarm_triggered = "TRIGGERED / ACTIVE"  

        self.latest_incident = {
            "timestamp": timestamp_str,
            "confidence": confidence_pct,
            "image_url": image_route_url
        }

        try:
            log_incident(timestamp_str, confidence_pct, alarm_triggered, image_route_url)
            print(f"[CRITICAL] Snapshot & DB Log Generated: {filename}")
        except Exception as e:
            print(f"[ERROR] Failed to write incident to DB: {e}")

    def stop(self):
        self.started = False
        self.stream.release()
        if GPIO_AVAILABLE:
            GPIO.cleanup()
