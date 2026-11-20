from django.db import migrations, models


def clear_unrecoverable_legacy_time_flags(apps, schema_editor):
    # Older rows discarded their actual time. Do not invent midnight or end-of-day.
    apps.get_model("radar", "Lead").objects.filter(deadline_at__isnull=True).update(deadline_has_time=False)


class Migration(migrations.Migration):
    dependencies = [("radar", "0001_initial")]

    operations = [
        migrations.AddField(
            model_name="lead",
            name="deadline_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.RunPython(clear_unrecoverable_legacy_time_flags, migrations.RunPython.noop),
    ]
