from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('care', '0003_general_tasks_and_record_tags')]

    operations = [
        migrations.AddField(
            model_name='lead',
            name='submission_key',
            field=models.UUIDField(blank=True, editable=False, null=True, unique=True),
        ),
    ]
