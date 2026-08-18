# Substitutions

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
- **Substitutions** record who covers which part of an absence. Most absences
  need a single substitution, but when no one person is free for the whole
  window the app splits it into the fewest sub-periods that *can* each be
  covered, so several teachers together fill the gap.

Not every minute of a reported absence needs covering. The school only runs
09:00–13:00 and 15:00–17:00 (`WORKING_HOURS` in
[substitutions/services.py](substitutions/services.py)), and any stretch that
falls within the absent teacher's *own* non-teaching hours wasn't a class to
begin with — so both are excluded before looking for a substitute. When an
absence spans one of these excluded stretches, it's split around it instead of
demanding one teacher for the whole window. The substitute-picking page lists
these excluded periods (and why) alongside the slots that do need a substitute.

Candidate ranking (see [substitutions/services.py](substitutions/services.py)) —
a teacher is eligible only if they're active, not the absent teacher, free for
the whole requested window, and not absent themselves. Eligible candidates are
then ordered by:

1. same grade level as the absent teacher (other grades are still shown, just
   deprioritized),
2. not already committed to substitute elsewhere in that window (those are shown
   for visibility but can't be picked),
3. fully free rather than doing paperwork,
4. fewest substitutions done so far, then name.

## Layout

| Path | What's in it |
| --- | --- |
| [config/](config/) | Django project settings, root URLconf, WSGI/ASGI entry points |
| [teachers/](teachers/) | `Teacher` and `WeeklyNonTeachingHours` models, schedule editor, CSV importer, admin week-calendar |
| [substitutions/](substitutions/) | `Absence` and `Substitution` models, dashboard, absence reporting, substitute picking, matching logic |
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
first_name,last_name,email,grade_level,weekday,start_time,end_time,is_paperwork
Jane,Doe,jane.doe@example.edu,primary,Monday,08:00,09:30,no
Jane,Doe,jane.doe@example.edu,primary,Monday,13:00,16:00,yes
John,Smith,john.smith@example.edu,pre_primary,Tuesday,09:00,10:00,no
```

See [teachers_template.csv](teachers_template.csv) (also downloadable from the
import page). Details:

- `grade_level` must be `primary` or `pre_primary`.
- `weekday` accepts names, abbreviations (`Mon`, `Tue`, …) or `0`–`6` with
  Monday as `0`. Times are `HH:MM` or `HH:MM:SS`.
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
