import os
import io
import json
import base64
import secrets
import smtplib
import ssl
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, date
from functools import wraps
from threading import Thread

import psycopg2
import qrcode
import requests
from flask import (
    Flask, render_template, request, redirect, url_for,
    session, flash, g, abort, send_from_directory
)
import bcrypt
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from vokativ import vokativ as _vokativ

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', secrets.token_hex(32))
app.config['UPLOAD_FOLDER'] = os.path.join(os.path.dirname(__file__), 'static', 'uploads')
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB

limiter = Limiter(get_remote_address, app=app, default_limits=[], storage_uri="memory://")

DATABASE_URL = os.environ.get('DATABASE_URL', '')
FIO_API_TOKEN = os.environ.get('FIO_API_TOKEN', '')

os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)



# ── Database abstraction ─────────────────────────────────────────
# Wraps PostgreSQL to use ? placeholders and dict-like row access

class PgCursorWrapper:
    def __init__(self, cursor):
        self._cursor = cursor
        self._desc = cursor.description

    def fetchone(self):
        row = self._cursor.fetchone()
        if row is None or self._desc is None:
            return None
        cols = [d[0] for d in self._desc]
        return DictRow(dict(zip(cols, row)))

    def fetchall(self):
        rows = self._cursor.fetchall()
        if not rows or self._desc is None:
            return []
        cols = [d[0] for d in self._desc]
        return [DictRow(dict(zip(cols, row))) for row in rows]


class DictRow(dict):
    def __getitem__(self, key):
        if isinstance(key, int):
            return list(self.values())[key]
        return super().__getitem__(key)


class PgConnectionWrapper:
    def __init__(self, conn):
        self._conn = conn

    def execute(self, query, params=None):
        query = query.replace('?', '%s')
        query = query.replace('INSERT OR REPLACE INTO site_settings (key, value) VALUES (%s, %s)',
                              'INSERT INTO site_settings (key, value) VALUES (%s, %s) '
                              'ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value')
        cur = self._conn.cursor()
        cur.execute(query, params or ())
        return PgCursorWrapper(cur)

    def commit(self):
        self._conn.commit()

    def close(self):
        self._conn.close()


def _connect_db():
    conn = psycopg2.connect(DATABASE_URL)
    return PgConnectionWrapper(conn)


# ── Database helpers ──────────────────────────────────────────────

def get_db():
    if 'db' not in g:
        g.db = _connect_db()
    return g.db


@app.teardown_appcontext
def close_db(exception):
    db = g.pop('db', None)
    if db is not None:
        db.close()


