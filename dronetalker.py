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

# --------------------
# LOGGING (THIS IS THE KEY BIT)
# --------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    force=True
)
log = logging.getLogger("dronetalker")

# --------------------
# APP
# --------------------
app = Flask(__name__)
CORS(app)

# --------------------
# DATABASE
# --------------------
def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS latest_target (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            lat REAL,
            lon REAL,
            accuracy REAL,
            created_at INTEGER,
            request_id TEXT
        )
    """)
    cur.execute("""
        INSERT OR IGNORE INTO latest_target
        (id, lat, lon, accuracy, created_at, request_id)
        VALUES (1, NULL, NULL, NULL, NULL, NULL)
    """)
    conn.commit()
    conn.close()
    log.info("Database initialised")

def set_latest(lat, lon, accuracy, request_id):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        UPDATE latest_target
        SET lat = ?, lon = ?, accuracy = ?, created_at = ?, request_id = ?
        WHERE id = 1
    """, (lat, lon, accuracy, int(time.time()), request_id))
    conn.commit()
    conn.close()

def get_latest():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        SELECT lat, lon, accuracy, created_at, request_id
        FROM latest_target WHERE id = 1
    """)
    row = cur.fetchone()
    conn.close()
    if not row:
        return None

    lat, lon, accuracy, created_at, request_id = row
    return {
        "lat": lat,
        "lon": lon,
        "accuracy": accuracy,
        "created_at": created_at,
        "request_id": request_id
    }

def valid_lat_lon(lat, lon):
    return lat is not None and lon is not None and -90 <= lat <= 90 and -180 <= lon <= 180

# --------------------
# ROUTES
# --------------------
@app.route("/", methods=["GET"])
def root():
    return jsonify({"ok": True, "service": "dronetalker"})

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"ok": True, "time": int(time.time())})

@app.route("/go", methods=["POST"])
def go():
    token = request.headers.get("X-APP-TOKEN", "")
    if token != APP_TOKEN:
        log.warning("Unauthorized POST /go")
        return jsonify({"ok": False, "error": "unauthorized"}), 401

    data = request.get_json(silent=True) or {}

    try:
        lat = float(data.get("lat"))
        lon = float(data.get("lon"))
        accuracy = float(data.get("accuracy"))
        request_id = str(data.get("request_id"))
    except (TypeError, ValueError):
        log.warning("Invalid payload received: %s", data)
        return jsonify({"ok": False, "error": "invalid data"}), 400

    if not valid_lat_lon(lat, lon):
        log.warning("Lat/Lon out of range: lat=%s lon=%s", lat, lon)
        return jsonify({"ok": False, "error": "lat/lon out of range"}), 400

    if accuracy > MAX_ACCURACY_M:
        log.warning("GPS accuracy too poor: %.1fm", accuracy)
        return jsonify({"ok": False, "error": f"gps too inaccurate ({accuracy:.1f}m)"}), 400

    # 🔥 THIS WILL 100% SHOW IN RENDER LOGS
    log.info(
        "NEW TARGET | lat=%.6f lon=%.6f acc=%.1fm request_id=%s",
        lat, lon, accuracy, request_id
    )

    set_latest(lat, lon, accuracy, request_id)

    return jsonify({
        "ok": True,
        "stored": {
            "lat": lat,
            "lon": lon,
            "accuracy": accuracy,
            "request_id": request_id,
            "created_at": int(time.time())
        }
    })

@app.route("/latest", methods=["GET"])
def latest():
    token = request.headers.get("X-APP-TOKEN", "")
    if token != APP_TOKEN:
        return jsonify({"ok": False, "error": "unauthorized"}), 401

    latest = get_latest()
    if not latest or latest["lat"] is None:
        return jsonify({"ok": False, "error": "no target"}), 404

    age = int(time.time()) - int(latest["created_at"])
    if age > MAX_AGE_SECONDS:
        return jsonify({"ok": False, "error": "target stale"}), 410

    log.info(
        "TARGET READ | lat=%.6f lon=%.6f acc=%.1fm age=%ss",
        latest["lat"], latest["lon"], latest["accuracy"], age
    )

    return jsonify({
        "ok": True,
        "target": latest,
        "age_seconds": age
    })

# --------------------
# STARTUP
# --------------------
if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=5000)
