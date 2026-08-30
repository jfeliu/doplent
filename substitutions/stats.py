"""Aggregate numbers for the admin stats dashboard (see
AbsenceAdmin.stats_dashboard). Everything is counted from the start of the
current course year - see `services.course_year_start`."""

from datetime import timedelta

from django.db.models import Count, DurationField, ExpressionWrapper, F, Q, Sum
from django.db.models.functions import Coalesce
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from teachers.models import Teacher

from .models import Absence, Substitution, SubstitutionOffer
from .services import course_year_start, format_duration, uncovered_ranges

# end - start as a DurationField, for summing how much time a set of
# substitutions / offers spans.
_SPAN = ExpressionWrapper(F("end_datetime") - F("start_datetime"), output_field=DurationField())
_SUB_SPAN = ExpressionWrapper(
    F("substitutions_done__end_datetime") - F("substitutions_done__start_datetime"),
    output_field=DurationField(),
)


def _pct(part: int, whole: int) -> int:
    return round(100 * part / whole) if whole else 0


def build_admin_stats(since=None) -> dict:
    """Everything the stats dashboard template needs, counted from `since`
    (default: the start of the current course year)."""
    now = timezone.now()
    since = since or course_year_start()

    absences = list(
        Absence.objects.filter(start_datetime__gte=since)
        .select_related("teacher__user")
        .prefetch_related("substitutions", "teacher__non_teaching_hours")
    )
    fully_covered = sum(1 for absence in absences if not uncovered_ranges(absence))

    subs = Substitution.objects.filter(start_datetime__gte=since).aggregate(
        n=Count("id"), span=Coalesce(Sum(_SPAN), timedelta(), output_field=DurationField())
    )

    offers = SubstitutionOffer.objects.filter(start_datetime__gte=since)
    offers_total = offers.count()
    by_status = {row["status"]: row["n"] for row in offers.values("status").annotate(n=Count("id"))}
    status_rows = [
        {
            "label": SubstitutionOffer.Status(value).label,
            "count": by_status.get(value, 0),
            "pct": _pct(by_status.get(value, 0), offers_total),
        }
        for value in SubstitutionOffer.Status.values
    ]
    accepted = by_status.get(SubstitutionOffer.Status.ACCEPTED, 0)
    declined = by_status.get(SubstitutionOffer.Status.DECLINED, 0)

    responded = list(offers.filter(responded_at__isnull=False).values_list("created_at", "responded_at"))
    avg_response = (
        sum((responded_at - created_at for created_at, responded_at in responded), timedelta())
        / len(responded)
        if responded
        else None
    )

    decline_reason_labels = dict(SubstitutionOffer.DeclineReason.choices)
    decline_reasons = [
        {"label": decline_reason_labels.get(row["decline_reason"], row["decline_reason"]), "count": row["n"]}
        for row in offers.filter(status=SubstitutionOffer.Status.DECLINED)
        .exclude(decline_reason="")
        .values("decline_reason")
        .annotate(n=Count("id"))
        .order_by("-n")
    ]
    absence_reason_labels = dict(Absence.Reason.choices)
    absence_reasons = [
        {"label": absence_reason_labels.get(row["reason"], row["reason"] or _("Unspecified")), "count": row["n"]}
        for row in Absence.objects.filter(start_datetime__gte=since)
        .values("reason")
        .annotate(n=Count("id"))
        .order_by("-n")
    ]

    since_sub = Q(substitutions_done__start_datetime__gte=since)
    top_substitutes = [
        {"teacher": teacher, "count": teacher.subs_n, "time": format_duration(teacher.subs_span)}
        for teacher in Teacher.objects.select_related("user")
        .annotate(
            subs_n=Count("substitutions_done", filter=since_sub),
            subs_span=Coalesce(Sum(_SUB_SPAN, filter=since_sub), timedelta(), output_field=DurationField()),
        )
        .filter(subs_n__gt=0)
        .order_by("-subs_span", "user__last_name")[:10]
    ]
    busiest_absentees = [
        {"teacher": teacher, "count": teacher.abs_n}
        for teacher in Teacher.objects.select_related("user")
        .annotate(abs_n=Count("absences", filter=Q(absences__start_datetime__gte=since)))
        .filter(abs_n__gt=0)
        .order_by("-abs_n", "user__last_name")[:10]
    ]

    return {
        "generated_at": now,
        "course_start": since,
        "totals": {
            "active_teachers": Teacher.objects.filter(active=True).count(),
            "absences": len(absences),
            "substitutions": subs["n"],
            "hours_covered": format_duration(subs["span"]),
            "offers": offers_total,
        },
        "coverage": {
            "fully_covered": fully_covered,
            "not_fully_covered": len(absences) - fully_covered,
            "rate": _pct(fully_covered, len(absences)),
        },
        "offers": {
            "status_rows": status_rows,
            "acceptance_rate": _pct(accepted, accepted + declined),
            "avg_response": format_duration(avg_response) if avg_response is not None else None,
        },
        "decline_reasons": decline_reasons,
        "absence_reasons": absence_reasons,
        "top_substitutes": top_substitutes,
        "busiest_absentees": busiest_absentees,
    }
