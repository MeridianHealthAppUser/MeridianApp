"""Doses in the Pharmacy catalogue: what doctors prescribe, the pen it comes in, how often it is sent and what it costs."""

from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import RequestFactory, TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from care.models import (
    MedicationDose, MedicationProduct, PatientSubscription, Shipment, ShipmentItem, StartingPlan, StartingPlanStep,
    TreatmentAuthorization,
)
from care.operations import product_allowance
from care.treatment import dose_in_effect
from practices.models import Company, CompanyMembership, Patient
from practices.services import ACTIVE_COMPANY_SESSION_KEY, ACTIVE_PATIENT_COMPANY_SESSION_KEY

from .intake_views import treatment_pricing
from .workflow_context import make_workflow_context


class DoseTestData:
    @classmethod
    def setUpTestData(cls):
        users = get_user_model().objects
        cls.company = Company.objects.create(name='Dose Practice', slug='dose-practice')
        cls.owner = users.create_user(email='dose-owner@example.test', first_name='Joshua', last_name='Czech')
        cls.admin = users.create_user(email='dose-admin@example.test', first_name='Lindiwe', last_name='Admin')
        cls.doctor = users.create_user(email='dose-doctor@example.test', first_name='Sam', last_name='Marchant')
        cls.patient_user = users.create_user(email='dose-patient@example.test')
        for user, role, kind in ((cls.owner, 'super_admin', 'doctor'), (cls.admin, 'practice_admin', ''), (cls.doctor, 'doctor', 'doctor')):
            CompanyMembership.objects.create(company=cls.company, user=user, role=role, clinician_type=kind)
        cls.patient = Patient.objects.create(company=cls.company, user=cls.patient_user, first_name='Nadia', last_name='Mokoena',
                                             assigned_doctor=cls.doctor)
        pen = dict(company=cls.company, price='100.00', category=MedicationProduct.Category.WEIGHT_MANAGEMENT)
        cls.sema_pen = MedicationProduct.objects.create(name='Semaglutide', strength='1 mg', **pen)
        cls.tirz_5 = MedicationProduct.objects.create(name='Tirzepatide', strength='5 mg', **pen)
        cls.tirz_10 = MedicationProduct.objects.create(name='Tirzepatide', strength='10 mg', **pen)
        cls.sema = MedicationDose.objects.create(company=cls.company, pen=cls.sema_pen, amount_mg='0.25', price='1995')
        cls.tirz_start = MedicationDose.objects.create(company=cls.company, pen=cls.tirz_5, amount_mg='2.5', price='2995',
                                                       dispatch_weeks=2, billing_weeks=2)
        cls.tirz = MedicationDose.objects.create(company=cls.company, pen=cls.tirz_5, amount_mg='5', price='2995',
                                                 dispatch_weeks=1, billing_weeks=4)
        # A 10 mg pen lasts five weeks at 7.5 mg; its price isn't known yet.
        cls.step_up = MedicationDose.objects.create(company=cls.company, pen=cls.tirz_10, amount_mg='7.5', dispatch_weeks=5,
                                                    billing_weeks=5, tier=MedicationDose.Tier.STEP_UP)
        cls.today = timezone.localdate()

    def login(self, user):
        self.client.force_login(user)
        session = self.client.session
        session[ACTIVE_COMPANY_SESSION_KEY] = self.company.pk
        session[ACTIVE_PATIENT_COMPANY_SESSION_KEY] = self.company.pk
        session.save()

    def token(self, user, kind, record=None):
        request = RequestFactory().get('/')
        request.user = user
        return make_workflow_context(request, self.company, kind, record)

    def prescribe(self, dose, **values):
        defaults = dict(company=self.company, patient=self.patient, prescribed_by=self.doctor, product=dose.pen, dose=dose,
                        max_dose=dose.label, quantity_per_cycle=dose.pens_per_dispatch, starts_on=self.today,
                        expires_on=self.today + timedelta(weeks=24))
        defaults.update(values)
        return TreatmentAuthorization.objects.create(**defaults)


