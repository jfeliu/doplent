import datetime

from django.contrib.auth.models import User
from django.core import mail
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from teachers.models import NonTeachingHoursKind, NonTeachingHoursPriority, Teacher, WeeklyNonTeachingHours

from .models import Absence, Substitution, SubstitutionOffer
from .offers import decline_offer
from .services import (
    build_coverage_grid,
    can_offer,
    course_year_start,
    find_available_substitutes,
    grid_slots,
    uncovered_ranges,
)
from .stats import build_admin_stats


def make_teacher(username, grade_level=Teacher.GradeLevel.PRIMARY, active=True, email=""):
    user = User.objects.create_user(username=username, password="pw", first_name=username, email=email)
    return Teacher.objects.create(user=user, grade_level=grade_level, active=active)


def give_free_all_week(teacher, start=datetime.time(8, 0), end=datetime.time(16, 0)):
    """Give the teacher a single non-teaching block covering the whole day, every day."""
    for weekday, _label in WeeklyNonTeachingHours.Weekday.choices:
        WeeklyNonTeachingHours.objects.create(teacher=teacher, weekday=weekday, start_time=start, end_time=end)


def dt(year, month, day, hour, minute=0):
    return timezone.make_aware(datetime.datetime(year, month, day, hour, minute))


def course_dt(day_offset, hour, minute=0):
    """A datetime `day_offset` days into the current course year (1 September) -
    for substitution/absence fixtures that must count toward this year's totals
    now that fairness counters and stats reset each 1 September."""
    return (course_year_start() + datetime.timedelta(days=day_offset)).replace(hour=hour, minute=minute)


def next_monday_dt(hour, minute=0):
    """An aware datetime on the next upcoming Monday, for offer tests -
    expire_stale_offers() compares against the real wall clock, so a fixed
    past calendar date (like the 2024-01-08 used elsewhere in this file)
    would look already-expired by the time this suite runs."""
    today = timezone.localdate()
    monday = today + datetime.timedelta(days=(7 - today.weekday()) % 7 or 7)
    return timezone.make_aware(datetime.datetime.combine(monday, datetime.time(hour, minute)))


def result_for(results, teacher):
    """find_available_substitutes returns fresh instances with ranking attributes
    set on them; look up the one matching `teacher` by pk instead of relying on
    object identity."""
    return next(candidate for candidate in results if candidate.pk == teacher.pk)


def make_substitution(absence, substitute_teacher, start=None, end=None):
    """Create a Substitution covering `absence`'s full range by default."""
    return Substitution.objects.create(
        absence=absence,
        substitute_teacher=substitute_teacher,
        start_datetime=start or absence.start_datetime,
        end_datetime=end or absence.end_datetime,
    )


def make_offer(absence, substitute_teacher, start=None, end=None, status=SubstitutionOffer.Status.PENDING):
    """Create a SubstitutionOffer covering `absence`'s full range by default."""
    return SubstitutionOffer.objects.create(
        absence=absence,
        substitute_teacher=substitute_teacher,
        start_datetime=start or absence.start_datetime,
        end_datetime=end or absence.end_datetime,
        status=status,
    )


def cell_values(teacher, *slot_indexes):
    """POST payload selecting `teacher` for the given picking-grid slot indexes."""
    return {"cell": [f"{teacher.pk}:{index}" for index in slot_indexes]}


