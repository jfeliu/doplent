# Doplent

A small Django app that helps a school cover teacher absences. A teacher reports
when they'll be away, and the app proposes colleagues who are genuinely free
during that window — ranked so the most sensible choice comes first — then
records who is covering what.

Built for a primary / pre-primary school. The UI is available in English and
Catalan.

## How it works

- **Teachers** register their *weekly non-teaching hours*: recurring free blocks
  each weekday when they're at school but not teaching. A block can be flagged
  as *paperwork* — the teacher is still available, just not idle.
- **Absences** are reported as a start/end datetime plus an optional reason.
- **Substitutions** record who covers which part of an absence. The
  substitute-picking page is a grid of 30-minute rows across the absence, one
  column per teacher who's free for some of it; the coverer clicks free blocks
  in a teacher's column to build a period and offers it to them. One teacher can
  take the whole absence, or several can each take a stretch.
- **Substitution offers** gate the above on the candidate's consent: offering a
  teacher a period on the picker sends them a pending offer (and an email)
  rather than booking them outright. They accept or reject it from
  their dashboard or the emailed link; only accepting creates the confirmed
  `Substitution`. Several candidates can be offered the same slot in
  parallel — first acceptance wins, the rest auto-expire. See
  [substitutions/offers.py](substitutions/offers.py) for the lifecycle and
  [substitutions/emails.py](substitutions/emails.py) for notifications (the
  acceptance email attaches a plain `.ics` file so the confirmed slot lands on
  the substitute's calendar, with no calendar-provider integration involved).

Not every minute of a reported absence needs covering. The school only runs
09:00–13:00 and 15:00–17:00 (`WORKING_HOURS` in
[substitutions/services.py](substitutions/services.py)), and any stretch that
falls within the absent teacher's *own* non-teaching hours wasn't a class to
begin with — so both are excluded before looking for a substitute. Those rows
show in the grid as a labelled band ("Outside school hours", "Your own
non-teaching time", "Already covered") rather than selectable cells.

Column ranking (see `build_coverage_grid` in
[substitutions/services.py](substitutions/services.py)) — a teacher is a column
whenever they're free for at least one 30-minute slot that still needs a
substitute. Columns are ordered by:

1. same grade level as the absent teacher (other grades still shown, just later),
2. narrowest availability in the window first, so a teacher free for only one
   slot gets grabbed before one who's free all morning,
3. least substitution time already done (summed duration), then name.

Cells are coloured by the kind of non-teaching block behind them (free,
paperwork, co-teaching, escolta'm); a teacher already covering another absence
during a slot shows that slot as busy, and a slot they already have a pending
offer for is marked and not selectable. Selecting cells for several teachers
and hitting **Send offers** fires one offer per contiguous run in a single go.

## Layout

| Path | What's in it |
| --- | --- |
| [config/](config/) | Django project settings, root URLconf, WSGI/ASGI entry points |
| [teachers/](teachers/) | `Teacher` and `WeeklyNonTeachingHours` models, schedule editor, CSV importer, admin week-calendar |
| [substitutions/](substitutions/) | `Absence`, `Substitution` and `SubstitutionOffer` models, dashboard, absence reporting, substitute picking, offer accept/reject, matching logic, email notifications |
| [templates/](templates/) | Project-level templates, including the admin overrides |
| [locale/ca/](locale/ca/LC_MESSAGES/) | Catalan translations |

## Getting started

Requires Python 3.10+ (the code uses `X | None` annotations and `list[...]`
generics) and the dependency in [requirements.txt](requirements.txt).

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt

python manage.py migrate
python manage.py createsuperuser
python manage.py runserver
```

Then open http://127.0.0.1:8000/ — you'll be sent to the login page. Note that
the dashboard needs the logged-in user to have a `Teacher` record, so create one
in the admin (or import teachers, below) before logging in as them.

Run the tests with:

```bash
python manage.py test
```

## Loading teachers

Rather than adding teachers one at a time, the admin has a CSV import at
**Teachers → Import from CSV**. One row per non-teaching block; repeat a
teacher's name across rows to give them several blocks, or leave the
weekday/time columns blank to register a teacher with no hours yet.

```csv
first_name,last_name,email,grade_level,weekday,start_time,end_time,type
Jane,Doe,jane.doe@example.edu,primary,Monday,08:00,09:30,free
Jane,Doe,jane.doe@example.edu,primary,Monday,13:00,16:00,paperwork
John,Smith,john.smith@example.edu,pre_primary,Tuesday,09:00,10:00,escoltam
```

See [teachers_template.csv](teachers_template.csv) (also downloadable from the
import page). Details:

- `grade_level` must be `primary` or `pre_primary`.
- `weekday` accepts names, abbreviations (`Mon`, `Tue`, …) or `0`–`6` with
  Monday as `0`. Times are `HH:MM` or `HH:MM:SS`.
- `type` is one of `free`, `paperwork`, `co_teaching`, `escoltam` (english or
  catalan spellings accepted); blank or no column means `free`. The
  substitute-picker draws teachers off `free` time first and `escoltam` last —
  the order is set in the admin under **Non-teaching hours priorities**.
- Usernames are derived from the name, lowercase and dot-separated
  (`Jane Doe` → `jane.doe`). Generated passwords for newly created users are
  shown once on the results page — save them then.
- Re-uploading the same file is safe: matching users and blocks are left alone
  rather than duplicated. If *any* row fails validation, nothing is saved.

The admin also has a **Weekly calendar** view showing every active teacher's
non-teaching hours as one week grid.

## Configuration

The defaults in [config/settings.py](config/settings.py) are development-only:
`DEBUG` is on, the secret key is the checked-in placeholder, and the database is
a local SQLite file. For anything else, copy [.env.example](.env.example) and set
these in your environment or process manager:

| Variable | Default | Notes |
| --- | --- | --- |
| `DJANGO_SECRET_KEY` | insecure placeholder | Set to a random value in production |
| `DJANGO_DEBUG` | `True` | Set to `False` outside local dev |
| `DJANGO_ALLOWED_HOSTS` | LAN IPs + localhost | Comma-separated hostnames |
| `DJANGO_CSRF_TRUSTED_ORIGINS` | empty | Comma-separated origins **with scheme** (`https://example.com`) - required once the site is served over HTTPS behind a proxy, e.g. Fly.io |
| `DATABASE_URL` | unset (falls back to the local SQLite file) | A Postgres connection string (e.g. from Supabase) for anything other than local dev |
| `DJANGO_EMAIL_BACKEND` | console backend | Prints emails to stdout; set to the SMTP backend for real delivery |
| `DJANGO_EMAIL_HOST` / `_PORT` / `_HOST_USER` / `_HOST_PASSWORD` / `_USE_TLS` | `localhost` / `25` / empty / empty / `False` | Only used by the SMTP backend |
| `DJANGO_DEFAULT_FROM_EMAIL` | `webmaster@localhost` | From-address for substitution-offer notifications |
| `DJANGO_EMAIL_TIMEOUT` | `10` (seconds) | Bounds how long a send can block the request - emails are sent synchronously, no task queue |

A teacher with no email address on file (blank `user.email`) is silently
skipped when notifications are sent — offers/confirmations still work
in-app, they just don't get emailed.

### Testing email with MailHog

The console backend (the default) just prints emails to the terminal, which
doesn't exercise real SMTP delivery or let you see the `.ics` attachment as a
mail client would. [MailHog](https://github.com/mailhog/MailHog) is a local
SMTP server with a web inbox, good for that without sending anything real:

```bash
docker run -d --name mailhog -p 1025:1025 -p 8025:8025 mailhog/mailhog
```

Point the app at it and run the server:

```bash
export DJANGO_EMAIL_BACKEND=django.core.mail.backends.smtp.EmailBackend
export DJANGO_EMAIL_HOST=localhost
export DJANGO_EMAIL_PORT=1025
export DJANGO_EMAIL_USE_TLS=False
export DJANGO_DEFAULT_FROM_EMAIL="Doplent <notifications@example.com>"
python manage.py runserver
```

Then open http://localhost:8025 to see everything sent - offer notifications,
acceptance confirmations (with the `.ics` attachment), and the reporting
teacher's notifications. A quick sanity check that doesn't require the app at
all: `python manage.py sendtestemail you@example.com`.

**A `.env` file is not loaded automatically** - nothing in this project reads
it (no `python-dotenv` or similar); it's just a template to copy values from.
If you'd rather keep the settings in a file, `source` it into your shell
before running the server instead of relying on it being picked up on its
own:

```bash
set -a && source .env && set +a
python manage.py runserver
```

For local testing, only put the email vars in that file - copying
`DJANGO_DEBUG`/`DJANGO_ALLOWED_HOSTS` from [.env.example](.env.example)
verbatim sets production-oriented values that make `runserver` reject
`localhost` requests. Also note the `DJANGO_DEFAULT_FROM_EMAIL` value needs
quoting (`"Doplent <notifications@example.com>"`) since it contains a `<` -
unquoted, `source` will fail with a shell syntax error.

The container keeps running in the background across restarts; `docker stop
mailhog && docker rm mailhog` removes it, or `docker start mailhog` brings an
existing one back.

## Deployment

The [Dockerfile](Dockerfile) and [fly.toml](fly.toml) target
[Fly.io](https://fly.io) for compute and [Supabase](https://supabase.com) (or
any Postgres) for the database — that combination needs no persistent volume
on Fly, since all state lives in Supabase. `whitenoise` (already in
[requirements.txt](requirements.txt)) serves static files straight from the
app container, so nothing else is needed for those.

1. Create a Supabase project and copy its pooled connection string. See
   [docs/supabase-setup.md](docs/supabase-setup.md) for a walkthrough.
2. `fly launch --no-deploy` in this directory to create the Fly app (it'll
   detect [fly.toml](fly.toml) - update the `app` name there first, since it
   must be globally unique), or edit [fly.toml](fly.toml) by hand if you'd
   rather not run `launch`. See [docs/fly-setup.md](docs/fly-setup.md) for a
   full walkthrough, including custom domains and a VM-memory gotcha that
   otherwise shows up as an unexplained `500` in production.
3. Set secrets - never put these in [fly.toml](fly.toml), which is checked
   into git:
   ```bash
   fly secrets set \
     DJANGO_SECRET_KEY="$(python -c 'import secrets; print(secrets.token_urlsafe(50))')" \
     DATABASE_URL="postgresql://...supabase connection string..." \
     DJANGO_ALLOWED_HOSTS="your-app-name.fly.dev" \
     DJANGO_CSRF_TRUSTED_ORIGINS="https://your-app-name.fly.dev" \
     DJANGO_EMAIL_BACKEND=django.core.mail.backends.smtp.EmailBackend \
     DJANGO_EMAIL_HOST=smtp.your-provider.com \
     DJANGO_EMAIL_PORT=587 \
     DJANGO_EMAIL_HOST_USER=notifications@your-domain.com \
     DJANGO_EMAIL_HOST_PASSWORD=... \
     DJANGO_EMAIL_USE_TLS=True \
     DJANGO_DEFAULT_FROM_EMAIL="Doplent <notifications@your-domain.com>"
   ```
   Supabase's free tier doesn't send email itself - use a separate SMTP
   provider's free tier (e.g. Brevo, Resend) for that. See
   [docs/brevo-setup.md](docs/brevo-setup.md) for a walkthrough of setting up
   Brevo with a Cloudflare-managed domain.
4. `fly deploy`. Migrations run automatically on every deploy via
   `release_command` in [fly.toml](fly.toml); nothing extra to run by hand.
5. Create the first admin user: `fly ssh console -C "python manage.py
   createsuperuser"`.
6. Set up database backups. Supabase's free plan takes none, so
   [.github/workflows/db-backup.yml](.github/workflows/db-backup.yml) runs a
   daily encrypted `pg_dump` and uploads it to a private Google Drive folder.
   See [docs/backup-setup.md](docs/backup-setup.md) for the Drive OAuth token,
   the four secrets it needs, and how to restore.

Both Fly's and Supabase's free tiers change their terms fairly often -
check current limits before relying on this staying free indefinitely.

## Translations

After changing translatable strings:

```bash
python manage.py makemessages -l ca
python manage.py compilemessages
```

Users can switch language through the standard `django.conf.urls.i18n` endpoints
mounted at `/i18n/`.

Watch out for `{{ }}` on floats in templates: Django localizes numbers per the
active language, and Catalan uses a comma decimal separator. Any numeric value
that feeds CSS or JS (not just display text) needs `{% load l10n %}` +
`{% localize off %}` around it, or it'll render as e.g. `12,5%` instead of
`12.5%` — invalid CSS that browsers silently drop. See the admin week-calendar
template ([weekly_calendar.html](templates/teachers/admin/weekly_calendar.html))
for an example.

## License

MIT — see [LICENSE](LICENSE).
