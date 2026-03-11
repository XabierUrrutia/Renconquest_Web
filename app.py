import os, json, hashlib, secrets, smtplib, logging
import urllib.request, urllib.error
from datetime import datetime, timedelta
from functools import wraps
from email.mime.text import MIMEText
from flask import (Flask, render_template, send_file, jsonify, abort,
                   request, redirect, url_for, session, flash, g)
import psycopg2
import psycopg2.extras

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", secrets.token_hex(32))
logging.basicConfig(level=logging.INFO)

# ── Config ────────────────────────────────────────────────────────────────────
GAME_VERSION   = "1.0.0"
INSTALLER_NAME = "Reconquest_Setup_v1.0.0.exe"
INSTALLER_URL  = "https://github.com/XabierUrrutia/Renconquest_Web/releases/download/v1.0.0/Reconquest_Setup_v1.0.0.exe"

OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")

SMTP_HOST  = os.environ.get("SMTP_HOST",  "smtp.gmail.com")
SMTP_PORT  = int(os.environ.get("SMTP_PORT", 587))
SMTP_USER  = os.environ.get("SMTP_USER",  "")
SMTP_PASS  = os.environ.get("SMTP_PASS",  "")
SITE_URL   = os.environ.get("SITE_URL",   "http://localhost:5000")

DATABASE_URL = os.environ.get("DATABASE_URL", "")


# ── Database ──────────────────────────────────────────────────────────────────
def get_db():
    if "db" not in g:
        conn = psycopg2.connect(DATABASE_URL)
        conn.autocommit = False
        g.db = conn
    return g.db

def db_execute(query, params=()):
    """Ejecuta una query y devuelve el cursor."""
    db = get_db()
    cur = db.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    # Convertir placeholders ? de SQLite a %s de PostgreSQL
    query = query.replace("?", "%s")
    cur.execute(query, params)
    return cur

def db_fetchone(query, params=()):
    cur = db_execute(query, params)
    return cur.fetchone()

def db_fetchall(query, params=()):
    cur = db_execute(query, params)
    return cur.fetchall()

def db_commit():
    get_db().commit()

@app.teardown_appcontext
def close_db(exc):
    db = g.pop("db", None)
    if db:
        if exc:
            db.rollback()
        db.close()

def init_db():
    conn = psycopg2.connect(DATABASE_URL)
    cur  = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id          SERIAL PRIMARY KEY,
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
            id         SERIAL PRIMARY KEY,
            user_id    INTEGER NOT NULL,
            token      TEXT    NOT NULL UNIQUE,
            expires_at TEXT    NOT NULL,
            used       INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS download_log (
            id         SERIAL PRIMARY KEY,
            user_id    INTEGER,
            ip         TEXT,
            user_agent TEXT,
            ts         TEXT    NOT NULL
        );
        CREATE TABLE IF NOT EXISTS bug_reports (
            id          SERIAL PRIMARY KEY,
            user_id     INTEGER,
            description TEXT    NOT NULL,
            created_at  TEXT    NOT NULL
        );
        CREATE TABLE IF NOT EXISTS reviews (
            id         SERIAL PRIMARY KEY,
            user_id    INTEGER NOT NULL,
            rating     INTEGER NOT NULL CHECK(rating BETWEEN 1 AND 5),
            body       TEXT    NOT NULL,
            approved   INTEGER NOT NULL DEFAULT 0,
            created_at TEXT    NOT NULL
        );
    """)
    cur.execute("SELECT id FROM users WHERE is_admin=1 LIMIT 1")
    if not cur.fetchone():
        salt = secrets.token_hex(16)
        pwd  = _hash_pwd("admin1234", salt)
        cur.execute(
            "INSERT INTO users (username,email,password,salt,is_admin,created_at) VALUES (%s,%s,%s,%s,1,%s)",
            ("admin", "admin@reconquest.local", pwd, salt, _now())
        )
    conn.commit()
    conn.close()

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
        return db_fetchone("SELECT * FROM users WHERE id=%s", (session["user_id"],))
    return None


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
# AI HELPER
# ══════════════════════════════════════════════════════════════════════════════

def openrouter_call(prompt, max_tokens=300):
    """Llama a OpenRouter y devuelve el texto de respuesta o None si falla."""
    if not OPENROUTER_API_KEY:
        return None
    payload = json.dumps({
        "model": "mistralai/mistral-7b-instruct:free",
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://openrouter.ai/api/v1/chat/completions",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {OPENROUTER_API_KEY}"
        },
        method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            return result["choices"][0]["message"]["content"].strip()
    except Exception as e:
        app.logger.error("OpenRouter error: %s", e)
        return None

def review_is_clean(text):
    """Devuelve (True, None) si la reseña es válida, o (False, motivo) si no lo es."""
    if not OPENROUTER_API_KEY:
        return True, None  # Si no hay API key, se permite sin filtro
    prompt = f"""Eres un moderador de contenido para un videojuego. Analiza la siguiente reseña y determina si contiene:
