"""Presentation and optional real-browser checks for the bounded event trail."""

from django.test import override_settings
import json
import os
import shutil
import subprocess
from datetime import datetime, timezone as datetime_timezone
from unittest import skipUnless

from django.conf import settings
from django.contrib.auth import get_user_model
from django.template.loader import render_to_string
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from care.models import PatientEvent
from practices.models import Company, CompanyMembership, Patient
from practices.services import ACTIVE_COMPANY_SESSION_KEY


@override_settings(MULTI_PRACTICE_ENABLED=True)
class RecordTimelinePresentationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name='Timeline Example', slug='timeline-example')
        cls.doctor = get_user_model().objects.create_user(email='timeline-ui@example.test', first_name='Example', last_name='Doctor')
        CompanyMembership.objects.create(company=cls.company, user=cls.doctor, role='doctor')
        cls.patient = Patient.objects.create(company=cls.company, first_name='Example', last_name='Patient')
        PatientEvent.objects.bulk_create([
            PatientEvent(company=cls.company, patient=cls.patient, category='clinical',
                         title=f'Shared event {number}', detail='An example event with recorded information.')
            for number in range(45)
        ])

    def page(self):
        self.client.force_login(self.doctor)
        session = self.client.session
        session[ACTIVE_COMPANY_SESSION_KEY] = self.company.pk
        session.save()
        return self.client.get(reverse('portal:staff-patient-record', args=[self.patient.pk]))

    def test_scroll_region_initial_batch_and_no_script_fallback(self):
        response = self.page()
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'data-record-timeline')
        self.assertContains(response, 'id="record-timeline-scroll" tabindex="0" role="region"')
        self.assertContains(response, 'aria-describedby="record-scroll-help"')
        self.assertContains(response, 'data-entry-key=', count=20)
        self.assertContains(response, '<noscript><nav')
        self.assertContains(response, 'aria-label="Clinical timeline pages"')
        self.assertContains(response, 'data-timeline-load hidden')
        self.assertContains(response, '/static/js/record_timeline.js')
        self.assertContains(response, 'aria-label="Patient sections"', count=1)
        self.assertContains(response, 'data-patient-section="history"')
        self.assertContains(response, '<input type="hidden" name="tab" value="history">')

    def test_related_sections_remain_inside_the_patient_workspace(self):
        response = self.page()
        html = response.content.decode().split('aria-label="Patient care actions">', 1)[1].split('</nav>', 1)[0]
        detail = reverse('portal:patient-detail', args=[self.patient.pk])
        for tab in ('consultations', 'blood-tests', 'messages', 'appointments'):
            self.assertIn(f'href="{detail}?tab={tab}"', html)
        self.assertNotIn('href="/schedule/', html)
        self.assertNotIn('href="/messages/', html)
        self.assertContains(response, 'More for this patient')

    def test_shared_entry_partial_keeps_stable_keys_and_escapes_clinical_text(self):
        html = render_to_string('portal/includes/record_timeline_entries.html', {'timeline_entries': [{
            'kind': 'event', 'id': 7, 'at': timezone.now(), 'category': 'clinical',
            'company_name': '<script>private()</script>', 'category_label': 'Clinical',
            'title': '<img src=x onerror=unsafe()>', 'detail': 'Recorded <history>',
            'answers': [{'label': 'Medication', 'answer': '<script>unsafe()</script>'}],
        }]})
        self.assertIn('data-entry-key="event:7"', html)
        self.assertIn('&lt;img src=x onerror=unsafe()&gt;', html)
        self.assertNotIn('<script>', html)
        self.assertNotIn('<img', html)

    def test_timestamps_remain_sast_if_request_timezone_changes(self):
        with timezone.override('Pacific/Honolulu'):
            html = render_to_string('portal/includes/record_timeline_entries.html', {'timeline_entries': [{
                'kind': 'event', 'id': 8, 'at': datetime(2026, 9, 12, 8, 0, tzinfo=datetime_timezone.utc),
                'category': 'clinical', 'title': 'Recorded entry',
            }]})
        self.assertIn('12 Sep 2026 · 10:00', html)
        self.assertIn('2026-09-12T10:00:00+02:00', html)

    @skipUnless(os.environ.get('MERIDIAN_TIMELINE_BROWSER'), 'Optional Playwright interaction checks')
    def test_browser_layout_loading_retry_and_access_expiry(self):
        response = self.page()
        entries = [{
            'kind': 'event', 'id': 10000 + index, 'at': timezone.now(), 'category': 'clinical',
            'company_name': self.company.name, 'category_label': 'Clinical',
            'title': f'Older example event {index}', 'detail': 'Recorded example history. ' * 8,
        } for index in range(20)]
        payload = {
            'html': response.content.decode(),
            'older': render_to_string('portal/includes/record_timeline_entries.html', {'timeline_entries': entries}),
        }
        result = subprocess.run(
            [os.environ.get('MERIDIAN_NODE') or shutil.which('node'), str(settings.BASE_DIR / 'scripts/test_record_timeline.cjs')],
            input=json.dumps(payload), text=True, capture_output=True, cwd=settings.BASE_DIR, timeout=120,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('"passed":', result.stdout)
