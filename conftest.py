"""Shared pytest fixtures — SQLite-backed Flask test client."""
import os
import sqlite3
from datetime import datetime
import pytest
import bcrypt

# Prevent real DB/API connections during tests
os.environ['TESTING'] = '1'
os.environ.setdefault('DATABASE_URL', 'unused')
os.environ.setdefault('FIO_API_TOKEN', 'test-token')
os.environ['SECRET_KEY'] = 'test-secret'


TIMESTAMP_COLUMNS = {'created_at', 'decided_at', 'qr_sent_at', 'updated_at', 'expires_at'}


def _parse_timestamp(value):
    """Try to parse a string timestamp into a datetime object."""
    if value is None or isinstance(value, datetime):
        return value
    for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M:%S.%f', '%Y-%m-%dT%H:%M:%S',
                '%Y-%m-%dT%H:%M:%S.%f'):
        try:
            return datetime.strptime(value, fmt)
        except (ValueError, TypeError):
            continue
    return value


def _convert_row(row):
    """Convert sqlite3.Row to a dict-like object with datetime parsing for timestamp columns."""
    if row is None:
        return None
    d = dict(row)
    for col in TIMESTAMP_COLUMNS:
        if col in d:
            d[col] = _parse_timestamp(d[col])
    return d


class RowProxy(dict):
    """Dict that also supports attribute-style and integer-index access like sqlite3.Row."""
    def __getattr__(self, key):
        try:
            return self[key]
        except KeyError:
            raise AttributeError(key)

    def __getitem__(self, key):
        if isinstance(key, int):
            return list(self.values())[key]
        return super().__getitem__(key)


class SqliteCursorWrapper:
    def __init__(self, cursor):
        self._cursor = cursor
        self._desc = cursor.description

    def fetchone(self):
        row = self._cursor.fetchone()
        if row is None:
            return None
        return RowProxy(_convert_row(row))

    def fetchall(self):
        rows = self._cursor.fetchall() or []
        return [RowProxy(_convert_row(r)) for r in rows]


class SqliteConnectionWrapper:
    def __init__(self, conn, closeable=True):
        self._conn = conn
        self._closeable = closeable

    def execute(self, query, params=None):
        # Translate PostgreSQL syntax to SQLite
        query = query.replace('SERIAL PRIMARY KEY', 'INTEGER PRIMARY KEY AUTOINCREMENT')
        query = query.replace('TIMESTAMP DEFAULT CURRENT_TIMESTAMP',
                              'TEXT DEFAULT CURRENT_TIMESTAMP')
        # Replace remaining standalone TIMESTAMP (not part of CURRENT_TIMESTAMP)
        import re
        query = re.sub(r'(?<!CURRENT_)TIMESTAMP', 'TEXT', query)
        query = query.replace('REAL', 'REAL')
        cur = self._conn.cursor()
        cur.execute(query, params or ())
        self._desc = cur.description
        return SqliteCursorWrapper(cur)

    def commit(self):
        self._conn.commit()

    def close(self):
        # Only close if allowed — Flask teardown calls close() after each request,
        # but we need the shared in-memory DB to stay open across requests.
        if self._closeable:
            self._conn.close()


@pytest.fixture()
def app(monkeypatch):
    """Create a Flask app wired to an in-memory SQLite database."""
    import app as flask_app

    conn = sqlite3.connect(':memory:')
    conn.row_factory = sqlite3.Row

    def fake_connect():
        # Return a non-closeable wrapper so Flask's close_db() doesn't kill
        # the shared in-memory connection between requests.
        return SqliteConnectionWrapper(conn, closeable=False)

    monkeypatch.setattr(flask_app, '_connect_db', fake_connect)
    flask_app.app.config['TESTING'] = True
    flask_app.app.config['SECRET_KEY'] = 'test-secret'

    # Initialize tables
    flask_app.init_db()

    yield flask_app.app

    conn.close()


@pytest.fixture()
def client(app):
    return app.test_client()


@pytest.fixture()
def admin_client(app, client):
    """A test client already logged in as admin 'adam'."""
    import app as flask_app

    pw_hash = bcrypt.hashpw(b'testpass', bcrypt.gensalt()).decode()

    with app.app_context():
        d = flask_app.get_db()
        d.execute('UPDATE admins SET password_hash = ? WHERE username = ?', (pw_hash, 'adam'))
        d.commit()

    client.post('/admin/login', data={'username': 'adam', 'password': 'testpass'})
    return client
