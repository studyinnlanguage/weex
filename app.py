"""
TradeBot SaaS — Webapp (Wrapper around Bot-Engine)
====================================================

Yeh webapp bot-engine ke charon taraf ek WRAPPER hai.
Bot-engine ko BILKUL MODIFY NAHI KIYA — woh original simple trading bot hai.

Architecture:
  Webapp (port 5000)
    ├── Login page (email + password)
    ├── License activation (admin ne di hoti hai)
    ├── Admin panel (license generate karo, users manage karo)
    └── Dashboard → bot-engine ko iframe/proxy karta hai

  Bot-Engine (port 5001+ — ek per user)
    ├── Original trading dashboard (UNCHANGED)
    ├── EMA 8,13,21,55 strategy (UNCHANGED)
    ├── 1:3 RR hardcoded (UNCHANGED)
    └── Sab trading logic (UNCHANGED)

Flow:
  1. User signup → login
  2. User license key enter kare (admin se mili)
  3. License valid → webapp user ka bot-engine spawn karta hai (port 5001+)
  4. Webapp dashboard mein bot-engine ka dashboard iframe mein dikhta hai
  5. User API keys enter kare, coins add kare, START dabaye
  6. Bot 24/7 chalega (server pe)

Bot-engine mein KOI MODIFICATION NAHI — yeh sirf wrapper hai.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import hashlib
import secrets
import subprocess
import time
import uuid
import shutil
import signal
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from flask import Flask, jsonify, render_template, request, Response, session, redirect, url_for
import urllib.request
import urllib.error
import requests as req_lib

# ============================================================
# Setup
# ============================================================

BASE_DIR = Path(__file__).resolve().parent
BOT_ENGINE_DIR = BASE_DIR / "bot-engine"
DB_FILE = BASE_DIR / "database.json"
LOG_DIR = BASE_DIR / "logs"
USER_CONFIGS_DIR = BASE_DIR / "user_configs"
LOG_DIR.mkdir(exist_ok=True)
USER_CONFIGS_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_DIR / "saas.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("saas")

app = Flask(__name__, template_folder=str(BASE_DIR / "templates"),
            static_folder=str(BASE_DIR / "static"))
app.config["SECRET_KEY"] = os.environ.get("FLASK_SECRET", "tradebot-saas-secret-change-me-32chars")
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=7)

# ============================================================
# Configuration
# ============================================================

# Admin password — change via ADMIN_SECRET env var
ADMIN_SECRET = os.environ.get("ADMIN_SECRET", "AdminBot@2024!")

# Encryption key for API keys (must be 32+ chars)
ENCRYPTION_KEY = os.environ.get("ENCRYPTION_KEY", "tradebot-cloud-encryption-key-CHANGE-ME-32-chars")

# Port range for per-user bot-engine instances
PORT_START = 5001
PORT_END = 5999

logger.info("=" * 60)
logger.info(" TradeBot SaaS Webapp - Starting")
logger.info(f" Bot engine dir: {BOT_ENGINE_DIR}")
logger.info(f" Admin password: ***{ADMIN_SECRET[-3:]}")
logger.info("=" * 60)

# ============================================================
# Encryption (XOR + base64) — simple but effective for API keys
# ============================================================

def encrypt(plain_text: str) -> str:
    if not plain_text:
        return ""
    try:
        import base64
        key = ENCRYPTION_KEY.encode("utf-8")
        text = plain_text.encode("utf-8")
        result = bytes([text[i] ^ key[i % len(key)] for i in range(len(text))])
        return base64.b64encode(result).decode("utf-8")
    except Exception as e:
        logger.error(f"Encryption failed: {e}")
        return ""

def decrypt(cipher_text: str) -> str:
    if not cipher_text:
        return ""
    try:
        import base64
        key = ENCRYPTION_KEY.encode("utf-8")
        data = base64.b64decode(cipher_text.encode("utf-8"))
        result = bytes([data[i] ^ key[i % len(key)] for i in range(len(data))])
        return result.decode("utf-8")
    except Exception as e:
        logger.error(f"Decryption failed: {e}")
        return ""

def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    hashed = hashlib.sha256((salt + password).encode()).hexdigest()
    return f"{salt}${hashed}"

def verify_password(password: str, stored: str) -> bool:
    try:
        salt, hashed = stored.split("$")
        return hashlib.sha256((salt + password).encode()).hexdigest() == hashed
    except:
        return False

# ============================================================
# Database (JSON file)
# ============================================================

def load_db() -> dict:
    if DB_FILE.exists():
        try:
            with open(DB_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"DB load failed: {e}")
    return {"users": {}, "licenses": {}, "bot_processes": {}}

def save_db(db: dict):
    try:
        with open(DB_FILE, "w", encoding="utf-8") as f:
            json.dump(db, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.error(f"DB save failed: {e}")

DB = load_db()

# ============================================================
# Auth helpers
# ============================================================

def is_logged_in() -> bool:
    return "user_id" in session

def is_admin() -> bool:
    user_id = session.get("user_id")
    if not user_id:
        return False
    user = DB["users"].get(user_id, {})
    return user.get("role") == "admin"

def current_user() -> Optional[dict]:
    user_id = session.get("user_id")
    if not user_id:
        return None
    return DB["users"].get(user_id)

def check_subscription(user: dict) -> dict:
    """Check user's subscription status."""
    sub = user.get("subscription", {})
    if not sub:
        return {"active": False, "status": "none", "days_left": 0, "plan": "none"}

    expires_at = sub.get("expires_at")
    if not expires_at:
        return {"active": False, "status": "none", "days_left": 0, "plan": "none"}

    try:
        expiry = datetime.fromisoformat(expires_at.replace("Z", ""))
        now = datetime.utcnow()
        days_left = (expiry - now).days

        if days_left <= 0:
            return {"active": False, "status": "expired", "days_left": 0,
                    "plan": sub.get("plan", "trial"), "expires_at": expires_at}

        return {"active": True, "status": "active", "days_left": days_left,
                "plan": sub.get("plan", "trial"), "expires_at": expires_at}
    except:
        return {"active": False, "status": "error", "days_left": 0, "plan": "none"}