def init_db():
    db = _connect_db()

    for stmt in [
        '''CREATE TABLE IF NOT EXISTS admins (
            id SERIAL PRIMARY KEY, username TEXT UNIQUE NOT NULL,
            email TEXT, password_hash TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''',
        '''CREATE TABLE IF NOT EXISTS password_reset_tokens (
            id SERIAL PRIMARY KEY, admin_id INTEGER NOT NULL REFERENCES admins(id),
            token TEXT UNIQUE NOT NULL, expires_at TIMESTAMP NOT NULL,
            used INTEGER DEFAULT 0, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''',
        '''CREATE TABLE IF NOT EXISTS site_settings (
            key TEXT PRIMARY KEY, value TEXT)''',
        '''CREATE TABLE IF NOT EXISTS etapy (
            id SERIAL PRIMARY KEY, number INTEGER NOT NULL UNIQUE,
            title TEXT NOT NULL, date TEXT, distance TEXT,
            elevation_up TEXT, elevation_down TEXT, route TEXT,
            waypoints TEXT, description TEXT, map_link TEXT,
            map_download TEXT, profile_image TEXT, qr_image TEXT,
            gpx_file TEXT, youtube_links TEXT, color TEXT DEFAULT '#ffc107')''',
        '''CREATE TABLE IF NOT EXISTS propozice (
            id SERIAL PRIMARY KEY, content TEXT NOT NULL,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''',
        '''CREATE TABLE IF NOT EXISTS ubytovani (
            id SERIAL PRIMARY KEY, etapa_number INTEGER,
            name TEXT NOT NULL, city TEXT, date TEXT,
            rooms_info TEXT, food_info TEXT, link TEXT,
            sort_order INTEGER DEFAULT 0)''',
        '''CREATE TABLE IF NOT EXISTS aktuality (
            id SERIAL PRIMARY KEY, title TEXT NOT NULL,
            content TEXT NOT NULL, published INTEGER DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''',
        '''CREATE TABLE IF NOT EXISTS registrace (
            id SERIAL PRIMARY KEY, name TEXT NOT NULL,
            email TEXT, phone TEXT, note TEXT,
            status TEXT DEFAULT 'pending', admin_note TEXT,
            decided_at TIMESTAMP, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            variable_symbol INTEGER, payment_status TEXT DEFAULT 'none',
            payment_amount REAL, qr_sent_at TIMESTAMP)''',
    ]:
        db.execute(stmt)
    db.commit()

    # Add payment columns to registrace if they don't exist (migration for existing DBs)
    for col_stmt in [
        "ALTER TABLE registrace ADD COLUMN variable_symbol INTEGER",
        "ALTER TABLE registrace ADD COLUMN payment_status TEXT DEFAULT 'none'",
        "ALTER TABLE registrace ADD COLUMN payment_amount REAL",
        "ALTER TABLE registrace ADD COLUMN qr_sent_at TIMESTAMP",
    ]:
        try:
            db.execute(col_stmt)
            db.commit()
        except Exception:
            db._conn.rollback()

    # Create default admins if they don't exist
    for username in ['michal', 'adam']:
        existing = db.execute('SELECT id FROM admins WHERE username = ?', (username,)).fetchone()
        if not existing:
            db.execute('INSERT INTO admins (username) VALUES (?)', (username,))

    # Default site settings (only inserted if not already present)
    defaults = {
        'event_name': 'STAROČESKÁ CYKLOEXPEDICE',
        'event_year': '2025',
        'event_days': '3',
        'event_km': '287',
        'event_elevation': '1 703',
        'event_dates': '11. – 13. září 2025',
        'contact_name_1': 'Michal Přikryl',
        'contact_name_2': 'Adam Přikryl',
        'contact_email': 'info@cykloexpedice.cz',
        'photos_link': 'https://photos.app.goo.gl/xvrkFtWUiQu31a3T9',
        'photos_text': 'Pokud někdo máte ještě fotky, které chcete sdílet s ostatními, nahrajte je prosím do galerie po kliku na tlačítko.',
        'fotky_enabled': '1',
        'payment_amount': '3500',
        'bank_account': '2703473997/2010',
        'bank_iban': 'CZ1620100000002703473997',
        # Email templates
        'email_submitted_subject': 'Přihláška přijata – {{event_name}} {{event_year}}',
        'email_submitted_body': '<h2 style="color: #99b20f; margin: 0 0 20px 0;">Přihláška přijata</h2>'
            '<p>Ahoj <strong>{{name_vocative}}</strong>,</p>'
            '<p>děkujeme za vaši přihlášku na <strong>{{event_name}} {{event_year}}</strong>. '
            'Vaše přihláška byla úspěšně zaregistrována.</p>'
            '<p>Jakmile bude kapacita naplněna, budeme vás kontaktovat s dalšími informacemi.</p>'
            '<p style="margin-top: 24px;">S pozdravem,<br>{{contact_name_1}} & {{contact_name_2}}</p>',
        'email_approved_subject': 'Přihláška schválena – {{event_name}} {{event_year}}',
        'email_approved_body': '<h2 style="color: #99b20f; margin: 0 0 20px 0;">Vaše přihláška byla schválena!</h2>'
            '<p>Ahoj <strong>{{name_vocative}}</strong>,</p>'
            '<p>s radostí vám oznamujeme, že vaše přihláška na <strong>{{event_name}} {{event_year}}</strong> '
            'byla <strong>schválena</strong>.</p>'
            '<p>Brzy vás budeme kontaktovat s dalšími podrobnostmi k expedici.</p>'
            '<p style="margin-top: 24px;">Těšíme se na vás!<br>{{contact_name_1}} & {{contact_name_2}}</p>',
        'email_denied_subject': 'Přihláška zamítnuta – {{event_name}} {{event_year}}',
        'email_denied_body': '<h2 style="color: #e53e3e; margin: 0 0 20px 0;">Přihláška zamítnuta</h2>'
            '<p>Ahoj <strong>{{name_vocative}}</strong>,</p>'
            '<p>bohužel vám musíme sdělit, že vaše přihláška na <strong>{{event_name}} {{event_year}}</strong> '
            'byla <strong>zamítnuta</strong>.</p>'
            '<div style="background: #fff5f5; border-left: 4px solid #e53e3e; padding: 15px; margin: 20px 0; border-radius: 0 8px 8px 0;">'
            '<strong>Důvod:</strong><br>{{reason}}</div>'
            '<p>Pokud máte dotazy, neváhejte nás kontaktovat na {{contact_email}}.</p>'
            '<p style="margin-top: 24px;">S pozdravem,<br>{{contact_name_1}} & {{contact_name_2}}</p>',
        'email_payment_subject': 'Platební údaje – {{event_name}} {{event_year}}',
        'email_payment_body': '<h2 style="color: #99b20f; margin: 0 0 20px 0;">Platební údaje</h2>'
            '<p>Ahoj <strong>{{name_vocative}}</strong>,</p>'
            '<p>vaše přihláška na <strong>{{event_name}} {{event_year}}</strong> byla schválena. '
            'Níže naleznete platební údaje.</p>'
            '<div style="background: #f9fafb; border: 1px solid #e5e7eb; border-radius: 12px; padding: 20px; margin: 20px 0;">'
            '<table style="width: 100%; font-size: 15px;">'
            '<tr><td style="padding: 8px 0; color: #6b7280;">Číslo účtu:</td><td style="padding: 8px 0; font-weight: 700;">{{bank_account}}</td></tr>'
            '<tr><td style="padding: 8px 0; color: #6b7280;">Částka:</td><td style="padding: 8px 0; font-weight: 700;">{{amount}} Kč</td></tr>'
            '<tr><td style="padding: 8px 0; color: #6b7280;">Variabilní symbol:</td><td style="padding: 8px 0; font-weight: 700;">{{vs}}</td></tr>'
            '<tr><td style="padding: 8px 0; color: #6b7280;">Poznámka:</td><td style="padding: 8px 0; font-weight: 700;">{{payment_note}}</td></tr>'
            '</table></div>'
            '{{qr_code}}'
            '<p style="text-align: center; font-size: 13px; color: #6b7280;">Naskenujte QR kód v bankovní aplikaci pro rychlou platbu.</p>'
            '<p style="margin-top: 24px;">Těšíme se na vás!<br>{{contact_name_1}} & {{contact_name_2}}</p>',
    }
    for key, value in defaults.items():
        existing = db.execute('SELECT key FROM site_settings WHERE key = ?', (key,)).fetchone()
        if not existing:
            db.execute('INSERT INTO site_settings (key, value) VALUES (?, ?)', (key, value))

    # Per-admin SMTP defaults
    for username in ['michal', 'adam']:
        smtp_defaults = {
            f'smtp_host_{username}': '',
            f'smtp_port_{username}': '587',
            f'smtp_user_{username}': '',
            f'smtp_password_{username}': '',
            f'smtp_from_{username}': '',
        }
        for key, value in smtp_defaults.items():
            existing = db.execute('SELECT key FROM site_settings WHERE key = ?', (key,)).fetchone()
            if not existing:
                db.execute('INSERT INTO site_settings (key, value) VALUES (?, ?)', (key, value))

    db.commit()
    db.close()


def get_settings():
    cached = g.get('_settings_cache')
    if cached is not None:
        return cached
    db = get_db()
    rows = db.execute('SELECT key, value FROM site_settings').fetchall()
    result = {row['key']: row['value'] for row in rows}
    g._settings_cache = result
    return result


# ── Email helper ──────────────────────────────────────────────────

def send_email(to_email, subject, html_body, sender_username=None):
    """Send email in a background thread. Uses the given admin's SMTP config, with fallback."""
    db = _connect_db()
    rows = db.execute('SELECT key, value FROM site_settings').fetchall()
    settings = {row['key']: row['value'] for row in rows}
    db.close()

    if not to_email:
        return

    def _get_smtp(username):
        return {
            'host': settings.get(f'smtp_host_{username}', ''),
            'port': settings.get(f'smtp_port_{username}', '587'),
            'user': settings.get(f'smtp_user_{username}', ''),
            'password': settings.get(f'smtp_password_{username}', ''),
            'from': settings.get(f'smtp_from_{username}', ''),
        }

    smtp = None
    candidates = [sender_username] if sender_username else []
    for username in ['adam', 'michal']:
        if username not in candidates:
            candidates.append(username)

    for username in candidates:
        cfg = _get_smtp(username)
        if cfg['host']:
            smtp = cfg
            break

    if not smtp:
        return

    def _send():
        try:
            msg = MIMEMultipart('alternative')
            msg['Subject'] = subject
            msg['From'] = smtp['from'] or 'info@cykloexpedice.cz'
            msg['To'] = to_email
            msg.attach(MIMEText(html_body, 'html', 'utf-8'))

            port = int(smtp['port'] or 587)
            context = ssl.create_default_context()

            if port == 465:
                with smtplib.SMTP_SSL(smtp['host'], port, context=context) as server:
                    server.login(smtp['user'], smtp['password'])
                    server.send_message(msg)
            else:
                with smtplib.SMTP(smtp['host'], port) as server:
                    server.starttls(context=context)
                    server.login(smtp['user'], smtp['password'])
                    server.send_message(msg)
        except Exception as e:
            print(f'[EMAIL ERROR] {e}')

    Thread(target=_send, daemon=True).start()


# ── Email template helpers ────────────────────────────────────

