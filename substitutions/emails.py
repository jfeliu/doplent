import datetime
import logging
from smtplib import SMTPException

from django.conf import settings
from django.core.mail import EmailMessage
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext as _

from .dates_ca import format_ca_datetime

logger = logging.getLogger(__name__)


def _send(to_teacher, subject, template_name, context, attach_ics_for=None) -> None:
    email_address = to_teacher.user.email
    if not email_address:
        logger.warning("Skipping email to %s: no email address on file.", to_teacher)
        return

    body = render_to_string(template_name, context)
    message = EmailMessage(subject, body, settings.DEFAULT_FROM_EMAIL, [email_address])
    if attach_ics_for is not None:
        message.attach("substitution.ics", _build_ics(attach_ics_for), "text/calendar")

    try:
        message.send()
    except (SMTPException, OSError):
        logger.exception("Failed to send email to %s", to_teacher)


def _ics_datetime(dt: datetime.datetime) -> str:
    return dt.astimezone(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _ics_escape(text) -> str:
    return str(text).replace("\\", "\\\\").replace(",", "\\,").replace(";", "\\;").replace("\n", "\\n")


def _build_ics(substitution) -> str:
    """A minimal, hand-rolled single-VEVENT .ics - deliberately no
    METHOD:REQUEST, so it's just "add to your calendar", not a meeting
    invite with RSVP semantics."""
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Doplent//Substitutions//EN",
        "BEGIN:VEVENT",
        f"UID:substitution-{substitution.pk}@doplent",
        f"DTSTAMP:{_ics_datetime(timezone.now())}",
        f"DTSTART:{_ics_datetime(substitution.start_datetime)}",
        f"DTEND:{_ics_datetime(substitution.end_datetime)}",
        f"SUMMARY:{_ics_escape(_('Covering for %(teacher)s') % {'teacher': substitution.absence.teacher})}",
        "END:VEVENT",
        "END:VCALENDAR",
    ]
    return "\r\n".join(lines) + "\r\n"


def send_offer_notification(request, offer) -> None:
    url = request.build_absolute_uri(reverse("respond_to_offer", args=[offer.pk]))
    subject = _("Substitution request: %(teacher)s, %(start)s") % {
        "teacher": offer.absence.teacher,
        "start": format_ca_datetime(offer.start_datetime),
    }
    _send(
        offer.substitute_teacher,
        subject,
        "substitutions/email/offer_notification.txt",
        {"offer": offer, "url": url},
    )


def send_confirmation(substitution) -> None:
    subject = _("You're confirmed to cover for %(teacher)s") % {"teacher": substitution.absence.teacher}
    _send(
        substitution.substitute_teacher,
        subject,
        "substitutions/email/confirmation.txt",
        {"substitution": substitution},
        attach_ics_for=substitution,
    )


def send_absence_covered_notification(substitution) -> None:
    subject = _("%(substitute)s will cover your absence") % {"substitute": substitution.substitute_teacher}
    _send(
        substitution.absence.teacher,
        subject,
        "substitutions/email/absence_covered.txt",
        {"substitution": substitution},
    )


def send_offer_declined_notification(offer) -> None:
    subject = _("%(teacher)s can't cover your absence") % {"teacher": offer.substitute_teacher}
    _send(offer.absence.teacher, subject, "substitutions/email/offer_declined.txt", {"offer": offer})
