"""Builds the data needed to render all teachers' weekly non-teaching hours
as a week-calendar grid (CSS-positioned blocks, like a typical calendar UI)."""
from dataclasses import dataclass
from datetime import time

from .models import WeeklyNonTeachingHours

PALETTE = [
    "#4f46e5", "#0891b2", "#059669", "#d97706", "#dc2626",
    "#7c3aed", "#db2777", "#0d9488", "#ca8a04", "#2563eb",
]

DEFAULT_DAY_START = 8 * 60
DEFAULT_DAY_END = 17 * 60
PX_PER_MINUTE = 1.2


def _minutes(t: time) -> int:
    return t.hour * 60 + t.minute


@dataclass
class Block:
    teacher_name: str
    is_paperwork: bool
    start: time
    end: time
    color: str
    top: float = 0.0
    height: float = 0.0
    left: float = 0.0
    width: float = 0.0
    compact: bool = False


def build_week_calendar():
    entries = list(
        WeeklyNonTeachingHours.objects.filter(teacher__active=True)
        .select_related("teacher__user")
        .order_by("weekday", "start_time")
    )

    if entries:
        day_start = min(_minutes(e.start_time) for e in entries)
        day_end = max(_minutes(e.end_time) for e in entries)
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

    weekday_values = sorted({e.weekday for e in entries} | set(range(5)))

    by_day = {w: [] for w in weekday_values}
    for entry in entries:
        by_day[entry.weekday].append(entry)

    days = [
        {
            "value": weekday,
            "label": WeeklyNonTeachingHours.Weekday(weekday).label,
            "blocks": _layout_day(by_day[weekday], day_start, total_range),
        }
        for weekday in weekday_values
    ]

    return {
        "hours": hours,
        "days": days,
        "has_entries": bool(entries),
        "calendar_height": total_range * PX_PER_MINUTE,
    }


def _layout_day(entries, day_start, total_range):
    """Assigns each entry a side-by-side lane so overlapping blocks never
    visually collide, using a simple greedy interval-graph-coloring pass."""
    lane_end = []
    assignments = []
    for entry in sorted(entries, key=lambda e: _minutes(e.start_time)):
        start = _minutes(entry.start_time)
        lane = next((i for i, end in enumerate(lane_end) if end <= start), None)
        if lane is None:
            lane = len(lane_end)
            lane_end.append(0)
        lane_end[lane] = _minutes(entry.end_time)
        assignments.append((entry, lane))

    total_lanes = len(lane_end) or 1
    blocks = []
    for entry, lane in assignments:
        start = _minutes(entry.start_time)
        end = _minutes(entry.end_time)
        blocks.append(
            Block(
                teacher_name=str(entry.teacher),
                is_paperwork=entry.is_paperwork,
                start=entry.start_time,
                end=entry.end_time,
                color=PALETTE[entry.teacher_id % len(PALETTE)],
                top=(start - day_start) / total_range * 100,
                height=(end - start) / total_range * 100,
                left=lane / total_lanes * 100,
                width=100 / total_lanes,
                compact=total_lanes > 3,
            )
        )
    return blocks
