"""
App de Prazos Moodle SENAC
Execute: python app.py
Acesse: http://localhost:5000
"""

from flask import Flask, render_template, request, redirect, url_for, flash, session
import sqlite3, os, requests
from datetime import date, datetime, timedelta
from icalendar import Calendar
from apscheduler.schedulers.background import BackgroundScheduler

app = Flask(__name__)
app.secret_key = "senac-prazos-2026"

BOT_TOKEN = "8712010743:AAHuT7LNZ2EUnGyj71f3oWN6VCJi2QlZJvI"
DB_PATH   = os.path.join(os.path.dirname(__file__), "prazos.db")
AVISO_DIAS = 3

# ── Banco de dados ─────────────────────────────────────────────────────────

def get_db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con

def init_db():
    with get_db() as con:
        con.executescript("""
            CREATE TABLE IF NOT EXISTS usuarios (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                nome       TEXT    NOT NULL,
                ical_url   TEXT    NOT NULL,
                chat_id    TEXT    NOT NULL UNIQUE,
                criado_em  TEXT    DEFAULT (date('now'))
            );
            CREATE TABLE IF NOT EXISTS prazos (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                usuario_id   INTEGER NOT NULL,
                data         TEXT    NOT NULL,
                titulo       TEXT    NOT NULL,
                curso        TEXT,
                atualizado_em TEXT   DEFAULT (datetime('now')),
                FOREIGN KEY (usuario_id) REFERENCES usuarios(id)
            );
        """)

# ── Telegram ───────────────────────────────────────────────────────────────

def send_telegram(chat_id, text):
    try:
        requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
            timeout=10
        )
    except Exception as e:
        print(f"Telegram erro: {e}")

# ── iCal / Moodle ──────────────────────────────────────────────────────────

def sincronizar_ical(usuario_id, ical_url):
    try:
        resp = requests.get(ical_url, timeout=15)
        resp.raise_for_status()
        cal  = Calendar.from_ical(resp.content)
        hoje = date.today()
        novos = []
        for comp in cal.walk():
            if comp.name != "VEVENT":
                continue
            dtstart = comp.get("DTSTART")
            if not dtstart:
                continue
            dt = dtstart.dt
            if hasattr(dt, "date"):
                dt = dt.date()
            if dt < hoje:
                continue
            titulo = str(comp.get("SUMMARY", "Sem título"))
            # Extrair nome do curso da descrição/URL
            desc   = str(comp.get("DESCRIPTION", ""))
            curso  = ""
            for line in desc.splitlines():
                if "course" in line.lower() or "disciplina" in line.lower():
                    curso = line.strip()[:80]
                    break
            novos.append((usuario_id, dt.isoformat(), titulo, curso))

        with get_db() as con:
            con.execute("DELETE FROM prazos WHERE usuario_id=?", (usuario_id,))
            con.executemany(
                "INSERT INTO prazos (usuario_id, data, titulo, curso) VALUES (?,?,?,?)",
                novos
            )
        return len(novos), None
    except Exception as e:
        return 0, str(e)

# ── Notificações diárias ───────────────────────────────────────────────────

def verificar_prazos():
    hoje  = date.today()
    limite = hoje + timedelta(days=AVISO_DIAS)
    with get_db() as con:
        usuarios = con.execute("SELECT * FROM usuarios").fetchall()
        for u in usuarios:
            prazos = con.execute(
                "SELECT * FROM prazos WHERE usuario_id=? AND data BETWEEN ? AND ? ORDER BY data",
                (u["id"], hoje.isoformat(), limite.isoformat())
            ).fetchall()
            if not prazos:
                continue
            linhas = [f"📚 <b>PRAZOS MOODLE SENAC</b> — {hoje.strftime('%d/%m/%Y')}\n"]
            for p in prazos:
                dt   = date.fromisoformat(p["data"])
                diff = (dt - hoje).days
                if diff == 0:   emoji, label = "🔴", "HOJE"
                elif diff == 1: emoji, label = "🟠", "AMANHÃ"
                else:           emoji, label = "🟡", f"em {diff} dias ({dt.strftime('%d/%m')})"
                curso = f"\n   <i>{p['curso']}</i>" if p["curso"] else ""
                linhas.append(f"{emoji} <b>{label}</b> — {p['titulo']}{curso}")
            linhas.append(f"\n🎓 Bons estudos, {u['nome'].split()[0]}!")
            send_telegram(u["chat_id"], "\n".join(linhas))

# ── Rotas ──────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    with get_db() as con:
        total = con.execute("SELECT COUNT(*) FROM usuarios").fetchone()[0]
    return render_template("index.html", total=total)

