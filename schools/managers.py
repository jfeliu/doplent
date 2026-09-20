from django.db import models


class SchoolScopedManager(models.Manager):
    """A plain models.Manager plus `for_school(school)`, so every school-aware
    model can be filtered the same way at the call site
    (`Model.objects.for_school(school)`) even though the actual ORM path to
    School differs per model - a direct `school` FK for some, a lookup through
    another model (e.g. "teacher__school") for others that only reach a
    school transitively. See the "Filtratge de consultes" section of the
    multi-school plan for why these aren't all denormalized onto one `school`
    column instead."""

    def __init__(self, school_field: str = "school"):
        self.school_field = school_field
        super().__init__()

    def for_school(self, school):
        return self.get_queryset().filter(**{self.school_field: school})
