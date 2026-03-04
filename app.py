import os, json, sqlite3, hashlib, secrets, smtplib, logging
from datetime import datetime, timedelta
from functools import wraps
from email.mime.text import MIMEText
from flask import (Flask, render_template, send_file, jsonify, abort,
                   request, redirect, url_for, session, flash, g)

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", secrets.token_hex(32))
logging.basicConfig(level=logging.INFO)

# ── Config ────────────────────────────────────────────────────────────────────
GAME_VERSION   = "1.0.0"
INSTALLER_NAME = "Reconquest_Setup_v1.0.0.exe"
INSTALLER_PATH = os.path.join(os.path.dirname(__file__), "static", "installer", INSTALLER_NAME)
STATS_FILE     = os.path.join(os.path.dirname(__file__), "data", "stats.json")
DB_PATH        = os.path.join(os.path.dirname(__file__), "data", "reconquest.db")

SMTP_HOST  = os.environ.get("SMTP_HOST",  "smtp.gmail.com")
SMTP_PORT  = int(os.environ.get("SMTP_PORT", 587))
SMTP_USER  = os.environ.get("SMTP_USER",  "")
SMTP_PASS  = os.environ.get("SMTP_PASS",  "")
SITE_URL   = os.environ.get("SITE_URL",   "http://localhost:5000")

os.makedirs(os.path.dirname(DB_PATH),    exist_ok=True)
os.makedirs(os.path.dirname(STATS_FILE), exist_ok=True)


# ── Database ──────────────────────────────────────────────────────────────────
def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA journal_mode=WAL")
    return g.db

@app.teardown_appcontext
def close_db(exc):
    db = g.pop("db", None)
    if db: db.close()

