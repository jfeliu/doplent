from datetime import date, datetime, time, timedelta

from django.db.models import Count
from django.utils import timezone

from teachers.models import NonTeachingHoursKind, NonTeachingHoursPriority, Teacher

from .models import Absence, Substitution

# The school's daily working hours - substitutes are never searched for
# outside these windows, regardless of any teacher's individual schedule.
WORKING_HOURS: list[tuple[time, time]] = [
    (time(9, 0), time(13, 0)),
    (time(15, 0), time(17, 0)),
]


def _daily_segments(start_dt, end_dt) -> list[tuple[date, time, time]]:
    """Split [start_dt, end_dt) into one (date, start_time, end_time) segment per
    calendar day it touches, in local time. A segment ending exactly at midnight
    does not spill into the next day."""
    start_dt = timezone.localtime(start_dt)
    end_dt = timezone.localtime(end_dt)

    last_date = end_dt.date()
    if end_dt.time() == time.min and last_date > start_dt.date():
        last_date -= timedelta(days=1)

    segments = []
    current_date = start_dt.date()
    while current_date <= last_date:
        seg_start = start_dt.time() if current_date == start_dt.date() else time.min
        seg_end = end_dt.time() if current_date == end_dt.date() else time.max
        segments.append((current_date, seg_start, seg_end))
        current_date += timedelta(days=1)
    return segments


def _merge_blocks(blocks: list[tuple[time, time]]) -> list[tuple[time, time]]:
    """Merge overlapping/touching (start, end) time blocks into maximal runs."""
    merged: list[tuple[time, time]] = []
    for start, end in sorted(blocks):
        if merged and start <= merged[-1][1]:
            if end > merged[-1][1]:
                merged[-1] = (merged[-1][0], end)
        else:
            merged.append((start, end))
    return merged


def _subtract_intervals(range_start, range_end, remove: list[tuple[datetime, datetime]]) -> list[tuple[datetime, datetime]]:
    """Return [range_start, range_end) minus the given (start, end) intervals,
    which may be unsorted and/or overlapping."""
    result = []
    cursor = range_start
    for r_start, r_end in sorted(remove):
        if r_start > cursor:
            result.append((cursor, min(r_start, range_end)))
        cursor = max(cursor, r_end)
        if cursor >= range_end:
            break
    if cursor < range_end:
        result.append((cursor, range_end))
    return result


def _outside_working_hours(start_dt, end_dt) -> list[tuple[datetime, datetime]]:
    """Datetime intervals within [start_dt, end_dt) that fall outside
    WORKING_HOURS, one entry per day touched."""
    intervals = []
    for day, seg_start, seg_end in _daily_segments(start_dt, end_dt):
        day_start = timezone.make_aware(datetime.combine(day, seg_start))
        day_end = timezone.make_aware(datetime.combine(day, seg_end))
        working_today = [
            (
                timezone.make_aware(datetime.combine(day, max(w_start, seg_start))),
                timezone.make_aware(datetime.combine(day, min(w_end, seg_end))),
            )
            for w_start, w_end in WORKING_HOURS
            if w_start < seg_end and w_end > seg_start
        ]
        intervals.extend(_subtract_intervals(day_start, day_end, working_today))
    return intervals


def _blocks_by_weekday(
    candidate: Teacher, ceiling: int | None = None, priority_by_kind: dict[str, int] | None = None
) -> dict[int, list[tuple[time, time]]]:
    """The candidate's non-teaching blocks grouped by weekday. With `ceiling`
    set, blocks whose kind ranks worse than that priority are left out - used to
    ask "could they still cover this drawing only on their more-available time?"."""
    blocks_by_weekday: dict[int, list[tuple[time, time]]] = {}
    for nth in candidate.non_teaching_hours.all():
        if ceiling is not None and priority_by_kind[nth.kind] > ceiling:
            continue
        blocks_by_weekday.setdefault(nth.weekday, []).append((nth.start_time, nth.end_time))
    return blocks_by_weekday


