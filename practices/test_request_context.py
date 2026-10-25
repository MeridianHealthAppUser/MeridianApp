"""Request-local access resolution saves repeat reads without retaining access."""

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied
from django.test import RequestFactory, TestCase, override_settings

from .context_processors import active_company as template_context
from .models import Company, CompanyMembership, Patient
from .role_switching import switch_own_practice_role
from .services import (
    ACTIVE_COMPANY_SESSION_KEY, ACTIVE_PATIENT_COMPANY_SESSION_KEY,
    active_membership_for, active_patient_for, get_active_company,
    get_active_patient_company, set_active_company, set_active_patient_company,
)


@override_settings(MULTI_PRACTICE_ENABLED=True)
class RequestAccessContextTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user(
            email='context@example.test', is_staff=True, is_superuser=True,
        )
        cls.other_user = get_user_model().objects.create_user(email='other-context@example.test')
        cls.alpha = Company.objects.create(name='Alpha', slug='context-alpha')
        cls.beta = Company.objects.create(name='Beta', slug='context-beta')
        for company in (cls.alpha, cls.beta):
            CompanyMembership.objects.create(company=company, user=cls.user, role='super_admin')
            Patient.objects.create(company=company, user=cls.user, first_name='Context', last_name='Patient')

    def request(self, method='get'):
        request = getattr(RequestFactory(), method)('/')
        request.user = self.user
        request.session = {
            ACTIVE_COMPANY_SESSION_KEY: self.alpha.pk,
            ACTIVE_PATIENT_COMPANY_SESSION_KEY: self.alpha.pk,
        }
        return request

    def test_template_context_reuses_the_access_already_resolved_by_the_view(self):
        for method in ('get', 'head'):
            request = self.request(method)
            company = get_active_company(request)
            membership = active_membership_for(request, company)
            patient_company = get_active_patient_company(request)
            patient = active_patient_for(request, patient_company)
            with self.assertNumQueries(0):
                context = template_context(request)
            self.assertIs(context['active_company'], company)
            self.assertIs(context['active_membership'], membership)
            self.assertIs(context['active_patient'], patient)

    def test_explicit_selection_changes_cannot_reuse_another_practice(self):
        request = self.request()
        self.assertEqual(get_active_company(request), self.alpha)
        self.assertEqual(get_active_patient_company(request), self.alpha)
        request.session[ACTIVE_COMPANY_SESSION_KEY] = self.beta.pk
        request.session[ACTIVE_PATIENT_COMPANY_SESSION_KEY] = self.beta.pk
        self.assertEqual(get_active_company(request), self.beta)
        self.assertEqual(get_active_patient_company(request), self.beta)
        self.assertEqual(active_membership_for(request).company_id, self.beta.pk)
        self.assertEqual(active_patient_for(request).company_id, self.beta.pk)

    def test_setters_invalidate_even_when_selecting_the_same_practice(self):
        request = self.request()
        self.assertEqual(active_membership_for(request).role, 'super_admin')
        first = active_patient_for(request)
        CompanyMembership.objects.filter(company=self.alpha, user=self.user).update(role='doctor')
        Patient.objects.filter(pk=first.pk).update(first_name='Updated')
        set_active_company(request, self.alpha)
        set_active_patient_company(request, self.alpha)
        self.assertEqual(active_membership_for(request).role, 'doctor')
        self.assertEqual(active_patient_for(request).first_name, 'Updated')

    def test_new_request_observes_revoked_access(self):
        first = self.request()
        self.assertIsNotNone(active_membership_for(first, self.alpha))
        self.assertIsNotNone(active_patient_for(first, self.alpha))
        CompanyMembership.objects.filter(company=self.alpha, user=self.user).update(is_active=False)
        Patient.objects.filter(company=self.alpha, user=self.user).update(is_active=False)
        second = self.request()
        self.assertIsNone(active_membership_for(second, self.alpha))
        self.assertIsNone(active_patient_for(second, self.alpha))
        self.assertEqual(get_active_company(second), self.beta)
        self.assertEqual(get_active_patient_company(second), self.beta)
        with self.assertRaises(PermissionDenied):
            set_active_company(second, self.alpha)
        with self.assertRaises(PermissionDenied):
            set_active_patient_company(second, self.alpha)

    def test_changed_identity_and_deployment_boundary_discard_cached_access(self):
        request = self.request()
        self.assertEqual(get_active_company(request), self.alpha)
        self.assertIsNotNone(active_patient_for(request))
        request.user = self.other_user
        self.assertIsNone(get_active_company(request))
        self.assertIsNone(active_membership_for(request, self.alpha))
        self.assertIsNone(active_patient_for(request, self.alpha))
        request.user = self.user
        self.assertEqual(get_active_company(request), self.alpha)
        with override_settings(MULTI_PRACTICE_ENABLED=False, SINGLE_PRACTICE_SLUG=self.beta.slug):
            self.assertEqual(get_active_company(request), self.beta)
            self.assertIsNone(active_membership_for(request, self.alpha))

    def test_missing_access_is_cached_without_exposing_another_identity(self):
        request = self.request()
        request.user = self.other_user
        self.assertIsNone(get_active_company(request))
        self.assertIsNone(get_active_patient_company(request))
        with self.assertNumQueries(0):
            self.assertIsNone(get_active_company(request))
            self.assertIsNone(get_active_patient_company(request))

    @override_settings(MULTI_PRACTICE_ENABLED=False, SINGLE_PRACTICE_SLUG='context-alpha')
    def test_post_role_switch_reads_current_membership_within_the_same_request(self):
        request = self.request('post')
        self.assertEqual(active_membership_for(request).role, 'super_admin')
        switch_own_practice_role(actor=self.user, role='doctor', request=request)
        self.assertEqual(active_membership_for(request).role, 'doctor')
        self.assertFalse(hasattr(request, '_practice_read_context'))
