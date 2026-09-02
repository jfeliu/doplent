from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils.translation import gettext_lazy as _


class Teacher(models.Model):
    class GradeLevel(models.TextChoices):
        PRIMARY = "primary", _("Primary")
        PRE_PRIMARY = "pre_primary", _("Pre-primary")

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="teacher")
    grade_level = models.CharField(max_length=20, choices=GradeLevel.choices, verbose_name=_("grade level"))
    active = models.BooleanField(default=True, verbose_name=_("active"))

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


class NonTeachingHoursPriority(models.Model):
    """How eagerly the substitute-picker pulls a teacher off each kind of
    non-teaching block. Lower `priority` is drawn from first (free time), higher
    is a last resort (escolta'm). One row per kind, seeded by migration and
    editable in the admin so the order can be retuned without a deploy."""

    kind = models.CharField(
        max_length=20, choices=NonTeachingHoursKind.choices, unique=True, verbose_name=_("kind")
    )
    priority = models.PositiveIntegerField(
        verbose_name=_("priority"), help_text=_("Lower is pulled first.")
    )

    class Meta:
        ordering = ["priority"]
        verbose_name = _("non-teaching hours priority")
        verbose_name_plural = _("non-teaching hours priorities")

    def __str__(self):
        return f"{self.get_kind_display()} ({self.priority})"

    @classmethod
    def ordering_map(cls) -> dict[str, int]:
        """`kind -> priority` for every kind. Any kind without a row falls to the
        end, so an unconfigured kind is treated as the least preferred."""
        configured = dict(cls.objects.values_list("kind", "priority"))
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
    # For co-teaching blocks only: the teacher who leads the co-taught class.
    # When the head is absent the co-teacher runs the room, so the head needs no
    # substitute for that slot (see substitutions.services). Required whenever
    # kind is co_teaching, and must be someone other than `teacher`. Hard-deleting
    # the head takes the (now meaningless) co-teaching block with it - in normal
    # use teachers are deactivated, not deleted, and that keeps the block intact.
    head = models.ForeignKey(
        Teacher,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="co_teaching_led",
        verbose_name=_("co-teaching head"),
        help_text=_("The teacher who leads this co-taught class. Required for co-teaching blocks."),
    )

    class Meta:
        ordering = ["weekday", "start_time"]
        verbose_name = _("weekly non-teaching hours")
        verbose_name_plural = _("weekly non-teaching hours")
        constraints = [
            models.CheckConstraint(check=models.Q(end_time__gt=models.F("start_time")), name="end_after_start"),
            # "co-teaching needs a head" is enforced in Model.clean(), the import
            # and the schedule form (every write path), not as a DB check - that
            # would block migrating databases that already hold headless
            # co-teaching blocks from before this field existed.
            models.CheckConstraint(
                check=~models.Q(head=models.F("teacher")),
                name="co_teaching_head_is_not_self",
            ),
        ]

    def __str__(self):
        return f"{self.teacher} - {self.get_weekday_display()} {self.start_time}-{self.end_time}"

    def clean(self):
        super().clean()
        if self.kind != NonTeachingHoursKind.CO_TEACHING:
            self.head = None
        elif self.head_id is None:
            raise ValidationError({"head": _("A co-teaching block needs a head teacher.")})
        if self.head_id and self.teacher_id and self.head_id == self.teacher_id:
            raise ValidationError({"head": _("The head teacher must be a different teacher.")})
