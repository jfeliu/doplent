# Generated manually.
#
# Teacher.role (added in 0008) replaces the old is_staff-based "can manage
# this school's roster/timetable" check (see schools.decorators). Without
# this backfill, every teacher whose Django user already had is_staff=True
# would silently lose that ability the moment this deploys.

from django.db import migrations


def backfill_staff_role(apps, schema_editor):
    Teacher = apps.get_model("teachers", "Teacher")
    Teacher.objects.filter(user__is_staff=True).update(role="staff")


def undo(apps, schema_editor):
    Teacher = apps.get_model("teachers", "Teacher")
    Teacher.objects.filter(user__is_staff=True, role="staff").update(role="member")


class Migration(migrations.Migration):

    dependencies = [
        ("teachers", "0008_nonteachinghourspriority_school_teacher_role_and_more"),
    ]

    operations = [
        migrations.RunPython(backfill_staff_role, undo),
    ]
