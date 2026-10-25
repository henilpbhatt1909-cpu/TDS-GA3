import sqlite3
import json
import os
from datetime import datetime, timezone

DB_FILE = os.path.join("instance", "incident_db.sqlite")

def init_db():
    os.makedirs("instance", exist_ok=True)
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS incident_telemetry (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            incident_id TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            action_taken TEXT NOT NULL,
            scaling_limit_reached BOOLEAN NOT NULL,
            telemetry_data TEXT NOT NULL
        )
    ''')
    conn.commit()
    conn.close()

def save_telemetry(incident_id: str, action_taken: str, scaling_limit_reached: bool, telemetry: dict):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    timestamp = datetime.now(timezone.utc).isoformat()
    cursor.execute(
        '''INSERT INTO incident_telemetry 
           (incident_id, timestamp, action_taken, scaling_limit_reached, telemetry_data) 
           VALUES (?, ?, ?, ?, ?)''',
        (incident_id, timestamp, action_taken, scaling_limit_reached, json.dumps(telemetry))
    )
    conn.commit()
    conn.close()

def get_telemetry_history():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('SELECT incident_id, timestamp, action_taken, scaling_limit_reached, telemetry_data FROM incident_telemetry ORDER BY id DESC')
    rows = cursor.fetchall()
    conn.close()
    
    return [
        {
            "incident_id": r[0],
            "timestamp": r[1],
            "action_taken": r[2],
            "scaling_limit_reached": bool(r[3]),
            "telemetry": json.loads(r[4])
        }
        for r in rows
    ]