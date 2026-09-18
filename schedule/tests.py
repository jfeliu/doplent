import datetime

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse

from teachers.models import NonTeachingHoursKind, Teacher, WeeklyNonTeachingHours

from .models import ClassGroup, ScheduleEntry, Subject
from .services import (
    build_group_grid,
    format_hours,
    group_hours_summary,
    hours_by_group,
    hours_by_teacher,
    merge_contiguous_slots,
    non_teaching_hours_by_teacher,
    split_entry_slots,
    teacher_hours_summary,
    teacher_options_for,
)


def make_teacher(first_name: str, last_name: str, active: bool = True) -> Teacher:
    user = User.objects.create_user(
        username=f"{first_name}.{last_name}".lower(), first_name=first_name, last_name=last_name
    )
    return Teacher.objects.create(user=user, grade_level=Teacher.GradeLevel.PRIMARY, active=active)


def make_group(name: str, tutor: Teacher) -> ClassGroup:
    group = ClassGroup.objects.create(name=name, grade_level=Teacher.GradeLevel.PRIMARY)
    group.tutors.set([tutor])
    return group


def t(hour: int, minute: int = 0) -> datetime.time:
    return datetime.time(hour, minute)


class ScheduleEntryValidationTests(TestCase):
    def setUp(self):
        self.tutor = make_teacher("Anna", "Tutor")
        self.group = make_group("3r A", tutor=self.tutor)
        self.subject = Subject.objects.create(name="Matemàtiques")
        self.other_subject = Subject.objects.create(name="Anglès")

    def test_valid_entry_passes_clean(self):
        entry = ScheduleEntry(
            class_group=self.group, subject=self.subject, teacher=self.tutor,
            weekday=ScheduleEntry.Weekday.MONDAY, start_time=t(9), end_time=t(10),
        )
        entry.full_clean()  # does not raise

    def test_rejects_double_booking_the_same_group(self):
        ScheduleEntry.objects.create(
            class_group=self.group, subject=self.subject, teacher=self.tutor,
            weekday=0, start_time=t(9), end_time=t(10),
        )
        other_teacher = make_teacher("Bea", "Specialist")
        clashing = ScheduleEntry(
            class_group=self.group, subject=self.other_subject, teacher=other_teacher,
            weekday=0, start_time=t(9, 30), end_time=t(10, 30),
        )
        with self.assertRaises(ValidationError):
            clashing.full_clean()

    def test_rejects_teacher_double_booked_across_groups(self):
        ScheduleEntry.objects.create(
            class_group=self.group, subject=self.subject, teacher=self.tutor,
            weekday=0, start_time=t(9), end_time=t(10),
        )
        other_group = make_group("3r B", tutor=make_teacher("Carla", "Tutor"))
        clashing = ScheduleEntry(
            class_group=other_group, subject=self.other_subject, teacher=self.tutor,
            weekday=0, start_time=t(9, 30), end_time=t(10, 30),
        )
        with self.assertRaises(ValidationError):
            clashing.full_clean()

    def test_rejects_overlap_with_teachers_own_non_teaching_hours(self):
        WeeklyNonTeachingHours.objects.create(
            teacher=self.tutor, weekday=0, start_time=t(9), end_time=t(10),
            kind=NonTeachingHoursKind.PAPERWORK,
        )
        entry = ScheduleEntry(
            class_group=self.group, subject=self.subject, teacher=self.tutor,
            weekday=0, start_time=t(9, 30), end_time=t(10, 30),
        )
        with self.assertRaises(ValidationError):
            entry.full_clean()

    def test_non_overlapping_entries_on_the_same_day_are_fine(self):
        ScheduleEntry.objects.create(
            class_group=self.group, subject=self.subject, teacher=self.tutor,
            weekday=0, start_time=t(9), end_time=t(10),
        )
        entry = ScheduleEntry(
            class_group=self.group, subject=self.other_subject, teacher=self.tutor,
            weekday=0, start_time=t(10), end_time=t(11),
        )
        entry.full_clean()  # touching, not overlapping - does not raise


class CoTeacherValidationTests(TestCase):
    def setUp(self):
        self.tutor = make_teacher("Anna", "Tutor")
        self.group = make_group("3r A", tutor=self.tutor)
        self.subject = Subject.objects.create(name="Matemàtiques")
        self.co_teacher = make_teacher("Bea", "Specialist")

    def _entry(self):
        return ScheduleEntry(
            class_group=self.group, subject=self.subject, teacher=self.tutor,
            weekday=0, start_time=t(9), end_time=t(10),
        )

    def test_valid_co_teacher_passes_validation(self):
        entry = self._entry()
        entry.full_clean()
        entry.validate_co_teachers([self.co_teacher.pk])  # does not raise

    def test_multiple_valid_co_teachers_pass_validation(self):
        second_co_teacher = make_teacher("Carla", "Other")
        entry = self._entry()
        entry.full_clean()
        entry.validate_co_teachers([self.co_teacher.pk, second_co_teacher.pk])  # does not raise

    def test_co_teacher_cannot_be_the_same_as_teacher(self):
        entry = self._entry()
        entry.full_clean()
        with self.assertRaises(ValidationError):
            entry.validate_co_teachers([self.tutor.pk])

    def test_the_same_co_teacher_cannot_be_picked_twice(self):
        entry = self._entry()
        entry.full_clean()
        with self.assertRaises(ValidationError):
            entry.validate_co_teachers([self.co_teacher.pk, self.co_teacher.pk])

    def test_co_teacher_already_teaching_elsewhere_is_rejected(self):
        other_group = make_group("3r B", tutor=make_teacher("Carla", "Tutor"))
        ScheduleEntry.objects.create(
            class_group=other_group, subject=self.subject, teacher=self.co_teacher,
            weekday=0, start_time=t(9), end_time=t(9, 30),
        )
        entry = self._entry()
        entry.full_clean()
        with self.assertRaises(ValidationError):
            entry.validate_co_teachers([self.co_teacher.pk])

    def test_co_teacher_already_co_teaching_elsewhere_is_rejected(self):
        other_group = make_group("3r B", tutor=make_teacher("Carla", "Tutor"))
        other_entry = ScheduleEntry.objects.create(
            class_group=other_group, subject=self.subject, teacher=make_teacher("Dana", "Other"),
            weekday=0, start_time=t(9), end_time=t(9, 30),
        )
        other_entry.co_teachers.set([self.co_teacher])
        entry = self._entry()
        entry.full_clean()
        with self.assertRaises(ValidationError):
            entry.validate_co_teachers([self.co_teacher.pk])

    def test_co_teacher_marked_non_teaching_is_rejected(self):
        WeeklyNonTeachingHours.objects.create(
            teacher=self.co_teacher, weekday=0, start_time=t(9), end_time=t(10),
            kind=NonTeachingHoursKind.PAPERWORK,
        )
        entry = self._entry()
        entry.full_clean()
        with self.assertRaises(ValidationError):
            entry.validate_co_teachers([self.co_teacher.pk])


