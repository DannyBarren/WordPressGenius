# WordPressGenius Production Guide

This guide covers running WordPressGenius as a single-user or small-team
SaaS/self-hosted tool. The app is still intentionally conservative: it asks for
confirmation before risky changes and stores only local activity, memory,
uploads, logs, and backups.

## Recommended hosting

Good low-friction options:

- Railway: simple Docker deploys, managed environment variables, quick HTTPS.
- Fly.io: low-cost VM-style Docker deploys near users.
- Small VPS: Hetzner, DigitalOcean, Linode, or AWS Lightsail with Docker Compose.
- Home/lab server: acceptable for personal use if HTTPS and backups are handled.

For most small teams, a cheap VPS with Docker Compose and Caddy is the easiest
to reason about.

## Environment files and secrets

Create a `.env` file in the project root (it is gitignored). Set at minimum:

```bash
APP_ENV=prod
WORDPRESS_SITE_URL=https://your-site.com
WORDPRESS_USERNAME=your-wordpress-user
WORDPRESS_APPLICATION_PASSWORD=xxxx xxxx xxxx xxxx xxxx xxxx
REQUIRE_CONFIRMATION_FOR_MAJOR_CHANGES=true
```

For Docker secrets, mount secret files and point the app at them:

```bash
WORDPRESS_APPLICATION_PASSWORD_FILE=/run/secrets/wordpress_application_password
OPENAI_API_KEY_FILE=/run/secrets/openai_api_key
```

Never commit `.env`, secret files, `data/`, or `logs/`.

## Docker deployment

Build and run:

```bash
docker compose up --build -d
```

Services:

- `wordpressgenius`: Streamlit UI on port `8501`
- `health`: FastAPI health service on port `8081`
- `redis`: optional Redis service reserved for future session/memory backend use

Check health:

```bash
curl http://localhost:8081/health
curl http://localhost:8081/ready
```

## Reverse proxy and HTTPS

Do not expose Streamlit directly to the public internet without HTTPS and access
control. Put Caddy or Nginx in front of it.

Example Caddyfile:

```caddyfile
wordpressgenius.example.com {
  reverse_proxy 127.0.0.1:8501
}

wordpressgenius-health.example.com {
  reverse_proxy 127.0.0.1:8081
}
```

If this is for a small team, restrict access using one of:

- VPN or Tailscale
- Caddy `basicauth`
- Cloudflare Access
- Nginx basic auth or IP allowlist


## Observability and UX polish

WordPressGenius includes optional production observability and small-team UX
features:

- Optional Sentry error tracking via `SENTRY_DSN` with PII disabled.
- Consent-based local usage analytics stored in `data/usage_analytics.jsonl`.
- TTL caching for frequent WordPress reads such as posts, pages, and settings.
- Responsive Streamlit styling with dark-mode-aware colors and clearer focus states.
- Sidebar prompt library grouped by Content, Operations, and Growth examples.
- Export controls for activity logs and backup ZIP downloads.

Usage analytics is disabled unless `USAGE_ANALYTICS_ENABLED=true` and the signed-in
user opts in through the sidebar.

## Logging and monitoring

Relevant settings:

```bash
LOG_LEVEL=INFO
ENABLE_FILE_LOGGING=true
LOG_FILE=logs/wordpressgenius.log
JSON_LOGS=true
```

In containers, logs are emitted to stdout and optionally to the mounted
`logs/` volume. JSON logs are recommended in production so Railway/Fly/Docker
log collectors can parse fields.

Monitor:

- `/health` for liveness and writable path checks
- container restart counts
- disk usage for `data/` and `logs/`
- WordPress REST API failures and permission errors

## Backup strategy

Persist and back up the app runtime directory:

```text
data/
  activity_log.jsonl
  backups/
  site_memory.json
  uploads/
logs/
```

Recommended:

- Snapshot `data/` at least daily.
- Keep off-server copies with restic, borg, rclone, or provider snapshots.
- Test restoring `data/backups/` before relying on undo.
- Set `BACKUP_KEEP_LAST=25` or higher if storage is cheap.

WordPressGenius backups are JSON snapshots of REST resources it can read before
risky actions. They are not a full WordPress database/filesystem backup. Keep
normal WordPress host backups enabled.

## Scaling notes

Current target: single-user or small-team.

Safe small-team setup:

- One Streamlit container
- One health container
- One Redis container reserved for future shared session/memory work
- One persistent Docker volume for `data/`
- One persistent Docker volume for `logs/`

Before scaling horizontally, add:

- encrypted credential storage
- external database-backed activity/memory
- Redis-backed rate limiting and sessions
- job queue for long-running media/bulk operations
- user authentication and authorization
- exact WordPress capability checks via a companion plugin or privileged endpoint


## Security and multi-user hardening

WordPressGenius includes a self-hosted authentication foundation for small teams:

- YAML-backed login users in `config/users.yml` (copy from `config/users.example.yml`).
- App roles: `viewer`, `editor`, and `admin`.
- Per-user WordPress credential vault encrypted with Fernet.
- User-attributed audit log at `data/audit_log.jsonl`.
- Per-user Streamlit rate limiting.
- Prompt guardrails for obvious instruction-override and secret-exfiltration attempts.
- Dependabot configuration for Python, GitHub Actions, and Docker updates.

Set `CREDENTIAL_ENCRYPTION_KEY` before saving credentials. Generate one with:

```bash
python -c "from core.security import generate_fernet_key; print(generate_fernet_key())"
```

For public SaaS, replace YAML auth with Auth0, Supabase, Firebase Auth, or an
OIDC provider and move credential storage to a managed encrypted database or KMS.

## Known production limitations

- Role checks are coarse and based on WordPress roles returned by `users/me`.
- Undo does not fully restore plugin/theme state or complex WooCommerce product
  variations.
- Analytics metrics depend on what installed plugins expose through REST APIs.
- Streamlit is best for small teams; a larger SaaS should split UI, API,
  workers, and database into separate services.