# ============================================================
# Bot-Engine Process Manager
# ============================================================

def find_free_port() -> int:
    """Find a free port for a new bot-engine instance."""
    used_ports = set()
    for proc_info in DB.get("bot_processes", {}).values():
        if proc_info.get("port"):
            used_ports.add(proc_info["port"])

    for port in range(PORT_START, PORT_END + 1):
        if port not in used_ports:
            return port
    raise RuntimeError("No free ports available")

def write_user_bot_config(user_id: str) -> str:
    """Write user's bot config to bot-engine's config.json.
    This is the ONLY file we touch in bot-engine dir — its config.json."""
    user = DB["users"].get(user_id, {})
    config = user.get("bot_config", {})

    # Decrypt API credentials
    api_key = decrypt(config.get("api_key_enc", ""))
    api_secret = decrypt(config.get("api_secret_enc", ""))
    api_passphrase = decrypt(config.get("api_passphrase_enc", ""))

    # Build config.json for bot-engine (matches bot-engine's expected format)
    bot_config = {
        "api_key": api_key,
        "api_secret": api_secret,
        "api_passphrase": api_passphrase,
        "exchange": config.get("exchange", "binance"),
        "testnet": config.get("testnet", True),
        "symbol": (config.get("symbols_list", ["BTCUSDT"]) or ["BTCUSDT"])[0],
        "symbols_list": config.get("symbols_list", ["BTCUSDT"]),
        "timeframe": config.get("timeframe", "5m"),
        "leverage": config.get("leverage", 10),
        "amount_mode": config.get("amount_mode", "fixed"),
        "amount": config.get("amount", 100),
        "amount_pct": config.get("amount_pct", 10),
        "stop_loss_pct": config.get("stop_loss_pct", 2),
        "take_profit_pct": config.get("take_profit_pct", 6),
        "mode": config.get("mode", "both"),
        "auto_start": False,
        "telegram_enabled": config.get("telegram_enabled", False),
        "telegram_bot_token": decrypt(config.get("telegram_bot_token_enc", "")),
        "telegram_chat_id": config.get("telegram_chat_id", ""),
        "email_enabled": config.get("email_enabled", False),
        "email_smtp_server": "smtp.gmail.com",
        "email_smtp_port": 587,
        "email_sender": config.get("email_sender", ""),
        "email_password": decrypt(config.get("email_password_enc", "")),
        "email_receiver": config.get("email_receiver", ""),
        "whatsapp_enabled": config.get("whatsapp_enabled", False),
        "whatsapp_phone": config.get("whatsapp_phone", ""),
        "whatsapp_apikey": decrypt(config.get("whatsapp_apikey_enc", "")),
    }

    # Write to bot-engine's config.json (this is THE config file bot-engine reads)
    config_path = BOT_ENGINE_DIR / "config.json"
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(bot_config, f, indent=2, ensure_ascii=False)

    return str(config_path)

def ensure_bot_engine_running(user_id: str) -> dict:
    """Ensure bot-engine process is running for this user.
    Starts the Flask web server (NOT trading) if not running.
    Returns status dict."""
    status = get_bot_status(user_id)
    if status["running"]:
        return {"success": True, "port": status["port"]}

    # Bot-engine not running — start it (just the web server, no trading)
    user = DB["users"].get(user_id)
    if not user:
        return {"success": False, "error": "User not found"}

    # Find free port
    try:
        port = find_free_port()
    except Exception as e:
        return {"success": False, "error": str(e)}

    # Write user config to bot-engine's config.json
    try:
        write_user_bot_config(user_id)
    except Exception as e:
        return {"success": False, "error": f"Config write failed: {e}"}

    # Spawn bot-engine process (just the Flask web server)
    # Use sys.executable so it works on Windows (python) AND Linux (python3)
    try:
        proc = subprocess.Popen(
            [sys.executable, "app.py"],
            cwd=str(BOT_ENGINE_DIR),
            env={**os.environ, "PORT": str(port), "HOST": "127.0.0.1"},
            stdout=open(LOG_DIR / f"bot_{user_id}.log", "w"),
            stderr=subprocess.STDOUT,
        )

        DB.setdefault("bot_processes", {})[user_id] = {
            "pid": proc.pid,
            "port": port,
            "started_at": datetime.utcnow().isoformat() + "Z",
        }
        save_db(DB)

        logger.info(f"Started bot-engine for user {user_id}: PID={proc.pid}, port={port}")
        time.sleep(2)  # Wait for Flask to start
        
        # Check if process is still alive after 2 seconds
        if not is_process_alive(proc.pid):
            # Process crashed immediately — read the log to find out why
            log_file = LOG_DIR / f"bot_{user_id}.log"
            error_detail = ""
            try:
                with open(log_file, "r", encoding="utf-8", errors="replace") as f:
                    error_detail = f.read()[-500:]  # Last 500 chars
            except:
                pass
            logger.error(f"Bot-engine crashed immediately for {user_id}. Log: {error_detail}")
            DB.get("bot_processes", {}).pop(user_id, None)
            save_db(DB)
            return {"success": False, "error": f"Bot-engine crashed on startup. Log: {error_detail[:200]}"}
        
        return {"success": True, "port": port, "pid": proc.pid}
    except Exception as e:
        logger.error(f"Failed to start bot-engine for {user_id}: {e}")
        return {"success": False, "error": str(e)}

