from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils.translation import gettext_lazy as _

from schools.managers import SchoolScopedManager
from schools.models import School, get_default_school_id


class Teacher(models.Model):
    class GradeLevel(models.TextChoices):
        PRIMARY = "primary", _("Primary")
        PRE_PRIMARY = "pre_primary", _("Pre-primary")

    class Role(models.TextChoices):
        STAFF = "staff", _("Staff")
        MEMBER = "member", _("Member")

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="teacher")
    school = models.ForeignKey(
        School,
        on_delete=models.PROTECT,
        related_name="teachers",
        default=get_default_school_id,
        verbose_name=_("school"),
    )
    role = models.CharField(
        max_length=10,
        choices=Role.choices,
        default=Role.MEMBER,
        verbose_name=_("role"),
        help_text=_("Staff can manage this school's roster and timetable."),
    )
    grade_level = models.CharField(max_length=20, choices=GradeLevel.choices, verbose_name=_("grade level"))
    active = models.BooleanField(default=True, verbose_name=_("active"))
    calendar_url = models.URLField(
        blank=True,
        verbose_name=_("calendar link"),
        help_text=_("Link to this teacher's calendar. Shown when relevant if set."),
    )

    objects = SchoolScopedManager()

    class Meta:
        ordering = ["user__last_name", "user__first_name"]
        verbose_name = _("teacher")
        verbose_name_plural = _("teachers")

    def __str__(self):
        return self.user.get_full_name() or self.user.username


class NonTeachingHoursKind(models.TextChoices):
    """The kinds of non-teaching time, declared in the default order the
    substitute-picker prefers to draw a teacher off them (free first, escolta'm
    last). The actual order is read from NonTeachingHoursPriority at runtime."""

    FREE = "free", _("Free")
    PAPERWORK = "paperwork", _("Paperwork")
    CO_TEACHING = "co_teaching", _("Co-teaching")
    ESCOLTAM = "escoltam", _("Escolta'm")
    POESIA = "poesia", "POESIA"
    CICLE = "cicle", "Cicle"
    REFORC = "reforc", "Reforç"


# Kinds where WeeklyNonTeachingHours.head is mandatory rather than forbidden -
# see WeeklyNonTeachingHours.clean() and NonTeachingHoursForm.clean(). Reforç
# reuses the co-teaching rule (name the teacher this block supports) rather
# than getting its own field.
HEAD_REQUIRED_KINDS = frozenset({NonTeachingHoursKind.CO_TEACHING, NonTeachingHoursKind.REFORC})


# The default ordering a newly created School's NonTeachingHoursPriority rows
# are seeded with - see schools.admin.SchoolAdmin, and teachers/migrations/
# 0004_non_teaching_hours_kind.py + 0007_add_poesia_and_cicle_non_teaching_kinds.py
# + 0010_add_reforc_non_teaching_kind.py for how the pre-existing schools got
# these same values.
DEFAULT_NON_TEACHING_HOURS_PRIORITIES = [
    (NonTeachingHoursKind.FREE, 0),
    (NonTeachingHoursKind.PAPERWORK, 10),
    (NonTeachingHoursKind.CO_TEACHING, 20),
    (NonTeachingHoursKind.ESCOLTAM, 30),
    (NonTeachingHoursKind.POESIA, 40),
    (NonTeachingHoursKind.CICLE, 50),
    (NonTeachingHoursKind.REFORC, 60),
]


