# Shared libraries

Files here are NOT skills — they're shared Python helpers that get deployed to
the brain's volume at `/data/hermes/lib/` alongside the skills.

## xero_pulse.py

Token-cache + refresh-token helper for the Pulse-registered Xero app.
Used by every specialist that talks to Xero. Reads access/refresh tokens from
mark-agent's XeroTenantToken Postgres table; refreshes them in-place when the
access token is about to expire.

### Required env vars on the brain

- `XERO_TOKEN_DB_URL` — public Postgres URL of mark-agent's database
- `XERO_PULSE_CLIENT_ID` — Pulse Xero app client ID
- `XERO_PULSE_CLIENT_SECRET` — Pulse Xero app client secret

### Required python deps

- `psycopg2-binary` (already in Hermes's bundled venv)

### Deploy

When updating, push to the brain with:

    cat lib/xero_pulse.py | railway ssh -- "tee /data/hermes/lib/xero_pulse.py > /dev/null"

Each specialist's `xero_*.py` does:

    sys.path.insert(0, "/data/hermes/lib")
    from xero_pulse import get_pulse_token, tenant_configured

so the brain path is the source of truth at runtime.