def render_email_layout(body_html, settings):
    """Wrap email body in a branded layout matching the website design."""
    event_name = settings.get('event_name', 'Cykloexpedice')
    event_year = settings.get('event_year', '')
    contact_email = settings.get('contact_email', '')
    contact_name_1 = settings.get('contact_name_1', '')
    contact_name_2 = settings.get('contact_name_2', '')
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<link href="https://fonts.googleapis.com/css2?family=Oswald:wght@500;700&family=Montserrat:wght@400;600;700&display=swap" rel="stylesheet">
</head><body style="margin: 0; padding: 0; background-color: #f5f5f4;">
<div style="max-width: 600px; margin: 0 auto; font-family: 'Montserrat', Arial, sans-serif;">
    <!-- Header -->
    <div style="background: #1c1c1b; padding: 40px 30px 32px; text-align: center;">
        <h1 style="font-family: 'Oswald', Arial Black, sans-serif; color: #fbb01f; font-size: 32px; margin: 0; letter-spacing: 3px; font-weight: 700; text-transform: uppercase;">
            {event_name}
        </h1>
        <div style="width: 60px; height: 4px; background: #fbb01f; margin: 16px auto 0; border-radius: 2px;"></div>
        <p style="color: #9ca3af; font-size: 14px; margin: 12px 0 0; letter-spacing: 1px;">{event_year}</p>
    </div>
    <!-- Body -->
    <div style="padding: 36px 30px; background: #ffffff; font-size: 15px; line-height: 1.7; color: #374151;">
        {body_html}
    </div>
    <!-- Footer -->
    <div style="background: #1c1c1b; padding: 28px 30px; text-align: center;">
        <p style="color: #9ca3af; font-size: 13px; margin: 0 0 8px;">{contact_name_1} & {contact_name_2}</p>
        <a href="mailto:{contact_email}" style="color: #fbb01f; font-size: 13px; text-decoration: none;">{contact_email}</a>
    </div>
</div>
</body></html>"""


def name_vocative(full_name):
    """Get the vocative (5. pád) of the first name, capitalized."""
    first = full_name.strip().split()[0] if full_name else ''
    if not first:
        return ''
    return _vokativ(first).capitalize()


def render_email_template(template_key, variables, settings=None):
    """Load email template from settings, replace placeholders, wrap in layout.
    Returns (subject, full_html).
    """
    if settings is None:
        settings = get_settings()
    # Auto-generate vocative from name
    if 'name' in variables and 'name_vocative' not in variables:
        variables['name_vocative'] = name_vocative(variables['name'])
    subject = settings.get(f'{template_key}_subject', '')
    body = settings.get(f'{template_key}_body', '')
    # Replace all {{placeholder}} with values
    for key, val in variables.items():
        subject = subject.replace(f'{{{{{key}}}}}', str(val))
        body = body.replace(f'{{{{{key}}}}}', str(val))
    html = render_email_layout(body, settings)
    return subject, html


# ── Payment helpers ───────────────────────────────────────────

def generate_payment_qr(iban, amount, vs, message=''):
    """Generate a SPAYD QR code as base64-encoded PNG."""
    spayd = f"SPD*1.0*ACC:{iban}*AM:{amount:.2f}*CC:CZK*X-VS:{vs}"
    if message:
        spayd += f"*MSG:{message}"
    qr = qrcode.QRCode(version=None, error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=8, border=4)
    qr.add_data(spayd)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format='PNG')
    return base64.b64encode(buf.getvalue()).decode()


def check_fio_payments():
    """Check Fio Bank API for new incoming payments and match them to pending registrations."""
    db = _connect_db()
    try:
        rows = db.execute('SELECT key, value FROM site_settings').fetchall()
        settings = {row['key']: row['value'] for row in rows}
        token = FIO_API_TOKEN
        if not token:
            return

        # Use date range: last 30 days
        date_from = date.today().replace(day=1).isoformat()
        date_to = date.today().isoformat()
        url = f"https://fioapi.fio.cz/v1/rest/periods/{token}/{date_from}/{date_to}/transactions.json"

        resp = requests.get(url, timeout=30)
        if resp.status_code != 200:
            print(f'[FIO API] HTTP {resp.status_code}')
            return

        data = resp.json()
        tx_list = data.get('accountStatement', {}).get('transactionList', {})
        transactions = tx_list.get('transaction') if tx_list else None
        if not transactions:
            return

        # Get all pending registrations
        pending = db.execute(
            "SELECT id, variable_symbol, payment_amount FROM registrace WHERE payment_status = 'pending'"
        ).fetchall()
        vs_map = {str(r['variable_symbol']): r for r in pending if r['variable_symbol']}

        for tx in transactions:
            # column1 = amount, column5 = VS
            amount_col = tx.get('column1')
            vs_col = tx.get('column5')
            if not amount_col or not vs_col:
                continue
            amount = amount_col.get('value', 0) if amount_col else 0
            vs_val = str(vs_col.get('value', '')) if vs_col else ''
            if amount > 0 and vs_val in vs_map:
                reg = vs_map[vs_val]
                if amount >= (reg['payment_amount'] or 0):
                    db.execute(
                        "UPDATE registrace SET payment_status = 'paid' WHERE id = ?",
                        (reg['id'],)
                    )
                    print(f'[FIO] Payment matched: VS={vs_val}, amount={amount}')

        db.commit()
    except Exception as e:
        print(f'[FIO API ERROR] {e}')
    finally:
        db.close()



# ── Auth helpers ──────────────────────────────────────────────────

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'admin_id' not in session:
            return redirect(url_for('admin_login'))
        return f(*args, **kwargs)
    return decorated


def save_file(file, prefix=''):
    if file and file.filename:
        ext = os.path.splitext(file.filename)[1].lower()
        filename = f"{prefix}{secrets.token_hex(8)}{ext}"
        file.save(os.path.join(app.config['UPLOAD_FOLDER'], filename))
        return filename
    return None


# ── Context processor ─────────────────────────────────────────────

@app.context_processor
def inject_globals():
    try:
        db = get_db()
        etapy = db.execute('SELECT number, title FROM etapy ORDER BY number').fetchall()
        settings = get_settings()
    except Exception:
        etapy = []
        settings = {}
    return dict(etapy_nav=etapy, settings=settings, now=datetime.now())


# ── Error handlers ────────────────────────────────────────────────

def _render_error(code, title, message):
    """Render a standalone error page that doesn't depend on the database."""
    try:
        return render_template('error.html', code=code, title=title, message=message), code
    except Exception:
        return f"""<!DOCTYPE html><html><head><meta charset="utf-8"><title>{title}</title>
        <style>body{{font-family:sans-serif;background:#1c1c1b;color:#fff;display:flex;align-items:center;
        justify-content:center;min-height:100vh;margin:0}}div{{text-align:center}}
        h1{{font-size:6rem;color:#fbb01f33;margin:0}}h2{{margin:8px 0 16px}}
        a{{color:#fbb01f;text-decoration:none}}</style></head>
        <body><div><h1>{code}</h1><h2>{title}</h2><p style="color:#9ca3af">{message}</p>
        <p><a href="/">Zpět na úvod</a></p></div></body></html>""", code


