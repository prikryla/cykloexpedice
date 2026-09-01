import os
import io
import json
import re
import hashlib
import base64
import secrets
import time as _time

import resend

from datetime import datetime, date, timedelta, timezone
from functools import wraps
from html import escape as html_escape
from threading import Thread
from zoneinfo import ZoneInfo

import psycopg2
import qrcode
import requests
from flask import (
    Flask, render_template, request, redirect, url_for,
    session, flash, g, abort
)
import bcrypt
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from vokativ import vokativ as _vokativ
from werkzeug.middleware.proxy_fix import ProxyFix

VERSION = '1.7.0'

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
app.secret_key = os.environ.get('SECRET_KEY', secrets.token_hex(32))
app.config['UPLOAD_FOLDER'] = os.path.join(os.path.dirname(__file__), 'static', 'uploads')
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['SESSION_COOKIE_SECURE'] = os.environ.get('FLASK_ENV') == 'production'
app.config['PERMANENT_SESSION_LIFETIME'] = 8 * 60 * 60

limiter = Limiter(get_remote_address, app=app, default_limits=[], storage_uri="memory://")

ALLOWED_EMAIL_TLDS = ('.cz', '.sk', '.com')


def generate_csrf_token():
    if '_csrf_token' not in session:
        session['_csrf_token'] = secrets.token_hex(32)
    return session['_csrf_token']


app.jinja_env.globals['csrf_token'] = generate_csrf_token


@app.before_request
def check_csrf():
    if request.method == 'POST':
        token = request.form.get('_csrf_token') or request.headers.get('X-CSRF-Token')
        if not token or token != session.get('_csrf_token'):
            if request.path.startswith('/admin') and 'admin_id' not in session:
                flash('Platnost přihlášení vypršela, přihlaste se prosím znovu.', 'warning')
                return redirect(url_for('admin_login'))
            abort(403)

ALLOWED_UPLOAD_EXTENSIONS = {'jpg', 'jpeg', 'png', 'gif', 'webp', 'gpx', 'zip', 'pdf'}
ADMIN_NOTIFICATION_EMAILS = ['adam.prikryl7@gmail.com', 'michal.prikryl@atlas.cz']
PRAGUE_TZ = ZoneInfo('Europe/Prague')


@app.template_filter('prague_time')
def prague_time_filter(dt, fmt='%d.%m.%Y %H:%M:%S'):
    if dt is None:
        return '–'
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(PRAGUE_TZ).strftime(fmt)


@app.template_filter('flag')
def flag_filter(country_code):
    if not country_code or len(country_code) != 2:
        return ''
    return ''.join(chr(ord(c) + 127397) for c in country_code.upper())


DATABASE_URL = os.environ.get('DATABASE_URL', '')
FIO_API_TOKEN = os.environ.get('FIO_API_TOKEN', '')
STRAVA_CLIENT_ID = os.environ.get('STRAVA_CLIENT_ID', '')
STRAVA_CLIENT_SECRET = os.environ.get('STRAVA_CLIENT_SECRET', '')

os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)


@app.after_request
def set_security_headers(response):
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
    response.headers['Permissions-Policy'] = 'camera=(), microphone=(), geolocation=()'
    if os.environ.get('FLASK_ENV') == 'production':
        response.headers['Strict-Transport-Security'] = 'max-age=31536000; includeSubDomains'
    return response


# ── Visitor tracking ─────────────────────────────────────────────

PUBLIC_ENDPOINTS = frozenset({
    'index', 'propozice', 'etapa', 'ubytovani',
    'aktuality', 'fotky', 'kontakt', 'registrace',
    'ochrana_osobnich_udaju',
})

_BOT_PATTERNS = re.compile(
    r'bot|crawl|spider|slurp|googlebot|bingbot|yandex|baidu|duckduck'
    r'|semrush|ahrefs|mj12|dotbot|petalbot|bytespider|gptbot|claudebot'
    r'|facebookexternalhit|twitterbot|linkedinbot|whatsapp|telegrambot'
    r'|curl|wget|python-requests|scrapy|httpclient|java/|go-http'
    r'|headlesschrome|phantomjs|puppeteer|lighthouse|pagespeed'
    r'|uptimerobot|pingdom|statuscake|site24x7|monitor'
    r'|iphone os 13_2_3'
    r'|windows nt 9[._]',
    re.IGNORECASE,
)


def _is_bot(user_agent):
    return bool(_BOT_PATTERNS.search(user_agent)) if user_agent else False


@app.after_request
def track_page_view(response):
    if (request.method == 'GET'
            and response.status_code == 200
            and request.endpoint in PUBLIC_ENDPOINTS):
        try:
            if request.cookies.get('analytics_consent') == 'rejected':
                return response
            ip = request.remote_addr or ''
            if not ip:
                return response
            ip_hash = hashlib.sha256(ip.encode()).hexdigest()[:16]
            path = request.path
            user_agent = (request.headers.get('User-Agent') or '')[:500]
            referrer = (request.headers.get('Referer') or '')[:500]
            bot = 1 if _is_bot(user_agent) else 0

            db = get_db()
            db.execute(
                'INSERT INTO page_views (ip_hash, path, user_agent, referrer, is_bot) '
                'VALUES (?, ?, ?, ?, ?)',
                (ip_hash, path, user_agent, referrer, bot),
            )
            db.commit()

            cached = db.execute(
                'SELECT ip_hash FROM ip_geolocation WHERE ip_hash = ?',
                (ip_hash,),
            ).fetchone()
            if not cached:
                Thread(target=_lookup_geo, args=(ip, ip_hash), daemon=True).start()
        except Exception:
            pass
    return response


