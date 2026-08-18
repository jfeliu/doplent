from django.db import models
from django.utils.translation import gettext_lazy as _

from teachers.models import Teacher


class Absence(models.Model):
    teacher = models.ForeignKey(Teacher, on_delete=models.CASCADE, related_name="absences")
    start_datetime = models.DateTimeField(verbose_name=_("unavailable from"))
    end_datetime = models.DateTimeField(verbose_name=_("unavailable until"))
    reason = models.CharField(max_length=255, blank=True, verbose_name=_("reason"))
    created_at = models.DateTimeField(auto_now_add=True, verbose_name=_("created at"))

    class Meta:
        ordering = ["-start_datetime"]
        verbose_name = _("absence")
        verbose_name_plural = _("absences")
        constraints = [
            models.CheckConstraint(
                check=models.Q(end_datetime__gt=models.F("start_datetime")), name="absence_end_after_start"
            ),
        ]

    def __str__(self):
        return f"{self.teacher} away {self.start_datetime} - {self.end_datetime}"


class Substitution(models.Model):
    """Covers a sub-period of an absence. Most absences have a single
    substitution spanning their whole range, but when no one teacher can
    cover the entire absence it's split into several substitutions, each
    covering a piece of it."""

    absence = models.ForeignKey(Absence, on_delete=models.CASCADE, related_name="substitutions")
    substitute_teacher = models.ForeignKey(Teacher, on_delete=models.CASCADE, related_name="substitutions_done")
    start_datetime = models.DateTimeField(verbose_name=_("covers from"))
    end_datetime = models.DateTimeField(verbose_name=_("covers until"))
    confirmed_at = models.DateTimeField(auto_now_add=True, verbose_name=_("confirmed at"))

    class Meta:
        ordering = ["start_datetime"]
        verbose_name = _("substitution")
        verbose_name_plural = _("substitutions")
        constraints = [
            models.CheckConstraint(
                check=models.Q(end_datetime__gt=models.F("start_datetime")), name="substitution_end_after_start"
            ),
        ]

    def __str__(self):
        return f"{self.substitute_teacher} covers {self.absence} ({self.start_datetime} - {self.end_datetime})"