@override_settings(MULTI_PRACTICE_ENABLED=True)
class DoseCatalogueTests(DoseTestData, TestCase):
    def dose_data(self, **extra):
        return {'pen': self.tirz_10.pk, 'amount_mg': '10', 'frequency': 'weekly', 'pens_per_dispatch': '1',
                'dispatch_weeks': '4', 'price': '', 'billing_weeks': '4', 'tier': 'step_up', 'is_active': 'on', **extra}

    def test_super_admins_add_doses_and_practice_administrators_read_them(self):
        self.login(self.owner)
        catalogue = self.client.get(reverse('portal:ops-catalogue'))
        self.assertContains(catalogue, '1 × 10 mg pen every 5 weeks')
        self.assertContains(catalogue, 'R2995.00 <small>every 4 weeks</small>', html=False)
        self.assertContains(catalogue, 'Price needed')
        create = reverse('portal:ops-dose-create')
        response = self.client.post(create, {'workflow_context': self.token(self.owner, 'catalogue-dose'), **self.dose_data()})
        dose = MedicationDose.objects.get(pen=self.tirz_10, amount_mg=10)
        self.assertRedirects(response, reverse('portal:ops-dose-detail', args=[dose.pk]), fetch_redirect_response=False)
        self.assertEqual((str(dose), dose.price, dose.dispatch_weeks, dose.tier), ('Tirzepatide 10 mg weekly', None, 4, 'step_up'))
        # A dose without a price is in the catalogue but can't be prescribed until it has one.
        self.assertFalse(dose.is_prescribable)
        # 7.5, not 7.500, which a browser with a decimal comma shows as 7,500.
        editor = self.client.get(reverse('portal:ops-dose-detail', args=[self.step_up.pk]))
        self.assertContains(editor, 'name="amount_mg" value="7.5"')
        self.login(self.admin)
        page = self.client.get(reverse('portal:ops-dose-detail', args=[dose.pk]))
        self.assertContains(page, 'Read-only catalogue record.')
        self.assertContains(page, '1 × 10 mg pen every 4 weeks')
        refused = self.client.post(create, {'workflow_context': self.token(self.admin, 'catalogue-dose'),
                                            **self.dose_data(amount_mg='12.5')})
        self.assertEqual(refused.status_code, 403)
        self.assertFalse(MedicationDose.objects.filter(amount_mg='12.5').exists())

    def test_a_medicine_has_each_dose_once_and_a_prescribed_dose_keeps_its_supply(self):
        self.login(self.owner)
        create = reverse('portal:ops-dose-create')
        # The same Tirzepatide dose in another pen is the same dose.
        duplicate = self.client.post(create, {'workflow_context': self.token(self.owner, 'catalogue-dose'),
                                              **self.dose_data(pen=self.tirz_5.pk, amount_mg='7.5')})
        self.assertContains(duplicate, 'Tirzepatide already offers this dose', status_code=400)
        # Once hidden, a dose can be replaced by the same dose in another pen.
        MedicationDose.objects.filter(pk=self.step_up.pk).update(is_active=False)
        replaced = self.client.post(create, {'workflow_context': self.token(self.owner, 'catalogue-dose'),
                                             **self.dose_data(pen=self.tirz_5.pk, amount_mg='7.5')})
        self.assertEqual(replaced.status_code, 302)
        self.assertEqual(MedicationDose.objects.filter(amount_mg='7.5', is_active=True).get().pen, self.tirz_5)
        for field, value in (('amount_mg', '0'), ('dispatch_weeks', '0'), ('dispatch_weeks', '27'), ('price', '-1')):
            with self.subTest(field=field, value=value):
                response = self.client.post(create, {'workflow_context': self.token(self.owner, 'catalogue-dose'),
                                                     **self.dose_data(**{field: value})})
                self.assertEqual(response.status_code, 400)
        self.prescribe(self.tirz)
        detail = reverse('portal:ops-dose-detail', args=[self.tirz.pk])
        current = {'pen': self.tirz_5.pk, 'amount_mg': '5', 'frequency': 'weekly', 'pens_per_dispatch': '1', 'dispatch_weeks': '1',
                   'price': '2995.00', 'billing_weeks': '4', 'tier': 'standard', 'is_active': 'on'}
        locked = self.client.post(detail, {'workflow_context': self.token(self.owner, 'catalogue-dose', self.tirz),
                                           **current, 'dispatch_weeks': '2'})
        self.assertContains(locked, 'Add a new dose for those changes', status_code=400)
        repriced = self.client.post(detail, {'workflow_context': self.token(self.owner, 'catalogue-dose', self.tirz),
                                             **current, 'price': '3100.00'})
        self.assertEqual(repriced.status_code, 302)
        self.tirz.refresh_from_db()
        self.assertEqual((self.tirz.dispatch_weeks, self.tirz.price), (1, Decimal('3100.00')))