def _lookup_geo(ip, ip_hash):
    try:
        resp = requests.get(
            f'http://ip-api.com/json/{ip}',
            params={'fields': 'status,country,countryCode'},
            timeout=5,
        )
        if resp.status_code == 200:
            data = resp.json()
            if data.get('status') == 'success':
                db = _connect_db()
                try:
                    db.execute(
                        'INSERT INTO ip_geolocation (ip_hash, country_code, country_name) '
                        'VALUES (?, ?, ?) '
                        'ON CONFLICT (ip_hash) DO NOTHING',
                        (ip_hash, data.get('countryCode', ''),
                         data.get('country', '')),
                    )
                    db.commit()
                finally:
                    db.close()
    except Exception:
        pass


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
        m = re.match(
            r'INSERT OR REPLACE INTO (\w+)\s*\(([^)]+)\)\s*VALUES\s*\(([^)]+)\)',
            query, re.IGNORECASE,
        )
        if m:
            table, col_str, vals = m.group(1), m.group(2), m.group(3)
            cols = [c.strip() for c in col_str.split(',')]
            updates = ', '.join(f'{c} = EXCLUDED.{c}' for c in cols[1:])
            query = (f'INSERT INTO {table} ({col_str}) VALUES ({vals}) '
                     f'ON CONFLICT ({cols[0]}) DO UPDATE SET {updates}')
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
        '''CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY, name TEXT NOT NULL,
            email TEXT UNIQUE NOT NULL, password_hash TEXT NOT NULL,
            strava_athlete_id BIGINT, strava_access_token TEXT,
            strava_refresh_token TEXT, strava_expires_at INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''',
        '''CREATE TABLE IF NOT EXISTS user_password_reset_tokens (
            id SERIAL PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id),
            token TEXT UNIQUE NOT NULL, expires_at TIMESTAMP NOT NULL,
            used INTEGER DEFAULT 0, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''',
        '''CREATE TABLE IF NOT EXISTS hidden_activities (
            id SERIAL PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id),
            strava_activity_id BIGINT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''',
        '''CREATE TABLE IF NOT EXISTS activity_device_cache (
            id SERIAL PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id),
            strava_activity_id BIGINT NOT NULL,
            device_name TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''',
        '''CREATE TABLE IF NOT EXISTS page_views (
            id SERIAL PRIMARY KEY,
            ip_hash TEXT NOT NULL,
            path TEXT NOT NULL,
            user_agent TEXT,
            referrer TEXT,
            is_bot INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''',
        '''CREATE TABLE IF NOT EXISTS ip_geolocation (
            ip_hash TEXT PRIMARY KEY,
            country_code TEXT,
            country_name TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''',
    ]:
        db.execute(stmt)
    db.commit()

    for idx_stmt in [
        'CREATE INDEX IF NOT EXISTS idx_page_views_created_at ON page_views(created_at)',
        'CREATE INDEX IF NOT EXISTS idx_page_views_ip_hash ON page_views(ip_hash)',
    ]:
        try:
            db.execute(idx_stmt)
            db.commit()
        except Exception:
            db._conn.rollback()

    # Add is_bot column to page_views if it doesn't exist (migration for existing DBs)
    try:
        db.execute("ALTER TABLE page_views ADD COLUMN is_bot INTEGER DEFAULT 0")
        db.commit()
    except Exception:
        db._conn.rollback()

    # Backfill is_bot for existing page_views that match bot patterns
    try:
        rows = db.execute(
            "SELECT id, user_agent FROM page_views WHERE is_bot = 0 AND user_agent IS NOT NULL",
        ).fetchall()
        updated = 0
        for row in rows:
            if _is_bot(row['user_agent']):
                db.execute("UPDATE page_views SET is_bot = 1 WHERE id = ?", (row['id'],))
                updated += 1
        if updated:
            db.commit()
            print(f'[ANALYTICS] Backfilled {updated} page views as bot traffic')
    except Exception:
        db._conn.rollback()

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

    # Add weather location columns to etapy if they don't exist (migration for existing DBs)
    for col_stmt in [
        "ALTER TABLE etapy ADD COLUMN route_start TEXT",
        "ALTER TABLE etapy ADD COLUMN route_end TEXT",
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
        'maintenance_enabled': '1',
        'maintenance_until': '2026-06-01T00:00:01',
        'max_capacity': '30',
        'payment_amount': '3500',
        'bank_account': '2703473997/2010',
        'bank_iban': 'CZ1620100000002703473997',
        # Email templates
        'email_submitted_subject': 'Přihláška přijata – {{event_name}} {{event_year}}',
        'email_submitted_body': '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">'
            '<tr><td align="center" style="padding-bottom:24px;">'
            '<table role="presentation" cellpadding="0" cellspacing="0" border="0"><tr>'
            '<td style="background-color:#99b20f;padding:8px 24px;font-family:Arial,sans-serif;font-size:13px;font-weight:700;color:#ffffff;text-transform:uppercase;letter-spacing:2px;">PŘIHLÁŠKA PŘIJATA</td>'
            '</tr></table></td></tr>'
            '<tr><td style="font-family:Arial,Helvetica,sans-serif;font-size:15px;line-height:26px;color:#374151;">'
            '<p style="margin:0 0 16px;">Ahoj <strong>{{name_vocative}}</strong>,</p>'
            '<p style="margin:0 0 16px;">děkujeme za vaši přihlášku na <strong>{{event_name}} {{event_year}}</strong>. '
            'Vaše přihláška byla úspěšně zaregistrována.</p>'
            '<p style="margin:0;">Jakmile bude kapacita naplněna, budeme vás kontaktovat s dalšími informacemi.</p>'
            '</td></tr></table>',
        'email_approved_subject': 'Přihláška schválena – {{event_name}} {{event_year}}',
        'email_approved_body': '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">'
            '<tr><td align="center" style="padding-bottom:24px;">'
            '<table role="presentation" cellpadding="0" cellspacing="0" border="0"><tr>'
            '<td style="background-color:#99b20f;padding:8px 24px;font-family:Arial,sans-serif;font-size:13px;font-weight:700;color:#ffffff;text-transform:uppercase;letter-spacing:2px;">PŘIHLÁŠKA SCHVÁLENA</td>'
            '</tr></table></td></tr>'
            '<tr><td style="font-family:Arial,Helvetica,sans-serif;font-size:15px;line-height:26px;color:#374151;">'
            '<p style="margin:0 0 16px;">Ahoj <strong>{{name_vocative}}</strong>,</p>'
            '<p style="margin:0 0 16px;">s radostí vám oznamujeme, že vaše přihláška na <strong>{{event_name}} {{event_year}}</strong> '
            'byla <strong>schválena</strong>.</p>'
            '<p style="margin:0;">Brzy vás budeme kontaktovat s dalšími podrobnostmi k expedici.</p>'
            '</td></tr></table>',
        'email_denied_subject': 'Přihláška zamítnuta – {{event_name}} {{event_year}}',
        'email_denied_body': '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">'
            '<tr><td align="center" style="padding-bottom:24px;">'
            '<table role="presentation" cellpadding="0" cellspacing="0" border="0"><tr>'
            '<td style="background-color:#e53e3e;padding:8px 24px;font-family:Arial,sans-serif;font-size:13px;font-weight:700;color:#ffffff;text-transform:uppercase;letter-spacing:2px;">PŘIHLÁŠKA ZAMÍTNUTA</td>'
            '</tr></table></td></tr>'
            '<tr><td style="font-family:Arial,Helvetica,sans-serif;font-size:15px;line-height:26px;color:#374151;">'
            '<p style="margin:0 0 16px;">Ahoj <strong>{{name_vocative}}</strong>,</p>'
            '<p style="margin:0 0 16px;">bohužel vám musíme sdělit, že vaše přihláška na <strong>{{event_name}} {{event_year}}</strong> '
            'byla <strong>zamítnuta</strong>.</p>'
            '</td></tr>'
            '<tr><td style="padding:0 0 20px;">'
            '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">'
            '<tr><td style="border-left:4px solid #e53e3e;padding:15px;background-color:#fff5f5;font-family:Arial,Helvetica,sans-serif;font-size:15px;line-height:24px;color:#374151;">'
            '<strong>Důvod:</strong><br>{{reason}}</td></tr></table></td></tr>'
            '<tr><td style="font-family:Arial,Helvetica,sans-serif;font-size:15px;line-height:26px;color:#374151;">'
            '<p style="margin:0;">Pokud máte dotazy, neváhejte nás kontaktovat na {{contact_email}}.</p>'
            '</td></tr></table>',
        'email_payment_confirmed_subject': 'Platba přijata – {{event_name}} {{event_year}}',
        'email_payment_confirmed_body': '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">'
            '<tr><td align="center" style="padding-bottom:24px;">'
            '<table role="presentation" cellpadding="0" cellspacing="0" border="0"><tr>'
            '<td style="background-color:#99b20f;padding:8px 24px;font-family:Arial,sans-serif;font-size:13px;font-weight:700;color:#ffffff;text-transform:uppercase;letter-spacing:2px;">PLATBA PŘIJATA</td>'
            '</tr></table></td></tr>'
            '<tr><td style="font-family:Arial,Helvetica,sans-serif;font-size:15px;line-height:26px;color:#374151;">'
            '<p style="margin:0 0 16px;">Ahoj <strong>{{name_vocative}}</strong>,</p>'
            '<p style="margin:0 0 16px;">potvrzujeme přijetí tvé platby a oficiálně tě tak vítáme na startovní listině '
            '<strong>{{event_name}} {{event_year}}</strong>!</p>'
            '<p style="margin:0;">Těšíme se na společné kilometry. Brzy se ozveme s dalšími informacemi k expedici.</p>'
            '</td></tr></table>',
        'email_payment_subject': 'Platební údaje – {{event_name}} {{event_year}}',
        'email_payment_body': '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">'
            '<tr><td align="center" style="padding-bottom:24px;">'
            '<table role="presentation" cellpadding="0" cellspacing="0" border="0"><tr>'
            '<td style="background-color:#fbb01f;padding:8px 24px;font-family:Arial,sans-serif;font-size:13px;font-weight:700;color:#1c1c1b;text-transform:uppercase;letter-spacing:2px;">PLATEBNÍ ÚDAJE</td>'
            '</tr></table></td></tr>'
            '<tr><td style="font-family:Arial,Helvetica,sans-serif;font-size:15px;line-height:26px;color:#374151;">'
            '<p style="margin:0 0 16px;">Ahoj <strong>{{name_vocative}}</strong>,</p>'
            '<p style="margin:0 0 20px;">vaše přihláška na <strong>{{event_name}} {{event_year}}</strong> byla schválena. '
            'Níže naleznete platební údaje.</p>'
            '</td></tr>'
            '<tr><td style="padding:0 0 20px;">'
            '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="border:1px solid #e5e7eb;">'
            '<tr><td bgcolor="#f9fafb" style="background-color:#f9fafb;padding:20px;">'
            '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">'
            '<tr><td style="padding:8px 0;color:#6b7280;font-family:Arial,Helvetica,sans-serif;font-size:15px;">Číslo účtu:</td><td style="padding:8px 0;font-weight:700;font-family:Arial,Helvetica,sans-serif;font-size:15px;">{{bank_account}}</td></tr>'
            '<tr><td style="padding:8px 0;color:#6b7280;font-family:Arial,Helvetica,sans-serif;font-size:15px;">Částka:</td><td style="padding:8px 0;font-weight:700;font-family:Arial,Helvetica,sans-serif;font-size:15px;">{{amount}} Kč</td></tr>'
            '<tr><td style="padding:8px 0;color:#6b7280;font-family:Arial,Helvetica,sans-serif;font-size:15px;">Variabilní symbol:</td><td style="padding:8px 0;font-weight:700;font-family:Arial,Helvetica,sans-serif;font-size:15px;">{{vs}}</td></tr>'
            '<tr><td style="padding:8px 0;color:#6b7280;font-family:Arial,Helvetica,sans-serif;font-size:15px;">Poznámka:</td><td style="padding:8px 0;font-weight:700;font-family:Arial,Helvetica,sans-serif;font-size:15px;">{{payment_note}}</td></tr>'
            '</table></td></tr></table></td></tr>'
            '<tr><td align="center" style="padding:0 0 8px;">{{qr_code}}</td></tr>'
            '<tr><td align="center" style="font-family:Arial,Helvetica,sans-serif;font-size:13px;color:#6b7280;">'
            '<p style="margin:0;">Naskenujte QR kód v bankovní aplikaci pro rychlou platbu.</p>'
            '</td></tr></table>',
    }
    for key, value in defaults.items():
        existing = db.execute('SELECT key FROM site_settings WHERE key = ?', (key,)).fetchone()
        if not existing:
            db.execute('INSERT INTO site_settings (key, value) VALUES (?, ?)', (key, value))

    # Migrate old div-based email templates to table-based
    email_keys = ['email_submitted_body', 'email_approved_body', 'email_denied_body', 'email_payment_body']
    for key in email_keys:
        row = db.execute('SELECT value FROM site_settings WHERE key = ?', (key,)).fetchone()
        if row and 'role="presentation"' not in row['value']:
            db.execute('INSERT OR REPLACE INTO site_settings (key, value) VALUES (?, ?)', (key, defaults[key]))

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

_RESEND_API_KEY = os.environ.get('RESEND_API_KEY', '')
_EMAIL_FROM = 'Cykloexpedice <info@cykloexpedice.cz>'


def send_email(to_email, subject, html_body):
    """Send email via Resend API in a background thread."""
    if not to_email:
        return

    if not _RESEND_API_KEY:
        print(f'[EMAIL] RESEND_API_KEY not set, skipping email to {to_email}: {subject}')
        return

    def _send():
        try:
            resend.api_key = _RESEND_API_KEY
            resend.Emails.send({
                'from': _EMAIL_FROM,
                'to': [to_email],
                'subject': subject,
                'html': html_body,
            })
        except Exception as e:
            print(f'[EMAIL ERROR] {e}')

    Thread(target=_send, daemon=True).start()


def send_bulk_notifications(recipients, subject, html_body):
    """Send email to each recipient individually with a 3s delay between sends."""
    def _run():
        for i, email in enumerate(recipients):
            if i > 0:
                _time.sleep(3)
            send_email(email, subject, html_body)
        print(f'[EMAIL] Bulk notification dispatched to {len(recipients)} recipients')
    Thread(target=_run, daemon=True).start()


def send_bulk_emails(email_items):
    """Send a list of unique emails individually with a 3s delay between sends.

    email_items: list of (to_email, subject, html_body) tuples.
    """
    def _run():
        for i, (to_email, subject, html_body) in enumerate(email_items):
            if i > 0:
                _time.sleep(3)
            send_email(to_email, subject, html_body)
        print(f'[EMAIL] Bulk emails dispatched to {len(email_items)} recipients')
    Thread(target=_run, daemon=True).start()


# ── Weather & SMS ─────────────────────────────────────────────

_DIACRITICS_TABLE = str.maketrans(
    'áčďéěíňóřšťúůýžÁČĎÉĚÍŇÓŘŠŤÚŮÝŽ',
    'acdeeinorstuuyzACDEEINORSTUUYZ',
)

WMO_DESCRIPTIONS = {
    0: 'Jasno', 1: 'Prevazne jasno', 2: 'Polojasno', 3: 'Zatazeno',
    45: 'Mlha', 48: 'Mlha',
    51: 'Mrholeni', 53: 'Mrholeni', 55: 'Mrholeni',
    56: 'Mrznouci mrholeni', 57: 'Mrznouci mrholeni',
    61: 'Dest', 63: 'Dest', 65: 'Silny dest',
    66: 'Mrznouci dest', 67: 'Mrznouci dest',
    71: 'Snezeni', 73: 'Snezeni', 75: 'Silne snezeni', 77: 'Snehove krupe',
    80: 'Prehnanky', 81: 'Prehnanky', 82: 'Silne prehnanky',
    85: 'Snehove prehnanky', 86: 'Snehove prehnanky',
    95: 'Bourka', 96: 'Bourka s krupobitim', 99: 'Bourka s krupobitim',
}


def strip_diacritics(text):
    return text.translate(_DIACRITICS_TABLE)


