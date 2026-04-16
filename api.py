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
    else:
        body = request.get_json(force=True) or {}
        key = body.get("key", "").strip()
        hwid = body.get("hwid", "").strip()
        edition = str(body.get("edition") or body.get("ed") or "").strip().lower()

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
            return jsonify({"kick": False, "notify": True, "message": message, "sound_id": sound_id}), 200

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

            result.append({
                "key": key,
                "hwid": session["hwid"],
                "last_seen": session["last_seen"],
                "owner_uid": owner_uid,
                "expiry": expiry_str,
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

    if secret != API_SECRET:
        return jsonify({"success": False, "reason": "Unauthorized"}), 403

    if not key or not message:
        return jsonify({"success": False, "reason": "Missing key or message"}), 400

    with pending_notifs_lock:
        pending_notifs[key] = {"message": message, "sound_id": sound_id}

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
    job_id   = str(body.get("job_id", "")).strip()
    secret   = body.get("secret", "").strip()

    if secret != API_SECRET:
        return jsonify({"success": False, "reason": "Unauthorized"}), 403

    if not key or not place_id or not job_id:
        return jsonify({"success": False, "reason": "Missing key, place_id, or job_id"}), 400

    with pending_teleport_lock:
        pending_teleport[key] = {"place_id": place_id, "job_id": job_id}

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

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Vyron Source Editor</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    background: #0d0d14;
    color: #e0e0e0;
    font-family: 'Segoe UI', sans-serif;
    min-height: 100vh;
    display: flex;
    flex-direction: column;
  }
  header {
    background: #13131f;
    border-bottom: 1px solid #2a2a3d;
    padding: 14px 24px;
    display: flex;
    align-items: center;
    justify-content: space-between;
  }
  header h1 { font-size: 18px; color: #5080ff; letter-spacing: 1px; }
  header span { font-size: 12px; color: #666; }
  .logout { color: #ff5555; font-size: 13px; text-decoration: none; }
  .logout:hover { text-decoration: underline; }
  .container { flex: 1; display: flex; flex-direction: column; padding: 20px 24px; gap: 14px; }
  .toolbar {
    display: flex;
    align-items: center;
    gap: 10px;
    flex-wrap: wrap;
  }
  .btn {
    padding: 9px 20px;
    border: none;
    border-radius: 6px;
    font-size: 13px;
    font-weight: 600;
    cursor: pointer;
    transition: opacity 0.15s;
  }
  .btn:hover { opacity: 0.85; }
  .btn-save { background: #5080ff; color: #fff; }
  .btn-discard { background: #2a2a3d; color: #aaa; }
  .status {
    font-size: 12px;
    padding: 6px 12px;
    border-radius: 5px;
    display: none;
  }
  .status.success { background: #1a3a1a; color: #4caf50; display: inline-block; }
  .status.error   { background: #3a1a1a; color: #f44336; display: inline-block; }
  .meta { font-size: 12px; color: #555; margin-left: auto; }
  #editor {
    flex: 1;
    width: 100%;
    min-height: 70vh;
    background: #13131f;
    border: 1px solid #2a2a3d;
    border-radius: 8px;
    color: #e0e0e0;
    font-family: 'Cascadia Code', 'Fira Code', 'Consolas', monospace;
    font-size: 13px;
    line-height: 1.6;
    padding: 16px;
    resize: vertical;
    outline: none;
    tab-size: 4;
  }
  #editor:focus { border-color: #5080ff; }
  .line-count { font-size: 12px; color: #555; }
</style>
</head>
<body>
<header>
  <h1>⚡ Vyron Source Editor</h1>
  <div style="display:flex;align-items:center;gap:16px;">
    <span id="lineCount" class="line-count"></span>
    <a href="/dashboard/logout" class="logout">Logout</a>
  </div>
</header>
<div class="container">
  <div class="toolbar">
    <button class="btn btn-save" onclick="saveSource()">💾 Save & Publish</button>
    <button class="btn btn-discard" onclick="discardChanges()">↩ Discard</button>
    <span id="status" class="status"></span>
    <span class="meta" id="savedAt">{{ saved_at }}</span>
  </div>
  <textarea id="editor" spellcheck="false">{{ source }}</textarea>
</div>
<script>
  const original = document.getElementById('editor').value;

  function updateLineCount() {
    const lines = document.getElementById('editor').value.split('\\n').length;
    document.getElementById('lineCount').textContent = lines + ' lines';
  }

  document.getElementById('editor').addEventListener('input', updateLineCount);
  document.getElementById('editor').addEventListener('keydown', function(e) {
    if (e.key === 'Tab') {
      e.preventDefault();
      const s = this.selectionStart, end = this.selectionEnd;
      this.value = this.value.substring(0, s) + '    ' + this.value.substring(end);
      this.selectionStart = this.selectionEnd = s + 4;
    }
    if ((e.ctrlKey || e.metaKey) && e.key === 's') {
      e.preventDefault();
      saveSource();
    }
  });

  updateLineCount();

  function showStatus(msg, type) {
    const el = document.getElementById('status');
    el.textContent = msg;
    el.className = 'status ' + type;
    setTimeout(() => { el.className = 'status'; }, 3000);
  }

  function saveSource() {
    const content = document.getElementById('editor').value;
    fetch('/dashboard/save', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ source: content })
    })
    .then(r => r.json())
    .then(d => {
      if (d.success) {
        showStatus('✅ Published successfully', 'success');
        document.getElementById('savedAt').textContent = 'Last saved: ' + new Date().toLocaleTimeString();
      } else {
        showStatus('❌ ' + (d.error || 'Save failed'), 'error');
      }
    })
    .catch(() => showStatus('❌ Network error', 'error'));
  }

  function discardChanges() {
    if (confirm('Discard all unsaved changes?')) {
      document.getElementById('editor').value = original;
      updateLineCount();
    }
  }
</script>
</body>
</html>"""

LOGIN_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Vyron Dashboard Login</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    background: #0d0d14;
    color: #e0e0e0;
    font-family: 'Segoe UI', sans-serif;
    min-height: 100vh;
    display: flex;
    align-items: center;
    justify-content: center;
  }
  .card {
    background: #13131f;
    border: 1px solid #2a2a3d;
    border-radius: 12px;
    padding: 40px 36px;
    width: 340px;
    text-align: center;
  }
  h1 { color: #5080ff; font-size: 22px; margin-bottom: 6px; }
  p { color: #555; font-size: 13px; margin-bottom: 28px; }
  input[type=password] {
    width: 100%;
    padding: 11px 14px;
    background: #0d0d14;
    border: 1px solid #2a2a3d;
    border-radius: 7px;
    color: #e0e0e0;
    font-size: 14px;
    outline: none;
    margin-bottom: 14px;
  }
  input[type=password]:focus { border-color: #5080ff; }
  button {
    width: 100%;
    padding: 11px;
    background: #5080ff;
    border: none;
    border-radius: 7px;
    color: #fff;
    font-size: 14px;
    font-weight: 600;
    cursor: pointer;
  }
  button:hover { opacity: 0.88; }
  .error { color: #f44336; font-size: 13px; margin-top: 12px; }
</style>
</head>
<body>
<div class="card">
  <h1>⚡ Vyron.cc</h1>
  <p>Source Editor — Staff Only</p>
  <form method="POST">
    <input type="password" name="password" placeholder="Password" autofocus>
    <button type="submit">Login</button>
    {% if error %}<div class="error">{{ error }}</div>{% endif %}
  </form>
</div>
</body>
</html>"""


@app.route("/dashboard", methods=["GET"])
def dashboard():
    if not session.get("authed"):
        return redirect("/dashboard/login")

    source = ""
    saved_at = "Never"
    if os.path.exists(SOURCE_FILE):
        with open(SOURCE_FILE, "r", encoding="utf-8") as f:
            source = f.read()
        mtime = os.path.getmtime(SOURCE_FILE)
        saved_at = "Last saved: " + time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(mtime))

    return render_template_string(DASHBOARD_HTML, source=source, saved_at=saved_at)


@app.route("/dashboard/login", methods=["GET", "POST"])
def dashboard_login():
    error = None
    if request.method == "POST":
        pw = request.form.get("password", "")
        if pw == DASHBOARD_PASSWORD:
            session["authed"] = True
            return redirect("/dashboard")
        else:
            error = "Incorrect password."
    return render_template_string(LOGIN_HTML, error=error)


@app.route("/dashboard/logout")
def dashboard_logout():
    session.clear()
    return redirect("/dashboard/login")


@app.route("/dashboard/save", methods=["POST"])
def dashboard_save():
    if not session.get("authed"):
        return jsonify({"success": False, "error": "Unauthorized"}), 403

    body = request.get_json(force=True) or {}
    source = body.get("source", "")

    try:
        with open(SOURCE_FILE, "w", encoding="utf-8") as f:
            f.write(source)
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


def run_api():
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, use_reloader=False)

def start_api_thread():
    t = threading.Thread(target=run_api, daemon=True)
    t.start()
