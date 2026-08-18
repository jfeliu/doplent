from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.dateparse import parse_datetime

from teachers.models import Teacher

from .forms import AbsenceForm
from .models import Absence, Substitution
from .services import build_coverage_plan, discarded_periods, uncovered_ranges


@login_required
def dashboard(request):
    teacher = get_object_or_404(Teacher, user=request.user)
    my_absences = list(teacher.absences.prefetch_related("substitutions__substitute_teacher").all())
    for absence in my_absences:
        absence.is_fully_covered = not uncovered_ranges(absence)
    covering = Substitution.objects.filter(substitute_teacher=teacher).select_related(
        "absence", "absence__teacher"
    )
    return render(
        request,
        "substitutions/dashboard.html",
        {"teacher": teacher, "my_absences": my_absences, "covering": covering},
    )


@login_required
def report_absence(request):
    teacher = get_object_or_404(Teacher, user=request.user)
    if request.method == "POST":
        form = AbsenceForm(request.POST)
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

    if not uncovered_ranges(absence):
        return redirect("dashboard")

    slots = build_coverage_plan(absence)

    if request.method == "POST":
        slot_start = parse_datetime(request.POST.get("slot_start", ""))
        slot_end = parse_datetime(request.POST.get("slot_end", ""))
        substitute_id = request.POST.get("substitute_id")
        slot = next(
            (s for s in slots if s["start_datetime"] == slot_start and s["end_datetime"] == slot_end), None
        )
        chosen = next((c for c in slot["candidates"] if str(c.pk) == substitute_id), None) if slot else None
        if chosen is not None and not chosen.already_substituting:
            Substitution.objects.create(
                absence=absence, substitute_teacher=chosen, start_datetime=slot_start, end_datetime=slot_end
            )
            if not uncovered_ranges(absence):
                return redirect("dashboard")
            return redirect("pick_substitute", absence_id=absence.pk)

    return render(
        request,
        "substitutions/pick_substitute.html",
        {"absence": absence, "slots": slots, "discarded_periods": discarded_periods(absence)},
    )