@app.route("/cadastrar", methods=["POST"])
def cadastrar():
    nome     = request.form["nome"].strip()
    chat_id  = request.form["chat_id"].strip()
    ical_url = request.form["ical_url"].strip()
    if not nome or not chat_id or not ical_url:
        flash("Preencha todos os campos.", "danger")
        return redirect(url_for("index"))
    try:
        with get_db() as con:
            con.execute(
                "INSERT INTO usuarios (nome, chat_id, ical_url) VALUES (?,?,?)",
                (nome, chat_id, ical_url)
            )
            uid = con.execute("SELECT id FROM usuarios WHERE chat_id=?", (chat_id,)).fetchone()["id"]
        qtd, erro = sincronizar_ical(uid, ical_url)
        if erro:
            flash(f"Cadastrado! Mas erro ao sincronizar: {erro}", "warning")
        else:
            flash(f"Bem-vindo, {nome}! {qtd} prazos importados.", "success")
            send_telegram(chat_id,
                f"✅ <b>Cadastro confirmado!</b>\n\nOlá, {nome.split()[0]}! "
                f"Você receberá alertas aqui quando prazos estiverem chegando. 🎓"
            )
        session["usuario_id"] = uid
        return redirect(url_for("dashboard", uid=uid))
    except sqlite3.IntegrityError:
        flash("Este Telegram Chat ID já está cadastrado.", "warning")
        with get_db() as con:
            uid = con.execute("SELECT id FROM usuarios WHERE chat_id=?", (chat_id,)).fetchone()
            if uid:
                session["usuario_id"] = uid["id"]
                return redirect(url_for("dashboard", uid=uid["id"]))
        return redirect(url_for("index"))

@app.route("/dashboard/<int:uid>")
def dashboard(uid):
    with get_db() as con:
        usuario = con.execute("SELECT * FROM usuarios WHERE id=?", (uid,)).fetchone()
        if not usuario:
            flash("Usuário não encontrado.", "danger")
            return redirect(url_for("index"))
        hoje  = date.today()
        prazos = con.execute(
            "SELECT * FROM prazos WHERE usuario_id=? AND data >= ? ORDER BY data",
            (uid, hoje.isoformat())
        ).fetchall()

    prazos_enriquecidos = []
    for p in prazos:
        dt   = date.fromisoformat(p["data"])
        diff = (dt - hoje).days
        if diff == 0:   urgencia, badge = "hoje",    "danger"
        elif diff <= 3: urgencia, badge = "urgente", "warning"
        elif diff <= 7: urgencia, badge = "semana",  "info"
        else:           urgencia, badge = "normal",  "secondary"
        prazos_enriquecidos.append({
            "data":     dt.strftime("%d/%m/%Y"),
            "titulo":   p["titulo"],
            "curso":    p["curso"],
            "diff":     diff,
            "urgencia": urgencia,
            "badge":    badge
        })
    return render_template("dashboard.html", usuario=usuario, prazos=prazos_enriquecidos, uid=uid)

@app.route("/sincronizar/<int:uid>", methods=["POST"])
def sincronizar(uid):
    with get_db() as con:
        u = con.execute("SELECT * FROM usuarios WHERE id=?", (uid,)).fetchone()
    if not u:
        flash("Usuário não encontrado.", "danger")
        return redirect(url_for("index"))
    qtd, erro = sincronizar_ical(uid, u["ical_url"])
    if erro:
        flash(f"Erro ao sincronizar: {erro}", "danger")
    else:
        flash(f"{qtd} prazos atualizados com sucesso!", "success")
    return redirect(url_for("dashboard", uid=uid))

@app.route("/testar/<int:uid>", methods=["POST"])
def testar_telegram(uid):
    with get_db() as con:
        u = con.execute("SELECT * FROM usuarios WHERE id=?", (uid,)).fetchone()
    if u:
        send_telegram(u["chat_id"],
            f"🔔 <b>Teste de notificação</b>\n\nOlá, {u['nome'].split()[0]}! "
            f"Suas notificações estão funcionando. 🎓"
        )
        flash("Mensagem de teste enviada no Telegram!", "success")
    return redirect(url_for("dashboard", uid=uid))

# ── Inicialização ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    init_db()
    scheduler = BackgroundScheduler()
    scheduler.add_job(verificar_prazos, "cron", hour=8, minute=0)
    scheduler.start()
    port = int(os.environ.get("PORT", 5000))
    print("=" * 50)
    print(f"  App Prazos SENAC rodando na porta {port}!")
    print("=" * 50)
    app.run(debug=False, host="0.0.0.0", port=port)
