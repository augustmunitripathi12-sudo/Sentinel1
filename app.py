import random
import string
import sqlite3
import uuid
import os
import json
from datetime import datetime, timedelta
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, session
from flask_socketio import SocketIO, emit, join_room
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from werkzeug.security import generate_password_hash, check_password_hash
# ===================== ADMIN CONFIG =====================
ADMIN_IP = "192.168.1.6"
# ========================================================
# ===================== CONFIG =====================
CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
def load_config():
    if not os.path.exists(CONFIG_FILE):
        default = {
            "secret_key": "change-this-to-a-very-long-random-string-123456789",
            "smtp_server": "smtp.gmail.com",
            "smtp_port": 587,
            "smtp_username": "",
            "smtp_password": "",
            "from_email": ""
        }
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(default, f, indent=2)
        return default
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        return json.load(f)
def save_config(cfg):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
config = load_config()
# ===================== HARDCODED SMTP CREDENTIALS =====================
SMTP_SERVER = "smtp.gmail.com"
SMTP_PORT = 587
SMTP_USERNAME = "augustmuni.tripathi12@gmail.com"
SMTP_PASSWORD = "verccigejkvydlts"
FROM_EMAIL = "augustmuni.tripathi12@gmail.com"
# =====================================================================
SECRET_KEY = config.get("secret_key", "change-this-secret-key")
# ==================================================
app = Flask(__name__)
app.config["SECRET_KEY"] = SECRET_KEY
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = "welcome"
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sentinel.db")
SPECIAL_CHARS = "!@#$%^&*"
# Admin debug log (always captures, even when SMTP works)
EMAIL_LOG = []
EMAIL_LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sentinel_emails.log")
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT UNIQUE NOT NULL, email TEXT UNIQUE NOT NULL, password_hash TEXT NOT NULL, finder_id TEXT UNIQUE NOT NULL, verified INTEGER DEFAULT 0, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
    c.execute("CREATE TABLE IF NOT EXISTS contacts (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, contact_id INTEGER NOT NULL, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, UNIQUE(user_id, contact_id))")
    c.execute("CREATE TABLE IF NOT EXISTS messages (id INTEGER PRIMARY KEY AUTOINCREMENT, sender_id INTEGER NOT NULL, receiver_id INTEGER NOT NULL, content TEXT NOT NULL, audio_data TEXT, is_voice INTEGER DEFAULT 0, timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP, is_read INTEGER DEFAULT 0)")
    c.execute("CREATE TABLE IF NOT EXISTS pending_verifications (id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT NOT NULL, email TEXT NOT NULL, password_hash TEXT NOT NULL, verification_token TEXT NOT NULL, finder_id TEXT NOT NULL, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
    c.execute("CREATE TABLE IF NOT EXISTS login_codes (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, code TEXT NOT NULL, expires_at TIMESTAMP NOT NULL, used INTEGER DEFAULT 0, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
    conn.commit()
    conn.close()
    migrate_db()
def migrate_db():
    """Add missing columns to old databases."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    # Migrate login_codes table
    c.execute("PRAGMA table_info(login_codes)")
    columns = [col[1] for col in c.fetchall()]
    if "created_at" not in columns:
        c.execute("ALTER TABLE login_codes ADD COLUMN created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP")
        conn.commit()
    # Migrate users table – add profile_picture column if missing
    c.execute("PRAGMA table_info(users)")
    user_columns = [col[1] for col in c.fetchall()]
    if "profile_picture" not in user_columns:
        c.execute("ALTER TABLE users ADD COLUMN profile_picture TEXT DEFAULT NULL")
        conn.commit()
    conn.close()
def is_admin():
    """Check if request is from admin IP (supports proxies via X-Forwarded-For)."""
    # Check direct connection IP
    if request.remote_addr == ADMIN_IP:
        return True
    # Check X-Forwarded-For header (set by reverse proxies / load balancers)
    forwarded_for = request.headers.get("X-Forwarded-For", "")
    if forwarded_for:
        # X-Forwarded-For can be a comma-separated list; the first entry is the real client
        client_ip = forwarded_for.split(",")[0].strip()
        if client_ip == ADMIN_IP:
            return True
    # Check X-Real-IP header (used by nginx)
    real_ip = request.headers.get("X-Real-IP", "").strip()
    if real_ip == ADMIN_IP:
        return True
    return False
def is_email_configured():
    return bool(SMTP_USERNAME and SMTP_PASSWORD and FROM_EMAIL)
def generate_finder_id(username):
    while True:
        digits = "".join(random.choices(string.digits, k=4))
        special = "".join(random.choices(SPECIAL_CHARS, k=2))
        finder_id = username + digits + special
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT 1 FROM users WHERE finder_id = ?", (finder_id,))
        exists = c.fetchone()
        conn.close()
        if not exists:
            return finder_id
def generate_2fa_code():
    return "".join(random.choices(string.digits, k=4))
def log_email(to_email, subject, body_html, body_text=""):
    entry = {
        "id": len(EMAIL_LOG) + 1,
        "to": to_email,
        "subject": subject,
        "body_html": body_html,
        "body_text": body_text,
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    }
    EMAIL_LOG.append(entry)
    try:
        with open(EMAIL_LOG_FILE, "a", encoding="utf-8") as f:
            f.write("\n" + "=" * 60 + "\n")
            f.write(" TO: " + to_email + "\n")
            f.write(" SUBJECT: " + subject + "\n")
            f.write(" TIME: " + entry["time"] + "\n")
            f.write("=" * 60 + "\n")
            f.write(body_text + "\n")
            f.write("=" * 60 + "\n")
    except Exception as e:
        print("Could not write to email log file: " + str(e))
    print("\n[" + "=" * 60)
    print(" EMAIL CAPTURED")
    print(" To: " + to_email)
    print(" Subject: " + subject)
    print(" Time: " + entry["time"])
    print(" Content: " + body_text)
    print("=" * 60 + "]")
    print(" View all emails at: http://localhost:5000/dev/mailbox\n")
    return True
def send_email(to_email, subject, body_html, body_text=""):
    # Always log for admin debugging
    log_email(to_email, subject, body_html, body_text)
    if not is_email_configured():
        print("WARNING: SMTP not configured. Email was NOT sent to real inbox.")
        print(" Visit http://localhost:5000/setup to configure SMTP.")
        return False
    try:
        import smtplib
        from email.mime.text import MIMEText
        from email.mime.multipart import MIMEMultipart
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = FROM_EMAIL
        msg["To"] = to_email
        msg.attach(MIMEText(body_text, "plain"))
        msg.attach(MIMEText(body_html, "html"))
        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_USERNAME, SMTP_PASSWORD)
            server.sendmail(FROM_EMAIL, to_email, msg.as_string())
        print(" Email sent successfully to " + to_email)
        return True
    except Exception as e:
        print("Email failed: " + str(e))
        return False
class User(UserMixin):
    def __init__(self, id, username, email, finder_id, verified, profile_picture=None):
        self.id = id
        self.username = username
        self.email = email
        self.finder_id = finder_id
        self.verified = verified
        self.profile_picture = profile_picture
@login_manager.user_loader
def load_user(user_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT id, username, email, finder_id, verified, profile_picture FROM users WHERE id = ?", (user_id,))
    user = c.fetchone()
    conn.close()
    if user:
        return User(user[0], user[1], user[2], user[3], user[4], user[5])
    return None
@app.route("/")
def welcome():
    return render_template("welcome.html")
@app.route("/tutorial")
def tutorial():
    return render_template("tutorial.html")
@app.route("/setup", methods=["GET", "POST"])
def setup():
    """Admin configuration page. Only accessible from admin IP."""
    if not is_admin():
        return "Access denied. Admin only.", 403
    if request.method == "POST":
        config["smtp_server"] = request.form.get("smtp_server", "smtp.gmail.com")
        try:
            config["smtp_port"] = int(request.form.get("smtp_port", 587))
        except ValueError:
            config["smtp_port"] = 587
        config["smtp_username"] = request.form.get("smtp_username", "").strip()
        config["smtp_password"] = request.form.get("smtp_password", "").strip()
        config["from_email"] = request.form.get("from_email", "").strip()
        config["secret_key"] = request.form.get("secret_key", SECRET_KEY).strip()
        save_config(config)
        flash("Settings saved. Restart the server for the new secret key to take effect.")
        return redirect(url_for("setup"))
    return render_template("setup.html", config=config, email_configured=is_email_configured())
@app.route("/login", methods=["GET", "POST"])
def login():
    if not is_email_configured():
        flash("Email is not configured. Please ask the admin to visit /setup.")
    if request.method == "POST":
        username = request.form["username"]
        password = request.form["password"]
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT id, username, email, password_hash, finder_id, verified FROM users WHERE username = ?", (username,))
        user = c.fetchone()
        conn.close()
        if not user:
            flash("Invalid username or password")
            return render_template("login.html")
        if not check_password_hash(user[3], password):
            flash("Invalid username or password")
            return render_template("login.html")
        if not is_email_configured():
            flash("Cannot send 2FA code: email is not configured. Ask the admin to visit /setup.")
            return render_template("login.html")
        code = generate_2fa_code()
        expires = datetime.now() + timedelta(minutes=10)
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("INSERT INTO login_codes (user_id, code, expires_at) VALUES (?, ?, ?)", (user[0], code, expires))
        conn.commit()
        conn.close()
        html = "<html><body style='font-family:Arial,sans-serif;max-width:500px;margin:40px auto;padding:20px;'><h2 style='color:#128c7e;'>Sentinel Login Code</h2><p>Hello " + user[1] + ",</p><p>Your 2FA code is:</p><div style='background:#f0f2f5;padding:20px;border-radius:12px;text-align:center;font-size:32px;font-weight:bold;letter-spacing:8px;color:#128c7e;margin:20px 0;'>" + code + "</div><p style='color:#667781;font-size:13px;'>Expires in 10 minutes.</p></body></html>"
        if send_email(user[2], "Your Sentinel Login Code", html, "Your code is: " + code):
            session["pending_2fa_user"] = user[0]
            return redirect(url_for("verify_2fa"))
        else:
            flash("Failed to send 2FA code. Check your SMTP settings at /setup.")
    return render_template("login.html")
@app.route("/verify-2fa", methods=["GET", "POST"])
def verify_2fa():
    user_id = session.get("pending_2fa_user")
    if not user_id:
        return redirect(url_for("login"))
    if request.method == "POST":
        code = request.form.get("code", "").strip()
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT id, user_id FROM login_codes WHERE user_id = ? AND code = ? AND used = 0 AND expires_at > ? ORDER BY created_at DESC LIMIT 1", (user_id, code, datetime.now()))
        result = c.fetchone()
        if result:
            c.execute("UPDATE login_codes SET used = 1 WHERE id = ?", (result[0],))
            conn.commit()
            conn.close()
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            c.execute("SELECT id, username, email, finder_id, verified, profile_picture FROM users WHERE id = ?", (user_id,))
            user = c.fetchone()
            conn.close()
            login_user(User(user[0], user[1], user[2], user[3], user[4], user[5]))
            session.pop("pending_2fa_user", None)
            return redirect(url_for("index"))
        else:
            flash("Invalid or expired code")
    return render_template("verify_2fa.html", is_admin=is_admin())
@app.route("/register", methods=["GET", "POST"])
def register():
    if not is_email_configured():
        flash("Email is not configured. Please ask the admin to visit /setup.")
    if request.method == "POST":
        username = request.form["username"]
        email = request.form["email"]
        password = request.form["password"]
        finder_id = generate_finder_id(username)
        token = str(uuid.uuid4())
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT 1 FROM users WHERE username = ? OR email = ?", (username, email))
        if c.fetchone():
            conn.close()
            flash("Username or email already registered")
            return render_template("register.html")
        c.execute("SELECT 1 FROM pending_verifications WHERE username = ? OR email = ?", (username, email))
        if c.fetchone():
            conn.close()
            flash("Username or email already in pending verification")
            return render_template("register.html")
        password_hash = generate_password_hash(password)
        c.execute("INSERT INTO pending_verifications (username, email, password_hash, verification_token, finder_id) VALUES (?, ?, ?, ?, ?)", (username, email, password_hash, token, finder_id))
        conn.commit()
        conn.close()
        verify_url = url_for("verify_email", token=token, _external=True)
        html = "<html><body style='font-family:Arial,sans-serif;max-width:500px;margin:40px auto;padding:20px;'><h2 style='color:#128c7e;'>Welcome to Sentinel!</h2><p>Hello " + username + ",</p><p>Click to verify:</p><a href='" + verify_url + "' style='display:inline-block;background:#128c7e;color:white;padding:14px 28px;border-radius:10px;text-decoration:none;font-weight:600;margin:16px 0;'>Verify My Account</a><p style='color:#667781;font-size:13px;'>Or: " + verify_url + "</p></body></html>"
        if not is_email_configured():
            flash("Cannot send verification email: SMTP not configured. Ask the admin to visit /setup.")
            return render_template("register.html")
        if send_email(email, "Verify Your Sentinel Account", html, "Click to verify: " + verify_url):
            flash("Verification email sent! Check your inbox (and spam folder).")
            return redirect(url_for("login"))
        else:
            flash("Failed to send verification email. Check your SMTP settings at /setup.")
    return render_template("register.html")
@app.route("/verify/<token>")
def verify_email(token):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT username, email, password_hash, finder_id FROM pending_verifications WHERE verification_token = ?", (token,))
    pending = c.fetchone()
    if not pending:
        conn.close()
        flash("Invalid or expired verification link")
        return redirect(url_for("login"))
    username, email, password_hash, finder_id = pending
    try:
        c.execute("INSERT INTO users (username, email, password_hash, finder_id, verified) VALUES (?, ?, ?, ?, 1)", (username, email, password_hash, finder_id))
        c.execute("DELETE FROM pending_verifications WHERE verification_token = ?", (token,))
        conn.commit()
        flash("Account verified! You can now log in.")
    except sqlite3.IntegrityError:
        flash("Account already exists. Please log in.")
    finally:
        conn.close()
    return redirect(url_for("login"))
@app.route("/dev/mailbox")
def dev_mailbox():
    """Admin only inbox. Only accessible from admin IP."""
    if not is_admin():
        return "Access denied. Admin only.", 403
    return render_template("dev_mailbox.html", emails=EMAIL_LOG)
@app.route("/chat")
@login_required
def index():
    return render_template("chat.html")
@app.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("welcome"))
@app.route("/api/profile")
@login_required
def profile():
    return jsonify({
        "username": current_user.username,
        "email": current_user.email,
        "finder_id": current_user.finder_id,
        "profile_picture": current_user.profile_picture
    })
@app.route("/api/profile/picture", methods=["POST"])
@login_required
def update_profile_picture():
    """Update the logged-in user's profile picture (base64 encoded)."""
    data = request.get_json()
    if not data or "image" not in data:
        return jsonify({"error": "No image data provided"}), 400
    image_data = data["image"]
    # Basic validation: must be a data URI for an image
    if not image_data.startswith("data:image/"):
        return jsonify({"error": "Invalid image format"}), 400
    # Limit size to ~2MB (base64 encoded ~2.7MB of raw data)
    if len(image_data) > 2_800_000:
        return jsonify({"error": "Image too large. Please use an image under 2MB."}), 400
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE users SET profile_picture = ? WHERE id = ?", (image_data, current_user.id))
    conn.commit()
    conn.close()
    # Update the in-memory user object so it reflects immediately
    current_user.profile_picture = image_data
    return jsonify({"success": True, "profile_picture": image_data})
@app.route("/api/contacts", methods=["GET", "POST"])
@login_required
def contacts():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    if request.method == "POST":
        data = request.get_json()
        finder_id = data.get("finder_id", "").strip()
        c.execute("SELECT id, username, finder_id FROM users WHERE finder_id = ?", (finder_id,))
        target = c.fetchone()
        if not target:
            conn.close()
            return jsonify({"error": "Finder ID not found"}), 404
        contact_id = target[0]
        if contact_id == current_user.id:
            conn.close()
            return jsonify({"error": "You cannot add yourself"}), 400
        try:
            c.execute("INSERT OR IGNORE INTO contacts (user_id, contact_id) VALUES (?, ?)", (current_user.id, contact_id))
            c.execute("INSERT OR IGNORE INTO contacts (user_id, contact_id) VALUES (?, ?)", (contact_id, current_user.id))
            conn.commit()
            conn.close()
            return jsonify({"success": True, "contact": {"id": contact_id, "username": target[1], "finder_id": target[2]}})
        except Exception as e:
            conn.close()
            return jsonify({"error": str(e)}), 500
    c.execute("SELECT u.id, u.username, u.finder_id FROM contacts c JOIN users u ON c.contact_id = u.id WHERE c.user_id = ? ORDER BY u.username", (current_user.id,))
    contacts = [{"id": r[0], "username": r[1], "finder_id": r[2]} for r in c.fetchall()]
    conn.close()
    return jsonify(contacts)
@app.route("/api/messages/<int:contact_id>")
@login_required
def messages(contact_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT m.sender_id, u.username, m.content, m.timestamp, m.is_voice, m.audio_data FROM messages m JOIN users u ON m.sender_id = u.id WHERE (m.sender_id = ? AND m.receiver_id = ?) OR (m.sender_id = ? AND m.receiver_id = ?) ORDER BY m.timestamp LIMIT 100", (current_user.id, contact_id, contact_id, current_user.id))
    msgs = [{"sender_id": r[0], "sender": r[1], "content": r[2], "timestamp": r[3], "is_voice": bool(r[4]), "audio_data": r[5]} for r in c.fetchall()]
    conn.close()
    return jsonify(msgs)
@socketio.on("connect")
def on_connect():
    if current_user.is_authenticated:
        join_room(str(current_user.id))
@socketio.on("private_message")
def handle_private_message(data):
    receiver_id = data.get("receiver_id")
    content = data.get("msg", "").strip()
    is_voice = data.get("is_voice", False)
    audio_data = data.get("audio_data", None)
    if not receiver_id or not content:
        return
    sender_id = current_user.id
    timestamp = datetime.now().strftime("%H:%M")
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT INTO messages (sender_id, receiver_id, content, is_voice, audio_data) VALUES (?, ?, ?, ?, ?)", (sender_id, receiver_id, content, 1 if is_voice else 0, audio_data))
    conn.commit()
    conn.close()
    payload = {"sender_id": sender_id, "sender": current_user.username, "receiver_id": receiver_id, "msg": content, "timestamp": timestamp, "is_voice": is_voice, "audio_data": audio_data}
    emit("new_message", payload, room=str(receiver_id))
    emit("new_message", payload, room=str(sender_id))
if __name__ == "__main__":
    init_db()
    print("=" * 60)
    print(" Sentinel Server Starting...")
    print(" Open your browser to: http://localhost:5000")
    if is_email_configured():
        print(" SMTP: Configured (" + SMTP_USERNAME + ")")
    else:
        print(" SMTP: NOT CONFIGURED")
        print(" Visit http://localhost:5000/setup to configure email.")
    print(" Admin IP: " + ADMIN_IP)
    print("=" * 60)
    socketio.run(app, host="0.0.0.0", port=5000, debug=True)