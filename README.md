# Cykloexpedice

Web application for **Cykloexpedice** — an annual multi-day cycling expedition organized since 2014. The site handles event presentation, participant registration, payment tracking, and administration.

## Features

- **Public site** — event info, route stages with GPX downloads, photo gallery, news, accommodation overview, and online registration
- **Admin panel** — registration management (approve / deny / delete), email notifications with branded templates, editable site content (stages, news, accommodation, event settings)
- **Payments** — automatic matching via Fio Banka API, QR code generation for bank transfers
- **Email system** — per-admin SMTP configuration, customizable HTML templates with live preview
- **Czech language support** — vocative case for personalized greetings (via `vokativ`)

## Tech Stack

| Layer       | Technology                          |
|-------------|-------------------------------------|
| Backend     | Python · Flask                      |
| Database    | PostgreSQL (psycopg2)               |
| Frontend    | Tailwind CSS (CDN) · Alpine.js (CDN)|
| Deployment  | Railway · Gunicorn                  |

## Project Structure

```
app.py           # Application entry point (routes, models, helpers)
templates/       # Jinja2 templates (public site + admin panel)
static/          # CSS, uploads, media
test_app.py      # Test suite
conftest.py      # Test fixtures
start.sh         # Local dev startup script
Procfile         # Railway/Heroku process definition
```

## Environment Variables

See `.env.example` for required configuration:

| Variable       | Description                        |
|----------------|-------------------------------------|
| `SECRET_KEY`   | Flask session secret                |
| `DATABASE_URL` | PostgreSQL connection string        |
| `FIO_API_TOKEN`| Fio Banka API token (payments)      |

## License

Private project.
