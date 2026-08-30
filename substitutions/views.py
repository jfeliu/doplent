from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.dateparse import parse_datetime
from django.utils.translation import gettext as _

from teachers.models import Teacher

from . import emails
from .forms import AbsenceForm
from .models import Absence, Substitution, SubstitutionOffer
from .offers import accept_offer, create_offer, decline_offer, expire_stale_offers
from .services import build_coverage_grid, can_offer, discarded_periods, uncovered_ranges


@login_required
def dashboard(request):
    teacher = get_object_or_404(Teacher, user=request.user)
    expire_stale_offers()

    my_absences = list(
        teacher.absences.prefetch_related("substitutions__substitute_teacher").annotate(
            pending_offers_count=Count(
                "offers", filter=Q(offers__status=SubstitutionOffer.Status.PENDING)
            )
        )
    )
    for absence in my_absences:
        absence.is_fully_covered = not uncovered_ranges(absence)

    covering = Substitution.objects.filter(substitute_teacher=teacher).select_related(
        "absence", "absence__teacher"
    )
    my_pending_offers = (
        SubstitutionOffer.objects.filter(substitute_teacher=teacher, status=SubstitutionOffer.Status.PENDING)
        .select_related("absence", "absence__teacher")
        .order_by("start_datetime")
    )
    return render(
        request,
        "substitutions/dashboard.html",
        {
            "teacher": teacher,
            "my_absences": my_absences,
            "covering": covering,
            "my_pending_offers": my_pending_offers,
        },
    )


@login_required
def report_absence(request):
    teacher = get_object_or_404(Teacher, user=request.user)
    if request.method == "POST":
        form = AbsenceForm(request.POST, teacher=teacher)
        if form.is_valid():
            absence = form.save(commit=False)
            absence.teacher = teacher
            absence.save()
            return redirect("pick_substitute", absence_id=absence.pk)
    else:
        form = AbsenceForm()
    return render(request, "substitutions/report_absence.html", {"form": form})


@login_required
def pick_substitute(request, absence_id):
    teacher = get_object_or_404(Teacher, user=request.user)
    absence = get_object_or_404(Absence, pk=absence_id, teacher=teacher)
    expire_stale_offers()

    if not uncovered_ranges(absence):
        return redirect("dashboard")

    if request.method == "POST":
        slot_start = parse_datetime(request.POST.get("slot_start", ""))
        slot_end = parse_datetime(request.POST.get("slot_end", ""))
        chosen = Teacher.objects.filter(pk=request.POST.get("substitute_id") or 0).first()
        if chosen is not None and can_offer(absence, chosen, slot_start, slot_end):
            offer, created = create_offer(absence, chosen, slot_start, slot_end)
            if created:
                emails.send_offer_notification(request, offer)
                messages.info(request, _("%(teacher)s chosen, awaiting confirmation.") % {"teacher": chosen})
        else:
            messages.info(request, _("That period is no longer available - it may have just been covered."))
        return redirect("pick_substitute", absence_id=absence.pk)

    return render(
        request,
        "substitutions/pick_substitute.html",
        {
            "absence": absence,
            "grid": build_coverage_grid(absence),
            "discarded_periods": discarded_periods(absence),
        },
    )


@login_required
def respond_to_offer(request, offer_id):
    teacher = get_object_or_404(Teacher, user=request.user)
    offer = get_object_or_404(SubstitutionOffer, pk=offer_id, substitute_teacher=teacher)

    if request.method == "POST" and offer.status == SubstitutionOffer.Status.PENDING:
        action = request.POST.get("action")
        if action == "accept":
            substitution = accept_offer(offer)
            if substitution is not None:
                emails.send_confirmation(substitution)
                emails.send_absence_covered_notification(substitution)
                messages.info(request, _("You're confirmed to cover this."))
            else:
                messages.info(request, _("Sorry - this slot is no longer available."))
        elif action == "decline":
            if decline_offer(offer):
                emails.send_offer_declined_notification(offer)
                messages.info(request, _("You've declined this offer."))
        return redirect("dashboard")

    return render(request, "substitutions/respond_to_offer.html", {"offer": offer})