@app.errorhandler(404)
def page_not_found(e):
    try:
        return render_template('404.html'), 404
    except Exception:
        return _render_error(404, 'Stránka nenalezena',
                             'Omlouváme se, ale stránka, kterou hledáte, neexistuje.')


@app.errorhandler(500)
def internal_error(e):
    return _render_error(500, 'Chyba serveru',
                         'Omlouváme se, došlo k neočekávané chybě. Zkuste to prosím později.')


@app.errorhandler(429)
def ratelimit_handler(e):
    return _render_error(429, 'Příliš mnoho požadavků',
                         'Překročili jste limit požadavků. Zkuste to prosím za chvíli.')


@app.errorhandler(Exception)
def handle_exception(e):
    if isinstance(e, psycopg2.OperationalError):
        print(f'[DB ERROR] {e}')
        return _render_error(503, 'Služba nedostupná',
                             'Připojení k databázi se nezdařilo. Zkuste to prosím za chvíli.')
    print(f'[ERROR] {type(e).__name__}: {e}')
    return _render_error(500, 'Chyba serveru',
                         'Omlouváme se, došlo k neočekávané chybě. Zkuste to prosím později.')


# ── Public routes ─────────────────────────────────────────────────

@app.route('/')
def index():
    return render_template('index.html')


@app.route('/propozice')
def propozice():
    db = get_db()
    row = db.execute('SELECT content FROM propozice ORDER BY id DESC LIMIT 1').fetchone()
    content = row['content'] if row else ''
    return render_template('propozice.html', content=content)


@app.route('/etapa/<int:number>')
def etapa(number):
    db = get_db()
    row = db.execute('SELECT * FROM etapy WHERE number = ?', (number,)).fetchone()
    if not row:
        abort(404)
    youtube = json.loads(row['youtube_links']) if row['youtube_links'] else []
    prev_e = db.execute('SELECT number FROM etapy WHERE number < ? ORDER BY number DESC LIMIT 1', (number,)).fetchone()
    next_e = db.execute('SELECT number FROM etapy WHERE number > ? ORDER BY number ASC LIMIT 1', (number,)).fetchone()
    return render_template('etapa.html', etapa=row, youtube=youtube,
                           prev_etapa=prev_e, next_etapa=next_e)


@app.route('/ubytovani')
def ubytovani():
    db = get_db()
    rows = db.execute('SELECT * FROM ubytovani ORDER BY sort_order').fetchall()
    return render_template('ubytovani.html', ubytovani=rows)


@app.route('/aktuality')
def aktuality():
    db = get_db()
    rows = db.execute('SELECT * FROM aktuality WHERE published = 1 ORDER BY created_at DESC').fetchall()
    return render_template('aktuality.html', aktuality=rows)


@app.route('/fotky')
def fotky():
    settings = get_settings()
    if settings.get('fotky_enabled', '1') != '1':
        abort(404)
    return render_template('fotky.html')


@app.route('/kontakt')
def kontakt():
    return render_template('kontakt.html')


@app.route('/registrace', methods=['GET', 'POST'])
@limiter.limit("5 per minute", methods=["POST"])
def registrace():
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        email = request.form.get('email', '').strip()
        phone = request.form.get('phone', '').strip()
        note = request.form.get('note', '').strip()
        if not name:
            flash('Jméno je povinné.', 'error')
            return render_template('registrace.html')
        db = get_db()
        db.execute(
            'INSERT INTO registrace (name, email, phone, note) VALUES (?, ?, ?, ?)',
            (name, email, phone, note)
        )
        db.commit()

        # Send confirmation email
        if email:
            settings = get_settings()
            tpl_vars = {
                'name': name,
                'event_name': settings.get('event_name', 'Cykloexpedice'),
                'event_year': settings.get('event_year', ''),
                'contact_name_1': settings.get('contact_name_1', ''),
                'contact_name_2': settings.get('contact_name_2', ''),
                'contact_email': settings.get('contact_email', ''),
            }
            subj, html = render_email_template('email_submitted', tpl_vars, settings)
            send_email(email, subj, html)

        flash('Děkujeme za přihlášku! Ozveme se vám.', 'success')
        return redirect(url_for('registrace'))
    return render_template('registrace.html')


# ── Admin auth routes ─────────────────────────────────────────────

@app.route('/admin/login', methods=['GET', 'POST'])
@limiter.limit("10 per minute", methods=["POST"])
def admin_login():
    if request.method == 'POST':
        username = request.form.get('username', '').strip().lower()
        password = request.form.get('password', '')
        db = get_db()
        admin = db.execute('SELECT * FROM admins WHERE username = ?', (username,)).fetchone()

        if not admin:
            flash('Neplatné přihlašovací údaje.', 'error')
            return render_template('admin/login.html')

        # First login - no password set yet
        if admin['password_hash'] is None:
            session['setup_admin_id'] = admin['id']
            return redirect(url_for('admin_set_password'))

        if bcrypt.checkpw(password.encode(), admin['password_hash'].encode()):
            session['admin_id'] = admin['id']
            session['admin_username'] = admin['username']
            return redirect(url_for('admin_dashboard'))

        flash('Neplatné přihlašovací údaje.', 'error')
    return render_template('admin/login.html')


@app.route('/admin/set-password', methods=['GET', 'POST'])
def admin_set_password():
    if 'setup_admin_id' not in session:
        return redirect(url_for('admin_login'))

    if request.method == 'POST':
        password = request.form.get('password', '')
        password2 = request.form.get('password2', '')
        if len(password) < 6:
            flash('Heslo musí mít alespoň 6 znaků.', 'error')
            return render_template('admin/set_password.html')
        if password != password2:
            flash('Hesla se neshodují.', 'error')
            return render_template('admin/set_password.html')

        hashed = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
        db = get_db()
        db.execute('UPDATE admins SET password_hash = ? WHERE id = ?',
                   (hashed, session['setup_admin_id']))
        db.commit()
        session['admin_id'] = session.pop('setup_admin_id')
        admin = db.execute('SELECT username FROM admins WHERE id = ?', (session['admin_id'],)).fetchone()
        session['admin_username'] = admin['username']
        flash('Heslo bylo nastaveno.', 'success')
        return redirect(url_for('admin_dashboard'))

    return render_template('admin/set_password.html')


@app.route('/admin/logout')
def admin_logout():
    session.clear()
    return redirect(url_for('admin_login'))


# ── Admin: Change password ────────────────────────────────────────

@app.route('/admin/change-password', methods=['GET', 'POST'])
@login_required
def admin_change_password():
    if request.method == 'POST':
        current = request.form.get('current_password', '')
        new_pw = request.form.get('new_password', '')
        new_pw2 = request.form.get('new_password2', '')

        db = get_db()
        admin = db.execute('SELECT * FROM admins WHERE id = ?', (session['admin_id'],)).fetchone()

        if not bcrypt.checkpw(current.encode(), admin['password_hash'].encode()):
            flash('Současné heslo je nesprávné.', 'error')
            return render_template('admin/change_password.html')
        if len(new_pw) < 6:
            flash('Nové heslo musí mít alespoň 6 znaků.', 'error')
            return render_template('admin/change_password.html')
        if new_pw != new_pw2:
            flash('Nová hesla se neshodují.', 'error')
            return render_template('admin/change_password.html')

        hashed = bcrypt.hashpw(new_pw.encode(), bcrypt.gensalt()).decode()
        db.execute('UPDATE admins SET password_hash = ? WHERE id = ?', (hashed, session['admin_id']))
        db.commit()
        flash('Heslo bylo změněno.', 'success')
        return redirect(url_for('admin_dashboard'))

    return render_template('admin/change_password.html')


