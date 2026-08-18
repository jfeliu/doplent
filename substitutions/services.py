from datetime import date, datetime, time, timedelta

from django.db.models import Count
from django.utils import timezone

from teachers.models import Teacher

from .models import Absence, Substitution


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


def _blocks_by_weekday(candidate: Teacher, only_non_paperwork: bool = False) -> dict[int, list[tuple[time, time]]]:
    blocks_by_weekday: dict[int, list[tuple[time, time]]] = {}
    for nth in candidate.non_teaching_hours.all():
        if only_non_paperwork and nth.is_paperwork:
            continue
        blocks_by_weekday.setdefault(nth.weekday, []).append((nth.start_time, nth.end_time))
    return blocks_by_weekday


def _covers(blocks_by_weekday: dict[int, list[tuple[time, time]]], segments) -> bool:
    for day, seg_start, seg_end in segments:
        merged = _merge_blocks(blocks_by_weekday.get(day.weekday(), []))
        if not any(block_start <= seg_start and block_end >= seg_end for block_start, block_end in merged):
            return False
    return True


def _is_free_during(candidate: Teacher, segments) -> bool:
    """A candidate is free for a segment only if their non-teaching blocks for
    that weekday - merged together - fully cover it as one continuous run.
    Paperwork blocks count here: the teacher is still on campus and available,
    just not idle."""
    return _covers(_blocks_by_weekday(candidate), segments)


def _has_nothing_to_do_during(candidate: Teacher, segments) -> bool:
    """True if the candidate's coverage can be satisfied using only non-paperwork
    blocks - i.e. pulling them in wouldn't interrupt any paperwork/admin time."""
    return _covers(_blocks_by_weekday(candidate, only_non_paperwork=True), segments)


def _find_available_substitutes_for_range(absence: Absence, start_dt, end_dt) -> list[Teacher]:
    """Same as `find_available_substitutes`, but scoped to an explicit
    [start_dt, end_dt) window instead of the absence's own range - used both
    for the absence as a whole and for each split-off period within it."""
    segments = _daily_segments(start_dt, end_dt)

    busy_with_own_absence = Absence.objects.filter(
        start_datetime__lt=end_dt, end_datetime__gt=start_dt
    ).exclude(pk=absence.pk).values_list("teacher_id", flat=True)

    busy_substituting = set(
        Substitution.objects.filter(
            start_datetime__lt=end_dt, end_datetime__gt=start_dt
        ).values_list("substitute_teacher_id", flat=True)
    )

    candidates = (
        Teacher.objects.filter(active=True)
        .exclude(pk=absence.teacher_id)
        .exclude(pk__in=list(busy_with_own_absence))
        .prefetch_related("non_teaching_hours")
        .annotate(substitutions_count=Count("substitutions_done"))
        .order_by("substitutions_count", "user__last_name", "user__first_name")
    )

    eligible = [candidate for candidate in candidates if _is_free_during(candidate, segments)]
    for candidate in eligible:
        candidate.same_grade = candidate.grade_level == absence.teacher.grade_level
        candidate.already_substituting = candidate.pk in busy_substituting
        candidate.has_nothing_to_do = _has_nothing_to_do_during(candidate, segments)

    # Stable sort: candidates keep their existing substitutions_count/name order
    # within each tier, since list comprehension above preserved the queryset's
    # ordering and Python's sort is stable.
    eligible.sort(
        key=lambda candidate: (
            0 if candidate.same_grade else 1,
            1 if candidate.already_substituting else 0,
            0 if candidate.has_nothing_to_do else 1,
        )
    )
    return eligible


def find_available_substitutes(absence: Absence) -> list[Teacher]:
    """Return teachers eligible to cover `absence`, ranked first by whether they
    teach the same grade level as the absent teacher (other grades are still
    shown, just deprioritized), then by whether they're already committed to
    substitute elsewhere during the window (shown for visibility, but not
    selectable), then by whether they have nothing to do (fully free, not doing
    paperwork), then by fewest substitutions already done (ascending), then
    name. Eligibility requires: not the absent teacher, active, free
    (non-teaching, paperwork or not) for the whole requested window, and not
    out themselves (own absence) during that window.

    Each returned Teacher has `same_grade`, `already_substituting` and
    `has_nothing_to_do` attributes set, so callers (e.g. templates) can show
    which tier a candidate falls in and whether they can be picked."""
    return _find_available_substitutes_for_range(absence, absence.start_datetime, absence.end_datetime)


