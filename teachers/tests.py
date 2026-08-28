import datetime

from django.contrib.auth.models import Permission, User
from django.contrib.contenttypes.models import ContentType
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse

from .calendar import LANE_MIN_WIDTH, build_week_calendar
from .importer import import_teachers_from_csv
from .models import Teacher, WeeklyNonTeachingHours


def csv_file(content: str) -> SimpleUploadedFile:
    return SimpleUploadedFile("teachers.csv", content.encode("utf-8"), content_type="text/csv")


def make_teacher(first_name: str, last_name: str, active: bool = True) -> Teacher:
    user = User.objects.create_user(
        username=f"{first_name}.{last_name}".lower(), first_name=first_name, last_name=last_name
    )
    return Teacher.objects.create(user=user, grade_level=Teacher.GradeLevel.PRIMARY, active=active)


def add_hours(teacher: Teacher, weekday: int, start: str, end: str, is_paperwork: bool = False):
    start_hour, start_minute = (int(part) for part in start.split(":"))
    end_hour, end_minute = (int(part) for part in end.split(":"))
    return WeeklyNonTeachingHours.objects.create(
        teacher=teacher,
        weekday=weekday,
        start_time=datetime.time(start_hour, start_minute),
        end_time=datetime.time(end_hour, end_minute),
        is_paperwork=is_paperwork,
    )


@override_settings(LANGUAGE_CODE="en")
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


MONDAY = WeeklyNonTeachingHours.Weekday.MONDAY


class BuildWeekCalendarTests(TestCase):
    def _monday(self, calendar):
        return next(day for day in calendar["days"] if day["value"] == MONDAY)

    def test_every_block_carries_its_teacher_name(self):
        add_hours(make_teacher("Jane", "Doe"), MONDAY, "08:00", "09:30")
        add_hours(make_teacher("John", "Smith"), MONDAY, "08:00", "09:00")

        blocks = self._monday(build_week_calendar())["blocks"]

        self.assertEqual({"Jane Doe", "John Smith"}, {block.teacher_name for block in blocks})

    def test_day_widens_with_overlapping_lanes_so_names_stay_readable(self):
        for index in range(4):
            add_hours(make_teacher("Teacher", f"Number{index}"), MONDAY, "08:00", "09:00")

        calendar = build_week_calendar()
        monday = self._monday(calendar)
        tuesday = next(day for day in calendar["days"] if day["value"] == WeeklyNonTeachingHours.Weekday.TUESDAY)

        self.assertEqual(monday["min_width"], 4 * LANE_MIN_WIDTH)
        self.assertEqual(tuesday["min_width"], LANE_MIN_WIDTH)

    def test_short_block_puts_name_and_time_on_one_line(self):
        teacher = make_teacher("Jane", "Doe")
        add_hours(teacher, MONDAY, "08:00", "08:15")
        add_hours(teacher, MONDAY, "09:00", "11:00")

        short, long = self._monday(build_week_calendar())["blocks"]

        self.assertTrue(short.inline)
        self.assertFalse(short.show_time)  # only room for the name
        self.assertFalse(long.inline)
        self.assertTrue(long.show_time)
        self.assertTrue(long.wrap)

    def test_each_teacher_gets_a_distinct_colour_listed_in_the_legend(self):
        add_hours(make_teacher("Zoe", "Alba"), MONDAY, "08:00", "09:00")
        add_hours(make_teacher("Adam", "Bell"), MONDAY, "10:00", "11:00")

        calendar = build_week_calendar()
        legend = calendar["teacher_legend"]

        self.assertEqual(["Adam Bell", "Zoe Alba"], [entry["name"] for entry in legend])
        self.assertEqual(2, len({entry["color"] for entry in legend}))
        colors_by_name = {entry["name"]: entry["color"] for entry in legend}
        for block in self._monday(calendar)["blocks"]:
            self.assertEqual(colors_by_name[block.teacher_name], block.color)

    def test_inactive_teachers_are_left_out(self):
        add_hours(make_teacher("Gone", "Away", active=False), MONDAY, "08:00", "09:00")

        calendar = build_week_calendar()

        self.assertFalse(calendar["has_entries"])
        self.assertEqual([], calendar["teacher_legend"])
        self.assertEqual([], self._monday(calendar)["blocks"])


class WeeklyCalendarAdminViewTests(TestCase):
    def setUp(self):
        staff = User.objects.create_user(username="staff_calendar", password="pw", is_staff=True)
        staff.user_permissions.set(Permission.objects.filter(content_type=ContentType.objects.get_for_model(Teacher)))
        self.client.force_login(staff)
        add_hours(make_teacher("Jane", "Doe"), MONDAY, "08:00", "09:30")

    def test_style_values_use_a_decimal_point_regardless_of_active_language(self):
        # Django localizes {{ float }} output per the active language, and
        # Catalan (like most of Europe) uses a comma for decimals - which
        # would silently corrupt every inline "top/height/left/width" CSS
        # value on this page (e.g. "12,5%" instead of "12.5%") and collapse
        # the whole calendar to nothing. {% localize off %} in the template
        # is what prevents this.
        self.client.cookies["django_language"] = "ca"

        response = self.client.get(reverse("admin:teachers_teacher_weekly_calendar"))

        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertIn("dilluns", html)  # confirms Catalan really is active
        self.assertNotRegex(html, r"\d,\d")
