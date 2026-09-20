from collections import defaultdict
from datetime import time
from urllib.parse import urlencode

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.translation import gettext as _

from schools.decorators import school_staff_required
from teachers.models import Teacher

from .calendar import build_teacher_week
from .models import MAX_CO_TEACHERS, ClassGroup, ScheduleEntry, Subject
from .services import (
    build_group_grid,
    format_hours,
    group_hours_summary,
    hours_by_teacher,
    merge_contiguous_slots,
    split_entry_slots,
    teacher_hours_summary,
    teacher_options_for,
)


@school_staff_required
def group_list(request):
    groups = ClassGroup.objects.for_school(request.school).prefetch_related("tutors__user")
    return render(request, "schedule/group_list.html", {"groups": groups})


@school_staff_required
def teacher_list(request):
    teachers = Teacher.objects.filter(active=True, school=request.school).select_related("user")
    return render(request, "schedule/teacher_list.html", {"teachers": teachers})


@school_staff_required
def teacher_calendar(request, teacher_id):
    teacher = get_object_or_404(Teacher, pk=teacher_id, school=request.school)
    context = {
        "teacher": teacher,
        "hours_label": format_hours(hours_by_teacher([teacher.pk]).get(teacher.pk, 0)),
        "all_groups": ClassGroup.objects.for_school(request.school).prefetch_related("tutors__user"),
        "all_teachers": Teacher.objects.filter(active=True, school=request.school).select_related("user"),
        "teacher_hours_summary": teacher_hours_summary(request.school),
        "group_hours_summary": group_hours_summary(request.school),
        **build_teacher_week(teacher),
    }
    return render(request, "schedule/teacher_calendar.html", context)


def _subject_from_post(value, school) -> Subject | None:
    """The Subject for a posted id, or None - a plain `.filter(pk=value)`
    raises an uncaught ValueError (not a ValidationError) when `value` isn't
    numeric, since that's how the database backend rejects a bad pk lookup."""
    try:
        return Subject.objects.filter(pk=value, school=school).first()
    except (ValueError, TypeError):
        return None


def _teacher_from_post(value, school) -> Teacher | None:
    """The active Teacher for a posted id, or None - see _subject_from_post
    for why this can't just be `Teacher.objects.filter(pk=value).first()`."""
    try:
        return Teacher.objects.filter(pk=value, active=True, school=school).first()
    except (ValueError, TypeError):
        return None


def _redirect_keeping_selection(group_id, subject_id=None, teacher_id=None, co_teacher_ids=()):
    """Redirect back to the grid, carrying the subject/teacher/co-teachers
    just used as query params so the form re-selects them instead of
    resetting to the first option - picking a class group's day's worth of
    slots for the same subject/teacher usually takes several submissions in
    a row."""
    url = reverse("group_schedule", args=[group_id])
    if subject_id is None or teacher_id is None:
        return redirect(url)
    query = urlencode([("subject", subject_id), ("teacher", teacher_id), *(("co_teacher", i) for i in co_teacher_ids)])
    return redirect(f"{url}?{query}")


def _co_teacher_ids(request) -> list[int]:
    """The distinct, active teacher ids POSTed across every "co_teacher"
    dropdown (there can be several - see the "+ Add co-teacher" button),
    in the order picked, capped at MAX_CO_TEACHERS."""
    raw_ids = []
    for value in request.POST.getlist("co_teacher")[:MAX_CO_TEACHERS]:
        try:
            raw_ids.append(int(value))
        except (TypeError, ValueError):
            continue
    active_ids = set(
        Teacher.objects.filter(pk__in=raw_ids, active=True, school=request.school).values_list("pk", flat=True)
    )
    seen: list[int] = []
    for teacher_id in raw_ids:
        if teacher_id in active_ids and teacher_id not in seen:
            seen.append(teacher_id)
    return seen


