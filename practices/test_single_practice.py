"""The default deployment keeps all data but exposes only Meridian Health."""

from io import StringIO
from datetime import date, timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied
from django.core.management import call_command
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse

from accounts.forms import EmailAuthenticationForm
from care.models import MessageThread, PatientMessage
from care.services import post_patient_message
from .models import Company, CompanyMembership, Patient
from .services import (
    ACTIVE_COMPANY_SESSION_KEY, ACTIVE_PATIENT_COMPANY_SESSION_KEY,
    active_membership_for, active_patient_for, available_companies_for,
    available_patient_companies_for, get_active_company, get_active_patient_company,
)
from .tenancy import enabled_companies


@override_settings(MULTI_PRACTICE_ENABLED=False, SINGLE_PRACTICE_SLUG='meridian-health')
class SinglePracticeBoundaryTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.meridian = Company.objects.create(name='Meridian Health', slug='meridian-health')
        cls.other = Company.objects.create(name='Other Practice', slug='other')
        cls.doctor = get_user_model().objects.create_user('shared@example.com', 'Test-Access!2026')
        cls.patient_user = get_user_model().objects.create_user('patient@example.com', 'Test-Access!2026')
        cls.hidden_user = get_user_model().objects.create_user('hidden@example.com', 'Test-Access!2026')
        for company in (cls.meridian, cls.other):
            CompanyMembership.objects.create(company=company, user=cls.doctor, role='doctor')
        CompanyMembership.objects.create(company=cls.other, user=cls.hidden_user, role='super_admin')
        cls.patient = Patient.objects.create(company=cls.meridian, user=cls.patient_user, first_name='Local', last_name='Patient')
        cls.hidden_patient = Patient.objects.create(company=cls.other, user=cls.patient_user, first_name='Hidden', last_name='Patient')
        cls.hidden_thread = MessageThread.objects.create(company=cls.other, patient=cls.hidden_patient, subject='Hidden conversation')

    def request(self, user):
        request = RequestFactory().get('/')
        request.user = user
        request.session = {ACTIVE_COMPANY_SESSION_KEY: self.other.pk, ACTIVE_PATIENT_COMPANY_SESSION_KEY: self.other.pk}
        return request

    def test_only_pinned_practice_is_available_even_with_shared_memberships(self):
        self.assertEqual(list(enabled_companies()), [self.meridian])
        self.assertEqual(list(available_companies_for(self.doctor)), [self.meridian])
        self.assertEqual(list(available_patient_companies_for(self.patient_user)), [self.meridian])

    def test_old_staff_and_patient_sessions_resolve_to_meridian(self):
        request = self.request(self.doctor)
        self.assertEqual(get_active_company(request), self.meridian)
        self.assertEqual(request.session[ACTIVE_COMPANY_SESSION_KEY], self.meridian.pk)
        request = self.request(self.patient_user)
        self.assertEqual(get_active_patient_company(request), self.meridian)
        self.assertEqual(request.session[ACTIVE_PATIENT_COMPANY_SESSION_KEY], self.meridian.pk)

    def test_explicit_other_company_cannot_bypass_session_resolver(self):
        self.assertIsNone(active_membership_for(self.request(self.doctor), self.other))
        self.assertIsNone(active_patient_for(self.request(self.patient_user), self.other))

    def test_hidden_membership_never_grants_meridian_access(self):
        request = self.request(self.hidden_user)
        self.assertIsNone(get_active_company(request))
        self.assertNotIn(ACTIVE_COMPANY_SESSION_KEY, request.session)
        self.assertFalse(available_companies_for(self.hidden_user).exists())

    def test_missing_pinned_practice_fails_closed(self):
        with override_settings(SINGLE_PRACTICE_SLUG='missing'):
            self.assertFalse(enabled_companies().exists())
            self.assertIsNone(get_active_company(self.request(self.doctor)))
            self.assertIsNone(get_active_patient_company(self.request(self.patient_user)))

    def test_inactive_meridian_does_not_fall_back_to_other_practice(self):
        Company.objects.filter(pk=self.meridian.pk).update(is_active=False)
        self.assertFalse(enabled_companies().exists())
        self.assertIsNone(get_active_company(self.request(self.doctor)))

    def test_staff_switch_endpoint_is_disabled_for_every_target(self):
        self.client.force_login(self.doctor)
        for company in (self.meridian, self.other):
            response = self.client.post(reverse('portal:activate-company', args=[company.slug]))
            self.assertEqual(response.status_code, 403)

    def test_patient_switch_endpoint_is_disabled_for_every_target(self):
        self.client.force_login(self.patient_user)
        for company in (self.meridian, self.other):
            response = self.client.post(reverse('portal:activate-patient-company', args=[company.slug]))
            self.assertEqual(response.status_code, 403)

    def test_switch_endpoint_does_not_reveal_whether_another_practice_exists(self):
        for user, route in ((self.doctor, 'portal:activate-company'),
                            (self.patient_user, 'portal:activate-patient-company')):
            self.client.force_login(user)
            self.assertEqual(self.client.post(reverse(route, args=['does-not-exist'])).status_code, 403)

    def test_old_patient_detail_link_is_not_accessible(self):
        self.client.force_login(self.doctor)
        response = self.client.get(reverse('portal:patient-detail', args=[self.hidden_patient.pk]))
        self.assertEqual(response.status_code, 404)

    def test_hidden_thread_cannot_receive_patient_or_staff_messages(self):
        for sender in (self.doctor, self.patient_user):
            with self.assertRaises(PermissionDenied):
                post_patient_message(thread=self.hidden_thread, sender=sender, body='Not allowed')
        self.assertFalse(PatientMessage.objects.exists())

    def test_shared_accounts_keep_their_existing_passwords(self):
        for user in (self.doctor, self.patient_user):
            form = EmailAuthenticationForm(data={'email': user.email, 'password': 'Test-Access!2026'})
            self.assertTrue(form.is_valid(), form.errors)
            self.assertEqual(form.get_user(), user)

    def test_other_practice_only_account_cannot_log_in(self):
        form = EmailAuthenticationForm(data={'email': self.hidden_user.email, 'password': 'Test-Access!2026'})
        self.assertFalse(form.is_valid())
        self.assertEqual(form.errors.as_data()['__all__'][0].code, 'invalid_login')

    def test_restoring_feature_restores_existing_access_without_data_recreation(self):
        with override_settings(MULTI_PRACTICE_ENABLED=True):
            self.assertEqual(available_companies_for(self.doctor).count(), 2)
            self.assertEqual(get_active_company(self.request(self.doctor)), self.other)
        self.assertEqual(Company.objects.count(), 2)
        self.assertEqual(CompanyMembership.objects.filter(user=self.doctor).count(), 2)
        self.assertEqual(Patient.objects.filter(user=self.patient_user).count(), 2)

    def test_clinical_and_operations_service_guards_reject_disabled_practice(self):
        from care.availability import _lock_own_doctor
        from care.clinical import _lock_context, _require_doctor
        from care.operations import require_operations_actor
        from care.patient_care import _own_active_patient
        from care.review_rules import _lock_practice
        from care.task_services import _staff_membership

        checks = (
            lambda: _lock_context(self.other, self.hidden_patient, self.doctor),
            lambda: _require_doctor(self.other, self.doctor),
            lambda: require_operations_actor(self.other, self.hidden_user),
            lambda: _own_active_patient(self.other, self.hidden_patient, self.patient_user),
            lambda: _lock_practice(self.other, self.hidden_user),
            lambda: _staff_membership(self.other, self.doctor),
            lambda: _lock_own_doctor(self.other, self.doctor, self.doctor),
        )
        for check in checks:
            with self.subTest(check=check), self.assertRaises(PermissionDenied):
                check()


