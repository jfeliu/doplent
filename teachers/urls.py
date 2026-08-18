from django.urls import path

from . import views

urlpatterns = [
    path("schedule/", views.edit_schedule, name="edit_schedule"),
]