def geocode_city(name):
    resp = requests.get(
        'https://geocoding-api.open-meteo.com/v1/search',
        params={'name': name, 'count': 1, 'language': 'cs'},
        timeout=10,
    )
    results = resp.json().get('results', [])
    if not results:
        return None
    return results[0]['latitude'], results[0]['longitude']


def get_weather_forecast(city_name, date_str):
    coords = geocode_city(city_name)
    if not coords:
        return None
    lat, lon = coords
    resp = requests.get(
        'https://api.open-meteo.com/v1/forecast',
        params={
            'latitude': lat, 'longitude': lon,
            'daily': 'temperature_2m_max,temperature_2m_min,precipitation_probability_max,weather_code',
            'hourly': 'temperature_2m',
            'timezone': 'Europe/Prague',
        },
        timeout=10,
    )
    data = resp.json()
    daily = data.get('daily', {})
    times = daily.get('time', [])
    if date_str not in times:
        return None
    idx = times.index(date_str)
    hourly = data.get('hourly', {})
    hourly_times = hourly.get('time', [])
    hourly_temps = hourly.get('temperature_2m', [])
    day_temps = []
    for ht, temp in zip(hourly_times, hourly_temps):
        if ht.startswith(date_str) and temp is not None:
            hour = int(ht[11:13])
            if 7 <= hour <= 18:
                day_temps.append(temp)
    temp_avg = round(sum(day_temps) / len(day_temps), 1) if day_temps else (daily['temperature_2m_max'][idx] + daily['temperature_2m_min'][idx]) / 2
    return {
        'city': city_name,
        'temp_avg': temp_avg,
        'weather_code': daily['weather_code'][idx],
        'precip_prob': daily['precipitation_probability_max'][idx],
    }


WMO_SEVERITY = {
    0: 0, 1: 1, 2: 2, 3: 3, 45: 4, 48: 4,
    51: 5, 53: 5, 55: 6, 56: 6, 57: 7,
    61: 7, 63: 8, 65: 9, 66: 8, 67: 9,
    71: 7, 73: 8, 75: 9, 77: 7,
    80: 7, 81: 8, 82: 9, 85: 7, 86: 8,
    95: 10, 96: 11, 99: 11,
}


def compose_weather_sms(etapa, weather_start, weather_end):
    date_raw = etapa['date'] or ''
    date_part = date_raw.split('(')[0].strip()
    try:
        dt = datetime.strptime(date_part, '%d.%m.%Y')
        date_short = f'{dt.day}.{dt.month}.'
    except ValueError:
        date_short = date_part

    ws, we = weather_start, weather_end
    temp_avg = round((ws['temp_avg'] + we['temp_avg']) / 2)
    sev_s = WMO_SEVERITY.get(ws['weather_code'], 0)
    sev_e = WMO_SEVERITY.get(we['weather_code'], 0)
    better_code = ws['weather_code'] if sev_s <= sev_e else we['weather_code']
    worse_code = ws['weather_code'] if sev_s >= sev_e else we['weather_code']
    bad_threshold = 5
    s_bad = sev_s >= bad_threshold
    e_bad = sev_e >= bad_threshold
    base_desc = WMO_DESCRIPTIONS.get(better_code, '?')
    worse_desc = WMO_DESCRIPTIONS.get(worse_code, '?')

    lines = ['Predpoved pocasi']
    lines.append(f'Cykloexpedice Den {etapa["number"]} ({date_short})')
    lines.append(f'{ws["city"]} - {we["city"]}')

    if s_bad and e_bad:
        lines.append(f'{worse_desc} na cele trase.')
    elif s_bad and not e_bad:
        lines.append(f'{base_desc}, {worse_desc.lower()} v okoli {ws["city"]}.')
    elif e_bad and not s_bad:
        lines.append(f'{base_desc}, {worse_desc.lower()} v okoli {we["city"]}.')
    else:
        lines.append(f'{base_desc}.')

    lines.append(f'Teplota pres den: {temp_avg}°C.')
    lines.append('Prijemnou jizdu!')
    return strip_diacritics('\n'.join(lines))


def send_sms(phone_number, message):
    sid = os.environ.get('TWILIO_ACCOUNT_SID')
    token = os.environ.get('TWILIO_AUTH_TOKEN')
    from_number = os.environ.get('TWILIO_PHONE_NUMBER')
    if not all([sid, token, from_number]):
        print(f'[SMS STUB] To: {phone_number}\n{message}')
        return False
    from twilio.rest import Client  # noqa: E402
    client = Client(sid, token)
    client.messages.create(body=message, from_=from_number, to=phone_number)
    return True


def send_weather_sms_for_etapa(etapa_number):
    with app.app_context():
        db = get_db()
        etapa = db.execute('SELECT * FROM etapy WHERE number = ?', (etapa_number,)).fetchone()
        if not etapa or not etapa['route_start'] or not etapa['route_end']:
            print(f'[SMS] Etapa {etapa_number}: missing route_start/route_end, skipping')
            return 0
        today = datetime.now(ZoneInfo('Europe/Prague')).strftime('%Y-%m-%d')
        w_start = get_weather_forecast(etapa['route_start'], today)
        w_end = get_weather_forecast(etapa['route_end'], today)
        if not w_start or not w_end:
            print(f'[SMS] Etapa {etapa_number}: weather fetch failed')
            return 0
        message = compose_weather_sms(etapa, w_start, w_end)
        recipients = db.execute(
            "SELECT phone FROM registrace WHERE status = 'approved' AND phone IS NOT NULL AND phone != ''",
        ).fetchall()
        sent = 0
        for r in recipients:
            phone = r['phone']
            if not phone.startswith('+'):
                phone = '+420' + phone
            if send_sms(phone, message):
                sent += 1
            _time.sleep(1)
        db.execute(
            "INSERT OR REPLACE INTO site_settings (key, value) VALUES (?, ?)",
            (f'sms_sent_etapa_{etapa_number}', datetime.now(ZoneInfo('Europe/Prague')).isoformat()),
        )
        db.commit()
        print(f'[SMS] Etapa {etapa_number}: sent to {sent}/{len(recipients)} recipients')
        return sent


# ── Email template helpers ────────────────────────────────────

_EMAIL_LOGO_URL = 'https://cykloexpedice.cz/static/logo-email.png'


