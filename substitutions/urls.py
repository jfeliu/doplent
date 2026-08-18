from django.urls import path

from . import views

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("absences/new/", views.report_absence, name="report_absence"),
    path("absences/<int:absence_id>/pick-substitute/", views.pick_substitute, name="pick_substitute"),
]