def uncovered_ranges(absence: Absence) -> list[tuple[datetime, datetime]]:
    """Return the gaps in `absence`'s time range not yet covered by any
    confirmed Substitution, as a list of (start, end) datetime pairs in
    chronological order. Empty once the absence is fully covered."""
    covered = sorted((sub.start_datetime, sub.end_datetime) for sub in absence.substitutions.all())
    gaps = []
    cursor = absence.start_datetime
    for start, end in covered:
        if start > cursor:
            gaps.append((cursor, start))
        cursor = max(cursor, end)
    if cursor < absence.end_datetime:
        gaps.append((cursor, absence.end_datetime))
    return gaps


def _merged_intervals_for_range(candidate: Teacher, start_dt, end_dt) -> list[tuple[datetime, datetime]]:
    """Return the candidate's free time within [start_dt, end_dt) as a list of
    aware (start, end) datetime intervals, built from their weekly
    non-teaching hours (merged per day) and clipped to the requested range."""
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


def _greedy_min_cover(intervals, range_start, range_end):
    """Classic greedy minimum-interval-cover: return the fewest (start, end)
    pieces that together cover [range_start, range_end), always extending as
    far as possible at each step. Returns None if the intervals can't fully
    cover the range (there's a gap nobody is free during)."""
    intervals = sorted(intervals)
    n = len(intervals)
    i = 0
    current = range_start
    periods = []
    while current < range_end:
        farthest = current
        while i < n and intervals[i][0] <= current:
            farthest = max(farthest, intervals[i][1])
            i += 1
        if farthest <= current:
            return None
        periods.append((current, min(farthest, range_end)))
        current = farthest
    return periods


def _split_into_covered_periods(absence: Absence, start_dt, end_dt) -> list[tuple[datetime, datetime]] | None:
    """Find the fewest contiguous sub-periods of [start_dt, end_dt) such that
    each one has at least one teacher genuinely free for its whole span (not
    counting their own overlapping absence or another substitution they're
    already committed to). Returns None if no combination of teachers can
    fully cover the range."""
    busy_with_own_absence = Absence.objects.filter(
        start_datetime__lt=end_dt, end_datetime__gt=start_dt
    ).exclude(pk=absence.pk).values_list("teacher_id", flat=True)
    busy_substituting = Substitution.objects.filter(
        start_datetime__lt=end_dt, end_datetime__gt=start_dt
    ).values_list("substitute_teacher_id", flat=True)

    pool = (
        Teacher.objects.filter(active=True)
        .exclude(pk=absence.teacher_id)
        .exclude(pk__in=list(busy_with_own_absence))
        .exclude(pk__in=list(busy_substituting))
        .prefetch_related("non_teaching_hours")
    )

    intervals = []
    for candidate in pool:
        intervals.extend(_merged_intervals_for_range(candidate, start_dt, end_dt))

    return _greedy_min_cover(intervals, start_dt, end_dt)


def build_coverage_plan(absence: Absence) -> list[dict]:
    """Return the coverage slots still needed for `absence`: one per gap not
    yet covered by a confirmed Substitution. Each slot is a dict with
    `start_datetime`, `end_datetime` and `candidates` (as returned by
    `find_available_substitutes`, scoped to that slot).

    When a single teacher can cover a gap by themselves, it's returned as one
    slot spanning the whole gap - same as before splitting existed. Otherwise
    the gap is split into the fewest sub-periods that each have at least one
    genuinely available teacher, so the gap can be covered by several
    teachers together. If a gap can't be covered at all, even split, it's
    still returned as a single slot (with no selectable candidates) so the
    caller can show why."""
    slots = []
    for gap_start, gap_end in uncovered_ranges(absence):
        gap_candidates = _find_available_substitutes_for_range(absence, gap_start, gap_end)
        if any(not candidate.already_substituting for candidate in gap_candidates):
            slots.append({"start_datetime": gap_start, "end_datetime": gap_end, "candidates": gap_candidates})
            continue

        periods = _split_into_covered_periods(absence, gap_start, gap_end)
        if periods is None:
            slots.append({"start_datetime": gap_start, "end_datetime": gap_end, "candidates": gap_candidates})
            continue

        for period_start, period_end in periods:
            slots.append(
                {
                    "start_datetime": period_start,
                    "end_datetime": period_end,
                    "candidates": _find_available_substitutes_for_range(absence, period_start, period_end),
                }
            )
    return slots
