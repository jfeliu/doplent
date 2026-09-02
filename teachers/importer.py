import csv
import io
import secrets
import string
from dataclasses import dataclass, field
from datetime import datetime, time

from django.contrib.auth.models import User
from django.db import transaction
from django.utils.translation import gettext_lazy as _

from .models import NonTeachingHoursKind, Teacher, WeeklyNonTeachingHours

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

# Accepts the stored codes plus common english/catalan spellings. A blank cell
# (or no `type` column at all) means free time.
KIND_ALIASES = {
    "": NonTeachingHoursKind.FREE,
    "free": NonTeachingHoursKind.FREE,
    "lliure": NonTeachingHoursKind.FREE,
    "paperwork": NonTeachingHoursKind.PAPERWORK,
    "carrec": NonTeachingHoursKind.PAPERWORK,
    "càrrec": NonTeachingHoursKind.PAPERWORK,
    "co_teaching": NonTeachingHoursKind.CO_TEACHING,
    "co-teaching": NonTeachingHoursKind.CO_TEACHING,
    "coteaching": NonTeachingHoursKind.CO_TEACHING,
    "codocencia": NonTeachingHoursKind.CO_TEACHING,
    "codocència": NonTeachingHoursKind.CO_TEACHING,
    "escoltam": NonTeachingHoursKind.ESCOLTAM,
    "escolta'm": NonTeachingHoursKind.ESCOLTAM,
    "escolta_m": NonTeachingHoursKind.ESCOLTAM,
}

# The header of the "co-teaching head" column - it holds the lead teacher's
# "First Last" name for co_teaching rows and is blank for every other kind. The
# space matches the school's spreadsheets; an underscore is accepted too.
HEAD_COLUMN = "co_teaching head"
HEAD_COLUMN_ALIASES = (HEAD_COLUMN, "co_teaching_head", "co-teaching head")

EXPORT_COLUMNS = [
    "first_name", "last_name", "email", "grade_level",
    "weekday", "start_time", "end_time", "type", HEAD_COLUMN,
]

CSV_TEMPLATE = (
    "first_name,last_name,email,grade_level,weekday,start_time,end_time,type,co_teaching head\n"
    "Jane,Doe,jane.doe@example.edu,primary,Monday,08:00,09:30,free,\n"
    "Jane,Doe,jane.doe@example.edu,primary,Monday,13:00,16:00,paperwork,\n"
    "Jane,Doe,jane.doe@example.edu,primary,Wednesday,08:00,12:00,co_teaching,John Smith\n"
    "John,Smith,john.smith@example.edu,pre_primary,Tuesday,09:00,10:00,escoltam,\n"
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


def _parse_kind(raw: str) -> str:
    normalized = raw.strip().lower()
    if normalized in KIND_ALIASES:
        return KIND_ALIASES[normalized]
    raise ValueError(
        _("unrecognized type '%(value)s', expected one of %(choices)s")
        % {"value": raw, "choices": ", ".join(NonTeachingHoursKind.values)}
    )


def _generate_password() -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(12))


def _derive_username(first_name: str, last_name: str) -> str:
    """Build a username from a teacher's name: lowercase, dot-separated,
    with any internal whitespace in each name part stripped out."""
    return f"{''.join(first_name.split()).lower()}.{''.join(last_name.split()).lower()}"


@dataclass
class _BlockSpec:
    """A non-teaching block parsed from one CSV row, kept aside until every
    teacher in the file exists so a `co_teaching head` naming a teacher defined
    further down the CSV still resolves."""
    teacher: Teacher
    weekday: int
    start_time: time
    end_time: time
    kind: str
    head_name: str
    username: str
    line_number: int


_AMBIGUOUS = object()


def _row_get(row: dict, aliases) -> str:
    for alias in aliases:
        value = row.get(alias)
        if value:
            return value
    return ""


def _normalize_name(name: str) -> str:
    """Fold a full name to a match key: trimmed, single-spaced, case-insensitive."""
    return " ".join(name.split()).casefold()


