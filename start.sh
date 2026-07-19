#!/bin/bash
cd "$(dirname "${BASH_SOURCE[0]}")"

# ── Ensure local PostgreSQL is running ────────────────────────────
if ! systemctl is-active --quiet postgresql; then
    echo "Starting PostgreSQL..."
    sudo systemctl start postgresql
fi

# ── Create venv if it doesn't exist ───────────────────────────────
if [ ! -d "venv" ]; then
    echo "Creating virtual environment..."
    python3 -m venv venv
fi

# ── Install/update dependencies ───────────────────────────────────
echo "Installing dependencies..."
venv/bin/pip install -q -r requirements.txt

# ── Load .env file ────────────────────────────────────────────────
if [ -f .env ]; then
    set -a
    source .env
    set +a
fi

# ── Force local-only environment ──────────────────────────────────
export DATABASE_URL="postgresql://user:password@localhost:5432/cykloexpedice"
export SECRET_KEY="dev-secret-key"

# Disable external services to prevent affecting production
export FIO_API_TOKEN=""
export TWILIO_ACCOUNT_SID=""
export TWILIO_AUTH_TOKEN=""
export TWILIO_PHONE_NUMBER=""

# ── Clear SMTP credentials from the local database ───────────────
# Production dump contains real SMTP passwords in site_settings
PGGSSENCMODE=disable PGPASSWORD=password psql -h 127.0.0.1 -U user -d cykloexpedice -q -c \
    "DELETE FROM site_settings WHERE key LIKE 'smtp_password_%';" 2>/dev/null

# ── Run the app ───────────────────────────────────────────────────
echo "Starting app at http://localhost:5000 (local-only mode)"
venv/bin/python app.py
