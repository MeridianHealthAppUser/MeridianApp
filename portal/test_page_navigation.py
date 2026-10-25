"""Navigation shells and delayed HTML retain the ordinary page access boundary."""

from datetime import timedelta
import json
import os
from pathlib import Path
import shutil
import subprocess
from tempfile import TemporaryDirectory
from unittest import skipUnless
from unittest.mock import patch
from urllib.parse import urlsplit

from django.contrib.auth import get_user_model
from django.conf import settings
from django.contrib.messages import SUCCESS
from django.contrib.messages.storage.base import Message
from django.contrib.messages.storage.cookie import MessageEncoder
from django.db import connection
from django.test import Client, SimpleTestCase, TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from care.models import (
    Appointment, AuditEvent, ClinicalNote, MessageThread, MessageThreadParticipant,
    PatientMessage, PracticeSettings,
)
from practices.models import Company, CompanyMembership, Patient
from practices.services import ACTIVE_COMPANY_SESSION_KEY, ACTIVE_PATIENT_COMPANY_SESSION_KEY


@override_settings(MULTI_PRACTICE_ENABLED=True)
class PageNavigationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        users = get_user_model().objects
        cls.company = Company.objects.create(name='Navigation Practice', slug='navigation-practice')
        cls.other_company = Company.objects.create(name='Other Navigation Practice', slug='other-navigation-practice')
        cls.doctor = users.create_user(email='navigation-doctor@example.test')
        cls.other_doctor = users.create_user(email='navigation-other-doctor@example.test')
        cls.admin = users.create_user(email='navigation-administrator@example.test')
        cls.patient_user = users.create_user(email='navigation-patient@example.test')
        cls.membership = CompanyMembership.objects.create(company=cls.company, user=cls.doctor, role='doctor')
        CompanyMembership.objects.create(company=cls.other_company, user=cls.doctor, role='doctor')
        CompanyMembership.objects.create(company=cls.company, user=cls.other_doctor, role='doctor')
        CompanyMembership.objects.create(company=cls.company, user=cls.admin, role='practice_admin')
        cls.patient = Patient.objects.create(company=cls.company, user=cls.patient_user,
                                             first_name='Navigation', last_name='Patient', assigned_doctor=cls.doctor)
        cls.other_patient = Patient.objects.create(company=cls.other_company, first_name='Foreign',
                                                   last_name='Private', assigned_doctor=cls.doctor)
        cls.unassigned_patient = Patient.objects.create(company=cls.company, first_name='Unassigned',
                                                        last_name='Private', assigned_doctor=cls.other_doctor)
        cls.thread = MessageThread.objects.create(company=cls.company, patient=cls.patient, subject='Authorized conversation')
        MessageThreadParticipant.objects.create(company=cls.company, thread=cls.thread, user=cls.doctor)
        cls.message = PatientMessage.objects.create(company=cls.company, thread=cls.thread,
                                                    sender=cls.doctor, body='AUTHORIZED_NAVIGATION_MESSAGE')
        cls.incoming = PatientMessage.objects.create(company=cls.company, thread=cls.thread,
                                                     sender=cls.patient_user, body='AUTHORIZED_PATIENT_MESSAGE')
        cls.foreign_thread = MessageThread.objects.create(company=cls.other_company, patient=cls.other_patient,
                                                          subject='FOREIGN_NAVIGATION_CONVERSATION')
        PatientMessage.objects.create(company=cls.other_company, thread=cls.foreign_thread,
                                      sender=cls.doctor, body='FOREIGN_NAVIGATION_MESSAGE')
        ClinicalNote.objects.create(company=cls.company, patient=cls.patient, author=cls.doctor,
                                    body='AUTHORIZED_NAVIGATION_NOTE')
        PracticeSettings.objects.create(company=cls.company, store_view_log=True)

    def login(self, user=None, *, patient=False, company=None):
        self.client.force_login(user or self.doctor)
        session = self.client.session
        session[ACTIVE_PATIENT_COMPANY_SESSION_KEY if patient else ACTIVE_COMPANY_SESSION_KEY] = (company or self.company).pk
        session.save()

    def content(self, url, query=None, *, scope=None):
        headers = {'HTTP_X_MERIDIAN_NAVIGATION': 'content'}
        if scope is not None:
            headers['HTTP_X_MERIDIAN_NAVIGATION_SCOPE'] = scope
        return self.client.get(url, query or {}, **headers)

    def test_registered_shells_skip_body_queries_and_message_read_side_effects(self):
        cases = (
            (self.doctor, False, 'portal:desktop-dashboard', {}, 'portal.views.StaffDashboardContextMixin.get_context_data'),
            (self.doctor, False, 'portal:staff-inbox', {'thread': self.thread.pk}, 'portal.inbox.staff_inbox_context'),
            (self.patient_user, True, 'portal:patient-messages', {'thread': self.thread.pk}, 'portal.patient_views.patient_messages_context'),
            (self.patient_user, True, 'portal:patient-appointments', {}, 'portal.patient_views.patient_appointments_context'),
        )
        before_audits = AuditEvent.objects.count()
        for user, patient, route, query, body_builder in cases:
            with self.subTest(route=route):
                self.login(user, patient=patient)
                with patch(body_builder, side_effect=AssertionError('Shell evaluated page body')), CaptureQueriesContext(connection) as queries:
                    response = self.client.get(reverse(route), {'__shell': '1', **query})
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, 'data-page-navigation')
                self.assertNotContains(response, self.message.body)
                self.assertNotContains(response, self.incoming.body)
                for query in queries:
                    self.assertNotIn('"care_patientmessage"', query['sql'])
                    self.assertNotIn('"care_auditevent"', query['sql'])
        self.assertEqual(AuditEvent.objects.count(), before_audits)
        self.message.refresh_from_db()
        self.incoming.refresh_from_db()
        self.assertIsNone(self.message.read_at)
        self.assertIsNone(self.incoming.staff_read_at)

    def test_browser_document_get_uses_shell_but_full_and_existing_http_fallback_render_content(self):
        self.login(self.patient_user, patient=True)
        url = reverse('portal:patient-messages')
        query = {'thread': self.thread.pk}
        shell = self.client.get(url, query, HTTP_ACCEPT='text/html')
        self.assertNotContains(shell, self.message.body)
        for data, headers in ((query, {}), ({**query, '__full': '1'}, {'HTTP_ACCEPT': 'text/html'})):
            response = self.client.get(url, data, **headers)
            self.assertEqual(response.status_code, 200)
            self.assertContains(response, self.message.body)
            self.assertIn('text/html', response['Content-Type'])

    def test_team_shell_rejects_an_empty_selected_thread_like_the_full_view(self):
        self.login()
        url = reverse('portal:team-inbox')
        self.assertEqual(self.client.get(url, {'thread': '', '__shell': '1'}).status_code, 404)
        self.assertEqual(self.client.get(url, {'thread': '', '__full': '1'}).status_code, 404)

    def test_doctor_shipping_shells_follow_the_practice_setting_like_the_full_view(self):
        self.login()
        for name in ('ops-shipping', 'ops-history'):
            self.assertEqual(self.client.get(reverse(f'portal:{name}'), HTTP_ACCEPT='text/html').status_code, 200)
        PracticeSettings.objects.filter(company=self.company).update(doctors_ship=False)
        for name in ('ops-shipping', 'ops-history'):
            self.assertEqual(self.client.get(reverse(f'portal:{name}'), HTTP_ACCEPT='text/html').status_code, 403)
            self.assertEqual(self.client.get(reverse(f'portal:{name}'), {'__full': '1'}).status_code, 403)

    def test_realtime_snapshots_remain_rendered_html_even_with_browser_accept_header(self):
        self.login(self.patient_user, patient=True)
        response = self.client.get(reverse('portal:patient-messages'), {'thread': self.thread.pk},
                                   HTTP_ACCEPT='text/html', HTTP_X_MERIDIAN_REALTIME='snapshot')
        self.assertContains(response, self.message.body)
        self.assertIn('text/html', response['Content-Type'])
        self.message.refresh_from_db()
        self.assertIsNone(self.message.read_at)

    def test_content_contract_returns_authorized_html_and_private_uncached_metadata(self):
        self.login(self.patient_user, patient=True)
        url = reverse('portal:patient-messages')
        response = self.content(url, {'thread': self.thread.pk})
        self.assertEqual(response.status_code, 200)
        self.assertIn('no-store', response['Cache-Control'])
        payload = response.json()
        self.assertEqual(payload['version'], 1)
        self.assertEqual(payload['layout'], 'patient')
        self.assertEqual(payload['scope'], f'patient:{self.company.pk}:{self.patient.pk}:{self.patient_user.pk}')
        self.assertEqual(urlsplit(payload['canonical_url']).path, url)
        self.assertIn(self.message.body, payload['html'])
        self.assertNotIn('FOREIGN_NAVIGATION_MESSAGE', payload['html'])
        self.assertNotIn('<html', payload['html'].lower())
        self.assertIsInstance(payload['title'], str)
        self.assertIsInstance(payload['assets']['styles'], list)
        self.assertIsInstance(payload['assets']['scripts'], list)
        self.assertIn('data-live-messages', payload['live_messages_html'])
        self.assertIn('csrfmiddlewaretoken', payload['live_messages_html'])
        self.assertIn(payload['scope'], payload['live_messages_html'])

    def test_staff_scope_changes_after_role_or_practice_change_before_private_content_is_rendered(self):
        self.login()
        url = reverse('portal:staff-inbox')
        first = self.content(url).json()
        self.assertEqual(first['layout'], 'staff')
        CompanyMembership.objects.filter(pk=self.membership.pk).update(role='practice_admin', clinician_type='')
        with patch('portal.inbox.staff_inbox_context', side_effect=AssertionError('Stale role rendered body')):
            changed_role = self.content(url, scope=first['scope'])
        self.assertEqual(changed_role.status_code, 409)
        self.assertNotContains(changed_role, self.message.body, status_code=409)
        current = self.content(url).json()
        self.assertNotEqual(current['scope'], first['scope'])
        session = self.client.session
        session[ACTIVE_COMPANY_SESSION_KEY] = self.other_company.pk
        session.save()
        changed_practice = self.content(url, scope=current['scope'])
        self.assertEqual(changed_practice.status_code, 409)
        self.assertNotContains(changed_practice, 'FOREIGN_NAVIGATION_MESSAGE', status_code=409)

    def test_fragment_header_cannot_bypass_patient_staff_or_tenant_authorization(self):
        for shell in (False, True):
            with self.subTest(shell=shell):
                self.login(self.patient_user, patient=True)
                staff_url = reverse('portal:staff-inbox')
                response = self.client.get(staff_url, {'__shell': '1'}) if shell else self.content(staff_url)
                self.assertEqual(response.status_code, 403)
                self.login()
                patient_url = reverse('portal:patient-messages')
                response = self.client.get(patient_url, {'__shell': '1'}) if shell else self.content(patient_url)
                self.assertEqual(response.status_code, 403)
                foreign_url = reverse('portal:patient-detail', args=[self.other_patient.pk])
                response = self.client.get(foreign_url, {'__shell': '1'}) if shell else self.content(foreign_url)
                self.assertEqual(response.status_code, 404)
                self.assertNotContains(response, self.other_patient.last_name, status_code=404)

    def test_anonymous_and_revoked_membership_cannot_receive_private_fragments(self):
        url = reverse('portal:staff-inbox')
        response = self.content(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(urlsplit(response.json()['redirect_url']).path, reverse('accounts:login'))
        self.assertNotContains(response, self.message.body)
        self.login()
        scope = self.content(url).json()['scope']
        CompanyMembership.objects.filter(user=self.doctor).update(is_active=False)
        response = self.content(url, scope=scope)
        self.assertIn(response.status_code, (403, 409))
        self.assertNotContains(response, self.message.body, status_code=response.status_code)

    def test_record_shell_checks_assignment_and_valid_tabs_before_body_or_audit(self):
        self.login()
        before = AuditEvent.objects.count()
        denied = self.client.get(reverse('portal:patient-detail', args=[self.unassigned_patient.pk]), {'__shell': '1'})
        self.assertEqual(denied.status_code, 302)
        self.assertEqual(urlsplit(denied['Location']).path, reverse('portal:patient-list'))
        own = reverse('portal:patient-detail', args=[self.patient.pk])
        with patch('portal.patient_workspace.workspace_tab_context', side_effect=AssertionError('Shell evaluated record body')):
            response = self.client.get(own, {'__shell': '1', 'tab': 'overview'})
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'AUTHORIZED_NAVIGATION_NOTE')
        invalid = self.client.get(own, {'__shell': '1', 'tab': 'not-a-real-tab'})
        self.assertEqual(invalid.status_code, 404)
        canonical = self.client.get(own, {'__shell': '1', 'tab': 'notes'})
        self.assertEqual(canonical.status_code, 302)
        self.assertIn('tab=overview', canonical['Location'])
        self.assertEqual(AuditEvent.objects.count(), before)

    def test_content_redirects_retain_record_permissions_and_the_canonical_destination(self):
        self.login()
        denied = self.content(reverse('portal:patient-detail', args=[self.unassigned_patient.pk]))
        self.assertEqual(denied.status_code, 200)
        self.assertEqual(urlsplit(denied.json()['redirect_url']).path, reverse('portal:patient-list'))
        own = reverse('portal:patient-detail', args=[self.patient.pk])
        redirected = self.content(own, {'tab': 'notes'})
        self.assertEqual(redirected.status_code, 200)
        self.assertEqual(redirected.json()['redirect_url'], own + '?tab=overview')
        self.assertNotContains(redirected, 'AUTHORIZED_NAVIGATION_NOTE')
        missing = self.content('/patients/999999999/')
        self.assertEqual(missing.status_code, 404)
        self.assertNotContains(missing, 'AUTHORIZED_NAVIGATION_NOTE', status_code=404)

    def test_post_actions_keep_csrf_and_are_not_converted_to_navigation_gets(self):
        self.login(self.patient_user, patient=True)
        url = reverse('portal:patient-message-seen', args=[self.thread.pk])
        response = self.client.get(url, {'__shell': '1'}, HTTP_X_MERIDIAN_NAVIGATION='content')
        self.assertEqual(response.status_code, 405)
        secured = Client(enforce_csrf_checks=True)
        secured.force_login(self.patient_user)
        rejected = secured.post(url, {'message_ids': [self.message.pk]}, content_type='application/json',
                                HTTP_X_MERIDIAN_NAVIGATION='content')
        self.assertEqual(rejected.status_code, 403)
        self.message.refresh_from_db()
        self.assertIsNone(self.message.read_at)

    def test_dashboard_counts_are_deferred_without_delaying_or_rebuilding_the_working_lists(self):
        CompanyMembership.objects.filter(pk=self.membership.pk).update(role='super_admin')
        self.login()
        url = reverse('portal:desktop-dashboard')
        with patch('portal.views.staff_dashboard_metrics', side_effect=AssertionError('Primary content loaded deferred metrics')):
            content = self.content(url)
        self.assertEqual(content.status_code, 200)
        self.assertIn('data-content-section="dashboard-metrics"', content.json()['html'])
        self.assertIn('Upcoming appointments', content.json()['html'])
        with patch('portal.views.StaffDashboardContextMixin.get_context_data', side_effect=AssertionError('Section rebuilt whole dashboard')), \
                patch('portal.views.staff_dashboard_metrics', return_value={'patients': 17}) as metrics:
            section = self.client.get(url, HTTP_X_MERIDIAN_NAVIGATION='section',
                                      HTTP_X_MERIDIAN_SECTION='dashboard-metrics',
                                      HTTP_X_MERIDIAN_NAVIGATION_SCOPE=content.json()['scope'])
        self.assertEqual(section.status_code, 200)
        self.assertEqual(section.json()['section'], 'dashboard-metrics')
        self.assertIn('17', section.json()['html'])
        self.assertNotIn('Upcoming appointments', section.json()['html'])
        self.assertIn('no-store', section['Cache-Control'])
        metrics.assert_called_once()
        with patch('portal.views.staff_dashboard_metrics', return_value={'patients': 17}) as metrics:
            full = self.client.get(url)
        self.assertContains(full, '17')
        self.assertNotContains(full, 'data-content-section="dashboard-metrics"')
        metrics.assert_called_once()

    def test_deferred_section_rechecks_role_scope_and_registered_section_name(self):
        self.login()
        url = reverse('portal:desktop-dashboard')
        headers = {'HTTP_X_MERIDIAN_NAVIGATION': 'section', 'HTTP_X_MERIDIAN_SECTION': 'dashboard-metrics'}
        with patch('portal.views.staff_dashboard_metrics', side_effect=AssertionError('Unauthorized metrics query')):
            self.assertEqual(self.client.get(url, **headers).status_code, 403)
            self.assertEqual(self.client.get(url, **{**headers, 'HTTP_X_MERIDIAN_SECTION': 'arbitrary-data'}).status_code, 404)
            self.assertEqual(self.client.get(url, **headers, HTTP_X_MERIDIAN_NAVIGATION_SCOPE='staff:other').status_code, 409)

    @override_settings(PAGE_NAVIGATION_ENABLED=False)
    def test_feature_switch_retains_complete_normal_rendering(self):
        self.login(self.patient_user, patient=True)
        response = self.client.get(reverse('portal:patient-messages'), {'thread': self.thread.pk}, HTTP_ACCEPT='text/html')
        self.assertContains(response, self.message.body)
        self.assertNotContains(response, 'id="page-navigation-config"')

    @override_settings(MESSAGE_STORAGE='django.contrib.messages.storage.session.SessionStorage')
    def test_shell_preserves_flash_and_booking_confirmation_until_content_is_rendered_once(self):
        from .patient_views import JUST_BOOKED

        self.login(self.patient_user, patient=True)
        appointment = Appointment.objects.create(company=self.company, patient=self.patient, clinician=self.doctor,
                                                 starts_at=timezone.now() + timedelta(days=2), duration_minutes=30)
        session = self.client.session
        session[JUST_BOOKED] = appointment.pk
        session['_messages'] = MessageEncoder().encode([Message(SUCCESS, 'Saved exactly once')])
        session.save()
        url = reverse('portal:patient-appointments')
        shell = self.client.get(url, {'__shell': '1'})
        self.assertEqual(shell.status_code, 200)
        self.assertNotContains(shell, 'Saved exactly once')
        self.assertEqual(self.client.session[JUST_BOOKED], appointment.pk)
        self.assertIn('_messages', self.client.session)
        first = self.content(url)
        self.assertEqual(first.status_code, 200)
        self.assertIn('Saved exactly once', first.json()['toast_html'])
        self.assertNotIn(JUST_BOOKED, self.client.session)
        self.assertNotIn('_messages', self.client.session)
        second = self.content(url)
        self.assertEqual(second.json()['toast_html'], '')