class FindAvailableSubstitutesTests(TestCase):
    def setUp(self):
        self.absent = make_teacher("absent", grade_level=Teacher.GradeLevel.PRIMARY)
        # 2024-01-08 is a Monday.
        self.absence = Absence.objects.create(
            teacher=self.absent, start_datetime=dt(2024, 1, 8, 9), end_datetime=dt(2024, 1, 8, 11)
        )

    def test_grade_mismatch_included_but_deprioritized(self):
        other_grade = make_teacher("other_grade", grade_level=Teacher.GradeLevel.PRE_PRIMARY)
        give_free_all_week(other_grade)
        same_grade = make_teacher("same_grade")
        give_free_all_week(same_grade)

        results = find_available_substitutes(self.absence)
        self.assertIn(other_grade, results)
        self.assertFalse(result_for(results, other_grade).same_grade)
        self.assertTrue(result_for(results, same_grade).same_grade)
        self.assertLess(results.index(same_grade), results.index(other_grade))

    def test_missing_weekday_non_teaching_hours_excluded(self):
        candidate = make_teacher("no_monday")
        for weekday, _label in WeeklyNonTeachingHours.Weekday.choices:
            if weekday == WeeklyNonTeachingHours.Weekday.MONDAY:
                continue
            WeeklyNonTeachingHours.objects.create(
                teacher=candidate, weekday=weekday, start_time=datetime.time(8, 0), end_time=datetime.time(16, 0)
            )

        self.assertNotIn(candidate, find_available_substitutes(self.absence))

    def test_insufficient_hours_excluded(self):
        candidate = make_teacher("short_hours")
        give_free_all_week(candidate, start=datetime.time(9, 30), end=datetime.time(16, 0))

        self.assertNotIn(candidate, find_available_substitutes(self.absence))

    def test_fully_covered_hours_included(self):
        candidate = make_teacher("available")
        give_free_all_week(candidate)

        self.assertIn(candidate, find_available_substitutes(self.absence))

    def test_gap_between_non_teaching_blocks_excludes_candidate(self):
        # Free 8-9:30 and 10:30-16 on Monday: a class from 9-11 falls in the gap.
        candidate = make_teacher("has_a_class_in_the_middle")
        WeeklyNonTeachingHours.objects.create(
            teacher=candidate,
            weekday=WeeklyNonTeachingHours.Weekday.MONDAY,
            start_time=datetime.time(8, 0),
            end_time=datetime.time(9, 30),
        )
        WeeklyNonTeachingHours.objects.create(
            teacher=candidate,
            weekday=WeeklyNonTeachingHours.Weekday.MONDAY,
            start_time=datetime.time(10, 30),
            end_time=datetime.time(16, 0),
        )

        self.assertNotIn(candidate, find_available_substitutes(self.absence))

    def test_touching_non_teaching_blocks_merge_to_cover_request(self):
        # Free 8-9:30 and 9:30-16 (back-to-back) on Monday should merge into one
        # continuous free run that covers the 9-11 request.
        candidate = make_teacher("touching_blocks")
        WeeklyNonTeachingHours.objects.create(
            teacher=candidate,
            weekday=WeeklyNonTeachingHours.Weekday.MONDAY,
            start_time=datetime.time(8, 0),
            end_time=datetime.time(9, 30),
        )
        WeeklyNonTeachingHours.objects.create(
            teacher=candidate,
            weekday=WeeklyNonTeachingHours.Weekday.MONDAY,
            start_time=datetime.time(9, 30),
            end_time=datetime.time(16, 0),
        )

        self.assertIn(candidate, find_available_substitutes(self.absence))

    def test_overlapping_own_absence_excluded(self):
        candidate = make_teacher("also_out")
        give_free_all_week(candidate)
        Absence.objects.create(
            teacher=candidate, start_datetime=dt(2024, 1, 8, 10), end_datetime=dt(2024, 1, 8, 12)
        )

        self.assertNotIn(candidate, find_available_substitutes(self.absence))

    def test_already_substituting_elsewhere_shown_but_flagged(self):
        candidate = make_teacher("busy_substitute")
        give_free_all_week(candidate)
        other_absent = make_teacher("other_absent")
        give_free_all_week(other_absent)
        other_absence = Absence.objects.create(
            teacher=other_absent, start_datetime=dt(2024, 1, 8, 10), end_datetime=dt(2024, 1, 8, 12)
        )
        make_substitution(other_absence, candidate)

        results = find_available_substitutes(self.absence)
        self.assertIn(candidate, results)
        self.assertTrue(result_for(results, candidate).already_substituting)

    def test_already_substituting_ranked_below_available_candidates(self):
        busy_substitute = make_teacher("busy_substitute_2")
        give_free_all_week(busy_substitute)
        other_absent = make_teacher("other_absent_2")
        give_free_all_week(other_absent)
        other_absence = Absence.objects.create(
            teacher=other_absent, start_datetime=dt(2024, 1, 8, 10), end_datetime=dt(2024, 1, 8, 12)
        )
        make_substitution(other_absence, busy_substitute)

        available = make_teacher("truly_available")
        give_free_all_week(available)

        results = find_available_substitutes(self.absence)
        self.assertLess(results.index(available), results.index(busy_substitute))
        self.assertFalse(result_for(results, available).already_substituting)

    def test_ranking_by_fewest_substitutions(self):
        busy = make_teacher("busy")
        give_free_all_week(busy)
        idle = make_teacher("idle")
        give_free_all_week(idle)

        past_absent = make_teacher("past_absent")
        give_free_all_week(past_absent)
        for i in range(3):
            past_absence = Absence.objects.create(
                teacher=past_absent,
                start_datetime=course_dt(i, 9),
                end_datetime=course_dt(i, 10),
            )
            make_substitution(past_absence, busy)

        results = find_available_substitutes(self.absence)
        self.assertEqual(results[0], idle)
        self.assertIn(busy, results)
        self.assertLess(results.index(idle), results.index(busy))

    def test_ranking_by_time_covered_not_substitution_count(self):
        # many_short has done three 30-minute substitutions (1h30m total);
        # few_long has done one 3-hour substitution. Ranking is by time
        # covered, so many_short - with fewer total hours - comes first even
        # though they have more substitutions to their name.
        many_short = make_teacher("many_short")
        give_free_all_week(many_short)
        few_long = make_teacher("few_long")
        give_free_all_week(few_long)

        past_absent = make_teacher("past_absent_time")
        give_free_all_week(past_absent)
        for i in range(3):
            short_absence = Absence.objects.create(
                teacher=past_absent,
                start_datetime=course_dt(i, 9),
                end_datetime=course_dt(i, 9, 30),
            )
            make_substitution(short_absence, many_short)
        long_absence = Absence.objects.create(
            teacher=past_absent,
            start_datetime=course_dt(5, 9),
            end_datetime=course_dt(5, 12),
        )
        make_substitution(long_absence, few_long)

        results = find_available_substitutes(self.absence)
        self.assertLess(results.index(many_short), results.index(few_long))
        self.assertEqual(result_for(results, many_short).coverage_done, datetime.timedelta(hours=1, minutes=30))
        self.assertEqual(result_for(results, many_short).coverage_done_label, "1h 30m")

    def test_absent_teacher_and_inactive_excluded(self):
        inactive = make_teacher("inactive", active=False)
        give_free_all_week(inactive)

        results = find_available_substitutes(self.absence)
        self.assertNotIn(self.absent, results)
        self.assertNotIn(inactive, results)

    def test_paperwork_only_coverage_is_still_eligible_but_flagged(self):
        candidate = make_teacher("only_paperwork")
        WeeklyNonTeachingHours.objects.create(
            teacher=candidate,
            weekday=WeeklyNonTeachingHours.Weekday.MONDAY,
            start_time=datetime.time(8, 0),
            end_time=datetime.time(16, 0),
            kind=NonTeachingHoursKind.PAPERWORK,
        )

        results = find_available_substitutes(self.absence)
        self.assertIn(candidate, results)
        self.assertFalse(result_for(results, candidate).is_fully_free)

    def test_free_candidate_ranked_above_paperwork_candidate_despite_more_substitutions(self):
        # busy_but_free has done more substitutions but is truly idle for the
        # window; paperwork_light has done fewer but would be pulled off
        # paperwork. Being free should win regardless of substitution count.
        busy_but_free = make_teacher("busy_but_free")
        give_free_all_week(busy_but_free)

        paperwork_light = make_teacher("paperwork_light")
        WeeklyNonTeachingHours.objects.create(
            teacher=paperwork_light,
            weekday=WeeklyNonTeachingHours.Weekday.MONDAY,
            start_time=datetime.time(8, 0),
            end_time=datetime.time(16, 0),
            kind=NonTeachingHoursKind.PAPERWORK,
        )

        past_absent = make_teacher("past_absent_2")
        give_free_all_week(past_absent)
        for i in range(2):
            past_absence = Absence.objects.create(
                teacher=past_absent,
                start_datetime=dt(2024, 1, 1 + i, 9),
                end_datetime=dt(2024, 1, 1 + i, 10),
            )
            make_substitution(past_absence, busy_but_free)

        results = find_available_substitutes(self.absence)
        self.assertLess(results.index(busy_but_free), results.index(paperwork_light))
        self.assertTrue(result_for(results, busy_but_free).is_fully_free)
        self.assertFalse(result_for(results, paperwork_light).is_fully_free)

    def test_kind_priority_orders_free_then_co_teaching_then_escoltam(self):
        # Three otherwise-identical candidates whose only block over the window
        # differs by kind - they should come back free, then co-teaching, then
        # escolta'm.
        by_kind = {}
        for name, kind in [
            ("free_cand", NonTeachingHoursKind.FREE),
            ("coteach_cand", NonTeachingHoursKind.CO_TEACHING),
            ("escoltam_cand", NonTeachingHoursKind.ESCOLTAM),
        ]:
            teacher = make_teacher(name)
            WeeklyNonTeachingHours.objects.create(
                teacher=teacher,
                weekday=WeeklyNonTeachingHours.Weekday.MONDAY,
                start_time=datetime.time(8, 0),
                end_time=datetime.time(16, 0),
                kind=kind,
            )
            by_kind[name] = teacher

        results = find_available_substitutes(self.absence)
        self.assertLess(results.index(by_kind["free_cand"]), results.index(by_kind["coteach_cand"]))
        self.assertLess(results.index(by_kind["coteach_cand"]), results.index(by_kind["escoltam_cand"]))

    def test_priorities_are_seeded_by_migration(self):
        self.assertEqual(
            NonTeachingHoursPriority.ordering_map(),
            {"free": 0, "paperwork": 10, "co_teaching": 20, "escoltam": 30},
        )

    def test_reordering_priorities_changes_the_ranking(self):
        first = make_teacher("was_free")
        WeeklyNonTeachingHours.objects.create(
            teacher=first,
            weekday=WeeklyNonTeachingHours.Weekday.MONDAY,
            start_time=datetime.time(8, 0),
            end_time=datetime.time(16, 0),
            kind=NonTeachingHoursKind.FREE,
        )
        second = make_teacher("was_paperwork")
        WeeklyNonTeachingHours.objects.create(
            teacher=second,
            weekday=WeeklyNonTeachingHours.Weekday.MONDAY,
            start_time=datetime.time(8, 0),
            end_time=datetime.time(16, 0),
            kind=NonTeachingHoursKind.PAPERWORK,
        )
        NonTeachingHoursPriority.objects.filter(kind=NonTeachingHoursKind.FREE).update(priority=99)

        results = find_available_substitutes(self.absence)
        self.assertLess(results.index(second), results.index(first))

    def test_mixed_kind_blocks_rank_by_the_worst_kind_needed(self):
        # Free 8-9:30, paperwork 9:30-16: the 9-11 request needs both blocks to
        # be covered at all, so paperwork time was involved.
        candidate = make_teacher("mixed_blocks")
        WeeklyNonTeachingHours.objects.create(
            teacher=candidate,
            weekday=WeeklyNonTeachingHours.Weekday.MONDAY,
            start_time=datetime.time(8, 0),
            end_time=datetime.time(9, 30),
            kind=NonTeachingHoursKind.FREE,
        )
        WeeklyNonTeachingHours.objects.create(
            teacher=candidate,
            weekday=WeeklyNonTeachingHours.Weekday.MONDAY,
            start_time=datetime.time(9, 30),
            end_time=datetime.time(16, 0),
            kind=NonTeachingHoursKind.PAPERWORK,
        )

        results = find_available_substitutes(self.absence)
        self.assertIn(candidate, results)
        self.assertFalse(result_for(results, candidate).is_fully_free)

    def test_requester_own_non_teaching_block_excluded_from_coverage_need(self):
        # The absent teacher has a personal free block 9:30-10 within the
        # 9-11 absence; a substitute is only needed for 9-9:30 and 10-11, not
        # for time the requester wasn't teaching anyway.
        WeeklyNonTeachingHours.objects.create(
            teacher=self.absent,
            weekday=WeeklyNonTeachingHours.Weekday.MONDAY,
            start_time=datetime.time(9, 30),
            end_time=datetime.time(10, 0),
        )
        candidate = make_teacher("covers_around_the_gap")
        WeeklyNonTeachingHours.objects.create(
            teacher=candidate,
            weekday=WeeklyNonTeachingHours.Weekday.MONDAY,
            start_time=datetime.time(9, 0),
            end_time=datetime.time(9, 30),
        )
        WeeklyNonTeachingHours.objects.create(
            teacher=candidate,
            weekday=WeeklyNonTeachingHours.Weekday.MONDAY,
            start_time=datetime.time(10, 0),
            end_time=datetime.time(11, 0),
        )

        self.assertIn(candidate, find_available_substitutes(self.absence))

    def test_requester_free_for_whole_absence_needs_no_substitute(self):
        give_free_all_week(self.absent)

        self.assertEqual(uncovered_ranges(self.absence), [])
        self.assertEqual(find_available_substitutes(self.absence), [])