- Insultos, lenguaje ofensivo, odio o contenido inapropiado
- Spam, texto sin sentido, caracteres aleatorios o contenido irrelevante

Reseña: "{text}"

Responde ÚNICAMENTE con JSON en este formato exacto, sin texto adicional:
{{"ok": true}} si la reseña es válida
{{"ok": false, "reason": "motivo breve en español"}} si no lo es"""
    reply = openrouter_call(prompt, max_tokens=80)
    if not reply:
        return True, None  # Si falla la API, se permite
    try:
        reply = reply.replace("```json", "").replace("```", "").strip()
        data = json.loads(reply)
        if data.get("ok"):
            return True, None
        return False, data.get("reason", "Contenido no permitido.")
    except Exception:
        return True, None  # Si falla el parseo, se permite


# ══════════════════════════════════════════════════════════════════════════════
# ROUTES — PUBLIC
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/")
def index():
    reviews = db_fetchall(
        """SELECT r.rating, r.body, r.created_at, u.username
           FROM reviews r JOIN users u ON r.user_id=u.id
           WHERE r.approved=1 ORDER BY r.created_at DESC"""
    )
    avg_rating = None
    if reviews:
        avg_rating = round(sum(r["rating"] for r in reviews) / len(reviews), 1)
    user = current_user()
    user_reviewed = False
    if user:
        user_reviewed = bool(db_fetchone(
            "SELECT id FROM reviews WHERE user_id=%s", (user["id"],)
        ))
    dl_row = db_fetchone("SELECT COUNT(*) as cnt FROM download_log")
    total_downloads = dl_row["cnt"] if dl_row else 0

    return render_template("index.html",
        version=GAME_VERSION,
        downloads=total_downloads,
        installer_exists=True,
        installer_size=None,
        user=user,
        reviews=reviews,
        avg_rating=avg_rating,
        user_reviewed=user_reviewed,
    )

@app.route("/download")
@login_required
def download():
    db_execute(
        "INSERT INTO download_log (user_id,ip,user_agent,ts) VALUES (?,?,?,?)",
        (session["user_id"], request.remote_addr, request.user_agent.string[:120], _now())
    )
    db_commit()
    return redirect(INSTALLER_URL)

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
            dup = db_fetchone("SELECT id FROM users WHERE username=%s OR email=%s",
                              (username, email))
            if dup:
                error = "El nombre de usuario o email ya está registrado."
            else:
                salt = secrets.token_hex(16)
                db_execute(
                    "INSERT INTO users (username,email,password,salt,created_at) VALUES (?,?,?,?,?)",
                    (username, email, _hash_pwd(password, salt), salt, _now())
                )
                db_commit()
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
        user = db_fetchone(
            "SELECT * FROM users WHERE (username=%s OR email=%s) AND is_active=1",
            (ident, ident.lower())
        )
        if not user or _hash_pwd(password, user["salt"]) != user["password"]:
            error = "Credenciales incorrectas."
        else:
            session.permanent = True
            app.permanent_session_lifetime = timedelta(days=7)
            session["user_id"]  = user["id"]
            session["username"] = user["username"]
            session["is_admin"] = bool(user["is_admin"])
            db_execute("UPDATE users SET last_login=%s WHERE id=%s", (_now(), user["id"]))
            db_commit()
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
        user  = db_fetchone("SELECT * FROM users WHERE email=%s", (email,))
        if user:
            token = secrets.token_urlsafe(32)
            exp   = (datetime.utcnow() + timedelta(hours=1)).isoformat()
            db_execute("INSERT INTO reset_tokens (user_id,token,expires_at) VALUES (?,?,?)",
                       (user["id"], token, exp))
            db_commit()
            ok = send_reset_email(email, token)
            if not ok:
                dev_token = token
        sent = True
    return render_template("forgot.html", sent=sent, dev_token=dev_token)

@app.route("/reset/<token>", methods=["GET","POST"])
def reset(token):
    row = db_fetchone(
        "SELECT * FROM reset_tokens WHERE token=%s AND used=0", (token,)
    )
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
            db_execute("UPDATE users SET password=%s,salt=%s WHERE id=%s",
                       (_hash_pwd(pwd, salt), salt, row["user_id"]))
            db_execute("UPDATE reset_tokens SET used=1 WHERE token=%s", (token,))
            db_commit()
            flash("Contraseña actualizada.", "success")
            return redirect(url_for("login"))
    return render_template("reset.html", invalid=invalid, token=token, error=error)


# ══════════════════════════════════════════════════════════════════════════════
# ROUTES — ADMIN
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/admin")
@admin_required
def admin_dashboard():
    users = db_fetchall(
        "SELECT id,username,email,is_admin,is_active,created_at,last_login FROM users ORDER BY created_at DESC"
    )
    recent_dls = db_fetchall(
        """SELECT d.ts, d.ip, u.username
           FROM download_log d LEFT JOIN users u ON d.user_id=u.id
           ORDER BY d.ts DESC LIMIT 25"""
    )
    bug_reports = db_fetchall(
        """SELECT b.id, b.description, b.created_at, u.username
           FROM bug_reports b LEFT JOIN users u ON b.user_id=u.id
           ORDER BY b.created_at DESC"""
    )
    dl_row = db_fetchone("SELECT COUNT(*) as cnt FROM download_log")
    total_downloads = dl_row["cnt"] if dl_row else 0

    return render_template("admin.html",
        users=users,
        total_users=len(users),
        active_users=sum(1 for u in users if u["is_active"]),
        total_downloads=total_downloads,
        recent_dls=recent_dls,
        version=GAME_VERSION,
        bug_reports=bug_reports,
    )

@app.route("/admin/user/<int:uid>/toggle", methods=["POST"])
@admin_required
def admin_toggle(uid):
    if uid == session["user_id"]:
        flash("No puedes desactivarte a ti mismo.", "error")
    else:
        db_execute("UPDATE users SET is_active = 1 - is_active WHERE id=%s", (uid,))
        db_commit()
    return redirect(url_for("admin_dashboard"))

@app.route("/admin/user/<int:uid>/delete", methods=["POST"])
@admin_required
def admin_delete(uid):
    if uid == session["user_id"]:
        flash("No puedes eliminarte a ti mismo.", "error")
    else:
        db_execute("DELETE FROM users WHERE id=%s", (uid,))
        db_execute("DELETE FROM reset_tokens WHERE user_id=%s", (uid,))
        db_commit()
    return redirect(url_for("admin_dashboard"))

@app.route("/admin/user/<int:uid>/toggle_admin", methods=["POST"])
@admin_required
def admin_toggle_admin(uid):
    if uid != session["user_id"]:
        db_execute("UPDATE users SET is_admin = 1 - is_admin WHERE id=%s", (uid,))
        db_commit()
    return redirect(url_for("admin_dashboard"))

@app.route("/api/stats")
@admin_required
def api_stats():
    dl_row   = db_fetchone("SELECT COUNT(*) as cnt FROM download_log")
    user_row = db_fetchone("SELECT COUNT(*) as cnt FROM users")
    active_row = db_fetchone("SELECT COUNT(*) as cnt FROM users WHERE is_active=1")
    return jsonify({
        "total_downloads": dl_row["cnt"] if dl_row else 0,
        "total_users":     user_row["cnt"] if user_row else 0,
        "active_users":    active_row["cnt"] if active_row else 0,
    })

@app.route("/api/version")
def api_version():
    return jsonify({"version": GAME_VERSION, "available": True})


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
    existing = db_fetchone(
        "SELECT id FROM reviews WHERE user_id=%s", (session["user_id"],)
    )
    if existing:
        flash("Ya has enviado una reseña. Solo se permite una por usuario.", "error")
        return redirect(url_for("index") + "#resenas")
    # Filtro de contenido con Gemini
    clean, reason = review_is_clean(body)
    if not clean:
        flash(f"Tu reseña no ha podido publicarse: {reason}", "error")
        return redirect(url_for("index") + "#resenas")
    db_execute(
        "INSERT INTO reviews (user_id, rating, body, created_at) VALUES (?,?,?,?)",
        (session["user_id"], int(rating), body, _now())
    )
    db_commit()
    flash("Reseña enviada. Estará visible tras ser aprobada.", "success")
    return redirect(url_for("index") + "#resenas")


@app.route("/admin/reviews")
@admin_required
def admin_reviews():
    pending = db_fetchall(
        """SELECT r.*, u.username FROM reviews r
           JOIN users u ON r.user_id=u.id
           WHERE r.approved=0 ORDER BY r.created_at DESC"""
    )
    approved = db_fetchall(
        """SELECT r.*, u.username FROM reviews r
           JOIN users u ON r.user_id=u.id
           WHERE r.approved=1 ORDER BY r.created_at DESC"""
    )
    return render_template("admin_reviews.html",
                           pending=pending, approved=approved)

@app.route("/admin/reviews/<int:rid>/approve", methods=["POST"])
@admin_required
def review_approve(rid):
    db_execute("UPDATE reviews SET approved=1 WHERE id=%s", (rid,))
    db_commit()
    return redirect(url_for("admin_reviews"))

@app.route("/admin/reviews/<int:rid>/delete", methods=["POST"])
@admin_required
def review_delete(rid):
    db_execute("DELETE FROM reviews WHERE id=%s", (rid,))
    db_commit()
    return redirect(url_for("admin_reviews"))


# ══════════════════════════════════════════════════════════════════════════════
# PROFILE
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/profile")
@login_required
def profile():
    user    = db_fetchone("SELECT * FROM users WHERE id=%s", (session["user_id"],))
    reviews = db_fetchall(
        "SELECT * FROM reviews WHERE user_id=%s ORDER BY created_at DESC",
        (session["user_id"],)
    )
    dl_row  = db_fetchone(
        "SELECT COUNT(*) as cnt FROM download_log WHERE user_id=%s",
        (session["user_id"],)
    )
    dl_count = dl_row["cnt"] if dl_row else 0
    return render_template("profile.html", user=user, reviews=reviews, dl_count=dl_count)


@app.route("/profile/edit", methods=["GET","POST"])
@login_required
def profile_edit():
    user  = db_fetchone("SELECT * FROM users WHERE id=%s", (session["user_id"],))
    error = None
    if request.method == "POST":
        action = request.form.get("action")

        if action == "email":
            new_email = request.form.get("email","").strip().lower()
            if not new_email:
                error = "El email no puede estar vacío."
            else:
                dup = db_fetchone(
                    "SELECT id FROM users WHERE email=%s AND id!=%s",
                    (new_email, session["user_id"])
                )
                if dup:
                    error = "Ese email ya está en uso."
                else:
                    db_execute("UPDATE users SET email=%s WHERE id=%s",
                               (new_email, session["user_id"]))
                    db_commit()
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
                db_execute("UPDATE users SET password=%s,salt=%s WHERE id=%s",
                           (_hash_pwd(new_pwd, salt), salt, session["user_id"]))
                db_commit()
                flash("Contraseña actualizada correctamente.", "success")
                return redirect(url_for("profile"))

        elif action == "avatar":
            avatar_url = request.form.get("avatar_url","").strip()
            db_execute("UPDATE users SET avatar_url=%s WHERE id=%s",
                       (avatar_url or None, session["user_id"]))
            db_commit()
            flash("Avatar actualizado.", "success")
            return redirect(url_for("profile"))

        user = db_fetchone("SELECT * FROM users WHERE id=%s", (session["user_id"],))

    return render_template("profile_edit.html", user=user, error=error)


# ══════════════════════════════════════════════════════════════════════════════
# ADMIN — CHART DATA
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/api/admin/downloads_chart")
@admin_required
def api_downloads_chart():
    rows = db_fetchall(
        """SELECT substr(ts,1,10) as day, COUNT(*) as cnt
           FROM download_log
           GROUP BY day
           ORDER BY day ASC
           LIMIT 30"""
    )
    return jsonify({
        "labels": [r["day"] for r in rows],
        "data":   [r["cnt"] for r in rows],
    })


# ══════════════════════════════════════════════════════════════════════════════
# BUG REPORTS
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/api/bug", methods=["POST"])
def api_bug():
    data = request.get_json(silent=True) or {}
    description = data.get("description", "").strip()
    if not description or len(description) < 5:
        return jsonify({"ok": False, "error": "Descripción demasiado corta."}), 400
    if len(description) > 2000:
        return jsonify({"ok": False, "error": "Descripción demasiado larga."}), 400
    user_id = session.get("user_id")
    db_execute(
        "INSERT INTO bug_reports (user_id, description, created_at) VALUES (?,?,?)",
        (user_id, description, _now())
    )
    db_commit()
    return jsonify({"ok": True})

@app.route("/admin/bug/<int:bid>/delete", methods=["POST"])
@admin_required
def admin_bug_delete(bid):
    db_execute("DELETE FROM bug_reports WHERE id=%s", (bid,))
    db_commit()
    return redirect(url_for("admin_dashboard"))


# ══════════════════════════════════════════════════════════════════════════════
# CHATBOT
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/api/chat", methods=["POST"])
def api_chat():
    if not OPENROUTER_API_KEY:
        return jsonify({"error": "Chatbot no configurado."}), 503

    data = request.get_json(silent=True) or {}
    messages = data.get("messages", [])
    if not messages or not isinstance(messages, list):
        return jsonify({"error": "Petición inválida."}), 400

    messages = messages[-10:]

    system_prompt = """Eres el asistente de soporte oficial de Reconquest, un videojuego RTS gratuito.

