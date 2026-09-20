from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils.translation import gettext_lazy as _

from schools.managers import SchoolScopedManager
from schools.models import School, get_default_school_id
from teachers.models import Teacher, WeeklyNonTeachingHours

# The most tutor/co-teacher dropdowns the UI ever shows at once, for a single
# class group or a single schedule entry - just a sane upper bound, not a
# meaningful business rule.
MAX_TUTORS = 6
MAX_CO_TEACHERS = 6


class Subject(models.Model):
    school = models.ForeignKey(
        School,
        on_delete=models.PROTECT,
        related_name="subjects",
        default=get_default_school_id,
        verbose_name=_("school"),
    )
    name = models.CharField(max_length=100, verbose_name=_("name"))

    objects = SchoolScopedManager()

    class Meta:
        ordering = ["name"]
        verbose_name = _("subject")
        verbose_name_plural = _("subjects")
        constraints = [
            models.UniqueConstraint(fields=["school", "name"], name="unique_subject_name_per_school"),
        ]

    def __str__(self):
        return self.name


class ClassGroup(models.Model):
    """A class/group of pupils (e.g. "3r Primària A"), as opposed to a school
    year - see substitutions.services.course_year_start for that other,
    unrelated sense of "course" already used elsewhere in this codebase."""

    school = models.ForeignKey(
        School,
        on_delete=models.PROTECT,
        related_name="class_groups",
        default=get_default_school_id,
        verbose_name=_("school"),
    )
    name = models.CharField(max_length=100, verbose_name=_("name"))
    grade_level = models.CharField(
        max_length=20, choices=Teacher.GradeLevel.choices, verbose_name=_("grade level")
    )
    tutor_count = models.PositiveSmallIntegerField(
        default=1,
        validators=[MinValueValidator(1), MaxValueValidator(MAX_TUTORS)],
        verbose_name=_("number of tutors"),
        help_text=_("How many tutor dropdowns to show when editing this group's tutors below."),
    )
    tutors = models.ManyToManyField(
        Teacher,
        blank=True,
        related_name="tutored_groups",
        verbose_name=_("tutors"),
        help_text=_("The homeroom teacher(s), who usually teach most of this group's subjects."),
    )

    objects = SchoolScopedManager()

    class Meta:
        ordering = ["name"]
        verbose_name = _("class group")
        verbose_name_plural = _("class groups")

    def __str__(self):
        return self.name


class TeacherSubject(models.Model):
    """Which subjects a teacher is qualified to teach - used to flag/prioritise
    candidates when assigning a ScheduleEntry. Not wired into substitution
    matching yet."""

    teacher = models.ForeignKey(Teacher, on_delete=models.CASCADE, related_name="subjects_taught")
    subject = models.ForeignKey(Subject, on_delete=models.CASCADE, related_name="qualified_teachers")

    objects = SchoolScopedManager(school_field="teacher__school")

    class Meta:
        ordering = ["subject__name"]
        constraints = [
            models.UniqueConstraint(fields=["teacher", "subject"], name="unique_teacher_subject"),
        ]
        verbose_name = _("teacher expertise")
        verbose_name_plural = _("teacher expertise")

    def __str__(self):
        return f"{self.teacher} - {self.subject}"


