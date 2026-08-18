import datetime

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase

from .importer import import_teachers_from_csv
from .models import Teacher, WeeklyNonTeachingHours


def csv_file(content: str) -> SimpleUploadedFile:
    return SimpleUploadedFile("teachers.csv", content.encode("utf-8"), content_type="text/csv")


class ImportTeachersFromCsvTests(TestCase):
    def test_creates_teacher_with_multiple_non_teaching_blocks(self):
        content = (
            "first_name,last_name,email,grade_level,weekday,start_time,end_time\n"
            "Jane,Doe,jane@example.edu,primary,Monday,08:00,09:30\n"
            "Jane,Doe,jane@example.edu,primary,Wednesday,08:00,12:00\n"
        )
        result = import_teachers_from_csv(csv_file(content))

        self.assertTrue(result.ok)
        self.assertEqual(result.hours_created, 2)
        self.assertEqual({"jane.doe"}, result.teachers_touched)
        self.assertEqual(len(result.created_users), 1)
        username, password = result.created_users[0]
        self.assertEqual(username, "jane.doe")
        self.assertTrue(len(password) >= 8)

        teacher = Teacher.objects.get(user__username="jane.doe")
        self.assertEqual(teacher.grade_level, Teacher.GradeLevel.PRIMARY)
        self.assertEqual(teacher.non_teaching_hours.count(), 2)
        self.assertTrue(teacher.user.check_password(password))

    def test_teacher_without_hours_row_is_still_created(self):
        content = (
            "first_name,last_name,email,grade_level,weekday,start_time,end_time\n"
            "No,Hours,,pre_primary,,,\n"
        )
        result = import_teachers_from_csv(csv_file(content))

        self.assertTrue(result.ok)
        teacher = Teacher.objects.get(user__username="no.hours")
        self.assertEqual(teacher.non_teaching_hours.count(), 0)

    def test_missing_required_columns_reported_and_nothing_saved(self):
        content = "first_name,last_name\nJane,Doe\n"
        result = import_teachers_from_csv(csv_file(content))

        self.assertFalse(result.ok)
        self.assertEqual(result.errors[0].line_number, 0)
        self.assertEqual(User.objects.count(), 0)

    def test_grade_level_conflict_rolls_back_entire_import(self):
        content = (
            "first_name,last_name,email,grade_level,weekday,start_time,end_time\n"
            "Jane,Doe,,primary,Monday,08:00,09:30\n"
            "Jane,Doe,,pre_primary,Tuesday,08:00,09:30\n"
        )
        result = import_teachers_from_csv(csv_file(content))

        self.assertFalse(result.ok)
        self.assertIn("conflicts", result.errors[0].message)
        self.assertEqual(User.objects.count(), 0)
        self.assertEqual(Teacher.objects.count(), 0)

    def test_invalid_weekday_and_time_reported_with_line_numbers(self):
        content = (
            "first_name,last_name,email,grade_level,weekday,start_time,end_time\n"
            "Jane,Doe,,primary,Funday,08:00,09:30\n"
            "John,Smith,,primary,Monday,not-a-time,09:30\n"
        )
        result = import_teachers_from_csv(csv_file(content))

        self.assertFalse(result.ok)
        self.assertEqual(len(result.errors), 2)
        self.assertEqual(result.errors[0].line_number, 2)
        self.assertEqual(result.errors[1].line_number, 3)

    def test_reimporting_same_file_is_idempotent(self):
        content = (
            "first_name,last_name,email,grade_level,weekday,start_time,end_time\n"
            "Jane,Doe,,primary,Monday,08:00,09:30\n"
        )
        first = import_teachers_from_csv(csv_file(content))
        second = import_teachers_from_csv(csv_file(content))

        self.assertTrue(first.ok and second.ok)
        self.assertEqual(len(first.created_users), 1)
        self.assertEqual(len(second.created_users), 0)
        self.assertEqual(first.hours_created, 1)
        self.assertEqual(second.hours_created, 0)
        self.assertEqual(WeeklyNonTeachingHours.objects.count(), 1)

    def test_existing_user_is_not_renamed(self):
        # "Jane" + "Doe" derives to the same "jane.doe" username as the
        # already-existing account below, so the import should reuse it
        # rather than creating a new one or renaming it.
        existing = User.objects.create_user(username="jane.doe", first_name="Original", last_name="Name")
        content = (
            "first_name,last_name,email,grade_level,weekday,start_time,end_time\n"
            "Jane,Doe,,primary,Monday,08:00,09:30\n"
        )
        result = import_teachers_from_csv(csv_file(content))

        self.assertTrue(result.ok)
        self.assertEqual(len(result.created_users), 0)
        existing.refresh_from_db()
        self.assertEqual(existing.first_name, "Original")
        self.assertTrue(Teacher.objects.filter(user=existing).exists())

    def test_is_paperwork_column_sets_flag_per_block(self):
        content = (
            "first_name,last_name,email,grade_level,weekday,start_time,end_time,is_paperwork\n"
            "Jane,Doe,,primary,Monday,08:00,09:30,yes\n"
            "Jane,Doe,,primary,Monday,13:00,16:00,no\n"
        )
        result = import_teachers_from_csv(csv_file(content))

        self.assertTrue(result.ok)
        teacher = Teacher.objects.get(user__username="jane.doe")
        paperwork_block = teacher.non_teaching_hours.get(start_time=datetime.time(8, 0))
        free_block = teacher.non_teaching_hours.get(start_time=datetime.time(13, 0))
        self.assertTrue(paperwork_block.is_paperwork)
        self.assertFalse(free_block.is_paperwork)

    def test_missing_is_paperwork_column_defaults_to_false(self):
        content = (
            "first_name,last_name,email,grade_level,weekday,start_time,end_time\n"
            "Jane,Doe,,primary,Monday,08:00,09:30\n"
        )
        result = import_teachers_from_csv(csv_file(content))

        self.assertTrue(result.ok)
        block = WeeklyNonTeachingHours.objects.get(teacher__user__username="jane.doe")
        self.assertFalse(block.is_paperwork)

    def test_invalid_is_paperwork_value_reported(self):
        content = (
            "first_name,last_name,email,grade_level,weekday,start_time,end_time,is_paperwork\n"
            "Jane,Doe,,primary,Monday,08:00,09:30,maybe\n"
        )
        result = import_teachers_from_csv(csv_file(content))

        self.assertFalse(result.ok)
        self.assertIn("is_paperwork", result.errors[0].message)

    def test_conflicting_is_paperwork_for_same_block_rolls_back(self):
        WeeklyNonTeachingHours.objects.create(
            teacher=Teacher.objects.create(
                user=User.objects.create_user(username="jane.doe"),
                grade_level=Teacher.GradeLevel.PRIMARY,
            ),
            weekday=WeeklyNonTeachingHours.Weekday.MONDAY,
            start_time=datetime.time(8, 0),
            end_time=datetime.time(9, 30),
            is_paperwork=False,
        )
        content = (
            "first_name,last_name,email,grade_level,weekday,start_time,end_time,is_paperwork\n"
            "Jane,Doe,,primary,Monday,08:00,09:30,yes\n"
        )
        result = import_teachers_from_csv(csv_file(content))

        self.assertFalse(result.ok)
        self.assertIn("conflicts", result.errors[0].message)

    def test_username_derived_from_name_lowercase_dot_separated(self):
        content = (
            "first_name,last_name,email,grade_level,weekday,start_time,end_time\n"
            "Mary Jane,Van Der Berg,,primary,,,\n"
        )
        result = import_teachers_from_csv(csv_file(content))

        self.assertTrue(result.ok)
        username, _password = result.created_users[0]
        self.assertEqual(username, "maryjane.vanderberg")
