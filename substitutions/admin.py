from django.contrib import admin
from django.template.response import TemplateResponse
from django.urls import path
from django.utils.translation import gettext_lazy as _

from .models import Absence, Substitution, SubstitutionOffer
from .stats import build_admin_stats


@admin.register(Absence)
class AbsenceAdmin(admin.ModelAdmin):
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
            "stats": build_admin_stats(),
        }
        return TemplateResponse(request, "substitutions/admin/stats_dashboard.html", context)


@admin.register(Substitution)
class SubstitutionAdmin(admin.ModelAdmin):
    list_display = ["absence", "substitute_teacher", "start_datetime", "end_datetime", "confirmed_at"]


@admin.register(SubstitutionOffer)
class SubstitutionOfferAdmin(admin.ModelAdmin):
    list_display = [
        "absence", "substitute_teacher", "start_datetime", "end_datetime",
        "status", "decline_reason", "created_at", "responded_at",
    ]
    list_filter = ["status", "decline_reason", "substitute_teacher"]