class PickSubstituteViewTests(TestCase):
    def setUp(self):
        self.absent = make_teacher("absent_view", grade_level=Teacher.GradeLevel.PRIMARY)
        # 2024-01-08 is a Monday.
        self.absence = Absence.objects.create(
            teacher=self.absent, start_datetime=dt(2024, 1, 8, 9), end_datetime=dt(2024, 1, 8, 11)
        )
        self.client.force_login(self.absent.user)

    def test_cannot_select_a_candidate_already_substituting_elsewhere(self):
        busy_substitute = make_teacher("busy_substitute_view")
        give_free_all_week(busy_substitute)
        other_absent = make_teacher("other_absent_view")
        give_free_all_week(other_absent)
        other_absence = Absence.objects.create(
            teacher=other_absent, start_datetime=dt(2024, 1, 8, 10), end_datetime=dt(2024, 1, 8, 12)
        )
        make_substitution(other_absence, busy_substitute)

        # busy 10:00-12:00 elsewhere; the absence's slots 2-3 overlap that.
        response = self.client.post(
            reverse("pick_substitute", args=[self.absence.pk]),
            cell_values(busy_substitute, 0, 1, 2, 3),
        )

        self.assertEqual(response.status_code, 302)
        self.assertFalse(Substitution.objects.filter(absence=self.absence).exists())
        self.assertFalse(SubstitutionOffer.objects.filter(absence=self.absence).exists())


def _weekday_block(teacher, start, end, kind=NonTeachingHoursKind.FREE):
    return WeeklyNonTeachingHours.objects.create(
        teacher=teacher,
        weekday=WeeklyNonTeachingHours.Weekday.MONDAY,
        start_time=start,
        end_time=end,
        kind=kind,
    )


class BuildCoverageGridTests(TestCase):
    def setUp(self):
        self.absent = make_teacher("grid_absent", grade_level=Teacher.GradeLevel.PRIMARY)
        # 2024-01-08 is a Monday, 9am-noon.
        self.absence = Absence.objects.create(
            teacher=self.absent, start_datetime=dt(2024, 1, 8, 9), end_datetime=dt(2024, 1, 8, 12)
        )

    def _col(self, grid, teacher):
        return next((c for c in grid["columns"] if c["teacher"].pk == teacher.pk), None)

    def _states(self, grid, teacher):
        return [cell["state"] for cell in self._col(grid, teacher)["cells"]]

    def test_slots_span_the_absence_in_30_minute_steps_all_needing_cover(self):
        grid = build_coverage_grid(self.absence)

        self.assertEqual(
            [(s["start_datetime"], s["end_datetime"]) for s in grid["slots"]],
            [
                (dt(2024, 1, 8, 9), dt(2024, 1, 8, 9, 30)),
                (dt(2024, 1, 8, 9, 30), dt(2024, 1, 8, 10)),
                (dt(2024, 1, 8, 10), dt(2024, 1, 8, 10, 30)),
                (dt(2024, 1, 8, 10, 30), dt(2024, 1, 8, 11)),
                (dt(2024, 1, 8, 11), dt(2024, 1, 8, 11, 30)),
                (dt(2024, 1, 8, 11, 30), dt(2024, 1, 8, 12)),
            ],
        )
        self.assertTrue(all(s["reason"] is None for s in grid["slots"]))
        self.assertTrue(grid["needs_cover"])

    def test_lunch_slots_flagged_as_outside_working_hours(self):
        absence = Absence.objects.create(
            teacher=make_teacher("grid_lunch"),
            start_datetime=dt(2024, 1, 8, 12),
            end_datetime=dt(2024, 1, 8, 16),
        )
        reasons = {s["start_datetime"].strftime("%H:%M"): s["reason"] for s in build_coverage_grid(absence)["slots"]}

        self.assertIsNone(reasons["12:00"])
        self.assertEqual(reasons["13:00"], "outside_working_hours")
        self.assertEqual(reasons["14:30"], "outside_working_hours")
        self.assertIsNone(reasons["15:00"])

    def test_requester_own_free_block_flags_those_slots(self):
        _weekday_block(self.absent, datetime.time(10, 0), datetime.time(10, 30))

        grid = build_coverage_grid(self.absence)

        self.assertEqual(
            [s["reason"] for s in grid["slots"]],
            [None, None, "requester_free", None, None, None],
        )

    def test_covered_slots_flagged(self):
        _weekday_block(make_teacher("grid_cover_done"), datetime.time(9, 0), datetime.time(10, 0))
        make_substitution(
            self.absence, make_teacher("grid_covered_by"), start=dt(2024, 1, 8, 9), end=dt(2024, 1, 8, 10)
        )

        self.assertEqual(
            [s["reason"] for s in build_coverage_grid(self.absence)["slots"]],
            ["covered", "covered", None, None, None, None],
        )

    def test_column_included_only_when_free_for_a_needed_slot(self):
        helper = make_teacher("grid_helper")
        _weekday_block(helper, datetime.time(9, 0), datetime.time(9, 30))
        elsewhere = make_teacher("grid_elsewhere")
        _weekday_block(elsewhere, datetime.time(15, 0), datetime.time(16, 0))

        grid = build_coverage_grid(self.absence)

        self.assertIsNotNone(self._col(grid, helper))
        self.assertIsNone(self._col(grid, elsewhere))

    def test_cell_states_free_and_busy_and_unavailable(self):
        cand = make_teacher("grid_states")
        _weekday_block(cand, datetime.time(9, 0), datetime.time(11, 0))
        other = make_teacher("grid_states_other")
        other_absence = Absence.objects.create(
            teacher=other, start_datetime=dt(2024, 1, 8, 10), end_datetime=dt(2024, 1, 8, 11)
        )
        make_substitution(other_absence, cand)

        self.assertEqual(
            self._states(build_coverage_grid(self.absence), cand),
            ["free", "free", "busy", "busy", "unavailable", "unavailable"],
        )

    def test_pending_offer_marks_the_cells(self):
        cand = make_teacher("grid_pending")
        _weekday_block(cand, datetime.time(9, 0), datetime.time(12, 0))
        make_offer(self.absence, cand, start=dt(2024, 1, 8, 9), end=dt(2024, 1, 8, 10))

        self.assertEqual(
            self._states(build_coverage_grid(self.absence), cand),
            ["pending", "pending", "free", "free", "free", "free"],
        )

    def test_cell_kind_comes_from_the_covering_block(self):
        cand = make_teacher("grid_kind")
        _weekday_block(cand, datetime.time(9, 0), datetime.time(10, 0), kind=NonTeachingHoursKind.CO_TEACHING)

        cells = build_coverage_grid(self.absence)["columns"]
        col = next(c for c in cells if c["teacher"].pk == cand.pk)
        self.assertEqual(col["cells"][0]["kind"], NonTeachingHoursKind.CO_TEACHING)

    def test_columns_lead_with_the_narrowest_availability(self):
        narrow = make_teacher("grid_narrow")
        _weekday_block(narrow, datetime.time(9, 0), datetime.time(9, 30))
        wide = make_teacher("grid_wide")
        give_free_all_week(wide)

        order = [c["teacher"].pk for c in build_coverage_grid(self.absence)["columns"]]
        self.assertLess(order.index(narrow.pk), order.index(wide.pk))

    def test_rows_align_cells_with_the_column_order(self):
        a = make_teacher("grid_row_a")
        _weekday_block(a, datetime.time(9, 0), datetime.time(9, 30))
        b = make_teacher("grid_row_b")
        give_free_all_week(b)

        grid = build_coverage_grid(self.absence)
        self.assertEqual(
            [cell["teacher_id"] for cell in grid["rows"][0]["cells"]],
            [col["teacher"].pk for col in grid["columns"]],
        )

    def test_non_cover_rows_carry_a_reason_label_on_the_first_of_a_run(self):
        _weekday_block(self.absent, datetime.time(10, 0), datetime.time(11, 0))
        _weekday_block(make_teacher("grid_reason_helper"), datetime.time(9, 0), datetime.time(9, 30))

        rows = build_coverage_grid(self.absence)["rows"]
        reasons = [(r["reason"], str(r["reason_label"])) for r in rows]
        # slots: 9:00, 9:30 need cover; 10:00, 10:30 are the requester's own time
        self.assertEqual(reasons[0], (None, ""))
        self.assertEqual(reasons[2][0], "requester_free")
        self.assertNotEqual(reasons[2][1], "")   # labelled - first of the run
        self.assertEqual(reasons[3], ("requester_free", ""))  # continuation - no label


