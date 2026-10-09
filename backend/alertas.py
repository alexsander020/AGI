"""Alertas por e-mail (SMTP) e WhatsApp (Twilio). Configuração por variáveis de ambiente.

Regras (limiares ajustáveis por env):
  SOLO_SECO     média das últimas 3 leituras de umidade < limite do dispositivo (padrão ALERTA_UMID_MIN)
  NPK_BAIXO     média das últimas 5 leituras válidas de N/P/K abaixo de ALERTA_N_MIN / P_MIN / K_MIN
  NPK_FALHA     10 leituras seguidas sem NPK (Modbus com problema)
  OFFLINE       sem dados há mais de ALERTA_OFFLINE_MIN (5 min), ou "offline" no tópico de status
Cada tipo, por dispositivo, só dispara de novo após ALERTA_COOLDOWN_MIN (60 min).
"""
import json, os, smtplib, sqlite3, threading, time, urllib.parse, urllib.request, base64
from email.message import EmailMessage

def _f(nome, padrao): return float(os.getenv(nome, padrao))

UMID_MIN = _f("ALERTA_UMID_MIN", 30)
NPK_MIN = {"n": _f("ALERTA_N_MIN", 20), "p": _f("ALERTA_P_MIN", 10), "k": _f("ALERTA_K_MIN", 60)}  # mg/kg: AJUSTE por cultura
COOLDOWN = _f("ALERTA_COOLDOWN_MIN", 60) * 60
OFFLINE_S = _f("ALERTA_OFFLINE_MIN", 5) * 60

SMTP_HOST = os.getenv("SMTP_HOST", ""); SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", ""); SMTP_PASS = os.getenv("SMTP_PASS", "")
EMAIL_TO = [e.strip() for e in os.getenv("ALERT_EMAIL_TO", "").split(",") if e.strip()]
TW_SID = os.getenv("TWILIO_SID", ""); TW_TOKEN = os.getenv("TWILIO_TOKEN", "")
TW_FROM = os.getenv("TWILIO_WHATSAPP_FROM", "")      # ex.: whatsapp:+14155238886
WA_TO = [n.strip() for n in os.getenv("ALERT_WHATSAPP_TO", "").split(",") if n.strip()]  # ex.: +5511999999999

def enviar_email(assunto, texto, para):
    if not (SMTP_HOST and para): return
    m = EmailMessage(); m["Subject"] = assunto; m["From"] = SMTP_USER or "alertas@solo"; m["To"] = ", ".join(para)
    m.set_content(texto)
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as s:
        s.starttls()
        if SMTP_USER: s.login(SMTP_USER, SMTP_PASS)
        s.send_message(m)

def enviar_whatsapp(texto, para):
    if not (TW_SID and TW_TOKEN and TW_FROM and para): return
    url = f"https://api.twilio.com/2010-04-01/Accounts/{TW_SID}/Messages.json"
    auth = base64.b64encode(f"{TW_SID}:{TW_TOKEN}".encode()).decode()
    for num in para:
        corpo = urllib.parse.urlencode({"From": TW_FROM, "To": f"whatsapp:{num}", "Body": texto}).encode()
        req = urllib.request.Request(url, corpo, {"Authorization": f"Basic {auth}"})
        urllib.request.urlopen(req, timeout=15).read()

def _lista(txt): return [x.strip() for x in (txt or "").split(",") if x.strip()]

def contexto_dispositivo(conn_fn, device):
    """Nome amigável e destinatários DO CLIENTE dono do dispositivo (fallback: lista global do operador)."""
    with conn_fn() as c:
        r = c.execute("""SELECT d.nome, f.nome AS fazenda, cl.email_alerta, cl.whatsapp_alerta FROM dispositivos d
                         JOIN fazendas f ON f.id=d.fazenda_id JOIN clientes cl ON cl.id=f.cliente_id
                         WHERE d.device=?""", (device,)).fetchone()
    if not r: return device, EMAIL_TO, WA_TO
    return f"{r['fazenda']} / {r['nome']}", _lista(r["email_alerta"]) or EMAIL_TO, _lista(r["whatsapp_alerta"]) or WA_TO

