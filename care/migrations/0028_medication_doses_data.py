"""Fill the doses from the catalogue as it was, so every current prescription and plan works exactly as before.

Each main medicine strength was prescribed as itself, so each becomes a pen with that one dose: its strength, how
often it was taken and its price, one pen at a time, sent and billed as often as the practice delivered (28 days is
4 weeks). Prescriptions of those products point at their dose; starting plans move from months to weeks (a month is
4 weeks). A Super Admin then adds the real pens and doses in the Pharmacy catalogue.
"""

import re
from decimal import Decimal

from django.db import migrations

FREQUENCIES = {'weekly', 'daily', 'as_directed'}


def _mg(strength):
    match = re.search(r'\d+(?:\.\d+)?', strength or '')
    return Decimal(match.group()) if match else Decimal('0')


def forwards(apps, schema_editor):
    MedicationProduct = apps.get_model('care', 'MedicationProduct')
    MedicationDose = apps.get_model('care', 'MedicationDose')
    PracticeSettings = apps.get_model('care', 'PracticeSettings')
    StartingPlan = apps.get_model('care', 'StartingPlan')
    StartingPlanStep = apps.get_model('care', 'StartingPlanStep')
    TreatmentAuthorization = apps.get_model('care', 'TreatmentAuthorization')

    weeks_by_company = {
        company_id: max(1, round(days / 7))
        for company_id, days in PracticeSettings.objects.values_list('company_id', 'delivery_interval_days')
    }
    doses = {}

    def dose_for(product):
        if product.pk not in doses:
            weeks = weeks_by_company.get(product.company_id, 4)
            doses[product.pk] = MedicationDose.objects.create(
                company_id=product.company_id, pen=product, amount_mg=_mg(product.strength),
                frequency=product.dose_frequency if product.dose_frequency in FREQUENCIES else 'as_directed',
                pens_per_dispatch=1, dispatch_weeks=weeks, price=product.price, billing_weeks=weeks,
            )
        return doses[product.pk]

    for product in MedicationProduct.objects.filter(category='weight_management', requires_authorisation=True).order_by('pk'):
        dose_for(product)
    for plan in StartingPlan.objects.select_related('then_product').order_by('pk'):
        plan.then_dose = dose_for(plan.then_product)
        plan.default_weeks = plan.default_months * 4
        plan.save(update_fields=('then_dose', 'default_weeks'))
    for step in StartingPlanStep.objects.select_related('product').order_by('pk'):
        step.dose = dose_for(step.product)
        step.weeks = step.months * 4
        step.save(update_fields=('dose', 'weeks'))
    for product_id, dose in doses.items():
        TreatmentAuthorization.objects.filter(product_id=product_id).update(dose=dose)


class Migration(migrations.Migration):
    dependencies = [('care', '0027_medication_doses')]

    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