@override_settings(MULTI_PRACTICE_ENABLED=True)
class DoseSupplyAndPriceTests(DoseTestData, TestCase):
    def send(self, pen, dose, on, quantity=1, dispatched=False):
        parcel = Shipment.objects.create(
            company=self.company, patient=self.patient, scheduled_for=on,
            status=Shipment.Status.DISPATCHED if dispatched else Shipment.Status.READY,
            dispatched_at=timezone.now() - timedelta(days=(self.today - on).days) if dispatched else None)
        ShipmentItem.objects.create(company=self.company, shipment=parcel, product=pen, dose=dose, quantity=quantity)

    def test_a_dose_sends_its_pens_once_in_each_of_its_periods_and_a_renewal_keeps_the_cycle(self):
        MedicationDose.objects.filter(pk=self.step_up.pk).update(price='4500')
        started = self.prescribe(self.step_up, starts_on=self.today - timedelta(days=10), status='cancelled')
        renewal = self.prescribe(self.step_up, replaces=started)
        self.send(self.tirz_10, '7.5 mg weekly', self.today - timedelta(days=10), dispatched=True)
        # The pen sent ten days ago lasts five weeks from when the dose started, renewal or not.
        authorization, remaining, _ = product_allowance(self.patient, self.tirz_10)
        self.assertEqual((authorization, remaining), (renewal, 0))
        self.assertEqual(product_allowance(self.patient, self.tirz_10, on_date=self.today + timedelta(days=24))[1], 0)
        self.assertEqual(product_allowance(self.patient, self.tirz_10, on_date=self.today + timedelta(days=25))[1], 1)

    def test_a_care_plan_costs_what_its_dose_in_effect_costs(self):
        plan = StartingPlan.objects.create(company=self.company, medicine='Tirzepatide', then_dose=self.tirz, default_weeks=24)
        StartingPlanStep.objects.create(company=self.company, plan=plan, position=1, dose=self.tirz_start, weeks=2)
        first = self.prescribe(self.tirz_start, expires_on=self.today + timedelta(days=13), plan_step=1,
                               plan_key='2b8f0d6e-3c55-4a43-9a52-0e6d3f0c5a11')
        self.prescribe(self.tirz, starts_on=self.today + timedelta(days=14), plan_step=2, plan_key=first.plan_key)
        subscription = PatientSubscription.objects.create(company=self.company, patient=self.patient, authorization=first,
                                                          plan_name='Local plan', starts_on=self.today)
        self.assertEqual(dose_in_effect(subscription), self.tirz_start)
        self.assertEqual(dose_in_effect(subscription, self.today + timedelta(days=14)), self.tirz)
        self.login(self.patient_user)
        page = self.client.get(reverse('portal:patient-treatment'))
        self.assertContains(page, 'R2995<span>every 2 weeks</span>', html=False)
        self.assertContains(page, 'Tirzepatide 2.5 mg weekly')
        self.login(self.admin)
        listing = self.client.get(reverse('portal:treatment-subscriptions'))
        self.assertContains(listing, 'R2995.00 every 2 weeks')

    def tirzepatide_plan(self):
        from care.starting_plans import initiate_prescription

        plan = StartingPlan.objects.create(company=self.company, medicine='Tirzepatide', then_dose=self.tirz, default_weeks=24)
        StartingPlanStep.objects.create(company=self.company, plan=plan, position=1, dose=self.tirz_start, weeks=2)
        return initiate_prescription(company=self.company, patient=self.patient, actor=self.doctor, current=None, plan=plan, weeks=24)

    def weeks(self, timeline):
        return timeline['weeks']

    def test_my_treatment_shows_the_dose_pens_week_and_next_parcel_and_a_weekly_timeline(self):
        from .patient_summary import treatment_timeline

        first = self.tirzepatide_plan()
        ends = self.today + timedelta(weeks=24)
        parcel = Shipment.objects.filter(patient=self.patient).order_by('scheduled_for').first()
        # Week 1 is the week of the first parcel.
        week_one = parcel.scheduled_for - timedelta(days=parcel.scheduled_for.weekday())
        self.login(self.patient_user)
        page = self.client.get(reverse('portal:patient-treatment'))
        heading = 'Week 1 of 24' if week_one <= self.today else f'Week 1 starts {week_one:%-d %b}, with your first pens'
        for text in ('Tirzepatide 2.5 mg weekly', heading, 'Your dose is <strong>2.5 mg once a week</strong>',
                     'You receive <strong>1 × 5 mg pen every 2 weeks</strong>',
                     f'From week 3 ({week_one + timedelta(weeks=2):%-d %b}) your dose changes to 5 mg weekly',
                     f'{parcel.scheduled_for:%a %-d %b} · 1 × Tirzepatide 5 mg pen', f'{ends.day} {ends:%b %Y}',
                     'R2995<span>every 2 weeks</span>', 'Your treatment timeline',
                     # Before week 1 it is next; from then on, the patient is there.
                     'You are here' if week_one <= self.today else 'Up next'):
            self.assertContains(page, text, html=False)
        # The timeline opens in a dialog from How your dose works, at where the patient is.
        self.assertContains(page, f'href="{reverse("portal:patient-treatment")}?timeline=1" data-modal-open="treatment-timeline"', html=False)
        self.assertContains(page, '<dialog class="app-dialog app-dialog--wide patient-timeline" id="treatment-timeline" aria-labelledby="treatment-timeline-heading">', html=False)
        self.assertContains(page, 'aria-current="step" data-dialog-scroll' if week_one <= self.today else ' data-dialog-scroll>', html=False)
        opened = self.client.get(reverse('portal:patient-treatment'), {'timeline': 1})
        self.assertContains(opened, 'aria-labelledby="treatment-timeline-heading" open>', html=False)
        # With one current prescription, Start your subscription starts on it.
        self.assertContains(page, f'<option value="{first.pk}" selected>Tirzepatide 2.5 mg weekly · authorised until {ends:%d %b %Y}</option>', html=False)

        timeline = treatment_timeline(self.company, self.patient, first)
        weeks = self.weeks(timeline)
        # The plan starting is week 1's decision; until its pens go out, week 1 is next rather than here.
        self.assertEqual([(event['text'], event['date']) for event in weeks[0]['events']], [('Prescription started', self.today)])
        self.assertEqual((len(weeks), weeks[0]['date'], weeks[0]['current'], weeks[0]['next']),
                         (24, week_one, week_one <= self.today, week_one > self.today))
        self.assertEqual((weeks[0]['dose'], weeks[0]['payments']), ('2.5 mg', [Decimal('2995.00')]))
        self.assertEqual([(item['contents'], item['when']) for item in weeks[0]['parcels']],
                         [('1 × 5 mg pen', f'ships {parcel.scheduled_for:%a %-d %b}')])
        # The plan's step up starts in the week its pens go out; 5 mg weekly is billed every 4 weeks from then.
        self.assertEqual((weeks[2]['dose'], weeks[2]['change']), ('5 mg', 'up from 2.5 mg'))
        self.assertEqual([week['number'] for week in weeks if week['payments']], [1, 3, 7, 11, 15, 19, 23])
        self.assertEqual([len(week['parcels']) for week in weeks], [1, 0] + [1] * 22)
        self.assertEqual(sum(len(week['events']) for week in weeks), 1)

    def test_the_timeline_shows_the_doctor_increasing_the_dose_from_a_week(self):
        from care.prescriptions import change_prescription, current_prescription

        from .patient_summary import treatment_overview, treatment_timeline

        self.tirzepatide_plan()
        # Six weeks later, the doctor steps the patient up to 7.5 mg.
        moved = timedelta(weeks=6)
        for step in TreatmentAuthorization.objects.filter(patient=self.patient):
            TreatmentAuthorization.objects.filter(pk=step.pk).update(
                starts_on=step.starts_on - moved, expires_on=step.expires_on - moved, created_at=step.created_at - moved)
        for parcel in Shipment.objects.filter(patient=self.patient):
            day = parcel.scheduled_for - moved
            sent = day < self.today
            Shipment.objects.filter(pk=parcel.pk).update(
                scheduled_for=day, status='delivered' if sent else parcel.status,
                dispatched_at=timezone.now() - timedelta(days=(self.today - day).days) if sent else None)
        week_one = min(Shipment.objects.filter(patient=self.patient).values_list('scheduled_for', flat=True))
        week_one -= timedelta(days=week_one.weekday())
        MedicationDose.objects.filter(pk=self.step_up.pk).update(price='4500')
        changed = change_prescription(company=self.company, patient=self.patient, actor=self.doctor,
                                      current=current_prescription(self.company, self.patient), dose=self.step_up, weeks=12)
        timeline = treatment_timeline(self.company, self.patient, changed)
        weeks = self.weeks(timeline)
        self.assertEqual(timeline['total'], 18)
        # The new dose starts in the week its pens go out.
        self.assertEqual([(week['number'], week['dose']) for week in weeks[:8]],
                         [(1, '2.5 mg'), (2, '2.5 mg'), (3, '5 mg'), (4, '5 mg'), (5, '5 mg'), (6, '5 mg'), (7, '7.5 mg'), (8, '7.5 mg')])
        self.assertEqual((weeks[6]['change'], weeks[6]['payments'], [item['contents'] for item in weeks[6]['parcels']]),
                         ('up from 5 mg', [Decimal('4500.00')], ['1 × 10 mg pen']))
        self.assertEqual([week['number'] for week in weeks if week['payments'] and week['dose'] == '7.5 mg'], [7, 12, 17])
        # Each decision is in the week it was made: the plan in week 1, the change in this week.
        now = (self.today - week_one).days // 7 + 1
        events = [(week['number'], event['text'], event['by'], event['date']) for week in weeks for event in week['events']]
        self.assertEqual(events, [(1, 'Prescription started', self.doctor.full_name, self.today - moved),
                                  (now, 'Prescription adjusted', self.doctor.full_name, self.today)])
        self.assertEqual([week['number'] for week in weeks if week['current']], [now])
        self.assertTrue(all(week['past'] for week in weeks[:now - 1]))
        self.assertEqual([item['when'].split()[0] for week in weeks[:6] for item in week['parcels']], ['delivered'] * 5)
        # The card counts the same weeks.
        self.assertEqual(treatment_overview(self.company, self.patient, changed)['week'], now)

    def test_the_timeline_names_each_decision_in_a_word(self):
        from care.prescriptions import current_prescription, renew_prescription
        from care.starting_plans import initiate_prescription

        from .patient_summary import treatment_timeline

        first = self.tirzepatide_plan()
        # Two weeks in, the doctor starts the plan again, then renews it.
        moved = timedelta(weeks=2)
        for step in TreatmentAuthorization.objects.filter(patient=self.patient):
            TreatmentAuthorization.objects.filter(pk=step.pk).update(
                starts_on=step.starts_on - moved, expires_on=step.expires_on - moved, created_at=step.created_at - moved)
        again = initiate_prescription(company=self.company, patient=self.patient, actor=self.doctor,
                                      current=current_prescription(self.company, self.patient),
                                      plan=StartingPlan.objects.get(medicine='Tirzepatide'), weeks=24)
        renewal = renew_prescription(company=self.company, patient=self.patient, actor=self.doctor,
                                     current=current_prescription(self.company, self.patient), weeks=4)
        events = [event['text'] for week in treatment_timeline(self.company, self.patient, renewal)['weeks'] for event in week['events']]
        self.assertEqual(events, ['Prescription started', 'Prescription reinitiated', 'Prescription renewed'])
        self.assertEqual(again.replaces.plan_key, first.plan_key)
        # Started again the day it was written, a prescription that never took effect isn't in the timeline.
        Shipment.objects.all().delete()
        TreatmentAuthorization.objects.update(replaces=None)
        TreatmentAuthorization.objects.all().delete()
        plan = StartingPlan.objects.get(medicine='Tirzepatide')
        first = initiate_prescription(company=self.company, patient=self.patient, actor=self.doctor, current=None, plan=plan, weeks=24)
        again = initiate_prescription(company=self.company, patient=self.patient, actor=self.doctor, current=first, plan=plan, weeks=24)
        events = [event['text'] for week in treatment_timeline(self.company, self.patient, again)['weeks'] for event in week['events']]
        self.assertEqual(events, ['Prescription started'])

    def test_each_week_shows_its_first_and_last_day(self):
        from datetime import date

        from .patient_summary import week_range

        self.assertEqual([week_range(date(2026, 10, 19)), week_range(date(2026, 10, 26)), week_range(date(2026, 12, 28))],
                         ['19–25 Oct 2026', '26 Oct–1 Nov 2026', '28 Dec 2026–3 Jan 2027'])

    def test_patients_see_each_medicines_starting_plan_with_its_prices(self):
        plan = StartingPlan.objects.create(company=self.company, medicine='Tirzepatide', then_dose=self.tirz, default_weeks=24)
        StartingPlanStep.objects.create(company=self.company, plan=plan, position=1, dose=self.tirz_start, weeks=2)
        sema = StartingPlan.objects.create(company=self.company, medicine='Semaglutide', then_dose=self.sema, default_weeks=24)
        StartingPlanStep.objects.create(company=self.company, plan=sema, position=1, dose=self.sema, weeks=4)
        pricing = treatment_pricing(self.company)
        self.assertEqual([(medicine['medicine'], [(row['when'], str(row['dose'])) for row in medicine['rows']])
                          for medicine in pricing['treatment_prices']], [
            ('Semaglutide', [('Weeks 1–4', 'Semaglutide 0.25 mg weekly'), ('Thereafter', 'Semaglutide 0.25 mg weekly')]),
            ('Tirzepatide', [('Weeks 1–2', 'Tirzepatide 2.5 mg weekly'), ('Thereafter', 'Tirzepatide 5 mg weekly')]),
        ])
        self.assertEqual(pricing['treatment_ships'], 'Every 1 to 4 weeks, by dose')
        # A plan with a dose that can't be prescribed isn't published.
        MedicationDose.objects.filter(pk=self.tirz.pk).update(price=None)
        self.assertEqual([medicine['medicine'] for medicine in treatment_pricing(self.company)['treatment_prices']], ['Semaglutide'])


