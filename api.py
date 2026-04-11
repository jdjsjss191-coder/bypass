from flask import Flask, request, jsonify
import json, os, threading, time

app = Flask(__name__)
DATA_FILE = "data.json"
API_SECRET = os.environ.get("API_SECRET", "vyron_secret")

# In-memory active sessions: key -> {hwid, last_seen, kick_reason}
# A session is "active" if last_seen within 60 seconds
active_sessions: dict = {}
active_sessions_lock = threading.Lock()

# Pending kicks: key -> reason (set by bot, consumed by /heartbeat)
pending_kicks: dict = {}
pending_kicks_lock = threading.Lock()

SESSION_TIMEOUT = 60  # seconds before a session is considered inactive

@app.route("/")
def health():
    return "OK", 200

def load_data():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r") as f:
            return json.load(f)
    return {"keys": {}, "blacklist": {}, "temp_keys": {}, "key_hwid": {}}

def save_data(data):
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=2)

@app.route("/check", methods=["GET", "POST"])
def check_key():
    if request.method == "GET":
        key  = request.args.get("key", "").strip()
        hwid = request.args.get("hwid", "").strip()
    else:
        body = request.get_json(force=True) or {}
        key  = body.get("key", "").strip()
        hwid = body.get("hwid", "").strip()

    if not key or not hwid:
        return jsonify({"valid": False, "reason": "Missing key or hwid"}), 400

    data = load_data()

    # collect all valid keys
    all_keys = set()
    for keys in data.get("keys", {}).values():
        all_keys.update(keys)

    for uid, tkeys in data.get("temp_keys", {}).items():
        for t in tkeys:
            if t["expiry"] > int(time.time()):
                all_keys.add(t["key"])

    if key not in all_keys:
        return jsonify({"valid": False, "reason": "Invalid key"}), 200

    # check expiry on permanent keys
    key_expiry = data.get("key_expiry", {})
    if key in key_expiry and key_expiry[key] is not None:
        if int(time.time()) > key_expiry[key]:
            return jsonify({"valid": False, "reason": "Key expired"}), 200

    # check blacklist
    for uid, keys in data.get("keys", {}).items():
        if key in keys and uid in data.get("blacklist", {}):
            return jsonify({"valid": False, "reason": "Blacklisted: " + data["blacklist"][uid]}), 200

    # hwid check
    key_hwid = data.setdefault("key_hwid", {})
    if key not in key_hwid:
        key_hwid[key] = hwid
        executions = data.setdefault("key_executions", {})
        executions[key] = executions.get(key, 0) + 1
        save_data(data)
        # Register active session
        with active_sessions_lock:
            active_sessions[key] = {"hwid": hwid, "last_seen": int(time.time())}
        return jsonify({"valid": True, "reason": "Key bound to HWID"}), 200
    elif key_hwid[key] != hwid:
        return jsonify({"valid": False, "reason": "HWID mismatch"}), 200
    else:
        executions = data.setdefault("key_executions", {})
        executions[key] = executions.get(key, 0) + 1
        data.setdefault("key_last_exec", {})[key] = int(time.time())
        save_data(data)
        # Register active session
        with active_sessions_lock:
            active_sessions[key] = {"hwid": hwid, "last_seen": int(time.time())}
        return jsonify({"valid": True, "reason": "OK"}), 200


@app.route("/heartbeat", methods=["GET", "POST"])
def heartbeat():
    """Called by the script every ~15s to keep session alive. Returns kick instruction if pending."""
    if request.method == "GET":
        key  = request.args.get("key", "").strip()
        hwid = request.args.get("hwid", "").strip()
    else:
        body = request.get_json(force=True) or {}
        key  = body.get("key", "").strip()
        hwid = body.get("hwid", "").strip()

    if not key or not hwid:
        return jsonify({"kick": False}), 400

    # Update last_seen
    with active_sessions_lock:
        if key in active_sessions and active_sessions[key]["hwid"] == hwid:
            active_sessions[key]["last_seen"] = int(time.time())
        else:
            active_sessions[key] = {"hwid": hwid, "last_seen": int(time.time())}

    # Check for pending kick
    with pending_kicks_lock:
        if key in pending_kicks:
            reason = pending_kicks.pop(key)
            # Remove from active sessions
            with active_sessions_lock:
                active_sessions.pop(key, None)
            return jsonify({"kick": True, "reason": reason}), 200

    return jsonify({"kick": False}), 200


@app.route("/sessions", methods=["GET"])
def get_sessions():
    """Returns all currently active sessions. Used by the bot."""
    now = int(time.time())
    data = load_data()

    result = []
    with active_sessions_lock:
        for key, session in list(active_sessions.items()):
            if now - session["last_seen"] > SESSION_TIMEOUT:
                continue  # skip stale sessions

            # Find owner
            owner_uid = None
            for uid, keys in data.get("keys", {}).items():
                if key in keys:
                    owner_uid = uid
                    break

            expiry = data.get("key_expiry", {}).get(key)
            if expiry is None:
                expiry_str = "Lifetime"
            elif now > expiry:
                expiry_str = "Expired"
            else:
                secs_left = expiry - now
                if secs_left < 3600:
                    expiry_str = f"{secs_left // 60}m"
                elif secs_left < 86400:
                    expiry_str = f"{secs_left // 3600}h"
                else:
                    expiry_str = f"{secs_left // 86400}d"

            result.append({
                "key": key,
                "hwid": session["hwid"],
                "last_seen": session["last_seen"],
                "owner_uid": owner_uid,
                "expiry": expiry_str,
            })

    return jsonify(result), 200


@app.route("/kick", methods=["POST"])
def kick_session():
    """Queue a kick for a key. Called by the bot."""
    body = request.get_json(force=True) or {}
    key    = body.get("key", "").strip()
    reason = body.get("reason", "Kicked by staff").strip()
    secret = body.get("secret", "").strip()

    if secret != API_SECRET:
        return jsonify({"success": False, "reason": "Unauthorized"}), 403

    if not key:
        return jsonify({"success": False, "reason": "Missing key"}), 400

    with pending_kicks_lock:
        pending_kicks[key] = reason

    return jsonify({"success": True}), 200


def run_api():
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, use_reloader=False)

def start_api_thread():
    t = threading.Thread(target=run_api, daemon=True)
    t.start()
