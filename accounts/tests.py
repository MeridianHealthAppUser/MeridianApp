from django.contrib.auth import authenticate, get_user_model
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse

from practices.models import Company, CompanyMembership, Patient


class UserIdentityTests(TestCase):
    def test_email_is_the_authentication_identifier(self):
        user = get_user_model().objects.create_user('DOCTOR@EXAMPLE.COM', 'safe-password')

        authenticated_user = authenticate(email='DOCTOR@example.com', password='safe-password')

        self.assertEqual(user.email, 'DOCTOR@example.com')
        self.assertEqual(authenticated_user, user)


class LoginViewTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            'doctor@example.com',
            'correct-horse-battery-staple',
            first_name='Demo',
            last_name='Doctor',
        )
        company = Company.objects.create(name='Demo Practice', slug='demo-practice')
        CompanyMembership.objects.create(
            user=self.user,
            company=company,
            role=CompanyMembership.Role.DOCTOR,
        )

    def test_email_login_redirects_to_the_workspace(self):
        response = self.client.post(
            reverse('accounts:login'),
            {
                'email': self.user.email,
                'password': 'correct-horse-battery-staple',
                'remember_me': 'on',
            },
        )

        self.assertRedirects(
            response,
            reverse('portal:desktop-dashboard'),
            fetch_redirect_response=False,
        )
        self.assertEqual(int(self.client.session['_auth_user_id']), self.user.pk)

    def test_login_honours_a_safe_next_url(self):
        response = self.client.post(
            reverse('accounts:login'),
            {
                'email': self.user.email,
                'password': 'correct-horse-battery-staple',
                'next': reverse('portal:mobile-dashboard'),
            },
        )

        self.assertRedirects(
            response,
            reverse('portal:mobile-dashboard'),
            fetch_redirect_response=False,
        )


class DemoSeedCommandTests(TestCase):
    @override_settings(DEBUG=True)
    def test_demo_seed_is_idempotent_and_creates_the_expected_access(self):
        call_command('seed_demo')
        call_command('seed_demo')

        joshua = get_user_model().objects.get(email='joshua.czech@meridianhealth.co.za')
        sam = get_user_model().objects.get(email='sam.marchant@meridianhealth.co.za')
        lindiwe = get_user_model().objects.get(email='lindiwe.mahlangu@meridianhealth.co.za')
        nadia = get_user_model().objects.get(email='nadia.m@example.co.za')

        self.assertFalse(joshua.is_superuser)
        self.assertFalse(joshua.has_perm('care.view_clinicalnote'))
        self.assertTrue(joshua.check_password('MeridianDemo!2026'))
        self.assertEqual(
            set(joshua.company_memberships.values_list('company__slug', 'role')),
            {
                ('meridian-health', CompanyMembership.Role.SUPER_ADMIN),
                ('orion-mens-health', CompanyMembership.Role.SUPER_ADMIN),
            },
        )
        self.assertEqual(
            set(sam.company_memberships.values_list('company__slug', 'role')),
            {
                ('meridian-health', CompanyMembership.Role.DOCTOR),
                ('orion-mens-health', CompanyMembership.Role.DOCTOR),
            },
        )
        self.assertEqual(
            list(lindiwe.company_memberships.values_list('company__slug', 'role')),
            [('meridian-health', CompanyMembership.Role.PRACTICE_ADMIN)],
        )
        self.assertEqual(Company.objects.filter(is_active=True).count(), 2)
        self.assertEqual(
            set(Patient.objects.filter(user=nadia).values_list('company__slug', flat=True)),
            {'meridian-health', 'orion-mens-health'},
        )
