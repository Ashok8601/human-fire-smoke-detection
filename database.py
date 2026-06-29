import sqlite3
from werkzeug.security import generate_password_hash
import os

DB_NAME = "security_system.db"

def get_db_connection():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # 1. Users Table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            role TEXT DEFAULT 'operator',
            is_blocked INTEGER DEFAULT 0,
            is_flagged INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    # 2. User Activity Logs Table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS activity_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL,
            action TEXT NOT NULL,
            details TEXT,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # 3. NEW: Intrusion/Incident Logs Table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS incident_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            confidence TEXT NOT NULL,
            alarm_status TEXT NOT NULL,
            image_url TEXT NOT NULL
        )
    ''')
    
    # Default Admin account check
    cursor.execute("SELECT * FROM users WHERE username = 'admin'")
    if not cursor.fetchone():
        hashed_pw = generate_password_hash("admin123")
        cursor.execute(
            "INSERT INTO users (username, password_hash, role) VALUES (?, ?, ?)",
            ("admin", hashed_pw, "admin")
        )
        print("[DATABASE] Default Admin created (admin/admin123)")
        
    conn.commit()
    conn.close()

def log_activity(username, action, details=""):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("INSERT INTO activity_logs (username, action, details) VALUES (?, ?, ?)", (username, action, details))
    conn.commit()
    conn.close()

# NEW: Function to log AI camera incidents
def log_incident(timestamp, confidence, alarm_status, image_url):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO incident_logs (timestamp, confidence, alarm_status, image_url) VALUES (?, ?, ?, ?)",
        (timestamp, confidence, alarm_status, image_url)
    )
    conn.commit()
    conn.close()

if __name__ == '__main__':
    init_db()