class MergeContiguousSlotsTests(TestCase):
    def test_merges_touching_slots(self):
        slots = [(t(9), t(9, 30)), (t(9, 30), t(10))]
        self.assertEqual(merge_contiguous_slots(slots), [(t(9), t(10))])

    def test_keeps_a_gap_as_separate_runs(self):
        slots = [(t(9), t(9, 30)), (t(10), t(10, 30))]
        self.assertEqual(merge_contiguous_slots(slots), [(t(9), t(9, 30)), (t(10), t(10, 30))])


class SplitEntrySlotsTests(TestCase):
    def setUp(self):
        self.tutor = make_teacher("Anna", "Tutor")
        self.group = make_group("3r A", tutor=self.tutor)
        self.subject = Subject.objects.create(name="Matemàtiques")
        # a 2-hour entry: 9:00-11:00, four half-hour slots
        self.entry = ScheduleEntry.objects.create(
            class_group=self.group, subject=self.subject, teacher=self.tutor,
            weekday=0, start_time=t(9), end_time=t(11),
        )

    def test_selecting_a_middle_chunk_splits_into_two_kept_runs(self):
        kept, selected = split_entry_slots(self.entry, {t(9, 30), t(10)})
        self.assertEqual(kept, [(t(9), t(9, 30)), (t(10, 30), t(11))])
        self.assertEqual(selected, [(t(9, 30), t(10, 30))])

    def test_selecting_a_prefix_leaves_one_kept_run(self):
        kept, selected = split_entry_slots(self.entry, {t(9), t(9, 30)})
        self.assertEqual(kept, [(t(10), t(11))])
        self.assertEqual(selected, [(t(9), t(10))])

    def test_selecting_everything_leaves_nothing_kept(self):
        kept, selected = split_entry_slots(self.entry, {t(9), t(9, 30), t(10), t(10, 30)})
        self.assertEqual(kept, [])
        self.assertEqual(selected, [(t(9), t(11))])


class HoursByTeacherTests(TestCase):
    def test_sums_minutes_across_every_group(self):
        tutor = make_teacher("Anna", "Tutor")
        group_a = make_group("3r A", tutor=tutor)
        group_b = make_group("3r B", tutor=make_teacher("Carla", "Tutor"))
        subject = Subject.objects.create(name="Matemàtiques")
        ScheduleEntry.objects.create(
            class_group=group_a, subject=subject, teacher=tutor, weekday=0, start_time=t(9), end_time=t(10)
        )
        ScheduleEntry.objects.create(
            class_group=group_b, subject=subject, teacher=tutor, weekday=1, start_time=t(9), end_time=t(9, 30)
        )
        self.assertEqual(hours_by_teacher([tutor.pk])[tutor.pk], 90)
        self.assertEqual(format_hours(90), "1h 30m")

    def test_counts_co_teaching_time_too(self):
        tutor = make_teacher("Anna", "Tutor")
        co_teacher = make_teacher("Bea", "Specialist")
        group = make_group("3r A", tutor=tutor)
        subject = Subject.objects.create(name="Matemàtiques")
        entry = ScheduleEntry.objects.create(
            class_group=group, subject=subject, teacher=tutor,
            weekday=0, start_time=t(9), end_time=t(10),
        )
        entry.co_teachers.set([co_teacher])
        self.assertEqual(hours_by_teacher([co_teacher.pk])[co_teacher.pk], 60)

    def test_counts_every_co_teacher_when_there_are_several(self):
        tutor = make_teacher("Anna", "Tutor")
        co_teacher_a = make_teacher("Bea", "Specialist")
        co_teacher_b = make_teacher("Carla", "Specialist")
        group = make_group("3r A", tutor=tutor)
        subject = Subject.objects.create(name="Matemàtiques")
        entry = ScheduleEntry.objects.create(
            class_group=group, subject=subject, teacher=tutor,
            weekday=0, start_time=t(9), end_time=t(10),
        )
        entry.co_teachers.set([co_teacher_a, co_teacher_b])
        totals = hours_by_teacher([co_teacher_a.pk, co_teacher_b.pk])
        self.assertEqual(totals[co_teacher_a.pk], 60)
        self.assertEqual(totals[co_teacher_b.pk], 60)


