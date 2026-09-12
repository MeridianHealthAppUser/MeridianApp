"""Regression coverage for legacy write paths and deployment-only hazards."""

import io
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import override_settings
from django.urls import reverse

from care.models import AuditEvent, ClinicalNote, ClinicalTask, MessageThread, PatientMessage, RecordTag
from care.task_services import save_task
from care.test_operations import OperationsFixture
from practices.models import Patient
from practices.services import ACTIVE_COMPANY_SESSION_KEY
from .task_forms import TaskEditorForm


class FinalWriteAuditTests(OperationsFixture):
    def setUp(self):
        self.client.force_login(self.doctor)
        self.select(self.company)

    def select(self, company):
        session = self.client.session
        session[ACTIVE_COMPANY_SESSION_KEY] = company.pk
        session.save()

    def task_url(self, task=None):
        return reverse('portal:task-edit', args=[task.pk]) if task else reverse('portal:task-create')

    def task_data(self, **extra):
        return dict(title='General team preparation', description='Private task draft', patient='', assigned_to='',
                    status='open', priority='normal', due_at='', **extra)

    def token(self, task=None):
        response = self.client.get(self.task_url(task))
        self.assertEqual(response.status_code, 200)
        self.assertIn('no-store', response['Cache-Control'])
        return response.context['workflow_context']

    def test_general_task_cannot_follow_an_old_tab_into_another_practice(self):
        token = self.token()
        self.select(self.beta)
        response = self.client.post(self.task_url(), self.task_data(workflow_context=token))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context['form'].non_field_errors())
        self.assertEqual(response.context['workflow_context'], token)
        self.assertFalse(ClinicalTask.objects.exists())
        self.assertFalse(AuditEvent.objects.filter(action='task.created').exists())

    def test_missing_invalid_and_expired_contexts_fail_closed(self):
        with patch('django.core.signing.time.time', return_value=1):
            expired = self.token()
        for token in ('', 'invalid', expired):
            with self.subTest(token=token):
                response = self.client.post(self.task_url(), self.task_data(workflow_context=token))
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.context['form'].non_field_errors())
                self.assertEqual(response.context['workflow_context'], token)
        self.assertFalse(ClinicalTask.objects.exists())

    def test_task_context_is_bound_to_actor_and_record(self):
        token = self.token()
        self.client.force_login(self.admin)
        self.select(self.company)
        response = self.client.post(self.task_url(), self.task_data(workflow_context=token))
        self.assertTrue(response.context['form'].non_field_errors())
        first = ClinicalTask.objects.create(company=self.company, title='First task', created_by=self.admin)
        second = ClinicalTask.objects.create(company=self.company, title='Second task', created_by=self.admin)
        token = self.token(first)
        response = self.client.post(self.task_url(second), self.task_data(workflow_context=token))
        self.assertTrue(response.context['form'].non_field_errors())
        second.refresh_from_db()
        self.assertEqual(second.title, 'Second task')

    def test_another_edit_invalidates_the_original_revision_without_losing_draft_or_tags(self):
        task = ClinicalTask.objects.create(company=self.company, title='Original', created_by=self.doctor)
        token = self.token(task)
        task.title = 'Updated by another tab'
        task.save()
        response = self.client.post(self.task_url(task), self.task_data(workflow_context=token, new_tag='Do not create'))
        self.assertTrue(response.context['form'].non_field_errors())
        self.assertContains(response, 'Private task draft')
        self.assertEqual(response.context['workflow_context'], token)
        task.refresh_from_db()
        self.assertEqual(task.title, 'Updated by another tab')
        self.assertFalse(RecordTag.objects.exists())

    def test_valid_context_still_creates_and_updates_general_tasks(self):
        response = self.client.post(self.task_url(), self.task_data(workflow_context=self.token(), new_tag='Urgent'))
        self.assertEqual(response.status_code, 302)
        task = ClinicalTask.objects.get()
        self.assertEqual(task.company, self.company)
        self.assertEqual(list(task.tags.values_list('name', flat=True)), ['Urgent'])
        data = self.task_data(workflow_context=self.token(task))
        data['status'] = 'done'
        response = self.client.post(self.task_url(task), data)
        self.assertEqual(response.status_code, 302)
        task.refresh_from_db()
        self.assertEqual(task.status, 'done')
        self.assertIsNotNone(task.completed_at)

    def test_legacy_patient_task_uses_shared_tags_audit_and_server_owned_open_state(self):
        tag = RecordTag.objects.create(company=self.company, name='Important')
        response = self.client.post(reverse('portal:patient-task-create', args=[self.patient.pk]), {
            'patient': self.patient.pk, 'title': 'Patient coordination', 'description': 'Task description',
            'priority': 'normal', 'tags': [tag.pk], 'new_tag': 'Needs a follow-up', 'status': 'done',
            'company': self.beta.pk, 'created_by': self.beta_admin.pk,
        })
        self.assertEqual(response.status_code, 302)
        task = ClinicalTask.objects.get()
        self.assertEqual((task.company_id, task.patient_id, task.created_by_id),
                         (self.company.pk, self.patient.pk, self.doctor.pk))
        self.assertEqual(task.status, 'open')
        self.assertIsNone(task.completed_at)
        self.assertEqual(set(task.tags.values_list('name', flat=True)), {'Important', 'Needs a follow-up'})
        audit = AuditEvent.objects.get(action='task.created')
        self.assertEqual(set(audit.metadata['tag_ids']), set(task.tags.values_list('pk', flat=True)))
        self.assertNotIn(task.description, str(audit.metadata))

    def test_legacy_task_foreign_tags_and_oversized_descriptions_do_not_write(self):
        tag = RecordTag.objects.create(company=self.beta, name='Foreign label')
        for extra in ({'tags': [tag.pk]}, {'description': 'x' * 10001}):
            data = {'patient': self.patient.pk, 'title': 'Rejected task', 'priority': 'normal', **extra}
            response = self.client.post(reverse('portal:patient-task-create', args=[self.patient.pk]), data)
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.context['task_form'].errors)
        self.assertFalse(ClinicalTask.objects.exists())

    def test_service_rechecks_inactive_user_even_with_a_previously_valid_actor_and_form(self):
        form = TaskEditorForm(self.task_data(), company=self.company)
        self.assertTrue(form.is_valid(), form.errors)
        get_user_model().objects.filter(pk=self.doctor.pk).update(is_active=False)
        with self.assertRaises(PermissionDenied):
            save_task(company=self.company, actor=self.doctor, form=form)
        self.assertFalse(ClinicalTask.objects.exists())

    def test_cached_valid_form_cannot_save_an_inactive_patient_or_a_moved_tag(self):
        data = self.task_data()
        data['patient'] = self.patient.pk
        form = TaskEditorForm(data, company=self.company)
        self.assertTrue(form.is_valid(), form.errors)
        Patient.objects.filter(pk=self.patient.pk).update(is_active=False)
        with self.assertRaises(ValidationError):
            save_task(company=self.company, actor=self.doctor, form=form)
        tag = RecordTag.objects.create(company=self.company, name='Moved label')
        data = self.task_data(tags=[tag.pk])
        form = TaskEditorForm(data, company=self.company)
        self.assertTrue(form.is_valid(), form.errors)
        RecordTag.objects.filter(pk=tag.pk).update(company=self.beta)
        with self.assertRaises(ValidationError):
            save_task(company=self.company, actor=self.doctor, form=form)
        self.assertFalse(ClinicalTask.objects.exists())
        self.assertFalse(AuditEvent.objects.filter(action='task.created').exists())

    def test_note_append_cannot_overwrite_prior_text_signature_or_patient_with_posted_ids(self):
        note = ClinicalNote.objects.create(company=self.company, patient=self.patient, author=self.doctor,
                                           body='Original clinical note')
        original = (note.body, note.author_id, note.created_at, note.patient_id)
        response = self.client.post(reverse('portal:patient-note-create', args=[self.patient.pk]), {
            'pk': note.pk, 'id': note.pk, 'body': 'Separate appended note', 'note_type': 'consult',
            'author': self.other_user.pk, 'patient': self.beta_patient.pk, 'company': self.beta.pk,
        })
        self.assertEqual(response.status_code, 302)
        note.refresh_from_db()
        self.assertEqual((note.body, note.author_id, note.created_at, note.patient_id), original)
        appended = ClinicalNote.objects.exclude(pk=note.pk).get()
        self.assertEqual((appended.author_id, appended.patient_id, appended.company_id),
                         (self.doctor.pk, self.patient.pk, self.company.pk))

    def test_head_legacy_record_and_staff_inbox_does_not_mark_read_or_audit(self):
        thread = MessageThread.objects.create(company=self.company, patient=self.patient, subject='Unread conversation')
        message = PatientMessage.objects.create(company=self.company, thread=thread, sender=self.patient_user, body='Not opened yet')
        before = AuditEvent.objects.count()
        for url in (reverse('portal:patient-detail', args=[self.patient.pk]), reverse('portal:staff-inbox')):
            response = self.client.head(url, {'thread': thread.pk})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.content, b'')
            self.assertIn('no-store', response['Cache-Control'])
        message.refresh_from_db()
        self.assertIsNone(message.read_at)
        self.assertEqual(AuditEvent.objects.count(), before)

    def test_invalid_staff_forms_do_not_mark_other_patient_messages_read(self):
        thread = MessageThread.objects.create(company=self.company, patient=self.patient, subject='Unread conversation')
        message = PatientMessage.objects.create(company=self.company, thread=thread, sender=self.patient_user, body='Not opened yet')
        before = AuditEvent.objects.count()
        response = self.client.post(reverse('portal:patient-task-create', args=[self.patient.pk]), {
            'patient': self.patient.pk, 'title': '', 'priority': 'normal'})
        self.assertEqual(response.status_code, 200)
        response = self.client.post(reverse('portal:staff-message-create', args=[thread.pk]), {'body': '', 'return_to': 'inbox'})
        self.assertEqual(response.status_code, 200)
        message.refresh_from_db()
        self.assertIsNone(message.read_at)
        self.assertEqual(AuditEvent.objects.count(), before)


class DemoDeploymentSafetyTests(OperationsFixture):
    @override_settings(DEBUG=False)
    def test_production_demo_command_cannot_create_records_or_reset_credentials_even_with_flag(self):
        before = get_user_model().objects.count()
        original_password = self.doctor.password
        for options in ({}, {'reset_passwords': True}):
            with self.assertRaises(CommandError):
                call_command('seed_demo', stdout=io.StringIO(), **options)
        self.assertEqual(get_user_model().objects.count(), before)
        self.doctor.refresh_from_db()
        self.assertEqual(self.doctor.password, original_password)

    @override_settings(DEBUG=True)
    def test_development_seed_preserves_existing_password_unless_reset_is_explicit(self):
        user = get_user_model().objects.create_user('sam.marchant@meridianhealth.co.za', password='ChosenPersonalPassword!58')
        original_hash = user.password
        call_command('seed_demo', stdout=io.StringIO())
        user.refresh_from_db()
        self.assertEqual(user.password, original_hash)
        self.assertTrue(user.check_password('ChosenPersonalPassword!58'))
        call_command('seed_demo', reset_passwords=True, stdout=io.StringIO())
        user.refresh_from_db()
        self.assertTrue(user.check_password('MeridianDemo!2026'))
