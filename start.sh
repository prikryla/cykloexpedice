#!/bin/bash
cd "$(dirname "${BASH_SOURCE[0]}")"

# Create venv if it doesn't exist
if [ ! -d "venv" ]; then
    echo "Creating virtual environment..."
    python3 -m venv venv
fi

# Install/update dependencies
echo "Installing dependencies..."
venv/bin/pip install -q -r requirements.txt

# Load .env file
if [ -f .env ]; then
    set -a
    source .env
    set +a
fi

# Run the app
echo "Starting app at http://localhost:5000"
venv/bin/python app.py
