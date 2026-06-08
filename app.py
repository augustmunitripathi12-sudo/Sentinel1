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
from werkzeug.middleware.proxy_fix import ProxyFix

# ===================== CONFIG =====================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "sentinel.db")
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")
EMAIL_LOG_FILE = os.path.join(BASE_DIR, "sentinel_emails.log")
ADMIN_IP = "192.168.1.6"
SPECIAL_CHARS = "!@#$%^&*"

# ===================== HARDCODED CREDENTIALS =====================
HARDCODED_EMAIL = "augustmuni.tripathi12@gmail.com"
HARDCODED_16DIGIT_PASSWORD = "verccigejkvydlts"  # verc cige jkvy dlts (spaces removed)

# ===================== FLASK =====================
app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = "login"

# ===================== CONFIG LOAD =====================
def load_config():
    if not os.path.exists(CONFIG_FILE):
        default = {
            "secret_key": "change-this-secret",
            "smtp_server": "smtp.gmail.com",
            "smtp_port": 587,
            "smtp_username": HARDCODED_EMAIL,
            "smtp_password": HARDCODED_16DIGIT_PASSWORD,
            "from_email": HARDCODED_EMAIL
        }
        with open(CONFIG_FILE, "w") as f:
            json.dump(default, f, indent=2)
        return default
    with open(CONFIG_FILE, "r") as f:
        return json.load(f)

config = load_config()
app.config["SECRET_KEY"] = config["secret_key"]