# ── Admin: Profile (set email) ────────────────────────────────────

@app.route('/admin/profile', methods=['GET', 'POST'])
@login_required
def admin_profile():
    db = get_db()
    admin = db.execute('SELECT * FROM admins WHERE id = ?', (session['admin_id'],)).fetchone()

    if request.method == 'POST':
        email = request.form.get('email', '').strip()
        db.execute('UPDATE admins SET email = ? WHERE id = ?', (email, session['admin_id']))

        # Save per-admin SMTP settings
        username = admin['username']
        for key in ['smtp_host', 'smtp_port', 'smtp_user', 'smtp_password', 'smtp_from']:
            val = request.form.get(key, '')
            db.execute('INSERT OR REPLACE INTO site_settings (key, value) VALUES (?, ?)',
                       (f'{key}_{username}', val))

        db.commit()
        flash('Profil uložen.', 'success')
        return redirect(url_for('admin_profile'))

    # Load per-admin SMTP settings
    settings = get_settings()
    username = admin['username']
    smtp = {
        'smtp_host': settings.get(f'smtp_host_{username}', ''),
        'smtp_port': settings.get(f'smtp_port_{username}', '587'),
        'smtp_user': settings.get(f'smtp_user_{username}', ''),
        'smtp_password': settings.get(f'smtp_password_{username}', ''),
        'smtp_from': settings.get(f'smtp_from_{username}', ''),
    }
    return render_template('admin/profile.html', admin=admin, smtp=smtp)


# ── Forgot / Reset password ───────────────────────────────────────

@app.route('/admin/forgot-password', methods=['GET', 'POST'])
@limiter.limit("3 per minute", methods=["POST"])
def admin_forgot_password():
    if request.method == 'POST':
        username = request.form.get('username', '').strip().lower()
        db = get_db()
        admin = db.execute('SELECT * FROM admins WHERE username = ?', (username,)).fetchone()

        # Always show success message to prevent username enumeration
        if admin and admin['email']:
            token = secrets.token_urlsafe(48)
            expires = datetime.now().timestamp() + 3600  # 1 hour
            db.execute(
                'INSERT INTO password_reset_tokens (admin_id, token, expires_at) VALUES (?, ?, ?)',
                (admin['id'], token, datetime.fromtimestamp(expires))
            )
            db.commit()

            reset_url = request.host_url.rstrip('/') + url_for('admin_reset_password', token=token)
            settings = get_settings()
            body = f"""
                <h2 style="color: #1c1c1b; margin: 0 0 20px 0;">Obnovení hesla</h2>
                <p>Dobrý den, <strong>{admin['username']}</strong>,</p>
                <p>obdrželi jsme žádost o obnovení hesla k vašemu účtu v administraci Cykloexpedice.</p>
                <p>Klikněte na tlačítko níže pro nastavení nového hesla:</p>
                <p style="text-align: center; margin: 30px 0;">
                    <a href="{reset_url}"
                       style="background: #fbb01f; color: #1c1c1b; padding: 14px 36px; border-radius: 50px;
                              text-decoration: none; font-weight: 700; font-size: 14px; display: inline-block;
                              letter-spacing: 1px;">
                        NASTAVIT NOV&Eacute; HESLO
                    </a>
                </p>
                <p style="font-size: 13px; color: #999;">Tento odkaz je platný 1 hodinu. Pokud jste o obnovení hesla nežádali, tento e-mail ignorujte.</p>
                <p style="font-size: 12px; color: #ccc; word-break: break-all; margin-top: 20px;">
                    Pokud tlačítko nefunguje, zkopírujte tento odkaz do prohlížeče:<br>{reset_url}
                </p>
            """
            html = render_email_layout(body, settings)
            send_email(admin['email'], 'Obnovení hesla – Cykloexpedice Admin', html)

        flash('Pokud účet existuje a má nastavený e-mail, odeslali jsme odkaz pro obnovení hesla.', 'success')
        return redirect(url_for('admin_login'))

    return render_template('admin/forgot_password.html')


@app.route('/admin/reset-password/<token>', methods=['GET', 'POST'])
@limiter.limit("5 per minute", methods=["POST"])
def admin_reset_password(token):
    db = get_db()
    row = db.execute(
        'SELECT * FROM password_reset_tokens WHERE token = ? AND used = 0', (token,)
    ).fetchone()

    if not row:
        flash('Odkaz pro obnovení hesla je neplatný nebo vypršel.', 'error')
        return redirect(url_for('admin_login'))

    expires = row['expires_at']
    if isinstance(expires, str):
        expires = datetime.fromisoformat(expires)
    if expires < datetime.now():
        flash('Odkaz pro obnovení hesla je neplatný nebo vypršel.', 'error')
        return redirect(url_for('admin_login'))

    if request.method == 'POST':
        password = request.form.get('password', '')
        password2 = request.form.get('password2', '')

        if len(password) < 6:
            flash('Heslo musí mít alespoň 6 znaků.', 'error')
            return render_template('admin/reset_password.html', token=token)
        if password != password2:
            flash('Hesla se neshodují.', 'error')
            return render_template('admin/reset_password.html', token=token)

        hashed = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
        db.execute('UPDATE admins SET password_hash = ? WHERE id = ?', (hashed, row['admin_id']))
        db.execute('UPDATE password_reset_tokens SET used = 1 WHERE id = ?', (row['id'],))
        db.execute('UPDATE password_reset_tokens SET used = 1 WHERE admin_id = ? AND id != ?',
                   (row['admin_id'], row['id']))
        db.commit()

        flash('Heslo bylo úspěšně změněno. Přihlaste se.', 'success')
        return redirect(url_for('admin_login'))

    return render_template('admin/reset_password.html', token=token)


# ── Admin dashboard ───────────────────────────────────────────────

@app.route('/admin')
@login_required
def admin_dashboard():
    db = get_db()
    stats = {
        'etapy': db.execute('SELECT COUNT(*) c FROM etapy').fetchone()['c'],
        'aktuality': db.execute('SELECT COUNT(*) c FROM aktuality').fetchone()['c'],
        'registrace': db.execute('SELECT COUNT(*) c FROM registrace').fetchone()['c'],
        'ubytovani': db.execute('SELECT COUNT(*) c FROM ubytovani').fetchone()['c'],
    }
    return render_template('admin/dashboard.html', stats=stats)


# ── Admin: Etapy ──────────────────────────────────────────────────

@app.route('/admin/etapy')
@login_required
def admin_etapy():
    db = get_db()
    rows = db.execute('SELECT * FROM etapy ORDER BY number').fetchall()
    return render_template('admin/etapy.html', etapy=rows)


