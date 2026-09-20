from django.contrib import admin
from django.core.exceptions import PermissionDenied
from django.http import HttpResponse
from django.urls import path
from django.shortcuts import render

from schedule.admin import TeacherSubjectInline
from schools.admin import SchoolScopedAdminMixin
from schools.models import get_default_school

from .calendar import build_week_calendar
from .forms import TeacherCSVImportForm
from .importer import CSV_TEMPLATE, export_teachers_to_csv, import_teachers_from_csv
from .models import NonTeachingHoursPriority, Teacher, WeeklyNonTeachingHours


class WeeklyNonTeachingHoursInline(SchoolScopedAdminMixin, admin.TabularInline):
    model = WeeklyNonTeachingHours
    fk_name = "teacher"
    fields = ["weekday", "start_time", "end_time", "kind", "head"]
    extra = 2

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        if db_field.name == "head":
            kwargs["queryset"] = self.scope_to_school(Teacher.objects.all(), request)
        return super().formfield_for_foreignkey(db_field, request, **kwargs)


@admin.register(NonTeachingHoursPriority)
class NonTeachingHoursPriorityAdmin(SchoolScopedAdminMixin, admin.ModelAdmin):
    list_display = ["kind", "priority"]
    list_editable = ["priority"]
    ordering = ["priority"]

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Teacher)
class TeacherAdmin(SchoolScopedAdminMixin, admin.ModelAdmin):
    list_display = ["__str__", "grade_level", "active"]
    list_filter = ["grade_level", "active"]
    inlines = [WeeklyNonTeachingHoursInline, TeacherSubjectInline]
    change_list_template = "teachers/admin/teacher_change_list.html"

    def get_urls(self):
        custom_urls = [
            path(
                "import-csv/",
                self.admin_site.admin_view(self.import_csv),
                name="teachers_teacher_import_csv",
            ),
            path(
                "import-csv/template/",
                self.admin_site.admin_view(self.download_csv_template),
                name="teachers_teacher_import_csv_template",
            ),
            path(
                "export-csv/",
                self.admin_site.admin_view(self.export_csv),
                name="teachers_teacher_export_csv",
            ),
            path(
                "weekly-calendar/",
                self.admin_site.admin_view(self.weekly_calendar),
                name="teachers_teacher_weekly_calendar",
            ),
        ]
        return custom_urls + super().get_urls()

    def _import_export_school(self, request):
        """The school CSV import/export and the weekly calendar act on: the
        requesting staff user's own school, or (for a superuser with no
        linked Teacher) the root school, as a reasonable default."""
        return self.admin_school(request) or get_default_school()

    def weekly_calendar(self, request):
        context = {
            **self.admin_site.each_context(request),
            "title": "Weekly non-teaching hours",
            "opts": self.model._meta,
            **build_week_calendar(self._import_export_school(request)),
        }
        return render(request, "teachers/admin/weekly_calendar.html", context)

    def download_csv_template(self, request):
        response = HttpResponse(CSV_TEMPLATE, content_type="text/csv")
        response["Content-Disposition"] = 'attachment; filename="teachers_template.csv"'
        return response

    def export_csv(self, request):
        if not self.has_view_permission(request):
            raise PermissionDenied
        response = HttpResponse(export_teachers_to_csv(self._import_export_school(request)), content_type="text/csv")
        response["Content-Disposition"] = 'attachment; filename="teachers.csv"'
        return response

    def import_csv(self, request):
        if not self.has_add_permission(request):
            raise PermissionDenied

        if request.method == "POST":
            form = TeacherCSVImportForm(request.POST, request.FILES)
            if form.is_valid():
                result = import_teachers_from_csv(request.FILES["csv_file"], self._import_export_school(request))
                context = {
                    **self.admin_site.each_context(request),
                    "title": "Import results",
                    "result": result,
                    "opts": self.model._meta,
                }
                return render(request, "teachers/admin/teacher_import_results.html", context)
        else:
            form = TeacherCSVImportForm()

        context = {
            **self.admin_site.each_context(request),
            "title": "Import teachers from CSV",
            "form": form,
            "opts": self.model._meta,
        }
        return render(request, "teachers/admin/teacher_import_form.html", context)
