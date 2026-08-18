from django.conf import settings
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
    is_paperwork = models.BooleanField(
        default=False,
        verbose_name=_("paperwork"),
        help_text=_("Reserved for paperwork/admin work, as opposed to fully free time."),
    )

    class Meta:
        ordering = ["weekday", "start_time"]
        verbose_name = _("weekly non-teaching hours")
        verbose_name_plural = _("weekly non-teaching hours")
        constraints = [
            models.CheckConstraint(check=models.Q(end_time__gt=models.F("start_time")), name="end_after_start"),
        ]

    def __str__(self):
        return f"{self.teacher} - {self.get_weekday_display()} {self.start_time}-{self.end_time}"
