from flask import Flask, request, jsonify, redirect, Response, render_template_string, session
import json, os, threading, time, secrets

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET", secrets.token_hex(32))

# Allow the website to call the API from a different domain
@app.after_request
def add_cors(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, X-Admin-Password"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return response

@app.route("/admin/source", methods=["OPTIONS"])
@app.route("/source", methods=["OPTIONS"])
def options_handler():
    return "", 204

DATA_FILE = "data.json"
API_SECRET = os.environ.get("API_SECRET", "vyron_secret")
DASHBOARD_PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "vyron_admin")
SOURCE_FILE = os.path.join(os.path.dirname(__file__), "mooze.txt")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "")


def _fire_webhook(payload: dict):
    """Send a webhook notification in a background thread. Never raises."""
    if not WEBHOOK_URL:
        return
    try:
        import requests as _requests
        _requests.post(WEBHOOK_URL, json=payload, timeout=5)
    except Exception:
        pass

# In-memory active sessions: key -> {hwid, last_seen, kick_reason}
# A session is "active" if last_seen within 60 seconds
active_sessions: dict = {}
active_sessions_lock = threading.Lock()

# Pending kicks: key -> reason (set by bot, consumed by /heartbeat)
pending_kicks: dict = {}
pending_kicks_lock = threading.Lock()

# Pending notifications: key -> message (set by bot, consumed by /heartbeat)
pending_notifs: dict = {}
pending_notifs_lock = threading.Lock()

# Pending music commands: key -> {action, sound_id, loop} (set by bot, consumed by /heartbeat)
# action = "play" | "stop"
pending_music: dict = {}
pending_music_lock = threading.Lock()

# Pending teleport commands: key -> {place_id, job_id} (set by bot, consumed by /heartbeat)
pending_teleport: dict = {}
pending_teleport_lock = threading.Lock()

# Frozen keys: set of keys currently frozen (persistent until unfreeze)
frozen_keys: set = set()
frozen_keys_lock = threading.Lock()

SESSION_TIMEOUT = 60  # seconds before a session is considered inactive

DISCORD_INVITE = os.environ.get("DISCORD_INVITE", "https://discord.gg/RzCyAwnMqa")

# Browser user-agent keywords — if any match, redirect to Discord
BROWSER_AGENTS = ("mozilla", "chrome", "safari", "firefox", "edge", "opera", "webkit")

def _is_browser(ua: str) -> bool:
    ua_lower = ua.lower()
    return any(kw in ua_lower for kw in BROWSER_AGENTS)

@app.route("/source-editor")
def source_editor():
    html_path = os.path.join(os.path.dirname(__file__), "..", "vyron-site", "source.html")
    if not os.path.exists(html_path):
        return "Not found", 404
    with open(html_path, "r", encoding="utf-8") as f:
        return f.read(), 200, {"Content-Type": "text/html"}


@app.route("/")
def health():
    return "OK", 200

SOURCE_TOKEN = os.environ.get("SOURCE_TOKEN", "")

@app.route("/source")
def serve_source():
    """
    Serves mooze.txt to Roblox executors (HttpGet).
    Redirects browsers to the Discord server instead.
    Requires a valid SOURCE_TOKEN header or query param.
    """
    ua = request.headers.get("User-Agent", "")
    if _is_browser(ua):
        return redirect(DISCORD_INVITE, code=302)

    # Token check — executors must pass ?token=SOURCE_TOKEN
    token = request.args.get("token", "").strip()
    if SOURCE_TOKEN and token != SOURCE_TOKEN:
        return redirect(DISCORD_INVITE, code=302)

    source_path = os.path.join(os.path.dirname(__file__), "mooze.txt")
    if not os.path.exists(source_path):
        return "-- source not found", 404

    with open(source_path, "r", encoding="utf-8") as f:
        source = f.read()

    return Response(source, mimetype="text/plain")

def load_data():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r") as f:
            return json.load(f)
    return {
        "keys": {},
        "keys_internal": {},
        "blacklist": {},
        "temp_keys": {},
        "temp_keys_internal": {},
        "key_hwid": {},
    }

def save_data(data):
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=2)