def start_user_bot(user_id: str) -> dict:
    """Start a user's bot-engine instance."""
    user = DB["users"].get(user_id)
    if not user:
        return {"success": False, "error": "User not found"}

    # Check if already running
    existing = DB.get("bot_processes", {}).get(user_id)
    if existing and existing.get("pid"):
        try:
            os.kill(existing["pid"], 0)
            return {"success": True, "port": existing["port"], "message": "Bot already running"}
        except (ProcessLookupError, PermissionError):
            pass

    # Validate config
    config = user.get("bot_config", {})
    if not config.get("api_key_enc"):
        return {"success": False, "error": "API key not set. Please save settings first."}
    if config.get("exchange") == "weex" and not config.get("api_passphrase_enc"):
        return {"success": False, "error": "WEEX passphrase required"}
    if not config.get("symbols_list"):
        return {"success": False, "error": "Please add at least one coin"}

    # Find free port
    try:
        port = find_free_port()
    except Exception as e:
        return {"success": False, "error": str(e)}

    # Write user config to bot-engine's config.json
    try:
        write_user_bot_config(user_id)
    except Exception as e:
        return {"success": False, "error": f"Config write failed: {e}"}

    # Spawn bot-engine process
    # Use sys.executable so it works on Windows (python) AND Linux (python3)
    try:
        proc = subprocess.Popen(
            [sys.executable, "app.py"],
            cwd=str(BOT_ENGINE_DIR),
            env={**os.environ, "PORT": str(port), "HOST": "127.0.0.1"},
            stdout=open(LOG_DIR / f"bot_{user_id}.log", "w"),
            stderr=subprocess.STDOUT,
        )

        DB.setdefault("bot_processes", {})[user_id] = {
            "pid": proc.pid,
            "port": port,
            "started_at": datetime.utcnow().isoformat() + "Z",
        }
        save_db(DB)

        logger.info(f"Started bot-engine for user {user_id}: PID={proc.pid}, port={port}")
        time.sleep(2)
        return {"success": True, "port": port, "pid": proc.pid}
    except Exception as e:
        logger.error(f"Failed to start bot for {user_id}: {e}")
        return {"success": False, "error": str(e)}

def stop_user_bot(user_id: str) -> dict:
    """Stop a user's bot-engine instance."""
    proc_info = DB.get("bot_processes", {}).get(user_id)
    if not proc_info or not proc_info.get("pid"):
        return {"success": True, "message": "Bot not running"}

    pid = proc_info["pid"]
    kill_process(pid)  # Cross-platform kill

    DB.get("bot_processes", {}).pop(user_id, None)
    save_db(DB)
    logger.info(f"Stopped bot-engine for user {user_id}")
    return {"success": True, "message": "Bot stopped"}

def is_process_alive(pid: int) -> bool:
    """Check if a process is alive (cross-platform: Windows + Linux)."""
    try:
        if sys.platform == 'win32':
            # Windows: os.kill(pid, 0) doesn't work — use ctypes
            import ctypes
            kernel32 = ctypes.windll.kernel32
            SYNCHRONIZE = 0x00100000
            handle = kernel32.OpenProcess(SYNCHRONIZE, False, pid)
            if handle:
                kernel32.CloseHandle(handle)
                return True
            return False
        else:
            # Linux/Mac: signal 0 = check if alive
            os.kill(pid, 0)
            return True
    except (ProcessLookupError, PermissionError, OSError):
        return False

def kill_process(pid: int):
    """Kill a process (cross-platform)."""
    try:
        if sys.platform == 'win32':
            # Windows: use taskkill to kill process tree
            subprocess.run(['taskkill', '/F', '/T', '/PID', str(pid)],
                          capture_output=True, timeout=5)
        else:
            # Linux/Mac: SIGTERM then SIGKILL
            os.kill(pid, signal.SIGTERM)
            time.sleep(1)
            try:
                os.kill(pid, signal.SIGKILL)
            except:
                pass
    except (ProcessLookupError, PermissionError, OSError):
        pass

def get_bot_status(user_id: str) -> dict:
    """Get bot status for a user."""
    proc_info = DB.get("bot_processes", {}).get(user_id)
    if not proc_info or not proc_info.get("pid"):
        return {"running": False}

    # Check if process is alive (cross-platform)
    if is_process_alive(proc_info["pid"]):
        return {
            "running": True,
            "port": proc_info["port"],
            "pid": proc_info["pid"],
            "started_at": proc_info.get("started_at"),
        }
    else:
        # Process is dead — clean up
        DB.get("bot_processes", {}).pop(user_id, None)
        save_db(DB)
        return {"running": False}

