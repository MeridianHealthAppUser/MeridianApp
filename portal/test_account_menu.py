"""Shared account identity, role-aware destinations and safe sign-out markup."""
from django.test import override_settings
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from practices.models import Company, CompanyMembership, Patient
from practices.services import ACTIVE_COMPANY_SESSION_KEY, ACTIVE_PATIENT_COMPANY_SESSION_KEY


@override_settings(MULTI_PRACTICE_ENABLED=True)
class AccountMenuPresentationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.company = Company.objects.create(name='Menu practice', slug='menu-practice')
        cls.doctor = User.objects.create_user(email='menu-doctor@example.test', first_name='Sam', last_name='Marchant')
        cls.admin = User.objects.create_user(email='menu-admin@example.test', first_name='Practice', last_name='Administrator')
        cls.super_admin = User.objects.create_user(email='menu-super@example.test', first_name='Super', last_name='Admin')
        cls.patient_user = User.objects.create_user(email='menu-patient@example.test', first_name='Nadia', last_name='Mokoena')
        cls.unassigned = User.objects.create_user(email='menu-unassigned@example.test')
        for user, role in ((cls.doctor, 'doctor'), (cls.admin, 'practice_admin'), (cls.super_admin, 'super_admin')):
            CompanyMembership.objects.create(company=cls.company, user=user, role=role)
        cls.patient = Patient.objects.create(company=cls.company, user=cls.patient_user, assigned_doctor=cls.doctor, first_name='Nadia', last_name='Mokoena')

    def login(self, user):
        self.client.force_login(user)
        session = self.client.session
        session[ACTIVE_COMPANY_SESSION_KEY] = self.company.pk
        session[ACTIVE_PATIENT_COMPANY_SESSION_KEY] = self.company.pk
        session.save()

    def menu(self, response):
        return response.content.decode().split('<details class="account-menu"', 1)[1].split('</details>', 1)[0]

    def test_every_staff_role_has_top_right_identity_and_no_sidebar_copy(self):
        for user, label in ((self.doctor, 'Doctor'), (self.admin, 'Practice administrator'), (self.super_admin, 'Super admin')):
            self.login(user)
            response = self.client.get(reverse('portal:desktop-dashboard'))
            self.assertContains(response, f'Account menu for {user.full_name}, {label}')
            self.assertContains(response, 'account_menu.css')
            self.assertContains(response, 'id="company-select"')
            self.assertNotContains(response, 'console-sidebar__footer')
            self.assertNotContains(response, 'class="account-name"')
            self.assertNotContains(response, 'class="header-signout"')
            self.assertIn(reverse('accounts:profile'), self.menu(response))
            self.assertIn(reverse('portal:account-access-history'), self.menu(response))
            sidebar = response.content.decode().split('aria-label="Staff pages"', 1)[1].split('</nav>', 1)[0]
            self.assertNotIn('>Account</span>', sidebar)
            self.assertNotIn(reverse('portal:account-access-history'), sidebar)
            self.assertNotIn(reverse('accounts:password-change'), sidebar)

    def test_initials_and_disclosure_accessibility_are_present(self):
        self.login(self.doctor)
        response = self.client.get(reverse('portal:staff-tasks'))
        self.assertContains(response, '<span class="account-menu__avatar" aria-hidden="true">SM</span>', html=True)
        self.assertContains(response, 'aria-controls="account-menu-panel"')
        self.assertContains(response, 'aria-label="Your account"')
        self.assertContains(response, 'account_menu.js')

    def test_all_legacy_staff_shells_remove_duplicate_sidebar_identity(self):
        self.login(self.doctor)
        for url in (reverse('portal:staff-inbox'), reverse('portal:patient-detail', args=[self.patient.pk]), reverse('portal:patient-list')):
            response = self.client.get(url)
            self.assertNotContains(response, 'console-sidebar__footer')
            self.assertContains(response, 'data-account-menu')

    def test_patient_identity_and_destinations_are_not_staff_role(self):
        self.login(self.patient_user)
        response = self.client.get(reverse('portal:patient-dashboard'))
        self.assertContains(response, 'Account menu for Nadia Mokoena, Patient')
        self.assertContains(response, 'id="patient-company-select"')
        self.assertNotContains(response, 'class="patient-console__account"')
        for name in ('patient-account', 'patient-privacy', 'patient-data-requests'):
            self.assertIn(reverse(f'portal:{name}'), self.menu(response))
        self.assertNotIn(reverse('portal:account-access-history'), self.menu(response))

    def test_mixed_identity_matches_the_portal_and_keeps_switch_links(self):
        Patient.objects.create(company=self.company, user=self.doctor, first_name='Sam', last_name='Marchant')
        self.login(self.doctor)
        staff = self.client.get(reverse('portal:desktop-dashboard'))
        self.assertContains(staff, 'Account menu for Sam Marchant, Doctor')
        self.assertIn('Switch to my care', self.menu(staff))
        patient = self.client.get(reverse('portal:patient-dashboard'))
        self.assertContains(patient, 'Account menu for Sam Marchant, Patient')
        self.assertIn('Switch to workspace', self.menu(patient))

    def test_account_security_page_keeps_identity_for_all_authenticated_accounts(self):
        for user, label in ((self.doctor, 'Doctor'), (self.patient_user, 'Patient'), (self.unassigned, 'Account')):
            self.login(user)
            response = self.client.get(reverse('accounts:password-change'))
            self.assertContains(response, f'Account menu for {user.full_name}, {label}')

    def test_sign_out_is_only_a_post_form_with_csrf(self):
        self.login(self.doctor)
        response = self.client.get(reverse('portal:desktop-dashboard'))
        menu = self.menu(response)
        self.assertIn(f'method="post" action="{reverse("accounts:logout")}"', menu)
        self.assertIn('name="csrfmiddlewaretoken"', menu)
        self.assertIn('<button type="submit">Sign out</button>', menu)
        self.assertNotIn(f'href="{reverse("accounts:logout")}"', menu)

    def test_anonymous_pages_have_no_account_controls(self):
        response = self.client.get(reverse('accounts:login'))
        self.assertNotContains(response, 'data-account-menu')
        self.assertNotContains(response, 'account_menu.css')