@app.route("/check", methods=["GET", "POST"])
def check_key():
    edition = ""
    if request.method == "GET":
        key = request.args.get("key", "").strip()
        hwid = request.args.get("hwid", "").strip()
        edition = (request.args.get("edition") or request.args.get("ed") or "").strip().lower()
        roblox_id = request.args.get("roblox_id", "").strip()
        roblox_name = request.args.get("roblox_name", "").strip()
    else:
        body = request.get_json(force=True) or {}
        key = body.get("key", "").strip()
        hwid = body.get("hwid", "").strip()
        edition = str(body.get("edition") or body.get("ed") or "").strip().lower()
        roblox_id = str(body.get("roblox_id", "") or "").strip()
        roblox_name = str(body.get("roblox_name", "") or "").strip()

    if not key or not hwid:
        return jsonify({"valid": False, "reason": "Missing key or hwid"}), 400

    data = load_data()

    if not edition:
        edition = "ext"
    if edition in ("external", "ext", "e"):
        edition = "ext"
    elif edition in ("internal", "int", "i"):
        edition = "int"
    else:
        edition = "ext"

    all_keys = set()
    if edition == "int":
        for keys in data.get("keys_internal", {}).values():
            all_keys.update(keys)
        for uid, tkeys in data.get("temp_keys_internal", {}).items():
            for t in tkeys:
                if t.get("expiry", 0) > int(time.time()):
                    all_keys.add(t["key"])
    else:
        for keys in data.get("keys", {}).values():
            all_keys.update(keys)
        for uid, tkeys in data.get("temp_keys", {}).items():
            for t in tkeys:
                if t.get("expiry", 0) > int(time.time()):
                    all_keys.add(t["key"])

    if key not in all_keys:
        return jsonify({"valid": False, "reason": "Invalid key"}), 200

    # check expiry on permanent keys
    key_expiry = data.get("key_expiry", {})
    if key in key_expiry and key_expiry[key] is not None:
        if int(time.time()) > key_expiry[key]:
            return jsonify({"valid": False, "reason": "Key expired"}), 200

    # check blacklist (either pool)
    for pool in (data.get("keys", {}), data.get("keys_internal", {})):
        for uid, keys in pool.items():
            if key in keys and uid in data.get("blacklist", {}):
                return jsonify({"valid": False, "reason": "Blacklisted: " + data["blacklist"][uid]}), 200

    # determine key type for analytics
    if key.startswith("VyronInt-"):
        key_type = "internal"
    elif key.startswith("VyronExt-"):
        key_type = "external"
    else:
        key_type = "script"

    # hwid check
    key_hwid = data.setdefault("key_hwid", {})
    if key not in key_hwid:
        key_hwid[key] = hwid
        executions = data.setdefault("key_executions", {})
        executions[key] = executions.get(key, 0) + 1
        data.setdefault("key_last_exec", {})[key] = int(time.time())
        if roblox_id or roblox_name:
            data.setdefault("key_roblox_info", {})[key] = {
                "id": roblox_id,
                "name": roblox_name,
            }
        save_data(data)
        # Register active session
        with active_sessions_lock:
            active_sessions[key] = {"hwid": hwid, "last_seen": int(time.time())}
        # Webhook notification
        threading.Thread(target=_fire_webhook, args=({
            "embeds": [{
                "title": "✅ Key Executed (New HWID Bound)",
                "color": 0x00CC66,
                "fields": [
                    {"name": "Key", "value": f"`{key}`", "inline": False},
                    {"name": "HWID", "value": hwid, "inline": True},
                    {"name": "PlaceId", "value": "N/A", "inline": True},
                ],
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }]
        },), daemon=True).start()
        return jsonify({"valid": True, "reason": "Key bound to HWID"}), 200
    elif key_hwid[key] != hwid:
        return jsonify({"valid": False, "reason": "HWID mismatch"}), 200
    else:
        executions = data.setdefault("key_executions", {})
        executions[key] = executions.get(key, 0) + 1
        data.setdefault("key_last_exec", {})[key] = int(time.time())
        if roblox_id or roblox_name:
            data.setdefault("key_roblox_info", {})[key] = {
                "id": roblox_id,
                "name": roblox_name,
            }
        save_data(data)
        # Register active session
        with active_sessions_lock:
            active_sessions[key] = {"hwid": hwid, "last_seen": int(time.time())}
        # Webhook notification
        threading.Thread(target=_fire_webhook, args=({
            "embeds": [{
                "title": "✅ Key Executed",
                "color": 0x5080FF,
                "fields": [
                    {"name": "Key", "value": f"`{key}`", "inline": False},
                    {"name": "HWID", "value": hwid, "inline": True},
                    {"name": "PlaceId", "value": "N/A", "inline": True},
                ],
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }]
        },), daemon=True).start()
        return jsonify({"valid": True, "reason": "OK"}), 200