# ===================== DB INIT =====================
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE,
        email TEXT UNIQUE,
        password_hash TEXT,
        finder_id TEXT UNIQUE,
        verified INTEGER DEFAULT 0,
        profile_picture TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS pending_verifications (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT,
        email TEXT,
        password_hash TEXT,
        verification_token TEXT,
        finder_id TEXT
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS login_codes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        code TEXT,
        expires_at TIMESTAMP,
        used INTEGER DEFAULT 0
    )""")
    conn.commit()
    conn.close()

init_db()

# ===================== HELPERS =====================
def is_email_configured():
    return all([
        config.get("smtp_username"),
        config.get("smtp_password"),
        config.get("from_email")
    ])

def generate_finder_id(username):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    while True:
        finder_id = username + "".join(random.choices(string.digits, k=4))
        c.execute("SELECT 1 FROM users WHERE finder_id=?", (finder_id,))
        if not c.fetchone():
            conn.close()
            return finder_id

def generate_2fa():
    return "".join(random.choices(string.digits, k=4))

def generate_16_digit_password():
    chars = string.ascii_letters + string.digits + SPECIAL_CHARS
    return ''.join(random.choices(chars, k=16))

def send_email(to, subject, body):
    print(f"[EMAIL] {to} | {subject}")
    if not is_email_configured():
        with open(EMAIL_LOG_FILE, "a") as f:
            f.write(f"[{datetime.now()}] TO: {to} | SUBJ: {subject}\nBODY: {body}\n\n")
        return False
    try:
        import smtplib
        from email.mime.text import MIMEText
        msg = MIMEText(body, "html")
        msg["Subject"] = subject
        msg["From"] = config["from_email"]
        msg["To"] = to
        server = smtplib.SMTP(config["smtp_server"], config["smtp_port"])
        server.starttls()
        server.login(config["smtp_username"], config["smtp_password"])
        server.sendmail(config["from_email"], to, msg.as_string())
        server.quit()
        return True
    except Exception as e:
        print("EMAIL ERROR:", e)
        return False

# ===================== USER CLASS =====================
class User(UserMixin):
    def __init__(self, id, username, email, finder_id):
        self.id = id
        self.username = username
        self.email = email
        self.finder_id = finder_id

@login_manager.user_loader
def load_user(user_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT id, username, email, finder_id FROM users WHERE id=?", (user_id,))
    u = c.fetchone()
    conn.close()
    return User(*u) if u else None

# ===================== ROUTES =====================
@app.route("/")
def home():
    return "Sentinel Server Running"

@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        username = request.form["username"]
        email = request.form["email"]
        password = request.form["password"]
        
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT 1 FROM users WHERE email=? OR username=?", (email, username))
        if c.fetchone():
            conn.close()
            return "User already exists"
        
        finder_id = generate_finder_id(username)
        token = str(uuid.uuid4())
        password_hash = generate_password_hash(password)
        
        c.execute("""INSERT INTO pending_verifications
            (username,email,password_hash,verification_token,finder_id)
            VALUES (?,?,?,?,?)""",
            (username, email, password_hash, token, finder_id))
        conn.commit()
        conn.close()
        
        BASE_URL = request.host_url.rstrip('/')
        verify_url = BASE_URL + url_for("verify_email", token=token)
        
        email_body = f"""
        <h2>Welcome to Sentinel</h2>
        <p>Click here to verify your account:</p>
        <a href='{verify_url}'>VERIFY ACCOUNT</a>
        <p>Your Finder ID will be: <strong>{finder_id}</strong></p>
        """
        send_email(email, "Verify Your Sentinel Account", email_body)
        return "Registration successful. Check your email for verification link."
    
    return render_template("register.html")

@app.route("/verify/<token>")
def verify_email(token):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT username,email,password_hash,finder_id FROM pending_verifications WHERE verification_token=?", (token,))
    data = c.fetchone()
    if not data:
        conn.close()
        return "Invalid or expired verification link"
    
    c.execute("""INSERT INTO users(username,email,password_hash,finder_id,verified)
                 VALUES (?,?,?,?,1)""", data)
    c.execute("DELETE FROM pending_verifications WHERE verification_token=?", (token,))
    conn.commit()
    conn.close()
    return "Account verified successfully!"

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form["email"]
        password = request.form["password"]
        
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT id, username, email, password_hash, finder_id FROM users WHERE email=?", (email,))
        user_data = c.fetchone()
        conn.close()
        
        if user_data and check_password_hash(user_data[3], password):
            user = User(user_data[0], user_data[1], user_data[2], user_data[4])
            login_user(user)
            
            code = generate_2fa()
            expires = datetime.now() + timedelta(minutes=10)
            
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            c.execute("INSERT INTO login_codes (user_id, code, expires_at) VALUES (?,?,?)", 
                     (user_data[0], code, expires))
            conn.commit()
            conn.close()
            
            send_email(email, "Your Sentinel Login Code", f"Your 4-digit code: <strong>{code}</strong><br>Expires in 10 minutes.")
            return redirect(url_for("verify_2fa"))
        
        return "Invalid credentials"
    return render_template("login.html")

@app.route("/verify-2fa", methods=["GET", "POST"])
def verify_2fa():
    if request.method == "POST":
        code = request.form["code"]
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT user_id FROM login_codes WHERE code=? AND used=0 AND expires_at > ?", 
                 (code, datetime.now()))
        result = c.fetchone()
        if result:
            c.execute("UPDATE login_codes SET used=1 WHERE code=?", (code,))
            conn.commit()
            conn.close()
            return "Login successful!"
        conn.close()
        return "Invalid or expired code"
    return render_template("verify_2fa.html")

@app.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("home"))

@socketio.on("connect")
def handle_connect():
    if current_user.is_authenticated:
        join_room(str(current_user.id))
        emit("status", {"message": "Connected to Sentinel"})

# ===================== MAIN =====================
if __name__ == "__main__":
    os.makedirs("templates", exist_ok=True)
    for fname in ["register.html", "login.html", "verify_2fa.html"]:
        path = os.path.join("templates", fname)
        if not os.path.exists(path):
            with open(path, "w") as f:
                f.write(f"<h1>{fname.replace('.html','').title()}</h1><form method='post'>...</form>")
    
    socketio.run(app, host="0.0.0.0", port=5000, debug=True)
