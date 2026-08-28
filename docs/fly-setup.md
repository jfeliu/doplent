# Setting up Fly.io for hosting

[Fly.io](https://fly.io) runs the app itself — the [Dockerfile](../Dockerfile)
and [fly.toml](../fly.toml) in the repo root target it directly, with
[Supabase](supabase-setup.md) providing the database. Since all state lives in
Supabase, Fly machines need no persistent volume. Fly requires a card on file
and bills usage-based; there's a small free allowance but no no-card free
tier — check current pricing before relying on cost staying near zero.

## 1. Install and authenticate the CLI

```bash
curl -L https://fly.io/install.sh | sh
fly auth login
```

## 2. Create the app

```bash
fly launch --no-deploy
```

This detects the Dockerfile and generates/updates `fly.toml`. Review it
before deploying:

- `app` — must be globally unique; rename if taken.
- `primary_region` — pick the region closest to your users
  ([full list](https://fly.io/docs/reference/regions/)).
- `[[vm]]` **memory — don't leave this at the auto-generated `256mb`.** That's
  too tight for Django + 2 gunicorn workers + psycopg + whitenoise's static
  manifest loaded at once: requests get silently OOM-killed (`exit 137`),
  which surfaces to visitors as a plain `500 Internal Server Error` with
  **nothing in `fly logs`** (Django's default logging drops request-error
  tracebacks to console when `DEBUG=False` unless `LOGGING`/`ADMINS` is
  configured, which this project doesn't do). `512mb` is the confirmed-working
  minimum for this app; bump further if it recurs.
- `[[statics]]` `guest_path` must match `STATIC_ROOT` in
  [config/settings.py](../config/settings.py) exactly (both currently
  `static`, not `staticfiles`) - Fly's edge serves this path directly,
  bypassing the app entirely, so a mismatch 404s every static asset with no
  error anywhere obvious.

## 3. Set secrets

Never put real values in `fly.toml` - it's checked into git. Everything
sensitive goes through `fly secrets`, sourced from an untracked
`.env.production` (see [.env.example](../.env.example) for the full variable
list; it's gitignored, same as `.env`):

```bash
fly secrets import --app <app-name> < .env.production
```

Or set individual values by hand:

```bash
fly secrets set --app <app-name> \
  DJANGO_SECRET_KEY="$(python -c 'import secrets; print(secrets.token_urlsafe(50))')" \
  DATABASE_URL="postgresql://...supabase pooler connection string..." \
  DJANGO_ALLOWED_HOSTS="your-domain.com,www.your-domain.com" \
  DJANGO_CSRF_TRUSTED_ORIGINS="https://your-domain.com,https://www.your-domain.com"
```

`fly secrets list --app <app-name>` shows names (not values) and whether
they're staged or deployed - staged ones apply on the next deploy.

## 4. Deploy

```bash
fly deploy --app <app-name>
```

Migrations run automatically on every deploy via `release_command` in
[fly.toml](../fly.toml) - nothing extra to run by hand. If the release
command fails with a Postgres connection timeout, it's usually Supabase's
free-tier project waking from an idle pause; retry the deploy.

## 5. Point a custom domain at it

```bash
fly ips list --app <app-name>          # note the A (v4) and AAAA (v6) addresses
fly certs create your-domain.com --app <app-name>
fly certs create www.your-domain.com --app <app-name>
```

Add the DNS records Fly recommends (matches the `fly ips list` output) with
whoever hosts your domain. In Cloudflare: **DNS → Records → Add record**, one
A and one AAAA for the root domain and again for `www`, pointing at the
addresses from `fly ips list`. **Leave them DNS only (grey cloud, not
orange/proxied)** - Cloudflare terminating TLS itself blocks Fly's Let's
Encrypt validation from ever completing, and `fly certs check` hangs on
"awaiting configuration" forever. (Same rule as the mail-auth records in
[brevo-setup.md](brevo-setup.md#2-verify-your-sending-domain) - Cloudflare
proxying breaks anything that needs to resolve directly.)

```bash
fly certs check your-domain.com --app <app-name>
```

Once DNS propagates (minutes, occasionally up to an hour) this reports
`Certificate is verified and active`.

## 6. Verify it's actually working

`fly.dev`'s own subdomain will correctly return `400 Bad Request` once a
custom domain is configured - that's Django's `ALLOWED_HOSTS` doing its job,
not a bug, since it only lists your real domain. Test against the real
domain instead:

```bash
curl -I https://your-domain.com/login/
```

If you need to reproduce a failure directly on the machine (e.g. a `500`
with nothing useful in `fly logs`), Django's test client re-raises view
exceptions with a full traceback, which SSH exec will print - long
multi-line `-c` scripts get mangled by nested shell quoting through SSH, so
base64-encode them instead:

```bash
cat > /tmp/diag.py << 'EOF'
import traceback
from django.test import Client
c = Client(SERVER_NAME="your-domain.com", raise_request_exception=False)
r = c.get("/login/", SERVER_NAME="your-domain.com", HTTP_X_FORWARDED_PROTO="https")
print("STATUS", r.status_code)
if getattr(r, "exc_info", None):
    traceback.print_exception(*r.exc_info)
EOF
B64=$(base64 -w0 /tmp/diag.py)
fly ssh console --app <app-name> -C "sh -c \"echo $B64 | base64 -d > /tmp/d.py && python manage.py shell < /tmp/d.py\""
```

## 7. Create the first admin user

```bash
fly ssh console --app <app-name> -C "python manage.py createsuperuser"
```

## 8. Set up continuous deployment (GitHub Actions)

[.github/workflows/fly-deploy.yml](../.github/workflows/fly-deploy.yml) runs
`flyctl deploy --remote-only` on every push to `main`/`master`. It needs a
`FLY_API_TOKEN` repository secret - without it, that step fails almost
instantly (finishes in about a second, no build/upload logs at all) because
flyctl has nothing to authenticate with and never gets as far as talking to
Fly.

Generate a token scoped to just this app rather than reusing your personal
`fly auth token`:

```bash
fly tokens create deploy --app <app-name> -x 999999h
```

(`-x` sets a long expiry; omit it and Fly defaults to a short-lived token
that needs periodic renewal.)

Then in GitHub: **Settings → Secrets and variables → Actions → New repository
secret**, name it `FLY_API_TOKEN`, and paste the token value.

If a deploy run fails instantly again later, the token likely expired or was
revoked - regenerate it the same way and update the secret with the new
value.

## Notes on the free/low-cost tier

- `auto_stop_machines`/`min_machines_running = 0` in [fly.toml](../fly.toml)
  scales to zero when idle to keep cost near-zero on a low-traffic school
  app - the first request after idle takes a few seconds to cold-start a
  machine.
- An idle SSH session doesn't count as traffic and won't keep a machine
  warm; it can auto-stop mid-session, which shows up as the SSH command
  exiting with a nonsensical status code rather than a clean error.
- Fly's and Supabase's free/low-cost tiers both change their terms fairly
  often - check current limits before relying on this staying cheap
  indefinitely.
