import django.db.models.deletion
from django.db import migrations, models


def backfill_substitution_range(apps, schema_editor):
    Substitution = apps.get_model("substitutions", "Substitution")
    for substitution in Substitution.objects.select_related("absence"):
        substitution.start_datetime = substitution.absence.start_datetime
        substitution.end_datetime = substitution.absence.end_datetime
        substitution.save(update_fields=["start_datetime", "end_datetime"])


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('substitutions', '0002_alter_absence_options_alter_substitution_options_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='substitution',
            name='start_datetime',
            field=models.DateTimeField(null=True, verbose_name='covers from'),
        ),
        migrations.AddField(
            model_name='substitution',
            name='end_datetime',
            field=models.DateTimeField(null=True, verbose_name='covers until'),
        ),
        migrations.RunPython(backfill_substitution_range, noop),
        migrations.AlterField(
            model_name='substitution',
            name='start_datetime',
            field=models.DateTimeField(verbose_name='covers from'),
        ),
        migrations.AlterField(
            model_name='substitution',
            name='end_datetime',
            field=models.DateTimeField(verbose_name='covers until'),
        ),
        migrations.AlterField(
            model_name='substitution',
            name='absence',
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE, related_name='substitutions', to='substitutions.absence'
            ),
        ),
        migrations.AlterModelOptions(
            name='substitution',
            options={'ordering': ['start_datetime'], 'verbose_name': 'substitution', 'verbose_name_plural': 'substitutions'},
        ),
        migrations.AddConstraint(
            model_name='substitution',
            constraint=models.CheckConstraint(
                condition=models.Q(('end_datetime__gt', models.F('start_datetime'))), name='substitution_end_after_start'
            ),
        ),
    ]