def _covers(blocks_by_weekday: dict[int, list[tuple[time, time]]], segments) -> bool:
    for day, seg_start, seg_end in segments:
        merged = _merge_blocks(blocks_by_weekday.get(day.weekday(), []))
        if not any(block_start <= seg_start and block_end >= seg_end for block_start, block_end in merged):
            return False
    return True


def _disruption_priority(candidate: Teacher, segments, priority_by_kind: dict[str, int]) -> int | None:
    """How disruptive it would be to pull `candidate` in for `segments`: the
    best (lowest) priority ceiling at which their blocks still fully cover every
    segment as one continuous run per day. `free` time gives the lowest number,
    `escolta'm` the highest. Returns None when even all their blocks together
    can't cover it - which is exactly the old eligibility test."""
    for ceiling in sorted(set(priority_by_kind.values())):
        if _covers(_blocks_by_weekday(candidate, ceiling, priority_by_kind), segments):
            return ceiling
    return None


def _coverage_segments(absence: Absence, start_dt, end_dt) -> list[tuple[date, time, time]]:
    """Segments within [start_dt, end_dt) that actually need a substitute -
    excludes any time the absent teacher's own weekly non-teaching hours
    already cover (no one needs to cover a class that wasn't happening
    anyway), and any time outside the school's working hours."""
    requester_free = _merged_intervals_for_range(absence.teacher, start_dt, end_dt)
    non_working = _outside_working_hours(start_dt, end_dt)
    needed_ranges = _subtract_intervals(start_dt, end_dt, requester_free + non_working)
    return [segment for r_start, r_end in needed_ranges for segment in _daily_segments(r_start, r_end)]


def _candidate_pool(absence: Absence, start_dt, end_dt):
    """Teachers who could conceivably cover part of [start_dt, end_dt): active,
    not the absent teacher, and not away on their own absence then. Teachers
    already substituting elsewhere are still included - they're shown in the
    picker, just not selectable."""
    busy_with_own_absence = (
        Absence.objects.filter(start_datetime__lt=end_dt, end_datetime__gt=start_dt)
        .exclude(pk=absence.pk)
        .values_list("teacher_id", flat=True)
    )
    return (
        Teacher.objects.filter(active=True)
        .exclude(pk=absence.teacher_id)
        .exclude(pk__in=list(busy_with_own_absence))
        .prefetch_related("non_teaching_hours")
    )


def _find_available_substitutes_for_range(absence: Absence, start_dt, end_dt) -> list[Teacher]:
    """Same as `find_available_substitutes`, but scoped to an explicit
    [start_dt, end_dt) window instead of the absence's own range - used both
    for the absence as a whole and for each split-off period within it.

    Each returned Teacher carries `disruption_priority` (the priority of the
    least-available kind of non-teaching time they'd have to be pulled off, per
    NonTeachingHoursPriority), `is_fully_free` (that kind is the top-priority
    one) and `coverage_kind_label` (its translated label), alongside
    `same_grade` and `already_substituting`."""
    segments = _coverage_segments(absence, start_dt, end_dt)

    busy_substituting = set(
        Substitution.objects.filter(
            start_datetime__lt=end_dt, end_datetime__gt=start_dt
        ).values_list("substitute_teacher_id", flat=True)
    )

    candidates = (
        _candidate_pool(absence, start_dt, end_dt)
        .annotate(substitutions_count=Count("substitutions_done"))
        .order_by("substitutions_count", "user__last_name", "user__first_name")
    )

    priority_by_kind = NonTeachingHoursPriority.ordering_map()
    best_priority = min(priority_by_kind.values())
    label_by_priority: dict[int, str] = {}
    for kind, priority in priority_by_kind.items():
        label = str(NonTeachingHoursKind(kind).label)
        label_by_priority[priority] = (
            f"{label_by_priority[priority]} / {label}" if priority in label_by_priority else label
        )

    eligible = []
    for candidate in candidates:
        disruption = _disruption_priority(candidate, segments, priority_by_kind)
        if disruption is None:
            continue
        candidate.disruption_priority = disruption
        candidate.is_fully_free = disruption == best_priority
        candidate.coverage_kind_label = label_by_priority[disruption]
        candidate.same_grade = candidate.grade_level == absence.teacher.grade_level
        candidate.already_substituting = candidate.pk in busy_substituting
        eligible.append(candidate)

    # Stable sort: candidates keep their existing substitutions_count/name order
    # within each tier, since the loop above preserved the queryset's ordering
    # and Python's sort is stable.
    eligible.sort(
        key=lambda candidate: (
            0 if candidate.same_grade else 1,
            1 if candidate.already_substituting else 0,
            candidate.disruption_priority,
        )
    )
    return eligible


