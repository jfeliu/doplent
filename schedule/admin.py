from django import forms
from django.contrib import admin
from django.core.exceptions import ValidationError
from django.forms.widgets import SelectMultiple
from django.urls import reverse
from django.utils.html import format_html, format_html_join
from django.utils.translation import gettext_lazy as _

from schools.admin import SchoolScopedAdminMixin
from teachers.models import Teacher

from .models import MAX_TUTORS, ClassGroup, ScheduleEntry, Subject, TeacherSubject


class TutorSlotsWidget(SelectMultiple):
    """Renders `max_slots` independent <select> dropdowns sharing one field
    name, instead of SelectMultiple's single multi-select listbox - each
    tutor is then picked from its own dropdown. Submission still works
    exactly like SelectMultiple (multiple values under one name, read back
    via `data.getlist`), so no other field/form plumbing is needed - only
    empty (unselected) slots are stripped before they reach the field's
    `clean()`, which otherwise can't render for a "no choice made" slot.

    Only the class group's `tutor_count` slots start visible - see
    static/schedule/admin/tutor_slots.js, which shows/hides the rest as that
    field's value changes."""

    template_name = None  # rendered by hand below, not through a template

    def __init__(self, *args, max_slots=MAX_TUTORS, **kwargs):
        self.max_slots = max_slots
        super().__init__(*args, **kwargs)

    def value_from_datadict(self, data, files, name):
        values = super().value_from_datadict(data, files, name) or []
        return [value for value in values if value]

    def render(self, name, value, attrs=None, renderer=None):
        selected = [str(v) for v in (value or [])]
        selects = format_html_join(
            "",
            "{}",
            ((self._render_select(name, selected[index] if index < len(selected) else "", index),) for index in range(self.max_slots)),
        )
        return format_html('<div class="tutor-slots">{}</div>', selects)

    def _render_select(self, name, selected_value, index):
        options = format_html_join(
            "",
            '<option value="{}"{}>{}</option>',
            (
                (choice_value, " selected" if selected_value and str(choice_value) == selected_value else "", label)
                for choice_value, label in self.choices
            ),
        )
        return format_html(
            '<select name="{}" class="tutor-slot-select"{}><option value="">{}</option>{}</select> ',
            name, " hidden" if index != 0 else "", _("— none —"), options,
        )


class ScheduleEntryInlineForm(forms.ModelForm):
    """ScheduleEntry.clean() (run via _post_clean below) already checks the
    main teacher for conflicts, but co_teachers is a ManyToManyField and
    can't be validated there - construct_instance() skips m2m fields, and
    the row has no pk yet to check against for a new entry. So co_teachers
    gets its own pass here, once _post_clean has populated the instance's
    other fields."""

    class Meta:
        model = ScheduleEntry
        fields = "__all__"

    def _post_clean(self):
        super()._post_clean()
        co_teachers = self.cleaned_data.get("co_teachers")
        entry = self.instance
        if not co_teachers or self.cleaned_data.get("DELETE"):
            return
        if not entry.teacher_id or entry.weekday is None or not entry.start_time or not entry.end_time:
            return
        if entry.start_time >= entry.end_time:
            return
        try:
            entry.validate_co_teachers([teacher.pk for teacher in co_teachers])
        except ValidationError as exc:
            # validate_co_teachers raises a dict-keyed ValidationError (it
            # doubles as the model-level error shape used elsewhere); add_error
            # rejects that combined with an explicit field, so re-key it via
            # .messages instead of passing `exc` through directly.
            self.add_error("co_teachers", exc.messages)


class ScheduleEntryInline(SchoolScopedAdminMixin, admin.TabularInline):
    model = ScheduleEntry
    form = ScheduleEntryInlineForm
    fk_name = "class_group"
    fields = ["weekday", "start_time", "end_time", "subject", "teacher", "co_teachers"]
    filter_horizontal = ["co_teachers"]
    extra = 2

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        if db_field.name in ("subject", "teacher"):
            model = Subject if db_field.name == "subject" else Teacher
            kwargs["queryset"] = self.scope_to_school(model.objects.all(), request)
        return super().formfield_for_foreignkey(db_field, request, **kwargs)

    def formfield_for_manytomany(self, db_field, request, **kwargs):
        if db_field.name == "co_teachers":
            kwargs["queryset"] = self.scope_to_school(Teacher.objects.all(), request)
        return super().formfield_for_manytomany(db_field, request, **kwargs)


@admin.register(Subject)
class SubjectAdmin(SchoolScopedAdminMixin, admin.ModelAdmin):
    list_display = ["name"]
    search_fields = ["name"]


@admin.register(ClassGroup)
class ClassGroupAdmin(SchoolScopedAdminMixin, admin.ModelAdmin):
    list_display = ["name", "grade_level", "tutors_label", "schedule_link"]
    list_filter = ["grade_level"]
    fields = ["name", "grade_level", "tutor_count", "tutors"]
    inlines = [ScheduleEntryInline]

    class Media:
        js = ["schedule/admin/tutor_slots.js"]

    def get_queryset(self, request):
        return super().get_queryset(request).prefetch_related("tutors__user")

    def formfield_for_manytomany(self, db_field, request, **kwargs):
        if db_field.name == "tutors":
            # Not filtered to active=True: a tutor who is later deactivated
            # must stay pickable here, or saving the group for any unrelated
            # reason would silently drop them (no matching <option> means the
            # browser submits nothing for their slot). Active teachers still
            # sort first, and inactive ones are labelled, for convenience.
            kwargs["queryset"] = self.scope_to_school(Teacher.objects.all(), request).select_related(
                "user"
            ).order_by("-active", "user__last_name", "user__first_name")
            kwargs["widget"] = TutorSlotsWidget()
        field = super().formfield_for_manytomany(db_field, request, **kwargs)
        if db_field.name == "tutors":
            field.label_from_instance = lambda teacher: (
                str(teacher) if teacher.active else f"{teacher} ({_('inactive')})"
            )
        return field

    @admin.display(description=_("tutors"))
    def tutors_label(self, obj):
        return ", ".join(str(tutor) for tutor in obj.tutors.all()) or "—"

    @admin.display(description=_("timetable"))
    def schedule_link(self, obj):
        return format_html('<a href="{}">{}</a>', reverse("group_schedule", args=[obj.pk]), _("View / edit"))


class TeacherSubjectInline(SchoolScopedAdminMixin, admin.TabularInline):
    """Registered onto TeacherAdmin from teachers/admin.py - not here, to keep
    that admin page's layout in one place."""

    model = TeacherSubject
    extra = 1

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        if db_field.name == "subject":
            kwargs["queryset"] = self.scope_to_school(Subject.objects.all(), request)
        return super().formfield_for_foreignkey(db_field, request, **kwargs)