class CanOfferTests(TestCase):
    def setUp(self):
        self.absent = make_teacher("co_absent", grade_level=Teacher.GradeLevel.PRIMARY)
        self.absence = Absence.objects.create(
            teacher=self.absent, start_datetime=dt(2024, 1, 8, 9), end_datetime=dt(2024, 1, 8, 12)
        )
        self.cand = make_teacher("co_cand")
        _weekday_block(self.cand, datetime.time(9, 0), datetime.time(10, 0))

    def test_accepts_an_aligned_range_the_teacher_is_free_for(self):
        self.assertTrue(can_offer(self.absence, self.cand, dt(2024, 1, 8, 9, 30), dt(2024, 1, 8, 10)))

    def test_rejects_a_misaligned_range(self):
        self.assertFalse(can_offer(self.absence, self.cand, dt(2024, 1, 8, 9, 15), dt(2024, 1, 8, 10)))

    def test_rejects_a_range_the_teacher_is_not_free_for(self):
        self.assertFalse(can_offer(self.absence, self.cand, dt(2024, 1, 8, 9, 30), dt(2024, 1, 8, 10, 30)))

    def test_rejects_a_range_outside_working_hours(self):
        _weekday_block(self.cand, datetime.time(13, 0), datetime.time(14, 0))
        self.assertFalse(can_offer(self.absence, self.cand, dt(2024, 1, 8, 13), dt(2024, 1, 8, 13, 30)))

    def test_rejects_an_already_covered_range(self):
        make_substitution(
            self.absence, make_teacher("co_covered_by"), start=dt(2024, 1, 8, 9), end=dt(2024, 1, 8, 10)
        )
        self.assertFalse(can_offer(self.absence, self.cand, dt(2024, 1, 8, 9, 30), dt(2024, 1, 8, 10)))

    def test_rejects_when_the_teacher_is_committed_elsewhere(self):
        other = make_teacher("co_other")
        other_absence = Absence.objects.create(
            teacher=other, start_datetime=dt(2024, 1, 8, 9), end_datetime=dt(2024, 1, 8, 10)
        )
        make_substitution(other_absence, self.cand)
        self.assertFalse(can_offer(self.absence, self.cand, dt(2024, 1, 8, 9), dt(2024, 1, 8, 10)))

    def test_rejects_the_absent_teacher_themselves(self):
        _weekday_block(self.absent, datetime.time(9, 0), datetime.time(10, 0))
        self.assertFalse(can_offer(self.absence, self.absent, dt(2024, 1, 8, 9), dt(2024, 1, 8, 10)))

    def test_rejects_a_range_overlapping_a_pending_offer_to_anyone(self):
        someone_else = make_teacher("co_pending_holder")
        make_offer(self.absence, someone_else, start=dt(2024, 1, 8, 9), end=dt(2024, 1, 8, 9, 30))
        # self.cand is free 9-10 and has no offer of their own, but the 9:00-9:30
        # slice is out for a decision, so any range touching it is locked.
        self.assertFalse(can_offer(self.absence, self.cand, dt(2024, 1, 8, 9), dt(2024, 1, 8, 10)))
        self.assertTrue(can_offer(self.absence, self.cand, dt(2024, 1, 8, 9, 30), dt(2024, 1, 8, 10)))


