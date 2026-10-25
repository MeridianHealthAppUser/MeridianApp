"""Clinicians and practice administrators get one flat menu; Super Admins see sections, and on mobile each section
has a page of its options."""

import re

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from care.messaging import start_conversation
from care.models import PracticeSettings
from care.team_messaging import start_team_conversation
from practices.models import Company, CompanyMembership, Patient
from practices.services import ACTIVE_COMPANY_SESSION_KEY
from .test_markup import actionable_paths


@override_settings(MULTI_PRACTICE_ENABLED=False, SINGLE_PRACTICE_SLUG='menu-practice')
class StaffMenuTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.company = Company.objects.create(name='Menu Practice', slug='menu-practice')
        # Doctors don't ship here (test_doctors_who_ship_get_weekly_shipping switches it on).
        PracticeSettings.objects.create(company=cls.company, doctors_ship=False)
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
        paths = actionable_paths('<nav ' + bar + '</nav>')
        return [key for key in ('care', 'operations', 'administration', 'insights', 'settings')
                if reverse('portal:staff-menu-section', args=[key]) in paths]

    def test_each_role_sees_only_its_sections_on_the_mobile_bar(self):
        expected = {
            # Clinicians and practice administrators have a flat menu instead of sections.
            'doctor': [],
            'practice_admin': [],
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
        paths = actionable_paths(page)
        for name in ('treatment-review-rules', 'management-users', 'policy-list', 'practice-settings'):
            self.assertIn(reverse(f'portal:{name}'), paths)
        self.assertNotIn(reverse('portal:management-practices'), paths)
        self.assertContains(page, 'aria-current="page"')
        for role in ('doctor', 'practice_admin'):
            self.login(role)
            for section in ('care', 'operations', 'administration', 'insights', 'settings'):
                self.assertEqual(self.client.get(reverse('portal:staff-menu-section', args=[section])).status_code, 404)
        self.assertEqual(self.client.get(reverse('portal:staff-menu-section', args=['invented'])).status_code, 404)

    def test_active_practice_box_is_hidden_for_a_single_practice(self):
        self.login('super_admin')
        self.assertNotContains(self.client.get(reverse('portal:staff-tasks')), 'Active practice')

    def test_doctors_see_no_operations_insights_or_settings_links_and_cannot_open_them(self):
        self.login('doctor')
        pages = ('treatment-subscriptions', 'ops-orders', 'ops-shipping', 'ops-history', 'ops-catalogue',
                 'metrics', 'activity-statements', 'treatment-review-rules')
        overview = self.client.get(reverse('portal:desktop-dashboard'))
        self.assertNotIn('shipments_due', overview.context['metrics'])
        paths = actionable_paths(overview)
        for name in pages:
            with self.subTest(page=name):
                self.assertNotIn(reverse(f'portal:{name}'), paths)
                self.assertEqual(self.client.get(reverse(f'portal:{name}')).status_code, 403)
        # A Super Admin who is also a doctor keeps every section.
        CompanyMembership.objects.filter(user=self.people['super_admin']).update(clinician_type='doctor')
        self.login('super_admin')
        overview = self.client.get(reverse('portal:desktop-dashboard'))
        self.assertIn('shipments_due', overview.context['metrics'])
        paths = actionable_paths(overview)
        for name in pages:
            with self.subTest(page=name, role='super_admin'):
                self.assertIn(reverse(f'portal:{name}'), paths)
                self.assertEqual(self.client.get(reverse(f'portal:{name}')).status_code, 200)

    def test_doctors_who_ship_get_weekly_shipping_and_dispatch_history_only(self):
        PracticeSettings.objects.filter(company=self.company).update(doctors_ship=True)
        self.login('doctor')
        overview = self.client.get(reverse('portal:desktop-dashboard')).content.decode()
        sidebar = overview[overview.index('class="console-nav'):]
        sidebar = sidebar[:sidebar.index('</nav>')]
        self.assertIn(f'href="{reverse("portal:ops-shipping")}"', sidebar)
        self.assertIn('<span class="console-nav__text">Weekly shipping</span>', sidebar)
        page = self.client.get(reverse('portal:ops-shipping'))
        self.assertEqual(page.status_code, 200)
        tabs = page.content.decode()
        tabs = tabs[tabs.index('class="patient-workspace-tabs"'):]
        tabs = tabs[:tabs.index('</nav>')]
        self.assertEqual(re.findall(r'<a href="[^"]+"[^>]*>([^<]+)', tabs), ['Weekly shipping', 'Dispatch history'])
        self.assertEqual(self.client.get(reverse('portal:ops-history')).status_code, 200)
        for name in ('treatment-subscriptions', 'ops-orders', 'ops-catalogue', 'ops-billing', 'staff-leads'):
            with self.subTest(page=name):
                self.assertEqual(self.client.get(reverse(f'portal:{name}')).status_code, 403)

    def test_clinicians_get_a_flat_menu_with_patient_records_and_messages_and_its_red_unread_count(self):
        doctor, admin = self.people['doctor'], self.people['practice_admin']
        patient_user = get_user_model().objects.create_user(email='patient@menu.test')
        patient = Patient.objects.create(company=self.company, user=patient_user, first_name='Pat', last_name='Patient',
                                         assigned_doctor=doctor)
        # One unread patient message and one unread team message.
        start_conversation(patient=patient, opened_by=patient_user, subject='Dose', body='Can we talk?', recipient=doctor)
        start_team_conversation(company=self.company, actor=admin, members=[doctor], subject='Rota', body='Friday?')
        self.login('doctor')
        page = self.client.get(reverse('portal:desktop-dashboard')).content.decode()
        sidebar = page[page.index('class="console-nav'):]
        sidebar = sidebar[:sidebar.index('</nav>')]
        self.assertNotIn('<details', sidebar)
        labels = ['Overview', 'My tasks', 'Patient Records &amp; Messages', 'Calendar &amp; Bookings']
        self.assertEqual([label for label in labels if label in sidebar], labels)
        self.assertNotIn('My Messages', sidebar)
        # Both red navigation badges combine patient/team unread counts.
        self.assertIn('<span class="message-unread-dot" data-live-unread="total" aria-hidden="true">2</span>', sidebar)
        self.assertIn('data-live-unread-label="total">, 2 unread messages</span>', sidebar)
        mobile = page[page.index('class="mobile-bottom-nav staff-mobile-nav"'):]
        self.assertIn('<span class="message-unread-dot" data-live-unread="total" aria-hidden="true">2</span>', mobile[:mobile.index('</nav>')])
        inbox = self.client.get(reverse('portal:staff-inbox'))
        self.assertContains(inbox, '<h1>Patient Records &amp; Messages</h1>', html=True)
        # Practice admins get a flat menu too, with Messages (no patient list), then Operations and Administration.
        # They sent the team message, so nothing is unread for them.
        self.login('practice_admin')
        page = self.client.get(reverse('portal:desktop-dashboard')).content.decode()
        sidebar = page[page.index('class="console-nav'):]
        sidebar = sidebar[:sidebar.index('</nav>')]
        self.assertNotIn('<details', sidebar)
        labels = ['Overview', 'My tasks', 'Messages', 'Calendar &amp; Bookings', 'Operations', 'Administration']
        self.assertEqual(re.findall(r'<span class="console-nav__text">([^<]+)</span>', sidebar), labels)
        self.assertNotIn('Insights', sidebar)
        self.assertIn('data-live-unread="total" hidden', sidebar)
        self.assertContains(self.client.get(reverse('portal:staff-inbox')), '<h1>Messages</h1>', html=True)

    def test_practice_admins_open_operations_and_administration_as_pages_of_tabs_without_insights(self):
        self.login('practice_admin')
        tabs = {
            'Operations': [('treatment-subscriptions', 'Subscriptions'), ('ops-orders', 'Non-prescription requests'),
                           ('ops-shipping', 'Weekly shipping'), ('ops-history', 'Dispatch history'),
                           ('ops-catalogue', 'Catalogue'), ('ops-billing', 'Billing')],
            'Administration': [('staff-leads', 'Leads'), ('dropouts', 'Dropouts'), ('staff-data-requests', 'Data requests'),
                               ('email-log', 'Emails')],
        }
        for group, pages in tabs.items():
            for name, label in pages:
                with self.subTest(page=name):
                    response = self.client.get(reverse(f'portal:{name}'))
                    self.assertContains(response, f'<h1>{group}</h1>', html=True)
                    html = response.content.decode()
                    nav = html[html.index('class="patient-workspace-tabs"'):]
                    nav = nav[:nav.index('</nav>')]
                    self.assertEqual(re.findall(r'<a href="[^"]+"[^>]*>([^<]+)', nav), [tab for _, tab in pages])
                    self.assertIn(f'<a href="{reverse(f"portal:{name}")}" class="is-active" aria-current="page">{label}', nav)
                    # Each tab's table has its search at the top right with the Filters menu before it, except
                    # Dispatch history, which opens on a week: its search only, the week being the dates.
                    self.assertContains(response, 'class="staff-list-tools"')
                    self.assertContains(response, 'data-conversation-search')
                    if name != 'ops-history':
                        self.assertTrue(response.context['filter_menu']['sections'])
                    self.assertNotContains(response, 'Apply filters')
                    # The menu entry stays current on every tab.
                    sidebar = html[html.index('class="console-nav'):]
                    self.assertIn(f'aria-current="true" href="{reverse(f"portal:{pages[0][0]}")}"', sidebar[:sidebar.index('</nav>')])
        for name in ('metrics', 'metrics-export'):
            self.assertEqual(self.client.get(reverse(f'portal:{name}')).status_code, 403)
        self.login('super_admin')
        self.assertEqual(self.client.get(reverse('portal:metrics')).status_code, 200)
