from functools import wraps

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import Http404


def _current_teacher(request):
    """The requesting user's Teacher row, or None if they aren't logged in as
    one (no linked Teacher, e.g. a superuser-only account)."""
    return getattr(request.user, "teacher", None)


def same_school_required(view_func):
    """Requires a resolved `request.school` and a logged-in user whose own
    Teacher belongs to it - any role. Use for pages any teacher at a school
    may use (report an absence, respond to an offer, ...), where the only
    thing to guard against is a teacher from one school reaching another
    school's data once schools are told apart by subdomain."""

    @wraps(view_func)
    @login_required
    def wrapper(request, *args, **kwargs):
        if request.school is None:
            raise Http404
        teacher = _current_teacher(request)
        if teacher is None or teacher.school_id != request.school.pk:
            raise PermissionDenied
        return view_func(request, *args, **kwargs)

    return wrapper


def school_staff_required(view_func):
    """Requires a resolved `request.school` and a logged-in user who is
    either a Django superuser or a STAFF-role Teacher of that school. Use for
    pages that manage a school's schedule/roster - the in-app replacement for
    Django's global @staff_member_required, which knew nothing about which
    school a "staff" user was staff of."""

    @wraps(view_func)
    @login_required
    def wrapper(request, *args, **kwargs):
        if request.school is None:
            raise Http404
        if request.user.is_superuser:
            return view_func(request, *args, **kwargs)
        teacher = _current_teacher(request)
        if teacher is None or teacher.school_id != request.school.pk:
            raise PermissionDenied
        from teachers.models import Teacher  # avoid a circular import at module load

        if teacher.role != Teacher.Role.STAFF:
            raise PermissionDenied
        return view_func(request, *args, **kwargs)

    return wrapper