@app.route("/heartbeat", methods=["GET", "POST"])
def heartbeat():
    """Called by the script every ~5s to keep session alive. Returns kick instruction if pending."""
    if request.method == "GET":
        key      = request.args.get("key", "").strip()
        hwid     = request.args.get("hwid", "").strip()
        place_id = request.args.get("place_id", "").strip()
        job_id   = request.args.get("job_id", "").strip()
    else:
        body     = request.get_json(force=True) or {}
        key      = body.get("key", "").strip()
        hwid     = body.get("hwid", "").strip()
        place_id = str(body.get("place_id", "")).strip()
        job_id   = str(body.get("job_id", "")).strip()

    if not key or not hwid:
        return jsonify({"kick": False}), 400

    # Update last_seen + location
    with active_sessions_lock:
        if key in active_sessions and active_sessions[key]["hwid"] == hwid:
            active_sessions[key]["last_seen"] = int(time.time())
            if place_id:
                active_sessions[key]["place_id"] = place_id
            if job_id:
                active_sessions[key]["job_id"] = job_id
        else:
            active_sessions[key] = {
                "hwid": hwid,
                "last_seen": int(time.time()),
                "place_id": place_id,
                "job_id": job_id,
            }

    # Check for pending kick
    with pending_kicks_lock:
        if key in pending_kicks:
            reason = pending_kicks.pop(key)
            # Remove from active sessions
            with active_sessions_lock:
                active_sessions.pop(key, None)
            return jsonify({"kick": True, "reason": reason}), 200

    # Check for pending notification
    with pending_notifs_lock:
        if key in pending_notifs:
            notif = pending_notifs.pop(key)
            message  = notif["message"] if isinstance(notif, dict) else notif
            sound_id = notif.get("sound_id", "") if isinstance(notif, dict) else ""
            discord_username = notif.get("discord_username", "") if isinstance(notif, dict) else ""
            return jsonify({
                "kick": False, 
                "notify": True, 
                "message": message, 
                "sound_id": sound_id,
                "discord_username": discord_username
            }), 200

    # Check for pending music command
    with pending_music_lock:
        if key in pending_music:
            cmd = pending_music.pop(key)
            return jsonify({
                "kick": False, "notify": False,
                "music": True,
                "music_action": cmd.get("action", "stop"),
                "music_sound_id": cmd.get("sound_id", ""),
                "music_loop": cmd.get("loop", False),
            }), 200

    # Check for pending teleport
    with pending_teleport_lock:
        if key in pending_teleport:
            tp = pending_teleport.pop(key)
            return jsonify({
                "kick": False, "notify": False,
                "teleport": True,
                "teleport_place_id": tp.get("place_id", ""),
                "teleport_job_id":   tp.get("job_id", ""),
            }), 200

    # Check freeze state — persistent, does NOT pop (stays until unfreeze)
    with frozen_keys_lock:
        is_frozen = key in frozen_keys

    if is_frozen:
        return jsonify({"kick": False, "notify": False, "freeze": True}), 200

    return jsonify({"kick": False, "notify": False}), 200


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
            if owner_uid is None:
                for uid, keys in data.get("keys_internal", {}).items():
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

            # Get roblox_info for this key
            roblox_info = data.get("key_roblox_info", {}).get(key, {})

            result.append({
                "key": key,
                "hwid": session["hwid"],
                "last_seen": session["last_seen"],
                "owner_uid": owner_uid,
                "expiry": expiry_str,
                "place_id": session.get("place_id", ""),
                "job_id": session.get("job_id", ""),
                "roblox_info": roblox_info,
            })

    return jsonify(result), 200


@app.route("/tamper", methods=["POST"])
def report_tamper():
    """Called by the script when tamper is detected. Notifies the Discord bot."""
    body = request.get_json(force=True) or {}
    key          = body.get("key", "unknown").strip()
    hwid         = body.get("hwid", "unknown").strip()
    roblox_user  = body.get("roblox_user", "unknown").strip()
    tamper_type  = body.get("tamper_type", "unknown").strip()

    # Store tamper report so the bot can pick it up
    data = load_data()
    data.setdefault("tamper_reports", []).append({
        "key":         key,
        "hwid":        hwid,
        "roblox_user": roblox_user,
        "tamper_type": tamper_type,
        "at":          int(time.time()),
    })
    save_data(data)

    # Find discord owner of this key
    owner_uid = None
    for uid, keys in data.get("keys", {}).items():
        if key in keys:
            owner_uid = uid
            break
    if owner_uid is None:
        for uid, keys in data.get("keys_internal", {}).items():
            if key in keys:
                owner_uid = uid
                break

    # Queue a notification back to the script (optional kick)
    with pending_kicks_lock:
        pending_kicks[key] = "Tamper detected. You have been removed."

    return jsonify({
        "success": True,
        "owner_uid": owner_uid,
        "roblox_user": roblox_user,
        "tamper_type": tamper_type,
    }), 200


@app.route("/tamper/pending", methods=["GET"])
def get_pending_tampers():
    """Called by the bot to fetch unprocessed tamper reports."""
    secret = request.headers.get("X-Admin-Password", "")
    if secret != DASHBOARD_PASSWORD:
        return jsonify({"error": "Unauthorized"}), 403

    data = load_data()
    reports = data.get("tamper_reports", [])
    # Clear after reading
    data["tamper_reports"] = []
    save_data(data)
    return jsonify(reports), 200



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


@app.route("/notify", methods=["POST"])
def notify_session():
    """Queue a notification for a key. Called by the bot."""
    body = request.get_json(force=True) or {}
    key     = body.get("key", "").strip()
    message = body.get("message", "").strip()
    secret  = body.get("secret", "").strip()
    sound_id = body.get("sound_id", "").strip()
    discord_username = body.get("discord_username", "").strip()

    if secret != API_SECRET:
        return jsonify({"success": False, "reason": "Unauthorized"}), 403

    if not key or not message:
        return jsonify({"success": False, "reason": "Missing key or message"}), 400

    with pending_notifs_lock:
        pending_notifs[key] = {
            "message": message, 
            "sound_id": sound_id,
            "discord_username": discord_username
        }

    return jsonify({"success": True}), 200