def find_available_substitutes(absence: Absence) -> list[Teacher]:
    """Return teachers eligible to cover `absence`, ranked first by whether they
    teach the same grade level as the absent teacher (other grades are still
    shown, just deprioritized), then by whether they're already committed to
    substitute elsewhere during the window (shown for visibility, but not
    selectable), then by how disruptive pulling them in would be (free time
    first, then paperwork, co-teaching, escolta'm - the order is configurable
    via NonTeachingHoursPriority), then by fewest substitutions already done
    (ascending), then name. Eligibility requires: not the absent teacher,
    active, some combination of non-teaching blocks (any kind) covering the
    whole requested window, and not out themselves (own absence) during it.

    Each returned Teacher has `same_grade`, `already_substituting`,
    `disruption_priority`, `is_fully_free` and `coverage_kind_label` attributes
    set, so callers (e.g. templates) can show which tier a candidate falls in
    and whether they can be picked."""
    return _find_available_substitutes_for_range(absence, absence.start_datetime, absence.end_datetime)


def uncovered_ranges(absence: Absence) -> list[tuple[datetime, datetime]]:
    """Return the gaps in `absence`'s time range not yet covered by any
    confirmed Substitution, as a list of (start, end) datetime pairs in
    chronological order. Time that falls within the absent teacher's own
    non-teaching hours, or outside the school's working hours, is never a
    gap - there's nothing to cover then. Empty once the absence is fully
    covered."""
    requester_free = _merged_intervals_for_range(absence.teacher, absence.start_datetime, absence.end_datetime)
    non_working = _outside_working_hours(absence.start_datetime, absence.end_datetime)
    needing_coverage = _subtract_intervals(
        absence.start_datetime, absence.end_datetime, requester_free + non_working
    )
    covered = [(sub.start_datetime, sub.end_datetime) for sub in absence.substitutions.all()]
    gaps = []
    for need_start, need_end in needing_coverage:
        gaps.extend(_subtract_intervals(need_start, need_end, covered))
    return gaps


def discarded_periods(absence: Absence) -> list[dict]:
    """Return the parts of `absence`'s reported range that need no substitute,
    each as a dict with `start_datetime`, `end_datetime` and `reason` - either
    "outside_working_hours" or "requester_free" - in chronological order.
    When both apply to a stretch of time, "outside_working_hours" wins since
    it's the more fundamental reason. Adjacent stretches with the same reason
    are merged into one period."""
    non_working = _outside_working_hours(absence.start_datetime, absence.end_datetime)
    requester_free = _merged_intervals_for_range(absence.teacher, absence.start_datetime, absence.end_datetime)

    boundaries = {absence.start_datetime, absence.end_datetime}
    for start, end in non_working + requester_free:
        if absence.start_datetime < start < absence.end_datetime:
            boundaries.add(start)
        if absence.start_datetime < end < absence.end_datetime:
            boundaries.add(end)
    boundaries = sorted(boundaries)

    periods = []
    for seg_start, seg_end in zip(boundaries, boundaries[1:]):
        if any(start <= seg_start < end for start, end in non_working):
            reason = "outside_working_hours"
        elif any(start <= seg_start < end for start, end in requester_free):
            reason = "requester_free"
        else:
            reason = None

        if reason and periods and periods[-1]["reason"] == reason and periods[-1]["end_datetime"] == seg_start:
            periods[-1]["end_datetime"] = seg_end
        elif reason:
            periods.append({"start_datetime": seg_start, "end_datetime": seg_end, "reason": reason})
    return periods


