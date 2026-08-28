from django.db import migrations, models

# Seeded order: gaps of 10 so the admin can slot a new kind between two
# existing ones without renumbering.
DEFAULT_PRIORITIES = [
    ("free", 0),
    ("paperwork", 10),
    ("co_teaching", 20),
    ("escoltam", 30),
]


def seed_and_migrate(apps, schema_editor):
    NonTeachingHoursPriority = apps.get_model("teachers", "NonTeachingHoursPriority")
    WeeklyNonTeachingHours = apps.get_model("teachers", "WeeklyNonTeachingHours")

    NonTeachingHoursPriority.objects.bulk_create(
        [NonTeachingHoursPriority(kind=kind, priority=priority) for kind, priority in DEFAULT_PRIORITIES]
    )
    WeeklyNonTeachingHours.objects.filter(is_paperwork=True).update(kind="paperwork")


def undo(apps, schema_editor):
    NonTeachingHoursPriority = apps.get_model("teachers", "NonTeachingHoursPriority")
    WeeklyNonTeachingHours = apps.get_model("teachers", "WeeklyNonTeachingHours")

    WeeklyNonTeachingHours.objects.filter(kind="paperwork").update(is_paperwork=True)
    NonTeachingHoursPriority.objects.all().delete()


class Migration(migrations.Migration):

    dependencies = [
        ("teachers", "0003_alter_teacher_options_and_more"),
    ]

    operations = [
        migrations.CreateModel(
            name="NonTeachingHoursPriority",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                (
                    "kind",
                    models.CharField(
                        choices=[
                            ("free", "Free"),
                            ("paperwork", "Paperwork"),
                            ("co_teaching", "Co-teaching"),
                            ("escoltam", "Escolta'm"),
                        ],
                        max_length=20,
                        unique=True,
                        verbose_name="kind",
                    ),
                ),
                (
                    "priority",
                    models.PositiveIntegerField(help_text="Lower is pulled first.", verbose_name="priority"),
                ),
            ],
            options={
                "verbose_name": "non-teaching hours priority",
                "verbose_name_plural": "non-teaching hours priorities",
                "ordering": ["priority"],
            },
        ),
        migrations.AddField(
            model_name="weeklynonteachinghours",
            name="kind",
            field=models.CharField(
                choices=[
                    ("free", "Free"),
                    ("paperwork", "Paperwork"),
                    ("co_teaching", "Co-teaching"),
                    ("escoltam", "Escolta'm"),
                ],
                default="free",
                max_length=20,
                verbose_name="kind",
            ),
        ),
        migrations.RunPython(seed_and_migrate, undo),
        migrations.RemoveField(
            model_name="weeklynonteachinghours",
            name="is_paperwork",
        ),
    ]