@app.route("/music", methods=["POST"])
def music_session():
    """Queue a music play/stop command for a key. Called by the bot."""
    body = request.get_json(force=True) or {}
    key      = body.get("key", "").strip()
    action   = body.get("action", "play").strip()   # "play" or "stop"
    sound_id = body.get("sound_id", "").strip()
    loop     = bool(body.get("loop", False))
    secret   = body.get("secret", "").strip()

    if secret != API_SECRET:
        return jsonify({"success": False, "reason": "Unauthorized"}), 403

    if not key:
        return jsonify({"success": False, "reason": "Missing key"}), 400

    if action == "play" and not sound_id:
        return jsonify({"success": False, "reason": "Missing sound_id for play action"}), 400

    with pending_music_lock:
        pending_music[key] = {"action": action, "sound_id": sound_id, "loop": loop}

    return jsonify({"success": True}), 200


@app.route("/teleport", methods=["POST"])
def teleport_session():
    """Queue a teleport command for a key. Called by the bot."""
    body     = request.get_json(force=True) or {}
    key      = body.get("key", "").strip()
    place_id = str(body.get("place_id", "")).strip()
    job_id   = str(body.get("job_id", "")).strip()  # optional — empty = join any server
    secret   = body.get("secret", "").strip()

    if secret != API_SECRET:
        return jsonify({"success": False, "reason": "Unauthorized"}), 403

    if not key or not place_id:
        return jsonify({"success": False, "reason": "Missing key or place_id"}), 400

    with pending_teleport_lock:
        pending_teleport[key] = {"place_id": place_id, "job_id": job_id}

    return jsonify({"success": True}), 200


@app.route("/freeze", methods=["POST"])
def freeze_session():
    """Freeze a key — sets walkspeed to 1 until unfrozen. Called by the bot."""
    body   = request.get_json(force=True) or {}
    key    = body.get("key", "").strip()
    secret = body.get("secret", "").strip()

    if secret != API_SECRET:
        return jsonify({"success": False, "reason": "Unauthorized"}), 403
    if not key:
        return jsonify({"success": False, "reason": "Missing key"}), 400

    with frozen_keys_lock:
        frozen_keys.add(key)

    return jsonify({"success": True}), 200


@app.route("/unfreeze", methods=["POST"])
def unfreeze_session():
    """Unfreeze a key — restores normal walkspeed. Called by the bot."""
    body   = request.get_json(force=True) or {}
    key    = body.get("key", "").strip()
    secret = body.get("secret", "").strip()

    if secret != API_SECRET:
        return jsonify({"success": False, "reason": "Unauthorized"}), 403
    if not key:
        return jsonify({"success": False, "reason": "Missing key"}), 400

    with frozen_keys_lock:
        frozen_keys.discard(key)

    return jsonify({"success": True}), 200


@app.route("/location/<key>", methods=["GET"])
def get_location(key: str):
    """Returns the current place_id and job_id for a key. Used by the bot for /joinuserkey."""
    secret = request.headers.get("X-Admin-Password", "")
    if secret != DASHBOARD_PASSWORD:
        return jsonify({"error": "Unauthorized"}), 403

    now = int(time.time())
    with active_sessions_lock:
        session = active_sessions.get(key)
        if not session or now - session.get("last_seen", 0) > SESSION_TIMEOUT:
            return jsonify({"online": False, "reason": "Key not in an active session"}), 200
        return jsonify({
            "online": True,
            "place_id": session.get("place_id", ""),
            "job_id":   session.get("job_id", ""),
            "last_seen": session.get("last_seen", 0),
        }), 200


# ─────────────────────────────────────────────
#  ADMIN SOURCE API (used by the external website)
# ─────────────────────────────────────────────

def _check_admin_password(req) -> bool:
    pw = req.headers.get("X-Admin-Password", "")
    return pw == DASHBOARD_PASSWORD


@app.route("/admin/source", methods=["GET"])
def admin_get_source():
    if not _check_admin_password(request):
        return jsonify({"error": "Unauthorized"}), 403

    source = ""
    saved_at = None
    if os.path.exists(SOURCE_FILE):
        with open(SOURCE_FILE, "r", encoding="utf-8") as f:
            source = f.read()
        mtime = os.path.getmtime(SOURCE_FILE)
        saved_at = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(mtime))

    return jsonify({"source": source, "saved_at": saved_at})


@app.route("/admin/source", methods=["POST"])
def admin_save_source():
    if not _check_admin_password(request):
        return jsonify({"success": False, "error": "Unauthorized"}), 403

    body = request.get_json(force=True) or {}
    source = body.get("source", "")

    try:
        with open(SOURCE_FILE, "w", encoding="utf-8") as f:
            f.write(source)
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


# ─────────────────────────────────────────────
#  DASHBOARD
# ─────────────────────────────────────────────

DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<meta name="robots" content="noindex,nofollow"/>
<title>Vyron Source Manager</title>
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;600&family=Instrument+Sans:wght@400;500;600;700&display=swap" rel="stylesheet"/>
<style>
:root{--bg:#030306;--card:rgba(14,14,22,.9);--b:rgba(255,255,255,.07);--bs:rgba(255,255,255,.13);--t:#f0f0f8;--m:#8080a0;--a:#6b8aff;--g:#3dd4a0;--r:#ff5566;--rad:12px}
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
html,body{height:100%}
body{font-family:"Instrument Sans",system-ui,sans-serif;background:var(--bg);color:var(--t);-webkit-font-smoothing:antialiased}
.bg{position:fixed;inset:0;z-index:0;pointer-events:none;background:radial-gradient(ellipse 90% 70% at 50% -20%,rgba(60,80,200,.3) 0%,transparent 55%),radial-gradient(ellipse 60% 50% at 90% 80%,rgba(40,160,140,.1) 0%,transparent 50%)}
.bg-grid{position:fixed;inset:0;z-index:0;pointer-events:none;background-image:linear-gradient(rgba(255,255,255,.022) 1px,transparent 1px),linear-gradient(90deg,rgba(255,255,255,.022) 1px,transparent 1px);background-size:52px 52px;mask-image:radial-gradient(ellipse 80% 60% at 50% 30%,black 20%,transparent 70%)}
#login{position:relative;z-index:10;min-height:100vh;display:flex;align-items:center;justify-content:center;padding:1.5rem}
.lcard{width:100%;max-width:400px;background:var(--card);border:1px solid var(--bs);border-radius:20px;padding:2.5rem 2rem;backdrop-filter:blur(24px);box-shadow:0 40px 100px rgba(0,0,0,.55)}
.llogo{text-align:center;margin-bottom:2rem}
.llogo .nm{font-size:1.6rem;font-weight:700;letter-spacing:-.04em}
.llogo .nm span{color:var(--m);font-weight:500}
.llogo .sub{font-family:"JetBrains Mono",monospace;font-size:.6rem;letter-spacing:.2em;text-transform:uppercase;color:var(--m);margin-top:.4rem}
.fl{margin-bottom:1.1rem}
.fl label{display:block;font-size:.75rem;font-weight:600;color:var(--m);margin-bottom:.4rem;letter-spacing:.05em;text-transform:uppercase}
.fl input{width:100%;padding:.8rem 1rem;border-radius:10px;border:1px solid var(--bs);background:rgba(0,0,0,.4);color:var(--t);font-family:"JetBrains Mono",monospace;font-size:.9rem;outline:none;transition:border-color .2s,box-shadow .2s}
.fl input:focus{border-color:var(--a);box-shadow:0 0 0 3px rgba(107,138,255,.15)}
.fl input::placeholder{color:rgba(255,255,255,.18)}
.lbtn{width:100%;padding:.9rem;border-radius:10px;border:none;background:linear-gradient(135deg,#6b8aff 0%,#5060e0 100%);color:#fff;font-family:"Instrument Sans",sans-serif;font-size:.95rem;font-weight:700;cursor:pointer;margin-top:.25rem;box-shadow:0 6px 24px rgba(107,138,255,.35);transition:filter .15s,transform .15s}
.lbtn:hover{filter:brightness(1.1);transform:translateY(-2px)}
.lerr{margin-top:.9rem;padding:.65rem 1rem;border-radius:8px;background:rgba(255,85,102,.1);border:1px solid rgba(255,85,102,.25);color:var(--r);font-size:.82rem;text-align:center;display:none}
.lerr.show{display:block}
.lnote{margin-top:1.25rem;text-align:center;font-size:.7rem;color:var(--m);display:flex;align-items:center;justify-content:center;gap:.35rem}
#editor{display:none;position:relative;z-index:10;min-height:100vh;flex-direction:column}
#editor.show{display:flex}
.topbar{position:sticky;top:0;z-index:50;background:rgba(3,3,6,.8);backdrop-filter:blur(20px);border-bottom:1px solid var(--b);padding:.8rem 1.5rem;display:flex;align-items:center;justify-content:space-between;gap:1rem}
.tl{display:flex;align-items:center;gap:.75rem}
.tlogo{font-size:1rem;font-weight:700;letter-spacing:-.03em}
.tlogo span{color:var(--m);font-weight:500}
.tbadge{font-family:"JetBrains Mono",monospace;font-size:.6rem;padding:.25rem .55rem;border-radius:999px;background:rgba(61,212,160,.1);border:1px solid rgba(61,212,160,.25);color:var(--g);letter-spacing:.08em}
.tr{display:flex;align-items:center;gap:.6rem}
.outbtn{padding:.38rem .8rem;border-radius:8px;border:1px solid var(--bs);background:rgba(255,255,255,.04);color:var(--m);font-size:.78rem;font-weight:600;cursor:pointer;transition:color .15s,border-color .15s}
.outbtn:hover{color:var(--r);border-color:rgba(255,85,102,.3)}
.body{flex:1;display:grid;grid-template-columns:1fr 300px;gap:1.25rem;max-width:1400px;width:100%;margin:0 auto;padding:1.5rem}
@media(max-width:860px){.body{grid-template-columns:1fr}}
.cpanel{display:flex;flex-direction:column;gap:.75rem}
.ph{display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:.5rem}
.ptitle{font-size:.95rem;font-weight:600}
.pmeta{font-family:"JetBrains Mono",monospace;font-size:.7rem;color:var(--m)}
.eframe{position:relative;border-radius:var(--rad);border:1px solid var(--bs);overflow:hidden;background:rgba(0,0,0,.45)}
.edots{position:absolute;top:11px;left:14px;z-index:2;display:flex;gap:6px}
.edots span{width:11px;height:11px;border-radius:50%;display:block}
.edots span:nth-child(1){background:#ff5f57}
.edots span:nth-child(2){background:#febc2e}
.edots span:nth-child(3){background:#28c840}
.efname{position:absolute;top:9px;left:50%;transform:translateX(-50%);z-index:2;font-family:"JetBrains Mono",monospace;font-size:.7rem;color:var(--m)}
#code{width:100%;min-height:520px;padding:2.75rem 1.1rem 1.1rem;background:transparent;border:none;color:#b8d0ff;font-family:"JetBrains Mono",monospace;font-size:.82rem;line-height:1.65;resize:vertical;outline:none;tab-size:4}
#code::placeholder{color:rgba(255,255,255,.15)}
.abar{display:flex;gap:.6rem;flex-wrap:wrap}
.btn{display:inline-flex;align-items:center;gap:.4rem;padding:.65rem 1.15rem;border-radius:9px;border:none;font-family:"Instrument Sans",sans-serif;font-size:.85rem;font-weight:600;cursor:pointer;transition:filter .15s,transform .15s;white-space:nowrap}
.btn:hover{filter:brightness(1.1);transform:translateY(-1px)}
.btn:active{transform:translateY(0)}
.btn-pub{background:linear-gradient(135deg,#6b8aff 0%,#5060e0 100%);color:#fff;box-shadow:0 4px 18px rgba(107,138,255,.3)}
.btn-load{background:rgba(255,255,255,.05);border:1px solid var(--bs);color:var(--t)}
.btn-copy{background:rgba(61,212,160,.1);border:1px solid rgba(61,212,160,.2);color:var(--g)}
.btn-clr{background:rgba(255,85,102,.1);border:1px solid rgba(255,85,102,.2);color:var(--r)}
.sbar{padding:.55rem .9rem;border-radius:8px;font-family:"JetBrains Mono",monospace;font-size:.75rem;display:none}
.sbar.ok{display:block;background:rgba(61,212,160,.08);border:1px solid rgba(61,212,160,.2);color:var(--g)}
.sbar.err{display:block;background:rgba(255,85,102,.08);border:1px solid rgba(255,85,102,.2);color:var(--r)}
.side{display:flex;flex-direction:column;gap:1rem}
.icard{background:var(--card);border:1px solid var(--b);border-radius:var(--rad);padding:1.25rem;backdrop-filter:blur(10px)}
.ilabel{font-family:"JetBrains Mono",monospace;font-size:.6rem;font-weight:600;letter-spacing:.16em;text-transform:uppercase;color:var(--a);margin-bottom:.75rem}
.irow{display:flex;justify-content:space-between;align-items:center;padding:.45rem 0;border-bottom:1px solid var(--b);font-size:.82rem}
.irow:last-child{border-bottom:none}
.ik{color:var(--m)}
.iv{font-family:"JetBrains Mono",monospace;font-size:.75rem}
.iv.g{color:var(--g)}
.iv.r{color:var(--r)}
.tcard{background:rgba(107,138,255,.06);border:1px solid rgba(107,138,255,.15);border-radius:var(--rad);padding:1.1rem}
.tcard ul{list-style:none;display:flex;flex-direction:column;gap:.45rem}
.tcard li{font-size:.8rem;color:var(--m);display:flex;align-items:flex-start;gap:.5rem}
.tcard li::before{content:"→";color:var(--a);flex-shrink:0}
</style>
</head>
<body>
<div class="bg"></div>
<div class="bg-grid"></div>
<div id="login">
  <div class="lcard">
    <div class="llogo">
      <div class="nm">Vyron<span>.cc</span></div>
      <div class="sub">Source Manager</div>
    </div>
    <div class="fl">
      <label>Password</label>
      <input type="password" id="pw" placeholder="Enter password" autocomplete="off"/>
    </div>
    <button class="lbtn" onclick="doLogin()">Access Editor</button>
    <div class="lerr" id="lerr">Incorrect password.</div>
    <div class="lnote">
      <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="11" width="18" height="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg>
      Restricted — staff only
    </div>
  </div>
</div>
<div id="editor">
  <div class="topbar">
    <div class="tl">
      <div class="tlogo">Vyron<span>.cc</span></div>
      <span class="tbadge">&#9679; SOURCE MANAGER</span>
    </div>
    <div class="tr">
      <span id="saved-label" style="font-family:'JetBrains Mono',monospace;font-size:.7rem;color:var(--m)"></span>
      <button class="outbtn" onclick="doLogout()">Sign out</button>
    </div>
  </div>
  <div class="body">
    <div class="cpanel">
      <div class="ph">
        <div class="ptitle">Script Source</div>
        <div class="pmeta" id="lcount">0 lines</div>
      </div>
      <div class="eframe">
        <div class="edots"><span></span><span></span><span></span></div>
        <div class="efname">vyronrewrite.lua</div>
        <textarea id="code" spellcheck="false" placeholder="-- paste your Lua source here..."></textarea>
      </div>
      <div class="abar">
        <button class="btn btn-pub" onclick="publishSource()">Publish Source</button>
        <button class="btn btn-load" onclick="loadSource()">Load Current</button>
        <button class="btn btn-copy" onclick="copySource()">Copy</button>
        <button class="btn btn-clr" onclick="document.getElementById('code').value='';updateMeta()">Clear</button>
      </div>
      <div class="sbar" id="pub-status"></div>
    </div>
    <div class="side">
      <div class="icard">
        <div class="ilabel">Source Info</div>
        <div class="irow"><span class="ik">Status</span><span class="iv g" id="i-status">Ready</span></div>
        <div class="irow"><span class="ik">Lines</span><span class="iv" id="i-lines">&#8212;</span></div>
        <div class="irow"><span class="ik">Characters</span><span class="iv" id="i-chars">&#8212;</span></div>
        <div class="irow"><span class="ik">Last published</span><span class="iv" id="i-pub">&#8212;</span></div>
      </div>
      <div class="tcard">
        <div class="ilabel">Tips</div>
        <ul>
          <li>Ctrl+S to publish instantly</li>
          <li>Load Current pulls the live source</li>
          <li>Publish overwrites what clients receive</li>
        </ul>
      </div>
    </div>
  </div>
</div>
<script>
const API="https://bypass-production-5fff.up.railway.app";
const CORRECT_PW="__DASHBOARD_PW__";
let pw=null,attempts=0,lockUntil=0;
function doLogin(){const el=document.getElementById("lerr");if(Date.now()<lockUntil){el.textContent="Too many attempts. Wait "+Math.ceil((lockUntil-Date.now())/1000)+"s.";el.classList.add("show");return;}const v=document.getElementById("pw").value;if(v!==CORRECT_PW){attempts++;if(attempts>=5){lockUntil=Date.now()+5*60*1000;attempts=0;}el.textContent="Incorrect password.";el.classList.add("show");document.getElementById("pw").value="";return;}pw=v;attempts=0;el.classList.remove("show");document.getElementById("login").style.display="none";document.getElementById("editor").classList.add("show");loadSource();}
document.getElementById("pw").addEventListener("keydown",e=>{if(e.key==="Enter")doLogin();});
function doLogout(){pw=null;document.getElementById("editor").classList.remove("show");document.getElementById("login").style.display="";document.getElementById("pw").value="";document.getElementById("code").value="";}
function h(){return{"Content-Type":"application/json","X-Admin-Password":pw};}
function updateMeta(){const v=document.getElementById("code").value;const l=v?v.split("\n").length:0;document.getElementById("lcount").textContent=l+" lines";document.getElementById("i-lines").textContent=l;document.getElementById("i-chars").textContent=v.length;}
document.getElementById("code").addEventListener("input",updateMeta);
document.getElementById("code").addEventListener("keydown",e=>{if((e.ctrlKey||e.metaKey)&&e.key==="s"){e.preventDefault();publishSource();}if(e.key==="Tab"){e.preventDefault();const s=e.target.selectionStart,end=e.target.selectionEnd;e.target.value=e.target.value.substring(0,s)+"    "+e.target.value.substring(end);e.target.selectionStart=e.target.selectionEnd=s+4;updateMeta();}});
async function loadSource(){const el=document.getElementById("pub-status");try{const r=await fetch(API+"/admin/source",{headers:h()});const d=await r.json();document.getElementById("code").value=d.source||"";if(d.saved_at){document.getElementById("saved-label").textContent="Last saved: "+d.saved_at;document.getElementById("i-pub").textContent=d.saved_at;}updateMeta();el.textContent="Loaded.";el.className="sbar ok";setTimeout(()=>{el.className="sbar";},3000);}catch(e){el.textContent="Failed: "+e.message;el.className="sbar err";}}
async function publishSource(){const src=document.getElementById("code").value;const el=document.getElementById("pub-status");const si=document.getElementById("i-status");si.textContent="Publishing...";si.className="iv";try{const r=await fetch(API+"/admin/source",{method:"POST",headers:h(),body:JSON.stringify({source:src})});const d=await r.json();if(d.success){const now=new Date().toLocaleTimeString();el.textContent="Published at "+now;el.className="sbar ok";document.getElementById("saved-label").textContent="Last saved: "+now;document.getElementById("i-pub").textContent=now;si.textContent="Published";si.className="iv g";}else{el.textContent="Failed: "+(d.error||"Unknown");el.className="sbar err";si.textContent="Error";si.className="iv r";}}catch(e){el.textContent="Error: "+e.message;el.className="sbar err";si.textContent="Error";si.className="iv r";}}
function copySource(){const src=document.getElementById("code").value;if(!src)return;navigator.clipboard.writeText(src).then(()=>{const el=document.getElementById("pub-status");el.textContent="Copied.";el.className="sbar ok";setTimeout(()=>{el.className="sbar";},2000);});}
document.addEventListener("contextmenu",e=>e.preventDefault());
document.addEventListener("keydown",e=>{if(e.key==="F12"||(e.ctrlKey&&e.shiftKey&&["I","J","C"].includes(e.key)))e.preventDefault();});
</script>
</body>
</html>"""

LOGIN_HTML = ""  # no longer used


@app.route("/dashboard", methods=["GET"])
def dashboard():
    html = DASHBOARD_HTML.replace("__DASHBOARD_PW__", DASHBOARD_PASSWORD)
    return html, 200, {"Content-Type": "text/html"}


@app.route("/dashboard/login", methods=["GET", "POST"])
def dashboard_login():
    return redirect("/dashboard")


@app.route("/dashboard/logout")
def dashboard_logout():
    return redirect("/dashboard")


@app.route("/dashboard/save", methods=["POST"])
def dashboard_save():
    if not _check_admin_password(request):
        return jsonify({"success": False, "error": "Unauthorized"}), 403
    body = request.get_json(force=True) or {}
    source = body.get("source", "")
    try:
        with open(SOURCE_FILE, "w", encoding="utf-8") as f:
            f.write(source)
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/lookup", methods=["GET"])
def lookup_key():
    """Return full info about a key. Requires X-Admin-Password header."""
    if not _check_admin_password(request):
        return jsonify({"error": "Unauthorized"}), 403

    key = request.args.get("key", "").strip()
    if not key:
        return jsonify({"error": "Missing key parameter"}), 400

    data = load_data()
    now = int(time.time())

    # Determine type and owner
    owner_uid = None

    for uid, keys in data.get("keys", {}).items():
        if key in keys:
            owner_uid = uid
            break
    if owner_uid is None:
        for uid, keys in data.get("keys_internal", {}).items():
            if key in keys:
                owner_uid = uid
                break

    # Check temp keys if still not found
    is_temp = False
    if owner_uid is None:
        for uid, tkeys in data.get("temp_keys", {}).items():
            for t in tkeys:
                if t.get("key") == key:
                    owner_uid = uid
                    is_temp = True
                    break
            if owner_uid:
                break
        if owner_uid is None:
            for uid, tkeys in data.get("temp_keys_internal", {}).items():
                for t in tkeys:
                    if t.get("key") == key:
                        owner_uid = uid
                        is_temp = True
                        break
                if owner_uid:
                    break

    if owner_uid is None:
        return jsonify({"error": "Key not found"}), 404

    # Determine key type
    if is_temp:
        key_type = "temp"
    elif key.startswith("VyronInt-"):
        key_type = "internal"
    else:
        key_type = "external"

    # Expiry
    expiry_ts = data.get("key_expiry", {}).get(key)
    if expiry_ts is None:
        expiry_str = "Lifetime"
    elif now > expiry_ts:
        expiry_str = "Expired"
    else:
        secs_left = expiry_ts - now
        if secs_left < 3600:
            expiry_str = f"{secs_left // 60}m"
        elif secs_left < 86400:
            expiry_str = f"{secs_left // 3600}h"
        elif secs_left < 604800:
            expiry_str = f"{secs_left // 86400}d"
        elif secs_left < 2592000:
            expiry_str = f"{secs_left // 604800}w"
        else:
            expiry_str = f"{secs_left // 2592000} month(s)"

    # Active session check
    with active_sessions_lock:
        session = active_sessions.get(key)
    is_active = bool(session and now - session.get("last_seen", 0) <= SESSION_TIMEOUT)

    # Roblox info
    roblox_info = data.get("key_roblox_info", {}).get(key, {})

    # Blacklisted?
    blacklisted = owner_uid in data.get("blacklist", {})

    return jsonify({
        "key": key,
        "type": key_type,
        "owner_uid": owner_uid,
        "generated_by": data.get("key_generated_by", {}).get(key),
        "created": data.get("key_created", {}).get(key),
        "expiry": expiry_str,
        "expiry_ts": expiry_ts,
        "hwid": data.get("key_hwid", {}).get(key),
        "roblox_id": roblox_info.get("id", ""),
        "roblox_name": roblox_info.get("name", ""),
        "executions": data.get("key_executions", {}).get(key, 0),
        "last_exec": data.get("key_last_exec", {}).get(key),
        "active": is_active,
        "blacklisted": blacklisted,
    }), 200


def run_api():
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, use_reloader=False)

def start_api_thread():
    t = threading.Thread(target=run_api, daemon=True)
    t.start()
