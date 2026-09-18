"""Builds one teacher's combined weekly view: their WeeklyNonTeachingHours
blocks (not teaching) plus their ScheduleEntry blocks (teaching), across every
class group they're in. This is the "does this teacher's week actually add
up" view - a double-booking across two class groups shows up as two blocks
stacked in the same lane, exactly like an overlapping non-teaching block does
in teachers.calendar (the two are laid out with the same algorithm)."""
from dataclasses import dataclass
from datetime import time

from django.utils.translation import gettext as _

from teachers.models import WeeklyNonTeachingHours

DEFAULT_DAY_START = 8 * 60
DEFAULT_DAY_END = 17 * 60
PX_PER_MINUTE = 1.2
LANE_MIN_WIDTH = 104
STACKED_MIN_HEIGHT = 34
TIME_MIN_HEIGHT = 20
WRAP_MIN_HEIGHT = 52


def _minutes(t: time) -> int:
    return t.hour * 60 + t.minute


@dataclass
class Block:
    label: str
    detail: str
    kind: str  # a NonTeachingHoursKind value, or "teaching"
    start: time
    end: time
    top: float = 0.0
    height: float = 0.0
    left: float = 0.0
    width: float = 0.0
    inline: bool = False
    show_time: bool = True
    wrap: bool = False


def _entries_for(teacher) -> list[tuple[int, time, time, str, str, str]]:
    """Every block for `teacher`, as (weekday, start, end, kind, label, detail)
    tuples - `kind` is a NonTeachingHoursKind value for a non-teaching block,
    or "teaching" for a ScheduleEntry."""
    non_teaching = [
        (nth.weekday, nth.start_time, nth.end_time, nth.kind, str(nth.get_kind_display()), "")
        for nth in teacher.non_teaching_hours.all()
    ]
    taught = teacher.schedule_entries.select_related("class_group", "subject").prefetch_related(
        "co_teachers__user"
    )
    co_taught = teacher.co_taught_schedule_entries.select_related("class_group", "subject", "teacher__user")
    teaching = [
        (
            entry.weekday, entry.start_time, entry.end_time, "teaching", str(entry.subject),
            (
                f"{entry.class_group} (+ {', '.join(str(c) for c in entry.co_teachers.all())})"
                if entry.co_teachers.all()
                else str(entry.class_group)
            ),
        )
        for entry in taught
    ] + [
        (
            entry.weekday, entry.start_time, entry.end_time, "teaching", str(entry.subject),
            f"{entry.class_group} ({_('co-teaching with')} {entry.teacher})",
        )
        for entry in co_taught
    ]
    return non_teaching + teaching


def build_teacher_week(teacher) -> dict:
    entries = _entries_for(teacher)

    if entries:
        day_start = min(_minutes(e[1]) for e in entries)
        day_end = max(_minutes(e[2]) for e in entries)
        day_start -= day_start % 60
        if day_end % 60:
            day_end += 60 - day_end % 60
    else:
        day_start, day_end = DEFAULT_DAY_START, DEFAULT_DAY_END

    total_range = day_end - day_start

    hours = []
    t = day_start
    while t <= day_end:
        hours.append({"label": f"{t // 60:02d}:{t % 60:02d}", "top": (t - day_start) / total_range * 100})
        t += 60

    weekday_values = sorted({e[0] for e in entries} | set(range(5)))
    by_day: dict[int, list] = {w: [] for w in weekday_values}
    for entry in entries:
        by_day[entry[0]].append(entry)

    days = []
    for weekday in weekday_values:
        blocks, lanes = _layout_day(by_day[weekday], day_start, total_range)
        days.append(
            {
                "value": weekday,
                "label": WeeklyNonTeachingHours.Weekday(weekday).label,
                "blocks": blocks,
                "min_width": lanes * LANE_MIN_WIDTH,
            }
        )

    return {
        "hours": hours,
        "days": days,
        "has_entries": bool(entries),
        "calendar_height": total_range * PX_PER_MINUTE,
    }


def _layout_day(entries, day_start, total_range) -> tuple[list[Block], int]:
    """Same greedy lane-assignment as teachers.calendar._layout_day: overlapping
    blocks (e.g. a double-booking across two class groups) land in separate
    side-by-side lanes instead of hiding each other."""
    lane_end: list[int] = []
    assignments = []
    for entry in sorted(entries, key=lambda e: _minutes(e[1])):
        start = _minutes(entry[1])
        lane = next((i for i, end in enumerate(lane_end) if end <= start), None)
        if lane is None:
            lane = len(lane_end)
            lane_end.append(0)
        lane_end[lane] = _minutes(entry[2])
        assignments.append((entry, lane))

    total_lanes = len(lane_end) or 1
    blocks = []
    for (weekday, start_time, end_time, kind, label, detail), lane in assignments:
        start = _minutes(start_time)
        end = _minutes(end_time)
        height_px = (end - start) * PX_PER_MINUTE
        blocks.append(
            Block(
                label=label,
                detail=detail,
                kind=kind,
                start=start_time,
                end=end_time,
                top=(start - day_start) / total_range * 100,
                height=(end - start) / total_range * 100,
                left=lane / total_lanes * 100,
                width=100 / total_lanes,
                inline=height_px < STACKED_MIN_HEIGHT,
                show_time=height_px >= TIME_MIN_HEIGHT,
                wrap=height_px >= WRAP_MIN_HEIGHT,
            )
        )
    return blocks, total_lanes