class WorkingHoursTests(TestCase):
    """Substitutes are never searched for outside the school's working hours
    (9-13, 15-17), regardless of any teacher's individual schedule."""

    def setUp(self):
        self.absent = make_teacher("absent_hours", grade_level=Teacher.GradeLevel.PRIMARY)

    def test_absence_entirely_outside_working_hours_needs_no_substitute(self):
        # 2024-01-08 is a Monday; 7-8am is before the 9am opening.
        absence = Absence.objects.create(
            teacher=self.absent, start_datetime=dt(2024, 1, 8, 7), end_datetime=dt(2024, 1, 8, 8)
        )

        self.assertEqual(uncovered_ranges(absence), [])
        self.assertEqual(find_available_substitutes(absence), [])

    def test_weekend_absence_needs_no_substitute(self):
        # 2024-01-13 is a Saturday - the school doesn't run.
        absence = Absence.objects.create(
            teacher=self.absent, start_datetime=dt(2024, 1, 13, 9), end_datetime=dt(2024, 1, 13, 11)
        )

        self.assertEqual(uncovered_ranges(absence), [])
        grid = build_coverage_grid(absence)
        self.assertFalse(grid["needs_cover"])
        self.assertEqual(grid["columns"], [])

    def test_absence_spanning_lunch_break_splits_around_it(self):
        # Noon to 3:30pm spans the 1-3pm non-working break.
        absence = Absence.objects.create(
            teacher=self.absent, start_datetime=dt(2024, 1, 8, 12), end_datetime=dt(2024, 1, 8, 15, 30)
        )
        candidate = make_teacher("covers_around_lunch")
        WeeklyNonTeachingHours.objects.create(
            teacher=candidate,
            weekday=WeeklyNonTeachingHours.Weekday.MONDAY,
            start_time=datetime.time(12, 0),
            end_time=datetime.time(13, 0),
        )
        WeeklyNonTeachingHours.objects.create(
            teacher=candidate,
            weekday=WeeklyNonTeachingHours.Weekday.MONDAY,
            start_time=datetime.time(15, 0),
            end_time=datetime.time(15, 30),
        )

        self.assertEqual(
            uncovered_ranges(absence),
            [(dt(2024, 1, 8, 12), dt(2024, 1, 8, 13)), (dt(2024, 1, 8, 15), dt(2024, 1, 8, 15, 30))],
        )
        self.assertIn(candidate, find_available_substitutes(absence))

    def test_absence_trimmed_to_closing_time(self):
        # 4:30pm to 6pm - only 4:30-5pm falls within working hours.
        absence = Absence.objects.create(
            teacher=self.absent, start_datetime=dt(2024, 1, 8, 16, 30), end_datetime=dt(2024, 1, 8, 18)
        )

        self.assertEqual(uncovered_ranges(absence), [(dt(2024, 1, 8, 16, 30), dt(2024, 1, 8, 17))])


class GridSlotReasonTests(TestCase):
    def setUp(self):
        self.absent = make_teacher("absent_reasons", grade_level=Teacher.GradeLevel.PRIMARY)

    def _reasons(self, start, end):
        absence = Absence.objects.create(teacher=self.absent, start_datetime=start, end_datetime=end)
        return [(s["start_datetime"].strftime("%H:%M"), s["reason"]) for s in grid_slots(absence)]

    def test_outside_working_hours_flagged(self):
        # 2024-01-08 is a Monday; 7-8am is before the 9am opening.
        self.assertEqual(
            self._reasons(dt(2024, 1, 8, 7), dt(2024, 1, 8, 8)),
            [("07:00", "outside_working_hours"), ("07:30", "outside_working_hours")],
        )

    def test_requester_free_time_flagged(self):
        _weekday_block(self.absent, datetime.time(9, 30), datetime.time(10, 0))
        self.assertEqual(
            self._reasons(dt(2024, 1, 8, 9), dt(2024, 1, 8, 11)),
            [("09:00", None), ("09:30", "requester_free"), ("10:00", None), ("10:30", None)],
        )

    def test_outside_hours_wins_over_requester_free_when_both_apply(self):
        _weekday_block(self.absent, datetime.time(13, 0), datetime.time(14, 0))  # within the lunch break
        reasons = dict(self._reasons(dt(2024, 1, 8, 12), dt(2024, 1, 8, 15)))
        self.assertEqual(reasons["13:00"], "outside_working_hours")

    def test_every_slot_needs_cover_when_fully_within_working_hours(self):
        self.assertTrue(all(r is None for _, r in self._reasons(dt(2024, 1, 8, 9), dt(2024, 1, 8, 11))))


