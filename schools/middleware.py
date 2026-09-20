from django.conf import settings

from .models import School


def resolve_school(host: str) -> School | None:
    """The School served at `host` (a request's Host header, port already
    stripped by the caller): the root school (subdomain=None) if `host` is
    exactly settings.BASE_DOMAIN, the school at that subdomain if `host` is
    "<subdomain>.<BASE_DOMAIN>", or None if `host` doesn't belong to this
    deployment's domain at all (or names an unknown subdomain).

    "www.<BASE_DOMAIN>" is treated as an alias for the bare base domain (the
    root school), not as a school named "www" - production's DJANGO_ALLOWED_HOSTS
    already accepts both doplent.feliuet.com and www.doplent.feliuet.com, and
    without this alias the "www." host would otherwise match the "<subdomain>.
    <BASE_DOMAIN>" case below and 404 as an unknown school.

    "testserver" - the fixed Host Django's test client sends unless a test
    overrides it - is treated as the base domain too, so existing tests that
    don't care about multi-school routing keep working against the root
    school without every one of them setting a Host header by hand. With
    DEBUG on, "localhost"/"127.0.0.1" are treated the same way, so
    `manage.py runserver` works against the root school out of the box
    without needing DJANGO_BASE_DOMAIN set for local dev."""
    base_domain = settings.BASE_DOMAIN
    root_aliases = {base_domain, "www." + base_domain, "testserver"}
    if settings.DEBUG:
        root_aliases |= {"localhost", "127.0.0.1"}
    if host in root_aliases:
        return School.objects.filter(subdomain__isnull=True).first()
    suffix = "." + base_domain
    if host.endswith(suffix):
        subdomain = host[: -len(suffix)]
        return School.objects.filter(subdomain=subdomain).first()
    return None


class CurrentSchoolMiddleware:
    """Resolves the School for the current request from its Host header and
    attaches it as `request.school` (None if the host matches no school -
    views/templates that require a school should treat that as a 404, except
    /admin/, which stays reachable from any host so a superuser can manage
    every school from one place)."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        host = request.get_host().split(":")[0]
        request.school = resolve_school(host)
        return self.get_response(request)
