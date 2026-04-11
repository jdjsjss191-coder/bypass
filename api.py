from flask import Flask, request, jsonify
import json, os, threading

app = Flask(__name__)
DATA_FILE = "data.json"
API_SECRET = os.environ.get("API_SECRET", "vyron_secret")

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

    import time
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
        # first use — bind hwid
        key_hwid[key] = hwid
        # track execution
        executions = data.setdefault("key_executions", {})
        executions[key] = executions.get(key, 0) + 1
        save_data(data)
        return jsonify({"valid": True, "reason": "Key bound to HWID"}), 200
    elif key_hwid[key] != hwid:
        return jsonify({"valid": False, "reason": "HWID mismatch"}), 200
    else:
        # valid execution — track it
        import time as _time
        executions = data.setdefault("key_executions", {})
        executions[key] = executions.get(key, 0) + 1
        data.setdefault("key_last_exec", {})[key] = int(_time.time())
        save_data(data)
        return jsonify({"valid": True, "reason": "OK"}), 200

def run_api():
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, use_reloader=False)

def start_api_thread():
    t = threading.Thread(target=run_api, daemon=True)
    t.start()