@override_settings(LANGUAGE_CODE="en")
class PickSubstituteGridBandsTests(TestCase):
    def test_non_cover_periods_are_labelled_bands_in_the_grid(self):
        absent = make_teacher("absent_page_discard", grade_level=Teacher.GradeLevel.PRIMARY)
        _weekday_block(absent, datetime.time(9, 30), datetime.time(10, 0))  # requester's own time
        candidate = make_teacher("covers_page_discard")
        _weekday_block(candidate, datetime.time(9, 0), datetime.time(9, 30))
        _weekday_block(candidate, datetime.time(10, 0), datetime.time(18, 0))
        # 2024-01-08 is a Monday; the absence spans the 13:00-15:00 lunch break.
        absence = Absence.objects.create(
            teacher=absent, start_datetime=dt(2024, 1, 8, 9), end_datetime=dt(2024, 1, 8, 18)
        )
        self.client.force_login(absent.user)

        response = self.client.get(reverse("pick_substitute", args=[absence.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "cov-band")
        self.assertContains(response, "Outside school hours")
        self.assertContains(response, "Your own non-teaching time")
        self.assertNotContains(response, "Periods that don't need a substitute")  # no separate card
        self.assertContains(response, str(candidate))  # the grid lists the available teacher


class SplitPickSubstituteViewTests(TestCase):
    def setUp(self):
        self.absent = make_teacher("absent_split_view", grade_level=Teacher.GradeLevel.PRIMARY)
        # Next Monday, 9am-noon - a future date, since this test's offers must
        # still be PENDING (not expired) when accepted.
        self.absence = Absence.objects.create(
            teacher=self.absent, start_datetime=next_monday_dt(9), end_datetime=next_monday_dt(12)
        )
        self.client.force_login(self.absent.user)

        self.first_half = make_teacher("view_first_half")
        WeeklyNonTeachingHours.objects.create(
            teacher=self.first_half,
            weekday=WeeklyNonTeachingHours.Weekday.MONDAY,
            start_time=datetime.time(9, 0),
            end_time=datetime.time(10, 30),
        )
        self.second_half = make_teacher("view_second_half")
        WeeklyNonTeachingHours.objects.create(
            teacher=self.second_half,
            weekday=WeeklyNonTeachingHours.Weekday.MONDAY,
            start_time=datetime.time(10, 30),
            end_time=datetime.time(12, 0),
        )

    def test_one_bulk_submit_offers_each_teacher_their_stretch_and_they_tile_the_absence(self):
        url = reverse("pick_substitute", args=[self.absence.pk])
        # first_half -> 9:00-10:30 (slots 0-2), second_half -> 10:30-12:00 (slots 3-5).
        response = self.client.post(
            url,
            {"cell": [f"{self.first_half.pk}:{i}" for i in (0, 1, 2)]
                     + [f"{self.second_half.pk}:{i}" for i in (3, 4, 5)]},
        )
        self.assertRedirects(response, url)

        first_offer = SubstitutionOffer.objects.get(absence=self.absence, substitute_teacher=self.first_half)
        second_offer = SubstitutionOffer.objects.get(absence=self.absence, substitute_teacher=self.second_half)
        self.assertEqual((first_offer.start_datetime, first_offer.end_datetime),
                          (next_monday_dt(9), next_monday_dt(10, 30)))
        self.assertEqual((second_offer.start_datetime, second_offer.end_datetime),
                          (next_monday_dt(10, 30), next_monday_dt(12)))
        self.assertFalse(Substitution.objects.filter(absence=self.absence).exists())

        self.client.force_login(self.first_half.user)
        self.client.post(reverse("respond_to_offer", args=[first_offer.pk]), {"action": "accept"})
        self.client.force_login(self.second_half.user)
        self.client.post(reverse("respond_to_offer", args=[second_offer.pk]), {"action": "accept"})

        substitutions = Substitution.objects.filter(absence=self.absence).order_by("start_datetime")
        self.assertEqual(list(substitutions.values_list("substitute_teacher", flat=True)),
                          [self.first_half.pk, self.second_half.pk])

    def test_two_non_adjacent_selections_for_one_teacher_become_two_offers(self):
        covers_all = make_teacher("view_covers_all")
        give_free_all_week(covers_all)
        # slots 0-1 (9:00-10:00) and 4-5 (11:00-12:00), leaving 10:00-11:00 for someone else.
        self.client.post(
            reverse("pick_substitute", args=[self.absence.pk]),
            cell_values(covers_all, 0, 1, 4, 5),
        )
        offers = SubstitutionOffer.objects.filter(
            absence=self.absence, substitute_teacher=covers_all
        ).order_by("start_datetime")
        self.assertEqual(
            [(o.start_datetime, o.end_datetime) for o in offers],
            [(next_monday_dt(9), next_monday_dt(10)), (next_monday_dt(11), next_monday_dt(12))],
        )

    def test_grid_lists_every_teacher_free_for_any_part_of_the_absence(self):
        covers_all = make_teacher("view_covers_all_2")
        give_free_all_week(covers_all)

        response = self.client.get(reverse("pick_substitute", args=[self.absence.pk]))

        self.assertContains(response, str(covers_all))
        self.assertContains(response, str(self.first_half))
        self.assertContains(response, str(self.second_half))

    def test_teacher_can_be_offered_only_part_of_their_free_window(self):
        # first_half is free 9:00-10:30; hand them just slot 2 (10:00-10:30).
        self.client.post(reverse("pick_substitute", args=[self.absence.pk]), cell_values(self.first_half, 2))

        offer = SubstitutionOffer.objects.get(absence=self.absence, substitute_teacher=self.first_half)
        self.assertEqual((offer.start_datetime, offer.end_datetime), (next_monday_dt(10), next_monday_dt(10, 30)))

    def test_an_offer_the_teacher_is_not_free_for_is_rejected(self):
        # second_half isn't free before 10:30; slot 0 is 9:00-9:30.
        self.client.post(reverse("pick_substitute", args=[self.absence.pk]), cell_values(self.second_half, 0))
        self.assertFalse(SubstitutionOffer.objects.filter(substitute_teacher=self.second_half).exists())


@override_settings(LANGUAGE_CODE="en")
class PickSubstituteOfferFlowTests(TestCase):
    def setUp(self):
        self.absent = make_teacher("absent_offer_view", grade_level=Teacher.GradeLevel.PRIMARY)
        self.absence = Absence.objects.create(
            teacher=self.absent, start_datetime=next_monday_dt(9), end_datetime=next_monday_dt(11)
        )
        self.client.force_login(self.absent.user)
        self.url = reverse("pick_substitute", args=[self.absence.pk])

    def _post(self, candidate, *slots):
        # default: the whole 9:00-11:00 absence (slots 0-3)
        return self.client.post(self.url, cell_values(candidate, *(slots or (0, 1, 2, 3))))

    def test_choosing_a_candidate_creates_a_pending_offer_not_a_substitution(self):
        candidate = make_teacher("offer_candidate", email="candidate@example.edu")
        give_free_all_week(candidate)

        self._post(candidate)

        offer = SubstitutionOffer.objects.get(absence=self.absence, substitute_teacher=candidate)
        self.assertEqual(offer.status, SubstitutionOffer.Status.PENDING)
        self.assertFalse(Substitution.objects.filter(absence=self.absence).exists())

    def test_offer_notification_email_sent_to_the_candidate(self):
        candidate = make_teacher("offer_emailed", email="candidate@example.edu")
        give_free_all_week(candidate)

        self._post(candidate)

        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["candidate@example.edu"])

    def test_blank_candidate_email_skips_sending_without_crashing(self):
        candidate = make_teacher("offer_no_email")  # blank email by default
        give_free_all_week(candidate)

        response = self._post(candidate)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(len(mail.outbox), 0)
        self.assertTrue(SubstitutionOffer.objects.filter(absence=self.absence, substitute_teacher=candidate).exists())

    def test_resubmitting_the_same_candidate_and_slot_does_not_duplicate_the_offer(self):
        candidate = make_teacher("offer_resubmit", email="candidate@example.edu")
        give_free_all_week(candidate)

        self._post(candidate)
        self._post(candidate)

        self.assertEqual(
            SubstitutionOffer.objects.filter(absence=self.absence, substitute_teacher=candidate).count(), 1
        )
        self.assertEqual(len(mail.outbox), 1)

    def test_a_pending_offer_locks_the_period_against_a_second_teacher(self):
        first = make_teacher("offer_lock_1", email="first@example.edu")
        give_free_all_week(first)
        second = make_teacher("offer_lock_2", email="second@example.edu")
        give_free_all_week(second)

        self._post(first)   # first gets 9:00-11:00
        self._post(second)  # blocked - a decision is pending for that period

        self.assertEqual(SubstitutionOffer.objects.filter(absence=self.absence).count(), 1)
        self.assertFalse(SubstitutionOffer.objects.filter(substitute_teacher=second).exists())

    def test_grid_locks_the_slots_of_a_pending_offer_for_everyone(self):
        offered = make_teacher("offer_pending_display", email="candidate@example.edu")
        give_free_all_week(offered)
        other = make_teacher("offer_pending_other")
        give_free_all_week(other)
        self._post(offered, 0, 1)  # 9:00-10:00

        grid = build_coverage_grid(self.absence)
        offered_col = next(c for c in grid["columns"] if c["teacher"].pk == offered.pk)
        other_col = next(c for c in grid["columns"] if c["teacher"].pk == other.pk)
        # the teacher it went to sees "pending"; everyone else just "locked"
        self.assertEqual([c["state"] for c in offered_col["cells"]], ["pending", "pending", "free", "free"])
        self.assertEqual([c["state"] for c in other_col["cells"]], ["locked", "locked", "free", "free"])

    def test_one_bulk_submit_will_not_offer_the_same_period_to_two_teachers(self):
        a = make_teacher("dup_a", email="a@example.edu")
        give_free_all_week(a)
        b = make_teacher("dup_b", email="b@example.edu")
        give_free_all_week(b)

        self.client.post(self.url, {"cell": [f"{a.pk}:0", f"{b.pk}:0"]})

        self.assertEqual(SubstitutionOffer.objects.filter(absence=self.absence).count(), 1)

    def test_a_declined_offer_is_marked_but_still_selectable(self):
        candidate = make_teacher("offer_declined_display", email="candidate@example.edu")
        give_free_all_week(candidate)
        make_offer(self.absence, candidate, status=SubstitutionOffer.Status.DECLINED)

        grid = build_coverage_grid(self.absence)
        column = next(c for c in grid["columns"] if c["teacher"].pk == candidate.pk)
        self.assertTrue(all(c["state"] == "free" and c["declined"] for c in column["cells"]))

        response = self.client.get(self.url)
        self.assertContains(response, "Previously declined")

        self._post(candidate)  # re-offering still works
        self.assertTrue(
            SubstitutionOffer.objects.filter(
                absence=self.absence, substitute_teacher=candidate, status=SubstitutionOffer.Status.PENDING
            ).exists()
        )


@override_settings(LANGUAGE_CODE="en")
class RespondToOfferViewTests(TestCase):
    def setUp(self):
        self.absent = make_teacher("absent_respond", grade_level=Teacher.GradeLevel.PRIMARY, email="absent@example.edu")
        self.candidate = make_teacher("respond_candidate", email="candidate@example.edu")
        give_free_all_week(self.candidate)
        # 2024-01-08 is a Monday.
        self.absence = Absence.objects.create(
            teacher=self.absent, start_datetime=dt(2024, 1, 8, 9), end_datetime=dt(2024, 1, 8, 11)
        )
        self.offer = make_offer(self.absence, self.candidate)
        self.url = reverse("respond_to_offer", args=[self.offer.pk])

    def test_anonymous_get_redirects_to_login(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("login"), response.url)

    def test_another_teachers_offer_is_not_found(self):
        someone_else = make_teacher("respond_someone_else")
        self.client.force_login(someone_else.user)

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 404)

    def test_accept_creates_the_substitution_and_notifies_both_sides(self):
        self.client.force_login(self.candidate.user)

        response = self.client.post(self.url, {"action": "accept"})

        self.assertRedirects(response, reverse("dashboard"))
        self.offer.refresh_from_db()
        self.assertEqual(self.offer.status, SubstitutionOffer.Status.ACCEPTED)
        self.assertIsNotNone(self.offer.responded_at)
        substitution = self.offer.resulting_substitution
        self.assertIsNotNone(substitution)
        self.assertEqual(substitution.substitute_teacher, self.candidate)

        self.assertEqual(len(mail.outbox), 2)
        confirmation = next(m for m in mail.outbox if m.to == ["candidate@example.edu"])
        self.assertEqual(len(confirmation.attachments), 1)
        filename, content, mimetype = confirmation.attachments[0]
        self.assertEqual(filename, "substitution.ics")
        self.assertIn("BEGIN:VEVENT", content)
        reporter_notice = next(m for m in mail.outbox if m.to == ["absent@example.edu"])
        self.assertTrue(reporter_notice.subject)

    def test_accept_fails_gracefully_when_the_slot_is_already_covered(self):
        other_substitute = make_teacher("respond_already_covered")
        give_free_all_week(other_substitute)
        make_substitution(self.absence, other_substitute)
        self.client.force_login(self.candidate.user)

        response = self.client.post(self.url, {"action": "accept"})

        self.assertRedirects(response, reverse("dashboard"))
        self.offer.refresh_from_db()
        self.assertEqual(self.offer.status, SubstitutionOffer.Status.EXPIRED)
        self.assertEqual(Substitution.objects.filter(absence=self.absence).count(), 1)

    def test_accept_fails_gracefully_when_the_candidate_is_now_busy_elsewhere(self):
        other_absent = make_teacher("respond_other_absent")
        give_free_all_week(other_absent)
        other_absence = Absence.objects.create(
            teacher=other_absent, start_datetime=dt(2024, 1, 8, 10), end_datetime=dt(2024, 1, 8, 12)
        )
        make_substitution(other_absence, self.candidate)
        self.client.force_login(self.candidate.user)

        response = self.client.post(self.url, {"action": "accept"})

        self.assertRedirects(response, reverse("dashboard"))
        self.offer.refresh_from_db()
        self.assertEqual(self.offer.status, SubstitutionOffer.Status.EXPIRED)
        self.assertFalse(Substitution.objects.filter(absence=self.absence).exists())

    def test_accepting_one_of_two_sibling_offers_expires_the_other(self):
        other_candidate = make_teacher("respond_sibling", email="sibling@example.edu")
        give_free_all_week(other_candidate)
        sibling_offer = make_offer(self.absence, other_candidate)

        self.client.force_login(self.candidate.user)
        self.client.post(self.url, {"action": "accept"})

        sibling_offer.refresh_from_db()
        self.assertEqual(sibling_offer.status, SubstitutionOffer.Status.EXPIRED)
        self.assertFalse(any(m.to == ["sibling@example.edu"] for m in mail.outbox))

    def test_decline_notifies_the_reporting_teacher_and_creates_nothing(self):
        self.client.force_login(self.candidate.user)

        response = self.client.post(self.url, {"action": "decline", "reason": "meeting"})

        self.assertRedirects(response, reverse("dashboard"))
        self.offer.refresh_from_db()
        self.assertEqual(self.offer.status, SubstitutionOffer.Status.DECLINED)
        self.assertEqual(self.offer.decline_reason, SubstitutionOffer.DeclineReason.MEETING)
        self.assertFalse(Substitution.objects.filter(absence=self.absence).exists())
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["absent@example.edu"])
        self.assertIn("Meeting", mail.outbox[0].body)

    def test_decline_requires_a_reason(self):
        self.client.force_login(self.candidate.user)

        response = self.client.post(self.url, {"action": "decline"})

        self.assertEqual(response.status_code, 200)
        self.offer.refresh_from_db()
        self.assertEqual(self.offer.status, SubstitutionOffer.Status.PENDING)
        self.assertEqual(mail.outbox, [])

    def test_decline_reason_radios_render_as_bootstrap_form_checks(self):
        self.client.force_login(self.candidate.user)

        response = self.client.get(self.url)
        html = response.content.decode()

        # every reason radio must carry form-check-input so its label sits beside it
        radios = [line for line in html.splitlines() if 'type="radio"' in line and 'name="reason"' in line]
        self.assertEqual(len(radios), 4)
        self.assertTrue(all("form-check-input" in radio for radio in radios))
        # the script that ticks "Other" when the free-text detail is typed into
        self.assertIn('value="other"]', html)

    def test_decline_with_other_needs_free_text_detail(self):
        self.client.force_login(self.candidate.user)

        rejected = self.client.post(self.url, {"action": "decline", "reason": "other"})
        self.assertEqual(rejected.status_code, 200)
        self.offer.refresh_from_db()
        self.assertEqual(self.offer.status, SubstitutionOffer.Status.PENDING)

        accepted = self.client.post(
            self.url, {"action": "decline", "reason": "other", "detail": "Dentist appointment"}
        )
        self.assertRedirects(accepted, reverse("dashboard"))
        self.offer.refresh_from_db()
        self.assertEqual(self.offer.status, SubstitutionOffer.Status.DECLINED)
        self.assertEqual(self.offer.decline_reason_detail, "Dentist appointment")
        self.assertIn("Dentist appointment", mail.outbox[0].body)

    def test_get_on_an_already_responded_offer_shows_read_only_status(self):
        self.offer.status = SubstitutionOffer.Status.DECLINED
        self.offer.save(update_fields=["status"])
        self.client.force_login(self.candidate.user)

        response = self.client.get(self.url)

        self.assertContains(response, "already declined")
        self.assertNotContains(response, "name=\"action\" value=\"accept\"")