class HoursSummaryTests(TestCase):
    def test_teacher_hours_summary_is_sorted_alphabetically_by_display_name(self):
        # last names would sort the other way round ("Aznar" < "Bosch"), but
        # the table shows "first last" - it should read top-to-bottom A-Z as
        # displayed, not by surname.
        make_teacher("Zoe", "Aznar")
        make_teacher("Anna", "Bosch")
        names = [row.teacher for row in teacher_hours_summary()]
        self.assertEqual([str(n) for n in names], sorted(str(n) for n in names))

    def test_group_hours_summary_is_sorted_alphabetically_by_name(self):
        tutor = make_teacher("Anna", "Tutor")
        make_group("Zebra", tutor=tutor)
        make_group("Alfa", tutor=tutor)
        names = [row.class_group.name for row in group_hours_summary()]
        self.assertEqual(names, sorted(names))

    def test_non_teaching_hours_by_teacher_sums_all_kinds(self):
        tutor = make_teacher("Anna", "Tutor")
        WeeklyNonTeachingHours.objects.create(
            teacher=tutor, weekday=0, start_time=t(9), end_time=t(10), kind=NonTeachingHoursKind.FREE
        )
        WeeklyNonTeachingHours.objects.create(
            teacher=tutor, weekday=1, start_time=t(9), end_time=t(9, 30), kind=NonTeachingHoursKind.PAPERWORK
        )
        self.assertEqual(non_teaching_hours_by_teacher([tutor.pk])[tutor.pk], 90)

    def test_hours_by_group_sums_its_own_entries_only(self):
        tutor = make_teacher("Anna", "Tutor")
        group_a = make_group("3r A", tutor=tutor)
        group_b = make_group("3r B", tutor=make_teacher("Carla", "Tutor"))
        subject = Subject.objects.create(name="Matemàtiques")
        ScheduleEntry.objects.create(
            class_group=group_a, subject=subject, teacher=tutor, weekday=0, start_time=t(9), end_time=t(10)
        )
        ScheduleEntry.objects.create(
            class_group=group_b, subject=subject, teacher=tutor, weekday=0, start_time=t(9), end_time=t(9, 30)
        )
        totals = hours_by_group()
        self.assertEqual(totals[group_a.pk], 60)
        self.assertEqual(totals[group_b.pk], 30)

    def test_teacher_hours_summary_lists_active_teachers_with_both_totals(self):
        tutor = make_teacher("Anna", "Tutor")
        make_teacher("Zoe", "Inactive", active=False)
        group = make_group("3r A", tutor=tutor)
        subject = Subject.objects.create(name="Matemàtiques")
        ScheduleEntry.objects.create(
            class_group=group, subject=subject, teacher=tutor, weekday=0, start_time=t(9), end_time=t(10)
        )
        WeeklyNonTeachingHours.objects.create(
            teacher=tutor, weekday=0, start_time=t(10), end_time=t(10, 30), kind=NonTeachingHoursKind.FREE
        )
        rows = teacher_hours_summary()
        self.assertEqual(len(rows), 1)  # inactive teacher excluded
        self.assertEqual(rows[0].teacher, tutor)
        self.assertEqual(rows[0].teaching_label, "1h")
        self.assertEqual(rows[0].non_teaching_label, "30m")

    def test_group_hours_summary_lists_every_group(self):
        tutor = make_teacher("Anna", "Tutor")
        group = make_group("3r A", tutor=tutor)
        subject = Subject.objects.create(name="Matemàtiques")
        ScheduleEntry.objects.create(
            class_group=group, subject=subject, teacher=tutor, weekday=0, start_time=t(9), end_time=t(10, 30)
        )
        rows = group_hours_summary()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].class_group, group)
        self.assertEqual(rows[0].hours_label, "1h 30m")


class TeacherOptionsForTests(TestCase):
    def test_tutor_listed_first(self):
        tutor = make_teacher("Anna", "Tutor")
        group = make_group("3r A", tutor=tutor)
        make_teacher("Zoe", "Aaa")  # would sort first alphabetically, but isn't the tutor
        options = teacher_options_for(group)
        self.assertEqual(options[0].teacher, tutor)
        self.assertTrue(options[0].is_tutor)

    def test_every_tutor_is_flagged_when_a_group_has_several(self):
        tutor_a = make_teacher("Anna", "Tutor")
        tutor_b = make_teacher("Bea", "Tutor")
        group = make_group("3r A", tutor=tutor_a)
        group.tutors.add(tutor_b)
        options = teacher_options_for(group)
        tutor_flags = {opt.teacher.pk: opt.is_tutor for opt in options}
        self.assertTrue(tutor_flags[tutor_a.pk])
        self.assertTrue(tutor_flags[tutor_b.pk])

    def test_inactive_teachers_excluded(self):
        tutor = make_teacher("Anna", "Tutor")
        group = make_group("3r A", tutor=tutor)
        make_teacher("Bea", "Inactive", active=False)
        options = teacher_options_for(group)
        self.assertTrue(all(opt.teacher.active for opt in options))


class ClassGroupTutorsTests(TestCase):
    def test_tutor_count_defaults_to_one(self):
        group = ClassGroup.objects.create(name="3r A", grade_level=Teacher.GradeLevel.PRIMARY)
        self.assertEqual(group.tutor_count, 1)

    def test_can_have_more_than_one_tutor(self):
        tutor_a = make_teacher("Anna", "Tutor")
        tutor_b = make_teacher("Bea", "Tutor")
        group = make_group("3r A", tutor=tutor_a)
        group.tutor_count = 2
        group.tutors.add(tutor_b)
        group.save()
        self.assertEqual(set(group.tutors.all()), {tutor_a, tutor_b})

    def test_a_group_can_have_no_tutor_at_all(self):
        group = ClassGroup.objects.create(name="3r A", grade_level=Teacher.GradeLevel.PRIMARY)
        self.assertEqual(group.tutors.count(), 0)


