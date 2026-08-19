from django.contrib import admin

from .models import Absence, Substitution, SubstitutionOffer


@admin.register(Absence)
class AbsenceAdmin(admin.ModelAdmin):
    list_display = ["teacher", "start_datetime", "end_datetime", "reason"]
    list_filter = ["teacher"]


@admin.register(Substitution)
class SubstitutionAdmin(admin.ModelAdmin):
    list_display = ["absence", "substitute_teacher", "start_datetime", "end_datetime", "confirmed_at"]


@admin.register(SubstitutionOffer)
class SubstitutionOfferAdmin(admin.ModelAdmin):
    list_display = ["absence", "substitute_teacher", "start_datetime", "end_datetime", "status", "created_at", "responded_at"]
    list_filter = ["status", "substitute_teacher"]