def init_db():
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    db.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            username    TEXT    NOT NULL UNIQUE,
            email       TEXT    NOT NULL UNIQUE,
            password    TEXT    NOT NULL,
            salt        TEXT    NOT NULL,
            is_admin    INTEGER NOT NULL DEFAULT 0,
            is_active   INTEGER NOT NULL DEFAULT 1,
            created_at  TEXT    NOT NULL,
            last_login  TEXT,
            avatar_url  TEXT
        );
        CREATE TABLE IF NOT EXISTS reset_tokens (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id    INTEGER NOT NULL,
            token      TEXT    NOT NULL UNIQUE,
            expires_at TEXT    NOT NULL,
            used       INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS download_log (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id    INTEGER,
            ip         TEXT,
            user_agent TEXT,
            ts         TEXT    NOT NULL
        );
        CREATE TABLE IF NOT EXISTS reviews (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id    INTEGER NOT NULL,
            rating     INTEGER NOT NULL CHECK(rating BETWEEN 1 AND 5),
            body       TEXT    NOT NULL,
            approved   INTEGER NOT NULL DEFAULT 0,
            created_at TEXT    NOT NULL
        );
    """)
    row = db.execute("SELECT id FROM users WHERE is_admin=1").fetchone()
    if not row:
        salt = secrets.token_hex(16)
        pwd  = _hash_pwd("admin1234", salt)
        db.execute(
            "INSERT INTO users (username,email,password,salt,is_admin,created_at) VALUES (?,?,?,?,1,?)",
            ("admin", "admin@reconquest.local", pwd, salt, _now())
        )
    db.commit()
    db.close()

def _now():
    return datetime.utcnow().isoformat()

def _hash_pwd(password, salt):
    return hashlib.sha256(f"{salt}{password}".encode()).hexdigest()


# ── Auth decorators ───────────────────────────────────────────────────────────
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login", next=request.path))
        return f(*args, **kwargs)
    return decorated

def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login", next=request.path))
        if not session.get("is_admin"):
            abort(403)
        return f(*args, **kwargs)
    return decorated

def current_user():
    if "user_id" in session:
        return get_db().execute("SELECT * FROM users WHERE id=?", (session["user_id"],)).fetchone()
    return None


# ── Stats ─────────────────────────────────────────────────────────────────────
def load_stats():
    if os.path.exists(STATS_FILE):
        with open(STATS_FILE) as f:
            return json.load(f)
    return {"total_downloads": 0}

def save_stats(s):
    with open(STATS_FILE, "w") as f:
        json.dump(s, f, indent=2)


# ── Email ─────────────────────────────────────────────────────────────────────
def send_reset_email(to_email, token):
    if not SMTP_USER:
        app.logger.warning("SMTP not configured. Reset link: %s/reset/%s", SITE_URL, token)
        return False
    try:
        link = f"{SITE_URL}/reset/{token}"
        msg  = MIMEText(
            f"Hola,\n\nRestablece tu contraseña de Reconquest en este enlace (válido 1 hora):\n\n"
            f"{link}\n\nSi no lo solicitaste, ignora este correo.\n\n— Equipo Reconquest",
            "plain", "utf-8"
        )
        msg["Subject"] = "Reconquest — Restablecer contraseña"
        msg["From"]    = SMTP_USER
        msg["To"]      = to_email
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
            s.starttls()
            s.login(SMTP_USER, SMTP_PASS)
            s.sendmail(SMTP_USER, to_email, msg.as_string())
        return True
    except Exception as e:
        app.logger.error("Email error: %s", e)
        return False


# ══════════════════════════════════════════════════════════════════════════════
# ROUTES — PUBLIC
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/")
def index():
    stats = load_stats()
    db = get_db()
    reviews = db.execute(
        """SELECT r.rating, r.body, r.created_at, u.username
           FROM reviews r JOIN users u ON r.user_id=u.id
           WHERE r.approved=1 ORDER BY r.created_at DESC"""
    ).fetchall()
    avg_rating = None
    if reviews:
        avg_rating = round(sum(r["rating"] for r in reviews) / len(reviews), 1)
    user = current_user()
    user_reviewed = False
    if user:
        user_reviewed = bool(db.execute(
            "SELECT id FROM reviews WHERE user_id=?", (user["id"],)
        ).fetchone())
    return render_template("index.html",
        version=GAME_VERSION,
        downloads=stats["total_downloads"],
        installer_exists=os.path.exists(INSTALLER_PATH),
        installer_size=(round(os.path.getsize(INSTALLER_PATH)/(1024*1024),1)
                        if os.path.exists(INSTALLER_PATH) else None),
        user=user,
        reviews=reviews,
        avg_rating=avg_rating,
        user_reviewed=user_reviewed,
    )

@app.route("/download")
@login_required
def download():
    if not os.path.exists(INSTALLER_PATH):
        abort(404)
    db = get_db()
    db.execute(
        "INSERT INTO download_log (user_id,ip,user_agent,ts) VALUES (?,?,?,?)",
        (session["user_id"], request.remote_addr, request.user_agent.string[:120], _now())
    )
    db.commit()
    stats = load_stats()
    stats["total_downloads"] = stats.get("total_downloads", 0) + 1
    save_stats(stats)
    return send_file(INSTALLER_PATH, as_attachment=True,
                     download_name=INSTALLER_NAME,
                     mimetype="application/octet-stream")

@app.route("/register", methods=["GET","POST"])
def register():
    if "user_id" in session:
        return redirect(url_for("index"))
    error = None
    if request.method == "POST":
        username = request.form.get("username","").strip()
        email    = request.form.get("email","").strip().lower()
        password = request.form.get("password","")
        confirm  = request.form.get("confirm","")
        if not username or not email or not password:
            error = "Todos los campos son obligatorios."
        elif len(password) < 8:
            error = "La contraseña debe tener al menos 8 caracteres."
        elif password != confirm:
            error = "Las contraseñas no coinciden."
        else:
            db  = get_db()
            dup = db.execute("SELECT id FROM users WHERE username=? OR email=?",
                             (username, email)).fetchone()
            if dup:
                error = "El nombre de usuario o email ya está registrado."
            else:
                salt = secrets.token_hex(16)
                db.execute(
                    "INSERT INTO users (username,email,password,salt,created_at) VALUES (?,?,?,?,?)",
                    (username, email, _hash_pwd(password, salt), salt, _now())
                )
                db.commit()
                flash("Cuenta creada. Ya puedes iniciar sesión.", "success")
                return redirect(url_for("login"))
    return render_template("register.html", error=error)

@app.route("/login", methods=["GET","POST"])
def login():
    if "user_id" in session:
        return redirect(url_for("index"))
    error = None
    if request.method == "POST":
        ident    = request.form.get("identifier","").strip()
        password = request.form.get("password","")
        db   = get_db()
        user = db.execute(
            "SELECT * FROM users WHERE (username=? OR email=?) AND is_active=1",
            (ident, ident.lower())
        ).fetchone()
        if not user or _hash_pwd(password, user["salt"]) != user["password"]:
            error = "Credenciales incorrectas."
        else:
            session.permanent = True
            app.permanent_session_lifetime = timedelta(days=7)
            session["user_id"]  = user["id"]
            session["username"] = user["username"]
            session["is_admin"] = bool(user["is_admin"])
            db.execute("UPDATE users SET last_login=? WHERE id=?", (_now(), user["id"]))
            db.commit()
            return redirect(request.args.get("next", url_for("index")))
    return render_template("login.html", error=error)

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("index"))

@app.route("/forgot", methods=["GET","POST"])
def forgot():
    sent = False
    dev_token = None
    if request.method == "POST":
        email = request.form.get("email","").strip().lower()
        db    = get_db()
        user  = db.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
        if user:
            token = secrets.token_urlsafe(32)
            exp   = (datetime.utcnow() + timedelta(hours=1)).isoformat()
            db.execute("INSERT INTO reset_tokens (user_id,token,expires_at) VALUES (?,?,?)",
                       (user["id"], token, exp))
            db.commit()
            ok = send_reset_email(email, token)
            if not ok:
                dev_token = token   # display link when SMTP not configured
        sent = True
    return render_template("forgot.html", sent=sent, dev_token=dev_token)

@app.route("/reset/<token>", methods=["GET","POST"])
def reset(token):
    db  = get_db()
    row = db.execute(
        "SELECT * FROM reset_tokens WHERE token=? AND used=0", (token,)
    ).fetchone()
    invalid = not row or row["expires_at"] < _now()
    error   = None
    if not invalid and request.method == "POST":
        pwd  = request.form.get("password","")
        conf = request.form.get("confirm","")
        if len(pwd) < 8:
            error = "La contraseña debe tener al menos 8 caracteres."
        elif pwd != conf:
            error = "Las contraseñas no coinciden."
        else:
            salt = secrets.token_hex(16)
            db.execute("UPDATE users SET password=?,salt=? WHERE id=?",
                       (_hash_pwd(pwd, salt), salt, row["user_id"]))
            db.execute("UPDATE reset_tokens SET used=1 WHERE token=?", (token,))
            db.commit()
            flash("Contraseña actualizada.", "success")
            return redirect(url_for("login"))
    return render_template("reset.html", invalid=invalid, token=token, error=error)


# ══════════════════════════════════════════════════════════════════════════════
# ROUTES — ADMIN
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/admin")
@admin_required
def admin_dashboard():
    db    = get_db()
    users = db.execute(
        "SELECT id,username,email,is_admin,is_active,created_at,last_login FROM users ORDER BY created_at DESC"
    ).fetchall()
    recent_dls = db.execute(
        """SELECT d.ts, d.ip, u.username
           FROM download_log d LEFT JOIN users u ON d.user_id=u.id
           ORDER BY d.ts DESC LIMIT 25"""
    ).fetchall()
    stats = load_stats()
    return render_template("admin.html",
        users=users,
        total_users=len(users),
        active_users=sum(1 for u in users if u["is_active"]),
        total_downloads=stats.get("total_downloads",0),
        recent_dls=recent_dls,
        version=GAME_VERSION,
    )

@app.route("/admin/user/<int:uid>/toggle", methods=["POST"])
@admin_required
def admin_toggle(uid):
    if uid == session["user_id"]:
        flash("No puedes desactivarte a ti mismo.", "error")
    else:
        get_db().execute("UPDATE users SET is_active = 1 - is_active WHERE id=?", (uid,))
        get_db().commit()
    return redirect(url_for("admin_dashboard"))

@app.route("/admin/user/<int:uid>/delete", methods=["POST"])
@admin_required
def admin_delete(uid):
    if uid == session["user_id"]:
        flash("No puedes eliminarte a ti mismo.", "error")
    else:
        db = get_db()
        db.execute("DELETE FROM users WHERE id=?", (uid,))
        db.execute("DELETE FROM reset_tokens WHERE user_id=?", (uid,))
        db.commit()
    return redirect(url_for("admin_dashboard"))

@app.route("/admin/user/<int:uid>/toggle_admin", methods=["POST"])
@admin_required
def admin_toggle_admin(uid):
    if uid != session["user_id"]:
        get_db().execute("UPDATE users SET is_admin = 1 - is_admin WHERE id=?", (uid,))
        get_db().commit()
    return redirect(url_for("admin_dashboard"))

@app.route("/api/stats")
@admin_required
def api_stats():
    db = get_db()
    return jsonify({
        **load_stats(),
        "total_users":  db.execute("SELECT COUNT(*) FROM users").fetchone()[0],
        "active_users": db.execute("SELECT COUNT(*) FROM users WHERE is_active=1").fetchone()[0],
    })

@app.route("/api/version")
def api_version():
    return jsonify({"version": GAME_VERSION, "available": os.path.exists(INSTALLER_PATH)})


# ══════════════════════════════════════════════════════════════════════════════
# REVIEWS
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/reviews/submit", methods=["POST"])
@login_required
def review_submit():
    rating = request.form.get("rating", "").strip()
    body   = request.form.get("body", "").strip()
    if not rating or not body:
        flash("Completa la puntuación y el comentario.", "error")
        return redirect(url_for("index") + "#resenas")
    if not rating.isdigit() or not (1 <= int(rating) <= 5):
        flash("Puntuación no válida.", "error")
        return redirect(url_for("index") + "#resenas")
    if len(body) < 10:
        flash("El comentario es demasiado corto (mínimo 10 caracteres).", "error")
        return redirect(url_for("index") + "#resenas")
    if len(body) > 800:
        flash("El comentario es demasiado largo (máximo 800 caracteres).", "error")
        return redirect(url_for("index") + "#resenas")
    db = get_db()
    existing = db.execute(
        "SELECT id FROM reviews WHERE user_id=?", (session["user_id"],)
    ).fetchone()
    if existing:
        flash("Ya has enviado una reseña. Solo se permite una por usuario.", "error")
        return redirect(url_for("index") + "#resenas")
    db.execute(
        "INSERT INTO reviews (user_id, rating, body, created_at) VALUES (?,?,?,?)",
        (session["user_id"], int(rating), body, _now())
    )
    db.commit()
    flash("Reseña enviada. Estará visible tras ser aprobada.", "success")
    return redirect(url_for("index") + "#resenas")


@app.route("/admin/reviews")
@admin_required
def admin_reviews():
    db      = get_db()
    pending = db.execute(
        """SELECT r.*, u.username FROM reviews r
           JOIN users u ON r.user_id=u.id
           WHERE r.approved=0 ORDER BY r.created_at DESC"""
    ).fetchall()
    approved = db.execute(
        """SELECT r.*, u.username FROM reviews r
           JOIN users u ON r.user_id=u.id
           WHERE r.approved=1 ORDER BY r.created_at DESC"""
    ).fetchall()
    return render_template("admin_reviews.html",
                           pending=pending, approved=approved)

@app.route("/admin/reviews/<int:rid>/approve", methods=["POST"])
@admin_required
def review_approve(rid):
    get_db().execute("UPDATE reviews SET approved=1 WHERE id=?", (rid,))
    get_db().commit()
    return redirect(url_for("admin_reviews"))

@app.route("/admin/reviews/<int:rid>/delete", methods=["POST"])
@admin_required
def review_delete(rid):
    get_db().execute("DELETE FROM reviews WHERE id=?", (rid,))
    get_db().commit()
    return redirect(url_for("admin_reviews"))


# ══════════════════════════════════════════════════════════════════════════════
# PROFILE
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/profile")
@login_required
def profile():
    db   = get_db()
    user = db.execute("SELECT * FROM users WHERE id=?", (session["user_id"],)).fetchone()
    reviews = db.execute(
        "SELECT * FROM reviews WHERE user_id=? ORDER BY created_at DESC",
        (session["user_id"],)
    ).fetchall()
    dl_count = db.execute(
        "SELECT COUNT(*) FROM download_log WHERE user_id=?",
        (session["user_id"],)
    ).fetchone()[0]
    return render_template("profile.html", user=user, reviews=reviews, dl_count=dl_count)


@app.route("/profile/edit", methods=["GET","POST"])
@login_required
def profile_edit():
    db   = get_db()
    user = db.execute("SELECT * FROM users WHERE id=?", (session["user_id"],)).fetchone()
    error = None
    if request.method == "POST":
        action = request.form.get("action")

        if action == "email":
            new_email = request.form.get("email","").strip().lower()
            if not new_email:
                error = "El email no puede estar vacío."
            else:
                dup = db.execute(
                    "SELECT id FROM users WHERE email=? AND id!=?",
                    (new_email, session["user_id"])
                ).fetchone()
                if dup:
                    error = "Ese email ya está en uso."
                else:
                    db.execute("UPDATE users SET email=? WHERE id=?",
                               (new_email, session["user_id"]))
                    db.commit()
                    flash("Email actualizado correctamente.", "success")
                    return redirect(url_for("profile"))

        elif action == "password":
            current  = request.form.get("current","")
            new_pwd  = request.form.get("password","")
            confirm  = request.form.get("confirm","")
            if _hash_pwd(current, user["salt"]) != user["password"]:
                error = "La contraseña actual no es correcta."
            elif len(new_pwd) < 8:
                error = "La nueva contraseña debe tener al menos 8 caracteres."
            elif new_pwd != confirm:
                error = "Las contraseñas no coinciden."
            else:
                salt = secrets.token_hex(16)
                db.execute("UPDATE users SET password=?,salt=? WHERE id=?",
                           (_hash_pwd(new_pwd, salt), salt, session["user_id"]))
                db.commit()
                flash("Contraseña actualizada correctamente.", "success")
                return redirect(url_for("profile"))

        elif action == "avatar":
            avatar_url = request.form.get("avatar_url","").strip()
            db.execute("UPDATE users SET avatar_url=? WHERE id=?",
                       (avatar_url or None, session["user_id"]))
            db.commit()
            flash("Avatar actualizado.", "success")
            return redirect(url_for("profile"))

        user = db.execute("SELECT * FROM users WHERE id=?", (session["user_id"],)).fetchone()

    return render_template("profile_edit.html", user=user, error=error)


# ══════════════════════════════════════════════════════════════════════════════
# ADMIN — CHART DATA
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/api/admin/downloads_chart")
@admin_required
def api_downloads_chart():
    db = get_db()
    rows = db.execute(
        """SELECT substr(ts,1,10) as day, COUNT(*) as cnt
           FROM download_log
           GROUP BY day
           ORDER BY day ASC
           LIMIT 30"""
    ).fetchall()
    return jsonify({
        "labels": [r["day"] for r in rows],
        "data":   [r["cnt"] for r in rows],
    })


if __name__ == "__main__":
    init_db()
    app.run(debug=True, host="0.0.0.0", port=5000)
