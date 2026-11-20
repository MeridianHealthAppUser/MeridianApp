from django.db import migrations


def send_to_dr_czech(apps, schema_editor):
    """Dr Czech asked for the monthly activity report at this address (Nov 2026); it can be changed in Insights."""
    apps.get_model('care', 'PracticeSettings').objects.filter(activity_report_email='').update(
        activity_report_email='drjczech@gmail.com')


class Migration(migrations.Migration):
    dependencies = [('care', '0057_insights_and_activity_reports')]
    operations = [migrations.RunPython(send_to_dr_czech, migrations.RunPython.noop)]
