import datetime

from django.contrib.auth.models import User
from django.core import mail
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from teachers.models import Teacher, WeeklyNonTeachingHours

from .models import Absence, Substitution, SubstitutionOffer
from .services import build_coverage_plan, discarded_periods, find_available_substitutes, uncovered_ranges


def make_teacher(username, grade_level=Teacher.GradeLevel.PRIMARY, active=True, email=""):
    user = User.objects.create_user(username=username, password="pw", first_name=username, email=email)
    return Teacher.objects.create(user=user, grade_level=grade_level, active=active)


def give_free_all_week(teacher, start=datetime.time(8, 0), end=datetime.time(16, 0)):
    """Give the teacher a single non-teaching block covering the whole day, every day."""
    for weekday, _label in WeeklyNonTeachingHours.Weekday.choices:
        WeeklyNonTeachingHours.objects.create(teacher=teacher, weekday=weekday, start_time=start, end_time=end)


def dt(year, month, day, hour, minute=0):
    return timezone.make_aware(datetime.datetime(year, month, day, hour, minute))


def next_monday_dt(hour, minute=0):
    """An aware datetime on the next upcoming Monday, for offer tests -
    expire_stale_offers() compares against the real wall clock, so a fixed
    past calendar date (like the 2024-01-08 used elsewhere in this file)
    would look already-expired by the time this suite runs."""
    today = timezone.localdate()
    monday = today + datetime.timedelta(days=(7 - today.weekday()) % 7 or 7)
    return timezone.make_aware(datetime.datetime.combine(monday, datetime.time(hour, minute)))


