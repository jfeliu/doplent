from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render

from .forms import NonTeachingHoursFormSet
from .models import Teacher


@login_required
def edit_schedule(request):
    teacher = get_object_or_404(Teacher, user=request.user)
    queryset = teacher.non_teaching_hours.all()

    if request.method == "POST":
        formset = NonTeachingHoursFormSet(request.POST, queryset=queryset)
        if formset.is_valid():
            instances = formset.save(commit=False)
            for instance in instances:
                instance.teacher = teacher
                instance.save()
            for obj in formset.deleted_objects:
                obj.delete()
            return redirect("dashboard")
    else:
        formset = NonTeachingHoursFormSet(queryset=queryset)

    return render(request, "teachers/schedule.html", {"formset": formset})