@school_staff_required
def group_schedule(request, group_id):
    class_group = get_object_or_404(ClassGroup, pk=group_id, school=request.school)
    subjects = list(Subject.objects.for_school(request.school))
    teacher_options = teacher_options_for(class_group)

    if request.method == "POST":
        subject = _subject_from_post(request.POST.get("subject"), request.school)
        teacher = _teacher_from_post(request.POST.get("teacher"), request.school)
        co_teacher_ids = _co_teacher_ids(request)
        if subject is not None and teacher is not None:
            _assign_selected_cells(request, class_group, subject, teacher, co_teacher_ids)
        return _redirect_keeping_selection(
            group_id, subject and subject.pk, teacher and teacher.pk, co_teacher_ids
        )

    selected_teacher_id = _int_or_none(request.GET.get("teacher"))
    selected_co_teachers = _teachers_in_order(_int_list(request.GET.getlist("co_teacher")), request.school)
    context = {
        "class_group": class_group,
        "subjects": subjects,
        "teacher_options": teacher_options,
        "selected_subject_id": _int_or_none(request.GET.get("subject")),
        "selected_teacher_id": selected_teacher_id,
        "selected_co_teachers": selected_co_teachers,
        "all_groups": ClassGroup.objects.for_school(request.school).prefetch_related("tutors__user"),
        "all_teachers": Teacher.objects.filter(active=True, school=request.school).select_related("user"),
        "teacher_hours_summary": teacher_hours_summary(request.school),
        "group_hours_summary": group_hours_summary(request.school),
        **build_group_grid(class_group),
    }
    return render(request, "schedule/group_schedule.html", context)


