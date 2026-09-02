import csv
import datetime
import io

from django.contrib.auth.models import Permission, User
from django.contrib.contenttypes.models import ContentType
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse

from django.core.exceptions import ValidationError

from .calendar import LANE_MIN_WIDTH, build_week_calendar
from .forms import NonTeachingHoursForm
from .importer import export_teachers_to_csv, import_teachers_from_csv
from .models import NonTeachingHoursKind, Teacher, WeeklyNonTeachingHours


def csv_file(content: str) -> SimpleUploadedFile:
    return SimpleUploadedFile("teachers.csv", content.encode("utf-8"), content_type="text/csv")


def make_teacher(first_name: str, last_name: str, active: bool = True) -> Teacher:
    user = User.objects.create_user(
        username=f"{first_name}.{last_name}".lower(), first_name=first_name, last_name=last_name
    )
    return Teacher.objects.create(user=user, grade_level=Teacher.GradeLevel.PRIMARY, active=active)


def add_hours(
    teacher: Teacher, weekday: int, start: str, end: str,
    kind: str = NonTeachingHoursKind.FREE, head: Teacher | None = None,
):
    start_hour, start_minute = (int(part) for part in start.split(":"))
    end_hour, end_minute = (int(part) for part in end.split(":"))
    if kind == NonTeachingHoursKind.CO_TEACHING and head is None:
        head = make_teacher("head", f"of_{teacher.user.username}")
    return WeeklyNonTeachingHours.objects.create(
        teacher=teacher,
        weekday=weekday,
        start_time=datetime.time(start_hour, start_minute),
        end_time=datetime.time(end_hour, end_minute),
        kind=kind,
        head=head,
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

    def test_type_column_sets_kind_per_block(self):
        content = (
            "first_name,last_name,email,grade_level,weekday,start_time,end_time,type,co_teaching head\n"
            "Jane,Doe,,primary,Monday,08:00,09:30,paperwork,\n"
            "Jane,Doe,,primary,Monday,10:00,12:00,co_teaching,John Smith\n"
            "Jane,Doe,,primary,Monday,13:00,16:00,free,\n"
            "John,Smith,,primary,,,,\n"
        )
        result = import_teachers_from_csv(csv_file(content))

        self.assertTrue(result.ok, result.errors)
        teacher = Teacher.objects.get(user__username="jane.doe")
        self.assertEqual(
            teacher.non_teaching_hours.get(start_time=datetime.time(10, 0)).head.user.get_full_name(),
            "John Smith",
        )
        self.assertEqual(
            teacher.non_teaching_hours.get(start_time=datetime.time(8, 0)).kind,
            NonTeachingHoursKind.PAPERWORK,
        )
        self.assertEqual(
            teacher.non_teaching_hours.get(start_time=datetime.time(10, 0)).kind,
            NonTeachingHoursKind.CO_TEACHING,
        )
        self.assertEqual(
            teacher.non_teaching_hours.get(start_time=datetime.time(13, 0)).kind,
            NonTeachingHoursKind.FREE,
        )

    def test_non_utf8_file_is_decoded_not_crashed(self):
        # Excel on Windows saves accented names as cp1252 - "Monclús" there is
        # ...0xfa, which is not valid UTF-8.
        content = (
            "first_name,last_name,email,grade_level,weekday,start_time,end_time,type\n"
            "Jana,Monclús,jana@example.edu,pre_primary,Monday,09:00,10:00,free\n"
        )
        upload = SimpleUploadedFile("teachers.csv", content.encode("cp1252"), content_type="text/csv")

        result = import_teachers_from_csv(upload)

        self.assertTrue(result.ok)
        self.assertTrue(Teacher.objects.filter(user__last_name="Monclús").exists())

    def test_type_accepts_catalan_aliases(self):
        content = (
            "first_name,last_name,email,grade_level,weekday,start_time,end_time,type\n"
            "Jane,Doe,,primary,Monday,08:00,09:30,càrrec\n"
            "Jane,Doe,,primary,Monday,10:00,12:00,Escolta'm\n"
        )
        result = import_teachers_from_csv(csv_file(content))

        self.assertTrue(result.ok)
        teacher = Teacher.objects.get(user__username="jane.doe")
        self.assertEqual(
            teacher.non_teaching_hours.get(start_time=datetime.time(8, 0)).kind,
            NonTeachingHoursKind.PAPERWORK,
        )
        self.assertEqual(
            teacher.non_teaching_hours.get(start_time=datetime.time(10, 0)).kind,
            NonTeachingHoursKind.ESCOLTAM,
        )

    def test_missing_type_column_defaults_to_free(self):
        content = (
            "first_name,last_name,email,grade_level,weekday,start_time,end_time\n"
            "Jane,Doe,,primary,Monday,08:00,09:30\n"
        )
        result = import_teachers_from_csv(csv_file(content))

        self.assertTrue(result.ok)
        block = WeeklyNonTeachingHours.objects.get(teacher__user__username="jane.doe")
        self.assertEqual(block.kind, NonTeachingHoursKind.FREE)

    def test_invalid_type_value_reported(self):
        content = (
            "first_name,last_name,email,grade_level,weekday,start_time,end_time,type\n"
            "Jane,Doe,,primary,Monday,08:00,09:30,maybe\n"
        )
        result = import_teachers_from_csv(csv_file(content))

        self.assertFalse(result.ok)
        self.assertIn("type", result.errors[0].message)

    def test_conflicting_type_for_same_block_rolls_back(self):
        WeeklyNonTeachingHours.objects.create(
            teacher=Teacher.objects.create(
                user=User.objects.create_user(username="jane.doe"),
                grade_level=Teacher.GradeLevel.PRIMARY,
            ),
            weekday=WeeklyNonTeachingHours.Weekday.MONDAY,
            start_time=datetime.time(8, 0),
            end_time=datetime.time(9, 30),
            kind=NonTeachingHoursKind.FREE,
        )
        content = (
            "first_name,last_name,email,grade_level,weekday,start_time,end_time,type\n"
            "Jane,Doe,,primary,Monday,08:00,09:30,paperwork\n"
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


@override_settings(LANGUAGE_CODE="en")
class CoTeachingHeadImportTests(TestCase):
    HEADER = "first_name,last_name,email,grade_level,weekday,start_time,end_time,type,co_teaching head\n"

    def test_co_teaching_row_without_a_head_is_rejected(self):
        content = self.HEADER + "Jane,Doe,,primary,Monday,10:00,12:00,co_teaching,\n"

        result = import_teachers_from_csv(csv_file(content))

        self.assertFalse(result.ok)
        self.assertEqual(result.errors[0].line_number, 2)
        self.assertFalse(WeeklyNonTeachingHours.objects.exists())

    def test_head_resolves_to_an_existing_teacher(self):
        make_teacher("John", "Smith")
        content = self.HEADER + "Jane,Doe,,primary,Monday,10:00,12:00,co_teaching,John Smith\n"

        result = import_teachers_from_csv(csv_file(content))

        self.assertTrue(result.ok, result.errors)
        block = WeeklyNonTeachingHours.objects.get(kind=NonTeachingHoursKind.CO_TEACHING)
        self.assertEqual(block.head.user.username, "john.smith")

    def test_head_can_be_defined_further_down_the_same_file(self):
        content = self.HEADER + (
            "Jane,Doe,,primary,Monday,10:00,12:00,co_teaching,John Smith\n"
            "John,Smith,,primary,,,,\n"
        )

        result = import_teachers_from_csv(csv_file(content))

        self.assertTrue(result.ok, result.errors)
        self.assertEqual(
            WeeklyNonTeachingHours.objects.get().head.user.username, "john.smith"
        )

    def test_unknown_head_name_rolls_the_whole_import_back(self):
        content = self.HEADER + (
            "Jane,Doe,,primary,Monday,08:00,09:00,free,\n"
            "Jane,Doe,,primary,Monday,10:00,12:00,co_teaching,Nobody Here\n"
        )

        result = import_teachers_from_csv(csv_file(content))

        self.assertFalse(result.ok)
        self.assertIn("Nobody Here", result.errors[0].message)
        self.assertFalse(Teacher.objects.exists())

    def test_a_teacher_cannot_be_their_own_head(self):
        content = self.HEADER + "Jane,Doe,,primary,Monday,10:00,12:00,co_teaching,Jane Doe\n"

        result = import_teachers_from_csv(csv_file(content))

        self.assertFalse(result.ok)
        self.assertFalse(WeeklyNonTeachingHours.objects.exists())

    def test_head_on_a_non_co_teaching_row_is_rejected(self):
        make_teacher("John", "Smith")
        content = self.HEADER + "Jane,Doe,,primary,Monday,10:00,12:00,free,John Smith\n"

        result = import_teachers_from_csv(csv_file(content))

        self.assertFalse(result.ok)

    def test_reimporting_the_same_file_keeps_the_head_and_adds_nothing(self):
        make_teacher("John", "Smith")
        content = self.HEADER + "Jane,Doe,,primary,Monday,10:00,12:00,co_teaching,John Smith\n"
        import_teachers_from_csv(csv_file(content))

        second = import_teachers_from_csv(csv_file(content))

        self.assertTrue(second.ok, second.errors)
        self.assertEqual(second.hours_created, 0)
        self.assertEqual(
            WeeklyNonTeachingHours.objects.get().head.user.username, "john.smith"
        )


@override_settings(LANGUAGE_CODE="en")
class CoTeachingHeadModelAndFormTests(TestCase):
    def _block(self, teacher, kind, head):
        return WeeklyNonTeachingHours(
            teacher=teacher, weekday=MONDAY,
            start_time=datetime.time(10, 0), end_time=datetime.time(11, 0),
            kind=kind, head=head,
        )

    def test_model_clean_requires_a_head_for_co_teaching(self):
        jane = make_teacher("Jane", "Doe")
        with self.assertRaises(ValidationError):
            self._block(jane, NonTeachingHoursKind.CO_TEACHING, None).clean()

    def test_model_clean_drops_a_stray_head_on_non_co_teaching(self):
        jane, john = make_teacher("Jane", "Doe"), make_teacher("John", "Smith")
        block = self._block(jane, NonTeachingHoursKind.FREE, john)
        block.clean()
        self.assertIsNone(block.head)

    def test_model_clean_rejects_being_your_own_head(self):
        jane = make_teacher("Jane", "Doe")
        with self.assertRaises(ValidationError):
            self._block(jane, NonTeachingHoursKind.CO_TEACHING, jane).clean()

    def test_schedule_form_head_choices_exclude_self_and_inactive(self):
        jane = make_teacher("Jane", "Doe")
        john = make_teacher("John", "Smith")
        make_teacher("Gone", "Away", active=False)
        form = NonTeachingHoursForm(owner=jane)
        self.assertEqual(list(form.fields["head"].queryset), [john])

    def test_schedule_form_flags_a_co_teaching_row_with_no_head(self):
        jane = make_teacher("Jane", "Doe")
        form = NonTeachingHoursForm(
            owner=jane,
            data={
                "weekday": MONDAY, "start_time": "10:00", "end_time": "11:00",
                "kind": NonTeachingHoursKind.CO_TEACHING, "head": "",
            },
        )
        self.assertFalse(form.is_valid())
        self.assertIn("head", form.errors)


@override_settings(LANGUAGE_CODE="en")
class ExportTeachersToCsvTests(TestCase):
    def test_blocks_are_written_one_row_each_in_the_import_format(self):
        jane = make_teacher("Jane", "Doe")
        jane.user.email = "jane@example.edu"
        jane.user.save()
        add_hours(jane, WeeklyNonTeachingHours.Weekday.MONDAY, "08:00", "09:30")
        add_hours(
            jane,
            WeeklyNonTeachingHours.Weekday.WEDNESDAY,
            "10:00",
            "12:00",
            kind=NonTeachingHoursKind.PAPERWORK,
        )

        rows = list(csv.DictReader(io.StringIO(export_teachers_to_csv())))

        self.assertEqual(
            [(r["weekday"], r["start_time"], r["end_time"], r["type"]) for r in rows],
            [("Monday", "08:00", "09:30", "free"), ("Wednesday", "10:00", "12:00", "paperwork")],
        )
        self.assertEqual(rows[0]["first_name"], "Jane")
        self.assertEqual(rows[0]["email"], "jane@example.edu")

    def test_teacher_without_hours_gets_a_single_blank_row(self):
        make_teacher("Ann", "Lee")

        rows = list(csv.DictReader(io.StringIO(export_teachers_to_csv())))

        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["weekday"], rows[0]["start_time"], rows[0]["type"]), ("", "", ""))

    def test_inactive_teachers_are_left_out(self):
        add_hours(make_teacher("Gone", "Away", active=False), WeeklyNonTeachingHours.Weekday.MONDAY, "08:00", "09:00")

        self.assertEqual(list(csv.DictReader(io.StringIO(export_teachers_to_csv()))), [])

    def test_co_teaching_block_carries_the_head_name(self):
        jane = make_teacher("Jane", "Doe")
        john = make_teacher("John", "Smith")
        add_hours(jane, WeeklyNonTeachingHours.Weekday.MONDAY, "10:00", "12:00", kind=NonTeachingHoursKind.CO_TEACHING, head=john)

        rows = {r["type"]: r for r in csv.DictReader(io.StringIO(export_teachers_to_csv()))}

        self.assertEqual(rows["co_teaching"]["co_teaching head"], "John Smith")
        self.assertEqual(rows[""]["co_teaching head"], "")  # John's blank row

    def test_export_round_trips_through_the_importer(self):
        jane = make_teacher("Jane", "Doe")
        john = make_teacher("John", "Smith")
        add_hours(jane, WeeklyNonTeachingHours.Weekday.MONDAY, "08:00", "09:30")
        add_hours(jane, WeeklyNonTeachingHours.Weekday.FRIDAY, "13:00", "16:00", kind=NonTeachingHoursKind.CO_TEACHING, head=john)

        result = import_teachers_from_csv(csv_file(export_teachers_to_csv()))

        self.assertTrue(result.ok, result.errors)
        self.assertEqual(result.hours_created, 0)  # everything already matches
        self.assertEqual(
            WeeklyNonTeachingHours.objects.get(kind=NonTeachingHoursKind.CO_TEACHING).head, john
        )


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

    def test_calendar_page_links_to_the_csv_export(self):
        response = self.client.get(reverse("admin:teachers_teacher_weekly_calendar"))

        self.assertContains(response, reverse("admin:teachers_teacher_export_csv"))

    def test_export_endpoint_returns_a_csv_attachment(self):
        response = self.client.get(reverse("admin:teachers_teacher_export_csv"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/csv")
        self.assertIn("attachment; filename=", response["Content-Disposition"])
        self.assertIn("Jane,Doe", response.content.decode())

    def test_export_endpoint_needs_view_permission(self):
        self.client.force_login(User.objects.create_user(username="nobody", password="pw", is_staff=True))

        response = self.client.get(reverse("admin:teachers_teacher_export_csv"))

        self.assertEqual(response.status_code, 403)


class EditScheduleAccessTests(TestCase):
    """Only admin (staff) users may edit weekly schedules."""

    def test_non_staff_teacher_gets_403_opening_the_editor(self):
        teacher = make_teacher("Reg", "User")
        self.client.force_login(teacher.user)

        self.assertEqual(self.client.get(reverse("edit_schedule")).status_code, 403)

    def test_non_staff_teacher_cannot_post_schedule_changes(self):
        teacher = make_teacher("Reg", "Poster")
        self.client.force_login(teacher.user)

        response = self.client.post(reverse("edit_schedule"), {})

        self.assertEqual(response.status_code, 403)
        self.assertFalse(teacher.non_teaching_hours.exists())

    def test_staff_teacher_can_open_the_editor(self):
        teacher = make_teacher("Boss", "User")
        teacher.user.is_staff = True
        teacher.user.save()
        self.client.force_login(teacher.user)

        self.assertEqual(self.client.get(reverse("edit_schedule")).status_code, 200)
