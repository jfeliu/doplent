# Backing up the database

Supabase's **free plan takes no backups** — no automated snapshots, nothing
downloadable, and a deleted or corrupted project is unrecoverable. (Daily
backups with 7-day retention start on the Pro plan; point-in-time recovery is a
paid add-on on top of that.)

[.github/workflows/db-backup.yml](../.github/workflows/db-backup.yml) fills the
gap: once a day it runs `pg_dump` against Supabase, gzips it, encrypts it, and
uploads the result to a private Google Drive folder. Old backups (over a year)
are pruned on each run. You can also trigger a run by hand from **Actions →
Database backup → Run workflow**.

Why Drive and not a workflow artifact: this repo is public, and artifacts on a
public repo can be downloaded by anyone. The dump also carries staff personal
data, so it's GPG symmetric-encrypted (AES-256) on the runner regardless — only
ciphertext leaves the machine.

## 1. Google Drive: a folder and an OAuth token

A *service account* can't be used here — service accounts have no Drive storage
quota, and the "shared drive" workaround needs Google Workspace. So the workflow
signs in as your own Google account with a token you generate once.

1. In your Drive, create a folder, e.g. `Doplent backups`. Open it and copy its
   ID from the URL — `https://drive.google.com/drive/folders/<THIS-PART>`.
2. On your laptop, install rclone (`brew install rclone`, `apt install rclone`,
   or <https://rclone.org/downloads/>) and run `rclone authorize "drive"`.
   Answer the prompts: leave `client_id` / `client_secret` blank (uses rclone's
   own), choose scope **`drive`** (full access), leave the rest at defaults.

   It opens a browser — sign in as the Google account that owns the folder and
   approve access. When it finishes it prints a JSON blob:

   ```
   {"access_token":"ya29...","token_type":"Bearer","refresh_token":"1//...","expiry":"..."}
   ```

   Copy that whole line (the token). It uses rclone's published OAuth client, so
   the refresh token stays valid as long as it's used at least every 6 months —
   a daily cron keeps it alive.

## 2. GitHub: four repository secrets

**Settings → Secrets and variables → Actions → New repository secret.**

| Secret | Value |
| --- | --- |
| `SUPABASE_DB_URL` | The **Session pooler** URI (port `5432`) from Supabase → Project Settings → Database → Connection string, with the password filled in. |
| `BACKUP_PASSPHRASE` | A strong random string. **Store it in your password manager** — lose it and every backup is unrecoverable. `python -c 'import secrets; print(secrets.token_urlsafe(32))'` |
| `GDRIVE_TOKEN` | The full JSON token line from step 1.2. |
| `GDRIVE_FOLDER_ID` | The folder ID from step 1.1. |

On `SUPABASE_DB_URL`, don't use the other two connection options: the **direct**
connection (`db.<ref>.supabase.co`) is IPv6-only and GitHub's runners have no
IPv6, and the **Transaction pooler** (port `6543`) can't run `pg_dump`. This is
also a different value from the app's `DATABASE_URL` on Fly (transaction pooler,
right for the app, wrong for `pg_dump`).

Then run it once from **Actions → Database backup → Run workflow** to confirm
the whole chain works.

## 3. Restoring

Download the dump you want from the Drive folder, then:

```bash
printf '%s' 'the-passphrase' > pp && chmod 600 pp

gpg --batch --pinentry-mode loopback --passphrase-file pp \
    --decrypt doplent-YYYYMMDDTHHMMSSZ.sql.gz.gpg \
  | gunzip \
  | psql "postgresql://postgres.xxxx:password@aws-0-eu-west-1.pooler.supabase.com:5432/postgres"

rm pp
```

Restore into a **fresh / empty** database (a new Supabase project, or after
resetting the schema) — the dump has no `DROP`s, so restoring over live data
collides on existing rows. It's taken with `--no-owner --no-privileges`, so it
applies regardless of the target project's role names.

Inspect a dump without a database:

```bash
gpg --batch --pinentry-mode loopback --passphrase-file pp \
    --decrypt doplent-*.sql.gz.gpg | gunzip | less
```

## Notes

- Retention is one year (`rclone delete --min-age 365d` in the workflow); adjust
  that line to taste.
- `pg_dump` is pinned to `postgres:17-alpine`; if Supabase moves past Postgres
  17, bump that tag or the dump step will refuse to run.
- `rclone` comes from the runner image, or `apt` if that image ever drops it.
  Both are old-ish but fine for `copy`/`delete`/`lsl`; add a pinned download
  from GitHub releases in the "Ensure rclone" step if you need a specific one.
- This is a stopgap for the free tier. Supabase Pro adds managed daily backups
  and removes the idle-pause, at which point this workflow is a belt-and-braces
  extra rather than the only line of defence.