def _despachar(assunto, texto, emails=None, whats=None):
    """Cada canal é independente: falha de um não impede o outro."""
    emails = EMAIL_TO if emails is None else emails
    whats = WA_TO if whats is None else whats
    for nome, fn in (("email", lambda: enviar_email(assunto, texto, emails)), ("whatsapp", lambda: enviar_whatsapp(f"{assunto}\n{texto}", whats))):
        try: fn()
        except Exception as e: print(f"alerta via {nome} falhou:", e)

def disparar(conn_fn, device, tipo, texto):
    """Respeita o cooldown (persistido no banco, sobrevive a reinício), grava histórico e envia em thread."""
    agora = int(time.time())
    with conn_fn() as c:
        ult = c.execute("SELECT MAX(ts) FROM alertas WHERE device=? AND tipo=?", (device, tipo)).fetchone()[0]
        if ult and agora - ult < COOLDOWN: return False
        c.execute("INSERT INTO alertas (ts, device, tipo, msg) VALUES (?,?,?,?)", (agora, device, tipo, texto))
    rotulo, emails, whats = contexto_dispositivo(conn_fn, device)
    threading.Thread(target=_despachar, args=(f"[Solo] {tipo} - {rotulo}", texto, emails, whats), daemon=True).start()
    return True

def limites_do(conn_fn, device):
    """Limiares efetivos do dispositivo: valores do cliente, com fallback nos padrões globais (env)."""
    lim = {"umid_min": UMID_MIN, "n_min": NPK_MIN["n"], "p_min": NPK_MIN["p"], "k_min": NPK_MIN["k"]}
    with conn_fn() as c:
        r = c.execute("SELECT umid_min, n_min, p_min, k_min FROM limites WHERE device=?", (device,)).fetchone()
    if r:
        for k in lim:
            if r[k] is not None: lim[k] = r[k]
    return lim

def avaliar(conn_fn, device):
    """Chamada após cada leitura gravada."""
    with conn_fn() as c:
        ult = c.execute("SELECT umid_media, npk_ok FROM leituras WHERE device=? ORDER BY id DESC LIMIT 10", (device,)).fetchall()
        validas = c.execute("SELECT n, p, k FROM leituras WHERE device=? AND npk_ok=1 ORDER BY id DESC LIMIT 5", (device,)).fetchall()
    lim = limites_do(conn_fn, device)
    if len(ult) >= 3 and lim["umid_min"] > 0:
        m = sum(r["umid_media"] for r in ult[:3]) / 3
        if m < lim["umid_min"]:
            disparar(conn_fn, device, "SOLO_SECO", f"Umidade média em {m:.0f}% (mínimo {lim['umid_min']:.0f}%). Verifique a irrigação.")
    if len(validas) == 5:
        baixos = []
        for k in ("n", "p", "k"):
            minimo = lim[k + "_min"]
            if minimo <= 0: continue                      # 0 = alerta desativado para esse nutriente
            media = sum(r[k] for r in validas) / 5
            if media < minimo: baixos.append(f"{k.upper()}={media:.0f} (mín. {minimo:.0f})")
        if baixos:
            disparar(conn_fn, device, "NPK_BAIXO", "Nutrientes abaixo do mínimo: " + ", ".join(baixos) + " mg/kg.")
    if len(ult) == 10 and all(r["npk_ok"] == 0 for r in ult):
        disparar(conn_fn, device, "NPK_FALHA", "O sensor NPK não responde há 10 leituras. Verifique cabo RS485, alimentação e endereço Modbus.")

def vigiar_offline(conn_fn):
    """Loop em thread: avisa dispositivos que pararam de enviar."""
    while True:
        time.sleep(60)
        try:
            with conn_fn() as c:
                rows = c.execute("SELECT l.device, MAX(l.ts) AS ts FROM leituras l JOIN dispositivos d ON d.device=l.device GROUP BY l.device").fetchall()
            for r in rows:
                if time.time() - r["ts"] > OFFLINE_S:
                    disparar(conn_fn, r["device"], "OFFLINE", f"Sem dados há mais de {OFFLINE_S/60:.0f} minutos.")
        except Exception as e:
            print("vigia offline:", e)