@app.route('/admin/etapy/new', methods=['GET', 'POST'])
@login_required
def admin_etapa_new():
    if request.method == 'POST':
        return _save_etapa(None)
    return render_template('admin/etapa_form.html', etapa=None, youtube_text='')


@app.route('/admin/etapy/<int:id>/edit', methods=['GET', 'POST'])
@login_required
def admin_etapa_edit(id):
    db = get_db()
    etapa = db.execute('SELECT * FROM etapy WHERE id = ?', (id,)).fetchone()
    if not etapa:
        abort(404)
    if request.method == 'POST':
        return _save_etapa(id)
    yt_links = '\n'.join(json.loads(etapa['youtube_links'])) if etapa['youtube_links'] else ''
    return render_template('admin/etapa_form.html', etapa=etapa, youtube_text=yt_links)


@app.route('/admin/etapy/<int:id>/delete', methods=['POST'])
@login_required
def admin_etapa_delete(id):
    db = get_db()
    db.execute('DELETE FROM etapy WHERE id = ?', (id,))
    db.commit()
    flash('Etapa smazána.', 'success')
    return redirect(url_for('admin_etapy'))


def _save_etapa(id):
    db = get_db()
    data = {
        'number': int(request.form.get('number', 0)),
        'title': request.form.get('title', ''),
        'date': request.form.get('date', ''),
        'distance': request.form.get('distance', ''),
        'elevation_up': request.form.get('elevation_up', ''),
        'elevation_down': request.form.get('elevation_down', ''),
        'route': request.form.get('route', ''),
        'waypoints': request.form.get('waypoints', ''),
        'description': request.form.get('description', ''),
        'map_link': request.form.get('map_link', ''),
        'color': request.form.get('color', '#ffc107'),
    }

    # YouTube links - stored as JSON array
    yt_raw = request.form.get('youtube_links', '')
    yt_list = [u.strip() for u in yt_raw.split('\n') if u.strip()]
    data['youtube_links'] = json.dumps(yt_list)

    # File uploads
    for field in ['profile_image', 'qr_image', 'gpx_file', 'map_download']:
        f = request.files.get(field)
        saved = save_file(f, f'etapa{data["number"]}_')
        if saved:
            data[field] = saved
        elif id:
            etapa = db.execute(f'SELECT {field} FROM etapy WHERE id = ?', (id,)).fetchone()
            data[field] = etapa[field] if etapa else None
        else:
            data[field] = None

    if id:
        cols = ', '.join(f'{k} = ?' for k in data)
        vals = list(data.values()) + [id]
        db.execute(f'UPDATE etapy SET {cols} WHERE id = ?', vals)
    else:
        cols = ', '.join(data.keys())
        placeholders = ', '.join('?' * len(data))
        db.execute(f'INSERT INTO etapy ({cols}) VALUES ({placeholders})', list(data.values()))

    db.commit()
    flash('Etapa uložena.', 'success')
    return redirect(url_for('admin_etapy'))


# ── Admin: Propozice ──────────────────────────────────────────────

@app.route('/admin/propozice', methods=['GET', 'POST'])
@login_required
def admin_propozice():
    db = get_db()
    if request.method == 'POST':
        content = request.form.get('content', '')
        existing = db.execute('SELECT id FROM propozice LIMIT 1').fetchone()
        if existing:
            db.execute('UPDATE propozice SET content = ?, updated_at = ? WHERE id = ?',
                       (content, datetime.now(), existing['id']))
        else:
            db.execute('INSERT INTO propozice (content) VALUES (?)', (content,))
        db.commit()
        flash('Propozice uloženy.', 'success')
        return redirect(url_for('admin_propozice'))

    row = db.execute('SELECT content FROM propozice ORDER BY id DESC LIMIT 1').fetchone()
    content = row['content'] if row else ''
    return render_template('admin/propozice.html', content=content)


# ── Admin: Ubytování ──────────────────────────────────────────────

@app.route('/admin/ubytovani')
@login_required
def admin_ubytovani():
    db = get_db()
    rows = db.execute('SELECT * FROM ubytovani ORDER BY sort_order').fetchall()
    return render_template('admin/ubytovani.html', ubytovani=rows)


@app.route('/admin/ubytovani/new', methods=['GET', 'POST'])
@login_required
def admin_ubytovani_new():
    if request.method == 'POST':
        return _save_ubytovani(None)
    return render_template('admin/ubytovani_form.html', item=None)


@app.route('/admin/ubytovani/<int:id>/edit', methods=['GET', 'POST'])
@login_required
def admin_ubytovani_edit(id):
    db = get_db()
    item = db.execute('SELECT * FROM ubytovani WHERE id = ?', (id,)).fetchone()
    if not item:
        abort(404)
    if request.method == 'POST':
        return _save_ubytovani(id)
    return render_template('admin/ubytovani_form.html', item=item)


@app.route('/admin/ubytovani/<int:id>/delete', methods=['POST'])
@login_required
def admin_ubytovani_delete(id):
    db = get_db()
    db.execute('DELETE FROM ubytovani WHERE id = ?', (id,))
    db.commit()
    flash('Ubytování smazáno.', 'success')
    return redirect(url_for('admin_ubytovani'))


def _save_ubytovani(id):
    db = get_db()
    data = {
        'etapa_number': int(request.form.get('etapa_number', 0)),
        'name': request.form.get('name', ''),
        'city': request.form.get('city', ''),
        'date': request.form.get('date', ''),
        'rooms_info': request.form.get('rooms_info', ''),
        'food_info': request.form.get('food_info', ''),
        'link': request.form.get('link', ''),
        'sort_order': int(request.form.get('sort_order', 0)),
    }
    if id:
        cols = ', '.join(f'{k} = ?' for k in data)
        db.execute(f'UPDATE ubytovani SET {cols} WHERE id = ?', list(data.values()) + [id])
    else:
        cols = ', '.join(data.keys())
        placeholders = ', '.join('?' * len(data))
        db.execute(f'INSERT INTO ubytovani ({cols}) VALUES ({placeholders})', list(data.values()))
    db.commit()
    flash('Ubytování uloženo.', 'success')
    return redirect(url_for('admin_ubytovani'))


# ── Admin: Aktuality ──────────────────────────────────────────────

@app.route('/admin/aktuality')
@login_required
def admin_aktuality():
    db = get_db()
    rows = db.execute('SELECT * FROM aktuality ORDER BY created_at DESC').fetchall()
    return render_template('admin/aktuality.html', aktuality=rows)


@app.route('/admin/aktuality/new', methods=['GET', 'POST'])
@login_required
def admin_aktualita_new():
    if request.method == 'POST':
        return _save_aktualita(None)
    return render_template('admin/aktualita_form.html', item=None)


@app.route('/admin/aktuality/<int:id>/edit', methods=['GET', 'POST'])
@login_required
def admin_aktualita_edit(id):
    db = get_db()
    item = db.execute('SELECT * FROM aktuality WHERE id = ?', (id,)).fetchone()
    if not item:
        abort(404)
    if request.method == 'POST':
        return _save_aktualita(id)
    return render_template('admin/aktualita_form.html', item=item)


