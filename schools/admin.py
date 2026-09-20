from django.contrib import admin

from .models import School


class SchoolScopedAdminMixin:
    """Mixin for ModelAdmin classes whose model reaches a School, directly or
    transitively. Restricts the change-list queryset (and, via
    `scope_to_school`, any FK/M2M pickers) to the current staff user's own
    school; superusers see and can pick from every school.

    `school_field_path` is the ORM lookup from this admin's own model to
    School - "school" for a model with a direct FK (the default), or e.g.
    "teacher__school" for one that only reaches a school transitively."""

    school_field_path = "school"

    def admin_school(self, request):
        """The school a non-superuser admin user acts as. None means either
        "every school" (a superuser) or "no school" (a staff user with no
        linked Teacher) - callers tell those apart via `request.user.is_superuser`."""
        if request.user.is_superuser:
            return None
        teacher = getattr(request.user, "teacher", None)
        return teacher.school if teacher else None

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        if request.user.is_superuser:
            return qs
        school = self.admin_school(request)
        if school is None:
            return qs.none()
        return qs.filter(**{self.school_field_path: school})

    def scope_to_school(self, queryset, request, field_path="school"):
        """Restrict `queryset` (of some other school-scoped model) to the
        current admin school - for use inside formfield_for_foreignkey/
        formfield_for_manytomany overrides. Returns `queryset` unchanged for
        a superuser."""
        if request.user.is_superuser:
            return queryset
        school = self.admin_school(request)
        if school is None:
            return queryset.none()
        return queryset.filter(**{field_path: school})


@admin.register(School)
class SchoolAdmin(admin.ModelAdmin):
    list_display = ["name", "subdomain"]
    prepopulated_fields = {"subdomain": ("name",)}

    def save_model(self, request, obj, form, change):
        is_new = obj.pk is None
        super().save_model(request, obj, form, change)
        if is_new:
            self._seed_non_teaching_hours_priorities(obj)

    @staticmethod
    def _seed_non_teaching_hours_priorities(school):
        from teachers.models import DEFAULT_NON_TEACHING_HOURS_PRIORITIES, NonTeachingHoursPriority

        NonTeachingHoursPriority.objects.bulk_create(
            [
                NonTeachingHoursPriority(school=school, kind=kind, priority=priority)
                for kind, priority in DEFAULT_NON_TEACHING_HOURS_PRIORITIES
            ]
        )