@override_settings(DEBUG=True, MULTI_PRACTICE_ENABLED=False, SINGLE_PRACTICE_SLUG='meridian-health')
class SinglePracticeDemoTests(TestCase):
    def test_demo_seed_creates_only_meridian(self):
        call_command('seed_demo', stdout=StringIO())
        self.assertEqual(list(Company.objects.values_list('slug', flat=True)), ['meridian-health'])
        self.assertEqual(CompanyMembership.objects.count(), 3)
        # Four staff and patient accounts, plus three more of Sam's patients.
        self.assertEqual(get_user_model().objects.count(), 7)
        self.assertEqual(
            list(Patient.objects.filter(assigned_doctor__email='sam.marchant@meridianhealth.co.za').order_by('medical_record_number')
                 .values_list('first_name', 'medical_record_number')),
            [('Nadia', 'MH-DEMO-001'), ('Ayesha', 'MH-DEMO-002'), ('Pieter', 'MH-DEMO-003'), ('Zanele', 'MH-DEMO-004')],
        )
        self.assertFalse(Patient.objects.filter(user__isnull=True).exists())

    def test_demo_seed_never_reactivates_an_archived_other_practice(self):
        other = Company.objects.create(name='Archived Orion', slug='orion-mens-health', is_active=False)
        call_command('seed_demo', stdout=StringIO())
        other.refresh_from_db()
        self.assertFalse(other.is_active)
        self.assertEqual(other.name, 'Archived Orion')
        self.assertFalse(CompanyMembership.objects.filter(company=other).exists())


