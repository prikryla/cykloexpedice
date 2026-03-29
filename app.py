import os
import json
import secrets
import smtplib
import ssl
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime
from functools import wraps
from threading import Thread

import psycopg2
from flask import (
    Flask, render_template, request, redirect, url_for,
    session, flash, g, abort, send_from_directory
)
import bcrypt

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', secrets.token_hex(32))
app.config['UPLOAD_FOLDER'] = os.path.join(os.path.dirname(__file__), 'static', 'uploads')
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB

DATABASE_URL = os.environ.get('DATABASE_URL', '')

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
            decided_at TIMESTAMP, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''',
    ]:
        db.execute(stmt)
    db.commit()

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
    db = get_db()
    rows = db.execute('SELECT key, value FROM site_settings').fetchall()
    return {row['key']: row['value'] for row in rows}


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
    db = get_db()
    etapy = db.execute('SELECT number, title FROM etapy ORDER BY number').fetchall()
    settings = get_settings()
    return dict(etapy_nav=etapy, settings=settings, now=datetime.now())


# ── Error handlers ────────────────────────────────────────────────

@app.errorhandler(404)
def page_not_found(e):
    return render_template('404.html'), 404


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
        flash('Děkujeme za přihlášku! Ozveme se vám.', 'success')
        return redirect(url_for('registrace'))
    return render_template('registrace.html')


# ── Admin auth routes ─────────────────────────────────────────────

@app.route('/admin/login', methods=['GET', 'POST'])
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
            html = f"""
            <div style="font-family: 'Montserrat', Arial, sans-serif; max-width: 600px; margin: 0 auto;">
                <div style="background: #1c1c1b; padding: 30px; text-align: center;">
                    <h1 style="color: #fbb01f; font-size: 28px; margin: 0;">Cykloexpedice – Admin</h1>
                </div>
                <div style="padding: 30px; background: #ffffff;">
                    <h2 style="color: #1c1c1b;">Obnovení hesla</h2>
                    <p>Dobrý den, <strong>{admin['username']}</strong>,</p>
                    <p>obdrželi jsme žádost o obnovení hesla k vašemu účtu v administraci Cykloexpedice.</p>
                    <p>Klikněte na tlačítko níže pro nastavení nového hesla:</p>
                    <p style="text-align: center; margin: 30px 0;">
                        <a href="{reset_url}"
                           style="background: #fbb01f; color: #1c1c1b; padding: 14px 36px; border-radius: 50px;
                                  text-decoration: none; font-weight: 700; font-size: 14px; display: inline-block;">
                            NASTAVIT NOVÉ HESLO
                        </a>
                    </p>
                    <p style="font-size: 13px; color: #999;">Tento odkaz je platný 1 hodinu. Pokud jste o obnovení hesla nežádali, tento e-mail ignorujte.</p>
                    <p style="font-size: 12px; color: #ccc; word-break: break-all; margin-top: 20px;">
                        Pokud tlačítko nefunguje, zkopírujte tento odkaz do prohlížeče:<br>{reset_url}
                    </p>
                </div>
                <div style="background: #f3f3f2; padding: 15px; text-align: center; font-size: 12px; color: #999;">
                    {settings.get('contact_email', '')}
                </div>
            </div>
            """
            send_email(admin['email'], 'Obnovení hesla – Cykloexpedice Admin', html)

        flash('Pokud účet existuje a má nastavený e-mail, odeslali jsme odkaz pro obnovení hesla.', 'success')
        return redirect(url_for('admin_login'))

    return render_template('admin/forgot_password.html')


@app.route('/admin/reset-password/<token>', methods=['GET', 'POST'])
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
    event_name = settings.get('event_name', 'Cykloexpedice')
    event_year = settings.get('event_year', '')
    html = f"""
    <div style="font-family: 'Montserrat', Arial, sans-serif; max-width: 600px; margin: 0 auto;">
        <div style="background: #1c1c1b; padding: 30px; text-align: center;">
            <h1 style="color: #fbb01f; font-size: 28px; margin: 0;">
                {event_name} {event_year}
            </h1>
        </div>
        <div style="padding: 30px; background: #ffffff;">
            <h2 style="color: #99b20f;">Vaše přihláška byla schválena!</h2>
            <p>Dobrý den, <strong>{reg['name']}</strong>,</p>
            <p>s radostí vám oznamujeme, že vaše přihláška na {event_name} {event_year} byla <strong>schválena</strong>.</p>
            <p>Brzy vás budeme kontaktovat s dalšími podrobnostmi k expedici.</p>
            <p>Těšíme se na vás!<br>
            {settings.get('contact_name_1', '')} & {settings.get('contact_name_2', '')}</p>
        </div>
        <div style="background: #f3f3f2; padding: 15px; text-align: center; font-size: 12px; color: #999;">
            {settings.get('contact_email', '')}
        </div>
    </div>
    """
    send_email(reg['email'], f'Přihláška schválena – {event_name} {event_year}', html,
               sender_username=session.get('admin_username'))

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
        event_name = settings.get('event_name', 'Cykloexpedice')
        event_year = settings.get('event_year', '')
        html = f"""
        <div style="font-family: 'Montserrat', Arial, sans-serif; max-width: 600px; margin: 0 auto;">
            <div style="background: #1c1c1b; padding: 30px; text-align: center;">
                <h1 style="color: #fbb01f; font-size: 28px; margin: 0;">
                    {event_name} {event_year}
                </h1>
            </div>
            <div style="padding: 30px; background: #ffffff;">
                <h2 style="color: #e53e3e;">Přihláška zamítnuta</h2>
                <p>Dobrý den, <strong>{reg['name']}</strong>,</p>
                <p>bohužel vám musíme sdělit, že vaše přihláška na {event_name} {event_year} byla <strong>zamítnuta</strong>.</p>
                <div style="background: #fff5f5; border-left: 4px solid #e53e3e; padding: 15px; margin: 20px 0;">
                    <strong>Důvod:</strong><br>{reason}
                </div>
                <p>Pokud máte dotazy, neváhejte nás kontaktovat na {settings.get('contact_email', '')}.</p>
                <p>S pozdravem,<br>
                {settings.get('contact_name_1', '')} & {settings.get('contact_name_2', '')}</p>
            </div>
            <div style="background: #f3f3f2; padding: 15px; text-align: center; font-size: 12px; color: #999;">
                {settings.get('contact_email', '')}
            </div>
        </div>
        """
        send_email(reg['email'], f'Přihláška zamítnuta – {event_name} {event_year}', html,
                   sender_username=session.get('admin_username'))

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


# ── Admin: Site settings ──────────────────────────────────────────

@app.route('/admin/nastaveni', methods=['GET', 'POST'])
@login_required
def admin_settings():
    db = get_db()
    if request.method == 'POST':
        keys = ['event_name', 'event_year', 'event_days', 'event_km',
                'event_elevation', 'event_dates', 'contact_name_1',
                'contact_name_2', 'contact_email', 'photos_link', 'photos_text']
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

init_db()

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(debug=True, host='0.0.0.0', port=port)
