from datetime import datetime, time, timedelta

from django import forms
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from .models import Absence, SubstitutionOffer
from .services import coverage_needed

WHOLE_DAY_START = time(9, 0)
WHOLE_DAY_END = time(17, 0)


def _time_choices():
    choices = [("", "---------")]
    current = datetime.combine(datetime.min, WHOLE_DAY_START)
    end = datetime.combine(datetime.min, WHOLE_DAY_END)
    while current <= end:
        label = current.strftime("%H:%M")
        choices.append((label, label))
        current += timedelta(minutes=30)
    return choices


TIME_CHOICES = _time_choices()


class AbsenceForm(forms.ModelForm):
    date = forms.DateField(label="Data", widget=forms.HiddenInput())
    whole_day = forms.BooleanField(
        label="Tot el dia (9:00 - 17:00)",
        required=False,
        widget=forms.CheckboxInput(attrs={"class": "form-check-input"}),
    )
    start_time = forms.ChoiceField(label="Des de", required=False, choices=TIME_CHOICES)
    end_time = forms.ChoiceField(label="Fins a", required=False, choices=TIME_CHOICES)

    class Meta:
        model = Absence
        fields = ["reason"]
        field_order = ["date", "whole_day", "start_time", "end_time", "reason"]

    def __init__(self, *args, teacher=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.teacher = teacher

    def clean(self):
        cleaned = super().clean()
        date = cleaned.get("date")
        whole_day = cleaned.get("whole_day")
        start_time_str = cleaned.get("start_time")
        end_time_str = cleaned.get("end_time")

        if whole_day:
            start_time, end_time = WHOLE_DAY_START, WHOLE_DAY_END
        elif not start_time_str or not end_time_str:
            self.add_error(None, _("Enter a start and end time, or select whole day."))
            start_time = end_time = None
        else:
            start_time = datetime.strptime(start_time_str, "%H:%M").time()
            end_time = datetime.strptime(end_time_str, "%H:%M").time()
            if end_time <= start_time:
                self.add_error(None, _("End must be after start."))

        if date and start_time and end_time:
            start_datetime = timezone.make_aware(datetime.combine(date, start_time))
            end_datetime = timezone.make_aware(datetime.combine(date, end_time))
            cleaned["start_datetime"] = start_datetime
            cleaned["end_datetime"] = end_datetime
            if self.teacher and not coverage_needed(self.teacher, start_datetime, end_datetime):
                self.add_error(
                    None,
                    _(
                        "You have no classes to cover then - the whole period falls "
                        "within your non-teaching hours, outside school hours, or on "
                        "a weekend."
                    ),
                )
        return cleaned

    def save(self, commit=True):
        instance = super().save(commit=False)
        instance.start_datetime = self.cleaned_data["start_datetime"]
        instance.end_datetime = self.cleaned_data["end_datetime"]
        if commit:
            instance.save()
        return instance


class DeclineOfferForm(forms.Form):
    """Why a teacher is turning down an offer - a preset reason, plus free text
    that's optional for the presets but required when they pick "Other"."""

    reason = forms.ChoiceField(
        label=_("Reason"),
        choices=SubstitutionOffer.DeclineReason.choices,
        widget=forms.RadioSelect,
    )
    detail = forms.CharField(
        label=_("Details"),
        required=False,
        max_length=255,
        widget=forms.TextInput(attrs={"class": "form-control"}),
    )

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("reason") == SubstitutionOffer.DeclineReason.OTHER and not cleaned.get("detail"):
            self.add_error("detail", _("Add a short reason."))
        return cleaned