class NonTeachingHoursPriority(models.Model):
    """How eagerly the substitute-picker pulls a teacher off each kind of
    non-teaching block. Lower `priority` is drawn from first (free time), higher
    is a last resort (escolta'm). One row per kind per school, seeded when the
    school is created and editable in the admin so the order can be retuned
    without a deploy."""

    school = models.ForeignKey(
        School,
        on_delete=models.CASCADE,
        related_name="non_teaching_hours_priorities",
        default=get_default_school_id,
        verbose_name=_("school"),
    )
    kind = models.CharField(max_length=20, choices=NonTeachingHoursKind.choices, verbose_name=_("kind"))
    priority = models.PositiveIntegerField(
        verbose_name=_("priority"), help_text=_("Lower is pulled first.")
    )

    objects = SchoolScopedManager()

    class Meta:
        ordering = ["priority"]
        verbose_name = _("non-teaching hours priority")
        verbose_name_plural = _("non-teaching hours priorities")
        constraints = [
            models.UniqueConstraint(fields=["school", "kind"], name="unique_priority_per_school_kind"),
        ]

    def __str__(self):
        return f"{self.get_kind_display()} ({self.priority})"

    @classmethod
    def ordering_map(cls, school) -> dict[str, int]:
        """`kind -> priority` for every kind, for one school. Any kind without
        a row falls to the end, so an unconfigured kind is treated as the
        least preferred."""
        configured = dict(cls.objects.filter(school=school).values_list("kind", "priority"))
        fallback = max(configured.values(), default=0) + 1
        return {kind: configured.get(kind, fallback) for kind in NonTeachingHoursKind.values}


class WeeklyNonTeachingHours(models.Model):
    """A recurring block of time each week when a teacher is at school but not
    teaching a class (a free period) - and so could potentially cover for
    someone else. A teacher can have several of these per weekday."""

    class Weekday(models.IntegerChoices):
        MONDAY = 0, _("Monday")
        TUESDAY = 1, _("Tuesday")
        WEDNESDAY = 2, _("Wednesday")
        THURSDAY = 3, _("Thursday")
        FRIDAY = 4, _("Friday")
        SATURDAY = 5, _("Saturday")
        SUNDAY = 6, _("Sunday")

    teacher = models.ForeignKey(Teacher, on_delete=models.CASCADE, related_name="non_teaching_hours")
    weekday = models.IntegerField(choices=Weekday.choices, verbose_name=_("weekday"))
    start_time = models.TimeField(verbose_name=_("start time"))
    end_time = models.TimeField(verbose_name=_("end time"))
    kind = models.CharField(
        max_length=20,
        choices=NonTeachingHoursKind.choices,
        default=NonTeachingHoursKind.FREE,
        verbose_name=_("kind"),
    )
    # The teacher this block is attached to - for co-teaching, the class's
    # lead teacher (when the head is absent the co-teacher runs the room, so
    # the head needs no substitute for that slot - see substitutions.services);
    # for Reforç, the teacher being supported. Required whenever kind is in
    # HEAD_REQUIRED_KINDS, and must be someone other than `teacher`.
    # Hard-deleting the head takes the (now meaningless) block with it - in
    # normal use teachers are deactivated, not deleted, and that keeps the
    # block intact.
    head = models.ForeignKey(
        Teacher,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="co_teaching_led",
        verbose_name=_("head teacher"),
        help_text=_("The teacher this block is with. Required for co-teaching and Reforç blocks."),
    )

    objects = SchoolScopedManager(school_field="teacher__school")

    class Meta:
        ordering = ["weekday", "start_time"]
        verbose_name = _("weekly non-teaching hours")
        verbose_name_plural = _("weekly non-teaching hours")
        constraints = [
            models.CheckConstraint(check=models.Q(end_time__gt=models.F("start_time")), name="end_after_start"),
            # "a HEAD_REQUIRED_KINDS block needs a head" is enforced in
            # Model.clean(), the import and the schedule form (every write
            # path), not as a DB check - that would block migrating databases
            # that already hold headless co-teaching blocks from before this
            # field existed. The constraint name predates Reforç but still
            # applies to it - "a block's head is never its own teacher".
            models.CheckConstraint(
                check=~models.Q(head=models.F("teacher")),
                name="co_teaching_head_is_not_self",
            ),
        ]

    def __str__(self):
        return f"{self.teacher} - {self.get_weekday_display()} {self.start_time}-{self.end_time}"

    def clean(self):
        super().clean()
        if self.kind not in HEAD_REQUIRED_KINDS:
            self.head = None
        elif self.head_id is None:
            raise ValidationError({"head": _("This block needs a head teacher.")})
        if self.head_id and self.teacher_id and self.head_id == self.teacher_id:
            raise ValidationError({"head": _("The head teacher must be a different teacher.")})
