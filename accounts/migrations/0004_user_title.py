from django.db import migrations, models


def doctors_default_to_dr(apps, schema_editor):
    """Staff accounts that are doctors start with the title Dr."""
    User = apps.get_model('accounts', 'User')
    User.objects.filter(title='', company_memberships__clinician_type='doctor').update(title='DR')


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0003_pathcare_details"),
        ("practices", "0004_patient_title"),
    ]

    operations = [
        migrations.AddField(
            model_name="user",
            name="title",
            field=models.CharField(
                blank=True,
                choices=[("MR", "Mr"), ("MRS", "Mrs"), ("MS", "Ms"), ("MISS", "Miss"), ("DR", "Dr"), ("PROF", "Prof")],
                max_length=8,
            ),
        ),
        migrations.RunPython(doctors_default_to_dr, migrations.RunPython.noop),
    ]