def render_email_layout(body_html, settings):
    """Wrap email body in a branded table-based layout for email client compatibility."""
    event_name = settings.get('event_name', 'Cykloexpedice')
    event_year = settings.get('event_year', '')
    contact_email = settings.get('contact_email', '')
    contact_first_1 = settings.get('contact_name_1', '').split()[0] if settings.get('contact_name_1') else ''
    contact_first_2 = settings.get('contact_name_2', '').split()[0] if settings.get('contact_name_2') else ''
    return f"""<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta http-equiv="X-UA-Compatible" content="IE=edge">
<title>{event_name}</title>
<!--[if mso]><style>table,td{{font-family:Arial,sans-serif!important;}}</style><![endif]-->
</head>
<body style="margin:0;padding:0;background-color:#1c1c1b;-webkit-text-size-adjust:100%;-ms-text-size-adjust:100%;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background-color:#1c1c1b;">
<tr><td align="center" style="padding:20px 10px;">

<!-- Main container -->
<table role="presentation" width="600" cellpadding="0" cellspacing="0" border="0" style="max-width:600px;width:100%;">

<!-- Header -->
<tr><td align="center" bgcolor="#1c1c1b" style="background-color:#1c1c1b;padding:36px 40px 28px;">
  <table role="presentation" cellpadding="0" cellspacing="0" border="0"><tr><td align="center">
    <img src="{_EMAIL_LOGO_URL}" alt="{event_name}" width="220" style="display:block;max-width:220px;width:100%;height:auto;">
  </td></tr>
  <tr><td align="center" style="padding-top:12px;">
    <p style="font-family:'Montserrat',Arial,Helvetica,sans-serif;color:#6b7280;font-size:13px;margin:0;letter-spacing:2px;text-transform:uppercase;">{event_year}</p>
  </td></tr></table>
</td></tr>

<!-- Gold accent line -->
<tr><td style="height:4px;background-color:#fbb01f;font-size:1px;line-height:1px;">&nbsp;</td></tr>

<!-- Body -->
<tr><td bgcolor="#fefdf8" style="background-color:#fefdf8;padding:40px 40px 44px;font-family:'Montserrat',Arial,Helvetica,sans-serif;font-size:15px;line-height:26px;color:#374151;">
  {body_html}
</td></tr>

<!-- Footer divider -->
<tr><td style="height:2px;background-color:#fbb01f33;font-size:1px;line-height:1px;">&nbsp;</td></tr>

<!-- Footer -->
<tr><td bgcolor="#2a2a29" style="background-color:#2a2a29;padding:28px 40px;text-align:center;">
  <img src="https://cykloexpedice.cz/static/badges/wachau_2026.png" alt="Wachau 2026" width="80" style="display:block;width:80px;height:80px;border-radius:50%;margin:0 auto 16px;">
  <p style="font-family:'Montserrat',Arial,Helvetica,sans-serif;color:#9ca3af;font-size:13px;margin:0 0 6px;font-weight:600;">{contact_first_1} a {contact_first_2}</p>
  <a href="mailto:{contact_email}" style="font-family:'Montserrat',Arial,Helvetica,sans-serif;color:#fbb01f;font-size:13px;text-decoration:none;letter-spacing:0.5px;">{contact_email}</a>
</td></tr>

<!-- Copyright -->
<tr><td bgcolor="#1c1c1b" style="background-color:#1c1c1b;padding:16px 40px;text-align:center;">
  <p style="font-family:'Montserrat',Arial,Helvetica,sans-serif;color:#4b5563;font-size:11px;margin:0;">&copy; 2014&ndash;2026 Cykloexpedice</p>
</td></tr>

</table>
</td></tr></table>
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

        email_items = []
        if transactions:
            pending = db.execute(
                "SELECT id, name, email, variable_symbol, payment_amount FROM registrace WHERE payment_status = 'pending'"
            ).fetchall()
            vs_map = {str(r['variable_symbol']): r for r in pending if r['variable_symbol']}
            settings_rows = db.execute('SELECT key, value FROM site_settings').fetchall()
            settings = {row['key']: row['value'] for row in settings_rows}

            for tx in transactions:
                amount_col = tx.get('column1')
                vs_col = tx.get('column5')
                if not amount_col or not vs_col:
                    continue
                amount = amount_col.get('value', 0) if amount_col else 0
                vs_val = str(vs_col.get('value', '')) if vs_col else ''
                vs_val = vs_val.lstrip('0') or '0'
                if amount > 0 and vs_val in vs_map:
                    reg = vs_map[vs_val]
                    if amount >= (reg['payment_amount'] or 0):
                        db.execute(
                            "UPDATE registrace SET payment_status = 'paid' WHERE id = ?",
                            (reg['id'],)
                        )
                        print(f'[FIO] Payment matched: VS={vs_val}, amount={amount}')
                        if reg['email']:
                            tpl_vars = {
                                'name': reg['name'],
                                'event_name': settings.get('event_name', 'Cykloexpedice'),
                                'event_year': settings.get('event_year', ''),
                                'contact_name_1': settings.get('contact_name_1', ''),
                                'contact_name_2': settings.get('contact_name_2', ''),
                                'contact_email': settings.get('contact_email', ''),
                            }
                            subject, html = render_email_template('email_payment_confirmed', tpl_vars, settings)
                            email_items.append((reg['email'], subject, html))

        db.execute(
            "INSERT INTO site_settings (key, value) VALUES ('last_payment_check', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (datetime.now(PRAGUE_TZ).strftime('%H:%M:%S'),)
        )
        db.commit()
        if email_items:
            send_bulk_emails(email_items)
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


def youtube_to_embed(url):
    """Convert various YouTube URL formats to embeddable nocookie URL."""
    m = re.match(r'(?:https?://)?(?:www\.)?youtube\.com/watch\?v=([A-Za-z0-9_-]+)', url)
    if not m:
        m = re.match(r'(?:https?://)?youtu\.be/([A-Za-z0-9_-]+)', url)
    if not m:
        m = re.match(r'(?:https?://)?(?:www\.)?youtube\.com/embed/([A-Za-z0-9_-]+)', url)
    if not m:
        m = re.match(r'(?:https?://)?(?:www\.)?youtube-nocookie\.com/embed/([A-Za-z0-9_-]+)', url)
    if m:
        return f'https://www.youtube-nocookie.com/embed/{m.group(1)}'
    return None


def save_file(file, prefix=''):
    if file and file.filename:
        ext = os.path.splitext(file.filename)[1].lower().lstrip('.')
        if ext not in ALLOWED_UPLOAD_EXTENSIONS:
            return None
        filename = f"{prefix}{secrets.token_hex(8)}.{ext}"
        file.save(os.path.join(app.config['UPLOAD_FOLDER'], filename))
        return filename
    return None


# ── Context processor ─────────────────────────────────────────────

@app.context_processor
def inject_globals():
    try:
        db = get_db()
        etapy = db.execute('SELECT number, title, distance, route, color FROM etapy ORDER BY number').fetchall()
        settings = get_settings()
    except Exception:
        etapy = []
        settings = {}
    maintenance_active = False
    if settings.get('maintenance_enabled') == '1':
        until = settings.get('maintenance_until', '')
        try:
            deadline = datetime.fromisoformat(until)
            if datetime.now() < deadline:
                maintenance_active = True
            else:
                db.execute("UPDATE site_settings SET value = '0' WHERE key = 'maintenance_enabled'")
                db.commit()
                settings['maintenance_enabled'] = '0'
        except (ValueError, TypeError):
            pass
    try:
        approved = db.execute("SELECT COUNT(*) c FROM registrace WHERE status = 'approved'").fetchone()['c']
    except Exception:
        approved = 0
    try:
        max_capacity = int(settings.get('max_capacity', '30'))
    except (ValueError, TypeError):
        max_capacity = 30
    registration_full = approved >= max_capacity
    return dict(etapy_nav=etapy, settings=settings, now=datetime.now(),
                maintenance_active=maintenance_active, registration_full=registration_full,
                app_version=VERSION)


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


@app.errorhandler(403)
def forbidden(e):
    return _render_error(403, 'Přístup odepřen',
                         'Nemáte oprávnění pro přístup k tomuto zdroji.')


@app.errorhandler(405)
def method_not_allowed(e):
    return _render_error(405, 'Metoda není povolena',
                         'Tato metoda není pro danou adresu povolena.')


@app.errorhandler(429)
def ratelimit_handler(e):
    return _render_error(429, 'Příliš mnoho požadavků',
                         'Překročili jste limit požadavků. Zkuste to prosím za chvíli.')


@app.errorhandler(Exception)
def handle_exception(e):
    from werkzeug.exceptions import HTTPException
    if isinstance(e, HTTPException):
        raise e
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
    youtube_raw = json.loads(row['youtube_links']) if row['youtube_links'] else []
    youtube = [u for u in (youtube_to_embed(u) for u in youtube_raw) if u]
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


@app.route('/ochrana-osobnich-udaju')
def ochrana_osobnich_udaju():
    return render_template('ochrana_osobnich_udaju.html')


@app.route('/registrace', methods=['GET', 'POST'])
@limiter.limit("5 per minute", methods=["POST"])
def registrace():
    if request.method == 'POST':
        db = get_db()
        approved = db.execute("SELECT COUNT(*) c FROM registrace WHERE status = 'approved'").fetchone()['c']
        settings = get_settings()
        try:
            max_capacity = int(settings.get('max_capacity', '30'))
        except (ValueError, TypeError):
            max_capacity = 30
        if approved >= max_capacity:
            flash('Kapacita expedice je naplněna.', 'error')
            return redirect(url_for('registrace'))
        name = request.form.get('name', '').strip()
        email = request.form.get('email', '').strip()
        phone = request.form.get('phone', '').strip()
        note = request.form.get('note', '').strip()
        if not name:
            flash('Jméno je povinné.', 'error')
            return render_template('registrace.html')
        if not email:
            flash('E-mail je povinný.', 'error')
            return render_template('registrace.html')
        email_domain = email.rsplit('@', 1)[-1].lower() if '@' in email else ''
        if not email_domain or not any(email_domain.endswith(tld) for tld in ALLOWED_EMAIL_TLDS):
            flash('Neplatná e-mailová adresa.', 'error')
            return render_template('registrace.html')
        if phone:
            cleaned = phone.replace(' ', '').replace('-', '')
            if cleaned.startswith('+'):
                if not cleaned.startswith('+420') and not cleaned.startswith('+421'):
                    flash('Povolená předvolba je pouze +420 nebo +421.', 'error')
                    return render_template('registrace.html')
                digits_after = cleaned[4:]
                if not digits_after.isdigit() or len(digits_after) != 9:
                    flash('Neplatné telefonní číslo.', 'error')
                    return render_template('registrace.html')
            else:
                if not cleaned.isdigit() or len(cleaned) != 9:
                    flash('Telefonní číslo musí mít 9 číslic.', 'error')
                    return render_template('registrace.html')
        if not request.form.get('gdpr_consent'):
            flash('Souhlas se zpracováním osobních údajů je povinný.', 'error')
            return render_template('registrace.html')
        existing = db.execute(
            'SELECT id FROM registrace WHERE email = ?', (email,)
        ).fetchone()
        if existing:
            flash('Tato e-mailová adresa je již registrována. Pokud máte dotazy, kontaktujte nás.', 'error')
            return render_template('registrace.html')
        db.execute(
            'INSERT INTO registrace (name, email, phone, note) VALUES (?, ?, ?, ?)',
            (name, email, phone, note)
        )
        db.commit()

        # Send confirmation email
        settings = get_settings()
        if email:
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

        # Notify admins about new registration
        try:
            pending_count = db.execute("SELECT COUNT(*) c FROM registrace WHERE status = 'pending'").fetchone()['c']
            event_name = settings.get('event_name', 'Cykloexpedice')
            event_year = settings.get('event_year', '')
            notif_subject = f'Nová přihláška – {event_name} {event_year}'
            safe_name = html_escape(name)
            safe_email = html_escape(email)
            safe_phone = html_escape(phone) if phone else '–'
            safe_note = html_escape(note) if note else '–'
            notif_body = (
                '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">'
                '<tr><td align="center" style="padding-bottom:24px;">'
                '<table role="presentation" cellpadding="0" cellspacing="0" border="0"><tr>'
                '<td style="background-color:#fbb01f;padding:8px 24px;font-family:Arial,sans-serif;font-size:13px;font-weight:700;color:#1c1c1b;text-transform:uppercase;letter-spacing:2px;">NOVÁ PŘIHLÁŠKA</td>'
                '</tr></table></td></tr>'
                '<tr><td style="font-family:Arial,Helvetica,sans-serif;font-size:15px;line-height:26px;color:#374151;">'
                f'<p style="margin:0 0 20px;">Na expedici <strong>{html_escape(event_name)} {html_escape(event_year)}</strong> se přihlásil nový účastník.</p>'
                '</td></tr>'
                '<tr><td style="padding:0 0 20px;">'
                '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="border:1px solid #e5e7eb;">'
                '<tr><td bgcolor="#f9fafb" style="background-color:#f9fafb;padding:20px;">'
                '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">'
                f'<tr><td style="padding:8px 0;color:#6b7280;font-family:Arial,Helvetica,sans-serif;font-size:15px;">Jméno:</td><td style="padding:8px 0;font-weight:700;font-family:Arial,Helvetica,sans-serif;font-size:15px;">{safe_name}</td></tr>'
                f'<tr><td style="padding:8px 0;color:#6b7280;font-family:Arial,Helvetica,sans-serif;font-size:15px;">E-mail:</td><td style="padding:8px 0;font-weight:700;font-family:Arial,Helvetica,sans-serif;font-size:15px;">{safe_email}</td></tr>'
                f'<tr><td style="padding:8px 0;color:#6b7280;font-family:Arial,Helvetica,sans-serif;font-size:15px;">Telefon:</td><td style="padding:8px 0;font-weight:700;font-family:Arial,Helvetica,sans-serif;font-size:15px;">{safe_phone}</td></tr>'
                f'<tr><td style="padding:8px 0;color:#6b7280;font-family:Arial,Helvetica,sans-serif;font-size:15px;">Poznámka:</td><td style="padding:8px 0;font-weight:700;font-family:Arial,Helvetica,sans-serif;font-size:15px;">{safe_note}</td></tr>'
                '</table></td></tr></table></td></tr>'
                '<tr><td style="font-family:Arial,Helvetica,sans-serif;font-size:13px;color:#6b7280;">'
                f'<p style="margin:0;">Celkem čeká na schválení: <strong>{pending_count}</strong></p>'
                '</td></tr></table>'
            )
            notif_html = render_email_layout(notif_body, settings)
            for admin_email in ADMIN_NOTIFICATION_EMAILS:
                send_email(admin_email, notif_subject, notif_html)
        except Exception as e:
            print(f'[NOTIFICATION ERROR] {e}')

        flash('Děkujeme za přihlášku! Ozveme se ti.', 'success')
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
            session.permanent = True
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
        if len(password) < 8:
            flash('Heslo musí mít alespoň 8 znaků.', 'error')
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


@app.route('/admin/logout', methods=['POST'])
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
        if len(new_pw) < 8:
            flash('Nové heslo musí mít alespoň 8 znaků.', 'error')
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
        db.commit()
        flash('Profil uložen.', 'success')
        return redirect(url_for('admin_profile'))

    return render_template('admin/profile.html', admin=admin)


# ── User auth ────────────────────────────────────────────────────

def user_login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('user_login'))
        return f(*args, **kwargs)
    return decorated


@app.route('/login', methods=['GET', 'POST'])
@limiter.limit("10 per minute", methods=["POST"])
def user_login():
    if request.method == 'POST':
        action = request.form.get('action', '')
        email = request.form.get('email', '').strip().lower()

        if action == 'check_email':
            if not email:
                flash('Zadejte e-mail.', 'error')
                return render_template('user/login.html', step='email')
            db = get_db()
            user = db.execute('SELECT id FROM users WHERE email = ?', (email,)).fetchone()
            if user:
                return render_template('user/login.html', step='login', email=email)
            return render_template('user/login.html', step='register', email=email)

        elif action == 'login':
            password = request.form.get('password', '')
            db = get_db()
            user = db.execute('SELECT * FROM users WHERE email = ?', (email,)).fetchone()
            if user and bcrypt.checkpw(password.encode(), user['password_hash'].encode()):
                session.permanent = True
                session['user_id'] = user['id']
                session['user_name'] = user['name']
                return redirect(url_for('user_app'))
            flash('Neplatné heslo.', 'error')
            return render_template('user/login.html', step='login', email=email)

        elif action == 'register':
            name = request.form.get('name', '').strip()
            password = request.form.get('password', '')
            password2 = request.form.get('password2', '')

            if not name or not email or not password:
                flash('Vyplňte všechna pole.', 'error')
                return render_template('user/login.html', step='register', email=email)
            if len(password) < 8:
                flash('Heslo musí mít alespoň 8 znaků.', 'error')
                return render_template('user/login.html', step='register', email=email)
            if password != password2:
                flash('Hesla se neshodují.', 'error')
                return render_template('user/login.html', step='register', email=email)

            db = get_db()
            existing = db.execute('SELECT id FROM users WHERE email = ?', (email,)).fetchone()
            if existing:
                flash('Účet s tímto e-mailem již existuje.', 'error')
                return render_template('user/login.html', step='login', email=email)

            hashed = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
            db.execute('INSERT INTO users (name, email, password_hash) VALUES (?, ?, ?)',
                       (name, email, hashed))
            db.commit()

            user = db.execute('SELECT * FROM users WHERE email = ?', (email,)).fetchone()
            session.permanent = True
            session['user_id'] = user['id']
            session['user_name'] = user['name']
            return redirect(url_for('user_app'))

    return render_template('user/login.html', step='email')


@app.route('/logout', methods=['POST'])
def user_logout():
    session.pop('user_id', None)
    session.pop('user_name', None)
    return redirect(url_for('user_login'))


# ── User forgot / reset password ─────────────────────────────────

@app.route('/forgot-password', methods=['GET', 'POST'])
@limiter.limit("3 per minute", methods=["POST"])
def user_forgot_password():
    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        db = get_db()
        user = db.execute('SELECT * FROM users WHERE email = ?', (email,)).fetchone()

        if user:
            token = secrets.token_urlsafe(48)
            expires = datetime.now().timestamp() + 3600
            db.execute(
                'INSERT INTO user_password_reset_tokens (user_id, token, expires_at) VALUES (?, ?, ?)',
                (user['id'], token, datetime.fromtimestamp(expires))
            )
            db.commit()

            reset_url = request.host_url.rstrip('/') + url_for('user_reset_password', token=token)
            settings = get_settings()
            body = (
                '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">'
                '<tr><td align="center" style="padding-bottom:24px;">'
                '<table role="presentation" cellpadding="0" cellspacing="0" border="0"><tr>'
                '<td style="background-color:#fbb01f;padding:8px 24px;font-family:Arial,sans-serif;font-size:13px;font-weight:700;color:#1c1c1b;text-transform:uppercase;letter-spacing:2px;">OBNOVENÍ HESLA</td>'
                '</tr></table></td></tr>'
                '<tr><td style="font-family:Arial,Helvetica,sans-serif;font-size:15px;line-height:26px;color:#374151;">'
                f'<p style="margin:0 0 16px;">Dobrý den, <strong>{html_escape(user["name"])}</strong>,</p>'
                '<p style="margin:0 0 16px;">obdrželi jsme žádost o obnovení hesla k vašemu účtu na Cykloexpedici.</p>'
                '<p style="margin:0 0 24px;">Klikněte na tlačítko níže pro nastavení nového hesla:</p>'
                '</td></tr>'
                '<tr><td align="center" style="padding:0 0 28px;">'
                '<!--[if mso]><v:roundrect xmlns:v="urn:schemas-microsoft-com:vml" href="' + reset_url + '" '
                'style="height:48px;v-text-anchor:middle;width:280px;" arcsize="50%" fillcolor="#fbb01f">'
                '<center style="color:#1c1c1b;font-family:Arial,sans-serif;font-size:14px;font-weight:bold;letter-spacing:1px;">NASTAVIT NOV&Eacute; HESLO</center>'
                '</v:roundrect><![endif]-->'
                '<!--[if !mso]><!-->'
                f'<a href="{reset_url}" style="background-color:#fbb01f;color:#1c1c1b;padding:14px 36px;'
                'text-decoration:none;font-weight:700;font-size:14px;font-family:Arial,sans-serif;display:inline-block;letter-spacing:1px;">'
                'NASTAVIT NOV&Eacute; HESLO</a>'
                '<!--<![endif]-->'
                '</td></tr>'
                '<tr><td style="font-family:Arial,Helvetica,sans-serif;font-size:13px;line-height:22px;color:#999999;">'
                '<p style="margin:0 0 16px;">Tento odkaz je platný 1 hodinu. Pokud jste o obnovení hesla nežádali, tento e-mail ignorujte.</p>'
                f'<p style="margin:0;font-size:12px;color:#cccccc;word-break:break-all;">Pokud tlačítko nefunguje, zkopírujte tento odkaz do prohlížeče:<br>{reset_url}</p>'
                '</td></tr></table>'
            )
            html = render_email_layout(body, settings)
            send_email(user['email'], 'Obnovení hesla – Cykloexpedice', html)

        flash('Pokud účet s tímto e-mailem existuje, odeslali jsme odkaz pro obnovení hesla.', 'success')
        return redirect(url_for('user_login'))

    return render_template('user/forgot_password.html')


@app.route('/reset-password/<token>', methods=['GET', 'POST'])
@limiter.limit("5 per minute", methods=["POST"])
def user_reset_password(token):
    db = get_db()
    row = db.execute(
        'SELECT * FROM user_password_reset_tokens WHERE token = ? AND used = 0', (token,)
    ).fetchone()

    if not row:
        flash('Odkaz pro obnovení hesla je neplatný nebo vypršel.', 'error')
        return redirect(url_for('user_login'))

    expires = row['expires_at']
    if isinstance(expires, str):
        expires = datetime.fromisoformat(expires)
    if expires < datetime.now():
        flash('Odkaz pro obnovení hesla je neplatný nebo vypršel.', 'error')
        return redirect(url_for('user_login'))

    if request.method == 'POST':
        password = request.form.get('password', '')
        password2 = request.form.get('password2', '')

        if len(password) < 8:
            flash('Heslo musí mít alespoň 8 znaků.', 'error')
            return render_template('user/reset_password.html', token=token)
        if password != password2:
            flash('Hesla se neshodují.', 'error')
            return render_template('user/reset_password.html', token=token)

        hashed = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
        db.execute('UPDATE users SET password_hash = ? WHERE id = ?', (hashed, row['user_id']))
        db.execute('UPDATE user_password_reset_tokens SET used = 1 WHERE id = ?', (row['id'],))
        db.execute('UPDATE user_password_reset_tokens SET used = 1 WHERE user_id = ? AND id != ?',
                   (row['user_id'], row['id']))
        db.commit()

        flash('Heslo bylo úspěšně změněno. Přihlaste se.', 'success')
        return redirect(url_for('user_login'))

    return render_template('user/reset_password.html', token=token)


# ── Strava OAuth2 ────────────────────────────────────────────────

def _refresh_strava_token(db, user):
    resp = requests.post('https://www.strava.com/oauth/token', data={
        'client_id': STRAVA_CLIENT_ID,
        'client_secret': STRAVA_CLIENT_SECRET,
        'grant_type': 'refresh_token',
        'refresh_token': user['strava_refresh_token'],
    }, timeout=15)
    if resp.status_code != 200:
        return None
    data = resp.json()
    db.execute('''UPDATE users SET strava_access_token = ?, strava_refresh_token = ?,
                  strava_expires_at = ? WHERE id = ?''',
               (data['access_token'], data['refresh_token'], data['expires_at'], user['id']))
    db.commit()
    return data['access_token']


def _get_strava_token(db, user):
    if not user['strava_access_token']:
        return None
    if user['strava_expires_at'] and int(_time.time()) >= user['strava_expires_at']:
        return _refresh_strava_token(db, user)
    return user['strava_access_token']


_PHONE_PATTERNS = ('iphone', 'android', 'strava iphone', 'strava android')


def _is_phone_activity(device_name):
    if not device_name:
        return False
    dn = device_name.lower()
    return any(p in dn for p in _PHONE_PATTERNS)


def _get_device_name(activity_id, token, db, user_id):
    row = db.execute(
        'SELECT device_name FROM activity_device_cache WHERE user_id = ? AND strava_activity_id = ?',
        (user_id, activity_id)).fetchone()
    if row:
        return row['device_name']
    try:
        resp = requests.get(f'https://www.strava.com/api/v3/activities/{activity_id}',
                            headers={'Authorization': f'Bearer {token}'}, timeout=15)
        if resp.status_code == 200:
            device_name = resp.json().get('device_name') or ''
        else:
            device_name = ''
    except requests.RequestException:
        device_name = ''
    db.execute('INSERT INTO activity_device_cache (user_id, strava_activity_id, device_name) VALUES (?, ?, ?)',
               (user_id, activity_id, device_name))
    db.commit()
    return device_name


def _dedup_rides(rides, token, db, user_id, hidden_ids):
    from collections import defaultdict
    seen_ids = set()
    unique_rides = []
    for r in rides:
        if r['id'] not in seen_ids:
            seen_ids.add(r['id'])
            unique_rides.append(r)
    rides = unique_rides
    by_date = defaultdict(list)
    for r in rides:
        by_date[r.get('start_date_local', '')[:10]].append(r)

    dominated = set()
    for date_key, group in by_date.items():
        if len(group) < 2:
            continue
        dupe_pairs = []
        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                d1, d2 = group[i]['distance'], group[j]['distance']
                if d1 and d2 and min(d1, d2) / max(d1, d2) >= 0.8:
                    dupe_pairs.append((i, j))
        if not dupe_pairs:
            continue
        dupe_indices = set()
        for i, j in dupe_pairs:
            dupe_indices.add(i)
            dupe_indices.add(j)
        devices = {}
        for idx in dupe_indices:
            a = group[idx]
            devices[idx] = _get_device_name(a['id'], token, db, user_id)
        phone_idx = {idx for idx in dupe_indices if _is_phone_activity(devices[idx])}
        device_idx = dupe_indices - phone_idx
        if phone_idx and device_idx:
            for idx in phone_idx:
                aid = group[idx]['id']
                dominated.add(aid)
                if aid not in hidden_ids:
                    existing = db.execute(
                        'SELECT id FROM hidden_activities WHERE user_id = ? AND strava_activity_id = ?',
                        (user_id, aid)).fetchone()
                    if not existing:
                        db.execute('INSERT INTO hidden_activities (user_id, strava_activity_id) VALUES (?, ?)',
                                   (user_id, aid))
                        db.commit()
                    hidden_ids.add(aid)

    return [r for r in rides if r['id'] not in dominated]


@app.route('/strava/connect')
@user_login_required
def strava_connect():
    if not STRAVA_CLIENT_ID:
        flash('Strava není nakonfigurována.', 'error')
        return redirect(url_for('user_app'))
    state = secrets.token_urlsafe(32)
    session['strava_oauth_state'] = state
    callback = url_for('strava_callback', _external=True)
    url = (f'https://www.strava.com/oauth/authorize?client_id={STRAVA_CLIENT_ID}'
           f'&redirect_uri={callback}&response_type=code'
           f'&scope=activity:read&approval_prompt=auto&state={state}')
    return redirect(url)


@app.route('/strava/callback')
@user_login_required
def strava_callback():
    state = request.args.get('state', '')
    expected = session.pop('strava_oauth_state', None)
    if not expected or state != expected:
        flash('Neplatný OAuth stav. Zkuste to znovu.', 'error')
        return redirect(url_for('user_app'))
    code = request.args.get('code')
    if not code:
        flash('Autorizace Strava selhala.', 'error')
        return redirect(url_for('user_app'))

    resp = requests.post('https://www.strava.com/oauth/token', data={
        'client_id': STRAVA_CLIENT_ID,
        'client_secret': STRAVA_CLIENT_SECRET,
        'code': code,
        'grant_type': 'authorization_code',
    }, timeout=15)

    if resp.status_code != 200:
        flash('Nepodařilo se připojit ke Strava.', 'error')
        return redirect(url_for('user_app'))

    data = resp.json()
    db = get_db()
    db.execute('''UPDATE users SET strava_athlete_id = ?, strava_access_token = ?,
                  strava_refresh_token = ?, strava_expires_at = ? WHERE id = ?''',
               (data['athlete']['id'], data['access_token'],
                data['refresh_token'], data['expires_at'], session['user_id']))
    db.commit()
    flash('Strava byla úspěšně propojena!', 'success')
    return redirect(url_for('user_app'))


@app.route('/strava/disconnect', methods=['POST'])
@user_login_required
def strava_disconnect():
    db = get_db()
    db.execute('''UPDATE users SET strava_athlete_id = NULL, strava_access_token = NULL,
                  strava_refresh_token = NULL, strava_expires_at = NULL WHERE id = ?''',
               (session['user_id'],))
    db.commit()
    flash('Strava byla odpojena.', 'success')
    return redirect(url_for('user_app'))


@app.route('/activity/<int:activity_id>')
@user_login_required
def user_activity_detail(activity_id):
    db = get_db()
    user = db.execute('SELECT * FROM users WHERE id = ?', (session['user_id'],)).fetchone()
    if not user['strava_access_token']:
        flash('Strava není propojena.', 'error')
        return redirect(url_for('user_app'))

    token = _get_strava_token(db, user)
    if not token:
        flash('Nepodařilo se obnovit Strava token.', 'error')
        return redirect(url_for('user_app'))

    try:
        resp = requests.get(f'https://www.strava.com/api/v3/activities/{activity_id}',
                            headers={'Authorization': f'Bearer {token}'},
                            timeout=15)
        if resp.status_code != 200:
            flash('Aktivitu se nepodařilo načíst.', 'error')
            return redirect(url_for('user_app'))
        activity = resp.json()
    except requests.RequestException:
        flash('Chyba při komunikaci se Strava.', 'error')
        return redirect(url_for('user_app'))

    return render_template('user/activity_detail.html', a=activity)


# ── User dashboard ───────────────────────────────────────────────

@app.route('/dashboard')
def user_dashboard_redirect():
    return redirect(url_for('user_app'))


@app.route('/app/hide-activity', methods=['POST'])
@user_login_required
def user_hide_activity():
    activity_id = request.form.get('activity_id', type=int)
    if not activity_id:
        flash('Neplatná aktivita.', 'error')
        return redirect(url_for('user_app'))
    db = get_db()
    existing = db.execute(
        'SELECT id FROM hidden_activities WHERE user_id = ? AND strava_activity_id = ?',
        (session['user_id'], activity_id)
    ).fetchone()
    if not existing:
        db.execute(
            'INSERT INTO hidden_activities (user_id, strava_activity_id) VALUES (?, ?)',
            (session['user_id'], activity_id)
        )
        db.commit()
    return redirect(url_for('user_app'))


@app.route('/app')
@user_login_required
def user_app():
    db = get_db()
    user = db.execute('SELECT * FROM users WHERE id = ?', (session['user_id'],)).fetchone()

    registration = db.execute(
        'SELECT * FROM registrace WHERE email = ? ORDER BY created_at DESC',
        (user['email'],)
    ).fetchone()

    if registration and registration['name'] in ('Adam Přikryl', 'Michal Přikryl'):
        registration = dict(registration)
        registration['payment_status'] = 'paid'

    hidden_rows = db.execute(
        'SELECT strava_activity_id FROM hidden_activities WHERE user_id = ?',
        (session['user_id'],)
    ).fetchall()
    hidden_ids = {row['strava_activity_id'] for row in hidden_rows}

    strava_connected = bool(user['strava_access_token'])
    past_expeditions = []

    EXPEDITIONS = [
        ('Staročeská Cykloexpedice 2025', '11.–13. 9. 2025',
         ['2025-09-11', '2025-09-12', '2025-09-13']),
        ('Ještědská 2022', '15.–17. 9. 2022',
         ['2022-09-15', '2022-09-16', '2022-09-17']),
        ('Jižanská 2021', '16.–18. 9. 2021',
         ['2021-09-16', '2021-09-17', '2021-09-18']),
        ('Moravská stezka 2020', '19.–20. 9. 2020',
         ['2020-09-19', '2020-09-20']),
        ('Wien 2019', '25.–26. 5. 2019',
         ['2019-05-25', '2019-05-26']),
    ]

    if strava_connected:
        token = _get_strava_token(db, user)
        if not token:
            strava_connected = False
        else:
            headers = {'Authorization': f'Bearer {token}'}

            for exp_name, exp_dates_label, exp_date_list in EXPEDITIONS:
                rides = []
                for date_str in exp_date_list:
                    dt = datetime.strptime(date_str, '%Y-%m-%d').replace(tzinfo=timezone.utc)
                    after_ts = int(dt.timestamp())
                    before_ts = after_ts + 86400
                    try:
                        resp = requests.get('https://www.strava.com/api/v3/athlete/activities',
                                            headers=headers,
                                            params={'after': after_ts, 'before': before_ts,
                                                    'per_page': 50},
                                            timeout=15)
                        if resp.status_code == 200:
                            rides.extend(a for a in resp.json()
                                         if a.get('type') == 'Ride' and a.get('id') not in hidden_ids)
                    except requests.RequestException:
                        pass
                rides = _dedup_rides(rides, token, db, session['user_id'], hidden_ids)
                past_expeditions.append({
                    'name': exp_name,
                    'dates': exp_dates_label,
                    'rides': rides,
                })

    initials = ''
    parts = (user['name'] or '').split()
    if parts:
        initials = parts[0][0].upper()
        if len(parts) > 1:
            initials += parts[-1][0].upper()

    return render_template('user/dashboard.html', user=user,
                           strava_connected=strava_connected,
                           past_expeditions=past_expeditions,
                           registration=registration,
                           initials=initials)


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
            body = (
                '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">'
                '<tr><td align="center" style="padding-bottom:24px;">'
                '<table role="presentation" cellpadding="0" cellspacing="0" border="0"><tr>'
                '<td style="background-color:#fbb01f;padding:8px 24px;font-family:Arial,sans-serif;font-size:13px;font-weight:700;color:#1c1c1b;text-transform:uppercase;letter-spacing:2px;">OBNOVENÍ HESLA</td>'
                '</tr></table></td></tr>'
                '<tr><td style="font-family:Arial,Helvetica,sans-serif;font-size:15px;line-height:26px;color:#374151;">'
                f'<p style="margin:0 0 16px;">Dobrý den, <strong>{html_escape(admin["username"])}</strong>,</p>'
                '<p style="margin:0 0 16px;">obdrželi jsme žádost o obnovení hesla k vašemu účtu v administraci Cykloexpedice.</p>'
                '<p style="margin:0 0 24px;">Klikněte na tlačítko níže pro nastavení nového hesla:</p>'
                '</td></tr>'
                '<tr><td align="center" style="padding:0 0 28px;">'
                '<!--[if mso]><v:roundrect xmlns:v="urn:schemas-microsoft-com:vml" href="' + reset_url + '" '
                'style="height:48px;v-text-anchor:middle;width:280px;" arcsize="50%" fillcolor="#fbb01f">'
                '<center style="color:#1c1c1b;font-family:Arial,sans-serif;font-size:14px;font-weight:bold;letter-spacing:1px;">NASTAVIT NOV&Eacute; HESLO</center>'
                '</v:roundrect><![endif]-->'
                '<!--[if !mso]><!-->'
                f'<a href="{reset_url}" style="background-color:#fbb01f;color:#1c1c1b;padding:14px 36px;'
                'text-decoration:none;font-weight:700;font-size:14px;font-family:Arial,sans-serif;display:inline-block;letter-spacing:1px;">'
                'NASTAVIT NOV&Eacute; HESLO</a>'
                '<!--<![endif]-->'
                '</td></tr>'
                '<tr><td style="font-family:Arial,Helvetica,sans-serif;font-size:13px;line-height:22px;color:#999999;">'
                '<p style="margin:0 0 16px;">Tento odkaz je platný 1 hodinu. Pokud jste o obnovení hesla nežádali, tento e-mail ignorujte.</p>'
                f'<p style="margin:0;font-size:12px;color:#cccccc;word-break:break-all;">Pokud tlačítko nefunguje, zkopírujte tento odkaz do prohlížeče:<br>{reset_url}</p>'
                '</td></tr></table>'
            )
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

        if len(password) < 8:
            flash('Heslo musí mít alespoň 8 znaků.', 'error')
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

        flash('Heslo bylo úspěšně změněno. Přihlas se.', 'success')
        return redirect(url_for('admin_login'))

    return render_template('admin/reset_password.html', token=token)


# ── Admin dashboard ───────────────────────────────────────────────

@app.route('/admin')
@login_required
def admin_dashboard():
    db = get_db()
    today_str = date.today().isoformat()
    stats = {
        'etapy': db.execute('SELECT COUNT(*) c FROM etapy').fetchone()['c'],
        'aktuality': db.execute('SELECT COUNT(*) c FROM aktuality').fetchone()['c'],
        'registrace': db.execute("SELECT COUNT(*) c FROM registrace WHERE status = 'approved'").fetchone()['c'],
        'ubytovani': db.execute('SELECT COUNT(*) c FROM ubytovani').fetchone()['c'],
        'payment_pending': db.execute("SELECT COUNT(*) c FROM registrace WHERE payment_status = 'pending'").fetchone()['c'],
        'payment_paid': db.execute("SELECT COUNT(*) c FROM registrace WHERE payment_status = 'paid'").fetchone()['c']
            + db.execute("SELECT COUNT(*) c FROM registrace WHERE name IN ('Adam Přikryl', 'Michal Přikryl') AND payment_status != 'paid'").fetchone()['c'],
        'today_views': db.execute('SELECT COUNT(*) c FROM page_views WHERE created_at >= ? AND is_bot = 0', (today_str,)).fetchone()['c'],
        'today_unique': db.execute('SELECT COUNT(DISTINCT ip_hash) c FROM page_views WHERE created_at >= ? AND is_bot = 0', (today_str,)).fetchone()['c'],
    }
    return render_template('admin/dashboard.html', stats=stats)


# ── Admin: Návštěvnost ────────────────────────────────────────────

@app.route('/admin/navstevnost')
@login_required
def admin_navstevnost():
    db = get_db()
    today = date.today()
    today_str = today.isoformat()

    period = request.args.get('period', '30')
    if period == 'today':
        start_str = today_str
    elif period == '7':
        start_str = (today - timedelta(days=7)).isoformat()
    elif period == '30':
        start_str = (today - timedelta(days=30)).isoformat()
    elif period == 'all':
        start_str = None
    else:
        start_str = (today - timedelta(days=30)).isoformat()
        period = '30'

    show_bots = request.args.get('bots') == '1'

    if start_str:
        date_clause = 'AND p.created_at >= ?'
        date_params = (start_str,)
    else:
        date_clause = ''
        date_params = ()

    bot_clause = '' if show_bots else 'AND p.is_bot = 0'

    total_views = db.execute(
        f'SELECT COUNT(*) c FROM page_views p WHERE 1=1 {bot_clause} {date_clause}', date_params,
    ).fetchone()['c']
    unique_visitors = db.execute(
        f'SELECT COUNT(DISTINCT ip_hash) c FROM page_views p WHERE 1=1 {bot_clause} {date_clause}', date_params,
    ).fetchone()['c']
    today_views = db.execute(
        f'SELECT COUNT(*) c FROM page_views p WHERE p.created_at >= ? {bot_clause}', (today_str,),
    ).fetchone()['c']
    today_unique = db.execute(
        f'SELECT COUNT(DISTINCT ip_hash) c FROM page_views p WHERE p.created_at >= ? {bot_clause}', (today_str,),
    ).fetchone()['c']
    bot_count = db.execute(
        f'SELECT COUNT(*) c FROM page_views p WHERE p.is_bot = 1 {date_clause}', date_params,
    ).fetchone()['c']

    pages = db.execute(
        f'SELECT path, COUNT(*) as views, COUNT(DISTINCT ip_hash) as unique_visitors '
        f'FROM page_views p WHERE 1=1 {bot_clause} {date_clause} GROUP BY path ORDER BY views DESC',
        date_params,
    ).fetchall()

    countries = db.execute(
        f'SELECT g.country_name, g.country_code, COUNT(*) as views, '
        f'COUNT(DISTINCT p.ip_hash) as unique_visitors '
        f'FROM page_views p JOIN ip_geolocation g ON p.ip_hash = g.ip_hash '
        f'WHERE 1=1 {bot_clause} {date_clause} '
        f'GROUP BY g.country_code, g.country_name ORDER BY views DESC LIMIT 20',
        date_params,
    ).fetchall()

    chart_start = (today - timedelta(days=29)).isoformat()
    bot_chart_clause = '' if show_bots else 'AND is_bot = 0'
    daily_rows = db.execute(
        'SELECT DATE(created_at) as day, COUNT(*) as views, '
        'COUNT(DISTINCT ip_hash) as unique_visitors '
        f'FROM page_views WHERE created_at >= ? {bot_chart_clause} '
        'GROUP BY DATE(created_at) ORDER BY day',
        (chart_start,),
    ).fetchall()

    daily_views = []
    data_map = {str(r['day']): r for r in daily_rows}
    for i in range(30):
        d = (today - timedelta(days=29 - i)).isoformat()
        row = data_map.get(d)
        daily_views.append({
            'date': d,
            'views': row['views'] if row else 0,
            'unique': row['unique_visitors'] if row else 0,
        })

    recent = db.execute(
        'SELECT p.path, p.user_agent, p.referrer, p.created_at, '
        'p.is_bot, g.country_name, g.country_code '
        'FROM page_views p LEFT JOIN ip_geolocation g ON p.ip_hash = g.ip_hash '
        f'WHERE 1=1 {bot_clause.replace("p.", "p.")} '
        'ORDER BY p.created_at DESC LIMIT 50',
    ).fetchall()

    return render_template(
        'admin/navstevnost.html',
        total_views=total_views, unique_visitors=unique_visitors,
        today_views=today_views, today_unique=today_unique,
        pages=pages, countries=countries,
        daily_views=daily_views, recent=recent, period=period,
        show_bots=show_bots, bot_count=bot_count,
    )


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
        'route_start': request.form.get('route_start', ''),
        'route_end': request.form.get('route_end', ''),
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
    notify = request.form.get('notify_participants')
    if id:
        db.execute('UPDATE aktuality SET title=?, content=?, published=? WHERE id=?',
                   (title, content, published, id))
    else:
        db.execute('INSERT INTO aktuality (title, content, published) VALUES (?,?,?)',
                   (title, content, published))
    db.commit()

    if notify and published:
        try:
            settings = get_settings()
            recipients = [r['email'] for r in db.execute(
                "SELECT email FROM registrace WHERE status = 'approved' "
                "AND email != '' AND email IS NOT NULL"
            ).fetchall()]
            if recipients:
                event_name = settings.get('event_name', 'Cykloexpedice')
                event_year = settings.get('event_year', '')
                safe_title = html_escape(title)
                aktuality_url = url_for('aktuality', _external=True)
                notif_subject = f'Nová aktualita – {event_name} {event_year}'
                notif_body = (
                    '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">'
                    '<tr><td align="center" style="padding-bottom:24px;">'
                    '<table role="presentation" cellpadding="0" cellspacing="0" border="0"><tr>'
                    '<td style="background-color:#fbb01f;padding:8px 24px;font-family:Arial,sans-serif;font-size:13px;font-weight:700;color:#1c1c1b;text-transform:uppercase;letter-spacing:2px;">NOVÁ AKTUALITA</td>'
                    '</tr></table></td></tr>'
                    '<tr><td style="font-family:Arial,Helvetica,sans-serif;font-size:15px;line-height:26px;color:#374151;">'
                    f'<p style="margin:0 0 8px;">Na webu <strong>{html_escape(event_name)} {html_escape(event_year)}</strong> byla přidána nová aktualita:</p>'
                    f'<p style="margin:0 0 24px;font-size:18px;font-weight:700;color:#1c1c1b;">{safe_title}</p>'
                    '</td></tr>'
                    '<tr><td align="center" style="padding-bottom:24px;">'
                    '<table role="presentation" cellpadding="0" cellspacing="0" border="0"><tr>'
                    f'<td style="background-color:#fbb01f;padding:12px 32px;font-family:Arial,sans-serif;font-size:14px;font-weight:700;color:#1c1c1b;border-radius:8px;">'
                    f'<a href="{aktuality_url}" style="color:#1c1c1b;text-decoration:none;">Zobrazit aktuality</a>'
                    '</td></tr></table></td></tr></table>'
                )
                notif_html = render_email_layout(notif_body, settings)
                send_bulk_notifications(recipients, notif_subject, notif_html)
                flash(f'Aktualita uložena. E-mail bude odeslán {len(recipients)} účastníkům.', 'success')
            else:
                flash('Aktualita uložena. Žádní schválení účastníci k notifikaci.', 'success')
        except Exception as e:
            print(f'[NOTIFICATION ERROR] {e}')
            flash('Aktualita uložena.', 'success')
    else:
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
    rows = db.execute(
        f"SELECT * FROM registrace {where} "
        "ORDER BY CASE status WHEN 'pending' THEN 0 WHEN 'approved' THEN 1 WHEN 'denied' THEN 2 ELSE 3 END, "
        "CASE WHEN payment_status = 'paid' THEN 0 ELSE 1 END, "
        "created_at DESC",
        params
    ).fetchall()
    patched = []
    for r in rows:
        if r['name'] in ('Adam Přikryl', 'Michal Přikryl'):
            r = dict(r)
            r['payment_status'] = 'paid'
            r['is_organizer'] = True
        patched.append(r)
    rows = patched
    counts = {
        'all': db.execute('SELECT COUNT(*) c FROM registrace').fetchone()['c'],
        'pending': db.execute("SELECT COUNT(*) c FROM registrace WHERE status = 'pending'").fetchone()['c'],
        'approved': db.execute("SELECT COUNT(*) c FROM registrace WHERE status = 'approved'").fetchone()['c'],
        'denied': db.execute("SELECT COUNT(*) c FROM registrace WHERE status = 'denied'").fetchone()['c'],
    }
    last_check = db.execute("SELECT value FROM site_settings WHERE key = 'last_payment_check'").fetchone()
    last_payment_check = last_check['value'] if last_check else None
    return render_template('admin/registrace.html', registrace=rows, query=q,
                           status_filter=status_filter, counts=counts,
                           last_payment_check=last_payment_check)


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
    send_email(reg['email'], subject, html)

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
        send_email(reg['email'], subject, html)

        flash(f'Registrace pro {reg["name"]} byla zamítnuta. E-mail odeslán.', 'success')
        return redirect(url_for('admin_registrace'))

    return render_template('admin/registrace_deny.html', reg=reg)


@app.route('/admin/registrace/<int:id>/edit', methods=['GET', 'POST'])
@login_required
def admin_registrace_edit(id):
    db = get_db()
    reg = db.execute('SELECT * FROM registrace WHERE id = ?', (id,)).fetchone()
    if not reg:
        abort(404)

    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        if not name:
            flash('Jméno nesmí být prázdné.', 'error')
            return render_template('admin/registrace_edit.html', reg=reg)
        db.execute('UPDATE registrace SET name = ? WHERE id = ?', (name, id))
        db.commit()
        flash(f'Jméno bylo změněno na „{name}".', 'success')
        return redirect(url_for('admin_registrace'))

    return render_template('admin/registrace_edit.html', reg=reg)


@app.route('/admin/registrace/bulk-delete', methods=['POST'])
@login_required
def admin_registrace_bulk_delete():
    ids = request.form.getlist('ids', type=int)
    if ids:
        db = get_db()
        placeholders = ','.join('?' * len(ids))
        db.execute(f'DELETE FROM registrace WHERE id IN ({placeholders})', ids)
        db.commit()
        flash(f'Smazáno {len(ids)} registrací.', 'success')
    return redirect(url_for('admin_registrace'))


@app.route('/admin/registrace/<int:id>/delete', methods=['POST'])
@login_required
def admin_registrace_delete(id):
    db = get_db()
    db.execute('DELETE FROM registrace WHERE id = ?', (id,))
    db.commit()
    flash('Registrace smazána.', 'success')
    return redirect(url_for('admin_registrace'))


@app.route('/admin/registrace/bulk-send-payment', methods=['POST'])
@login_required
def admin_registrace_bulk_send_payment():
    db = get_db()
    selected_ids = request.form.getlist('ids')
    amount_str = request.form.get('amount', '').strip()

    if not selected_ids:
        flash('Nejsou vybrány žádné registrace.', 'error')
        return redirect(url_for('admin_registrace'))

    try:
        amount = float(amount_str)
        if amount <= 0:
            raise ValueError
    except (ValueError, TypeError):
        flash('Neplatná částka.', 'error')
        return redirect(url_for('admin_registrace'))

    settings = get_settings()
    iban = settings.get('bank_iban', '')
    bank_account = settings.get('bank_account', '')

    if not iban:
        flash('Není nastaven IBAN. Zkontrolujte nastavení.', 'error')
        return redirect(url_for('admin_registrace'))

    placeholders = ','.join('?' * len(selected_ids))
    regs = db.execute(
        f"SELECT * FROM registrace WHERE id IN ({placeholders}) "
        "AND status = 'approved' AND email != '' AND email IS NOT NULL",
        selected_ids,
    ).fetchall()

    if not regs:
        flash('Žádné schválené registrace s e-mailem ve výběru.', 'info')
        return redirect(url_for('admin_registrace'))

    event_name = settings.get('event_name', 'Cykloexpedice')
    event_year = settings.get('event_year', '')
    email_items = []

    for reg in regs:
        vs = reg['variable_symbol'] or reg['id']
        name_parts = reg['name'].strip().split()
        payment_note = 'WACHAU_' + '_'.join(name_parts)

        qr_b64 = generate_payment_qr(iban, amount, vs, payment_note)

        db.execute(
            "UPDATE registrace SET variable_symbol = ?, payment_status = 'pending', "
            "payment_amount = ?, qr_sent_at = ? WHERE id = ?",
            (vs, amount, datetime.now(), reg['id']),
        )

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
        email_items.append((reg['email'], subject, html))

    db.commit()
    send_bulk_emails(email_items)
    flash(f'Platební QR kód bude odeslán {len(email_items)} účastníkům.', 'success')
    return redirect(url_for('admin_registrace'))


@app.route('/admin/registrace/<int:id>/resend-payment', methods=['POST'])
@login_required
def admin_registrace_resend_payment(id):
    db = get_db()
    reg = db.execute(
        "SELECT * FROM registrace WHERE id = ? AND payment_status = 'pending'", (id,)
    ).fetchone()
    if not reg:
        abort(404)

    settings = get_settings()
    iban = settings.get('bank_iban', '')
    bank_account = settings.get('bank_account', '')

    if not iban:
        flash('Není nastaven IBAN. Zkontrolujte nastavení.', 'error')
        return redirect(url_for('admin_registrace'))

    vs = reg['variable_symbol'] or reg['id']
    amount = reg['payment_amount'] or float(settings.get('payment_amount', 0))
    name_parts = reg['name'].strip().split()
    payment_note = 'WACHAU_' + '_'.join(name_parts)

    qr_b64 = generate_payment_qr(iban, amount, vs, payment_note)
    qr_html = f'<p style="text-align: center; margin: 25px 0;"><img src="data:image/png;base64,{qr_b64}" alt="QR platba" style="width: 250px; height: 250px;"></p>'

    event_name = settings.get('event_name', 'Cykloexpedice')
    event_year = settings.get('event_year', '')
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
    send_email(reg['email'], subject, html)

    flash(f'Platební údaje znovu odeslány na {reg["email"]}.', 'success')
    return redirect(url_for('admin_registrace'))


@app.route('/admin/registrace/check-payments', methods=['POST'])
@login_required
def admin_check_payments():
    check_fio_payments()
    flash('Platby zkontrolovány.', 'success')
    return redirect(url_for('admin_registrace'))


@app.route('/admin/registrace/<int:id>/mark-paid', methods=['POST'])
@login_required
def admin_registrace_mark_paid(id):
    db = get_db()
    reg = db.execute('SELECT * FROM registrace WHERE id = ?', (id,)).fetchone()
    if not reg:
        abort(404)
    settings = get_settings()
    amount = reg['payment_amount'] or float(settings.get('payment_amount', 0))
    vs = reg['variable_symbol'] or reg['id']
    db.execute(
        "UPDATE registrace SET payment_status = 'paid', variable_symbol = ?, payment_amount = ? WHERE id = ?",
        (vs, amount, id),
    )
    db.commit()
    flash(f'Platba pro {reg["name"]} byla ručně potvrzena.', 'success')
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
    {
        'key': 'email_payment_confirmed',
        'label': 'Potvrzení platby',
        'description': 'Automaticky odesláno účastníkovi po přijetí platby.',
        'placeholders': _COMMON_PLACEHOLDERS,
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
                'max_capacity', 'bank_account', 'bank_iban']
        for key in keys:
            val = request.form.get(key, '')
            db.execute('INSERT OR REPLACE INTO site_settings (key, value) VALUES (?, ?)', (key, val))
        # Checkbox toggle for fotky
        fotky_val = '1' if request.form.get('fotky_enabled') else '0'
        db.execute('INSERT OR REPLACE INTO site_settings (key, value) VALUES (?, ?)', ('fotky_enabled', fotky_val))
        # Checkbox toggle for maintenance
        maint_val = '1' if request.form.get('maintenance_enabled') else '0'
        db.execute('INSERT OR REPLACE INTO site_settings (key, value) VALUES (?, ?)', ('maintenance_enabled', maint_val))
        maint_until = request.form.get('maintenance_until', '').strip()
        if maint_until:
            db.execute('INSERT OR REPLACE INTO site_settings (key, value) VALUES (?, ?)', ('maintenance_until', maint_until))
        db.commit()
        flash('Nastavení uloženo.', 'success')
        return redirect(url_for('admin_settings'))
    return render_template('admin/settings.html')


# ── Admin: SMS ────────────────────────────────────────────────────

@app.route('/admin/sms')
@login_required
def admin_sms():
    db = get_db()
    etapy = db.execute('SELECT * FROM etapy ORDER BY number').fetchall()
    recipient_count = db.execute(
        "SELECT COUNT(*) c FROM registrace WHERE status = 'approved' AND phone IS NOT NULL AND phone != ''",
    ).fetchone()['c']
    settings = get_settings()
    sms_log = {}
    for e in etapy:
        val = settings.get(f'sms_sent_etapa_{e["number"]}')
        if val:
            sms_log[e['number']] = val
    return render_template('admin/sms.html', etapy=etapy, recipient_count=recipient_count, sms_log=sms_log)


@app.route('/admin/sms/preview/<int:etapa_number>')
@login_required
def admin_sms_preview(etapa_number):
    db = get_db()
    etapa = db.execute('SELECT * FROM etapy WHERE number = ?', (etapa_number,)).fetchone()
    if not etapa:
        flash('Etapa nenalezena.', 'error')
        return redirect(url_for('admin_sms'))
    if not etapa['route_start'] or not etapa['route_end']:
        flash('Vyplňte počáteční a koncové místo v nastavení etapy.', 'error')
        return redirect(url_for('admin_sms'))
    today = datetime.now(ZoneInfo('Europe/Prague')).strftime('%Y-%m-%d')
    w_start = get_weather_forecast(etapa['route_start'], today)
    w_end = get_weather_forecast(etapa['route_end'], today)
    error = None
    message = None
    if not w_start or not w_end:
        error = 'Nepodařilo se načíst počasí. Zkontrolujte názvy míst.'
    else:
        message = compose_weather_sms(etapa, w_start, w_end)
    recipient_count = db.execute(
        "SELECT COUNT(*) c FROM registrace WHERE status = 'approved' AND phone IS NOT NULL AND phone != ''",
    ).fetchone()['c']
    return render_template(
        'admin/sms_preview.html', etapa=etapa, message=message, error=error,
        w_start=w_start, w_end=w_end, recipient_count=recipient_count,
        message_len=len(message) if message else 0, wmo_descriptions=WMO_DESCRIPTIONS,
        forecast_date=today,
    )


@app.route('/admin/sms/send/<int:etapa_number>', methods=['POST'])
@login_required
def admin_sms_send(etapa_number):
    sent = send_weather_sms_for_etapa(etapa_number)
    if sent > 0:
        flash(f'SMS odeslána {sent} příjemcům.', 'success')
    else:
        flash('Žádné SMS nebyly odeslány. Zkontrolujte nastavení etapy a Twilio.', 'error')
    return redirect(url_for('admin_sms'))


# ── Init & Run ────────────────────────────────────────────────────

def _schedule_weather_sms():
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
    except ImportError:
        print('[SMS] APScheduler not installed, skipping scheduled SMS')
        return
    scheduler = BackgroundScheduler(timezone='Europe/Prague')
    db_conn = _connect_db()
    try:
        etapy = db_conn.execute(
            "SELECT number, date FROM etapy WHERE route_start IS NOT NULL AND route_start != ''",
        ).fetchall()
        now = datetime.now(ZoneInfo('Europe/Prague'))
        for e in etapy:
            if not e['date']:
                continue
            date_part = e['date'].split('(')[0].strip()
            try:
                run_date = datetime.strptime(date_part, '%d.%m.%Y').replace(
                    hour=7, minute=0, second=0, tzinfo=ZoneInfo('Europe/Prague'),
                )
            except ValueError:
                continue
            if run_date > now:
                scheduler.add_job(
                    send_weather_sms_for_etapa, 'date',
                    run_date=run_date, args=[e['number']],
                    id=f'weather_sms_{e["number"]}',
                )
                print(f'[SMS] Scheduled etapa {e["number"]} for {run_date}')
    finally:
        db_conn.close()
    if scheduler.get_jobs():
        scheduler.start()
        print(f'[SMS] Scheduler started with {len(scheduler.get_jobs())} job(s)')


def _schedule_payment_checks():
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
    except ImportError:
        print('[PAY] APScheduler not installed, skipping payment checks')
        return
    scheduler = BackgroundScheduler(timezone='Europe/Prague')
    scheduler.add_job(check_fio_payments, 'cron', minute='0,15,30,45', id='payment_check')
    scheduler.start()
    print('[PAY] Scheduled payment checks at :00, :15, :30, :45')


if not os.environ.get('TESTING'):
    init_db()
    _schedule_weather_sms()
    _schedule_payment_checks()


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(debug=os.environ.get('FLASK_DEBUG', '0') == '1', host='0.0.0.0', port=port)
