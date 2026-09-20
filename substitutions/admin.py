from django.contrib import admin
from django.template.response import TemplateResponse
from django.urls import path
from django.utils.translation import gettext_lazy as _

from schools.admin import SchoolScopedAdminMixin
from schools.models import get_default_school

from .models import Absence, Substitution, SubstitutionOffer
from .stats import build_admin_stats


def _admin_stats_school(request):
    """The school AbsenceAdmin's stats dashboard reports on: the requesting
    staff user's own school, or (for a superuser with no linked Teacher, who
    isn't "of" any one school) the root school, as a reasonable default."""
    teacher = getattr(request.user, "teacher", None)
    return teacher.school if teacher else get_default_school()


@admin.register(Absence)
class AbsenceAdmin(SchoolScopedAdminMixin, admin.ModelAdmin):
    school_field_path = "teacher__school"
    list_display = ["teacher", "start_datetime", "end_datetime", "reason"]
    list_filter = ["teacher"]
    change_list_template = "substitutions/admin/absence_change_list.html"

    def get_urls(self):
        custom = [
            path(
                "stats/",
                self.admin_site.admin_view(self.stats_dashboard),
                name="substitutions_stats",
            ),
        ]
        return custom + super().get_urls()

    def stats_dashboard(self, request):
        context = {
            **self.admin_site.each_context(request),
            "title": _("Stats dashboard"),
            "opts": self.model._meta,
            "stats": build_admin_stats(_admin_stats_school(request)),
        }
        return TemplateResponse(request, "substitutions/admin/stats_dashboard.html", context)


@admin.register(Substitution)
class SubstitutionAdmin(SchoolScopedAdminMixin, admin.ModelAdmin):
    school_field_path = "absence__teacher__school"
    list_display = ["absence", "substitute_teacher", "start_datetime", "end_datetime", "confirmed_at"]


@admin.register(SubstitutionOffer)
class SubstitutionOfferAdmin(SchoolScopedAdminMixin, admin.ModelAdmin):
    school_field_path = "absence__teacher__school"
    list_display = [
        "absence", "substitute_teacher", "start_datetime", "end_datetime",
        "status", "decline_reason", "created_at", "responded_at",
    ]
    list_filter = ["status", "decline_reason", "substitute_teacher"]