class RenderedNavigationFragmentTests(SimpleTestCase):
    def test_svg_self_closing_tags_and_entities_survive_content_extraction(self):
        from .page_navigation import _RenderedPage

        content = '<section><svg><path d="M0 0"/><circle r="2"/></svg><br/><p>Care &amp; support &#38; follow-up</p></section>'
        parser = _RenderedPage()
        parser.feed(f'<html><head><title>Care</title></head><body><main id="workspace-content">{content}</main><footer>Outside</footer></body></html>')
        self.assertEqual(''.join(parser.parts), content)
        self.assertEqual(parser.depth, 0)

    def test_a_page_running_another_sites_script_opens_as_a_full_page(self):
        from django.http import HttpResponse
        from django.test import RequestFactory
        from .page_navigation import PageNavigationMiddleware

        # Peach's card form: its widget script can't be added to a swapped-in page, so the browser loads it fully.
        widget = '<script src="https://eu-test.oppwa.com/v1/paymentWidgets.js?checkoutId=x"></script>'
        for script, status in ((widget, 409), ('<script src="/static/js/dialogs.js"></script>', 200)):
            request = RequestFactory().get('/patient/billing/card/mandate/1/', HTTP_X_MERIDIAN_NAVIGATION='content')
            request._page_navigation = {'scope': 'patient:1:1:1', 'layout': 'patient'}
            page = HttpResponse(f'<html><head><title>Card</title>{script}</head><body><main id="workspace-content">'
                                '<form class="paymentWidgets"></form></main></body></html>')
            response = PageNavigationMiddleware(lambda request: page)(request)
            self.assertEqual(response.status_code, status)
            if status == 409:
                self.assertEqual(json.loads(response.content)['full_navigation'], '/patient/billing/card/mandate/1/')


