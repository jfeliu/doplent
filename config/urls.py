from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import include, path
from django.utils.translation import gettext_lazy as _

admin.site.site_header = _("Doplent admin")
admin.site.site_title = _("Doplent admin")
admin.site.index_title = _("Roster & imports")

urlpatterns = [
    path("admin/", admin.site.urls),
    path("i18n/", include("django.conf.urls.i18n")),
    path("login/", auth_views.LoginView.as_view(), name="login"),
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("teachers/", include("teachers.urls")),
    path("", include("substitutions.urls")),
]