def _int_or_none(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _int_list(values) -> list[int]:
    result = []
    for value in values:
        parsed = _int_or_none(value)
        if parsed is not None:
            result.append(parsed)
    return result


def _teachers_in_order(teacher_ids: list[int], school) -> list[Teacher]:
    """The Teacher rows for `teacher_ids`, in that same order - a plain
    `filter(pk__in=...)` would reorder them by the model's default ordering
    instead of preserving the order the user picked them in."""
    by_id = {
        t.pk: t for t in Teacher.objects.filter(pk__in=teacher_ids, school=school).select_related("user")
    }
    return [by_id[i] for i in teacher_ids if i in by_id]


def _assign_selected_cells(request, class_group, subject, teacher, co_teacher_ids=()):
    """Turn the grid's checked `cell` values ("<weekday>:<start 'HH:MM'>")
    into one ScheduleEntry per contiguous run of slots, for the subject,
    teacher and co-teachers picked in the selectors above the grid."""
    by_weekday: dict[int, list[tuple[time, time]]] = defaultdict(list)
    for value in request.POST.getlist("cell"):
        weekday_part, sep, start_part = value.partition(":")
        if not (sep and weekday_part.isdigit()):
            continue
        try:
            hour, minute = (int(part) for part in start_part.split(":"))
            start_time = time(hour, minute)
            end_time = time(hour, minute + 30) if minute < 30 else time(hour + 1, 0)
        except ValueError:
            continue
        by_weekday[int(weekday_part)].append((start_time, end_time))

    created, failed = 0, 0
    for weekday, slots in by_weekday.items():
        for start_time, end_time in merge_contiguous_slots(slots):
            entry = ScheduleEntry(
                class_group=class_group, subject=subject, teacher=teacher,
                weekday=weekday, start_time=start_time, end_time=end_time,
            )
            try:
                entry.full_clean()
                entry.validate_co_teachers(co_teacher_ids)
            except ValidationError as exc:
                failed += 1
                messages.error(request, " ".join(exc.messages))
            else:
                entry.save()
                entry.co_teachers.set(co_teacher_ids)
                created += 1

    if created:
        messages.info(request, _("%(count)s slots added to the timetable.") % {"count": created})
    elif not failed:
        messages.info(request, _("Nothing selected."))


def _selected_slots_by_entry(request) -> dict[int, set[time]]:
    """entry_id -> set of slot start times, from the grid's checked
    `occupied_cell` values ("<weekday>:<start 'HH:MM'>|<entry_id>") - lets an
    edit or delete apply to only part of an existing entry's span instead of
    the whole thing."""
    by_entry: dict[int, set[time]] = defaultdict(set)
    for value in request.POST.getlist("occupied_cell"):
        left, sep, entry_part = value.rpartition("|")
        if not (sep and entry_part.isdigit()):
            continue
        weekday_part, sep2, start_part = left.partition(":")
        if not (sep2 and weekday_part.isdigit()):
            continue
        try:
            hour, minute = (int(part) for part in start_part.split(":"))
            slot_start = time(hour, minute)
        except ValueError:
            continue
        by_entry[int(entry_part)].add(slot_start)
    return by_entry


@school_staff_required
def edit_selected(request, group_id):
    """Edit exactly the grid slots checked for one or more existing entries:
    each affected entry is split into what's kept (unchanged, under its
    original subject/teacher) and what's selected (re-created under the
    subject/teacher/co-teacher picked above the grid)."""
    class_group = get_object_or_404(ClassGroup, pk=group_id, school=request.school)
    if request.method != "POST":
        return redirect("group_schedule", group_id=group_id)

    subject = _subject_from_post(request.POST.get("subject"), request.school)
    teacher = _teacher_from_post(request.POST.get("teacher"), request.school)
    co_teacher_ids = _co_teacher_ids(request)
    by_entry = _selected_slots_by_entry(request)

    updated, failed = 0, 0
    if subject is not None and teacher is not None:
        for entry_id, selected_starts in by_entry.items():
            entry = ScheduleEntry.objects.filter(pk=entry_id, class_group=class_group).first()
            if entry is None:
                continue
            kept, selected = split_entry_slots(entry, selected_starts)
            if not selected:
                continue
            weekday = entry.weekday
            original_subject, original_teacher = entry.subject, entry.teacher
            original_co_teacher_ids = list(entry.co_teachers.values_list("pk", flat=True))
            try:
                with transaction.atomic():
                    entry.delete()
                    for start, end in kept:
                        kept_entry = ScheduleEntry.objects.create(
                            class_group=class_group, subject=original_subject, teacher=original_teacher,
                            weekday=weekday, start_time=start, end_time=end,
                        )
                        kept_entry.co_teachers.set(original_co_teacher_ids)
                    for start, end in selected:
                        new_entry = ScheduleEntry(
                            class_group=class_group, subject=subject, teacher=teacher,
                            weekday=weekday, start_time=start, end_time=end,
                        )
                        new_entry.full_clean()
                        new_entry.validate_co_teachers(co_teacher_ids)
                        new_entry.save()
                        new_entry.co_teachers.set(co_teacher_ids)
                        updated += 1
            except ValidationError as exc:
                failed += 1
                messages.error(request, " ".join(exc.messages))

    if updated:
        messages.info(request, _("%(count)s slots updated.") % {"count": updated})
    elif not failed:
        messages.info(request, _("Nothing selected."))
    return _redirect_keeping_selection(
        group_id, subject and subject.pk, teacher and teacher.pk, co_teacher_ids
    )


@school_staff_required
def delete_selected(request, group_id):
    """Remove exactly the grid slots checked for one or more existing
    entries - the rest of each entry's span (if any) is kept untouched."""
    class_group = get_object_or_404(ClassGroup, pk=group_id, school=request.school)
    picked_subject_id = _int_or_none(request.POST.get("subject"))
    picked_teacher_id = _int_or_none(request.POST.get("teacher"))
    if request.method == "POST":
        for entry_id, selected_starts in _selected_slots_by_entry(request).items():
            entry = ScheduleEntry.objects.filter(pk=entry_id, class_group=class_group).first()
            if entry is None:
                continue
            kept, selected = split_entry_slots(entry, selected_starts)
            if not selected:
                continue
            weekday, subject, teacher = entry.weekday, entry.subject, entry.teacher
            co_teacher_ids = list(entry.co_teachers.values_list("pk", flat=True))
            with transaction.atomic():
                entry.delete()
                for start, end in kept:
                    kept_entry = ScheduleEntry.objects.create(
                        class_group=class_group, subject=subject, teacher=teacher,
                        weekday=weekday, start_time=start, end_time=end,
                    )
                    kept_entry.co_teachers.set(co_teacher_ids)
        messages.info(request, _("Removed from the timetable."))
    return _redirect_keeping_selection(group_id, picked_subject_id, picked_teacher_id, _co_teacher_ids(request))


@school_staff_required
def delete_entry(request, group_id, entry_id):
    entry = get_object_or_404(
        ScheduleEntry, pk=entry_id, class_group_id=group_id, class_group__school=request.school
    )
    if request.method == "POST":
        entry.delete()
        messages.info(request, _("Removed from the timetable."))
    return redirect("group_schedule", group_id=group_id)


@school_staff_required
def edit_entry(request, group_id, entry_id):
    entry = get_object_or_404(
        ScheduleEntry, pk=entry_id, class_group_id=group_id, class_group__school=request.school
    )
    subjects = list(Subject.objects.for_school(request.school))
    teacher_options = teacher_options_for(entry.class_group)

    if request.method == "POST":
        subject = _subject_from_post(request.POST.get("subject"), request.school)
        teacher = _teacher_from_post(request.POST.get("teacher"), request.school)
        co_teacher_ids = _co_teacher_ids(request)
        if subject is not None and teacher is not None:
            entry.subject = subject
            entry.teacher = teacher
            try:
                entry.full_clean()
                entry.validate_co_teachers(co_teacher_ids)
            except ValidationError as exc:
                messages.error(request, " ".join(exc.messages))
            else:
                entry.save()
                entry.co_teachers.set(co_teacher_ids)
                messages.info(request, _("Timetable entry updated."))
                return redirect("group_schedule", group_id=group_id)

    return render(
        request,
        "schedule/edit_entry.html",
        {"entry": entry, "subjects": subjects, "teacher_options": teacher_options},
    )