class BuildGroupGridTests(TestCase):
    def setUp(self):
        self.tutor = make_teacher("Anna", "Tutor")
        self.group = make_group("3r A", tutor=self.tutor)
        self.subject = Subject.objects.create(name="Música")

    def _row_at(self, grid, hour, minute=0):
        return next(row for row in grid["rows"] if row.start_time == t(hour, minute))

    def test_open_slot_has_no_occupied_entry(self):
        grid = build_group_grid(self.group)
        row = self._row_at(grid, 9)
        monday_cell = next(c for c in row.cells if c.weekday == 0)
        self.assertIsNone(monday_cell.occupied_entry)
        self.assertTrue(monday_cell.selectable)

    def test_existing_entry_occupies_every_overlapping_row(self):
        ScheduleEntry.objects.create(
            class_group=self.group, subject=self.subject, teacher=self.tutor,
            weekday=0, start_time=t(9), end_time=t(10),
        )
        grid = build_group_grid(self.group)
        for hour, minute in [(9, 0), (9, 30)]:
            row = self._row_at(grid, hour, minute)
            monday_cell = next(c for c in row.cells if c.weekday == 0)
            self.assertIsNotNone(monday_cell.occupied_entry)
            self.assertFalse(monday_cell.selectable)
        tuesday_cell = next(c for c in self._row_at(grid, 9).cells if c.weekday == 1)
        self.assertIsNone(tuesday_cell.occupied_entry)

    def test_group_hours_label_reflects_scheduled_minutes(self):
        ScheduleEntry.objects.create(
            class_group=self.group, subject=self.subject, teacher=self.tutor,
            weekday=0, start_time=t(9), end_time=t(10),
        )
        grid = build_group_grid(self.group)
        self.assertEqual(grid["group_hours_label"], "1h")

    def test_weekday_labels_cover_monday_to_friday(self):
        grid = build_group_grid(self.group)
        self.assertEqual([value for value, _ in grid["weekday_labels"]], [0, 1, 2, 3, 4])

    def test_lunch_is_a_single_band_row_not_ordinary_slots(self):
        grid = build_group_grid(self.group)
        band_rows = [row for row in grid["rows"] if row.is_band]
        self.assertEqual(len(band_rows), 1)
        self.assertEqual((band_rows[0].start_time, band_rows[0].end_time), (t(13), t(15)))
        # no ordinary row starts within the lunch window
        self.assertFalse(any(t(13) <= row.start_time < t(15) and not row.is_band for row in grid["rows"]))

    def test_occupied_cells_are_colored_by_subject(self):
        other_subject = Subject.objects.create(name="Anglès")
        ScheduleEntry.objects.create(
            class_group=self.group, subject=self.subject, teacher=self.tutor,
            weekday=0, start_time=t(9), end_time=t(9, 30),
        )
        ScheduleEntry.objects.create(
            class_group=self.group, subject=other_subject, teacher=self.tutor,
            weekday=1, start_time=t(9), end_time=t(9, 30),
        )
        grid = build_group_grid(self.group)
        row = self._row_at(grid, 9)
        monday_color = next(c for c in row.cells if c.weekday == 0).color
        tuesday_color = next(c for c in row.cells if c.weekday == 1).color
        self.assertTrue(monday_color)
        self.assertTrue(tuesday_color)
        self.assertNotEqual(monday_color, tuesday_color)


