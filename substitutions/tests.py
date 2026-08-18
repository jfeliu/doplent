import datetime

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from teachers.models import Teacher, WeeklyNonTeachingHours

from .models import Absence, Substitution
from .services import build_coverage_plan, find_available_substitutes


def make_teacher(username, grade_level=Teacher.GradeLevel.PRIMARY, active=True):
    user = User.objects.create_user(username=username, password="pw", first_name=username)
    return Teacher.objects.create(user=user, grade_level=grade_level, active=active)


def give_free_all_week(teacher, start=datetime.time(8, 0), end=datetime.time(16, 0)):
    """Give the teacher a single non-teaching block covering the whole day, every day."""
    for weekday, _label in WeeklyNonTeachingHours.Weekday.choices:
        WeeklyNonTeachingHours.objects.create(teacher=teacher, weekday=weekday, start_time=start, end_time=end)


def dt(year, month, day, hour, minute=0):
    return timezone.make_aware(datetime.datetime(year, month, day, hour, minute))


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


class FindAvailableSubstitutesTests(TestCase):
    def setUp(self):
        self.absent = make_teacher("absent", grade_level=Teacher.GradeLevel.PRIMARY)
        give_free_all_week(self.absent)
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


class PickSubstituteViewTests(TestCase):
    def setUp(self):
        self.absent = make_teacher("absent_view", grade_level=Teacher.GradeLevel.PRIMARY)
        give_free_all_week(self.absent)
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
        give_free_all_week(self.absent)
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


class SplitPickSubstituteViewTests(TestCase):
    def setUp(self):
        self.absent = make_teacher("absent_split_view", grade_level=Teacher.GradeLevel.PRIMARY)
        give_free_all_week(self.absent)
        # 2024-01-08 is a Monday, 9am-noon.
        self.absence = Absence.objects.create(
            teacher=self.absent, start_datetime=dt(2024, 1, 8, 9), end_datetime=dt(2024, 1, 8, 12)
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

    def test_picking_a_substitute_for_each_period_fully_covers_the_absence(self):
        url = reverse("pick_substitute", args=[self.absence.pk])

        response = self.client.post(
            url,
            {
                "slot_start": self.absence.start_datetime.isoformat(),
                "slot_end": dt(2024, 1, 8, 10, 30).isoformat(),
                "substitute_id": self.first_half.pk,
            },
        )
        self.assertRedirects(response, url)
        self.assertEqual(
            Substitution.objects.filter(absence=self.absence, substitute_teacher=self.first_half).count(), 1
        )

        response = self.client.post(
            url,
            {
                "slot_start": dt(2024, 1, 8, 10, 30).isoformat(),
                "slot_end": self.absence.end_datetime.isoformat(),
                "substitute_id": self.second_half.pk,
            },
        )
        self.assertRedirects(response, reverse("dashboard"))

        substitutions = Substitution.objects.filter(absence=self.absence).order_by("start_datetime")
        self.assertEqual(list(substitutions.values_list("substitute_teacher", flat=True)),
                          [self.first_half.pk, self.second_half.pk])