class NavigationBuildSignatureTests(SimpleTestCase):
    def test_unhashed_deploys_change_signature_when_source_or_templates_change(self):
        from .page_navigation import _source_build_signature, build_signature

        self.addCleanup(_source_build_signature.cache_clear)
        with TemporaryDirectory() as directory, override_settings(BASE_DIR=Path(directory)), \
                patch('portal.page_navigation.staticfiles_storage') as storage:
            storage.manifest_hash = ''
            root = Path(directory)
            script = root / 'static' / 'js' / 'app.js'
            template = root / 'templates' / 'page.html'
            backend = root / 'portal' / 'views.py'
            for path, content in ((script, 'old script'), (template, '<h1>Old</h1>'), (backend, 'old view')):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content)
            previous = build_signature()
            self.assertNotEqual(previous, 'development')
            # Source files are immutable within a deployment; each replacement
            # process computes a fresh signature before serving its first page.
            for path, content in ((script, 'new script'), (template, '<h1>New</h1>'), (backend, 'new view')):
                path.write_text(content)
                self.assertEqual(build_signature(), previous)
                _source_build_signature.cache_clear()
                changed = build_signature()
                self.assertNotEqual(changed, previous)
                previous = changed
            (root / '.env').write_text('UNRELATED_LOCAL_SECRET=not-a-build-input')
            (root / 'db.sqlite3').write_bytes(b'not-a-real-database')
            _source_build_signature.cache_clear()
            self.assertEqual(build_signature(), previous)

    def test_collected_static_manifest_is_the_preferred_build_identity(self):
        from .page_navigation import build_signature

        with patch('portal.page_navigation.staticfiles_storage') as storage, \
                patch('portal.page_navigation._source_build_signature') as fallback:
            storage.manifest_hash = 'collected-static-manifest'
            self.assertEqual(build_signature(), 'collected-static-manifest')
            fallback.assert_not_called()


