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

# Whatever a day's overlap looks like, every lane keeps at least this much room
# so a teacher's name stays readable; busy days scroll sideways instead.
LANE_MIN_WIDTH = 104

# A block needs some vertical room before the name and the times can sit on
# separate lines, and a little before the times are worth showing at all. The
# name itself is always rendered.
STACKED_MIN_HEIGHT = 34
TIME_MIN_HEIGHT = 20
WRAP_MIN_HEIGHT = 52


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
    inline: bool = False
    show_time: bool = True
    wrap: bool = False


def _assign_colors(entries) -> tuple[dict[int, str], list[dict]]:
    """Give each teacher their own palette color, handed out in name order so
    the legend reads alphabetically. The palette wraps around if there are more
    teachers than colors, so two teachers can share one on a large roster."""
    names: dict[int, str] = {}
    for entry in entries:
        names.setdefault(entry.teacher_id, str(entry.teacher))

    ordered = sorted(names.items(), key=lambda item: item[1].lower())
    colors = {teacher_id: PALETTE[index % len(PALETTE)] for index, (teacher_id, _) in enumerate(ordered)}
    legend = [{"name": name, "color": colors[teacher_id]} for teacher_id, name in ordered]
    return colors, legend


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

    colors, legend = _assign_colors(entries)

    days = []
    for weekday in weekday_values:
        blocks, lanes = _layout_day(by_day[weekday], day_start, total_range, colors)
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
        "teacher_legend": legend,
    }


def _layout_day(entries, day_start, total_range, colors) -> tuple[list[Block], int]:
    """Assigns each entry a side-by-side lane so overlapping blocks never
    visually collide, using a simple greedy interval-graph-coloring pass.
    Returns the blocks plus how many lanes the day needed."""
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
        height_px = (end - start) * PX_PER_MINUTE
        blocks.append(
            Block(
                teacher_name=str(entry.teacher),
                is_paperwork=entry.is_paperwork,
                start=entry.start_time,
                end=entry.end_time,
                color=colors[entry.teacher_id],
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