def proxy_to_bot(user_id: str, method: str, path: str, body=None) -> dict:
    """Proxy a request to user's bot-engine instance."""
    status = get_bot_status(user_id)
    if not status["running"] or not status.get("port"):
        return {"success": False, "error": "Bot is not running"}

    try:
        url = f"http://127.0.0.1:{status['port']}{path}"
        data = json.dumps(body).encode("utf-8") if body and method != "GET" else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=10) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode("utf-8"))
        except:
            return {"success": False, "error": f"HTTP {e.code}"}
    except Exception as e:
        return {"success": False, "error": str(e)}

# ============================================================
# Routes — Pages
# ============================================================

@app.route("/")
def index():
    """Main page — login if not authenticated, else redirect to bot-engine dashboard."""
    # Handle logout redirect (prevents redirect loop)
    if request.args.get('logout') == '1':
        session.clear()
        return render_template("saas_login.html")
    if not is_logged_in():
        return render_template("saas_login.html")
    # Check if user actually exists in DB
    user = current_user()
    if not user:
        session.clear()
        return render_template("saas_login.html")
    # User is logged in — redirect directly to bot-engine dashboard
    # This shows the FULL bot-engine dashboard (chart, settings, indicators, etc.)
    return redirect("/bot/")

@app.route("/admin")
def admin_panel():
    """Admin panel."""
    if not is_admin():
        return render_template("saas_admin.html", admin_login_required=True)
    return render_template("saas_admin.html", admin_login_required=False)

@app.route("/favicon.ico")
def favicon():
    png_bytes = bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
        "0000000d49444154789c63000100000005000100"
        "0d0a2db40000000049454e44ae426082"
    )
    return Response(png_bytes, mimetype="image/png")

# ============================================================
# Auth API
# ============================================================

@app.route("/api/auth/signup", methods=["POST"])
def api_signup():
    """User signup with email + password."""
    data = request.get_json(force=True)
    email = (data.get("email") or "").strip().lower()
    password = data.get("password", "")
    name = (data.get("name") or "").strip()

    if not email or not password:
        return jsonify({"success": False, "error": "Email aur password zaroori hai"})
    if len(password) < 6:
        return jsonify({"success": False, "error": "Password kam az kam 6 characters ka hona chahiye"})
    if "@" not in email or "." not in email:
        return jsonify({"success": False, "error": "Sahi email address daalein"})

    # Check if exists
    for u in DB["users"].values():
        if u.get("email") == email:
            return jsonify({"success": False, "error": "Yeh email pehle se registered hai"})

    # First user becomes admin
    role = "admin" if len(DB["users"]) == 0 else "user"

    user_id = str(uuid.uuid4())
    now = datetime.utcnow().isoformat() + "Z"

    user = {
        "id": user_id,
        "email": email,
        "name": name or email.split("@")[0],
        "password_hash": hash_password(password),
        "role": role,
        "banned": False,
        "created_at": now,
        "subscription": {
            "plan": "none",
            "status": "inactive",  # user must enter license key
            "started_at": now,
            "expires_at": now,
        },
        "license_key": None,
        "bot_config": {
            "exchange": "binance",
            "testnet": True,
            "symbols_list": [],
            "timeframe": "5m",
            "leverage": 10,
            "amount_mode": "fixed",
            "amount": 100,
            "amount_pct": 10,
            "stop_loss_pct": 2,
            "take_profit_pct": 6,
            "mode": "both",
            "api_key_enc": "",
            "api_secret_enc": "",
            "api_passphrase_enc": "",
        },
    }
    DB["users"][user_id] = user
    save_db(DB)

    session["user_id"] = user_id
    session.permanent = True

    logger.info(f"New user signup: {email} (role={role})")

    return jsonify({
        "success": True,
        "user": {"id": user_id, "email": email, "name": user["name"], "role": role},
        "subscription": user["subscription"],
        "message": "Account created! Ab license key enter karein." if role == "user" else "Welcome Admin!",
    })

@app.route("/api/auth/login", methods=["POST"])
def api_login():
    """User login with email + password."""
    data = request.get_json(force=True)
    email = (data.get("email") or "").strip().lower()
    password = data.get("password", "")

    if not email or not password:
        return jsonify({"success": False, "error": "Email aur password daalein"})

    user = None
    for u in DB["users"].values():
        if u.get("email") == email:
            user = u
            break

    if not user or not verify_password(password, user.get("password_hash", "")):
        return jsonify({"success": False, "error": "Email ya password galat hai"})

    if user.get("banned"):
        return jsonify({"success": False, "error": "Account suspended. Admin se contact karein."})

    session["user_id"] = user["id"]
    session.permanent = True

    logger.info(f"User login: {email}")

    return jsonify({
        "success": True,
        "user": {"id": user["id"], "email": user["email"], "name": user["name"], "role": user["role"]},
        "subscription": user.get("subscription", {}),
    })

@app.route("/api/auth/logout", methods=["POST"])
def api_logout():
    # Stop bot-engine process for this user
    user = current_user()
    if user:
        stop_user_bot(user["id"])
    session.pop("user_id", None)
    return jsonify({"success": True})

