"""Remove what doses replace: starting plans in months and strengths, how often a product's dose is taken, and the
practice-wide plan prices and delivery interval. A care plan's price is now its dose's."""

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [("care", "0028_medication_doses_data")]

    operations = [
        migrations.RemoveField(model_name="startingplan", name="then_product"),
        migrations.RemoveField(model_name="startingplan", name="default_months"),
        migrations.RemoveField(model_name="startingplanstep", name="product"),
        migrations.RemoveField(model_name="startingplanstep", name="months"),
        migrations.AlterField(
            model_name="startingplan",
            name="then_dose",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="starting_plans_ending",
                to="care.medicationdose",
            ),
        ),
        migrations.AlterField(
            model_name="startingplanstep",
            name="dose",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="starting_plan_steps",
                to="care.medicationdose",
            ),
        ),
        migrations.AlterField(
            model_name="startingplanstep",
            name="weeks",
            field=models.PositiveSmallIntegerField(),
        ),
        migrations.RemoveField(model_name="medicationproduct", name="dose_frequency"),
        migrations.RemoveField(model_name="practicesettings", name="standard_subscription_amount"),
        migrations.RemoveField(model_name="practicesettings", name="ongoing_subscription_amount"),
        migrations.RemoveField(model_name="practicesettings", name="delivery_interval_days"),
        migrations.RemoveField(model_name="patientsubscription", name="monthly_amount"),
    ]
