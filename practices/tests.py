from django.test import override_settings
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from .models import Company, CompanyMembership, Patient
from .services import ACTIVE_COMPANY_SESSION_KEY, get_active_company


@override_settings(MULTI_PRACTICE_ENABLED=True)
class CompanyScopingTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user('doctor@example.com', 'password')
        self.first_company = Company.objects.create(name='Alpha Practice', slug='alpha')
        self.second_company = Company.objects.create(name='Beta Practice', slug='beta')
        CompanyMembership.objects.create(
            user=self.user,
            company=self.first_company,
            role=CompanyMembership.Role.DOCTOR,
        )
        CompanyMembership.objects.create(
            user=self.user,
            company=self.second_company,
            role=CompanyMembership.Role.PRACTICE_ADMIN,
        )

    def test_user_can_switch_between_membership_companies(self):
        self.client.force_login(self.user)
        response = self.client.post(
            reverse('portal:activate-company', args=[self.second_company.slug]),
            {'next': reverse('portal:mobile-dashboard')},
        )

        self.assertRedirects(response, reverse('portal:mobile-dashboard'))
        self.assertEqual(self.client.session[ACTIVE_COMPANY_SESSION_KEY], self.second_company.pk)

    def test_user_cannot_switch_to_an_unrelated_company(self):
        unrelated = Company.objects.create(name='Private Practice', slug='private')
        self.client.force_login(self.user)

        response = self.client.post(reverse('portal:activate-company', args=[unrelated.slug]))

        self.assertEqual(response.status_code, 403)

    def test_switch_does_not_allow_an_external_return_url(self):
        self.client.force_login(self.user)

        response = self.client.post(
            reverse('portal:activate-company', args=[self.second_company.slug]),
            {'next': 'https://malicious.example'},
        )

        self.assertRedirects(response, reverse('portal:desktop-dashboard'), fetch_redirect_response=False)

    def test_company_scoped_queryset_does_not_mix_patient_records(self):
        Patient.objects.create(company=self.first_company, first_name='Amy', last_name='One')
        Patient.objects.create(company=self.second_company, first_name='Ben', last_name='Two')

        self.assertEqual(Patient.objects.for_company(self.first_company).count(), 1)
        self.assertEqual(Patient.objects.for_company(self.second_company).count(), 1)

    def test_first_available_company_becomes_active_context(self):
        request = self.client.request().wsgi_request
        request.user = self.user
        request.session = self.client.session

        company = get_active_company(request)

        self.assertEqual(company, self.first_company)
