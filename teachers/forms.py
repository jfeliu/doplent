from django import forms
from django.utils.translation import gettext_lazy as _

from .models import NonTeachingHoursKind, Teacher, WeeklyNonTeachingHours


class NonTeachingHoursForm(forms.ModelForm):
    class Meta:
        model = WeeklyNonTeachingHours
        fields = ["weekday", "start_time", "end_time", "kind", "head"]
        widgets = {
            "start_time": forms.TimeInput(attrs={"type": "time"}),
            "end_time": forms.TimeInput(attrs={"type": "time"}),
        }

    def __init__(self, *args, owner=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["head"].label = _("Co-teaching head")
        # Only active teachers can lead a class, and never the owner of the row
        # (a teacher can't be their own co-teaching head).
        head_qs = Teacher.objects.filter(active=True).select_related("user")
        if owner is not None:
            head_qs = head_qs.exclude(pk=owner.pk)
        self.fields["head"].queryset = head_qs

    def clean(self):
        cleaned = super().clean()
        start, end = cleaned.get("start_time"), cleaned.get("end_time")
        if start and end and end <= start:
            raise forms.ValidationError(_("End time must be after start time."))
        if cleaned.get("kind") == NonTeachingHoursKind.CO_TEACHING and not cleaned.get("head"):
            self.add_error("head", _("Pick the teacher who leads this co-taught class."))
        if cleaned.get("kind") != NonTeachingHoursKind.CO_TEACHING:
            cleaned["head"] = None
        return cleaned


NonTeachingHoursFormSet = forms.modelformset_factory(
    WeeklyNonTeachingHours, form=NonTeachingHoursForm, extra=2, can_delete=True
)


class TeacherCSVImportForm(forms.Form):
    csv_file = forms.FileField(label=_("CSV file"))
