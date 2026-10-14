"""Dr Czech's two standard starting plans, for any practice whose catalogue already has their strengths.

Semaglutide 0.25 mg weekly for 1 month, then 0.5 mg weekly for 1 month, then 1 mg weekly; Tirzepatide 2.5 mg
weekly for 1 month, then 5 mg weekly. A practice without those strengths gets none: a Super Admin adds the
strengths and plans in the Pharmacy catalogue.
"""

import re
from decimal import Decimal

from django.db import migrations

STANDARDS = (
    ('Semaglutide', ((Decimal('0.25'), 1), (Decimal('0.5'), 1)), Decimal('1')),
    ('Tirzepatide', ((Decimal('2.5'), 1),), Decimal('5')),
)


def _mg(product):
    match = re.search(r'\d+(?:\.\d+)?', product.strength or '')
    return Decimal(match.group()) if match else None


def seed(apps, schema_editor):
    Company = apps.get_model('practices', 'Company')
    MedicationProduct = apps.get_model('care', 'MedicationProduct')
    StartingPlan = apps.get_model('care', 'StartingPlan')
    StartingPlanStep = apps.get_model('care', 'StartingPlanStep')
    for company in Company.objects.all():
        for medicine, steps, then in STANDARDS:
            products = MedicationProduct.objects.filter(
                company=company, name__iexact=medicine, category='weight_management', requires_authorisation=True, is_active=True,
            )
            by_mg = {}
            for product in products:
                by_mg.setdefault(_mg(product), product)
            wanted = [mg for mg, _ in steps] + [then]
            if any(mg not in by_mg for mg in wanted) or StartingPlan.objects.filter(company=company, medicine=by_mg[then].name).exists():
                continue
            plan = StartingPlan.objects.create(company=company, medicine=by_mg[then].name, then_product=by_mg[then], default_months=6)
            for position, (mg, months) in enumerate(steps, start=1):
                StartingPlanStep.objects.create(company=company, plan=plan, position=position, product=by_mg[mg], months=months)


class Migration(migrations.Migration):
    dependencies = [('care', '0025_starting_plans'), ('practices', '0001_initial')]

    operations = [migrations.RunPython(seed, migrations.RunPython.noop)]