class GroupScheduleViewTests(TestCase):
    def setUp(self):
        self.tutor = make_teacher("Anna", "Tutor")
        self.group = make_group("3r A", tutor=self.tutor)
        self.subject = Subject.objects.create(name="Matemàtiques")
        self.staff_user = User.objects.create_user(username="coord", password="pw", is_staff=True)
        self.plain_user = User.objects.create_user(username="teacher", password="pw")
        Teacher.objects.create(user=self.plain_user, grade_level=Teacher.GradeLevel.PRIMARY)

    def test_requires_staff(self):
        self.client.force_login(self.plain_user)
        response = self.client.get(reverse("group_schedule", args=[self.group.pk]))
        self.assertNotEqual(response.status_code, 200)

    def test_shows_grid_for_staff(self):
        self.client.force_login(self.staff_user)
        response = self.client.get(reverse("group_schedule", args=[self.group.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, str(self.tutor))

    def test_shows_hours_summary_below_the_grid(self):
        ScheduleEntry.objects.create(
            class_group=self.group, subject=self.subject, teacher=self.tutor,
            weekday=0, start_time=t(9), end_time=t(10),
        )
        self.client.force_login(self.staff_user)
        response = self.client.get(reverse("group_schedule", args=[self.group.pk]))
        self.assertContains(response, "Hours per teacher")
        self.assertContains(response, "Hours per class group")
        self.assertContains(response, self.group.name)

    def test_co_teacher_picker_shows_no_rows_by_default(self):
        self.client.force_login(self.staff_user)
        response = self.client.get(reverse("group_schedule", args=[self.group.pk]))
        self.assertNotContains(response, 'name="co_teacher"')
        self.assertContains(response, "Add co-teacher")

    def test_post_redirects_keeping_the_subject_and_teacher_selected(self):
        self.client.force_login(self.staff_user)
        response = self.client.post(
            reverse("group_schedule", args=[self.group.pk]),
            {"subject": self.subject.pk, "teacher": self.tutor.pk, "cell": ["0:09:00"]},
        )
        expected_url = f"{reverse('group_schedule', args=[self.group.pk])}?subject={self.subject.pk}&teacher={self.tutor.pk}"
        self.assertRedirects(response, expected_url)

    def test_post_redirects_keeping_co_teachers_selected_too(self):
        co_teacher_a = make_teacher("Bea", "Specialist")
        co_teacher_b = make_teacher("Carla", "Specialist")
        self.client.force_login(self.staff_user)
        response = self.client.post(
            reverse("group_schedule", args=[self.group.pk]),
            {
                "subject": self.subject.pk, "teacher": self.tutor.pk,
                "co_teacher": [co_teacher_a.pk, co_teacher_b.pk], "cell": ["0:09:00"],
            },
        )
        expected_url = (
            f"{reverse('group_schedule', args=[self.group.pk])}"
            f"?subject={self.subject.pk}&teacher={self.tutor.pk}"
            f"&co_teacher={co_teacher_a.pk}&co_teacher={co_teacher_b.pk}"
        )
        self.assertRedirects(response, expected_url)

    def test_get_with_selection_query_params_preselects_the_form(self):
        co_teacher = make_teacher("Bea", "Specialist")
        self.client.force_login(self.staff_user)
        url = (
            f"{reverse('group_schedule', args=[self.group.pk])}"
            f"?subject={self.subject.pk}&teacher={self.tutor.pk}&co_teacher={co_teacher.pk}"
        )
        response = self.client.get(url)
        self.assertContains(
            response, f'<option value="{self.subject.pk}" selected>{self.subject.name}</option>'
        )
        self.assertContains(response, f'<option value="{self.tutor.pk}" selected>')
        self.assertContains(response, f'<option value="{co_teacher.pk}" selected>')

    def test_post_with_invalid_selection_redirects_without_query_params(self):
        self.client.force_login(self.staff_user)
        response = self.client.post(
            reverse("group_schedule", args=[self.group.pk]), {"cell": ["0:09:00"]},
        )
        self.assertRedirects(response, reverse("group_schedule", args=[self.group.pk]))

    def test_post_with_non_numeric_subject_or_teacher_does_not_crash(self):
        self.client.force_login(self.staff_user)
        response = self.client.post(
            reverse("group_schedule", args=[self.group.pk]),
            {"subject": "abc", "teacher": "xyz", "cell": ["0:09:00"]},
        )
        self.assertRedirects(response, reverse("group_schedule", args=[self.group.pk]))
        self.assertEqual(ScheduleEntry.objects.count(), 0)

    def test_post_with_co_teacher_sets_it_on_the_entry(self):
        co_teacher = make_teacher("Bea", "Specialist")
        self.client.force_login(self.staff_user)
        self.client.post(
            reverse("group_schedule", args=[self.group.pk]),
            {
                "subject": self.subject.pk, "teacher": self.tutor.pk, "co_teacher": co_teacher.pk,
                "cell": ["0:09:00"],
            },
        )
        entry = ScheduleEntry.objects.get()
        self.assertEqual(list(entry.co_teachers.all()), [co_teacher])

    def test_post_with_several_co_teachers_sets_them_all(self):
        co_teacher_a = make_teacher("Bea", "Specialist")
        co_teacher_b = make_teacher("Carla", "Specialist")
        self.client.force_login(self.staff_user)
        self.client.post(
            reverse("group_schedule", args=[self.group.pk]),
            {
                "subject": self.subject.pk, "teacher": self.tutor.pk,
                "co_teacher": [co_teacher_a.pk, co_teacher_b.pk],
                "cell": ["0:09:00"],
            },
        )
        entry = ScheduleEntry.objects.get()
        self.assertEqual(set(entry.co_teachers.all()), {co_teacher_a, co_teacher_b})

    def test_post_without_co_teacher_leaves_it_unset(self):
        self.client.force_login(self.staff_user)
        self.client.post(
            reverse("group_schedule", args=[self.group.pk]),
            {"subject": self.subject.pk, "teacher": self.tutor.pk, "cell": ["0:09:00"]},
        )
        entry = ScheduleEntry.objects.get()
        self.assertEqual(entry.co_teachers.count(), 0)

    def test_post_creates_one_entry_from_two_contiguous_cells(self):
        self.client.force_login(self.staff_user)
        response = self.client.post(
            reverse("group_schedule", args=[self.group.pk]),
            {"subject": self.subject.pk, "teacher": self.tutor.pk, "cell": ["0:09:00", "0:09:30"]},
        )
        expected_url = f"{reverse('group_schedule', args=[self.group.pk])}?subject={self.subject.pk}&teacher={self.tutor.pk}"
        self.assertRedirects(response, expected_url)
        self.assertEqual(ScheduleEntry.objects.count(), 1)
        entry = ScheduleEntry.objects.get()
        self.assertEqual((entry.start_time, entry.end_time), (t(9), t(10)))
        self.assertEqual(entry.teacher, self.tutor)
        self.assertEqual(entry.subject, self.subject)

    def test_post_two_separate_days_creates_two_entries(self):
        self.client.force_login(self.staff_user)
        self.client.post(
            reverse("group_schedule", args=[self.group.pk]),
            {"subject": self.subject.pk, "teacher": self.tutor.pk, "cell": ["0:09:00", "2:11:00"]},
        )
        self.assertEqual(ScheduleEntry.objects.count(), 2)
        self.assertEqual({e.weekday for e in ScheduleEntry.objects.all()}, {0, 2})

    def test_post_rejects_conflicting_cell_server_side(self):
        other_group = make_group("3r B", tutor=make_teacher("Carla", "Tutor"))
        ScheduleEntry.objects.create(
            class_group=other_group, subject=self.subject, teacher=self.tutor,
            weekday=0, start_time=t(9), end_time=t(9, 30),
        )
        self.client.force_login(self.staff_user)
        self.client.post(
            reverse("group_schedule", args=[self.group.pk]),
            {"subject": self.subject.pk, "teacher": self.tutor.pk, "cell": ["0:09:00"]},
        )
        # still just the pre-existing entry in the other group - nothing added to this one
        self.assertEqual(ScheduleEntry.objects.filter(class_group=self.group).count(), 0)

    def test_conflict_error_message_renders_as_danger_not_info(self):
        other_group = make_group("3r B", tutor=make_teacher("Carla", "Tutor"))
        ScheduleEntry.objects.create(
            class_group=other_group, subject=self.subject, teacher=self.tutor,
            weekday=0, start_time=t(9), end_time=t(9, 30),
        )
        self.client.force_login(self.staff_user)
        response = self.client.post(
            reverse("group_schedule", args=[self.group.pk]),
            {"subject": self.subject.pk, "teacher": self.tutor.pk, "cell": ["0:09:00"]},
            follow=True,
        )
        self.assertContains(response, "alert-danger")
        self.assertNotContains(response, "alert-error")

    def test_delete_entry_removes_it(self):
        entry = ScheduleEntry.objects.create(
            class_group=self.group, subject=self.subject, teacher=self.tutor,
            weekday=0, start_time=t(9), end_time=t(10),
        )
        self.client.force_login(self.staff_user)
        response = self.client.post(reverse("delete_schedule_entry", args=[self.group.pk, entry.pk]))
        self.assertRedirects(response, reverse("group_schedule", args=[self.group.pk]))
        self.assertEqual(ScheduleEntry.objects.count(), 0)

    def test_delete_entry_get_does_not_delete(self):
        entry = ScheduleEntry.objects.create(
            class_group=self.group, subject=self.subject, teacher=self.tutor,
            weekday=0, start_time=t(9), end_time=t(10),
        )
        self.client.force_login(self.staff_user)
        self.client.get(reverse("delete_schedule_entry", args=[self.group.pk, entry.pk]))
        self.assertEqual(ScheduleEntry.objects.count(), 1)


class EditEntryViewTests(TestCase):
    def setUp(self):
        self.tutor = make_teacher("Anna", "Tutor")
        self.group = make_group("3r A", tutor=self.tutor)
        self.subject = Subject.objects.create(name="Matemàtiques")
        self.other_subject = Subject.objects.create(name="Anglès")
        self.specialist = make_teacher("Bea", "Specialist")
        self.entry = ScheduleEntry.objects.create(
            class_group=self.group, subject=self.subject, teacher=self.tutor,
            weekday=0, start_time=t(9), end_time=t(10),
        )
        self.staff_user = User.objects.create_user(username="coord_edit", password="pw", is_staff=True)

    def test_requires_staff(self):
        plain_user = User.objects.create_user(username="plain_edit", password="pw")
        self.client.force_login(plain_user)
        response = self.client.get(reverse("edit_schedule_entry", args=[self.group.pk, self.entry.pk]))
        self.assertNotEqual(response.status_code, 200)

    def test_get_shows_form_prefilled(self):
        self.client.force_login(self.staff_user)
        response = self.client.get(reverse("edit_schedule_entry", args=[self.group.pk, self.entry.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, str(self.tutor))

    def test_post_changes_the_teacher(self):
        self.client.force_login(self.staff_user)
        response = self.client.post(
            reverse("edit_schedule_entry", args=[self.group.pk, self.entry.pk]),
            {"subject": self.subject.pk, "teacher": self.specialist.pk},
        )
        self.assertRedirects(response, reverse("group_schedule", args=[self.group.pk]))
        self.entry.refresh_from_db()
        self.assertEqual(self.entry.teacher, self.specialist)

    def test_post_can_add_a_co_teacher(self):
        co_teacher = make_teacher("Carla", "Co")
        self.client.force_login(self.staff_user)
        self.client.post(
            reverse("edit_schedule_entry", args=[self.group.pk, self.entry.pk]),
            {"subject": self.subject.pk, "teacher": self.tutor.pk, "co_teacher": co_teacher.pk},
        )
        self.entry.refresh_from_db()
        self.assertEqual(list(self.entry.co_teachers.all()), [co_teacher])

    def test_post_can_add_several_co_teachers(self):
        co_teacher_a = make_teacher("Carla", "Co")
        co_teacher_b = make_teacher("Dana", "Co")
        self.client.force_login(self.staff_user)
        self.client.post(
            reverse("edit_schedule_entry", args=[self.group.pk, self.entry.pk]),
            {
                "subject": self.subject.pk, "teacher": self.tutor.pk,
                "co_teacher": [co_teacher_a.pk, co_teacher_b.pk],
            },
        )
        self.entry.refresh_from_db()
        self.assertEqual(set(self.entry.co_teachers.all()), {co_teacher_a, co_teacher_b})

    def test_post_can_clear_the_co_teacher(self):
        self.entry.co_teachers.set([self.specialist])
        self.client.force_login(self.staff_user)
        self.client.post(
            reverse("edit_schedule_entry", args=[self.group.pk, self.entry.pk]),
            {"subject": self.subject.pk, "teacher": self.tutor.pk, "co_teacher": ""},
        )
        self.entry.refresh_from_db()
        self.assertEqual(self.entry.co_teachers.count(), 0)

    def test_post_rejects_a_teacher_already_busy_then(self):
        other_group = make_group("3r B", tutor=self.specialist)
        ScheduleEntry.objects.create(
            class_group=other_group, subject=self.other_subject, teacher=self.specialist,
            weekday=0, start_time=t(9), end_time=t(10),
        )
        self.client.force_login(self.staff_user)
        self.client.post(
            reverse("edit_schedule_entry", args=[self.group.pk, self.entry.pk]),
            {"subject": self.subject.pk, "teacher": self.specialist.pk},
        )
        self.entry.refresh_from_db()
        self.assertEqual(self.entry.teacher, self.tutor)  # unchanged


class EditSelectedAndDeleteSelectedViewTests(TestCase):
    """The grid's per-selection edit/delete - can act on part of a longer
    existing entry, not just the whole thing."""

    def setUp(self):
        self.tutor = make_teacher("Anna", "Tutor")
        self.group = make_group("3r A", tutor=self.tutor)
        self.subject = Subject.objects.create(name="Matemàtiques")
        self.other_subject = Subject.objects.create(name="Anglès")
        self.specialist = make_teacher("Bea", "Specialist")
        # a 2-hour entry: 9:00-11:00
        self.entry = ScheduleEntry.objects.create(
            class_group=self.group, subject=self.subject, teacher=self.tutor,
            weekday=0, start_time=t(9), end_time=t(11),
        )
        self.staff_user = User.objects.create_user(username="coord_selected", password="pw", is_staff=True)

    def _occupied_cell(self, weekday, hour, minute=0):
        return f"{weekday}:{hour:02d}:{minute:02d}|{self.entry.pk}"

    def test_edit_selected_updates_only_the_picked_middle_slots(self):
        self.client.force_login(self.staff_user)
        self.client.post(
            reverse("edit_selected_schedule_entries", args=[self.group.pk]),
            {
                "subject": self.other_subject.pk, "teacher": self.specialist.pk,
                "occupied_cell": [self._occupied_cell(0, 9, 30), self._occupied_cell(0, 10)],
            },
        )
        entries = list(ScheduleEntry.objects.filter(class_group=self.group).order_by("start_time"))
        self.assertEqual(len(entries), 3)
        self.assertEqual(
            [(e.start_time, e.end_time, e.subject, e.teacher) for e in entries],
            [
                (t(9), t(9, 30), self.subject, self.tutor),
                (t(9, 30), t(10, 30), self.other_subject, self.specialist),
                (t(10, 30), t(11), self.subject, self.tutor),
            ],
        )

    def test_edit_selected_redirects_keeping_the_subject_and_teacher_selected(self):
        self.client.force_login(self.staff_user)
        response = self.client.post(
            reverse("edit_selected_schedule_entries", args=[self.group.pk]),
            {
                "subject": self.other_subject.pk, "teacher": self.specialist.pk,
                "occupied_cell": [self._occupied_cell(0, 9, 30)],
            },
        )
        expected_url = (
            f"{reverse('group_schedule', args=[self.group.pk])}"
            f"?subject={self.other_subject.pk}&teacher={self.specialist.pk}"
        )
        self.assertRedirects(response, expected_url)

    def test_edit_selected_whole_entry_replaces_it_without_leftovers(self):
        self.client.force_login(self.staff_user)
        self.client.post(
            reverse("edit_selected_schedule_entries", args=[self.group.pk]),
            {
                "subject": self.other_subject.pk, "teacher": self.specialist.pk,
                "occupied_cell": [
                    self._occupied_cell(0, 9), self._occupied_cell(0, 9, 30),
                    self._occupied_cell(0, 10), self._occupied_cell(0, 10, 30),
                ],
            },
        )
        entries = list(ScheduleEntry.objects.filter(class_group=self.group))
        self.assertEqual(len(entries), 1)
        self.assertEqual((entries[0].start_time, entries[0].end_time), (t(9), t(11)))
        self.assertEqual(entries[0].teacher, self.specialist)

    def test_edit_selected_rejects_a_teacher_busy_for_the_new_slot(self):
        other_group = make_group("3r B", tutor=self.specialist)
        ScheduleEntry.objects.create(
            class_group=other_group, subject=self.other_subject, teacher=self.specialist,
            weekday=0, start_time=t(9, 30), end_time=t(10),
        )
        self.client.force_login(self.staff_user)
        self.client.post(
            reverse("edit_selected_schedule_entries", args=[self.group.pk]),
            {
                "subject": self.other_subject.pk, "teacher": self.specialist.pk,
                "occupied_cell": [self._occupied_cell(0, 9, 30)],
            },
        )
        # rolled back - the original 2-hour entry is untouched
        entries = list(ScheduleEntry.objects.filter(class_group=self.group))
        self.assertEqual(len(entries), 1)
        self.assertEqual((entries[0].start_time, entries[0].end_time), (t(9), t(11)))
        self.assertEqual(entries[0].teacher, self.tutor)

    def test_malformed_occupied_cell_is_ignored_not_a_crash(self):
        self.client.force_login(self.staff_user)
        response = self.client.post(
            reverse("edit_selected_schedule_entries", args=[self.group.pk]),
            {
                "subject": self.other_subject.pk, "teacher": self.specialist.pk,
                "occupied_cell": [f"0:99:99|{self.entry.pk}"],
            },
        )
        self.assertEqual(response.status_code, 302)
        entries = list(ScheduleEntry.objects.filter(class_group=self.group))
        self.assertEqual(len(entries), 1)
        self.assertEqual((entries[0].start_time, entries[0].end_time), (t(9), t(11)))

    def test_delete_selected_removes_only_the_picked_slot(self):
        self.client.force_login(self.staff_user)
        self.client.post(
            reverse("delete_selected_schedule_entries", args=[self.group.pk]),
            {"occupied_cell": [self._occupied_cell(0, 10)]},
        )
        entries = list(ScheduleEntry.objects.filter(class_group=self.group).order_by("start_time"))
        self.assertEqual(
            [(e.start_time, e.end_time) for e in entries],
            [(t(9), t(10)), (t(10, 30), t(11))],
        )

    def test_delete_selected_whole_entry_removes_it_entirely(self):
        self.client.force_login(self.staff_user)
        self.client.post(
            reverse("delete_selected_schedule_entries", args=[self.group.pk]),
            {
                "occupied_cell": [
                    self._occupied_cell(0, 9), self._occupied_cell(0, 9, 30),
                    self._occupied_cell(0, 10), self._occupied_cell(0, 10, 30),
                ]
            },
        )
        self.assertEqual(ScheduleEntry.objects.filter(class_group=self.group).count(), 0)

    def test_requires_staff(self):
        plain_user = User.objects.create_user(username="plain_selected", password="pw")
        self.client.force_login(plain_user)
        response = self.client.post(
            reverse("delete_selected_schedule_entries", args=[self.group.pk]),
            {"occupied_cell": [self._occupied_cell(0, 9)]},
        )
        self.assertNotEqual(response.get("Location", ""), reverse("group_schedule", args=[self.group.pk]))
        self.assertEqual(ScheduleEntry.objects.filter(class_group=self.group).count(), 1)


class TeacherCalendarViewTests(TestCase):
    def setUp(self):
        self.tutor = make_teacher("Anna", "Tutor")
        self.group = make_group("3r A", tutor=self.tutor)
        self.subject = Subject.objects.create(name="Matemàtiques")
        ScheduleEntry.objects.create(
            class_group=self.group, subject=self.subject, teacher=self.tutor,
            weekday=0, start_time=t(9), end_time=t(10),
        )
        self.staff_user = User.objects.create_user(username="coord_calendar", password="pw", is_staff=True)

    def test_full_calendar_page_shows_teaching_block_and_hours(self):
        self.client.force_login(self.staff_user)
        response = self.client.get(reverse("teacher_calendar", args=[self.tutor.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Matemàtiques")
        self.assertContains(response, "3r A")
        self.assertContains(response, "1h")
        self.assertContains(response, "Hours per teacher")
        self.assertContains(response, "Hours per class group")

    def test_switcher_order_matches_the_group_schedule_page(self):
        """Both pages must list the group switcher before the teacher
        switcher - if the order differed between pages, picking one would
        make the pair appear to swap places."""
        self.client.force_login(self.staff_user)
        response = self.client.get(reverse("teacher_calendar", args=[self.tutor.pk]))
        html = response.content.decode()
        # `<option value="...">`, not just any occurrence - base.html's language
        # form also stashes the current URL in a hidden `next` field earlier
        # in the page, which isn't part of the switcher.
        group_url_index = html.index(f'<option value="{reverse("group_schedule", args=[self.group.pk])}"')
        teacher_url_index = html.index(f'<option value="{reverse("teacher_calendar", args=[self.tutor.pk])}"')
        self.assertLess(group_url_index, teacher_url_index)

    def test_requires_staff(self):
        plain_user = User.objects.create_user(username="plain_calendar", password="pw")
        self.client.force_login(plain_user)
        response = self.client.get(reverse("teacher_calendar", args=[self.tutor.pk]))
        self.assertNotEqual(response.status_code, 200)


class GroupListAndTeacherListViewTests(TestCase):
    def setUp(self):
        self.tutor = make_teacher("Anna", "Tutor")
        self.group = make_group("3r A", tutor=self.tutor)
        self.staff_user = User.objects.create_user(username="coord_lists", password="pw", is_staff=True)

    def test_group_list_shows_groups_and_links_to_their_timetable(self):
        self.client.force_login(self.staff_user)
        response = self.client.get(reverse("group_list"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "3r A")
        self.assertContains(response, reverse("group_schedule", args=[self.group.pk]))

    def test_teacher_list_shows_active_teachers_only(self):
        make_teacher("Bea", "Inactive", active=False)
        self.client.force_login(self.staff_user)
        response = self.client.get(reverse("teacher_list"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, str(self.tutor))
        self.assertNotContains(response, "Inactive")


class ClassGroupAdminTests(TestCase):
    """The admin's tutor picker (TutorSlotsWidget in schedule/admin.py)
    renders one <select> per slot instead of a single multi-select box, but
    must still submit/round-trip like an ordinary tutors ManyToManyField."""

    def setUp(self):
        self.admin_user = User.objects.create_user(
            username="group_admin", password="pw", is_staff=True, is_superuser=True
        )
        self.client.force_login(self.admin_user)

    def test_add_form_renders_one_select_per_tutor_slot(self):
        response = self.client.get(reverse("admin:schedule_classgroup_add"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'name="tutors"', count=6)  # MAX_TUTORS slots

    def test_posting_two_tutors_sets_them_both(self):
        tutor_a = make_teacher("Anna", "Tutor")
        tutor_b = make_teacher("Bea", "Tutor")
        response = self.client.post(
            reverse("admin:schedule_classgroup_add"),
            {
                "name": "3r A", "grade_level": Teacher.GradeLevel.PRIMARY, "tutor_count": 2,
                "tutors": [tutor_a.pk, tutor_b.pk],
                **_inline_management_form_data(),
            },
        )
        self.assertEqual(response.status_code, 302)
        group = ClassGroup.objects.get(name="3r A")
        self.assertEqual(set(group.tutors.all()), {tutor_a, tutor_b})

    def test_leaving_extra_slots_blank_does_not_error(self):
        tutor = make_teacher("Anna", "Tutor")
        response = self.client.post(
            reverse("admin:schedule_classgroup_add"),
            {
                "name": "3r A", "grade_level": Teacher.GradeLevel.PRIMARY, "tutor_count": 1,
                "tutors": [tutor.pk, "", "", "", "", ""],
                **_inline_management_form_data(),
            },
        )
        self.assertEqual(response.status_code, 302)
        group = ClassGroup.objects.get(name="3r A")
        self.assertEqual(list(group.tutors.all()), [tutor])

    def test_deactivated_tutor_is_kept_when_saving_an_unrelated_change(self):
        tutor = make_teacher("Anna", "Tutor")
        group = make_group("3r A", tutor=tutor)
        tutor.active = False
        tutor.save()
        response = self.client.post(
            reverse("admin:schedule_classgroup_change", args=[group.pk]),
            {
                "name": "3r A (renamed)", "grade_level": Teacher.GradeLevel.PRIMARY, "tutor_count": 1,
                "tutors": [tutor.pk],
                **_inline_management_form_data(),
            },
        )
        self.assertEqual(response.status_code, 302)
        group.refresh_from_db()
        self.assertEqual(list(group.tutors.all()), [tutor])

    def test_deactivated_tutor_is_still_offered_as_a_choice(self):
        tutor = make_teacher("Anna", "Tutor")
        group = make_group("3r A", tutor=tutor)
        tutor.active = False
        tutor.save()
        response = self.client.get(reverse("admin:schedule_classgroup_change", args=[group.pk]))
        self.assertContains(response, f'<option value="{tutor.pk}" selected>')
        self.assertContains(response, "inactive")


class ScheduleEntryInlineValidationTests(TestCase):
    """Co-teachers picked via the ClassGroup admin's inline formset must be
    validated for conflicts too, not just the main teacher - see
    ScheduleEntryInlineForm._post_clean in schedule/admin.py."""

    def setUp(self):
        self.admin_user = User.objects.create_user(
            username="entry_admin", password="pw", is_staff=True, is_superuser=True
        )
        self.client.force_login(self.admin_user)
        self.tutor = make_teacher("Anna", "Tutor")
        self.busy_co_teacher = make_teacher("Bea", "Specialist")
        self.subject = Subject.objects.create(name="Matemàtiques")
        other_group = make_group("3r B", tutor=make_teacher("Carla", "Tutor"))
        ScheduleEntry.objects.create(
            class_group=other_group, subject=self.subject, teacher=self.busy_co_teacher,
            weekday=0, start_time=t(9), end_time=t(9, 30),
        )

    def _post_data(self, co_teacher_pk):
        return {
            "name": "3r A", "grade_level": Teacher.GradeLevel.PRIMARY, "tutor_count": 1,
            "tutors": [self.tutor.pk],
            "schedule_entries-TOTAL_FORMS": "1",
            "schedule_entries-INITIAL_FORMS": "0",
            "schedule_entries-MIN_NUM_FORMS": "0",
            "schedule_entries-MAX_NUM_FORMS": "1000",
            "schedule_entries-0-id": "",
            "schedule_entries-0-weekday": "0",
            "schedule_entries-0-start_time": "09:00:00",
            "schedule_entries-0-end_time": "09:30:00",
            "schedule_entries-0-subject": self.subject.pk,
            "schedule_entries-0-teacher": self.tutor.pk,
            "schedule_entries-0-co_teachers": [co_teacher_pk],
        }

    def test_conflicting_co_teacher_is_rejected(self):
        response = self.client.post(reverse("admin:schedule_classgroup_add"), self._post_data(self.busy_co_teacher.pk))
        self.assertEqual(response.status_code, 200)  # form re-rendered with an error, not redirected
        self.assertFalse(ClassGroup.objects.filter(name="3r A").exists())

    def test_free_co_teacher_is_accepted(self):
        free_co_teacher = make_teacher("Dana", "Other")
        response = self.client.post(reverse("admin:schedule_classgroup_add"), self._post_data(free_co_teacher.pk))
        self.assertEqual(response.status_code, 302)
        group = ClassGroup.objects.get(name="3r A")
        entry = group.schedule_entries.get()
        self.assertEqual(list(entry.co_teachers.all()), [free_co_teacher])


def _inline_management_form_data() -> dict:
    """The empty ScheduleEntryInline management form data the ClassGroup
    admin add/change view requires to validate, for tests posting to it
    directly rather than through a rendered form."""
    return {
        "schedule_entries-TOTAL_FORMS": "0",
        "schedule_entries-INITIAL_FORMS": "0",
        "schedule_entries-MIN_NUM_FORMS": "0",
        "schedule_entries-MAX_NUM_FORMS": "1000",
    }
