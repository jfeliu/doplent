from datetime import time

from django.core.exceptions import ValidationError
from django.db import models
from django.utils.translation import gettext_lazy as _

# Defaults match the constants this app used before it supported more than one
# school (see substitutions.services) - the first school ever created (a data
# migration, for the school already in production) gets exactly this config.
DEFAULT_WORKING_WEEKDAYS = [0, 1, 2, 3, 4]
DEFAULT_WORKING_HOURS = [["09:00", "13:00"], ["15:00", "17:00"]]


def default_working_weekdays():
    return list(DEFAULT_WORKING_WEEKDAYS)


def default_working_hours():
    return [list(window) for window in DEFAULT_WORKING_HOURS]


def get_default_school() -> "School":
    """The school every existing FK row (and every test that doesn't care
    about multi-school isolation) implicitly belongs to: the one served at
    the bare base domain (no subdomain). Created lazily on first use rather
    than by a fixture, so a fresh test database doesn't need one seeded."""
    return School.objects.get_or_create(subdomain=None, defaults={"name": "Doplent"})[0]


def get_default_school_id():
    return get_default_school().pk


class School(models.Model):
    name = models.CharField(max_length=200, verbose_name=_("name"))
    # None = this is the "root" school, served at the bare base domain
    # (e.g. doplent.feliuet.com) - at most one school may have this. Any
    # other value is served at "<subdomain>.<base domain>".
    subdomain = models.SlugField(
        unique=True,
        null=True,
        blank=True,
        verbose_name=_("subdomain"),
        help_text=_("Leave blank for the school served at the bare base domain."),
    )
    timezone = models.CharField(max_length=64, default="Europe/Madrid", verbose_name=_("timezone"))
    school_year_start_month = models.PositiveSmallIntegerField(
        default=9, verbose_name=_("school year start month")
    )
    school_year_start_day = models.PositiveSmallIntegerField(
        default=1, verbose_name=_("school year start day")
    )
    working_weekdays = models.JSONField(
        default=default_working_weekdays,
        verbose_name=_("working weekdays"),
        help_text=_("Weekdays the school runs classes, Monday=0 ... Sunday=6."),
    )
    working_hours = models.JSONField(
        default=default_working_hours,
        verbose_name=_("working hours"),
        help_text=_('Daily windows substitutes may be searched in, e.g. [["09:00", "13:00"]].'),
    )
    display_color = models.CharField(max_length=7, blank=True, verbose_name=_("display color"))

    class Meta:
        ordering = ["name"]
        verbose_name = _("school")
        verbose_name_plural = _("schools")

    def __str__(self):
        return self.name

    def clean(self):
        super().clean()
        if self.subdomain is None:
            conflict = School.objects.filter(subdomain__isnull=True).exclude(pk=self.pk)
            if conflict.exists():
                raise ValidationError(
                    {"subdomain": _("Only one school can be served at the bare base domain.")}
                )

    def working_weekdays_set(self) -> frozenset[int]:
        return frozenset(self.working_weekdays or DEFAULT_WORKING_WEEKDAYS)

    def working_hours_as_times(self) -> list[tuple[time, time]]:
        windows = self.working_hours or DEFAULT_WORKING_HOURS
        return [
            (time.fromisoformat(start), time.fromisoformat(end))
            for start, end in windows
        ]
