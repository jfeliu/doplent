from django.contrib import admin
from django.core.exceptions import PermissionDenied
from django.http import HttpResponse
from django.urls import path
from django.shortcuts import render

from .calendar import build_week_calendar
from .forms import TeacherCSVImportForm
from .importer import CSV_TEMPLATE, import_teachers_from_csv
from .models import Teacher, WeeklyNonTeachingHours


class WeeklyNonTeachingHoursInline(admin.TabularInline):
    model = WeeklyNonTeachingHours
    extra = 2


@admin.register(Teacher)
class TeacherAdmin(admin.ModelAdmin):
    list_display = ["__str__", "grade_level", "active"]
    list_filter = ["grade_level", "active"]
    inlines = [WeeklyNonTeachingHoursInline]
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
                "weekly-calendar/",
                self.admin_site.admin_view(self.weekly_calendar),
                name="teachers_teacher_weekly_calendar",
            ),
        ]
        return custom_urls + super().get_urls()

    def weekly_calendar(self, request):
        context = {
            **self.admin_site.each_context(request),
            "title": "Weekly non-teaching hours",
            "opts": self.model._meta,
            **build_week_calendar(),
        }
        return render(request, "teachers/admin/weekly_calendar.html", context)

    def download_csv_template(self, request):
        response = HttpResponse(CSV_TEMPLATE, content_type="text/csv")
        response["Content-Disposition"] = 'attachment; filename="teachers_template.csv"'
        return response

    def import_csv(self, request):
        if not self.has_add_permission(request):
            raise PermissionDenied

        if request.method == "POST":
            form = TeacherCSVImportForm(request.POST, request.FILES)
            if form.is_valid():
                result = import_teachers_from_csv(request.FILES["csv_file"])
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
