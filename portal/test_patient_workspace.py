"""Patient tabs and legacy actions keep one patient context without stacked pages."""

from datetime import timedelta
from urllib.parse import parse_qs, urlsplit

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from care.models import Appointment, AuditEvent, ClinicalNote, ClinicalTask, MessageThread, PatientMessage, Payment
from . import test_workflows
from .patient_workspace import CLINICAL_TABS, TABS


class PatientWorkspaceTests(TestCase):
    setUpTestData = classmethod(test_workflows.PortalWorkflowTests.setUpTestData.__func__)
    login = test_workflows.PortalWorkflowTests.login
    add_message = test_workflows.PortalWorkflowTests.add_message

    def path(self, patient=None):
        return reverse('portal:patient-detail', args=[(patient or self.patient).pk])

    def page(self, tab='overview', **query):
        return self.client.get(self.path(), {'tab': tab, **query})

    def test_each_authorized_tab_has_one_patient_heading_and_patient_bound_navigation(self):
        for actor in (self.doctor, self.administrator, self.super_admin):
            self.login(actor)
            for tab, _ in TABS:
                if actor == self.administrator and tab in CLINICAL_TABS:
                    continue
                with self.subTest(actor=actor.pk, tab=tab):
                    response = self.page(tab)
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.context['patient'].pk, self.patient.pk)
                    self.assertEqual(response.context['workspace_tab'], tab)
                    self.assertContains(response, f'<h1>{self.patient}</h1>', count=1)
                    self.assertContains(response, f'data-patient-section="{tab}"', count=1)
                    for link in response.context['workspace_tabs']:
                        split = urlsplit(link['url'])
                        self.assertEqual(split.path, self.path())
                        self.assertEqual(parse_qs(split.query)['tab'], [link['name']])
                    self.assertEqual([link['name'] for link in response.context['workspace_tabs'] if link['active']], [tab])

    def test_overview_is_summary_only_and_does_not_read_any_conversation(self):
        self.login(self.doctor)
        incoming = self.add_message(body='UNREAD_PRIVATE_CONVERSATION_BODY')
        ClinicalNote.objects.create(company=self.company, patient=self.patient, author=self.doctor, body='CLINICAL_NOTE_BODY')
        response = self.page()
        for private in ('UNREAD_PRIVATE_CONVERSATION_BODY', 'CLINICAL_NOTE_BODY', 'name="body"', 'name="summary"', 'name="starts_at"'):
            self.assertNotContains(response, private)
        self.assertContains(response, 'aria-label="Patient overview"')
        incoming.refresh_from_db()
        self.assertIsNone(incoming.read_at)

    def test_local_payments_tab_is_readonly_and_does_not_expose_provider_metadata(self):
        self.login(self.administrator)
        Payment.objects.create(company=self.company, patient=self.patient, amount='630.00', due_on=timezone.localdate(),
                               status='paid', provider_reference='PRIVATE_PROVIDER_REFERENCE', failure_reason='PRIVATE_PROVIDER_DIAGNOSTIC')
        Payment.objects.create(company=self.other_company, patient=self.other_practice_patient, amount='8888.88', due_on=timezone.localdate())
        before_users = get_user_model().objects.count()
        response = self.page('payments')
        self.assertContains(response, 'R630.00')
        self.assertContains(response, 'Live payment processing is not enabled')
        for hidden in ('8888.88', 'PRIVATE_PROVIDER_REFERENCE', 'PRIVATE_PROVIDER_DIAGNOSTIC'):
            self.assertNotContains(response, hidden)
        self.assertEqual(self.client.post(self.path() + '?tab=payments', {'paid': 'true'}).status_code, 405)
        self.assertEqual(Payment.objects.count(), 2)
        self.assertEqual(get_user_model().objects.count(), before_users)

    def test_administrative_history_exposes_only_fixed_operational_labels(self):
        self.login(self.administrator)
        AuditEvent.objects.create(company=self.company, patient=self.patient, action='shipment.dispatched',
                                  metadata={'private': 'PRIVATE_AUDIT_META'}, ip_address='192.0.2.25')
        AuditEvent.objects.create(company=self.company, patient=self.patient, action='consultation.signed')
        response = self.page('history')
        self.assertContains(response, 'Delivery dispatched')
        self.assertContains(response, 'Administrative activity only')
        self.assertNotContains(response, 'PRIVATE_AUDIT_META')
        self.assertNotContains(response, '192.0.2.25')
        self.assertEqual(response.context['workspace_row_count'], 1)

    def test_appointments_are_paginated_without_changing_patient_or_tab(self):
        self.login(self.doctor)
        Appointment.objects.bulk_create([Appointment(company=self.company, patient=self.patient, clinician=self.doctor,
            starts_at=timezone.now() + timedelta(days=index + 1), duration_minutes=30) for index in range(22)])
        response = self.page('appointments', status='booked')
        self.assertEqual(len(response.context['rows']), 20)
        next_url = response.context['workspace_next_url']
        self.assertEqual(urlsplit(next_url).path, self.path())
        self.assertEqual(parse_qs(urlsplit(next_url).query), {'tab': ['appointments'], 'status': ['booked'], 'page': ['2']})
        self.assertEqual(len(self.client.get(next_url).context['rows']), 2)
        invalid = self.page('appointments', status='invented')
        self.assertTrue(invalid.context['workspace_filter'].errors)
        self.assertEqual(invalid.context['rows'], [])

    def test_message_and_new_conversation_success_return_to_the_exact_patient_thread(self):
        self.login(self.administrator)
        response = self.client.post(reverse('portal:staff-message-create', args=[self.thread.pk]), {'body': 'Administrative reply'})
        self.assertRedirects(response, self.path() + f'?tab=messages&thread={self.thread.pk}', fetch_redirect_response=False)
        response = self.client.post(reverse('portal:staff-thread-create', args=[self.patient.pk]), {'subject': 'A new thread', 'body': 'New administrative message'})
        new_thread = MessageThread.objects.get(subject='A new thread')
        self.assertRedirects(response, self.path() + f'?tab=messages&thread={new_thread.pk}', fetch_redirect_response=False)
        self.assertEqual(new_thread.patient_id, self.patient.pk)

    def test_invalid_legacy_forms_render_only_their_own_tab_and_preserve_inputs(self):
        self.login(self.doctor)
        incoming = self.add_message(body='Keep unread while validating another section')
        for route, tab, data, expected in (
            ('patient-task-create', 'tasks', {'patient': self.patient.pk, 'title': '', 'description': 'Retained task draft', 'priority': 'high'}, 'Retained task draft'),
            ('patient-note-create', 'notes', {'body': '', 'note_type': 'clinical'}, 'This field is required.'),
            ('patient-appointment-create', 'appointments', {'patient': self.patient.pk, 'clinician': self.doctor.pk, 'starts_at': 'invalid'}, 'This patient form is out of date'),
            ('staff-thread-create', 'messages', {'subject': '', 'body': 'Retained new-thread draft'}, 'Retained new-thread draft'),
        ):
            with self.subTest(route=route):
                response = self.client.post(reverse(f'portal:{route}', args=[self.patient.pk]), data)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.context['workspace_tab'], tab)
                self.assertContains(response, 'patient-workspace-tabs')
                if route != 'patient-appointment-create':
                    self.assertContains(response, expected)
                self.assertNotContains(response, 'record-action-grid')
        incoming.refresh_from_db()
        self.assertIsNone(incoming.read_at)

    def test_legacy_thread_and_weight_pagination_bookmarks_choose_the_correct_tab(self):
        self.login(self.doctor)
        response = self.client.get(self.path(), {'thread': self.thread.pk})
        self.assertEqual(response.context['workspace_tab'], 'messages')
        response = self.client.get(self.path(), {'weight_page': 2})
        self.assertEqual(response.context['workspace_tab'], 'weights')