class DoseMigrationTests(TransactionTestCase):
    """Each main medicine strength becomes a pen with its one dose, so current prescriptions and plans carry on."""

    before = [('care', '0026_seed_starting_plans')]
    after = [('care', '0029_remove_months_and_practice_pricing')]

    def migrate(self, targets):
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(targets)
        others = [node for node in executor.loader.graph.leaf_nodes() if node[0] != 'care']
        return executor.loader.project_state(targets + others).apps

    def tearDown(self):
        self.migrate(MigrationExecutor(connection).loader.graph.leaf_nodes())
        super().tearDown()

    def test_the_catalogue_moves_to_doses_and_weeks(self):
        old = self.migrate(self.before)
        doctor = old.get_model('accounts', 'User').objects.create(email='migrate-dose-doctor@example.test', password='!')
        company = old.get_model('practices', 'Company').objects.create(name='Migrate', slug='migrate-doses')
        patient = old.get_model('practices', 'Patient').objects.create(company=company, first_name='M', last_name='P')
        old.get_model('care', 'PracticeSettings').objects.create(company=company, delivery_interval_days=35)
        Product = old.get_model('care', 'MedicationProduct')
        product = dict(company=company, category='weight_management', requires_authorisation=True, dose_frequency='weekly')
        quarter = Product.objects.create(name='Semaglutide', strength='0.25 mg', price='1995.00', **product)
        one = Product.objects.create(name='Semaglutide', strength='1.0 mg', price='3295.00', **product)
        add_on = Product.objects.create(company=company, name='Ondansetron', strength='4 mg', price='50.00', category='other')
        plan = old.get_model('care', 'StartingPlan').objects.create(company=company, medicine='Semaglutide', then_product=one,
                                                                     default_months=6)
        old.get_model('care', 'StartingPlanStep').objects.create(company=company, plan=plan, position=1, product=quarter, months=2)
        Authorization = old.get_model('care', 'TreatmentAuthorization')
        today = timezone.localdate()
        prescription = Authorization.objects.create(company=company, patient=patient, prescribed_by=doctor, product=quarter,
                                                    max_dose='0.25 mg weekly', starts_on=today, expires_on=today + timedelta(days=90))
        other = Authorization.objects.create(company=company, patient=patient, prescribed_by=doctor, product=add_on,
                                             max_dose='4 mg as needed', starts_on=today, expires_on=today + timedelta(days=90))

        new = self.migrate(self.after)
        Dose = new.get_model('care', 'MedicationDose')
        doses = {dose.pen_id: dose for dose in Dose.objects.filter(company_id=company.pk)}
        self.assertEqual(set(doses), {quarter.pk, one.pk})
        self.assertEqual((doses[quarter.pk].amount_mg, doses[quarter.pk].frequency, doses[quarter.pk].price,
                          doses[quarter.pk].pens_per_dispatch, doses[quarter.pk].dispatch_weeks, doses[quarter.pk].billing_weeks),
                         (Decimal('0.25'), 'weekly', Decimal('1995.00'), 1, 5, 5))
        plan = new.get_model('care', 'StartingPlan').objects.get(pk=plan.pk)
        step = new.get_model('care', 'StartingPlanStep').objects.get(plan_id=plan.pk)
        self.assertEqual((plan.then_dose_id, plan.default_weeks, step.dose_id, step.weeks),
                         (doses[one.pk].pk, 24, doses[quarter.pk].pk, 8))
        Authorization = new.get_model('care', 'TreatmentAuthorization')
        self.assertEqual(Authorization.objects.get(pk=prescription.pk).dose_id, doses[quarter.pk].pk)
        self.assertIsNone(Authorization.objects.get(pk=other.pk).dose_id)
