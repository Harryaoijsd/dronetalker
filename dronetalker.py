from flask import Flask, request, jsonify
from flask_cors import CORS
import sqlite3
import time
import os
import logging

# --------------------
# CONFIG
# --------------------
APP_TOKEN = os.environ.get("APP_TOKEN", "CHANGE_ME")
DB_PATH = os.environ.get("DB_PATH", "targets.db")
MAX_AGE_SECONDS = int(os.environ.get("MAX_AGE_SECONDS", "60"))
MAX_ACCURACY_M = float(os.environ.get("MAX_ACCURACY_M", "50"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    force=True
)
log = logging.getLogger("dronetalker")

app = Flask(__name__)
CORS(app)

# --------------------
# DATABASE
# --------------------
def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    
    # 1. Target Table (Existing)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS latest_target (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            lat REAL, lon REAL, accuracy REAL,
            created_at INTEGER, request_id TEXT
        )
    """)
    cur.execute("""
        INSERT OR IGNORE INTO latest_target
        (id, lat, lon, accuracy, created_at, request_id)
        VALUES (1, NULL, NULL, NULL, NULL, NULL)
    """)

    # 2. Command Table (New - for RTH/Hover)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS command_buffer (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            command TEXT,
            created_at INTEGER
        )
    """)
    cur.execute("""
        INSERT OR IGNORE INTO command_buffer (id, command, created_at)
        VALUES (1, "NONE", 0)
    """)

    # 3. Logs Table (New - for Drone Status)
    # We only keep the last 50 logs to keep it light
    cur.execute("""
        CREATE TABLE IF NOT EXISTS drone_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            message TEXT,
            created_at INTEGER
        )
    """)

    conn.commit()
    conn.close()
    log.info("Database initialised with Target, Command, and Log tables")

# --- DB HELPERS ---

def add_log_entry(message):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("INSERT INTO drone_logs (message, created_at) VALUES (?, ?)", (message, int(time.time())))
    # Cleanup old logs (keep last 50)
    cur.execute("DELETE FROM drone_logs WHERE id NOT IN (SELECT id FROM drone_logs ORDER BY id DESC LIMIT 50)")
    conn.commit()
    conn.close()

def get_recent_logs(limit=10):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT message, created_at FROM drone_logs ORDER BY id DESC LIMIT ?", (limit,))
    rows = cur.fetchall()
    conn.close()
    return [{"message": r[0], "time": r[1]} for r in rows]

def set_command(cmd):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("UPDATE command_buffer SET command = ?, created_at = ? WHERE id = 1", (cmd, int(time.time())))
    conn.commit()
    conn.close()

def get_current_command():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT command, created_at FROM command_buffer WHERE id = 1")
    row = cur.fetchone()
    conn.close()
    if row and row[0] != "NONE":
        # Check if command is stale (e.g., older than 10 seconds)
        if (int(time.time()) - row[1]) < 10:
            return row[0]
    return None

def set_latest_target(lat, lon, accuracy, request_id):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        UPDATE latest_target
        SET lat = ?, lon = ?, accuracy = ?, created_at = ?, request_id = ?
        WHERE id = 1
    """, (lat, lon, accuracy, int(time.time()), request_id))
    conn.commit()
    conn.close()

def get_latest_target():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT lat, lon, accuracy, created_at, request_id FROM latest_target WHERE id = 1")
    row = cur.fetchone()
    conn.close()
    if not row or row[0] is None: return None
    return {"lat": row[0], "lon": row[1], "accuracy": row[2], "created_at": row[3], "request_id": row[4]}

# --------------------
# ROUTES
# --------------------
@app.route("/", methods=["GET"])
def root():
    return jsonify({"ok": True, "service": "dronetalker"})

# --- 1. TARGET ROUTES (Existing) ---

@app.route("/go", methods=["POST"])
def go():
    token = request.headers.get("X-APP-TOKEN", "")
    if token != APP_TOKEN: return jsonify({"ok": False, "error": "unauthorized"}), 401
    
    data = request.get_json(silent=True) or {}
    try:
        lat, lon = float(data.get("lat")), float(data.get("lon"))
        acc = float(data.get("accuracy"))
        rid = str(data.get("request_id"))
    except: return jsonify({"ok": False, "error": "invalid data"}), 400

    if acc > MAX_ACCURACY_M: return jsonify({"ok": False, "error": f"gps poor ({acc:.1f}m)"}), 400

    log.info(f"TARGET | lat={lat} lon={lon}")
    set_latest_target(lat, lon, acc, rid)
    # Log this action to the drone log as well
    add_log_entry(f"New Target Received: {lat:.5f}, {lon:.5f}")
    
    return jsonify({"ok": True})

@app.route("/latest", methods=["GET"])
def latest():
    token = request.headers.get("X-APP-TOKEN", "")
    if token != APP_TOKEN: return jsonify({"ok": False, "error": "unauthorized"}), 401
    
    tgt = get_latest_target()
    if not tgt: return jsonify({"ok": False, "error": "no target"}), 404
    
    age = int(time.time()) - int(tgt["created_at"])
    if age > MAX_AGE_SECONDS: return jsonify({"ok": False, "error": "target stale"}), 410
    
    return jsonify({"ok": True, "target": tgt, "age_seconds": age})

# --- 2. COMMAND ROUTES (New: Hover / RTH) ---

@app.route("/drone/cmd", methods=["POST"])
def post_command():
    # Web App calls this
    token = request.headers.get("X-APP-TOKEN", "")
    if token != APP_TOKEN: return jsonify({"ok": False, "error": "unauthorized"}), 401
    
    data = request.get_json(silent=True) or {}
    cmd = data.get("command") # "HOVER" or "RTH" or "LAND"
    
    if cmd not in ["HOVER", "RTH", "LAND"]:
        return jsonify({"ok": False, "error": "invalid command"}), 400

    set_command(cmd)
    add_log_entry(f"Command Sent: {cmd}")
    log.info(f"COMMAND | {cmd}")
    return jsonify({"ok": True, "command": cmd})

@app.route("/drone/cmd", methods=["GET"])
def get_command():
    # Android Drone calls this to check for instructions
    token = request.headers.get("X-APP-TOKEN", "")
    if token != APP_TOKEN: return jsonify({"ok": False, "error": "unauthorized"}), 401
    
    cmd = get_current_command()
    if not cmd:
        return jsonify({"ok": True, "command": None})
    
    return jsonify({"ok": True, "command": cmd})

# --- 3. STATUS LOG ROUTES (New: Drone Updates) ---

@app.route("/drone/status", methods=["POST"])
def post_status():
    # Android Drone calls this to report status
    token = request.headers.get("X-APP-TOKEN", "")
    if token != APP_TOKEN: return jsonify({"ok": False, "error": "unauthorized"}), 401
    
    data = request.get_json(silent=True) or {}
    msg = data.get("message")
    
    if msg:
        add_log_entry(msg)
        log.info(f"DRONE STATUS | {msg}")
    
    return jsonify({"ok": True})

@app.route("/drone/status", methods=["GET"])
def get_status():
    # Web App calls this to show the feed
    # No auth needed strictly for read-only logs if you prefer, 
    # but let's keep it safe.
    logs = get_recent_logs(limit=20)
    return jsonify({"ok": True, "logs": logs})

if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=5000)