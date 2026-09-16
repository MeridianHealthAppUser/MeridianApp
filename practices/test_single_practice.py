"""The default deployment keeps all data but exposes only Meridian Health."""

from io import StringIO

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
        self.assertEqual(get_user_model().objects.count(), 4)

    def test_demo_seed_never_reactivates_an_archived_other_practice(self):
        other = Company.objects.create(name='Archived Orion', slug='orion-mens-health', is_active=False)
        call_command('seed_demo', stdout=StringIO())
        other.refresh_from_db()
        self.assertFalse(other.is_active)
        self.assertEqual(other.name, 'Archived Orion')
        self.assertFalse(CompanyMembership.objects.filter(company=other).exists())
