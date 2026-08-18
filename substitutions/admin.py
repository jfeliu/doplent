from django.contrib import admin

from .models import Absence, Substitution


@admin.register(Absence)
class AbsenceAdmin(admin.ModelAdmin):
    list_display = ["teacher", "start_datetime", "end_datetime", "reason"]
    list_filter = ["teacher"]


@admin.register(Substitution)
class SubstitutionAdmin(admin.ModelAdmin):
    list_display = ["absence", "substitute_teacher", "start_datetime", "end_datetime", "confirmed_at"]
