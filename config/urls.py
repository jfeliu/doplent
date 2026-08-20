from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.http import HttpResponse
from django.urls import include, path
from django.utils.translation import gettext_lazy as _

admin.site.site_header = _("Doplent admin")
admin.site.site_title = _("Doplent admin")
admin.site.index_title = _("Roster & imports")

# Served at the site root (not under /static/) so its default scope covers
# the whole app - a service worker registered from /static/sw.js would only
# be allowed to control /static/*.
_SERVICE_WORKER_JS = """
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (event) => event.waitUntil(self.clients.claim()));
self.addEventListener("fetch", (event) => event.respondWith(fetch(event.request)));
""".strip()


def service_worker(request):
    return HttpResponse(
        _SERVICE_WORKER_JS,
        content_type="text/javascript",
        headers={"Service-Worker-Allowed": "/"},
    )


urlpatterns = [
    path("admin/", admin.site.urls),
    path("sw.js", service_worker, name="service_worker"),
    path("i18n/", include("django.conf.urls.i18n")),
    path("login/", auth_views.LoginView.as_view(), name="login"),
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("teachers/", include("teachers.urls")),
    path("", include("substitutions.urls")),
]
