# Setting up Supabase for the database

[Supabase](https://supabase.com) hosts a managed Postgres instance Doplent
uses in place of the local SQLite file (see
[DATABASE_URL](../README.md#configuration) in the README's Configuration
section, and [config/settings.py](../config/settings.py) for
`dj_database_url.config()`). Free plan: 500MB storage, shared compute, no
credit card required.

## 1. Create a project

Sign up / sign in at [supabase.com](https://supabase.com), then **New
project**:

| Setting | Value |
| --- | --- |
| Organization | create or reuse one (free tier) |
| Name | e.g. `doplent` |
| Database Password | generate a strong one and save it in a password manager — it's shown only once and isn't recoverable, only resettable |
| Region | closest to your users; Supabase has no Madrid region, so `eu-west-1` (Ireland) or `eu-central-1` (Frankfurt) are the nearest options for a Fly `mad` deployment |
| Pricing plan | Free |

Provisioning takes a couple of minutes.

## 2. Get the connection string

**Project Settings → Database → Connection string → URI**.

Use the **Session pooler** (or **Transaction pooler**) string, not the direct
connection — the direct connection is IPv6-only and doesn't handle
reconnects well against Fly machines that scale to zero and back. The pooler
looks like:

```
postgresql://postgres.xxxxxxxxxxxx:[YOUR-PASSWORD]@aws-0-eu-west-1.pooler.supabase.com:6543/postgres
```

Replace `[YOUR-PASSWORD]` with the database password from step 1.

## 3. Configure Doplent

This maps to the `DATABASE_URL` var documented in the README's
[Configuration](../README.md#configuration) section.

Local testing (`.env`, sourced per the
[MailHog instructions](../README.md#testing-email-with-mailhog) pattern —
only needed if you want to test against real Postgres instead of the default
SQLite fallback):

```bash
export DATABASE_URL="postgresql://postgres.xxxxxxxxxxxx:your-password@aws-0-eu-west-1.pooler.supabase.com:6543/postgres"
```

Production (Fly.io secrets, per [Deployment](../README.md#deployment)):

```bash
fly secrets set DATABASE_URL="postgresql://postgres.xxxxxxxxxxxx:your-password@aws-0-eu-west-1.pooler.supabase.com:6543/postgres"
```

## 4. Run migrations

```bash
python manage.py migrate
```

On Fly this runs automatically on every deploy via `release_command` in
[fly.toml](../fly.toml) — nothing extra to run by hand there.

## Notes on the free tier

- **Free projects pause after 1 week with no API/DB activity.** The first
  request after a pause takes a few seconds to wake the database back up.
- Only one free project per organization.
- Supabase's free tier doesn't send email — see
  [docs/brevo-setup.md](brevo-setup.md) for that side.

Supabase's terms/limits change fairly often — check current ones before
relying on this staying free indefinitely.
