"""Shared styling remains an authenticated workspace concern."""
from django.test import override_settings
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from practices.models import Company, CompanyMembership, Patient
from practices.services import ACTIVE_COMPANY_SESSION_KEY, ACTIVE_PATIENT_COMPANY_SESSION_KEY


@override_settings(MULTI_PRACTICE_ENABLED=True)
class WorkspacePresentationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name='Presentation practice', slug='presentation-practice')
        cls.doctor = get_user_model().objects.create_user(email='presentation-doctor@example.test')
        cls.super_admin = get_user_model().objects.create_user(email='presentation-super@example.test')
        cls.patient_user = get_user_model().objects.create_user(email='presentation-patient@example.test')
        CompanyMembership.objects.create(company=cls.company, user=cls.doctor, role='doctor')
        CompanyMembership.objects.create(company=cls.company, user=cls.super_admin, role='super_admin')
        cls.patient = Patient.objects.create(company=cls.company, user=cls.patient_user, first_name='Example', last_name='Patient')

    def login(self, user):
        self.client.force_login(user)
        session = self.client.session
        session[ACTIVE_COMPANY_SESSION_KEY] = self.company.pk
        session[ACTIVE_PATIENT_COMPANY_SESSION_KEY] = self.company.pk
        session.save()

    def page(self, name):
        return self.client.get(reverse(f'portal:{name}'))

    def test_overview_has_spaced_metric_labels_and_compact_style_hook(self):
        self.login(self.doctor)
        for name in ('desktop-dashboard', 'mobile-dashboard'):
            response = self.page(name)
            self.assertContains(response, 'workspace-overview')
            self.assertContains(response, '<span class="metric-card__label">Open tasks</span>', html=True)
            self.assertContains(response, '<span class="metric-card__label">Unread messages</span>', html=True)
            self.assertNotContains(response, 'Opentasks')

    def test_polish_stylesheet_is_loaded_after_page_css(self):
        self.login(self.doctor)
        response = self.page('staff-tasks')
        html = response.content.decode()
        self.assertGreater(html.index('workspace_polish.css'), html.index('console.css'))
        self.assertContains(response, 'class="workspace-page"')
        self.assertContains(response, 'href="#workspace-content">Skip to content')

    def test_overview_greeting_is_time_neutral_and_appointment_count_is_labelled_today(self):
        self.login(self.doctor)
        for name in ('desktop-dashboard', 'mobile-dashboard'):
            response = self.page(name)
            self.assertContains(response, '>Welcome, ')
            self.assertNotContains(response, 'Good morning')
            self.assertContains(response, '<span class="metric-card__label">Appointments today</span>', html=True)
            self.assertIn('appointments', response.context['metrics'])

    def test_authenticated_public_pages_do_not_load_workspace_styles(self):
        self.login(self.doctor)
        for name in ('public-terms', 'public-privacy', 'public-contact', 'questionnaire'):
            self.assertNotContains(self.page(name), 'workspace_polish.css')
        self.assertNotContains(self.client.get(reverse('landing')), 'workspace_polish.css')

    def test_anonymous_pages_do_not_load_workspace_styles(self):
        for name in ('public-terms', 'public-privacy', 'public-contact', 'questionnaire'):
            self.assertNotContains(self.page(name), 'workspace_polish.css')

    def test_patient_workspace_loads_polish_without_staff_rail(self):
        self.login(self.patient_user)
        response = self.page('patient-dashboard')
        self.assertContains(response, 'workspace_polish.css')
        self.assertNotContains(response, 'data-rail-navigation')

    def test_desktop_rail_retains_role_appropriate_destinations_and_groups(self):
        self.login(self.super_admin)
        response = self.page('desktop-dashboard')
        self.assertContains(response, 'data-rail-navigation')
        for group in ('Care', 'Operations', 'Administration', 'Insights', 'Settings'):
            self.assertContains(response, f'data-navigation-group="{group.lower()}"')
            self.assertContains(response, f'</svg>{group}</span>')
        for name in ('staff-tasks', 'staff-schedule', 'ops-catalogue', 'policy-list', 'management-users', 'metrics', 'staff-data-requests'):
            self.assertContains(response, reverse(f'portal:{name}'))
        self.login(self.doctor)
        response = self.page('desktop-dashboard')
        self.assertNotContains(response, reverse('portal:management-users'))
        self.assertNotContains(response, reverse('portal:staff-data-requests'))

    def test_admin_task_title_and_navigation_use_short_label(self):
        self.login(self.super_admin)
        response = self.page('staff-tasks')
        self.assertContains(response, '<h1>Tasks</h1>', html=True)
        self.assertNotContains(response, 'Practice tasks')
        self.assertEqual(response.context['page_title'], 'Tasks')
