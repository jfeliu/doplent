from django.db import models
from django.utils.translation import gettext_lazy as _

from teachers.models import Teacher


class Absence(models.Model):
    class Reason(models.TextChoices):
        SICK_LEAVE = "baixa_it_at", "Baixa IT-AT"
        PERMITS = "permisos", "Permisos"
        SCHOOL_ACTIVITY = "activitat_escola", "Activitat Escola"
        FOUNDATION_ACTIVITY = "activitat_fundacio", "Activitat Fundació"

    teacher = models.ForeignKey(Teacher, on_delete=models.CASCADE, related_name="absences")
    start_datetime = models.DateTimeField(verbose_name=_("unavailable from"))
    end_datetime = models.DateTimeField(verbose_name=_("unavailable until"))
    reason = models.CharField(
        max_length=255, choices=Reason.choices, verbose_name=_("reason")
    )
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


class SubstitutionOffer(models.Model):
    """A pending request for a specific teacher to cover part of an absence.
    Notified by email; the candidate accepts or rejects it in-app. Accepting
    creates the confirmed Substitution - this model never affects what counts
    as covered on its own."""

    class Status(models.TextChoices):
        PENDING = "pending", _("Pending")
        ACCEPTED = "accepted", _("Accepted")
        DECLINED = "declined", _("Declined")
        EXPIRED = "expired", _("Expired")

    class DeclineReason(models.TextChoices):
        INTERVIEW = "interview", _("Interview")
        MEETING = "meeting", _("Meeting")
        PERSONAL = "personal", _("Personal reasons")
        OTHER = "other", _("Other")

    absence = models.ForeignKey(Absence, on_delete=models.CASCADE, related_name="offers")
    substitute_teacher = models.ForeignKey(Teacher, on_delete=models.CASCADE, related_name="substitution_offers")
    start_datetime = models.DateTimeField(verbose_name=_("offered from"))
    end_datetime = models.DateTimeField(verbose_name=_("offered until"))
    status = models.CharField(
        max_length=10, choices=Status.choices, default=Status.PENDING, verbose_name=_("status")
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name=_("offered at"))
    responded_at = models.DateTimeField(null=True, blank=True, verbose_name=_("responded at"))
    decline_reason = models.CharField(
        max_length=20, choices=DeclineReason.choices, blank=True, verbose_name=_("decline reason")
    )
    decline_reason_detail = models.CharField(
        max_length=255, blank=True, verbose_name=_("decline reason detail")
    )
    resulting_substitution = models.OneToOneField(
        Substitution,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="offer",
        verbose_name=_("resulting substitution"),
    )

    class Meta:
        ordering = ["-created_at"]
        verbose_name = _("substitution offer")
        verbose_name_plural = _("substitution offers")
        constraints = [
            models.CheckConstraint(
                check=models.Q(end_datetime__gt=models.F("start_datetime")), name="substitution_offer_end_after_start"
            ),
            models.UniqueConstraint(
                fields=["absence", "substitute_teacher", "start_datetime", "end_datetime"],
                condition=models.Q(status="pending"),
                name="unique_pending_offer_per_candidate_slot",
            ),
        ]

    def __str__(self):
        return f"{self.substitute_teacher} offered {self.absence} ({self.get_status_display()})"

    @property
    def decline_reason_label(self):
        """The decline reason as one human-readable string: the free-text
        detail on its own when "Other" was picked, the preset's label
        otherwise (with the detail appended in parentheses if also given).
        Empty when the offer wasn't declined with a reason."""
        if self.decline_reason == self.DeclineReason.OTHER:
            return self.decline_reason_detail
        if not self.decline_reason:
            return self.decline_reason_detail
        label = self.get_decline_reason_display()
        return f"{label} ({self.decline_reason_detail})" if self.decline_reason_detail else label
