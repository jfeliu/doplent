from django.urls import path

from . import views

urlpatterns = [
    path("", views.group_list, name="group_list"),
    path("teachers/", views.teacher_list, name="teacher_list"),
    path("teachers/<int:teacher_id>/", views.teacher_calendar, name="teacher_calendar"),
    path("<int:group_id>/", views.group_schedule, name="group_schedule"),
    path("<int:group_id>/entries/selected/edit/", views.edit_selected, name="edit_selected_schedule_entries"),
    path("<int:group_id>/entries/selected/delete/", views.delete_selected, name="delete_selected_schedule_entries"),
    path("<int:group_id>/entries/<int:entry_id>/delete/", views.delete_entry, name="delete_schedule_entry"),
    path("<int:group_id>/entries/<int:entry_id>/edit/", views.edit_entry, name="edit_schedule_entry"),
]