Información clave:
- Género: Estrategia en tiempo real (RTS), un jugador
- Ambientación: Ucronía histórica en Portugal — la Revolución de los Claveles de 1974 fracasó y desencadena una guerra civil ficticia
- El jugador conquista fábricas para obtener recursos, gestiona tropas y captura sectores hasta destruir la base enemiga
- Completamente GRATUITO. Requiere registro para descargar
- Solo disponible para Windows 10/11 (64-bit)
- Requisitos: 4 GB RAM, DirectX 11, ~500 MB de disco
- Si Windows SmartScreen alerta, es un falso positivo. Clic en "Más información → Ejecutar de todas formas"
- Desarrollado con Unity 6 y C# como Trabajo de Fin de Grado
- No tiene multijugador
- Para recuperar contraseña: ir a /forgot en la web

Responde siempre en español, de forma concisa y amigable. Si no sabes algo, di que contacten con el desarrollador. No inventes información."""

    or_messages = [{"role": "system", "content": system_prompt}]
    for msg in messages:
        or_messages.append({"role": msg.get("role", "user"), "content": msg.get("content", "")})

    payload = json.dumps({
        "model": "mistralai/mistral-7b-instruct:free",
        "messages": or_messages,
        "max_tokens": 300
    }).encode("utf-8")

    req = urllib.request.Request(
        "https://openrouter.ai/api/v1/chat/completions",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {OPENROUTER_API_KEY}"
        },
        method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            reply = result["choices"][0]["message"]["content"]
            return jsonify({"reply": reply})
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8")
        app.logger.error("OpenRouter API error: %s %s", e.code, body)
        return jsonify({"error": f"Error {e.code}: {e.read().decode()}"}), 502
    except Exception as e:
        app.logger.error("Chat error: %s", e)
        return jsonify({"error": f"Error interno: {str(e)}"}), 500



if __name__ == "__main__":
    init_db()
    app.run(debug=True, host="0.0.0.0", port=5000)