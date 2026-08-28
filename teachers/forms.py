from django import forms
from django.utils.translation import gettext_lazy as _

from .models import WeeklyNonTeachingHours


class NonTeachingHoursForm(forms.ModelForm):
    class Meta:
        model = WeeklyNonTeachingHours
        fields = ["weekday", "start_time", "end_time", "kind"]
        widgets = {
            "start_time": forms.TimeInput(attrs={"type": "time"}),
            "end_time": forms.TimeInput(attrs={"type": "time"}),
        }

    def clean(self):
        cleaned = super().clean()
        start, end = cleaned.get("start_time"), cleaned.get("end_time")
        if start and end and end <= start:
            raise forms.ValidationError(_("End time must be after start time."))
        return cleaned


NonTeachingHoursFormSet = forms.modelformset_factory(
    WeeklyNonTeachingHours, form=NonTeachingHoursForm, extra=2, can_delete=True
)


class TeacherCSVImportForm(forms.Form):
    csv_file = forms.FileField(label=_("CSV file"))
