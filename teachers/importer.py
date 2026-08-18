import csv
import io
import secrets
import string
from dataclasses import dataclass, field
from datetime import datetime

from django.contrib.auth.models import User
from django.db import transaction
from django.utils.translation import gettext_lazy as _

from .models import Teacher, WeeklyNonTeachingHours

REQUIRED_COLUMNS = {"first_name", "last_name", "grade_level"}

WEEKDAY_ALIASES = {
    "monday": 0, "mon": 0,
    "tuesday": 1, "tue": 1, "tues": 1,
    "wednesday": 2, "wed": 2,
    "thursday": 3, "thu": 3, "thurs": 3,
    "friday": 4, "fri": 4,
    "saturday": 5, "sat": 5,
    "sunday": 6, "sun": 6,
}

TRUE_VALUES = {"true", "yes", "y", "1"}
FALSE_VALUES = {"false", "no", "n", "0", ""}

CSV_TEMPLATE = (
    "first_name,last_name,email,grade_level,weekday,start_time,end_time,is_paperwork\n"
    "Jane,Doe,jane.doe@example.edu,primary,Monday,08:00,09:30,no\n"
    "Jane,Doe,jane.doe@example.edu,primary,Monday,13:00,16:00,yes\n"
    "Jane,Doe,jane.doe@example.edu,primary,Wednesday,08:00,12:00,no\n"
    "John,Smith,john.smith@example.edu,pre_primary,Tuesday,09:00,10:00,no\n"
)


@dataclass
class RowError:
    line_number: int
    message: str


@dataclass
class ImportResult:
    created_users: list[tuple[str, str]] = field(default_factory=list)
    teachers_touched: set[str] = field(default_factory=set)
    hours_created: int = 0
    errors: list[RowError] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def _parse_weekday(raw: str) -> int:
    normalized = raw.strip().lower()
    if normalized in WEEKDAY_ALIASES:
        return WEEKDAY_ALIASES[normalized]
    if normalized.isdigit() and 0 <= int(normalized) <= 6:
        return int(normalized)
    raise ValueError(_("unrecognized weekday '%(value)s'") % {"value": raw})


def _parse_time(raw: str):
    raw = raw.strip()
    for fmt in ("%H:%M", "%H:%M:%S"):
        try:
            return datetime.strptime(raw, fmt).time()
        except ValueError:
            continue
    raise ValueError(_("unrecognized time '%(value)s', expected HH:MM") % {"value": raw})


def _parse_bool(raw: str) -> bool:
    normalized = raw.strip().lower()
    if normalized in TRUE_VALUES:
        return True
    if normalized in FALSE_VALUES:
        return False
    raise ValueError(_("unrecognized is_paperwork value '%(value)s', expected yes/no") % {"value": raw})


def _generate_password() -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(12))


def _derive_username(first_name: str, last_name: str) -> str:
    """Build a username from a teacher's name: lowercase, dot-separated,
    with any internal whitespace in each name part stripped out."""
    return f"{''.join(first_name.split()).lower()}.{''.join(last_name.split()).lower()}"


def _process_row(row: dict, result: ImportResult) -> None:
    first_name = (row.get("first_name") or "").strip()
    last_name = (row.get("last_name") or "").strip()
    email = (row.get("email") or "").strip()
    grade_raw = (row.get("grade_level") or "").strip().lower().replace("-", "_")
    weekday_raw = (row.get("weekday") or "").strip()
    start_raw = (row.get("start_time") or "").strip()
    end_raw = (row.get("end_time") or "").strip()
    paperwork_raw = row.get("is_paperwork") or ""

    if not first_name or not last_name:
        raise ValueError(_("first_name and last_name are required"))
    if grade_raw not in Teacher.GradeLevel.values:
        raise ValueError(
            _("grade_level must be one of %(choices)s") % {"choices": ", ".join(Teacher.GradeLevel.values)}
        )

    username = _derive_username(first_name, last_name)

    user, user_created = User.objects.get_or_create(
        username=username,
        defaults={"first_name": first_name, "last_name": last_name, "email": email},
    )
    if user_created:
        password = _generate_password()
        user.set_password(password)
        user.save()
        result.created_users.append((username, password))

    teacher = Teacher.objects.filter(user=user).first()
    if teacher is None:
        teacher = Teacher.objects.create(user=user, grade_level=grade_raw)
    elif teacher.grade_level != grade_raw:
        raise ValueError(
            _("grade_level '%(grade)s' for %(username)s conflicts with existing grade_level '%(existing)s'")
            % {"grade": grade_raw, "username": username, "existing": teacher.grade_level}
        )
    result.teachers_touched.add(username)

    if weekday_raw or start_raw or end_raw:
        weekday = _parse_weekday(weekday_raw)
        start_time = _parse_time(start_raw)
        end_time = _parse_time(end_raw)
        is_paperwork = _parse_bool(paperwork_raw)
        if end_time <= start_time:
            raise ValueError(_("end_time must be after start_time"))
        block, created = WeeklyNonTeachingHours.objects.get_or_create(
            teacher=teacher,
            weekday=weekday,
            start_time=start_time,
            end_time=end_time,
            defaults={"is_paperwork": is_paperwork},
        )
        if created:
            result.hours_created += 1
        elif block.is_paperwork != is_paperwork:
            raise ValueError(
                _("is_paperwork for %(username)s's %(start)s-%(end)s block conflicts with the existing value")
                % {"username": username, "start": start_raw, "end": end_raw}
            )


def import_teachers_from_csv(uploaded_file) -> ImportResult:
    """Parse an uploaded CSV of teachers and their weekly non-teaching hours,
    creating User/Teacher/WeeklyNonTeachingHours records. One row per
    non-teaching block; repeat a teacher's first_name/last_name across rows to
    give them several blocks, or leave weekday/start_time/end_time blank to
    register a teacher with no hours yet. Each teacher's username is derived
    from their name (lowercase, dot-separated, e.g. "Jane Doe" -> "jane.doe").
    Re-uploading the same file is safe - matching users and non-teaching
    blocks are left as-is rather than duplicated. If any row fails validation,
    nothing is saved."""
    result = ImportResult()
    content = uploaded_file.read().decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(content))

    fieldnames = {name.strip() for name in (reader.fieldnames or [])}
    if not REQUIRED_COLUMNS.issubset(fieldnames):
        message = _("CSV must include columns: %(columns)s") % {"columns": ", ".join(sorted(REQUIRED_COLUMNS))}
        result.errors.append(RowError(0, str(message)))
        return result

    with transaction.atomic():
        for line_number, row in enumerate(reader, start=2):  # header is line 1
            if not any((value or "").strip() for value in row.values()):
                continue
            try:
                _process_row(row, result)
            except ValueError as exc:
                result.errors.append(RowError(line_number, str(exc)))

        if result.errors:
            transaction.set_rollback(True)

    return result
