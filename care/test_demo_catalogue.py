"""The demo catalogue (care/demo.py) that `manage.py seed_demo` loads, and `seed_demo --catalogue-only` for a database
already in use."""
from decimal import Decimal
from io import StringIO

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings

from care.models import MedicationDose, MedicationProduct, StartingPlan, TreatmentAuthorization
from practices.models import Company, Patient


def ladder(company, medicine):
    return [(dose.label, dose.dispatch_text, dose.price, dose.billing_text, dose.tier)
            for dose in MedicationDose.objects.filter(company=company, pen__name=medicine, is_active=True)
            .select_related('pen').order_by('amount_mg')]


@override_settings(DEBUG=True, MULTI_PRACTICE_ENABLED=False, SINGLE_PRACTICE_SLUG='meridian-health')
class DemoCatalogueTests(TestCase):
    def test_the_demo_has_tirzepatides_start_and_step_ups_to_15_mg(self):
        call_command('seed_demo', stdout=StringIO())
        company = Company.objects.get(slug='meridian-health')
        price = Decimal('3295.00')
        self.assertEqual(ladder(company, 'Tirzepatide'), [
            ('2.5 mg weekly', '2 × 5 mg pens every 4 weeks', price, 'every 4 weeks', 'standard'),
            ('5 mg weekly', '4 × 5 mg pens every 4 weeks', price, 'every 4 weeks', 'standard'),
            ('7.5 mg weekly', '4 × 10 mg pens every 5 weeks', price, 'every 5 weeks', 'step_up'),
            ('10 mg weekly', '4 × 10 mg pens every 4 weeks', price, 'every 4 weeks', 'step_up'),
            ('12.5 mg weekly', '4 × 15 mg pens every 5 weeks', price, 'every 5 weeks', 'step_up'),
            ('15 mg weekly', '4 × 15 mg pens every 4 weeks', price, 'every 4 weeks', 'step_up'),
        ])
        plan = StartingPlan.objects.get(company=company, medicine='Tirzepatide')
        self.assertEqual([(step.dose.label, step.weeks) for step in plan.steps.all()], [('2.5 mg weekly', 4)])
        self.assertEqual(plan.then_dose.label, '5 mg weekly')
        self.assertEqual([label for label, *_ in ladder(company, 'Semaglutide')],
                         ['0.25 mg weekly', '0.5 mg weekly', '1 mg weekly', '1.7 mg weekly', '2.4 mg weekly'])
        self.assertEqual(sorted(MedicationProduct.objects.filter(company=company, name='Semaglutide', is_active=True)
                                .values_list('strength', flat=True)), ['1.0 mg', '2.0 mg', '4.0 mg', '6.8 mg', '9.6 mg'])

    def test_running_the_seed_again_keeps_one_demo_prescription_for_nadia(self):
        call_command('seed_demo', stdout=StringIO())
        call_command('seed_demo', stdout=StringIO())
        nadia = Patient.objects.get(medical_record_number='MH-DEMO-001')
        active = TreatmentAuthorization.objects.filter(patient=nadia, status=TreatmentAuthorization.Status.ACTIVE)
        self.assertEqual([(str(item.product), item.max_dose) for item in active], [('Semaglutide 1.0 mg', '0.25 mg weekly')])

    def test_catalogue_only_updates_the_catalogue_and_leaves_people_alone(self):
        company = Company.objects.create(name='Meridian Health', slug='meridian-health')
        # The practice's own pen and dose, as typed into the catalogue, and a pen an earlier seed named by its dose.
        own = MedicationProduct.objects.create(company=company, name='Semaglutide', strength='1.0 mg', price='1995.00')
        own_dose = MedicationDose.objects.create(company=company, pen=own, amount_mg='0.25', price='1995.00')
        old_dose = MedicationDose.objects.create(company=company, pen=own, amount_mg='1', price='3295.00')
        old = MedicationProduct.objects.create(company=company, name='Semaglutide', strength='0.25 mg', price='1995.00')
        StartingPlan.objects.create(company=company, medicine='Semaglutide', name='Standard Starting Plan', then_dose=own_dose)
        users = get_user_model().objects.count()

        out = StringIO()
        call_command('seed_demo', catalogue_only=True, stdout=out)
        call_command('seed_demo', catalogue_only=True, stdout=StringIO())

        self.assertIn('Nothing else was changed', out.getvalue())
        self.assertEqual(get_user_model().objects.count(), users)
        self.assertFalse(Patient.objects.exists())
        own_dose.refresh_from_db(), old_dose.refresh_from_db(), old.refresh_from_db()
        self.assertTrue(own_dose.is_active)
        self.assertEqual(MedicationDose.objects.get(pen__strength='1.0 mg', amount_mg=Decimal('0.25'), is_active=True), own_dose)
        self.assertFalse(old_dose.is_active)  # 1 mg weekly now comes in the 4.0 mg pen.
        self.assertFalse(old.is_active)
        self.assertEqual(MedicationProduct.objects.filter(name='Semaglutide', strength='1.0 mg').count(), 1)
        self.assertEqual(len(ladder(company, 'Tirzepatide')), 6)
        self.assertEqual(StartingPlan.objects.get(medicine='Semaglutide').name, 'Standard Starting Plan')

    def test_catalogue_only_needs_the_practice_to_exist(self):
        with self.assertRaisesMessage(CommandError, 'Run seed_demo without --catalogue-only first'):
            call_command('seed_demo', catalogue_only=True, stdout=StringIO())
        self.assertFalse(Company.objects.exists())
