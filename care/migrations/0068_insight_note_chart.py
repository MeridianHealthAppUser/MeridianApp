# Each Insights note belongs to one chart (Dr Czech, 26 Nov 2026). The notes made before then marked every chart; he
# will add them again on the chart each is about, so they are removed here.

from django.db import migrations, models


def remove_unplaced_notes(apps, schema_editor):
    apps.get_model('care', 'InsightNote').objects.all().delete()


class Migration(migrations.Migration):

    dependencies = [
        ("care", "0067_order_cutoff_time"),
    ]

    operations = [
        migrations.RunPython(remove_unplaced_notes, migrations.RunPython.noop),
        migrations.AddField(
            model_name="insightnote",
            name="chart",
            field=models.CharField(
                choices=[
                    ("bookings", "Bookings per month"),
                    ("sources", "Sources of bookings"),
                    ("signups", "New subscriptions per month"),
                    ("mrr", "Monthly recurring revenue"),
                    ("dropouts", "Dropouts per month"),
                    ("churn", "Churn rate"),
                    ("doctors", "Each doctor, per month"),
                    ("retention", "Retention at a fixed month"),
                ],
                default="bookings",
                max_length=16,
            ),
            preserve_default=False,
        ),
    ]
