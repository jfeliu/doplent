# Setting up Brevo for outbound email

[Brevo](https://www.brevo.com) (formerly Sendinblue) is a free-tier SMTP
provider suitable for Doplent's substitution-offer notifications (see
[substitutions/emails.py](../substitutions/emails.py)). Free plan: 300
emails/day, no credit card required. This assumes your domain's DNS is
managed in Cloudflare.

## 1. Create an account

Sign up at [app.brevo.com](https://app.brevo.com). The free plan is selected
by default.

## 2. Verify your sending domain

In Brevo: **Senders, Domains & Dedicated IPs → Domains → Add a domain**,
enter your domain, and Brevo will show a handful of DNS records (an SPF TXT
record, a DKIM TXT record, and usually a tracking CNAME).

Add each one in Cloudflare: **DNS → Records → Add record**, using the exact
type/name/value Brevo gives you. Leave them **DNS only** (grey cloud, not
orange/proxied) — mail-auth records must resolve directly, not through
Cloudflare's proxy.

Back in Brevo, click verify. DNS propagation is usually a few minutes, but
can take up to an hour.

## 3. Get SMTP credentials

**SMTP & API → SMTP** tab:

| Setting | Value |
| --- | --- |
| Host | `smtp-relay.brevo.com` |
| Port | `587` |
| Login | your Brevo account email |
| Password | a generated **SMTP key** (not your account password) |

## 4. Configure Doplent

These map to the env vars documented in the README's
[Configuration](../README.md#configuration) section.

Local testing (`.env`, sourced per the
[MailHog instructions](../README.md#testing-email-with-mailhog) — swap in
these values instead of MailHog's to test real delivery):

```bash
export DJANGO_EMAIL_BACKEND=django.core.mail.backends.smtp.EmailBackend
export DJANGO_EMAIL_HOST=smtp-relay.brevo.com
export DJANGO_EMAIL_PORT=587
export DJANGO_EMAIL_HOST_USER=<your-brevo-login-email>
export DJANGO_EMAIL_HOST_PASSWORD=<smtp-key>
export DJANGO_EMAIL_USE_TLS=True
export DJANGO_DEFAULT_FROM_EMAIL="Doplent <notifications@yourdomain.com>"
```

Production (Fly.io secrets, per [Deployment](../README.md#deployment)):

```bash
fly secrets set \
  DJANGO_EMAIL_BACKEND=django.core.mail.backends.smtp.EmailBackend \
  DJANGO_EMAIL_HOST=smtp-relay.brevo.com \
  DJANGO_EMAIL_PORT=587 \
  DJANGO_EMAIL_HOST_USER=<your-brevo-login-email> \
  DJANGO_EMAIL_HOST_PASSWORD=<smtp-key> \
  DJANGO_EMAIL_USE_TLS=True \
  DJANGO_DEFAULT_FROM_EMAIL="Doplent <notifications@yourdomain.com>"
```

The `notifications@yourdomain.com` from-address must be on the domain you
verified in step 2, not `@brevo.com` or any other domain.

## 5. Test it

```bash
python manage.py sendtestemail you@example.com
```

Or trigger a real substitution offer from the app and check that it lands
(also check the Brevo dashboard's **Statistics** page — it shows
delivered/bounced/blocked per message, useful if something doesn't arrive).