@app.route('/admin/aktuality/<int:id>/delete', methods=['POST'])
@login_required
def admin_aktualita_delete(id):
    db = get_db()
    db.execute('DELETE FROM aktuality WHERE id = ?', (id,))
    db.commit()
    flash('Aktualita smazána.', 'success')
    return redirect(url_for('admin_aktuality'))


def _save_aktualita(id):
    db = get_db()
    title = request.form.get('title', '')
    content = request.form.get('content', '')
    published = 1 if request.form.get('published') else 0
    if id:
        db.execute('UPDATE aktuality SET title=?, content=?, published=? WHERE id=?',
                   (title, content, published, id))
    else:
        db.execute('INSERT INTO aktuality (title, content, published) VALUES (?,?,?)',
                   (title, content, published))
    db.commit()
    flash('Aktualita uložena.', 'success')
    return redirect(url_for('admin_aktuality'))


# ── Admin: Registrace ─────────────────────────────────────────────

@app.route('/admin/registrace')
@login_required
def admin_registrace():
    db = get_db()
    q = request.args.get('q', '').strip()
    status_filter = request.args.get('status', '').strip()
    conditions = []
    params = []
    if q:
        conditions.append("(name LIKE ? OR email LIKE ? OR phone LIKE ?)")
        params.extend([f'%{q}%', f'%{q}%', f'%{q}%'])
    if status_filter:
        conditions.append("status = ?")
        params.append(status_filter)
    where = 'WHERE ' + ' AND '.join(conditions) if conditions else ''
    rows = db.execute(f'SELECT * FROM registrace {where} ORDER BY created_at DESC', params).fetchall()
    counts = {
        'all': db.execute('SELECT COUNT(*) c FROM registrace').fetchone()['c'],
        'pending': db.execute("SELECT COUNT(*) c FROM registrace WHERE status = 'pending'").fetchone()['c'],
        'approved': db.execute("SELECT COUNT(*) c FROM registrace WHERE status = 'approved'").fetchone()['c'],
        'denied': db.execute("SELECT COUNT(*) c FROM registrace WHERE status = 'denied'").fetchone()['c'],
    }
    return render_template('admin/registrace.html', registrace=rows, query=q,
                           status_filter=status_filter, counts=counts)


@app.route('/admin/registrace/<int:id>/approve', methods=['POST'])
@login_required
def admin_registrace_approve(id):
    db = get_db()
    reg = db.execute('SELECT * FROM registrace WHERE id = ?', (id,)).fetchone()
    if not reg:
        abort(404)
    db.execute("UPDATE registrace SET status = 'approved', decided_at = ? WHERE id = ?",
               (datetime.now(), id))
    db.commit()

    # Send approval email
    settings = get_settings()
    tpl_vars = {
        'name': reg['name'],
        'event_name': settings.get('event_name', 'Cykloexpedice'),
        'event_year': settings.get('event_year', ''),
        'contact_name_1': settings.get('contact_name_1', ''),
        'contact_name_2': settings.get('contact_name_2', ''),
        'contact_email': settings.get('contact_email', ''),
    }
    subject, html = render_email_template('email_approved', tpl_vars, settings)
    send_email(reg['email'], subject, html, sender_username=session.get('admin_username'))

    flash(f'Registrace pro {reg["name"]} byla schválena. E-mail odeslán.', 'success')
    return redirect(url_for('admin_registrace'))


@app.route('/admin/registrace/<int:id>/deny', methods=['GET', 'POST'])
@login_required
def admin_registrace_deny(id):
    db = get_db()
    reg = db.execute('SELECT * FROM registrace WHERE id = ?', (id,)).fetchone()
    if not reg:
        abort(404)

    if request.method == 'POST':
        reason = request.form.get('reason', '').strip()
        if not reason:
            flash('Uveďte prosím důvod zamítnutí.', 'error')
            return render_template('admin/registrace_deny.html', reg=reg)

        db.execute("UPDATE registrace SET status = 'denied', admin_note = ?, decided_at = ? WHERE id = ?",
                   (reason, datetime.now(), id))
        db.commit()

        # Send denial email
        settings = get_settings()
        tpl_vars = {
            'name': reg['name'],
            'reason': reason,
            'event_name': settings.get('event_name', 'Cykloexpedice'),
            'event_year': settings.get('event_year', ''),
            'contact_name_1': settings.get('contact_name_1', ''),
            'contact_name_2': settings.get('contact_name_2', ''),
            'contact_email': settings.get('contact_email', ''),
        }
        subject, html = render_email_template('email_denied', tpl_vars, settings)
        send_email(reg['email'], subject, html, sender_username=session.get('admin_username'))

        flash(f'Registrace pro {reg["name"]} byla zamítnuta. E-mail odeslán.', 'success')
        return redirect(url_for('admin_registrace'))

    return render_template('admin/registrace_deny.html', reg=reg)


@app.route('/admin/registrace/<int:id>/delete', methods=['POST'])
@login_required
def admin_registrace_delete(id):
    db = get_db()
    db.execute('DELETE FROM registrace WHERE id = ?', (id,))
    db.commit()
    flash('Registrace smazána.', 'success')
    return redirect(url_for('admin_registrace'))


@app.route('/admin/registrace/<int:id>/send-payment', methods=['POST'])
@login_required
def admin_registrace_send_payment(id):
    db = get_db()
    reg = db.execute('SELECT * FROM registrace WHERE id = ?', (id,)).fetchone()
    if not reg:
        abort(404)
    if reg['status'] != 'approved':
        flash('Platební QR lze odeslat pouze u schválených registrací.', 'error')
        return redirect(url_for('admin_registrace'))
    if not reg['email']:
        flash('Registrace nemá zadaný e-mail.', 'error')
        return redirect(url_for('admin_registrace'))

    settings = get_settings()
    amount = float(settings.get('payment_amount', '0'))
    iban = settings.get('bank_iban', '')
    bank_account = settings.get('bank_account', '')

    if not iban or not amount:
        flash('Nejsou nastaveny platební údaje (IBAN, částka). Zkontrolujte nastavení.', 'error')
        return redirect(url_for('admin_registrace'))

    vs = reg['variable_symbol'] or reg['id']
    event_name = settings.get('event_name', 'Cykloexpedice')
    event_year = settings.get('event_year', '')

    # Build payment note from name: "WACHAU_firstname_lastname"
    name_parts = reg['name'].strip().split()
    payment_note = 'WACHAU_' + '_'.join(name_parts)

    # Generate QR code
    qr_b64 = generate_payment_qr(iban, amount, vs, payment_note)

    # Update registration
    db.execute(
        "UPDATE registrace SET variable_symbol = ?, payment_status = 'pending', "
        "payment_amount = ?, qr_sent_at = ? WHERE id = ?",
        (vs, amount, datetime.now(), id)
    )
    db.commit()

    # Send payment email
    qr_html = f'<p style="text-align: center; margin: 25px 0;"><img src="data:image/png;base64,{qr_b64}" alt="QR platba" style="width: 250px; height: 250px;"></p>'
    tpl_vars = {
        'name': reg['name'],
        'event_name': event_name,
        'event_year': event_year,
        'bank_account': bank_account,
        'amount': f'{amount:.0f}',
        'vs': str(vs),
        'payment_note': payment_note,
        'qr_code': qr_html,
        'contact_name_1': settings.get('contact_name_1', ''),
        'contact_name_2': settings.get('contact_name_2', ''),
        'contact_email': settings.get('contact_email', ''),
    }
    subject, html = render_email_template('email_payment', tpl_vars, settings)
    send_email(reg['email'], subject, html, sender_username=session.get('admin_username'))

    flash(f'Platební QR kód odeslán na {reg["email"]} (VS: {vs}).', 'success')
    return redirect(url_for('admin_registrace'))


