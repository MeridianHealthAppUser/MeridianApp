"""Shared styling remains an authenticated workspace concern."""
from django.test import override_settings
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from care.messaging import add_participant
from care.models import MessageThread
from practices.models import Company, CompanyMembership, Patient
from practices.services import ACTIVE_COMPANY_SESSION_KEY, ACTIVE_PATIENT_COMPANY_SESSION_KEY
from .test_markup import actionable_paths


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

    def test_overview_opens_records_and_conversations_with_secondary_buttons(self):
        thread = MessageThread.objects.create(company=self.company, patient=self.patient, subject='Dose question',
                                              opened_by=self.patient_user, last_message_at=timezone.now())
        add_participant(thread, self.super_admin)
        self.login(self.super_admin)
        record = reverse('portal:patient-detail', args=[self.patient.pk])
        for name in ('desktop-dashboard', 'mobile-dashboard'):
            with self.subTest(page=name):
                response = self.page(name)
                self.assertContains(response, f'<a class="console-button console-button--secondary" href="{record}">Open record<span class="sr-only"> for {self.patient}</span></a>')
                self.assertContains(response, f'<a class="console-button console-button--secondary" href="{record}?thread={thread.pk}#conversation-{thread.pk}">View conversation')
        # Who's in each conversation shows as tags, the patient first, as in Patient messages.
        for name in ('desktop-dashboard', 'mobile-dashboard'):
            with self.subTest(tags=name):
                response = self.page(name)
                self.assertEqual([tag['kind'] for tag in response.context['recent_messages'][0].people_tags], ['patient'])
                self.assertContains(response, '<span class="person-tags"><span class="sr-only">With </span><span class="person-tag person-tag--patient" title="Example Patient · Patient">')
                self.assertContains(response, 'css/person_tags.css')
        # A closed conversation has the "Closed" tag from Patient messages, just before View conversation.
        MessageThread.objects.filter(pk=thread.pk).update(is_closed=True)
        for name in ('desktop-dashboard', 'mobile-dashboard'):
            with self.subTest(closed=name):
                self.assertContains(self.page(name), f'<div class="message-list__actions"><span class="record-inbox-pill">Closed</span><a class="console-button console-button--secondary" href="{record}?thread={thread.pk}')
        # On the desktop Overview the button sits against the right edge of the row.
        self.assertContains(self.page('desktop-dashboard'), f'<td class="patient-table__action-cell"><a class="console-button console-button--secondary" href="{record}">Open record')

    def test_overview_has_spaced_metric_labels_and_compact_style_hook(self):
        self.login(self.super_admin)
        for name in ('desktop-dashboard', 'mobile-dashboard'):
            response = self.page(name)
            self.assertContains(response, 'workspace-overview')
            self.assertContains(response, '<span class="metric-card__label">Open tasks</span>', html=True)
            self.assertContains(response, '<span class="metric-card__label">Unread messages</span>', html=True)
            self.assertNotContains(response, 'Opentasks')

    def test_clinician_overview_has_no_metric_cards_and_no_single_practice_privacy_note(self):
        self.login(self.doctor)
        for name in ('desktop-dashboard', 'mobile-dashboard'):
            response = self.page(name)
            self.assertNotContains(response, 'metric-card')
            self.assertContains(response, 'Upcoming appointments')
        with self.settings(MULTI_PRACTICE_ENABLED=False, SINGLE_PRACTICE_SLUG=self.company.slug):
            self.assertNotContains(self.page('desktop-dashboard'), 'Practice privacy.')
        self.assertContains(self.page('desktop-dashboard'), 'Practice privacy.')

    def test_polish_stylesheet_is_loaded_after_page_css(self):
        self.login(self.doctor)
        response = self.page('staff-tasks')
        html = response.content.decode()
        self.assertGreater(html.index('workspace_polish.css'), html.index('console.css'))
        self.assertContains(response, 'class="workspace-page"')
        self.assertContains(response, 'href="#workspace-content">Skip to content')

    def test_overview_greeting_is_time_neutral_and_appointment_count_is_labelled_today(self):
        self.login(self.super_admin)
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
        paths = actionable_paths(response)
        for name in ('staff-tasks', 'staff-schedule', 'ops-catalogue', 'policy-list', 'management-users', 'metrics', 'staff-data-requests'):
            self.assertIn(reverse(f'portal:{name}'), paths)
        self.login(self.doctor)
        response = self.page('desktop-dashboard')
        paths = actionable_paths(response)
        self.assertNotIn(reverse('portal:management-users'), paths)
        self.assertNotIn(reverse('portal:staff-data-requests'), paths)

    def test_admin_task_title_and_navigation_use_short_label(self):
        self.login(self.super_admin)
        response = self.page('staff-tasks')
        self.assertContains(response, '<h1>Tasks</h1>', html=True)
        self.assertNotContains(response, 'Practice tasks')
        self.assertEqual(response.context['page_title'], 'Tasks')