@override_settings(LANGUAGE_CODE="en")
class DashboardOffersToMeTests(TestCase):
    def test_pending_offer_listed_with_a_link_to_respond(self):
        absent = make_teacher("dashboard_offer_absent")
        candidate = make_teacher("dashboard_offer_candidate")
        absence = Absence.objects.create(
            teacher=absent, start_datetime=next_monday_dt(9), end_datetime=next_monday_dt(11)
        )
        offer = make_offer(absence, candidate)
        self.client.force_login(candidate.user)

        response = self.client.get(reverse("dashboard"))

        self.assertContains(response, "Coverage requests for you")
        self.assertContains(response, reverse("respond_to_offer", args=[offer.pk]))

    def test_no_card_when_there_are_no_pending_offers(self):
        candidate = make_teacher("dashboard_no_offers")
        self.client.force_login(candidate.user)

        response = self.client.get(reverse("dashboard"))

        self.assertNotContains(response, "Coverage requests for you")


class CaDateFormattingTests(TestCase):
    def test_datetime_rendered_in_school_timezone_not_utc(self):
        """DB datetimes come back as UTC; the ca_datetime helper must convert to
        Europe/Madrid so a 9:00 absence never shows as 7:00."""
        from .dates_ca import format_ca_datetime

        aware = timezone.make_aware(datetime.datetime(2026, 6, 1, 9, 0))
        as_utc = aware.astimezone(datetime.timezone.utc)

        self.assertEqual(format_ca_datetime(as_utc), "Dilluns, 01/06/2026 09:00")

    def test_dashboard_shows_local_start_time(self):
        teacher = make_teacher("tz_dashboard")
        Absence.objects.create(
            teacher=teacher,
            start_datetime=dt(2026, 6, 1, 9),
            end_datetime=dt(2026, 6, 1, 17),
        )
        self.client.force_login(teacher.user)

        response = self.client.get(reverse("dashboard"))

        self.assertContains(response, "09:00")
        self.assertNotContains(response, "07:00")

    def test_timeframe_collapses_the_date_when_start_and_end_share_a_day(self):
        from .dates_ca import format_ca_timeframe

        self.assertEqual(
            format_ca_timeframe(dt(2026, 8, 31, 11), dt(2026, 8, 31, 12)),
            "Dilluns, 31/08/2026 11:00 – 12:00",
        )

    def test_timeframe_spells_out_both_ends_when_they_differ(self):
        from .dates_ca import format_ca_timeframe

        self.assertEqual(
            format_ca_timeframe(dt(2026, 8, 31, 11), dt(2026, 9, 1, 12)),
            "Dilluns, 31/08/2026 11:00 – Dimarts, 01/09/2026 12:00",
        )