@override_settings(DEBUG=True, MULTI_PRACTICE_ENABLED=False, SINGLE_PRACTICE_SLUG='meridian-health', PAYMENT_PROVIDER='sandbox')
class BillingDemoSeedTests(TestCase):
    @patch('django.utils.timezone.localdate', return_value=date(2026, 11, 3))
    def test_the_billing_demo_puts_each_patient_in_a_different_billing_and_shipping_state(self, localdate):
        from care.billing import billing_state
        from care.models import ClinicalTask, PatientSubscription, Shipment
        from care.operations import lock_reasons

        call_command('seed_demo', stdout=StringIO())
        call_command('seed_billing_demo', stdout=StringIO())
        states = {plan.patient.first_name: billing_state(plan)[0] for plan in PatientSubscription.objects.select_related('patient')}
        self.assertEqual({name: states.get(name) for name in ('Ayesha', 'Pieter', 'Zanele', 'Lerato', 'Ruan', 'Megan', 'Thabo')},
                         {'Ayesha': 'paid', 'Pieter': 'retrying', 'Zanele': 'failed', 'Lerato': 'update', 'Ruan': 'no_mandate',
                          'Megan': 'paid', 'Thabo': 'pending'})
        self.assertTrue(ClinicalTask.objects.filter(title__startswith='Debit failed: follow up with Zanele').exists())
        week = Shipment.objects.filter(status=Shipment.Status.READY).select_related('patient')
        clear = sorted(parcel.patient.first_name for parcel in week if not lock_reasons(parcel) and parcel.patient.first_name != 'Nadia')
        # Seeded on a Tuesday, the shipping day: its pens were ordered by last Thursday's cut-off, so what was paid for
        # by then goes out (Busi paused after paying, so hers still does; Ruan paid by EFT). Everything decided today
        # goes out next Tuesday: Ashleigh's and Kagiso's first payments, Sipho's and James's dose changes, Mark's late
        # payment (25 Nov 2026).
        self.assertEqual(clear, ['Ayesha', 'Busi', 'Lerato', 'Ruan'])
        for name in ('Ashleigh', 'Kagiso', 'Sipho', 'James', 'Mark'):
            following = Shipment.objects.filter(patient__first_name=name, status__in=('draft', 'ready'),
                                                scheduled_for__gte=date(2026, 11, 3)).order_by('scheduled_for').first()
            self.assertEqual(following.scheduled_for, date(2026, 11, 10), name)
        self.assertTrue(Shipment.objects.filter(status=Shipment.Status.DELIVERED).exists())
        # Card payments only (22 Nov 2026): no DebiCheck; Sipho's same-price dose change left the card alone; Kagiso's
        # and Precious's courses start on their first payment; Johan (who asked) and Precious (who paid late) wait for
        # the doctor; Naledi can't start without the doctor.
        from care.billing import paid_before_pause
        from care.models import AuditEvent, DebitMandate, SubscriptionCycle, TreatmentAuthorization
        from care.prescriptions import start_check

        self.assertFalse(DebitMandate.objects.exclude(method='card').exists())
        self.assertEqual({name: states.get(name) for name in ('Sipho', 'Kagiso', 'Precious', 'Busi')},
                         {'Sipho': 'paid', 'Kagiso': 'paid', 'Precious': 'paid', 'Busi': 'paid'})
        # Each course's weeks run from its first pens; only those paid for after a later order cut-off than they were
        # written move, and tell the patient.
        from care.models import PatientEvent

        self.assertEqual(set(AuditEvent.objects.filter(action='prescription.start_moved').values_list('patient__first_name', flat=True)),
                         {'Kagiso', 'Precious'})
        self.assertEqual(set(PatientEvent.objects.filter(title='Treatment start moved').values_list('patient__first_name', flat=True)),
                         {'Kagiso', 'Precious'})
        self.assertEqual(set(TreatmentAuthorization.objects.filter(needs_confirmation=True).values_list('patient__first_name', flat=True)),
                         {'Johan', 'Precious'})
        naledi = TreatmentAuthorization.objects.get(patient__first_name='Naledi')
        self.assertFalse(start_check(naledi)['asked'])
        self.assertTrue(any(paid_before_pause(parcel) for parcel in Shipment.objects.filter(patient__first_name='Busi')))
        # Treatment starts on the first payment: each paying patient's prescription is valid from the day they first
        # paid, and that payment's period from its parcel; a parcel whose payment didn't go through was missed (held,
        # unpaid): Zanele's.
        from care.parcels import first_shipping_day

        for name in ('Ayesha', 'Pieter', 'Zanele', 'Lerato', 'Megan', 'Sipho', 'Busi', 'Kagiso'):
            first_paid = SubscriptionCycle.objects.filter(patient__first_name=name).order_by('cycle_number').first()
            started = TreatmentAuthorization.objects.filter(patient__first_name=name).order_by('starts_on').first()
            self.assertEqual((started.starts_on, first_paid.starts_on),
                             (first_paid.due_on, first_shipping_day(first_paid.due_on, first_paid.company)), name)
        self.assertTrue(Shipment.objects.filter(patient__first_name='Zanele', status=Shipment.Status.HELD,
                                                hold_reason__startswith='Not paid').exists())
        # Tirzepatide at different points: Ashleigh starting the plan, Pierre stepping up to 5 mg, James moved to 7.5 mg
        # in week 9 (a 5-week period) and to 10 mg today in week 14 (4-weekly again), Andre every 5 weeks, Mark carried
        # on after a late payment.
        from care.billing import billing_weeks
        from care.prescriptions import moved_note

        plans = {plan.patient.first_name: plan for plan in PatientSubscription.objects.exclude(status='cancelled').filter(
            patient__first_name__in=('Ashleigh', 'Pierre', 'James', 'Andre', 'Mark')).select_related('patient', 'authorization')}
        self.assertEqual({name: billing_state(plan)[0] for name, plan in plans.items()},
                         {'Ashleigh': 'paid', 'Pierre': 'paid', 'James': 'paid', 'Andre': 'paid', 'Mark': 'paid'})
        self.assertEqual(TreatmentAuthorization.objects.filter(patient__first_name='Ashleigh').count(), 2)  # Plan steps.
        james = plans['James']
        self.assertEqual([(cycle.ends_on - cycle.starts_on).days + 1 for cycle in james.cycles.order_by('cycle_number')],
                         [28, 28, 35, 28])
        # The next payment a week before the next 10 mg parcel.
        self.assertEqual(james.next_debit_on, james.cycles.order_by('cycle_number').last().ends_on + timedelta(days=1 - 7))
        self.assertEqual(billing_weeks(james, james.next_debit_on + timedelta(weeks=1)), 4)
        self.assertEqual(list(TreatmentAuthorization.objects.filter(patient__first_name='James', replaces__isnull=False)
                              .order_by('starts_on').values_list('max_dose', flat=True)), ['7.5 mg weekly', '10 mg weekly'])
        self.assertEqual(james.authorization.max_dose, '10 mg weekly')
        andre = list(plans['Andre'].cycles.order_by('cycle_number').values_list('starts_on', flat=True))
        self.assertEqual({(later - earlier).days for earlier, later in zip(andre, andre[1:])}, {35})
        self.assertEqual(moved_note(plans['Mark'].authorization)['because'], 'after a missed payment')
        self.assertTrue(Shipment.objects.filter(patient__first_name='Mark', hold_reason__startswith='Missed: ').exists())
        with self.assertRaisesMessage(Exception, 'already seeded'):
            call_command('seed_billing_demo', stdout=StringIO())