@override_settings(MULTI_PRACTICE_ENABLED=True, PAGE_NAVIGATION_ENABLED=True, DEBUG=True,
                   VIDEO_ENABLED=True, ALLOWED_HOSTS=['testserver', 'meridian.test'],
                   EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend')
class RenderedNavigationBrowserTests(TestCase):
    """Actual Django responses and static assets; only network transport is simulated."""

    setUpTestData = classmethod(PageNavigationTests.setUpTestData.__func__)
    login = PageNavigationTests.login
    content = PageNavigationTests.content

    @skipUnless(os.environ.get('MERIDIAN_NAVIGATION_BROWSER') == '1', 'Optional rendered Playwright navigation checks')
    def test_rendered_patient_doctor_and_administrator_navigation(self):
        super_admin = get_user_model().objects.create_user(email='navigation-owner@example.test')
        CompanyMembership.objects.create(company=self.company, user=super_admin, role='super_admin')
        appointment = Appointment.objects.create(company=self.company, patient=self.patient, clinician=self.doctor,
                                                 starts_at=timezone.now() + timedelta(days=2), duration_minutes=15)
        patient_urls = [reverse(name) for name in (
            'portal:patient-dashboard', 'portal:patient-appointments', 'portal:patient-messages',
            'portal:patient-treatment', 'portal:patient-pharmacy')]
        doctor_urls = [reverse(name) for name in (
            'portal:desktop-dashboard', 'portal:staff-inbox', 'portal:patient-list')]
        doctor_urls += [reverse('portal:patient-detail', args=[self.patient.pk]) + '?tab=overview',
                        reverse('portal:staff-schedule')]
        datasets = []
        for role, user, urls in (('patient', self.patient_user, patient_urls), ('doctor', self.doctor, doctor_urls),
                                 ('super_admin', super_admin, [reverse('portal:desktop-dashboard')])):
            self.login(user, patient=role == 'patient')
            shell = self.client.get(urls[0], HTTP_ACCEPT='text/html')
            self.assertEqual(shell.status_code, 200)
            self.assertContains(shell, '"shell": true')
            entry = {'role': role, 'initial': urls[0], 'shell': shell.content.decode(), 'flow': urls,
                     'pages': {}, 'endpoints': {}, 'sections': {}}
            extra_urls = []
            if role != 'super_admin':
                inbox = reverse('portal:patient-messages' if role == 'patient' else 'portal:staff-inbox')
                extra_urls.append(f'{inbox}?thread={self.thread.pk}')
                entry['thread_url'] = extra_urls[-1]
            for url in urls + extra_urls:
                response = self.content(url)
                self.assertEqual(response.status_code, 200, (role, url))
                payload = response.json()
                self.assertIn('html', payload, (role, url, payload))
                snapshot = self.client.get(url, HTTP_X_MERIDIAN_REALTIME='snapshot')
                self.assertEqual(snapshot.status_code, 200, (role, url))
                entry['pages'][url] = {'content': payload, 'snapshot': snapshot.content.decode()}
            status_url = reverse('portal:patient-message-status' if role == 'patient' else 'portal:staff-message-status')
            entry['endpoints'][status_url] = self.client.get(status_url).json()
            if role != 'super_admin':
                seen_url = reverse('portal:patient-message-seen' if role == 'patient' else 'portal:staff-message-seen', args=[self.thread.pk])
                seen = self.client.post(seen_url, {'message_ids': [self.message.pk, self.incoming.pk]}, content_type='application/json')
                self.assertEqual(seen.status_code, 200)
                entry['endpoints'][seen_url] = seen.json()
            if role == 'patient':
                video_url = reverse('portal:patient-appointment-video-status')
                video = self.client.get(video_url, {'id': str(appointment.pk)})
                self.assertEqual(video.status_code, 200)
                entry['endpoints'][video_url] = video.json()
                times_url = reverse('portal:patient-booking-times')
                booking_day = (timezone.localdate() + timedelta(days=3)).isoformat()
                slots = self.client.get(times_url, {'clinician': self.doctor.pk, 'date': booking_day, 'duration': '15'})
                self.assertEqual(slots.status_code, 200)
                entry['endpoints'][times_url] = slots.json()
                entry['booking'] = {'clinician': str(self.doctor.pk), 'day': booking_day, 'times_url': times_url}
            if role == 'super_admin':
                scope = entry['pages'][urls[0]]['content']['scope']
                section = self.client.get(urls[0], HTTP_X_MERIDIAN_NAVIGATION='section',
                                          HTTP_X_MERIDIAN_SECTION='dashboard-metrics', HTTP_X_MERIDIAN_NAVIGATION_SCOPE=scope)
                self.assertEqual(section.status_code, 200)
                entry['sections']['dashboard-metrics'] = section.json()
            datasets.append(entry)
        result = subprocess.run(
            [os.environ.get('MERIDIAN_NODE') or shutil.which('node'), str(settings.BASE_DIR / 'scripts/test_navigation_rendered.cjs')],
            input=json.dumps(datasets), text=True, capture_output=True, cwd=settings.BASE_DIR, timeout=180,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('"flows":6', result.stdout)
        print(result.stdout, end='')