def _merged_intervals_for_range(candidate: Teacher, start_dt, end_dt) -> list[tuple[datetime, datetime]]:
    """Return the candidate's free time within [start_dt, end_dt) as a list of
    aware (start, end) datetime intervals, built from their weekly
    non-teaching hours (merged per day) and clipped to the requested range.
    All kinds of non-teaching block count equally here - the kind only affects
    ranking (see `_disruption_priority`), not what a teacher can be pulled off
    to cover, nor what counts as the requester not teaching anyway."""
    segments = _daily_segments(start_dt, end_dt)
    blocks_by_weekday = _blocks_by_weekday(candidate)
    intervals = []
    for day, seg_start, seg_end in segments:
        for block_start, block_end in _merge_blocks(blocks_by_weekday.get(day.weekday(), [])):
            clipped_start = max(block_start, seg_start)
            clipped_end = min(block_end, seg_end)
            if clipped_start < clipped_end:
                intervals.append(
                    (
                        timezone.make_aware(datetime.combine(day, clipped_start)),
                        timezone.make_aware(datetime.combine(day, clipped_end)),
                    )
                )
    return intervals


def _partition_by_availability(
    absence: Absence, gap_start, gap_end
) -> list[tuple[datetime, datetime, list[Teacher]]]:
    """Split [gap_start, gap_end) at each point where the set of available
    substitutes changes, merging back adjacent stretches that share the same
    set. Returns [(start, end, candidates), ...], candidates as from
    `find_available_substitutes` scoped to that stretch. A single entry means
    the same teachers are free right across the gap - nothing to split."""
    points = {gap_start, gap_end}
    for candidate in _candidate_pool(absence, gap_start, gap_end):
        for free_start, free_end in _merged_intervals_for_range(candidate, gap_start, gap_end):
            points.update((free_start, free_end))
    points = sorted(point for point in points if gap_start <= point <= gap_end)

    boundaries = [gap_start]
    previous_ids = None
    for segment_start, segment_end in zip(points, points[1:]):
        ids = frozenset(
            candidate.pk
            for candidate in _find_available_substitutes_for_range(absence, segment_start, segment_end)
        )
        if previous_ids is not None and ids != previous_ids:
            boundaries.append(segment_start)
        previous_ids = ids
    boundaries.append(gap_end)

    return [
        (start, end, _find_available_substitutes_for_range(absence, start, end))
        for start, end in zip(boundaries, boundaries[1:])
    ]


def build_coverage_plan(absence: Absence) -> list[dict]:
    """Return one entry per gap in `absence` not yet covered by a confirmed
    Substitution, in chronological order. Each entry is a dict:

      start_datetime, end_datetime - the gap
      whole    - teachers who can cover the entire gap (as
                 `find_available_substitutes`); [] when none can
      parts    - an alternative breakdown of the gap into consecutive
                 stretches, each a dict with start_datetime / end_datetime /
                 candidates; [] unless splitting between teachers actually helps
      coverable - whether the gap can be covered at all, whole or split

    `whole` and `parts` are both offered whenever both are possible, so the
    picker can hand the whole gap to one teacher or split it between several."""
    plan = []
    for gap_start, gap_end in uncovered_ranges(absence):
        whole = _find_available_substitutes_for_range(absence, gap_start, gap_end)
        whole_pickable = any(not candidate.already_substituting for candidate in whole)

        partition = _partition_by_availability(absence, gap_start, gap_end)
        splittable = len(partition) > 1 and all(
            any(not candidate.already_substituting for candidate in candidates)
            for _, _, candidates in partition
        )
        parts = (
            [
                {"start_datetime": start, "end_datetime": end, "candidates": candidates}
                for start, end, candidates in partition
            ]
            if splittable
            else []
        )
        if splittable and not whole_pickable:
            whole = []

        plan.append(
            {
                "start_datetime": gap_start,
                "end_datetime": gap_end,
                "whole": whole,
                "parts": parts,
                "coverable": whole_pickable or splittable,
            }
        )
    return plan