def _process_row(row: dict, result: ImportResult, line_number: int) -> _BlockSpec | None:
    first_name = (row.get("first_name") or "").strip()
    last_name = (row.get("last_name") or "").strip()
    email = (row.get("email") or "").strip()
    grade_raw = (row.get("grade_level") or "").strip().lower().replace("-", "_")
    weekday_raw = (row.get("weekday") or "").strip()
    start_raw = (row.get("start_time") or "").strip()
    end_raw = (row.get("end_time") or "").strip()
    kind_raw = row.get("type") or ""
    head_raw = _row_get(row, HEAD_COLUMN_ALIASES).strip()

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

    if not (weekday_raw or start_raw or end_raw):
        if head_raw:
            raise ValueError(
                _("'%(column)s' only applies to co_teaching blocks") % {"column": HEAD_COLUMN}
            )
        return None

    weekday = _parse_weekday(weekday_raw)
    start_time = _parse_time(start_raw)
    end_time = _parse_time(end_raw)
    kind = _parse_kind(kind_raw)
    if end_time <= start_time:
        raise ValueError(_("end_time must be after start_time"))
    if kind == NonTeachingHoursKind.CO_TEACHING and not head_raw:
        raise ValueError(
            _("a co_teaching block needs a '%(column)s' (the lead teacher's first and last name)")
            % {"column": HEAD_COLUMN}
        )
    if kind != NonTeachingHoursKind.CO_TEACHING and head_raw:
        raise ValueError(
            _("'%(column)s' only applies to co_teaching blocks") % {"column": HEAD_COLUMN}
        )
    return _BlockSpec(
        teacher=teacher,
        weekday=weekday,
        start_time=start_time,
        end_time=end_time,
        kind=kind,
        head_name=head_raw,
        username=username,
        line_number=line_number,
    )


def _teacher_name_index() -> dict:
    """`normalized "First Last" -> Teacher` for every teacher, with `_AMBIGUOUS`
    stored where two teachers share a name. Built after all rows are processed,
    so it also covers teachers created earlier in the same import."""
    index: dict = {}
    for teacher in Teacher.objects.select_related("user"):
        key = _normalize_name(f"{teacher.user.first_name} {teacher.user.last_name}")
        index[key] = _AMBIGUOUS if key in index else teacher
    return index


def _resolve_head(spec: _BlockSpec, name_index: dict, result: ImportResult) -> Teacher | None:
    match = name_index.get(_normalize_name(spec.head_name))
    if match is None:
        result.errors.append(RowError(
            spec.line_number,
            str(_("co_teaching head '%(name)s' doesn't match any teacher in the system or this file")
                % {"name": spec.head_name}),
        ))
        return None
    if match is _AMBIGUOUS:
        result.errors.append(RowError(
            spec.line_number,
            str(_("co_teaching head '%(name)s' matches more than one teacher") % {"name": spec.head_name}),
        ))
        return None
    if match.pk == spec.teacher.pk:
        result.errors.append(RowError(
            spec.line_number,
            str(_("%(username)s can't be the co_teaching head of their own block") % {"username": spec.username}),
        ))
        return None
    return match


def _apply_blocks(specs: list[_BlockSpec], result: ImportResult) -> None:
    """Create (or reconcile) every parsed block now that all teachers exist,
    resolving each co_teaching block's head against the full roster."""
    name_index = _teacher_name_index()
    for spec in specs:
        head = None
        if spec.kind == NonTeachingHoursKind.CO_TEACHING:
            head = _resolve_head(spec, name_index, result)
            if head is None:
                continue

        block, created = WeeklyNonTeachingHours.objects.get_or_create(
            teacher=spec.teacher,
            weekday=spec.weekday,
            start_time=spec.start_time,
            end_time=spec.end_time,
            defaults={"kind": spec.kind, "head": head},
        )
        if created:
            result.hours_created += 1
            continue
        if block.kind != spec.kind:
            result.errors.append(RowError(
                spec.line_number,
                str(_("type for %(username)s's %(start)s-%(end)s block conflicts with the existing value")
                    % {
                        "username": spec.username,
                        "start": spec.start_time.strftime("%H:%M"),
                        "end": spec.end_time.strftime("%H:%M"),
                    }),
            ))
            continue
        head_id = head.pk if head else None
        if block.head_id != head_id:
            block.head = head
            block.save(update_fields=["head"])


