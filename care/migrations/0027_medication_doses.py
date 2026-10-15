"""Doses in the Pharmacy catalogue: what a doctor prescribes, separate from the pen the dispensary sends.

Adds the dose table and the new dose and week fields next to the old product and month ones. 0028 fills them from
the existing catalogue and 0029 removes the old fields.
"""

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("care", "0026_seed_starting_plans"),
        ("practices", "0004_patient_title"),
    ]

    operations = [
        migrations.CreateModel(
            name="MedicationDose",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("amount_mg", models.DecimalField(decimal_places=3, max_digits=7)),
                (
                    "frequency",
                    models.CharField(
                        choices=[("weekly", "Weekly"), ("daily", "Daily"), ("as_directed", "As directed")],
                        default="weekly",
                        max_length=16,
                    ),
                ),
                ("pens_per_dispatch", models.PositiveSmallIntegerField(default=1)),
                ("dispatch_weeks", models.PositiveSmallIntegerField(default=4)),
                ("price", models.DecimalField(blank=True, decimal_places=2, max_digits=10, null=True)),
                ("billing_weeks", models.PositiveSmallIntegerField(default=4)),
                (
                    "tier",
                    models.CharField(
                        choices=[("standard", "Standard"), ("step_up", "Step-up"), ("if_necessary", "Only if necessary")],
                        default="standard",
                        max_length=16,
                    ),
                ),
                ("is_active", models.BooleanField(default=True)),
                (
                    "company",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="%(app_label)s_%(class)s_records",
                        to="practices.company",
                    ),
                ),
                (
                    "pen",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="doses",
                        to="care.medicationproduct",
                    ),
                ),
            ],
            options={
                "ordering": ("pen__name", "amount_mg", "pk"),
            },
        ),
        migrations.AddField(
            model_name="treatmentauthorization",
            name="dose",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="authorizations",
                to="care.medicationdose",
            ),
        ),
        migrations.AddField(
            model_name="startingplan",
            name="then_dose",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="starting_plans_ending",
                to="care.medicationdose",
            ),
        ),
        migrations.AddField(
            model_name="startingplan",
            name="default_weeks",
            field=models.PositiveSmallIntegerField(default=24),
        ),
        migrations.AddField(
            model_name="startingplanstep",
            name="dose",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="starting_plan_steps",
                to="care.medicationdose",
            ),
        ),
        migrations.AddField(
            model_name="startingplanstep",
            name="weeks",
            field=models.PositiveSmallIntegerField(default=4),
        ),
    ]