@app.route("/api/auth/me", methods=["GET"])
def api_me():
    if not is_logged_in():
        return jsonify({"success": False, "error": "Not logged in"})

    user = current_user()
    if not user:
        return jsonify({"success": False, "error": "User not found"})

    sub_status = check_subscription(user)
    bot_status = get_bot_status(user["id"])

    return jsonify({
        "success": True,
        "user": {
            "id": user["id"],
            "email": user["email"],
            "name": user["name"],
            "role": user["role"],
        },
        "subscription": sub_status,
        "license_key": user.get("license_key"),
        "bot_config": {
            **user.get("bot_config", {}),
            "api_key_enc": None,
            "api_secret_enc": None,
            "api_passphrase_enc": None,
            "has_api_key": bool(user.get("bot_config", {}).get("api_key_enc")),
            "has_passphrase": bool(user.get("bot_config", {}).get("api_passphrase_enc")),
        },
        "bot_running": bot_status.get("running", False),
        "bot_port": bot_status.get("port"),
    })

# ============================================================
# License API
# ============================================================

@app.route("/api/license/activate", methods=["POST"])
def api_license_activate():
    """User activates a license key."""
    if not is_logged_in():
        return jsonify({"success": False, "error": "Not logged in"}), 401

    user = current_user()
    if not user:
        return jsonify({"success": False, "error": "User not found"}), 404

    data = request.get_json(force=True)
    key = (data.get("key") or "").strip().upper()

    if not key:
        return jsonify({"success": False, "error": "License key daalein"})

    # Check license in DB
    lic = DB.get("licenses", {}).get(key)
    if not lic:
        return jsonify({"success": False, "error": "License key invalid hai"})

    if lic.get("revoked"):
        return jsonify({"success": False, "error": "Yeh license revoke kar diya gaya hai. Admin se contact karein."})

    if lic.get("used_by") and lic["used_by"] != user["id"]:
        return jsonify({"success": False, "error": "Yeh license dusre user pe activate hai. Ek license sirf ek user pe chalta hai."})

    # Check expiry
    try:
        expiry = datetime.fromisoformat(lic["expires_at"].replace("Z", ""))
        if datetime.utcnow() > expiry:
            return jsonify({"success": False, "error": "License expire ho gaya. Admin se new license lein."})
    except:
        return jsonify({"success": False, "error": "License expiry check fail"})

    # Activate license for this user
    lic["used_by"] = user["id"]
    lic["activated_at"] = datetime.utcnow().isoformat() + "Z"
    lic["active"] = True

    # Update user subscription
    now = datetime.utcnow()
    user["subscription"] = {
        "plan": lic.get("plan", "basic"),
        "status": "active",
        "started_at": now.isoformat() + "Z",
        "expires_at": lic["expires_at"],
    }
    user["license_key"] = key

    save_db(DB)
    logger.info(f"License {key} activated for user {user['email']}")

    days_left = (expiry - now).days
    return jsonify({
        "success": True,
        "message": f"License activated! {days_left} days remaining.",
        "subscription": user["subscription"],
    })

# ============================================================
# Bot Config API
# ============================================================

@app.route("/api/bot/config", methods=["GET", "POST"])
def api_bot_config():
    if not is_logged_in():
        return jsonify({"success": False, "error": "Not logged in"}), 401

    user = current_user()
    if not user:
        return jsonify({"success": False, "error": "User not found"}), 404

    if request.method == "GET":
        config = user.get("bot_config", {})
        return jsonify({
            "success": True,
            "config": {
                **config,
                "api_key_enc": None,
                "api_secret_enc": None,
                "api_passphrase_enc": None,
                "has_api_key": bool(config.get("api_key_enc")),
                "has_passphrase": bool(config.get("api_passphrase_enc")),
            }
        })

    # POST — save config
    data = request.get_json(force=True)
    config = user.setdefault("bot_config", {})

    if "exchange" in data:
        config["exchange"] = "weex" if data["exchange"] == "weex" else "binance"
    if "testnet" in data:
        config["testnet"] = bool(data["testnet"])
    if data.get("api_key") and str(data["api_key"]).strip():
        config["api_key_enc"] = encrypt(str(data["api_key"]).strip())
    if data.get("api_secret") and str(data["api_secret"]).strip():
        config["api_secret_enc"] = encrypt(str(data["api_secret"]).strip())
    if "api_passphrase" in data:
        if data["api_passphrase"] and str(data["api_passphrase"]).strip():
            config["api_passphrase_enc"] = encrypt(str(data["api_passphrase"]).strip())
        elif data["api_passphrase"] == "":
            config["api_passphrase_enc"] = ""
    if "symbols_list" in data:
        symbols = data["symbols_list"] if isinstance(data["symbols_list"], list) else []
        config["symbols_list"] = [str(s).upper().strip() for s in symbols if str(s).strip()]
    if "timeframe" in data:
        config["timeframe"] = data["timeframe"]
    if "leverage" in data:
        max_lev = 500 if config.get("exchange") == "weex" else 125
        config["leverage"] = max(1, min(max_lev, int(data["leverage"]) or 10))
    if "amount_mode" in data:
        config["amount_mode"] = "percent" if data["amount_mode"] == "percent" else "fixed"
    if "amount" in data:
        config["amount"] = max(1, float(data["amount"]) or 100)
    if "amount_pct" in data:
        config["amount_pct"] = max(1, min(100, float(data["amount_pct"]) or 10))
    if "stop_loss_pct" in data:
        sl = max(0.5, min(50, float(data["stop_loss_pct"]) or 2))
        config["stop_loss_pct"] = sl
        config["take_profit_pct"] = sl * 3  # 1:3 RR hardcoded
    if "mode" in data:
        config["mode"] = data["mode"] if data["mode"] in ("long", "short", "both") else "both"

    save_db(DB)
    return jsonify({
        "success": True,
        "config": {
            **config,
            "api_key_enc": None,
            "api_secret_enc": None,
            "api_passphrase_enc": None,
            "has_api_key": bool(config.get("api_key_enc")),
            "has_passphrase": bool(config.get("api_passphrase_enc")),
        }
    })