def result_for(results, teacher):
    """find_available_substitutes returns fresh instances with has_nothing_to_do
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
                start_datetime=dt(2024, 1, 1 + i, 9),
                end_datetime=dt(2024, 1, 1 + i, 10),
            )
            make_substitution(past_absence, busy)

        results = find_available_substitutes(self.absence)
        self.assertEqual(results[0], idle)
        self.assertIn(busy, results)
        self.assertLess(results.index(idle), results.index(busy))

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
            is_paperwork=True,
        )

        results = find_available_substitutes(self.absence)
        self.assertIn(candidate, results)
        self.assertFalse(result_for(results, candidate).has_nothing_to_do)

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
            is_paperwork=True,
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
        self.assertTrue(result_for(results, busy_but_free).has_nothing_to_do)
        self.assertFalse(result_for(results, paperwork_light).has_nothing_to_do)

    def test_mixed_paperwork_and_free_blocks_prefer_only_when_fully_non_paperwork_covers(self):
        # Free (non-paperwork) 8-9:30, paperwork 9:30-16: the 9-11 request needs
        # both blocks to be covered at all, so paperwork time was involved.
        candidate = make_teacher("mixed_blocks")
        WeeklyNonTeachingHours.objects.create(
            teacher=candidate,
            weekday=WeeklyNonTeachingHours.Weekday.MONDAY,
            start_time=datetime.time(8, 0),
            end_time=datetime.time(9, 30),
            is_paperwork=False,
        )
        WeeklyNonTeachingHours.objects.create(
            teacher=candidate,
            weekday=WeeklyNonTeachingHours.Weekday.MONDAY,
            start_time=datetime.time(9, 30),
            end_time=datetime.time(16, 0),
            is_paperwork=True,
        )

        results = find_available_substitutes(self.absence)
        self.assertIn(candidate, results)
        self.assertFalse(result_for(results, candidate).has_nothing_to_do)

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

        response = self.client.post(
            reverse("pick_substitute", args=[self.absence.pk]),
            {
                "slot_start": self.absence.start_datetime.isoformat(),
                "slot_end": self.absence.end_datetime.isoformat(),
                "substitute_id": busy_substitute.pk,
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(Substitution.objects.filter(absence=self.absence).exists())


class BuildCoveragePlanTests(TestCase):
    def setUp(self):
        self.absent = make_teacher("absent_split", grade_level=Teacher.GradeLevel.PRIMARY)
        # 2024-01-08 is a Monday, 9am-noon.
        self.absence = Absence.objects.create(
            teacher=self.absent, start_datetime=dt(2024, 1, 8, 9), end_datetime=dt(2024, 1, 8, 12)
        )

    def test_single_teacher_covering_everything_yields_one_slot(self):
        candidate = make_teacher("covers_all")
        give_free_all_week(candidate)

        slots = build_coverage_plan(self.absence)
        self.assertEqual(len(slots), 1)
        self.assertEqual(slots[0]["start_datetime"], self.absence.start_datetime)
        self.assertEqual(slots[0]["end_datetime"], self.absence.end_datetime)
        self.assertIn(candidate, slots[0]["candidates"])

    def test_no_one_covers_it_all_splits_into_periods_covering_the_full_range(self):
        # first_half is free 9-10:30, second_half is free 10:30-12: neither
        # alone covers the full 9-noon absence, but together they can.
        first_half = make_teacher("first_half")
        WeeklyNonTeachingHours.objects.create(
            teacher=first_half,
            weekday=WeeklyNonTeachingHours.Weekday.MONDAY,
            start_time=datetime.time(9, 0),
            end_time=datetime.time(10, 30),
        )
        second_half = make_teacher("second_half")
        WeeklyNonTeachingHours.objects.create(
            teacher=second_half,
            weekday=WeeklyNonTeachingHours.Weekday.MONDAY,
            start_time=datetime.time(10, 30),
            end_time=datetime.time(12, 0),
        )

        slots = build_coverage_plan(self.absence)

        self.assertEqual(len(slots), 2)
        self.assertEqual(slots[0]["start_datetime"], self.absence.start_datetime)
        self.assertEqual(slots[0]["end_datetime"], dt(2024, 1, 8, 10, 30))
        self.assertIn(first_half, slots[0]["candidates"])
        self.assertEqual(slots[1]["start_datetime"], dt(2024, 1, 8, 10, 30))
        self.assertEqual(slots[1]["end_datetime"], self.absence.end_datetime)
        self.assertIn(second_half, slots[1]["candidates"])

        # The union of the slots covers the absence with no gaps or overlaps.
        self.assertEqual(slots[0]["end_datetime"], slots[1]["start_datetime"])

    def test_gap_nobody_is_free_during_cannot_be_split_away(self):
        # Free 9-10 and 11-12, with a 10-11 hole nobody covers at all.
        only_edges = make_teacher("only_edges")
        WeeklyNonTeachingHours.objects.create(
            teacher=only_edges,
            weekday=WeeklyNonTeachingHours.Weekday.MONDAY,
            start_time=datetime.time(9, 0),
            end_time=datetime.time(10, 0),
        )
        WeeklyNonTeachingHours.objects.create(
            teacher=only_edges,
            weekday=WeeklyNonTeachingHours.Weekday.MONDAY,
            start_time=datetime.time(11, 0),
            end_time=datetime.time(12, 0),
        )

        slots = build_coverage_plan(self.absence)

        # No possible split fully covers the range, so no slot offers a
        # genuinely selectable candidate anywhere.
        self.assertTrue(all(not c.already_substituting for slot in slots for c in slot["candidates"]))
        self.assertFalse(any(slot["candidates"] for slot in slots))

    def test_already_covered_absence_has_no_open_slots(self):
        candidate = make_teacher("already_covering")
        give_free_all_week(candidate)
        make_substitution(self.absence, candidate)

        self.assertEqual(build_coverage_plan(self.absence), [])

    def test_requester_own_free_block_splits_the_plan_around_it(self):
        # self.absence is 9-noon Monday. The absent teacher has a personal
        # free block 10:30-11, so only 9-10:30 and 11-noon actually need a
        # substitute.
        WeeklyNonTeachingHours.objects.create(
            teacher=self.absent,
            weekday=WeeklyNonTeachingHours.Weekday.MONDAY,
            start_time=datetime.time(10, 30),
            end_time=datetime.time(11, 0),
        )
        candidate = make_teacher("covers_both_pieces")
        WeeklyNonTeachingHours.objects.create(
            teacher=candidate,
            weekday=WeeklyNonTeachingHours.Weekday.MONDAY,
            start_time=datetime.time(9, 0),
            end_time=datetime.time(10, 30),
        )
        WeeklyNonTeachingHours.objects.create(
            teacher=candidate,
            weekday=WeeklyNonTeachingHours.Weekday.MONDAY,
            start_time=datetime.time(11, 0),
            end_time=datetime.time(12, 0),
        )

        slots = build_coverage_plan(self.absence)

        self.assertEqual(len(slots), 2)
        self.assertEqual(slots[0]["start_datetime"], self.absence.start_datetime)
        self.assertEqual(slots[0]["end_datetime"], dt(2024, 1, 8, 10, 30))
        self.assertEqual(slots[1]["start_datetime"], dt(2024, 1, 8, 11, 0))
        self.assertEqual(slots[1]["end_datetime"], self.absence.end_datetime)
        self.assertIn(candidate, slots[0]["candidates"])
        self.assertIn(candidate, slots[1]["candidates"])

    def test_partially_covered_absence_only_plans_the_remaining_gap(self):
        first_half = make_teacher("first_half_confirmed")
        give_free_all_week(first_half)
        make_substitution(self.absence, first_half, start=self.absence.start_datetime, end=dt(2024, 1, 8, 10, 30))

        second_half = make_teacher("second_half_candidate")
        WeeklyNonTeachingHours.objects.create(
            teacher=second_half,
            weekday=WeeklyNonTeachingHours.Weekday.MONDAY,
            start_time=datetime.time(10, 30),
            end_time=datetime.time(12, 0),
        )

        slots = build_coverage_plan(self.absence)

        self.assertEqual(len(slots), 1)
        self.assertEqual(slots[0]["start_datetime"], dt(2024, 1, 8, 10, 30))
        self.assertEqual(slots[0]["end_datetime"], self.absence.end_datetime)
        self.assertIn(second_half, slots[0]["candidates"])


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


class DiscardedPeriodsTests(TestCase):
    def setUp(self):
        self.absent = make_teacher("absent_discarded", grade_level=Teacher.GradeLevel.PRIMARY)

    def test_outside_working_hours_reported(self):
        # 2024-01-08 is a Monday, 7-8am is before the 9am opening.
        absence = Absence.objects.create(
            teacher=self.absent, start_datetime=dt(2024, 1, 8, 7), end_datetime=dt(2024, 1, 8, 8)
        )

        self.assertEqual(
            discarded_periods(absence),
            [{"start_datetime": dt(2024, 1, 8, 7), "end_datetime": dt(2024, 1, 8, 8), "reason": "outside_working_hours"}],
        )

    def test_requester_free_time_reported(self):
        WeeklyNonTeachingHours.objects.create(
            teacher=self.absent,
            weekday=WeeklyNonTeachingHours.Weekday.MONDAY,
            start_time=datetime.time(9, 30),
            end_time=datetime.time(10, 0),
        )
        absence = Absence.objects.create(
            teacher=self.absent, start_datetime=dt(2024, 1, 8, 9), end_datetime=dt(2024, 1, 8, 11)
        )

        self.assertEqual(
            discarded_periods(absence),
            [{
                "start_datetime": dt(2024, 1, 8, 9, 30),
                "end_datetime": dt(2024, 1, 8, 10),
                "reason": "requester_free",
            }],
        )

    def test_both_reasons_reported_in_order_outside_hours_wins_when_overlapping(self):
        # Absent 12-15:30 (spans the 1-3pm break) and personally free 12-12:30
        # within working hours.
        WeeklyNonTeachingHours.objects.create(
            teacher=self.absent,
            weekday=WeeklyNonTeachingHours.Weekday.MONDAY,
            start_time=datetime.time(12, 0),
            end_time=datetime.time(12, 30),
        )
        absence = Absence.objects.create(
            teacher=self.absent, start_datetime=dt(2024, 1, 8, 12), end_datetime=dt(2024, 1, 8, 15, 30)
        )

        self.assertEqual(
            discarded_periods(absence),
            [
                {"start_datetime": dt(2024, 1, 8, 12), "end_datetime": dt(2024, 1, 8, 12, 30), "reason": "requester_free"},
                {"start_datetime": dt(2024, 1, 8, 13), "end_datetime": dt(2024, 1, 8, 15), "reason": "outside_working_hours"},
            ],
        )

    def test_no_discarded_periods_when_absence_fully_within_working_hours(self):
        absence = Absence.objects.create(
            teacher=self.absent, start_datetime=dt(2024, 1, 8, 9), end_datetime=dt(2024, 1, 8, 11)
        )

        self.assertEqual(discarded_periods(absence), [])


class PickSubstituteViewDiscardedPeriodsTests(TestCase):
    def test_page_shows_discarded_periods_with_reasons(self):
        absent = make_teacher("absent_page_discard", grade_level=Teacher.GradeLevel.PRIMARY)
        WeeklyNonTeachingHours.objects.create(
            teacher=absent,
            weekday=WeeklyNonTeachingHours.Weekday.MONDAY,
            start_time=datetime.time(9, 30),
            end_time=datetime.time(10, 0),
        )
        candidate = make_teacher("covers_page_discard")
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
            end_time=datetime.time(18, 0),
        )
        # 2024-01-08 is a Monday.
        absence = Absence.objects.create(
            teacher=absent, start_datetime=dt(2024, 1, 8, 9), end_datetime=dt(2024, 1, 8, 18)
        )
        self.client.force_login(absent.user)

        response = self.client.get(reverse("pick_substitute", args=[absence.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Periods that don't need a substitute")
        self.assertContains(response, "Outside school hours")
        self.assertContains(response, "Requester's own non-working hours")
        self.assertContains(response, "Covering")


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

    def test_picking_a_substitute_for_each_period_creates_offers_that_together_cover_the_absence(self):
        url = reverse("pick_substitute", args=[self.absence.pk])

        response = self.client.post(
            url,
            {
                "slot_start": self.absence.start_datetime.isoformat(),
                "slot_end": next_monday_dt(10, 30).isoformat(),
                "substitute_id": self.first_half.pk,
            },
        )
        self.assertRedirects(response, url)
        first_offer = SubstitutionOffer.objects.get(absence=self.absence, substitute_teacher=self.first_half)
        self.assertEqual(first_offer.status, SubstitutionOffer.Status.PENDING)

        response = self.client.post(
            url,
            {
                "slot_start": next_monday_dt(10, 30).isoformat(),
                "slot_end": self.absence.end_datetime.isoformat(),
                "substitute_id": self.second_half.pk,
            },
        )
        self.assertRedirects(response, url)
        second_offer = SubstitutionOffer.objects.get(absence=self.absence, substitute_teacher=self.second_half)

        # No Substitution exists until each candidate accepts their offer.
        self.assertFalse(Substitution.objects.filter(absence=self.absence).exists())

        self.client.force_login(self.first_half.user)
        self.client.post(reverse("respond_to_offer", args=[first_offer.pk]), {"action": "accept"})
        self.client.force_login(self.second_half.user)
        self.client.post(reverse("respond_to_offer", args=[second_offer.pk]), {"action": "accept"})

        substitutions = Substitution.objects.filter(absence=self.absence).order_by("start_datetime")
        self.assertEqual(list(substitutions.values_list("substitute_teacher", flat=True)),
                          [self.first_half.pk, self.second_half.pk])


class PickSubstituteOfferFlowTests(TestCase):
    def setUp(self):
        self.absent = make_teacher("absent_offer_view", grade_level=Teacher.GradeLevel.PRIMARY)
        self.absence = Absence.objects.create(
            teacher=self.absent, start_datetime=next_monday_dt(9), end_datetime=next_monday_dt(11)
        )
        self.client.force_login(self.absent.user)
        self.url = reverse("pick_substitute", args=[self.absence.pk])

    def _post(self, candidate):
        return self.client.post(
            self.url,
            {
                "slot_start": self.absence.start_datetime.isoformat(),
                "slot_end": self.absence.end_datetime.isoformat(),
                "substitute_id": candidate.pk,
            },
        )

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

    def test_two_candidates_can_both_receive_a_parallel_offer_for_the_same_slot(self):
        first = make_teacher("offer_parallel_1", email="first@example.edu")
        give_free_all_week(first)
        second = make_teacher("offer_parallel_2", email="second@example.edu")
        give_free_all_week(second)

        self._post(first)
        self._post(second)

        self.assertEqual(SubstitutionOffer.objects.filter(absence=self.absence).count(), 2)
        self.assertFalse(Substitution.objects.filter(absence=self.absence).exists())

    def test_page_shows_awaiting_response_for_a_pending_offer(self):
        candidate = make_teacher("offer_pending_display", email="candidate@example.edu")
        give_free_all_week(candidate)
        self._post(candidate)

        response = self.client.get(self.url)

        self.assertContains(response, "Awaiting response")
        self.assertContains(response, "Awaiting confirmation")

    def test_offer_re_enabled_after_being_declined(self):
        candidate = make_teacher("offer_declined_display", email="candidate@example.edu")
        give_free_all_week(candidate)
        offer = make_offer(self.absence, candidate, status=SubstitutionOffer.Status.DECLINED)

        response = self.client.get(self.url)

        self.assertContains(response, "Previously declined")
        # Declined doesn't block re-offering: the submit form is still there.
        self.assertContains(response, f'value="{candidate.pk}"')
        self.assertNotEqual(offer.status, SubstitutionOffer.Status.PENDING)


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

        response = self.client.post(self.url, {"action": "decline"})

        self.assertRedirects(response, reverse("dashboard"))
        self.offer.refresh_from_db()
        self.assertEqual(self.offer.status, SubstitutionOffer.Status.DECLINED)
        self.assertFalse(Substitution.objects.filter(absence=self.absence).exists())
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["absent@example.edu"])

    def test_get_on_an_already_responded_offer_shows_read_only_status(self):
        self.offer.status = SubstitutionOffer.Status.DECLINED
        self.offer.save(update_fields=["status"])
        self.client.force_login(self.candidate.user)

        response = self.client.get(self.url)

        self.assertContains(response, "already declined")
        self.assertNotContains(response, "name=\"action\" value=\"accept\"")


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
