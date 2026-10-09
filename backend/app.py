"""Backend multi-cliente do monitor de solo.
Cliente -> Fazendas -> Dispositivos (ESP32). Cada cliente só enxerga os próprios dispositivos.
Entrada de dados: MQTT (principal) e POST /api/leitura (testes; exige API_KEY).
"""
import json, os, re, secrets, sqlite3, threading, time
from collections import defaultdict, deque
from contextlib import contextmanager
from functools import wraps

import click
from flask import Flask, abort, flash, jsonify, redirect, render_template, request, session, url_for
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash

import alertas

DB_PATH = os.getenv("DB_PATH", "solo.db")
MQTT_HOST = os.getenv("MQTT_HOST", "")
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))
MQTT_TOPIC = os.getenv("MQTT_TOPIC", "solo/+/leitura")
MQTT_STATUS = os.getenv("MQTT_STATUS", "solo/+/status")
MQTT_USER = os.getenv("MQTT_USER", ""); MQTT_PASS = os.getenv("MQTT_PASS", "")
MQTT_CA = os.getenv("MQTT_CA", "")
API_KEY = os.getenv("API_KEY", "")
SECRET_KEY = os.getenv("SECRET_KEY", "")
if not SECRET_KEY:
    print("AVISO: defina SECRET_KEY; usando chave temporária (sessões caem a cada reinício).")
    SECRET_KEY = secrets.token_hex(32)

