from collections import defaultdict
from dataclasses import dataclass, field
from datetime import time

from django.db import models

from teachers.models import Teacher, WeeklyNonTeachingHours

from .models import ClassGroup, ScheduleEntry

# The weekly editing grid's day span - a fixed window rather than one derived
# from existing data, so an empty timetable still shows a sensible grid to
# fill in. Every row is a fixed 30-minute slot within it.
GRID_START = time(9, 0)
GRID_END = time(17, 0)
GRID_WEEKDAYS = [0, 1, 2, 3, 4]  # Monday-Friday
SLOT_MINUTES = 30

# Lunch break - no class is ever scheduled then, so instead of four ordinary
# (and always-empty) half-hour rows the grid collapses this span into one
# thin, unselectable band.
LUNCH_START = time(13, 0)
LUNCH_END = time(15, 0)

SUBJECT_PALETTE = [
    "#4f46e5", "#0891b2", "#059669", "#d97706", "#dc2626",
    "#7c3aed", "#db2777", "#0d9488", "#ca8a04", "#2563eb",
]


def _minutes(t: time) -> int:
    return t.hour * 60 + t.minute


def _time(total_minutes: int) -> time:
    return time(total_minutes // 60, total_minutes % 60)


def format_hours(total_minutes: int) -> str:
    """Render a minute count as a compact "1h 30m" / "45m" / "2h" string."""
    total_minutes = max(0, int(total_minutes))
    hours, minutes = divmod(total_minutes, 60)
    if hours and minutes:
        return f"{hours}h {minutes}m"
    if hours:
        return f"{hours}h"
    return f"{minutes}m"


def grid_time_slots() -> list[tuple[time, time]]:
    """Every 30-minute (start, end) slot between GRID_START and GRID_END,
    excluding the lunch band (see `LUNCH_START`/`LUNCH_END`)."""
    slots = []
    cursor = _minutes(GRID_START)
    end = _minutes(GRID_END)
    while cursor < end:
        start = _time(cursor)
        if start < LUNCH_START or start >= LUNCH_END:
            slots.append((start, _time(cursor + SLOT_MINUTES)))
        cursor += SLOT_MINUTES
    return slots


def _subject_colors(subjects) -> dict[int, str]:
    """Give each subject its own palette color, handed out in name order so
    it stays stable as entries are added/removed. Wraps around if there are
    more subjects than colors."""
    ordered = sorted({s.pk: s.name for s in subjects}.items(), key=lambda item: item[1].lower())
    return {pk: SUBJECT_PALETTE[index % len(SUBJECT_PALETTE)] for index, (pk, _name) in enumerate(ordered)}


def _overlaps(a_start: time, a_end: time, b_start: time, b_end: time) -> bool:
    return a_start < b_end and a_end > b_start


def _entry_minutes(entry) -> int:
    return _minutes(entry.end_time) - _minutes(entry.start_time)


def hours_by_teacher(teacher_ids=None) -> dict[int, int]:
    """Total minutes each teacher is already scheduled to teach, across every
    class group and whether as the main teacher or a co-teacher - their
    overall teaching load."""
    entries = ScheduleEntry.objects.all()
    if teacher_ids is not None:
        entries = entries.filter(
            models.Q(teacher_id__in=teacher_ids) | models.Q(co_teachers__in=teacher_ids)
        ).distinct()
    totals: dict[int, int] = defaultdict(int)
    for entry in entries.prefetch_related("co_teachers"):
        minutes = _entry_minutes(entry)
        totals[entry.teacher_id] += minutes
        for co_teacher_id in entry.co_teachers.all().values_list("pk", flat=True):
            totals[co_teacher_id] += minutes
    return totals


def non_teaching_hours_by_teacher(teacher_ids=None) -> dict[int, int]:
    """Total minutes each teacher has recorded as recurring non-teaching time
    each week (free, paperwork, co-teaching, escolta'm - see
    teachers.WeeklyNonTeachingHours)."""
    entries = WeeklyNonTeachingHours.objects.all()
    if teacher_ids is not None:
        entries = entries.filter(teacher_id__in=teacher_ids)
    totals: dict[int, int] = defaultdict(int)
    for teacher_id, start_time, end_time in entries.values_list("teacher_id", "start_time", "end_time"):
        totals[teacher_id] += _minutes(end_time) - _minutes(start_time)
    return totals


def hours_by_group(group_ids=None) -> dict[int, int]:
    """Total minutes each class group has scheduled in its own timetable."""
    entries = ScheduleEntry.objects.all()
    if group_ids is not None:
        entries = entries.filter(class_group_id__in=group_ids)
    totals: dict[int, int] = defaultdict(int)
    for group_id, start_time, end_time in entries.values_list("class_group_id", "start_time", "end_time"):
        totals[group_id] += _minutes(end_time) - _minutes(start_time)
    return totals


@dataclass
class TeacherHoursSummary:
    """One row of the "hours per teacher" table below the calendar - lets a
    coordinator see at a glance who still has room and who's already fully
    booked, teaching and non-teaching time side by side."""

    teacher: Teacher
    teaching_label: str
    non_teaching_label: str


def teacher_hours_summary() -> list[TeacherHoursSummary]:
    teachers = list(Teacher.objects.filter(active=True).select_related("user"))
    teaching = hours_by_teacher()
    non_teaching = non_teaching_hours_by_teacher()
    teachers.sort(key=lambda t: str(t).lower())
    return [
        TeacherHoursSummary(
            teacher=teacher,
            teaching_label=format_hours(teaching.get(teacher.pk, 0)),
            non_teaching_label=format_hours(non_teaching.get(teacher.pk, 0)),
        )
        for teacher in teachers
    ]


@dataclass
class GroupHoursSummary:
    """One row of the "hours per class group" table below the calendar -
    lets a coordinator see which groups' timetables are still incomplete."""

    class_group: ClassGroup
    hours_label: str


def group_hours_summary() -> list[GroupHoursSummary]:
    groups = list(ClassGroup.objects.all())
    totals = hours_by_group()
    groups.sort(key=lambda g: g.name.lower())
    return [
        GroupHoursSummary(class_group=group, hours_label=format_hours(totals.get(group.pk, 0)))
        for group in groups
    ]


@dataclass
class TeacherOption:
    """One entry in the "assign to" teacher selector - the group's tutor
    first, then everyone else alphabetically, each labelled with their
    current total teaching load so it doubles as an at-a-glance counter."""

    teacher: Teacher
    is_tutor: bool
    hours_label: str


def teacher_options_for(class_group: ClassGroup) -> list[TeacherOption]:
    teachers = list(Teacher.objects.filter(active=True).select_related("user"))
    load = hours_by_teacher()
    tutor_ids = set(class_group.tutors.values_list("pk", flat=True))
    teachers.sort(key=lambda t: (t.pk not in tutor_ids, t.user.last_name, t.user.first_name))
    return [
        TeacherOption(
            teacher=teacher,
            is_tutor=teacher.pk in tutor_ids,
            hours_label=format_hours(load.get(teacher.pk, 0)),
        )
        for teacher in teachers
    ]


@dataclass
class GridCell:
    weekday: int
    start_time: time
    end_time: time
    occupied_entry: ScheduleEntry | None = None
    color: str = ""

    @property
    def selectable(self) -> bool:
        return self.occupied_entry is None


@dataclass
class GridRow:
    start_time: time
    end_time: time
    cells: list[GridCell] = field(default_factory=list)
    is_band: bool = False


def build_group_grid(class_group: ClassGroup) -> dict:
    """The weekly editing grid for `class_group`: one row per 30-minute slot
    (Monday-Friday, GRID_START-GRID_END, lunch collapsed into one band), one
    column per weekday. A slot the group already has scheduled (any
    subject/teacher) renders as an occupied cell (not selectable), colored by
    subject; an open slot renders a checkbox - several slots across several
    days can be checked and, in one submission, assigned to a subject and
    teacher picked from the selectors above the grid.

    Returns {"rows": [GridRow, ...], "weekday_labels": [(value, label), ...],
    "group_hours_label": str}."""
    own_by_weekday: dict[int, list[ScheduleEntry]] = defaultdict(list)
    entries = class_group.schedule_entries.select_related("subject", "teacher__user").prefetch_related(
        "co_teachers__user"
    )
    for entry in entries:
        own_by_weekday[entry.weekday].append(entry)

    colors = _subject_colors({e.subject for entries in own_by_weekday.values() for e in entries})

    rows = []
    for slot_start, slot_end in grid_time_slots():
        cells = []
        for weekday in GRID_WEEKDAYS:
            occupied = next(
                (e for e in own_by_weekday.get(weekday, []) if _overlaps(e.start_time, e.end_time, slot_start, slot_end)),
                None,
            )
            cells.append(
                GridCell(
                    weekday=weekday,
                    start_time=slot_start,
                    end_time=slot_end,
                    occupied_entry=occupied,
                    color=colors.get(occupied.subject_id, "") if occupied else "",
                )
            )
        rows.append(GridRow(start_time=slot_start, end_time=slot_end, cells=cells))

    rows.append(GridRow(start_time=LUNCH_START, end_time=LUNCH_END, is_band=True))
    rows.sort(key=lambda row: row.start_time)

    group_minutes = sum(_entry_minutes(e) for entries in own_by_weekday.values() for e in entries)
    weekday_labels = [(weekday, ScheduleEntry.Weekday(weekday).label) for weekday in GRID_WEEKDAYS]

    return {"rows": rows, "weekday_labels": weekday_labels, "group_hours_label": format_hours(group_minutes)}


def merge_contiguous_slots(slots: list[tuple[time, time]]) -> list[tuple[time, time]]:
    """Merge a list of touching 30-minute (start, end) slots into maximal
    contiguous runs, e.g. [(9:00,9:30), (9:30,10:00)] -> [(9:00,10:00)]."""
    merged: list[tuple[time, time]] = []
    for start, end in sorted(slots):
        if merged and start == merged[-1][1]:
            merged[-1] = (merged[-1][0], end)
        else:
            merged.append((start, end))
    return merged


def slot_end(start_time: time) -> time:
    return _time(_minutes(start_time) + SLOT_MINUTES)


def slots_within(entry: ScheduleEntry) -> list[tuple[time, time]]:
    """Every 30-minute (start, end) slot within an existing entry's span."""
    slots = []
    cursor = _minutes(entry.start_time)
    end = _minutes(entry.end_time)
    while cursor < end:
        slots.append((_time(cursor), _time(cursor + SLOT_MINUTES)))
        cursor += SLOT_MINUTES
    return slots


def split_entry_slots(
    entry: ScheduleEntry, selected_starts: set[time]
) -> tuple[list[tuple[time, time]], list[tuple[time, time]]]:
    """Split an existing entry's half-hour slots by which ones were selected
    (picked in the grid to edit or remove), each side merged into maximal
    contiguous runs. Returns (kept, selected) - "kept" is what should remain
    under the entry's original subject/teacher, "selected" is what the
    caller is about to change or remove."""
    all_slots = slots_within(entry)
    kept = [(start, end) for start, end in all_slots if start not in selected_starts]
    selected = [(start, end) for start, end in all_slots if start in selected_starts]
    return merge_contiguous_slots(kept), merge_contiguous_slots(selected)
