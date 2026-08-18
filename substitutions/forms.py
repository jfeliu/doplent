from django import forms
from django.utils.translation import gettext_lazy as _

from .models import Absence


class AbsenceForm(forms.ModelForm):
    class Meta:
        model = Absence
        fields = ["start_datetime", "end_datetime", "reason"]
        widgets = {
            "start_datetime": forms.DateTimeInput(attrs={"type": "datetime-local"}),
            "end_datetime": forms.DateTimeInput(attrs={"type": "datetime-local"}),
        }

    def clean(self):
        cleaned = super().clean()
        start, end = cleaned.get("start_datetime"), cleaned.get("end_datetime")
        if start and end and end <= start:
            raise forms.ValidationError(_("End must be after start."))
        return cleaned
