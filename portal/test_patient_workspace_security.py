"""Adversarial patient-workspace reads keep existing role and tenant boundaries."""

from django.test import override_settings
from datetime import timedelta

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from care.messaging import add_participant
from care.models import (Appointment, AppointmentProposal, AuditEvent, ClinicalEncounter,
    ClinicalNote, ClinicalNoteTagAssignment, ClinicalTask, MessageThread, PatientMessage,
    RecordTag, TaskTagAssignment)
from practices.models import CompanyMembership, Patient
from . import test_records


@override_settings(MULTI_PRACTICE_ENABLED=True)
class PatientWorkspaceSecurityTests(TestCase):
    setUpTestData = classmethod(test_records.ClinicalRecordTests.setUpTestData.__func__)
    event = classmethod(test_records.ClinicalRecordTests.event.__func__)
    login = test_records.ClinicalRecordTests.login

    def setUp(self):
        self.login()
        self.path = reverse('portal:patient-detail', args=[self.patient.pk])
        self.thread = MessageThread.objects.create(company=self.alpha, patient=self.patient, subject='Owned conversation')
        self.message = PatientMessage.objects.create(company=self.alpha, thread=self.thread, sender=self.user, body='Owned unread patient message')
        self.foreign_thread = MessageThread.objects.create(company=self.beta, patient=self.beta_patient, subject='FOREIGN_THREAD_SECRET')
        add_participant(self.thread, self.doctor)
        add_participant(self.foreign_thread, self.doctor)
        self.foreign_message = PatientMessage.objects.create(company=self.beta, thread=self.foreign_thread, sender=self.user, body='FOREIGN_MESSAGE_SECRET')

    def get(self, tab='overview', **query):
        return self.client.get(self.path, {'tab': tab, **query})

    def test_unknown_tabs_are_404_without_patient_content_or_new_read_audit(self):
        before = AuditEvent.objects.count()
        for tab in ('../history', '<script>', 'all', 'private', '', 'Notes'):
            response = self.get(tab)
            self.assertEqual(response.status_code, 404)
            self.assertNotContains(response, 'Shared clinician note', status_code=404)
        self.assertEqual(AuditEvent.objects.count(), before)

    def test_query_patient_and_company_ids_cannot_replace_the_url_record(self):
        for tab in ('overview', 'appointments', 'consultations', 'blood-tests', 'notes', 'messages', 'tasks', 'payments', 'treatment', 'deliveries'):
            with self.subTest(tab=tab):
                response = self.get(tab, patient=self.beta_patient.pk, patient_id=self.beta_patient.pk,
                                    company=self.beta.pk, company_id=self.beta.pk, scope='all')
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.context['patient'].pk, self.patient.pk)
                self.assertEqual(response.context['company'].pk, self.alpha.pk)
                self.assertNotContains(response, 'FOREIGN_THREAD_SECRET')
                self.assertNotContains(response, 'FOREIGN_MESSAGE_SECRET')
        wrong = reverse('portal:patient-detail', args=[self.beta_patient.pk])
        self.assertEqual(self.client.get(wrong, {'tab': 'messages'}).status_code, 404)

    def test_doctor_own_private_notes_are_visible_but_other_private_notes_are_not(self):
        response = self.get('notes')
        self.assertContains(response, 'OWN_PRIVATE_NOTE_SECRET')
        self.assertNotContains(response, 'OTHER_PRIVATE_NOTE_SECRET')
        self.assertContains(response, 'Shared clinician note')

    def test_super_admin_cannot_view_own_private_note_from_a_previous_doctor_role(self):
        CompanyMembership.objects.filter(company=self.alpha, user=self.doctor).update(role='super_admin', clinician_type='')
        response = self.get('notes')
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'OWN_PRIVATE_NOTE_SECRET')
        self.assertNotContains(response, 'OTHER_PRIVATE_NOTE_SECRET')
        self.assertContains(response, 'Shared clinician note')
        self.assertIsNone(response.context['note_form'])

    def test_practice_admin_cannot_access_clinical_tabs_by_direct_query(self):
        self.login(self.admin)
        for tab in ('notes', 'consultations', 'blood-tests', 'treatment'):
            self.assertEqual(self.get(tab).status_code, 403)
        response = self.get('history')
        self.assertEqual(response.status_code, 200)
        for secret in ('Shared clinician note', 'OWN_PRIVATE_NOTE_SECRET', 'ALPHA_DOCTOR_ONLY_PROFILE', 'Signed consultation summary', 'Clinician reviewed the report.'):
            self.assertNotContains(response, secret)

    def test_consultations_only_include_current_doctor_drafts_or_signed_entries(self):
        own = ClinicalEncounter.objects.get(company=self.alpha, clinical_summary='OWN_DRAFT_SECRET')
        other = ClinicalEncounter.objects.get(company=self.alpha, clinical_summary='OTHER_DRAFT_SECRET')
        response = self.get('consultations')
        self.assertIn(own.pk, {row.pk for row in response.context['rows']})
        self.assertNotIn(other.pk, {row.pk for row in response.context['rows']})
        self.assertIn(self.signed.pk, {row.pk for row in response.context['rows']})
        self.login(self.super_admin)
        response = self.get('consultations')
        self.assertEqual({row.pk for row in response.context['rows']}, {self.signed.pk})

    def test_general_notes_do_not_expose_a_snapshot_linked_to_an_unsigned_consultation(self):
        note = ClinicalNote.objects.create(company=self.alpha, patient=self.patient, author=self.colleague,
                                           body='MALFORMED_UNSIGNED_SNAPSHOT_SECRET')
        draft = ClinicalEncounter.objects.get(company=self.alpha, clinical_summary='OTHER_DRAFT_SECRET')
        ClinicalEncounter.objects.filter(pk=draft.pk).update(signed_note=note)
        for actor in (self.doctor, self.super_admin):
            self.login(actor)
            response = self.get('notes')
            self.assertNotContains(response, note.body)
            self.assertNotContains(response, 'Signed duplicate must not appear twice')

    def test_malformed_message_company_or_thread_links_cannot_enter_conversation(self):
        PatientMessage.objects.create(company=self.beta, thread=self.thread, sender=self.user, body='MALFORMED_MESSAGE_COMPANY_SECRET')
        malformed = MessageThread.objects.create(company=self.alpha, patient=self.beta_patient, subject='MALFORMED_THREAD_PATIENT_SECRET')
        PatientMessage.objects.create(company=self.alpha, thread=malformed, sender=self.user, body='MALFORMED_THREAD_BODY_SECRET')
        response = self.get('messages', thread=self.thread.pk)
        self.assertContains(response, self.message.body)
        for secret in ('FOREIGN_MESSAGE_SECRET', 'MALFORMED_MESSAGE_COMPANY_SECRET', 'MALFORMED_THREAD_PATIENT_SECRET', 'MALFORMED_THREAD_BODY_SECRET'):
            self.assertNotContains(response, secret)
        self.assertEqual(self.get('messages', thread=self.foreign_thread.pk).status_code, 404)
        self.assertEqual(self.get('messages', thread=malformed.pk).status_code, 404)

    def test_invalid_or_oversized_thread_ids_are_404_not_server_errors(self):
        for value in ('-1', '0', 'garbage', '９９', '9999999999999999999', str(2**63)):
            with self.subTest(value=value):
                self.assertEqual(self.get('messages', thread=value).status_code, 404)

    def test_overview_other_tabs_and_head_do_not_mark_patient_messages_read(self):
        for tab in ('overview', 'appointments', 'notes', 'tasks', 'weights'):
            self.assertEqual(self.get(tab).status_code, 200)
        self.assertEqual(self.client.head(self.path, {'tab': 'messages'}).status_code, 200)
        self.message.refresh_from_db()
        self.assertIsNone(self.message.read_at)
        before = AuditEvent.objects.count()
        self.assertEqual(self.client.head(self.path, {'tab': 'overview'}).status_code, 200)
        self.assertEqual(AuditEvent.objects.count(), before)

    def test_message_pages_mark_only_selected_visible_fifty_messages(self):
        for number in range(60):
            PatientMessage.objects.create(company=self.alpha, thread=self.thread, sender=self.user, body=f'Owned history message {number}')
        response = self.get('messages', thread=self.thread.pk)
        self.assertEqual(len(response.context['selected_thread'].conversation_messages), 50)
        self.assertEqual(PatientMessage.objects.filter(thread=self.thread, read_at__isnull=False).count(), 50)
        self.foreign_message.refresh_from_db()
        self.assertIsNone(self.foreign_message.read_at)
        older = self.client.get(response.context['older_message_url'])
        self.assertEqual(len(older.context['selected_thread'].conversation_messages), 11)
        self.assertEqual(PatientMessage.objects.filter(thread=self.thread, read_at__isnull=False).count(), 61)

    def test_invalid_reply_preserves_draft_without_read_receipts_or_extra_audit(self):
        before = AuditEvent.objects.count()
        response = self.client.post(reverse('portal:staff-message-create', args=[self.thread.pk]), {'body': 'x' * 5001})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['workspace_tab'], 'messages')
        self.assertEqual(response.context['message_form']['body'].value(), 'x' * 5001)
        self.message.refresh_from_db()
        self.assertIsNone(self.message.read_at)
        self.assertEqual(AuditEvent.objects.count(), before)

    def test_current_membership_and_patient_activation_are_rechecked_on_every_tab(self):
        CompanyMembership.objects.filter(company=self.alpha, user=self.doctor).update(is_active=False)
        for tab in ('overview', 'messages', 'notes'):
            self.assertIn(self.get(tab).status_code, (403, 404))
        CompanyMembership.objects.filter(company=self.alpha, user=self.doctor).update(is_active=True)
        self.login()
        Patient.objects.filter(pk=self.patient.pk).update(is_active=False)
        for tab in ('overview', 'messages', 'notes'):
            self.assertEqual(self.get(tab).status_code, 404)

    def test_mixed_patient_staff_identity_does_not_switch_record_or_portal_role(self):
        own_patient = Patient.objects.create(company=self.beta, user=self.doctor, first_name='Staff own', last_name='Care record')
        self.login()
        response = self.get('messages')
        self.assertEqual(response.context['patient'].pk, self.patient.pk)
        self.assertNotEqual(response.context['patient'].pk, own_patient.pk)
        self.assertFalse(response.context.get('is_patient_portal', False))
        self.assertEqual(response.context['active_membership'].role, 'doctor')

    def test_bad_status_filter_never_broadens_an_existing_tab(self):
        for tab in ('appointments', 'consultations', 'blood-tests', 'tasks', 'payments', 'treatment', 'deliveries'):
            response = self.get(tab, status='PRIVATE_INVALID_STATUS')
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.context['workspace_filter'].errors)
            self.assertEqual(response.context['rows'], [])

    def test_foreign_note_and_task_tags_are_not_exposed_through_corrupt_assignments(self):
        tag = RecordTag.objects.create(company=self.beta, name='FOREIGN_PRIVATE_TAG_SECRET')
        ClinicalNoteTagAssignment.objects.create(company=self.alpha, note=self.shared_note, tag=tag)
        task = ClinicalTask.objects.create(company=self.alpha, patient=self.patient, title='Local task', assigned_to=self.doctor, created_by=self.doctor)
        TaskTagAssignment.objects.create(company=self.alpha, task=task, tag=tag)
        self.assertNotContains(self.get('notes'), tag.name)
        self.assertNotContains(self.get('tasks'), tag.name)

    def test_invalid_tags_for_an_older_note_keep_the_target_and_bound_draft_visible(self):
        note = ClinicalNote.objects.create(company=self.alpha, patient=self.patient, author=self.doctor, body='Older edited note')
        for number in range(25):
            ClinicalNote.objects.create(company=self.alpha, patient=self.patient, author=self.doctor, body=f'Newer note {number}')
        self.assertNotIn(note.pk, {row.pk for row in self.get('notes').context['rows']})
        before = AuditEvent.objects.count()
        response = self.client.post(reverse('portal:patient-note-tags', args=[self.patient.pk, note.pk]), {'new_tag': 'x' * 65})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['workspace_tab'], 'notes')
        target = next((row for row in response.context['notes'] if row.pk == note.pk), None)
        self.assertIsNotNone(target, 'The invalid edited note must remain visible even when it is older than page one.')
        self.assertTrue(target.tags_form.errors)
        self.assertEqual(target.tags_form['new_tag'].value(), 'x' * 65)
        self.assertEqual(AuditEvent.objects.count(), before)

    def test_foreign_appointment_in_corrupt_proposal_is_not_shown_in_local_messages(self):
        appointment = Appointment.objects.create(company=self.beta, patient=self.beta_patient, clinician=self.doctor,
                                                starts_at=timezone.now() + timedelta(days=2))
        AppointmentProposal.objects.create(company=self.alpha, patient=self.patient, appointment=appointment,
            thread=self.thread, proposed_by=self.doctor, recipient=self.user, proposer_role='doctor', kind='reschedule',
            original_starts_at=appointment.starts_at, original_status='booked', original_clinician=self.doctor,
            original_duration_minutes=15, proposed_starts_at=appointment.starts_at + timedelta(hours=1), note='CORRUPT_FOREIGN_PROPOSAL_SECRET')
        self.assertNotContains(self.get('messages'), 'CORRUPT_FOREIGN_PROPOSAL_SECRET')
