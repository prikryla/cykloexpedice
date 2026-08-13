# Cykloexpedice

Web application for **Cykloexpedice** — an annual multi-day cycling expedition organized since 2014. The site handles event presentation, participant registration, payment tracking, and administration.

## Features

- **Public site** — event info, route stages with elevation stats, photo gallery, news (aktuality), accommodation overview, contact page, and online registration
- **User portal** — personal dashboard with Strava integration, activity tracking across past expeditions, and registration status
- **Admin panel** — registration management (approve / deny / delete), email notifications with branded templates, editable site content (stages, news, accommodation, event settings), maintenance mode
- **Payments** — automatic matching via Fio Banka API with scheduled checks, QR code generation for bank transfers
- **Email** — transactional email via Resend API, customizable HTML templates with live preview
- **Weather SMS** — automated weather forecasts via Open-Meteo API, SMS delivery via Twilio with scheduling
- **Czech language support** — informal address (tykání), vocative case for personalized greetings (via `vokativ`)

## Tech Stack

| Layer        | Technology                                                  |
|--------------|-------------------------------------------------------------|
| Backend      | Python · Flask · APScheduler                                |
| Database     | PostgreSQL (psycopg2)                                       |
| Frontend     | Tailwind CSS (CDN) · Alpine.js (CDN) · Bootstrap Icons      |
| Maps         | Leaflet (Strava activity polylines)                         |
| Email        | Resend API                                                  |
| SMS          | Twilio (optional)                                           |
| Integrations | Strava OAuth · Fio Banka API · Open-Meteo API              |
| Auth         | bcrypt · flask-limiter · CSRF tokens                        |
| Deployment   | Hetzner · Docker · Gunicorn                                 |
| CI/CD        | GitHub Actions (tests + linting via ruff, auto-deploy)      |

## Project Structure

```
app.py                # Main application (routes, models, helpers)
templates/
  ├── admin/          # Admin panel templates
  ├── user/           # User portal templates (dashboard, login, Strava)
  └── *.html          # Public site templates
static/
  ├── badges/         # Expedition badge images
  ├── uploads/        # User-uploaded content
  └── *.png/mp4       # Logo, favicon, hero video
test_app.py           # Test suite
conftest.py           # Test fixtures
requirements.txt      # Python dependencies
.env.example          # Environment variable template
Dockerfile            # Container image definition
docker-compose.yaml   # Docker Compose service config
.github/workflows/    # CI/CD pipelines (tests, deploy)
ruff.toml             # Linter configuration
start.sh              # Local dev startup script
```

## Environment Variables

See `.env.example` for required configuration:

| Variable              | Description                            |
|-----------------------|----------------------------------------|
| `SECRET_KEY`          | Flask session secret                   |
| `DATABASE_URL`        | PostgreSQL connection string           |
| `FIO_API_TOKEN`       | Fio Banka API token (payments)         |
| `RESEND_API_KEY`      | Resend API key (transactional email)   |
| `STRAVA_CLIENT_ID`    | Strava OAuth app client ID             |
| `STRAVA_CLIENT_SECRET`| Strava OAuth app client secret         |

Optional (for weather SMS):

| Variable              | Description                            |
|-----------------------|----------------------------------------|
| `TWILIO_ACCOUNT_SID`  | Twilio account SID                     |
| `TWILIO_AUTH_TOKEN`    | Twilio auth token                      |
| `TWILIO_PHONE_NUMBER` | Twilio sender phone number             |

## License

[MIT](LICENSE)