def _decode(raw: bytes) -> str | None:
    """Decode uploaded CSV bytes. Tries UTF-8 (the template's encoding), then
    Windows-1252, which is what Excel on Windows produces for Western European
    text - so a file with accented names like "Monclús" still imports. Returns
    None if neither fits."""
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return None


def import_teachers_from_csv(uploaded_file) -> ImportResult:
    """Parse an uploaded CSV of teachers and their weekly non-teaching hours,
    creating User/Teacher/WeeklyNonTeachingHours records. One row per
    non-teaching block; repeat a teacher's first_name/last_name across rows to
    give them several blocks, or leave weekday/start_time/end_time blank to
    register a teacher with no hours yet. Each teacher's username is derived
    from their name (lowercase, dot-separated, e.g. "Jane Doe" -> "jane.doe").
    Re-uploading the same file is safe - matching users and non-teaching
    blocks are left as-is rather than duplicated. Co-teaching blocks must name
    a "co_teaching head" (a teacher's "First Last", resolved against the whole
    roster including teachers added later in the same file). If any row fails
    validation, nothing is saved."""
    result = ImportResult()
    content = _decode(uploaded_file.read())
    if content is None:
        result.errors.append(RowError(0, str(_("Could not read the file - save it as UTF-8 CSV and try again."))))
        return result
    reader = csv.DictReader(io.StringIO(content))

    fieldnames = {name.strip() for name in (reader.fieldnames or [])}
    if not REQUIRED_COLUMNS.issubset(fieldnames):
        message = _("CSV must include columns: %(columns)s") % {"columns": ", ".join(sorted(REQUIRED_COLUMNS))}
        result.errors.append(RowError(0, str(message)))
        return result

    with transaction.atomic():
        specs: list[_BlockSpec] = []
        for line_number, row in enumerate(reader, start=2):  # header is line 1
            if not any((value or "").strip() for value in row.values()):
                continue
            try:
                spec = _process_row(row, result, line_number)
            except ValueError as exc:
                result.errors.append(RowError(line_number, str(exc)))
                continue
            if spec is not None:
                specs.append(spec)

        if not result.errors:
            _apply_blocks(specs, result)

        if result.errors:
            transaction.set_rollback(True)

    return result


def export_teachers_to_csv() -> str:
    """Serialize every active teacher and their weekly non-teaching hours as a
    CSV string in the same format import_teachers_from_csv accepts, so the
    current calendar can be downloaded, edited and re-uploaded without loss.
    One row per non-teaching block; a teacher with no hours yet gets a single
    row with the weekday/time columns left blank. Weekdays are written in
    English and types as their stored codes, both of which the importer
    understands regardless of the active language. Co-teaching blocks carry
    their head teacher's "First Last" name in the co_teaching head column."""
    teachers = (
        Teacher.objects.filter(active=True)
        .select_related("user")
        .prefetch_related("non_teaching_hours__head__user")
        .order_by("user__last_name", "user__first_name")
    )

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(EXPORT_COLUMNS)
    for teacher in teachers:
        base = [
            teacher.user.first_name,
            teacher.user.last_name,
            teacher.user.email,
            teacher.grade_level,
        ]
        blocks = sorted(
            teacher.non_teaching_hours.all(),
            key=lambda block: (block.weekday, block.start_time),
        )
        if not blocks:
            writer.writerow(base + ["", "", "", "", ""])
            continue
        for block in blocks:
            head_name = ""
            if block.kind == NonTeachingHoursKind.CO_TEACHING and block.head_id:
                head_name = f"{block.head.user.first_name} {block.head.user.last_name}".strip()
            writer.writerow(
                base
                + [
                    WeeklyNonTeachingHours.Weekday(block.weekday).name.title(),
                    block.start_time.strftime("%H:%M"),
                    block.end_time.strftime("%H:%M"),
                    block.kind,
                    head_name,
                ]
            )
    return buffer.getvalue()
