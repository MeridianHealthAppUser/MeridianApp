"""Initiate prescription from a starting plan, and Super Admins keeping the plans in the Pharmacy catalogue."""

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from care.models import MedicationProduct, StartingPlan, StartingPlanStep, TreatmentAuthorization
from care.operations import product_allowance
from care.prescriptions import add_months, current_prescription, prescription_state
from care.review_automation import due_authorizations
from care.treatment import step_in_effect
from practices.models import Company, CompanyMembership, Patient
from practices.services import ACTIVE_COMPANY_SESSION_KEY

from .workflow_context import make_workflow_context


@override_settings(MULTI_PRACTICE_ENABLED=True)
class StartingPlanTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        users = get_user_model().objects
        cls.company = Company.objects.create(name='Plan Practice', slug='plan-practice')
        cls.doctor = users.create_user(email='plan-doctor@example.test', first_name='Sam', last_name='Marchant')
        cls.admin = users.create_user(email='plan-admin@example.test', first_name='Lindiwe', last_name='Admin')
        cls.owner = users.create_user(email='plan-owner@example.test', first_name='Joshua', last_name='Czech')
        for user, role, kind in ((cls.doctor, 'doctor', 'doctor'), (cls.admin, 'practice_admin', ''), (cls.owner, 'super_admin', 'doctor')):
            CompanyMembership.objects.create(company=cls.company, user=user, role=role, clinician_type=kind)
        cls.patient = Patient.objects.create(company=cls.company, first_name='Nadia', last_name='Mokoena', assigned_doctor=cls.doctor)
        product = dict(company=cls.company, price='100.00', category=MedicationProduct.Category.WEIGHT_MANAGEMENT,
                       dose_frequency=MedicationProduct.DoseFrequency.WEEKLY)
        cls.quarter = MedicationProduct.objects.create(name='Semaglutide', strength='0.25 mg', **product)
        cls.half = MedicationProduct.objects.create(name='Semaglutide', strength='0.5 mg', **product)
        cls.one = MedicationProduct.objects.create(name='Semaglutide', strength='1.0 mg', **product)
        cls.tirzepatide = MedicationProduct.objects.create(name='Tirzepatide', strength='5 mg', **product)
        cls.plan = StartingPlan.objects.create(company=cls.company, medicine='Semaglutide', then_product=cls.one, default_months=6)
        StartingPlanStep.objects.create(company=cls.company, plan=cls.plan, position=1, product=cls.quarter, months=1)
        StartingPlanStep.objects.create(company=cls.company, plan=cls.plan, position=2, product=cls.half, months=1)
        cls.today = timezone.localdate()

    def login(self, user):
        self.client.force_login(user)
        session = self.client.session
        session[ACTIVE_COMPANY_SESSION_KEY] = self.company.pk
        session.save()

    def token(self, user, kind, record=None, patient=None):
        request = RequestFactory().get('/')
        request.user = user
        return make_workflow_context(request, self.company, kind, record, patient=patient)

    def initiate(self, months='4', **extra):
        self.login(self.doctor)
        current = current_prescription(self.company, self.patient)
        return self.client.post(reverse('portal:patient-prescription-initiate', args=[self.patient.pk]), {
            'workflow_context': self.token(self.doctor, 'patient-prescription', patient=self.patient),
            'current': current.pk if current else '', 'medicine': 'Semaglutide', 'plan': self.plan.pk, 'months': months, **extra})

    def steps(self):
        return list(TreatmentAuthorization.objects.exclude(status='cancelled').exclude(plan_key=None).order_by('plan_step'))

    def test_the_dialog_offers_the_plans_by_medicine_and_starts_on_the_usual_length(self):
        self.login(self.doctor)
        page = self.client.get(reverse('portal:patient-detail', args=[self.patient.pk]))
        self.assertContains(page, 'Initiate prescription')
        self.assertEqual(page.context['starting_plan_medicines'], ['Semaglutide'])
        self.assertEqual(page.context['initiate_form'].initial['months'], '6')
        plan = page.context['starting_plans'][0]
        self.assertEqual(plan.summary, '0.25 mg weekly for 1 month, then 0.5 mg weekly for 1 month, then 1.0 mg weekly')
        # The lengths leave the last strength at least a month.
        self.assertEqual(plan.month_choices, [3, 4, 5, 6])
        self.assertContains(page, 'data-months="3,4,5,6"')

    def test_initiating_saves_a_step_per_strength_so_dispensing_follows_the_plan(self):
        old = TreatmentAuthorization.objects.create(
            company=self.company, patient=self.patient, prescribed_by=self.doctor, product=self.half, max_dose='0.5 mg weekly',
            starts_on=self.today - timedelta(days=60), expires_on=self.today + timedelta(days=60))
        response = self.initiate()
        self.assertEqual(response.status_code, 302)
        old.refresh_from_db()
        self.assertEqual(old.status, 'cancelled')
        first, second, last = self.steps()
        self.assertEqual(len({first.plan_key, second.plan_key, last.plan_key}), 1)
        self.assertEqual([(step.product, step.max_dose) for step in (first, second, last)],
                         [(self.quarter, '0.25 mg weekly'), (self.half, '0.5 mg weekly'), (self.one, '1.0 mg weekly')])
        self.assertEqual((first.starts_on, first.expires_on), (self.today, add_months(self.today, 1) - timedelta(days=1)))
        self.assertEqual(second.starts_on, add_months(self.today, 1))
        self.assertEqual((last.starts_on, last.expires_on), (add_months(self.today, 2), add_months(self.today, 4)))
        self.assertEqual((first.replaces, second.replaces), (old, None))
        # The current prescription is the step in effect; it ends when the plan does.
        current = current_prescription(self.company, self.patient)
        self.assertEqual((current, current.ends_on), (first, last.expires_on))
        self.assertEqual(prescription_state(current, self.today), 'valid')
        # Each month's strength can only be dispensed while its step is current.
        self.assertEqual(product_allowance(self.patient, self.quarter)[0], first)
        self.assertTrue(product_allowance(self.patient, self.half)[2])
        self.assertEqual(product_allowance(self.patient, self.half, on_date=second.starts_on)[0], second)
        self.assertEqual(step_in_effect(first, second.starts_on), second)
        # Steps followed by another don't raise review reminders; the plan's end does.
        due = list(due_authorizations(company=self.company, on_date=last.expires_on, within_days=0))
        self.assertNotIn(first, due)
        self.assertNotIn(second, due)
        self.assertIn(last, due)
        page = self.client.get(reverse('portal:patient-detail', args=[self.patient.pk]))
        self.assertContains(page, '<dt>Starting plan</dt>', html=False)
        self.assertContains(page, f'Prescribed to {last.expires_on.day} {last.expires_on:%b}')
        prescriptions = [entry for entry in page.context['clinical_log'] if entry['kind'] == 'prescription']
        # The plan is one line in the log, on its first step.
        self.assertEqual([entry['id'] for entry in prescriptions if entry['id'] != f'prescription-{old.pk}'], [f'prescription-{first.pk}'])
        self.assertTrue(first.log_text.startswith('Prescription initiated: Semaglutide 0.25 mg weekly for 1 month, then 0.5 mg weekly '
                                                  'for 1 month, then 1.0 mg weekly, 4 months in all, valid to '))

    def test_a_length_the_plan_cant_fill_reopens_the_dialog(self):
        response = self.initiate(months='2')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.context['open_dialog'], 'initiate')
        self.assertContains(response, 'Choose a longer prescription', status_code=400)
        mismatched = self.initiate(medicine='Tirzepatide')
        self.assertEqual(mismatched.status_code, 400)
        self.assertContains(mismatched, 'Choose a starting plan for the selected medicine.', status_code=400)
        self.assertFalse(TreatmentAuthorization.objects.exists())

    def test_renewing_mid_plan_extends_the_last_strength(self):
        self.initiate()
        first, second, last = self.steps()
        self.client.post(reverse('portal:patient-prescription-renew', args=[self.patient.pk]), {
            'workflow_context': self.token(self.doctor, 'patient-prescription', patient=self.patient), 'current': first.pk, 'months': '2'})
        last.refresh_from_db()
        self.assertEqual(last.status, 'cancelled')
        steps = self.steps()
        self.assertEqual(steps[:2], [first, second])
        renewal = steps[2]
        self.assertEqual((renewal.product, renewal.starts_on, renewal.expires_on, renewal.replaces),
                         (self.one, last.starts_on, add_months(last.expires_on, 2), last))
        current = current_prescription(self.company, self.patient)
        self.assertEqual((current, current.ends_on), (first, renewal.expires_on))

    def test_changing_mid_plan_replaces_the_rest_of_it(self):
        self.initiate()
        first, second, last = self.steps()
        self.client.post(reverse('portal:patient-prescription-change', args=[self.patient.pk]), {
            'workflow_context': self.token(self.doctor, 'patient-prescription', patient=self.patient), 'current': first.pk,
            'medicine': 'Semaglutide', 'product': self.one.pk, 'months': '3'})
        self.assertEqual(self.steps(), [])
        self.assertEqual(TreatmentAuthorization.objects.filter(pk__in=(first.pk, second.pk, last.pk), status='cancelled').count(), 3)
        current = current_prescription(self.company, self.patient)
        self.assertEqual((current.product, current.plan_key, current.ends_on), (self.one, None, add_months(self.today, 3)))

    def test_super_admins_keep_the_plans_in_the_pharmacy_catalogue(self):
        create = reverse('portal:ops-starting-plan-create')
        self.login(self.owner)
        self.assertContains(self.client.get(reverse('portal:ops-catalogue')), '0.25 mg weekly for 1 month, then 0.5 mg weekly')
        data = {'workflow_context': self.token(self.owner, 'catalogue-starting-plan'), 'medicine': 'Semaglutide', 'name': 'Slow start',
                'step_1_product': self.quarter.pk, 'step_1_months': '2', 'then_product': self.half.pk, 'default_months': '5',
                'is_active': 'on'}
        wrong = self.client.post(create, {**data, 'then_product': self.tirzepatide.pk})
        self.assertContains(wrong, 'Every strength must be an active Semaglutide', status_code=400)
        short = self.client.post(create, {**data, 'default_months': '2'})
        self.assertContains(short, 'The usual length must be longer than the steps', status_code=400)
        saved = self.client.post(create, data)
        plan = StartingPlan.objects.get(name='Slow start')
        self.assertRedirects(saved, reverse('portal:ops-starting-plan-detail', args=[plan.pk]), fetch_redirect_response=False)
        self.assertEqual([(step.product, step.months) for step in plan.steps.all()], [(self.quarter, 2)])
        self.assertEqual((plan.then_product, plan.default_months, plan.is_active), (self.half, 5, True))
        # Practice administrators read plans; only Super Admins change them.
        self.login(self.admin)
        page = self.client.get(reverse('portal:ops-starting-plan-detail', args=[plan.pk]))
        self.assertContains(page, 'Read-only catalogue record.')
        self.assertEqual(self.client.post(create, {**data, 'workflow_context': self.token(self.admin, 'catalogue-starting-plan')}).status_code, 403)
