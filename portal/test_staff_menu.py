"""Mobile shows only the menu sections; each section has a page listing its options."""

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from practices.models import Company, CompanyMembership
from practices.services import ACTIVE_COMPANY_SESSION_KEY


@override_settings(MULTI_PRACTICE_ENABLED=False, SINGLE_PRACTICE_SLUG='menu-practice')
class StaffMenuTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.company = Company.objects.create(name='Menu Practice', slug='menu-practice')
        cls.people = {}
        for role in ('doctor', 'practice_admin', 'super_admin'):
            user = User.objects.create_user(email=f'{role}@menu.test')
            CompanyMembership.objects.create(company=cls.company, user=user, role=role)
            cls.people[role] = user

    def login(self, role):
        self.client.force_login(self.people[role])
        session = self.client.session
        session[ACTIVE_COMPANY_SESSION_KEY] = self.company.pk
        session.save()

    def bar_sections(self):
        html = self.client.get(reverse('portal:mobile-dashboard')).content.decode()
        bar = html[html.index('class="mobile-bottom-nav staff-mobile-nav"'):]
        bar = bar[:bar.index('</nav>')]
        return [key for key in ('care', 'operations', 'administration', 'insights', 'settings')
                if reverse('portal:staff-menu-section', args=[key]) in bar]

    def test_each_role_sees_only_its_sections_on_the_mobile_bar(self):
        expected = {
            'doctor': ['care', 'operations', 'insights', 'settings'],
            'practice_admin': ['care', 'operations', 'administration', 'insights'],
            'super_admin': ['care', 'operations', 'administration', 'insights', 'settings'],
        }
        for role, sections in expected.items():
            with self.subTest(role=role):
                self.login(role)
                self.assertEqual(self.bar_sections(), sections)

    def test_a_section_page_lists_its_options_and_hidden_sections_are_not_found(self):
        self.login('super_admin')
        page = self.client.get(reverse('portal:staff-menu-section', args=['settings']))
        self.assertContains(page, '<h1>Settings</h1>', html=True)
        for name in ('treatment-review-rules', 'management-users', 'policy-list', 'practice-settings'):
            self.assertContains(page, reverse(f'portal:{name}'))
        self.assertNotContains(page, reverse('portal:management-practices'))
        self.assertContains(page, 'aria-current="page"')
        self.login('doctor')
        self.assertEqual(self.client.get(reverse('portal:staff-menu-section', args=['administration'])).status_code, 404)
        self.assertEqual(self.client.get(reverse('portal:staff-menu-section', args=['invented'])).status_code, 404)

    def test_active_practice_box_is_hidden_for_a_single_practice(self):
        self.login('super_admin')
        self.assertNotContains(self.client.get(reverse('portal:staff-tasks')), 'Active practice')
