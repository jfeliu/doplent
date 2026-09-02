from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext as _
from django.utils.translation import ngettext

from teachers.models import Teacher

from . import emails
from .forms import AbsenceForm, DeclineOfferForm
from .models import Absence, Substitution, SubstitutionOffer
from .offers import accept_offer, create_offer, decline_offer, expire_stale_offers
from .services import (
    build_coverage_grid,
    can_offer,
    coverage_done_for,
    course_year_start,
    format_duration,
    grid_slots,
    uncovered_ranges,
)


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

    covering = (
        Substitution.objects.filter(
            substitute_teacher=teacher, start_datetime__gte=course_year_start()
        )
        .select_related("absence", "absence__teacher")
        .order_by("start_datetime")
    )
    covered_total_label = format_duration(coverage_done_for(teacher))
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
            "covered_total_label": covered_total_label,
            "course_year_start": course_year_start(),
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
        _send_offers(request, absence)
        return redirect("pick_substitute", absence_id=absence.pk)

    return render(
        request,
        "substitutions/pick_substitute.html",
        {"absence": absence, "grid": build_coverage_grid(absence)},
    )


def _send_offers(request, absence):
    """Turn the grid's checked `cell` values ("<teacher_pk>:<slot_index>") into
    one offer per contiguous run of slots per teacher, skipping any that don't
    pass `can_offer`."""
    slot_bounds = {slot["index"]: slot for slot in grid_slots(absence)}
    by_teacher: dict[int, list[int]] = {}
    for value in request.POST.getlist("cell"):  # each is "<teacher_pk>:<slot_index>"
        teacher_part, sep, slot_part = value.partition(":")
        if sep and teacher_part.isdigit() and slot_part.isdigit() and int(slot_part) in slot_bounds:
            by_teacher.setdefault(int(teacher_part), []).append(int(slot_part))

    sent = 0
    for teacher_id, indexes in by_teacher.items():
        teacher = Teacher.objects.filter(pk=teacher_id).first()
        if teacher is None:
            continue
        for run_start, run_end in _contiguous_runs(sorted(set(indexes))):
            start = slot_bounds[run_start]["start_datetime"]
            end = slot_bounds[run_end]["end_datetime"]
            if not can_offer(absence, teacher, start, end):
                continue
            offer, created = create_offer(absence, teacher, start, end)
            if created:
                emails.send_offer_notification(request, offer)
                sent += 1

    if sent:
        messages.info(
            request,
            ngettext("%(count)s offer sent.", "%(count)s offers sent.", sent) % {"count": sent},
        )
    else:
        messages.info(request, _("Nothing offered - those periods may have just been taken."))


def _contiguous_runs(indexes):
    """[0, 1, 3, 4, 5] -> [(0, 1), (3, 5)]."""
    runs = []
    for index in indexes:
        if runs and index == runs[-1][1] + 1:
            runs[-1] = (runs[-1][0], index)
        else:
            runs.append((index, index))
    return runs


@login_required
def respond_to_offer(request, offer_id):
    teacher = get_object_or_404(Teacher, user=request.user)
    offer = get_object_or_404(SubstitutionOffer, pk=offer_id, substitute_teacher=teacher)
    decline_form = DeclineOfferForm()

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
            return redirect("dashboard")
        elif action == "decline":
            decline_form = DeclineOfferForm(request.POST)
            if decline_form.is_valid():
                if decline_offer(
                    offer, decline_form.cleaned_data["reason"], decline_form.cleaned_data["detail"]
                ):
                    emails.send_offer_declined_notification(offer)
                    messages.info(request, _("You've declined this offer."))
                return redirect("dashboard")

    return render(
        request,
        "substitutions/respond_to_offer.html",
        {"offer": offer, "decline_form": decline_form},
    )