class DefaultLanguageTests(TestCase):
    def test_visitors_get_catalan_even_with_an_english_browser(self):
        response = self.client.get(reverse("login"), HTTP_ACCEPT_LANGUAGE="en-US,en;q=0.9")

        self.assertEqual(response["Content-Language"], "ca")
        self.assertContains(response, "Inicia la sessió")

    def test_language_switcher_choice_is_honoured(self):
        self.client.cookies["django_language"] = "en"

        response = self.client.get(reverse("login"))

        self.assertEqual(response["Content-Language"], "en")
        self.assertContains(response, "Log in")


class ReportAbsenceFormTests(TestCase):
    """The report form refuses a period the requesting teacher wasn't due to be
    teaching for at all - there'd be nothing for a substitute to cover."""

    def setUp(self):
        self.teacher = make_teacher("reporter")
        self.client.force_login(self.teacher.user)

    def _post(self, start, end, **extra):
        # 2024-01-08 is a Monday.
        return self.client.post(
            reverse("report_absence"),
            {"date": "2024-01-08", "start_time": start, "end_time": end, "reason": Absence.Reason.PERMITS, **extra},
        )

    def test_period_entirely_within_own_non_teaching_hours_is_rejected(self):
        WeeklyNonTeachingHours.objects.create(
            teacher=self.teacher,
            weekday=WeeklyNonTeachingHours.Weekday.MONDAY,
            start_time=datetime.time(9, 0),
            end_time=datetime.time(12, 0),
        )

        response = self._post("09:00", "11:00")

        self.assertEqual(response.status_code, 200)
        self.assertIn(
            "classes per cobrir", " ".join(response.context["form"].non_field_errors())
        )
        self.assertFalse(Absence.objects.exists())

    def test_period_entirely_outside_school_hours_is_rejected(self):
        response = self._post("13:00", "15:00")

        self.assertEqual(response.status_code, 200)
        self.assertIn(
            "classes per cobrir", " ".join(response.context["form"].non_field_errors())
        )
        self.assertFalse(Absence.objects.exists())


    def test_period_on_a_weekend_is_rejected(self):
        # 2024-01-13 is a Saturday.
        response = self.client.post(
            reverse("report_absence"),
            {"date": "2024-01-13", "start_time": "09:00", "end_time": "11:00", "reason": Absence.Reason.PERMITS},
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(
            "classes per cobrir", " ".join(response.context["form"].non_field_errors())
        )
        self.assertFalse(Absence.objects.exists())

    def test_period_with_some_teaching_time_still_goes_through(self):
        WeeklyNonTeachingHours.objects.create(
            teacher=self.teacher,
            weekday=WeeklyNonTeachingHours.Weekday.MONDAY,
            start_time=datetime.time(9, 0),
            end_time=datetime.time(10, 0),
        )

        response = self._post("09:00", "11:00")

        self.assertRedirects(response, reverse("pick_substitute", args=[Absence.objects.get().pk]))


@override_settings(LANGUAGE_CODE="en")
class StatsDashboardTests(TestCase):
    def setUp(self):
        self.absent = make_teacher("stats_absent", email="stats_absent@example.edu")
        self.sub = make_teacher("stats_sub")
        give_free_all_week(self.sub)
        # Two absences this course year: one fully covered, one left with a gap.
        covered = Absence.objects.create(
            teacher=self.absent, start_datetime=course_dt(1, 9), end_datetime=course_dt(1, 10)
        )
        make_substitution(covered, self.sub)
        self.uncovered = Absence.objects.create(
            teacher=self.absent, start_datetime=course_dt(2, 9), end_datetime=course_dt(2, 11)
        )
        offer = make_offer(self.uncovered, self.sub)
        decline_offer(offer, SubstitutionOffer.DeclineReason.MEETING)

        # Last year's absence + substitution must not count toward this year.
        last_year = Absence.objects.create(
            teacher=self.absent,
            start_datetime=course_dt(-40, 9),
            end_datetime=course_dt(-40, 12),
        )
        make_substitution(last_year, self.sub)

    def test_build_admin_stats_numbers(self):
        stats = build_admin_stats()

        # Only this year's two absences / one substitution count - last year's
        # 3-hour substitution is excluded.
        self.assertEqual(stats["totals"]["absences"], 2)
        self.assertEqual(stats["totals"]["substitutions"], 1)
        self.assertEqual(stats["totals"]["hours_covered"], "1h")
        self.assertEqual(stats["coverage"]["fully_covered"], 1)
        self.assertEqual(stats["coverage"]["not_fully_covered"], 1)
        self.assertEqual(stats["coverage"]["rate"], 50)
        self.assertIn({"label": "Meeting", "count": 1}, stats["decline_reasons"])
        self.assertEqual(stats["top_substitutes"][0]["teacher"], self.sub)
        self.assertEqual(stats["top_substitutes"][0]["count"], 1)
        self.assertEqual(stats["top_substitutes"][0]["time"], "1h")
        self.assertEqual(stats["course_start"], course_year_start())

    def test_dashboard_page_renders_for_staff(self):
        staff = User.objects.create_user("stats_staff", password="pw", is_staff=True, is_superuser=True)
        self.client.force_login(staff)

        response = self.client.get(reverse("admin:substitutions_stats"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Stats dashboard")
        self.assertContains(response, "50%")  # coverage rate
        self.assertContains(response, "Meeting")  # decline reason breakdown
        self.assertContains(response, "stats_sub")  # top substitute
        self.assertRegex(response.content.decode(), r"Generated .+\d")  # timestamp filled in

    def test_dashboard_page_is_staff_only(self):
        response = self.client.get(reverse("admin:substitutions_stats"))
        self.assertEqual(response.status_code, 302)
        self.assertIn("/admin/login/", response.url)


class CourseYearStartTests(SimpleTestCase):
    def test_september_onwards_is_this_years_september(self):
        self.assertEqual(
            course_year_start(datetime.date(2026, 9, 1)),
            timezone.make_aware(datetime.datetime(2026, 9, 1)),
        )
        self.assertEqual(
            course_year_start(datetime.date(2026, 12, 31)),
            timezone.make_aware(datetime.datetime(2026, 9, 1)),
        )

    def test_before_september_is_last_years_september(self):
        self.assertEqual(
            course_year_start(datetime.date(2026, 8, 31)),
            timezone.make_aware(datetime.datetime(2025, 9, 1)),
        )
        self.assertEqual(
            course_year_start(datetime.date(2026, 1, 1)),
            timezone.make_aware(datetime.datetime(2025, 9, 1)),
        )