CAMPOS = ["umid_media", "n", "p", "k", "ph", "ec", "temp", "umid_sensor", "valvula", "npk_ok"]
ID_RE = re.compile(r"^[a-z0-9_-]{3,40}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)   # atrás do Caddy
app.config.update(SECRET_KEY=SECRET_KEY, SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                  SESSION_COOKIE_SECURE=os.getenv("COOKIE_SECURE", "1") == "1",
                  PERMANENT_SESSION_LIFETIME=8 * 3600)

# ---------- Banco ----------
@contextmanager
def db():
    c = sqlite3.connect(DB_PATH, timeout=10)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL"); c.execute("PRAGMA foreign_keys=ON")
    try:
        with c:
            yield c
    finally:
        c.close()

def init_db():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS clientes (id INTEGER PRIMARY KEY, nome TEXT NOT NULL,
            email_alerta TEXT DEFAULT '', whatsapp_alerta TEXT DEFAULT '');
        CREATE TABLE IF NOT EXISTS usuarios (id INTEGER PRIMARY KEY, email TEXT UNIQUE NOT NULL,
            senha_hash TEXT NOT NULL, papel TEXT NOT NULL CHECK (papel IN ('admin','cliente')),
            cliente_id INTEGER REFERENCES clientes(id) ON DELETE CASCADE);
        CREATE TABLE IF NOT EXISTS fazendas (id INTEGER PRIMARY KEY, nome TEXT NOT NULL,
            cliente_id INTEGER NOT NULL REFERENCES clientes(id) ON DELETE CASCADE);
        CREATE TABLE IF NOT EXISTS dispositivos (id INTEGER PRIMARY KEY, device TEXT UNIQUE NOT NULL,
            zona TEXT UNIQUE NOT NULL, nome TEXT NOT NULL,
            fazenda_id INTEGER NOT NULL REFERENCES fazendas(id) ON DELETE CASCADE);
        CREATE TABLE IF NOT EXISTS leituras (id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL,
            device TEXT NOT NULL, z1 INTEGER, z2 INTEGER, z3 INTEGER, solo TEXT, umid_media REAL,
            n REAL, p REAL, k REAL, ph REAL, ec REAL, temp REAL, umid_sensor REAL, valvula INTEGER, npk_ok INTEGER);
        CREATE INDEX IF NOT EXISTS idx_ts ON leituras(device, ts);
        CREATE TABLE IF NOT EXISTS limites (device TEXT PRIMARY KEY REFERENCES dispositivos(device) ON DELETE CASCADE,
            umid_min REAL, n_min REAL, p_min REAL, k_min REAL);
        CREATE TABLE IF NOT EXISTS alertas (id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL,
            device TEXT NOT NULL, tipo TEXT NOT NULL, msg TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS idx_alertas ON alertas(device, tipo, ts);
        """)

def salvar(d: dict, topic: str | None = None):
    """Grava só de dispositivo cadastrado. Via MQTT, o tópico precisa bater com a zona do cadastro
    (impede que o ESP32 de um cliente publique como se fosse o de outro)."""
    z = d.get("z")
    dev = str(d.get("id", ""))[:64]
    if not isinstance(z, list) or len(z) != 3 or not dev:
        raise ValueError("payload inválido")
    with db() as c:
        reg = c.execute("SELECT zona FROM dispositivos WHERE device=?", (dev,)).fetchone()
    if not reg:
        raise LookupError("dispositivo não cadastrado")
    if topic is not None:
        partes = topic.split("/")
        if len(partes) != 3 or partes[1] != reg["zona"]:
            raise PermissionError("tópico não corresponde à zona do dispositivo")
    vals = {c: (float(d[c]) if d.get(c) is not None else None) for c in CAMPOS}
    if not vals["npk_ok"]:
        for c in ("n", "p", "k", "ph", "ec", "temp", "umid_sensor"):
            vals[c] = None
    with db() as c:
        c.execute("""INSERT INTO leituras (ts, device, z1, z2, z3, solo, umid_media, n, p, k, ph, ec,
                     temp, umid_sensor, valvula, npk_ok) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                  [int(time.time()), dev, *z, str(d.get("solo", ""))[:10], *[vals[c] for c in CAMPOS]])
    alertas.avaliar(db, dev)

# ---------- MQTT ----------
def iniciar_mqtt():
    if not MQTT_HOST:
        return
    import paho.mqtt.client as mqtt
    cli = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    if MQTT_USER: cli.username_pw_set(MQTT_USER, MQTT_PASS)
    if MQTT_CA: cli.tls_set(ca_certs=MQTT_CA)
    cli.on_connect = lambda c, u, f, rc, p=None: (c.subscribe(MQTT_TOPIC), c.subscribe(MQTT_STATUS))
    def on_message(c, u, msg):
        try:
            if msg.topic.endswith("/status"):
                if msg.payload == b"offline":
                    zona = msg.topic.split("/")[1]
                    with db() as cx:
                        r = cx.execute("SELECT device FROM dispositivos WHERE zona=?", (zona,)).fetchone()
                    if r: alertas.disparar(db, r["device"], "OFFLINE", "O dispositivo perdeu a conexão com o broker.")
                return
            salvar(json.loads(msg.payload), topic=msg.topic)
        except Exception as e:
            print("mensagem MQTT ignorada:", type(e).__name__, e)
    cli.on_message = on_message
    cli.reconnect_delay_set(1, 30)
    cli.connect_async(MQTT_HOST, MQTT_PORT)
    cli.loop_start()

# ---------- Autenticação ----------
_falhas = defaultdict(deque)
_DUMMY = generate_password_hash("x")

@app.context_processor
def _ctx():
    def csrf_token():
        if "csrf" not in session: session["csrf"] = secrets.token_urlsafe(32)
        return session["csrf"]
    return {"csrf_token": csrf_token}

@app.before_request
def _csrf():
    if request.method == "POST" and request.endpoint != "ingest":
        tok = request.form.get("csrf") or request.headers.get("X-CSRF", "")
        if not tok or not secrets.compare_digest(tok, session.get("csrf", "")):
            abort(400)

def usuario_atual():
    uid = session.get("uid")
    if not uid: return None
    with db() as c:
        return c.execute("SELECT * FROM usuarios WHERE id=?", (uid,)).fetchone()

def login_required(f):
    @wraps(f)
    def w(*a, **k):
        u = usuario_atual()
        if not u:
            if request.path.startswith("/api/"): return jsonify(erro="não autenticado"), 401
            return redirect(url_for("login"))
        return f(u, *a, **k)
    return w

def admin_required(f):
    @wraps(f)
    @login_required
    def w(u, *a, **k):
        if u["papel"] != "admin": abort(403)
        return f(u, *a, **k)
    return w

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        ip = request.remote_addr; agora = time.time(); fila = _falhas[ip]
        while fila and agora - fila[0] > 300: fila.popleft()
        if len(fila) >= 5:
            return render_template("login.html", erro="Muitas tentativas. Aguarde 5 minutos."), 429
        email = request.form.get("email", "").strip().lower()
        with db() as c:
            u = c.execute("SELECT * FROM usuarios WHERE email=?", (email,)).fetchone()
        ok = check_password_hash(u["senha_hash"] if u else _DUMMY, request.form.get("senha", ""))
        if u and ok:
            session.clear(); session["uid"] = u["id"]; session.permanent = True
            return redirect(url_for("dashboard"))
        fila.append(agora)
        return render_template("login.html", erro="E-mail ou senha incorretos."), 401
    return render_template("login.html")

@app.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

# ---------- Escopo por cliente ----------
def dispositivos_visiveis(u):
    sql = """SELECT d.device, d.nome, d.zona, f.nome AS fazenda, cl.nome AS cliente
             FROM dispositivos d JOIN fazendas f ON f.id=d.fazenda_id JOIN clientes cl ON cl.id=f.cliente_id"""
    with db() as c:
        if u["papel"] == "admin":
            rows = c.execute(sql + " ORDER BY cl.nome, f.nome, d.nome").fetchall()
        else:
            rows = c.execute(sql + " WHERE cl.id=? ORDER BY f.nome, d.nome", (u["cliente_id"],)).fetchall()
    return [dict(r) for r in rows]

def device_permitido(u, device):
    if device not in {d["device"] for d in dispositivos_visiveis(u)}:
        abort(404)      # 404 (e não 403) para não revelar que o dispositivo existe
    return device

# ---------- Páginas ----------
@app.route("/")
@login_required
def dashboard(u):
    return render_template("dashboard.html", usuario=u)

@app.route("/admin")
@admin_required
def admin(u):
    with db() as c:
        clientes = c.execute("SELECT * FROM clientes ORDER BY nome").fetchall()
        fazendas = c.execute("""SELECT f.id, f.nome, cl.nome AS cliente FROM fazendas f
                                JOIN clientes cl ON cl.id=f.cliente_id ORDER BY cl.nome, f.nome""").fetchall()
    return render_template("admin.html", usuario=u, clientes=clientes, fazendas=fazendas,
                           dispositivos=dispositivos_visiveis(u))

def _voltar(msg, cat="erro"):
    flash(msg, cat); return redirect(url_for("admin"))

@app.post("/admin/cliente")
@admin_required
def admin_cliente(u):
    f = request.form
    nome, email, senha = f.get("nome", "").strip(), f.get("email", "").strip().lower(), f.get("senha", "")
    if not nome or not EMAIL_RE.match(email) or len(senha) < 10:
        return _voltar("Informe nome, e-mail válido e senha com 10+ caracteres.")
    try:
        with db() as c:
            cur = c.execute("INSERT INTO clientes (nome, email_alerta, whatsapp_alerta) VALUES (?,?,?)",
                            (nome, f.get("email_alerta", "").strip(), f.get("whatsapp_alerta", "").strip()))
            c.execute("INSERT INTO usuarios (email, senha_hash, papel, cliente_id) VALUES (?,?,?,?)",
                      (email, generate_password_hash(senha), "cliente", cur.lastrowid))
    except sqlite3.IntegrityError:
        return _voltar("Esse e-mail de login já existe.")
    return _voltar(f"Cliente {nome} criado.", "ok")

@app.post("/admin/fazenda")
@admin_required
def admin_fazenda(u):
    f = request.form
    try:
        with db() as c:
            c.execute("INSERT INTO fazendas (nome, cliente_id) VALUES (?,?)",
                      (f.get("nome", "").strip(), int(f.get("cliente_id", 0))))
    except (sqlite3.IntegrityError, ValueError):
        return _voltar("Dados da fazenda inválidos.")
    return _voltar("Fazenda criada.", "ok")

@app.post("/admin/dispositivo")
@admin_required
def admin_dispositivo(u):
    f = request.form
    dev, zona, nome = f.get("device", "").strip(), f.get("zona", "").strip(), f.get("nome", "").strip()
    if not (ID_RE.match(dev) and ID_RE.match(zona) and nome):
        return _voltar("Use a-z, 0-9, _ ou - (3 a 40 caracteres) em dispositivo e zona.")
    try:
        with db() as c:
            c.execute("INSERT INTO dispositivos (device, zona, nome, fazenda_id) VALUES (?,?,?,?)",
                      (dev, zona, nome, int(f.get("fazenda_id", 0))))
    except (sqlite3.IntegrityError, ValueError):
        return _voltar("Dispositivo/zona já existe ou fazenda inválida.")
    return _voltar(f"Dispositivo cadastrado. Crie o usuário MQTT: scripts/novo-usuario.sh {dev} {zona}", "ok")

# ---------- API ----------
@app.route("/api/dispositivos")
@login_required
def api_dispositivos(u):
    return jsonify(dispositivos_visiveis(u))

@app.route("/api/ultima")
@login_required
def ultima(u):
    dev = device_permitido(u, request.args.get("device", ""))
    with db() as c:
        r = c.execute("SELECT * FROM leituras WHERE device=? ORDER BY id DESC LIMIT 1", (dev,)).fetchone()
    if not r: return jsonify(vazio=True)
    d = dict(r); d["atrasada"] = int(time.time()) - d["ts"] > 60
    return jsonify(d)

@app.route("/api/historico")
@login_required
def historico(u):
    dev = device_permitido(u, request.args.get("device", ""))
    horas = min(max(request.args.get("horas", 24, type=float), 0.1), 720)
    with db() as c:
        rows = c.execute("""SELECT ts, umid_media, n, p, k, ph, temp FROM leituras
                            WHERE device=? AND ts >= ? ORDER BY ts LIMIT 5000""",
                         (dev, int(time.time() - horas * 3600))).fetchall()
    return jsonify([dict(r) for r in rows])

@app.route("/api/alertas")
@login_required
def lista_alertas(u):
    devs = [d["device"] for d in dispositivos_visiveis(u)]
    if not devs: return jsonify([])
    with db() as c:
        rows = c.execute(f"SELECT ts, device, tipo, msg FROM alertas WHERE device IN ({','.join('?'*len(devs))}) "
                         "ORDER BY id DESC LIMIT 20", devs).fetchall()
    return jsonify([dict(r) for r in rows])

FAIXAS = {"umid_min": (0, 100), "n_min": (0, 10000), "p_min": (0, 10000), "k_min": (0, 10000)}

def _estado_limites(dev):
    with db() as c:
        r = c.execute("SELECT 1 FROM limites WHERE device=?", (dev,)).fetchone()
    return {"limites": alertas.limites_do(db, dev), "personalizado": bool(r),
            "padrao": alertas.limites_do(db, "")}   # device inexistente => só padrões

@app.route("/api/limites", methods=["GET", "POST"])
@login_required
def api_limites(u):
    if request.method == "GET":
        return jsonify(_estado_limites(device_permitido(u, request.args.get("device", ""))))
    corpo = request.get_json(silent=True) or {}
    dev = device_permitido(u, str(corpo.get("device", "")))
    if corpo.get("restaurar") is True:
        with db() as c: c.execute("DELETE FROM limites WHERE device=?", (dev,))
        return jsonify(_estado_limites(dev))
    novos = {}
    for campo, (lo, hi) in FAIXAS.items():
        v = (corpo.get("limites") or {}).get(campo)
        try:
            v = float(v)
        except (TypeError, ValueError):
            return jsonify(erro=f"{campo}: informe um número"), 400
        if not (lo <= v <= hi) or v != v:
            return jsonify(erro=f"{campo}: valor entre {lo} e {hi}"), 400
        novos[campo] = v
    with db() as c:
        c.execute("""INSERT INTO limites (device, umid_min, n_min, p_min, k_min) VALUES (?,?,?,?,?)
                     ON CONFLICT(device) DO UPDATE SET umid_min=excluded.umid_min, n_min=excluded.n_min,
                     p_min=excluded.p_min, k_min=excluded.k_min""",
                  (dev, novos["umid_min"], novos["n_min"], novos["p_min"], novos["k_min"]))
    return jsonify(_estado_limites(dev))

@app.route("/api/leitura", methods=["POST"])
def ingest():
    if not API_KEY: return jsonify(erro="endpoint desativado"), 403
    if not secrets.compare_digest(request.headers.get("X-API-Key", ""), API_KEY):
        return jsonify(erro="não autorizado"), 401
    try:
        salvar(request.get_json(force=True))
    except LookupError:
        return jsonify(erro="dispositivo não cadastrado"), 404
    except (ValueError, TypeError, KeyError):
        return jsonify(erro="payload inválido"), 400
    return jsonify(ok=True), 201

# ---------- CLI ----------
@app.cli.command("criar-admin")
@click.argument("email")
def criar_admin(email):
    """flask --app app criar-admin voce@empresa.com"""
    senha = click.prompt("Senha (10+ caracteres)", hide_input=True, confirmation_prompt=True)
    if len(senha) < 10: raise click.ClickException("senha curta demais")
    with db() as c:
        c.execute("INSERT INTO usuarios (email, senha_hash, papel) VALUES (?,?, 'admin')",
                  (email.strip().lower(), generate_password_hash(senha)))
    click.echo("Admin criado.")

init_db()
iniciar_mqtt()
threading.Thread(target=alertas.vigiar_offline, args=(db,), daemon=True).start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