class ScheduleEntry(models.Model):
    """A recurring weekly block of a class group's timetable: who teaches what,
    and when. The positive counterpart of teachers.WeeklyNonTeachingHours,
    which only ever records when a teacher is *not* teaching."""

    class Weekday(models.IntegerChoices):
        MONDAY = 0, _("Monday")
        TUESDAY = 1, _("Tuesday")
        WEDNESDAY = 2, _("Wednesday")
        THURSDAY = 3, _("Thursday")
        FRIDAY = 4, _("Friday")
        SATURDAY = 5, _("Saturday")
        SUNDAY = 6, _("Sunday")

    class_group = models.ForeignKey(ClassGroup, on_delete=models.CASCADE, related_name="schedule_entries")
    subject = models.ForeignKey(Subject, on_delete=models.PROTECT, related_name="schedule_entries")
    teacher = models.ForeignKey(Teacher, on_delete=models.CASCADE, related_name="schedule_entries")
    co_teachers = models.ManyToManyField(
        Teacher,
        blank=True,
        related_name="co_taught_schedule_entries",
        verbose_name=_("co-teachers"),
        help_text=_("Other teacher(s) who also teach this class alongside the main one, if any."),
    )
    weekday = models.IntegerField(choices=Weekday.choices, verbose_name=_("weekday"))
    start_time = models.TimeField(verbose_name=_("start time"))
    end_time = models.TimeField(verbose_name=_("end time"))

    objects = SchoolScopedManager(school_field="teacher__school")

    class Meta:
        ordering = ["weekday", "start_time"]
        verbose_name = _("schedule entry")
        verbose_name_plural = _("schedule entries")
        constraints = [
            models.CheckConstraint(
                check=models.Q(end_time__gt=models.F("start_time")), name="schedule_entry_end_after_start"
            ),
        ]

    def __str__(self):
        return f"{self.class_group} - {self.get_weekday_display()} {self.start_time}-{self.end_time}"

    def clean(self):
        super().clean()
        if not self.start_time or not self.end_time or self.start_time >= self.end_time:
            return  # the CheckConstraint (or a plain required-field error) covers this
        if self.weekday is None or not self.teacher_id or not self.class_group_id:
            return

        overlapping = self._overlapping_entries()

        group_conflict = overlapping.filter(class_group_id=self.class_group_id).first()
        if group_conflict:
            raise ValidationError(
                _("%(group)s already has %(subject)s scheduled then.")
                % {"group": self.class_group, "subject": group_conflict.subject}
            )

        # The main teacher can't already be teaching (as either role)
        # elsewhere, nor be marked non-teaching, over this slot. Co-teachers
        # (a ManyToManyField) get the same check, but separately - see
        # `validate_co_teachers` below, since they can't be validated here
        # until the entry has a pk.
        conflict = _teaching_conflict(overlapping, self.teacher_id, self.weekday, self.start_time, self.end_time)
        if conflict is not None:
            raise ValidationError({"teacher": _conflict_message(self.teacher, conflict)})

    def _overlapping_entries(self):
        # Scoped to this entry's own school - two different schools' teachers
        # never conflict with each other, even if their timetables happen to
        # share a weekday/time.
        return ScheduleEntry.objects.filter(
            weekday=self.weekday,
            start_time__lt=self.end_time,
            end_time__gt=self.start_time,
            class_group__school_id=self.class_group.school_id,
        ).exclude(pk=self.pk)

    def validate_co_teachers(self, co_teacher_ids: list[int]) -> None:
        """Validate the co-teachers about to be set on this entry (a
        ManyToManyField, so it can't be checked from `clean()` - the entry
        needs a pk before `co_teachers.set()` can be called at all). Call
        this after `full_clean()` succeeds and before saving the co-teachers,
        e.g.:

            entry.full_clean()
            entry.validate_co_teachers(co_teacher_ids)
            entry.save()
            entry.co_teachers.set(co_teacher_ids)

        Checks each co-teacher is distinct from the main teacher and from
        each other, and free over this entry's slot - the same rules
        `clean()` applies to the main teacher."""
        overlapping = self._overlapping_entries()
        seen: set[int] = set()
        for teacher_id in co_teacher_ids:
            if teacher_id == self.teacher_id:
                raise ValidationError(
                    {"co_teachers": _("A co-teacher must be different from the main teacher.")}
                )
            if teacher_id in seen:
                raise ValidationError({"co_teachers": _("The same co-teacher can't be picked twice.")})
            seen.add(teacher_id)

            conflict = _teaching_conflict(overlapping, teacher_id, self.weekday, self.start_time, self.end_time)
            if conflict is not None:
                teacher = Teacher.objects.get(pk=teacher_id)
                raise ValidationError({"co_teachers": _conflict_message(teacher, conflict)})


def _teaching_conflict(overlapping, teacher_id, weekday, start_time, end_time):
    """A conflicting ScheduleEntry or WeeklyNonTeachingHours for `teacher_id`
    over [start_time, end_time) on `weekday`, if any - they're already
    teaching (as the main teacher or a co-teacher) elsewhere, or marked
    non-teaching then. `overlapping` is the caller's own-entry-excluded
    ScheduleEntry queryset for that slot (already scoped to one school),
    reused across every participant checked for one entry. The
    WeeklyNonTeachingHours half of the check needs no separate school
    filter - `teacher_id` alone already pins it to one teacher, and so to one
    school."""
    busy = overlapping.filter(models.Q(teacher_id=teacher_id) | models.Q(co_teachers=teacher_id)).first()
    if busy is not None:
        return busy
    return WeeklyNonTeachingHours.objects.filter(
        teacher_id=teacher_id, weekday=weekday, start_time__lt=end_time, end_time__gt=start_time
    ).first()


def _conflict_message(person, conflict) -> str:
    if isinstance(conflict, ScheduleEntry):
        return _("%(teacher)s is already teaching %(group)s then.") % {
            "teacher": person, "group": conflict.class_group,
        }
    return _("%(teacher)s is marked as %(kind)s then, not teaching.") % {
        "teacher": person, "kind": conflict.get_kind_display(),
    }