# ============================================================
# Bot Control API
# ============================================================

@app.route("/api/bot/start", methods=["POST"])
def api_bot_start():
    if not is_logged_in():
        return jsonify({"success": False, "error": "Not logged in"}), 401

    user = current_user()
    if not user:
        return jsonify({"success": False, "error": "User not found"}), 404

    sub = check_subscription(user)
    if not sub["active"]:
        return jsonify({"success": False, "error": "Subscription inactive/expired. License activate karein."}), 403

    result = start_user_bot(user["id"])
    return jsonify(result)

@app.route("/api/bot/stop", methods=["POST"])
def api_bot_stop():
    if not is_logged_in():
        return jsonify({"success": False, "error": "Not logged in"}), 401

    user = current_user()
    if not user:
        return jsonify({"success": False, "error": "User not found"}), 404

    result = stop_user_bot(user["id"])
    return jsonify(result)

@app.route("/api/bot/status", methods=["GET"])
def api_bot_status():
    if not is_logged_in():
        return jsonify({"success": False, "error": "Not logged in"}), 401

    user = current_user()
    if not user:
        return jsonify({"success": False, "error": "User not found"}), 404

    status = get_bot_status(user["id"])
    if not status["running"]:
        return jsonify({"success": True, "running": False})

    # Fetch live data from bot-engine
    bot_data = proxy_to_bot(user["id"], "GET", "/api/status")
    bot_balance = proxy_to_bot(user["id"], "GET", "/api/balance")

    return jsonify({
        "success": True,
        "running": True,
        "port": status["port"],
        "started_at": status.get("started_at"),
        "bot_status": bot_data,
        "balance": bot_balance,
    })

@app.route("/api/bot/proxy", methods=["GET", "POST"])
def api_bot_proxy():
    """Proxy ANY request to user's bot-engine instance.
    This lets the dashboard talk to bot-engine without modifying bot-engine."""
    if not is_logged_in():
        return jsonify({"success": False, "error": "Not logged in"}), 401

    user = current_user()
    if not user:
        return jsonify({"success": False, "error": "User not found"}), 404

    path = request.args.get("path", "/")
    method = request.args.get("method", request.method).upper()
    body = request.get_json(silent=True) if request.method == "POST" else None

    result = proxy_to_bot(user["id"], method, path, body)
    return jsonify(result)

@app.route("/api/bot/embed")
def api_bot_embed():
    """Get bot-engine dashboard URL for iframe embedding."""
    if not is_logged_in():
        return jsonify({"success": False, "error": "Not logged in"}), 401

    user = current_user()
    if not user:
        return jsonify({"success": False, "error": "User not found"}), 404

    status = get_bot_status(user["id"])
    if not status["running"]:
        return jsonify({"success": False, "error": "Bot not running. Start bot first."})

    return jsonify({
        "success": True,
        "url": f"/bot/",
        "port": status["port"],
    })


# ============================================================
# FULL PROXY — serves bot-engine dashboard in iframe
# This proxies HTML, CSS, JS, API calls, AND socket.io polling
# so the bot-engine's full dashboard works inside the SaaS app.
# ============================================================