@app.route('/admin/registrace/check-payments', methods=['POST'])
@login_required
def admin_check_payments():
    check_fio_payments()
    flash('Platby zkontrolovány.', 'success')
    return redirect(url_for('admin_registrace'))


# ── Admin: Email templates ────────────────────────────────────

_COMMON_PLACEHOLDERS = {
    'name': 'Jméno účastníka',
    'name_vocative': 'Jméno v 5. pádu (Michale, Vlasto…)',
    'event_name': 'Název akce',
    'event_year': 'Rok akce',
    'contact_name_1': 'Jméno organizátora 1',
    'contact_name_2': 'Jméno organizátora 2',
    'contact_email': 'Kontaktní e-mail',
}

EMAIL_TEMPLATES = [
    {
        'key': 'email_submitted',
        'label': 'Potvrzení přihlášky',
        'description': 'Odesláno účastníkovi po odeslání přihlášky.',
        'placeholders': _COMMON_PLACEHOLDERS,
    },
    {
        'key': 'email_approved',
        'label': 'Schválení přihlášky',
        'description': 'Odesláno účastníkovi po schválení přihlášky adminem.',
        'placeholders': _COMMON_PLACEHOLDERS,
    },
    {
        'key': 'email_denied',
        'label': 'Zamítnutí přihlášky',
        'description': 'Odesláno účastníkovi po zamítnutí přihlášky adminem.',
        'placeholders': {**_COMMON_PLACEHOLDERS, 'reason': 'Důvod zamítnutí'},
    },
    {
        'key': 'email_payment',
        'label': 'Platební údaje',
        'description': 'Odesláno účastníkovi s platebními údaji a QR kódem.',
        'placeholders': {
            **_COMMON_PLACEHOLDERS,
            'bank_account': 'Číslo účtu',
            'amount': 'Částka v Kč',
            'vs': 'Variabilní symbol',
            'payment_note': 'Poznámka k platbě',
            'qr_code': 'QR kód (obrázek)',
        },
    },
]


@app.route('/admin/email-sablony', methods=['GET', 'POST'])
@login_required
def admin_email_templates():
    db = get_db()
    if request.method == 'POST':
        for tpl in EMAIL_TEMPLATES:
            key = tpl['key']
            subj = request.form.get(f'{key}_subject', '')
            body = request.form.get(f'{key}_body', '')
            db.execute('INSERT OR REPLACE INTO site_settings (key, value) VALUES (?, ?)',
                       (f'{key}_subject', subj))
            db.execute('INSERT OR REPLACE INTO site_settings (key, value) VALUES (?, ?)',
                       (f'{key}_body', body))
        db.commit()
        flash('E-mailové šablony uloženy.', 'success')
        return redirect(url_for('admin_email_templates'))

    settings = get_settings()
    return render_template('admin/email_templates.html',
                           templates=EMAIL_TEMPLATES, settings=settings)


@app.route('/admin/email-sablony/preview', methods=['POST'])
@login_required
def admin_email_template_preview():
    """Return rendered email preview HTML."""
    settings = get_settings()
    subject = request.form.get('subject', '')
    body = request.form.get('body', '')
    # Fill placeholders with sample data
    sample = {
        'name': 'Jan Novák',
        'name_vocative': 'Jane',
        'event_name': settings.get('event_name', 'Cykloexpedice'),
        'event_year': settings.get('event_year', '2025'),
        'contact_name_1': settings.get('contact_name_1', 'Organizátor 1'),
        'contact_name_2': settings.get('contact_name_2', 'Organizátor 2'),
        'contact_email': settings.get('contact_email', 'info@example.cz'),
        'reason': 'Kapacita expedice byla naplněna.',
        'bank_account': settings.get('bank_account', '1234567890/0000'),
        'amount': settings.get('payment_amount', '3500'),
        'vs': '42',
        'payment_note': 'WACHAU_Jan_Novak',
        'qr_code': '<p style="text-align:center;margin:25px 0;"><img src="data:image/svg+xml;base64,PHN2ZyB3aWR0aD0iMjUwIiBoZWlnaHQ9IjI1MCIgeG1sbnM9Imh0dHA6Ly93d3cudzMub3JnLzIwMDAvc3ZnIj48cmVjdCB3aWR0aD0iMjUwIiBoZWlnaHQ9IjI1MCIgZmlsbD0iI2YzZjRmNiIgcng9IjEyIi8+PHRleHQgeD0iMTI1IiB5PSIxMjUiIHRleHQtYW5jaG9yPSJtaWRkbGUiIGR5PSIuM2VtIiBmaWxsPSIjOWNhM2FmIiBmb250LWZhbWlseT0ic2Fucy1zZXJpZiIgZm9udC1zaXplPSIxNCI+UVIga8OzZDwvdGV4dD48L3N2Zz4=" alt="QR" style="width:250px;height:250px;"></p>',
    }
    for key, val in sample.items():
        subject = subject.replace(f'{{{{{key}}}}}', val)
        body = body.replace(f'{{{{{key}}}}}', val)
    html = render_email_layout(body, settings)
    return html


# ── Admin: Site settings ──────────────────────────────────────────

@app.route('/admin/nastaveni', methods=['GET', 'POST'])
@login_required
def admin_settings():
    db = get_db()
    if request.method == 'POST':
        keys = ['event_name', 'event_year', 'event_days', 'event_km',
                'event_elevation', 'event_dates', 'contact_name_1',
                'contact_name_2', 'contact_email', 'photos_link', 'photos_text',
                'payment_amount', 'bank_account', 'bank_iban']
        for key in keys:
            val = request.form.get(key, '')
            db.execute('INSERT OR REPLACE INTO site_settings (key, value) VALUES (?, ?)', (key, val))
        # Checkbox toggle for fotky
        fotky_val = '1' if request.form.get('fotky_enabled') else '0'
        db.execute('INSERT OR REPLACE INTO site_settings (key, value) VALUES (?, ?)', ('fotky_enabled', fotky_val))
        db.commit()
        flash('Nastavení uloženo.', 'success')
        return redirect(url_for('admin_settings'))
    return render_template('admin/settings.html')


# ── Init & Run ────────────────────────────────────────────────────

if not os.environ.get('TESTING'):
    init_db()


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(debug=True, host='0.0.0.0', port=port)
