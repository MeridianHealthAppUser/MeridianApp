"""Free-form record labels retain the task/note tenancy and permission boundary."""

from django.test import override_settings
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase

from portal.task_forms import TaskEditorForm
from practices.models import Company, CompanyMembership, Patient

from .models import (
    AuditEvent, ClinicalNote, ClinicalNoteTagAssignment, ClinicalTask, RecordTag, TaskTagAssignment,
)
from .services import complete_task
from .task_services import save_task, set_record_tags


@override_settings(MULTI_PRACTICE_ENABLED=True)
class RecordTagTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name='Labels Alpha', slug='labels-alpha')
        cls.other_company = Company.objects.create(name='Labels Beta', slug='labels-beta')
        users = get_user_model().objects
        cls.doctor = users.create_user(email='labels-doctor@example.test')
        cls.other_doctor = users.create_user(email='labels-second@example.test')
        cls.admin = users.create_user(email='labels-admin@example.test')
        cls.foreign_doctor = users.create_user(email='labels-foreign@example.test')
        cls.inactive_doctor = users.create_user(email='labels-inactive@example.test', is_active=False)
        cls.patient_user = users.create_user(email='labels-patient@example.test')
        for user, role in (
            (cls.doctor, CompanyMembership.Role.DOCTOR),
            (cls.other_doctor, CompanyMembership.Role.DOCTOR),
            (cls.inactive_doctor, CompanyMembership.Role.DOCTOR),
            (cls.admin, CompanyMembership.Role.PRACTICE_ADMIN),
        ):
            CompanyMembership.objects.create(company=cls.company, user=user, role=role)
        for user in (cls.doctor, cls.foreign_doctor):
            CompanyMembership.objects.create(company=cls.other_company, user=user, role=CompanyMembership.Role.DOCTOR)
        cls.patient = Patient.objects.create(company=cls.company, user=cls.patient_user, first_name='Label', last_name='Patient')
        cls.other_patient = Patient.objects.create(company=cls.other_company, first_name='Beta', last_name='Patient')
        cls.task = ClinicalTask.objects.create(
            company=cls.company, title='General task', assigned_to=cls.doctor, created_by=cls.doctor,
        )
        cls.other_task = ClinicalTask.objects.create(
            company=cls.other_company, title='Beta task', assigned_to=cls.doctor, created_by=cls.doctor,
        )
        cls.unrelated_task = ClinicalTask.objects.create(
            company=cls.company, title='Colleague task', assigned_to=cls.other_doctor, created_by=cls.admin,
        )
        cls.note = ClinicalNote.objects.create(
            company=cls.company, patient=cls.patient, author=cls.doctor, body='Clinical body remains unmodified.',
        )
        cls.other_note = ClinicalNote.objects.create(
            company=cls.other_company, patient=cls.other_patient, author=cls.doctor, body='Beta clinical note.',
        )
        cls.tag = RecordTag.objects.create(company=cls.company, name='Important')
        cls.other_tag = RecordTag.objects.create(company=cls.other_company, name='Other practice label')

    def apply_tags(self, *, record=None, actor=None, tags=(), new_tag=''):
        return set_record_tags(record=record or self.task, actor=actor or self.doctor, tags=tags, new_tag=new_tag)

    def test_general_task_does_not_require_a_patient(self):
        task = ClinicalTask(company=self.company, title='Prepare staff training', assigned_to=self.admin)
        task.full_clean()
        task.save()
        self.assertIsNone(task.patient_id)

    def test_patient_task_and_assignee_must_be_in_same_practice(self):
        for field, value in (('patient', self.other_patient), ('assigned_to', self.foreign_doctor),
                             ('assigned_to', self.inactive_doctor)):
            with self.subTest(field=field, value=value.pk):
                task = ClinicalTask(company=self.company, title='Boundary check', **{field: value})
                with self.assertRaises(ValidationError):
                    task.full_clean()

    def test_tags_accept_arbitrary_names_and_collapse_surrounding_whitespace(self):
        for raw, expected in (
            (' urgent ', 'urgent'), ('  Patient   difficult  ', 'Patient difficult'),
            ('Discuss / next visit!', 'Discuss / next visit!'),
        ):
            with self.subTest(raw=raw):
                tag = RecordTag(company=self.company, name=raw)
                tag.full_clean()
                self.assertEqual(tag.name, expected)

    def test_blank_and_oversized_tag_names_are_invalid(self):
        for name in ('   ', 'x' * 65):
            with self.subTest(name=name):
                with self.assertRaises(ValidationError):
                    RecordTag(company=self.company, name=name).full_clean()

    def test_case_insensitive_name_uniqueness_is_scoped_to_each_company(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            RecordTag.objects.create(company=self.company, name='IMPORTANT')
        tag = RecordTag.objects.create(company=self.other_company, name='Important')
        self.assertEqual(tag.company_id, self.other_company.pk)

    def test_through_models_reject_cross_practice_tags_and_records(self):
        for assignment in (
            TaskTagAssignment(company=self.company, task=self.task, tag=self.other_tag),
            TaskTagAssignment(company=self.company, task=self.other_task, tag=self.tag),
            ClinicalNoteTagAssignment(company=self.company, note=self.note, tag=self.other_tag),
            ClinicalNoteTagAssignment(company=self.company, note=self.other_note, tag=self.tag),
        ):
            with self.subTest(model=type(assignment).__name__, values=assignment.__dict__):
                with self.assertRaises(ValidationError):
                    assignment.full_clean()

    def test_new_labels_are_reusable_across_tasks_and_notes(self):
        self.apply_tags(new_tag='  Follow up later  ')
        tag = self.task.tags.get()
        self.assertEqual((tag.name, tag.company_id, tag.created_by_id),
                         ('Follow up later', self.company.pk, self.doctor.pk))
        self.apply_tags(record=self.note, new_tag='FOLLOW UP LATER')
        self.assertEqual(self.note.tags.get().pk, tag.pk)
        self.assertEqual(RecordTag.objects.for_company(self.company).filter(name__iexact='follow up later').count(), 1)
        self.assertEqual(TaskTagAssignment.objects.get(task=self.task).company_id, self.company.pk)
        self.assertEqual(ClinicalNoteTagAssignment.objects.get(note=self.note).company_id, self.company.pk)

    def test_existing_and_new_duplicate_label_create_only_one_assignment(self):
        self.apply_tags(tags=[self.tag], new_tag=' important ')
        self.assertEqual(list(self.task.tags.values_list('pk', flat=True)), [self.tag.pk])

    def test_removing_a_label_does_not_delete_reusable_tag_or_other_assignments(self):
        self.apply_tags(tags=[self.tag])
        self.apply_tags(record=self.note, tags=[self.tag])
        self.apply_tags(tags=[])
        self.assertFalse(self.task.tags.exists())
        self.assertTrue(RecordTag.objects.filter(pk=self.tag.pk).exists())
        self.assertTrue(self.note.tags.filter(pk=self.tag.pk).exists())

    def test_label_does_not_change_fixed_task_workflow_or_note_body(self):
        self.apply_tags(new_tag='Urgent')
        self.apply_tags(record=self.note, new_tag='Patient difficult')
        self.task.refresh_from_db()
        self.note.refresh_from_db()
        self.assertEqual((self.task.priority, self.task.status), (ClinicalTask.Priority.NORMAL, ClinicalTask.Status.OPEN))
        self.assertEqual(self.note.body, 'Clinical body remains unmodified.')

    def test_cross_practice_selection_is_rejected_without_losing_current_labels(self):
        self.apply_tags(tags=[self.tag])
        count = AuditEvent.objects.count()
        with self.assertRaises(ValidationError):
            self.apply_tags(tags=[self.other_tag], new_tag='Must not be created')
        self.assertEqual(list(self.task.tags.values_list('pk', flat=True)), [self.tag.pk])
        self.assertFalse(RecordTag.objects.filter(name='Must not be created').exists())
        self.assertEqual(AuditEvent.objects.count(), count)

    def test_doctors_cannot_label_unrelated_tasks_but_administrators_can(self):
        with self.assertRaises(PermissionDenied):
            self.apply_tags(record=self.unrelated_task, tags=[self.tag])
        self.apply_tags(record=self.unrelated_task, actor=self.admin, tags=[self.tag])
        self.assertTrue(self.unrelated_task.tags.filter(pk=self.tag.pk).exists())

    def test_only_the_author_doctor_may_change_note_tags(self):
        for actor in (self.other_doctor, self.admin, self.foreign_doctor, self.patient_user):
            with self.subTest(actor=actor.email):
                with self.assertRaises(PermissionDenied):
                    self.apply_tags(record=self.note, actor=actor, tags=[self.tag])
        self.assertFalse(self.note.tags.exists())

    def test_inactive_actor_and_revoked_membership_cannot_change_tags(self):
        with self.assertRaises(PermissionDenied):
            self.apply_tags(actor=self.inactive_doctor, tags=[self.tag])
        CompanyMembership.objects.filter(company=self.company, user=self.doctor).update(is_active=False)
        with self.assertRaises(PermissionDenied):
            self.apply_tags(tags=[self.tag])

    def test_tag_audit_records_only_identifiers_not_sensitive_text(self):
        self.apply_tags(record=self.note, new_tag='Sensitive staff-only wording')
        tag = self.note.tags.get()
        audit = AuditEvent.objects.get(action='record.tags_updated', target_id=str(self.note.pk))
        self.assertEqual((audit.company_id, audit.patient_id, audit.actor_id),
                         (self.company.pk, self.patient.pk, self.doctor.pk))
        self.assertEqual(audit.metadata, {'tag_ids': [tag.pk]})
        self.assertNotIn(tag.name, str(audit.metadata))
        self.assertNotIn(self.note.body, str(audit.metadata))

    def task_form(self, *, instance=None, **overrides):
        return TaskEditorForm({
            'title': 'Save a general task', 'description': 'Private task detail', 'patient': '',
            'assigned_to': self.doctor.pk, 'priority': 'normal', 'status': 'open',
            'new_tag': '', **overrides,
        }, company=self.company, instance=instance)

    def test_task_service_stamps_creator_and_audits_metadata_without_task_note(self):
        form = self.task_form(created_by=self.other_doctor.pk, new_tag='Custom staff label')
        self.assertTrue(form.is_valid(), form.errors)
        task = save_task(company=self.company, actor=self.doctor, form=form)
        self.assertEqual(task.created_by_id, self.doctor.pk)
        self.assertIsNone(task.patient_id)
        event = AuditEvent.objects.get(action='task.created', target_id=str(task.pk))
        self.assertEqual(event.metadata, {
            'assigned_to_id': self.doctor.pk, 'tag_ids': [task.tags.get().pk], 'status': 'open',
        })
        self.assertNotIn(task.description, str(event.metadata))

    def test_task_form_validates_tag_tenant_and_preserves_note(self):
        form = self.task_form(tags=[self.other_tag.pk], new_tag='Unsaved new label')
        self.assertFalse(form.is_valid())
        self.assertIn('tags', form.errors)
        self.assertEqual(form['description'].value(), 'Private task detail')
        with self.assertRaises(ValidationError):
            save_task(company=self.company, actor=self.doctor, form=form)
        self.assertFalse(RecordTag.objects.filter(name='Unsaved new label').exists())

    def test_task_service_authorizes_against_stored_task_not_tampered_form_instance(self):
        form = self.task_form(instance=self.unrelated_task)
        self.assertTrue(form.is_valid(), form.errors)
        form.instance.created_by = self.doctor
        with self.assertRaises(PermissionDenied):
            save_task(company=self.company, actor=self.doctor, form=form)
        self.unrelated_task.refresh_from_db()
        self.assertEqual(self.unrelated_task.created_by_id, self.admin.pk)

    def test_task_service_rejects_mismatched_form_company(self):
        form = self.task_form()
        self.assertTrue(form.is_valid(), form.errors)
        with self.assertRaises(ValidationError):
            save_task(company=self.other_company, actor=self.doctor, form=form)

    def test_edit_can_complete_and_reopen_without_losing_original_creator(self):
        task = save_task(company=self.company, actor=self.doctor, form=self.task_form(status='done'))
        original_completed_at = task.completed_at
        self.assertIsNotNone(original_completed_at)
        task = save_task(company=self.company, actor=self.doctor,
                         form=self.task_form(instance=task, status='done', new_tag='Complete but important'))
        self.assertEqual(task.completed_at, original_completed_at)
        task = save_task(company=self.company, actor=self.doctor,
                         form=self.task_form(instance=task, status='in_progress'))
        self.assertIsNone(task.completed_at)
        self.assertEqual(task.created_by_id, self.doctor.pk)

    def test_completion_service_rejects_doctor_who_did_not_create_or_receive_task(self):
        for task in (
            self.unrelated_task,
            ClinicalTask.objects.create(company=self.company, title='Unassigned team task', created_by=self.admin),
        ):
            with self.subTest(task=task.title):
                with self.assertRaises(PermissionDenied):
                    complete_task(task=task, actor=self.doctor)
                task.refresh_from_db()
                self.assertEqual(task.status, ClinicalTask.Status.OPEN)
        self.assertFalse(AuditEvent.objects.filter(action='task.completed').exists())

    def test_completion_service_allows_creator_to_complete_delegated_general_task(self):
        self.task.assigned_to = self.other_doctor
        self.task.save(update_fields=['assigned_to'])
        completed = complete_task(task=self.task, actor=self.doctor)
        self.assertEqual(completed.status, ClinicalTask.Status.DONE)
        self.assertIsNotNone(completed.completed_at)
        self.assertIsNone(completed.patient_id)
        audit = AuditEvent.objects.get(action='task.completed', target_id=str(self.task.pk))
        self.assertEqual((audit.actor_id, audit.company_id, audit.patient_id),
                         (self.doctor.pk, self.company.pk, None))

    def test_completion_service_rejects_inactive_patient_even_for_task_creator(self):
        self.patient.is_active = False
        self.patient.save(update_fields=['is_active'])
        self.task.patient = self.patient
        self.task.save(update_fields=['patient'])
        with self.assertRaises(PermissionDenied):
            complete_task(task=self.task, actor=self.doctor)
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, ClinicalTask.Status.OPEN)
        self.assertFalse(AuditEvent.objects.filter(action='task.completed').exists())

    def test_completion_service_rejects_inactive_company_even_for_task_creator(self):
        self.company.is_active = False
        self.company.save(update_fields=['is_active'])
        with self.assertRaises(PermissionDenied):
            complete_task(task=self.task, actor=self.doctor)
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, ClinicalTask.Status.OPEN)
        self.assertFalse(AuditEvent.objects.filter(action='task.completed').exists())