@app.route('/bot/', methods=['GET', 'POST', 'PUT', 'DELETE', 'PATCH', 'OPTIONS'])
@app.route('/bot/<path:path>', methods=['GET', 'POST', 'PUT', 'DELETE', 'PATCH', 'OPTIONS'])
def bot_engine_proxy(path=''):
    """Proxy ALL requests to user's bot-engine instance.
    Auto-starts bot-engine if not running."""
    if not is_logged_in():
        return redirect("/")

    user = current_user()
    if not user:
        session.clear()
        return redirect("/")

    # Auto-start bot-engine if not running
    status = get_bot_status(user["id"])
    if not status["running"]:
        result = ensure_bot_engine_running(user["id"])
        if not result["success"]:
            error_msg = result.get('error', 'Unknown error')
            logger.error(f"Bot-engine start failed for {user['id']}: {error_msg}")
            return f'''<html><body style="background:#0b0e11;color:#eaecef;font-family:sans-serif;text-align:center;padding:80px 20px;">
            <h2 style="color:#f6465d;">⚠ Bot Engine Start Failed</h2>
            <p style="color:#848e9c;">Error: {error_msg}</p>
            <p style="color:#848e9c;">Please check:</p>
            <ul style="color:#848e9c;text-align:left;max-width:400px;margin:20px auto;">
                <li>Python installed hai?</li>
                <li>Bot-engine ke dependencies installed hain? (pip install -r requirements.txt)</li>
                <li>Logs check karo: logs/bot_{user["id"]}.log</li>
            </ul>
            <button onclick="location.reload()" style="margin-top:20px;padding:10px 20px;background:#f0b90b;color:#0b0e11;border:none;border-radius:8px;cursor:pointer;font-weight:700;">Retry</button>
            </body></html>''', 500
        # Wait a bit more for Flask to fully start
        time.sleep(1)
        status = get_bot_status(user["id"])
        if not status["running"]:
            return '''<html><body style="background:#0b0e11;color:#eaecef;font-family:sans-serif;text-align:center;padding:80px 20px;">
            <h2 style="color:#f6465d;">⚠ Bot Engine Failed to Start</h2>
            <p style="color:#848e9c;">Process start hua par turant crash ho gaya.</p>
            <p style="color:#848e9c;">Logs check karo: logs/ folder mein bot_*.log file</p>
            <button onclick="location.reload()" style="margin-top:20px;padding:10px 20px;background:#f0b90b;color:#0b0e11;border:none;border-radius:8px;cursor:pointer;font-weight:700;">Retry</button>
            </body></html>''', 500

    port = status["port"]
    method = request.method

    # Build target URL
    url = f"http://127.0.0.1:{port}/{path}"
    if request.query_string:
        url += f"?{request.query_string.decode()}"

    # Forward request headers (excluding host/cookie)
    fwd_headers = {}
    for k, v in request.headers:
        if k.lower() not in ('host', 'cookie', 'content-length'):
            fwd_headers[k] = v

    # Get request body
    body = request.get_data() if method in ('POST', 'PUT', 'PATCH') else None

    try:
        resp = req_lib.request(method, url, headers=fwd_headers, data=body,
                               stream=True, timeout=30, allow_redirects=False)
    except req_lib.exceptions.ConnectionError:
        return "Bot engine not responding. Please refresh.", 502
    except Exception as e:
        return f"Proxy error: {str(e)}", 500

    # Build response headers (excluding hop-by-hop headers)
    excluded = {'content-encoding', 'transfer-encoding', 'connection',
                'content-length', 'keep-alive'}
    response_headers = [(k, v) for k, v in resp.headers.items()
                       if k.lower() not in excluded]

    content = resp.content
    content_type = resp.headers.get('content-type', '')

    # If HTML, rewrite URLs so they go through /bot/ prefix
    if 'text/html' in content_type:
        html = content.decode('utf-8', errors='replace')
        # Rewrite static file URLs
        html = html.replace('href="/static/', 'href="/bot/static/')
        html = html.replace('src="/static/', 'src="/bot/static/')
        # Rewrite API URLs in inline JS
        html = html.replace("fetch('/api/", "fetch('/bot/api/")
        html = html.replace('fetch("/api/', 'fetch("/bot/api/')
        # Rewrite socket.io using regex (handles io({ with newlines, spaces, etc.)
        import re
        html = re.sub(r'io\s*\(\s*\{', "io({path: '/bot/socket.io', ", html)
        html = re.sub(r'io\s*\(\s*\)', "io({path: '/bot/socket.io'})", html)
        # Inject floating logout + admin button (SaaS controls)
        saas_bar = '''
<div style="position:fixed;top:10px;right:10px;z-index:99999;display:flex;gap:8px;">
  <a href="/admin" style="background:rgba(246,70,93,0.2);color:#f6465d;border:1px solid #f6465d;padding:6px 14px;border-radius:6px;text-decoration:none;font-size:12px;font-family:sans-serif;">Admin</a>
  <button onclick="fetch('/api/auth/logout',{method:'POST'}).then(()=>window.location.href='/?logout=1')" style="background:#f6465d;color:white;border:none;padding:6px 14px;border-radius:6px;cursor:pointer;font-size:12px;font-family:sans-serif;">Logout</button>
</div>
'''
        html = html.replace('</body>', saas_bar + '</body>')
        content = html.encode('utf-8')

    # If JavaScript, rewrite fetch/socket URLs
    elif 'javascript' in content_type:
        js = content.decode('utf-8', errors='replace')
        # Rewrite ALL API fetch calls
        js = js.replace("fetch('/api/", "fetch('/bot/api/")
        js = js.replace('fetch("/api/', 'fetch("/bot/api/')
        # Rewrite socket.io using regex (handles io({ with newlines, spaces, etc.)
        import re
        js = re.sub(r'io\s*\(\s*\{', "io({path: '/bot/socket.io', ", js)
        js = re.sub(r'io\s*\(\s*\)', "io({path: '/bot/socket.io'})", js)
        content = js.encode('utf-8')

    return Response(content, status=resp.status_code, headers=response_headers)

# ============================================================
# Admin API
# ============================================================

@app.route("/api/admin/login", methods=["POST"])
def api_admin_login():
    """Admin login (separate from user)."""
    data = request.get_json(force=True)
    password = data.get("password", "")

    if password != ADMIN_SECRET:
        return jsonify({"success": False, "error": "Admin password galat hai"})

    # Find admin user
    admin_user = None
    for u in DB["users"].values():
        if u.get("role") == "admin":
            admin_user = u
            break

    if not admin_user:
        admin_id = str(uuid.uuid4())
        now = datetime.utcnow().isoformat() + "Z"
        admin_user = {
            "id": admin_id,
            "email": "admin@tradebot.com",
            "name": "Admin",
            "password_hash": hash_password(password),
            "role": "admin",
            "banned": False,
            "created_at": now,
            "subscription": {"plan": "lifetime", "status": "active", "started_at": now,
                            "expires_at": "9999-12-31T23:59:59Z"},
            "license_key": None,
            "bot_config": {},
        }
        DB["users"][admin_id] = admin_user
        save_db(DB)

    session["user_id"] = admin_user["id"]
    session.permanent = True
    return jsonify({"success": True, "user": {"id": admin_user["id"], "email": admin_user["email"], "role": "admin"}})

