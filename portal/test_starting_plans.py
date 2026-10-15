"""Initiate prescription from a starting plan, and Super Admins keeping the plans in the Pharmacy catalogue."""

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from care.models import MedicationDose, MedicationProduct, Shipment, StartingPlan, StartingPlanStep, TreatmentAuthorization
from care.operations import prepare_shipment, product_allowance
from care.prescriptions import add_weeks, current_prescription, prescription_state
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
        # Dr Czech's standard starts: Semaglutide's 24-week cycle and Tirzepatide's two weeks at 2.5 mg.
        pen = dict(company=cls.company, price='100.00', category=MedicationProduct.Category.WEIGHT_MANAGEMENT)
        # Each Semaglutide strength has its own pen, one sent every 4 weeks.
        sema = {strength: MedicationProduct.objects.create(name='Semaglutide', strength=strength, **pen)
                for strength in ('0.25 mg', '0.5 mg', '1.0 mg')}
        cls.quarter = MedicationDose.objects.create(company=cls.company, pen=sema['0.25 mg'], amount_mg='0.25', price='1995')
        cls.half = MedicationDose.objects.create(company=cls.company, pen=sema['0.5 mg'], amount_mg='0.5', price='1995')
        cls.one = MedicationDose.objects.create(company=cls.company, pen=sema['1.0 mg'], amount_mg='1', price='3295')
        cls.tirz_pen = MedicationProduct.objects.create(name='Tirzepatide', strength='5 mg', **pen)
        cls.tirz_start = MedicationDose.objects.create(company=cls.company, pen=cls.tirz_pen, amount_mg='2.5', price='2995',
                                                       dispatch_weeks=2, billing_weeks=2)
        cls.tirzepatide = MedicationDose.objects.create(company=cls.company, pen=cls.tirz_pen, amount_mg='5', price='2995',
                                                        dispatch_weeks=1, billing_weeks=4)
        cls.plan = StartingPlan.objects.create(company=cls.company, medicine='Semaglutide', then_dose=cls.one, default_weeks=24)
        StartingPlanStep.objects.create(company=cls.company, plan=cls.plan, position=1, dose=cls.quarter, weeks=4)
        StartingPlanStep.objects.create(company=cls.company, plan=cls.plan, position=2, dose=cls.half, weeks=4)
        cls.tirz_plan = StartingPlan.objects.create(company=cls.company, medicine='Tirzepatide', then_dose=cls.tirzepatide,
                                                    default_weeks=24)
        StartingPlanStep.objects.create(company=cls.company, plan=cls.tirz_plan, position=1, dose=cls.tirz_start, weeks=2)
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

    def initiate(self, weeks='16', **extra):
        self.login(self.doctor)
        current = current_prescription(self.company, self.patient)
        return self.client.post(reverse('portal:patient-prescription-initiate', args=[self.patient.pk]), {
            'workflow_context': self.token(self.doctor, 'patient-prescription', patient=self.patient),
            'current': current.pk if current else '', 'medicine': 'Semaglutide', 'plan': self.plan.pk, 'weeks': weeks, **extra})

    def steps(self):
        return list(TreatmentAuthorization.objects.exclude(status='cancelled').exclude(plan_key=None).order_by('plan_step'))

    def test_the_dialog_offers_the_plans_by_medicine_and_starts_on_the_usual_length(self):
        self.login(self.doctor)
        page = self.client.get(reverse('portal:patient-detail', args=[self.patient.pk]))
        self.assertContains(page, 'Initiate prescription')
        self.assertEqual(page.context['starting_plan_medicines'], ['Semaglutide', 'Tirzepatide'])
        self.assertEqual(page.context['initiate_form'].initial['weeks'], '24')
        plan = page.context['starting_plans'][0]
        self.assertEqual(plan.summary, '0.25 mg weekly for 4 weeks, then 0.5 mg weekly for 4 weeks, then 1 mg weekly')
        # The lengths leave the last dose some weeks.
        self.assertEqual(plan.week_choices, [12, 16, 20, 24])
        self.assertContains(page, 'data-weeks="12,16,20,24"')
        self.assertEqual(page.context['starting_plans'][1].summary, '2.5 mg weekly for 2 weeks, then 5 mg weekly')
        # A plan whose doses can't all be prescribed, here for want of a price, isn't offered.
        MedicationDose.objects.filter(pk=self.tirz_start.pk).update(price=None)
        page = self.client.get(reverse('portal:patient-detail', args=[self.patient.pk]))
        self.assertEqual(page.context['starting_plan_medicines'], ['Semaglutide'])

    def test_initiating_saves_a_step_per_strength_so_dispensing_follows_the_plan(self):
        old = TreatmentAuthorization.objects.create(
            company=self.company, patient=self.patient, prescribed_by=self.doctor, product=self.half.pen, dose=self.half,
            max_dose='0.5 mg weekly', starts_on=self.today - timedelta(days=60), expires_on=self.today + timedelta(days=60))
        response = self.initiate()
        self.assertEqual(response.status_code, 302)
        old.refresh_from_db()
        self.assertEqual(old.status, 'cancelled')
        first, second, last = self.steps()
        self.assertEqual(len({first.plan_key, second.plan_key, last.plan_key}), 1)
        self.assertEqual([(step.dose, step.product, step.max_dose) for step in (first, second, last)],
                         [(self.quarter, self.quarter.pen, '0.25 mg weekly'), (self.half, self.half.pen, '0.5 mg weekly'),
                          (self.one, self.one.pen, '1 mg weekly')])
        self.assertEqual((first.starts_on, first.expires_on), (self.today, add_weeks(self.today, 4) - timedelta(days=1)))
        self.assertEqual(second.starts_on, add_weeks(self.today, 4))
        self.assertEqual((last.starts_on, last.expires_on), (add_weeks(self.today, 8), add_weeks(self.today, 16)))
        self.assertEqual((first.replaces, second.replaces), (old, None))
        # The current prescription is the step in effect; it ends when the plan does.
        current = current_prescription(self.company, self.patient)
        self.assertEqual((current, current.ends_on), (first, last.expires_on))
        self.assertEqual(prescription_state(current, self.today), 'valid')
        # Each step's pen can only be dispensed while its step is current, and its drafted parcel uses it.
        self.assertEqual(product_allowance(self.patient, self.quarter.pen)[:2], (first, 0))
        self.assertTrue(product_allowance(self.patient, self.half.pen)[2])
        self.assertEqual(product_allowance(self.patient, self.half.pen, on_date=second.starts_on)[0], second)
        self.assertEqual(step_in_effect(first, second.starts_on), second)
        # Steps followed by another don't raise review reminders; the plan's end does.
        due = list(due_authorizations(company=self.company, on_date=last.expires_on, within_days=0))
        self.assertNotIn(first, due)
        self.assertNotIn(second, due)
        self.assertIn(last, due)
        # The patient list shows the step in effect, not a later one saved ahead, and when the whole plan ends.
        listed = self.client.get(reverse('portal:patient-list')).context['patients'][0]
        self.assertEqual((listed.record_authorization, listed.record_authorization.ends_on), (first, last.expires_on))
        page = self.client.get(reverse('portal:patient-detail', args=[self.patient.pk]))
        self.assertContains(page, '<dt>Starting plan</dt>', html=False)
        self.assertContains(page, f'Prescribed to {last.expires_on.day} {last.expires_on:%b}')
        prescriptions = [entry for entry in page.context['clinical_log'] if entry['kind'] == 'prescription']
        # The plan is one line in the log, on its first step.
        self.assertEqual([entry['id'] for entry in prescriptions if entry['id'] != f'prescription-{old.pk}'], [f'prescription-{first.pk}'])
        self.assertTrue(first.log_text.startswith('Prescription initiated: Semaglutide 0.25 mg weekly for 4 weeks, then 0.5 mg weekly '
                                                  'for 4 weeks, then 1 mg weekly, 16 weeks in all, valid to '))

    def drafted(self):
        """The parcels drafted from the prescription, each checked and marked ready: [(week, dose, pens, pen)]."""
        parcels = []
        for parcel in Shipment.objects.filter(patient=self.patient).order_by('scheduled_for', 'pk'):
            self.assertEqual((parcel.status, parcel.scheduled_for.weekday()), ('draft', 1))
            prepare_shipment(shipment=parcel, actor=self.owner)
            item = parcel.items.get()
            parcels.append(((parcel.scheduled_for - self.today).days // 7 + 1, item.dose, item.quantity, item.product.strength))
        return parcels

    def test_a_semaglutide_course_drafts_one_pen_of_its_strength_every_4_weeks(self):
        self.initiate(weeks='24')
        self.assertEqual(self.drafted(), [(1, '0.25 mg weekly', 1, '0.25 mg'), (5, '0.5 mg weekly', 1, '0.5 mg'),
                                          (9, '1 mg weekly', 1, '1.0 mg'), (13, '1 mg weekly', 1, '1.0 mg'),
                                          (17, '1 mg weekly', 1, '1.0 mg'), (21, '1 mg weekly', 1, '1.0 mg')])
        # Numbered by month of treatment, for the Dispensed log.
        self.assertEqual(list(Shipment.objects.order_by('scheduled_for').values_list('cycle_number', flat=True)), [1, 2, 3, 4, 5, 6])

    def test_a_tirzepatide_start_drafts_a_5_mg_pen_in_weeks_1_3_4_5_and_6(self):
        self.initiate(weeks='24', medicine='Tirzepatide', plan=self.tirz_plan.pk)
        self.assertEqual(self.drafted()[:5], [(1, '2.5 mg weekly', 1, '5 mg'), (3, '5 mg weekly', 1, '5 mg'),
                                              (4, '5 mg weekly', 1, '5 mg'), (5, '5 mg weekly', 1, '5 mg'),
                                              (6, '5 mg weekly', 1, '5 mg')])

    def test_a_length_the_plan_cant_fill_reopens_the_dialog(self):
        response = self.initiate(weeks='8')
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
            'workflow_context': self.token(self.doctor, 'patient-prescription', patient=self.patient), 'current': first.pk, 'weeks': '8'})
        last.refresh_from_db()
        self.assertEqual(last.status, 'cancelled')
        steps = self.steps()
        self.assertEqual(steps[:2], [first, second])
        renewal = steps[2]
        self.assertEqual((renewal.dose, renewal.starts_on, renewal.expires_on, renewal.replaces),
                         (self.one, last.starts_on, add_weeks(last.expires_on, 8), last))
        current = current_prescription(self.company, self.patient)
        self.assertEqual((current, current.ends_on), (first, renewal.expires_on))

    def test_changing_mid_plan_replaces_the_rest_of_it(self):
        self.initiate()
        first, second, last = self.steps()
        self.client.post(reverse('portal:patient-prescription-change', args=[self.patient.pk]), {
            'workflow_context': self.token(self.doctor, 'patient-prescription', patient=self.patient), 'current': first.pk,
            'medicine': 'Semaglutide', 'dose': self.one.pk, 'weeks': '12'})
        self.assertEqual(self.steps(), [])
        self.assertEqual(TreatmentAuthorization.objects.filter(pk__in=(first.pk, second.pk, last.pk), status='cancelled').count(), 3)
        current = current_prescription(self.company, self.patient)
        self.assertEqual((current.dose, current.plan_key, current.ends_on), (self.one, None, add_weeks(self.today, 12)))

    def test_super_admins_keep_the_plans_in_the_pharmacy_catalogue(self):
        create = reverse('portal:ops-starting-plan-create')
        self.login(self.owner)
        self.assertContains(self.client.get(reverse('portal:ops-catalogue')), '0.25 mg weekly for 4 weeks, then 0.5 mg weekly')
        data = {'workflow_context': self.token(self.owner, 'catalogue-starting-plan'), 'medicine': 'Semaglutide', 'name': 'Slow start',
                'step_1_dose': self.quarter.pk, 'step_1_weeks': '8', 'then_dose': self.half.pk, 'default_weeks': '20',
                'is_active': 'on'}
        wrong = self.client.post(create, {**data, 'then_dose': self.tirzepatide.pk})
        self.assertContains(wrong, 'Every dose must be an offered, priced Semaglutide dose', status_code=400)
        short = self.client.post(create, {**data, 'default_weeks': '8'})
        self.assertContains(short, 'The usual length must be longer than the steps', status_code=400)
        saved = self.client.post(create, data)
        plan = StartingPlan.objects.get(name='Slow start')
        self.assertRedirects(saved, reverse('portal:ops-starting-plan-detail', args=[plan.pk]), fetch_redirect_response=False)
        self.assertEqual([(step.dose, step.weeks) for step in plan.steps.all()], [(self.quarter, 8)])
        self.assertEqual((plan.then_dose, plan.default_weeks, plan.is_active), (self.half, 20, True))
        # Practice administrators read plans; only Super Admins change them.
        self.login(self.admin)
        page = self.client.get(reverse('portal:ops-starting-plan-detail', args=[plan.pk]))
        self.assertContains(page, 'Read-only catalogue record.')
        self.assertEqual(self.client.post(create, {**data, 'workflow_context': self.token(self.admin, 'catalogue-starting-plan')}).status_code, 403)