@app.route("/api/admin/users", methods=["GET"])
def api_admin_users():
    if not is_admin():
        return jsonify({"success": False, "error": "Admin access required"}), 403

    users = []
    for u in DB["users"].values():
        if u.get("role") == "admin":
            continue
        sub = check_subscription(u)
        bot_status = get_bot_status(u["id"])
        users.append({
            "id": u["id"],
            "email": u["email"],
            "name": u.get("name", ""),
            "role": u.get("role", "user"),
            "banned": u.get("banned", False),
            "created_at": u.get("created_at"),
            "subscription": sub,
            "license_key": u.get("license_key"),
            "bot_running": bot_status.get("running", False),
            "exchange": u.get("bot_config", {}).get("exchange", "none"),
        })

    return jsonify({"success": True, "users": users})

@app.route("/api/admin/licenses", methods=["GET"])
def api_admin_list_licenses():
    if not is_admin():
        return jsonify({"success": False, "error": "Admin access required"}), 403

    licenses = list(DB.get("licenses", {}).values())
    return jsonify({"success": True, "licenses": licenses})

@app.route("/api/admin/licenses/create", methods=["POST"])
def api_admin_create_license():
    """Admin creates a license key.
    Body: {days: int, plan: str, note: str}"""
    if not is_admin():
        return jsonify({"success": False, "error": "Admin access required"}), 403

    data = request.get_json(force=True)
    days = int(data.get("days", 30))
    plan = data.get("plan", "basic")
    note = data.get("note", "")

    if days <= 0:
        return jsonify({"success": False, "error": "Days must be positive"})

    # Generate license key: TRDBOT-XXXX-XXXX-XXXX-XXXX
    parts = []
    for _ in range(4):
        parts.append(secrets.token_hex(2).upper())
    key = f"TRDBOT-{parts[0]}-{parts[1]}-{parts[2]}-{parts[3]}"

    now = datetime.utcnow()
    expires = now + timedelta(days=days)

    lic = {
        "key": key,
        "plan": plan,
        "days": days,
        "note": note,
        "created_at": now.isoformat() + "Z",
        "expires_at": expires.isoformat() + "Z",
        "used_by": None,
        "activated_at": None,
        "active": False,
        "revoked": False,
    }

    DB.setdefault("licenses", {})[key] = lic
    save_db(DB)

    logger.info(f"Admin created license: {key} ({days}d)")
    return jsonify({"success": True, "license": lic})

@app.route("/api/admin/licenses/revoke", methods=["POST"])
def api_admin_revoke_license():
    if not is_admin():
        return jsonify({"success": False, "error": "Admin access required"}), 403

    data = request.get_json(force=True)
    key = (data.get("key") or "").strip().upper()

    lic = DB.get("licenses", {}).get(key)
    if not lic:
        return jsonify({"success": False, "error": "License not found"})

    lic["revoked"] = True
    lic["active"] = False

    # Also deactivate user's subscription if license was used
    if lic.get("used_by"):
        user = DB["users"].get(lic["used_by"])
        if user:
            user["subscription"] = {"plan": "none", "status": "inactive",
                                   "started_at": datetime.utcnow().isoformat() + "Z",
                                   "expires_at": datetime.utcnow().isoformat() + "Z"}
            # Stop their bot
            stop_user_bot(lic["used_by"])

    save_db(DB)
    return jsonify({"success": True, "message": f"License {key} revoked"})

@app.route("/api/admin/licenses/delete", methods=["POST"])
def api_admin_delete_license():
    if not is_admin():
        return jsonify({"success": False, "error": "Admin access required"}), 403

    data = request.get_json(force=True)
    key = (data.get("key") or "").strip().upper()

    if key not in DB.get("licenses", {}):
        return jsonify({"success": False, "error": "License not found"})

    DB["licenses"].pop(key, None)
    save_db(DB)
    return jsonify({"success": True, "message": f"License {key} deleted"})

@app.route("/api/admin/ban", methods=["POST"])
def api_admin_ban():
    if not is_admin():
        return jsonify({"success": False, "error": "Admin access required"}), 403

    data = request.get_json(force=True)
    user_id = data.get("user_id")
    banned = bool(data.get("banned", False))

    user = DB["users"].get(user_id)
    if not user:
        return jsonify({"success": False, "error": "User not found"})

    if user.get("role") == "admin" and banned:
        return jsonify({"success": False, "error": "Cannot ban admin"})

    user["banned"] = banned
    if banned:
        stop_user_bot(user_id)
    save_db(DB)

    return jsonify({"success": True, "banned": banned, "message": "Banned" if banned else "Unbanned"})

@app.route("/api/admin/delete", methods=["POST"])
def api_admin_delete():
    if not is_admin():
        return jsonify({"success": False, "error": "Admin access required"}), 403

    data = request.get_json(force=True)
    user_id = data.get("user_id")

    user = DB["users"].get(user_id)
    if not user:
        return jsonify({"success": False, "error": "User not found"})

    if user.get("role") == "admin":
        return jsonify({"success": False, "error": "Cannot delete admin"})

    stop_user_bot(user_id)
    DB["users"].pop(user_id, None)
    save_db(DB)
    return jsonify({"success": True, "message": "User deleted"})

# ============================================================
# Entry point
# ============================================================

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    host = os.environ.get("HOST", "0.0.0.0")
    logger.info(f"SaaS webapp running on port {port}")
    from werkzeug.serving import run_simple
    run_simple(host, port, app, use_reloader=False, use_debugger=False)
