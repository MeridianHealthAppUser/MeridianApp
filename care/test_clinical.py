"""Clinical signing and results review require the responsible clinician."""

from django.test import override_settings
import hashlib
import uuid
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase
from django.utils import timezone

from practices.models import Company, CompanyMembership, Patient

from .clinical import (
    create_lab_request, is_clinical_workflow_task, review_lab_request,
    save_consultation, submit_lab_result,
)
from .models import (
    Appointment, AuditEvent, ClinicalEncounter, ClinicalNote, ClinicalTask,
    LabRequest, LabResult, PatientEvent, Payment, TreatmentAuthorization,
)
from .services import complete_task


PDF = b'%PDF-1.4\n1 0 obj << /Type /Catalog >> endobj\n%%EOF'


@override_settings(MULTI_PRACTICE_ENABLED=True)
class ClinicalWorkflowTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name='Alpha Clinical Practice', slug='alpha-clinical')
        cls.beta = Company.objects.create(name='Beta Clinical Practice', slug='beta-clinical')
        users = get_user_model().objects
        cls.doctor = users.create_user(email='clinical-owner@example.test')
        cls.colleague = users.create_user(email='clinical-colleague@example.test')
        cls.admin = users.create_user(email='clinical-admin@example.test')
        cls.super_admin = users.create_user(email='clinical-super@example.test')
        cls.patient_user = users.create_user(email='clinical-patient@example.test')
        cls.other_user = users.create_user(email='clinical-other-patient@example.test')
        for user, role in (
            (cls.doctor, 'doctor'), (cls.colleague, 'doctor'),
            (cls.admin, 'practice_admin'), (cls.super_admin, 'super_admin'),
        ):
            CompanyMembership.objects.create(company=cls.company, user=user, role=role)
        CompanyMembership.objects.create(company=cls.beta, user=cls.doctor, role='doctor')
        cls.patient = Patient.objects.create(company=cls.company, user=cls.patient_user, first_name='Alice', last_name='Patient')
        cls.other_patient = Patient.objects.create(company=cls.company, user=cls.other_user, first_name='Beth', last_name='Patient')
        cls.beta_patient = Patient.objects.create(company=cls.beta, user=cls.patient_user, first_name='Alice', last_name='Beta')
        cls.occurred_at = timezone.now().replace(microsecond=0) - timedelta(hours=1)
        cls.appointment = Appointment.objects.create(
            company=cls.company, patient=cls.patient, clinician=cls.doctor,
            starts_at=cls.occurred_at, duration_minutes=30, status='booked',
        )

    def consultation(self, **overrides):
        values = {
            'company': self.company, 'patient': self.patient, 'actor': self.doctor,
            'summary': 'Confidential clinical consultation.', 'occurred_at': self.occurred_at,
            'submission_key': uuid.uuid4(),
        }
        values.update(overrides)
        return save_consultation(**values)

    def lab(self, **overrides):
        values = {
            'company': self.company, 'patient': self.patient, 'actor': self.doctor,
            'panel_name': 'HbA1c, U&E, LFT and lipids', 'submission_key': uuid.uuid4(),
        }
        values.update(overrides)
        return create_lab_request(**values)

    def upload(self, request, **overrides):
        values = {'lab_request': request, 'actor': self.patient_user, 'filename': 'results.pdf', 'content': PDF}
        values.update(overrides)
        return submit_lab_result(**values)

    def test_draft_creates_one_author_assigned_signing_task_and_revision(self):
        encounter = self.consultation(summary='')
        self.assertEqual(encounter.status, ClinicalEncounter.Status.DRAFT)
        self.assertGreater(encounter.revision, 0)
        self.assertIsNone(encounter.signed_at)
        self.assertIsNone(encounter.signed_note_id)
        task = encounter.signing_task
        self.assertEqual((task.company_id, task.patient_id, task.assigned_to_id), (self.company.pk, self.patient.pk, self.doctor.pk))
        self.assertEqual(task.status, ClinicalTask.Status.OPEN)
        self.assertTrue(is_clinical_workflow_task(task))
        self.assertFalse(ClinicalNote.objects.exists())

    def test_draft_edit_increments_revision_and_stale_edit_is_rejected(self):
        encounter = self.consultation()
        revision = encounter.revision
        edited = self.consultation(encounter=encounter, expected_revision=revision, summary='Updated clinical draft.')
        self.assertEqual(edited.pk, encounter.pk)
        self.assertEqual(edited.revision, revision + 1)
        self.assertEqual(ClinicalTask.objects.count(), 1)
        with self.assertRaises(ValidationError):
            self.consultation(encounter=edited, expected_revision=revision, summary='Stale content must not overwrite.')
        edited.refresh_from_db()
        self.assertEqual(edited.clinical_summary, 'Updated clinical draft.')

    def test_signing_creates_snapshot_and_completes_task_without_changing_appointments(self):
        encounter = self.consultation(appointment=self.appointment)
        appointment_before = list(Appointment.objects.values())
        signed = self.consultation(
            encounter=encounter, expected_revision=encounter.revision,
            summary='Signed clinical narrative.', sign=True,
        )
        self.assertEqual(signed.status, ClinicalEncounter.Status.SIGNED)
        self.assertEqual(signed.signed_by_id, self.doctor.pk)
        self.assertIsNotNone(signed.signed_at)
        self.assertEqual(signed.signed_note.body, 'Signed clinical narrative.')
        self.assertEqual((signed.signed_note.company_id, signed.signed_note.patient_id, signed.signed_note.author_id),
                         (self.company.pk, self.patient.pk, self.doctor.pk))
        self.assertEqual(signed.signing_task.status, ClinicalTask.Status.DONE)
        self.assertIsNotNone(signed.signing_task.completed_at)
        self.assertEqual(list(Appointment.objects.values()), appointment_before)
        self.assertFalse(Payment.objects.exists())
        self.assertFalse(TreatmentAuthorization.objects.exists())

    def test_signed_replay_is_idempotent_and_changed_content_cannot_overwrite(self):
        encounter = self.consultation()
        old_revision = encounter.revision
        signed = self.consultation(encounter=encounter, expected_revision=old_revision, sign=True)
        before = (ClinicalNote.objects.count(), AuditEvent.objects.count(), signed.revision, signed.signed_at)
        replay = self.consultation(encounter=signed, expected_revision=old_revision, sign=True)
        self.assertEqual(replay.pk, signed.pk)
        self.assertEqual((ClinicalNote.objects.count(), AuditEvent.objects.count(), replay.revision, replay.signed_at), before)
        with self.assertRaises(ValidationError):
            self.consultation(encounter=signed, expected_revision=signed.revision, summary='Overwrite signed contents.', sign=True)
        signed.refresh_from_db()
        self.assertEqual(signed.clinical_summary, 'Confidential clinical consultation.')
        self.assertEqual(signed.signed_note.body, signed.clinical_summary)

    def test_consultation_create_uuid_deduplicates_without_editing_existing_draft(self):
        key = uuid.uuid4()
        encounter = self.consultation(submission_key=key)
        repeated = self.consultation(submission_key=key, summary='Replay must not edit the original.')
        self.assertEqual(repeated.pk, encounter.pk)
        self.assertEqual(repeated.clinical_summary, encounter.clinical_summary)
        self.assertEqual((ClinicalEncounter.objects.count(), ClinicalTask.objects.count()), (1, 1))
        with self.assertRaises((PermissionDenied, ValidationError)):
            self.consultation(submission_key=key, patient=self.other_patient)

    def test_only_doctor_can_create_and_only_author_can_edit_or_sign(self):
        for actor in (self.admin, self.super_admin, self.patient_user):
            with self.assertRaises(PermissionDenied):
                self.consultation(actor=actor)
        encounter = self.consultation()
        for actor in (self.colleague, self.admin, self.super_admin, self.patient_user):
            with self.subTest(actor=actor.email):
                with self.assertRaises(PermissionDenied):
                    self.consultation(actor=actor, encounter=encounter, expected_revision=encounter.revision, sign=True)

    def test_consultation_validates_summary_signing_and_practice_patient(self):
        for changes in ({'summary': 'x' * 10001}, {'summary': '   ', 'sign': True}, {'patient': self.beta_patient}):
            with self.subTest(changes=str(changes)[:80]):
                with self.assertRaises((ValidationError, PermissionDenied)):
                    self.consultation(**changes)
        self.assertFalse(ClinicalEncounter.objects.exists())
        self.assertFalse(ClinicalTask.objects.exists())

    def test_omitted_appointment_is_preserved_but_rebinding_is_rejected(self):
        encounter = self.consultation(appointment=self.appointment)
        updated = self.consultation(encounter=encounter, expected_revision=encounter.revision, summary='Draft revision.')
        self.assertEqual(updated.appointment_id, self.appointment.pk)
        replacement = Appointment.objects.create(
            company=self.company, patient=self.patient, clinician=self.doctor,
            starts_at=self.occurred_at - timedelta(days=1),
        )
        with self.assertRaises(ValidationError):
            self.consultation(encounter=updated, expected_revision=updated.revision, appointment=replacement)

    def test_lab_creation_is_scoped_and_uuid_replay_does_not_replace_panel(self):
        key = uuid.uuid4()
        request = self.lab(submission_key=key)
        replay = self.lab(submission_key=key, panel_name='Changed replay panel')
        self.assertEqual((replay.pk, replay.panel_name), (request.pk, request.panel_name))
        self.assertEqual((request.company_id, request.patient_id, request.requested_by_id), (self.company.pk, self.patient.pk, self.doctor.pk))
        self.assertEqual(request.status, LabRequest.Status.REQUESTED)
        self.assertIsNone(request.review_task_id)
        self.assertEqual(LabRequest.objects.count(), 1)
        self.assertFalse(LabResult.objects.exists())

    def test_lab_creation_requires_active_doctor_valid_panel_and_matching_patient(self):
        for actor in (self.admin, self.super_admin, self.patient_user):
            with self.assertRaises(PermissionDenied):
                self.lab(actor=actor)
        for changes in ({'panel_name': ''}, {'panel_name': 'x' * 256}, {'patient': self.beta_patient}):
            with self.assertRaises((PermissionDenied, ValidationError)):
                self.lab(**changes)
        CompanyMembership.objects.filter(company=self.company, user=self.doctor).update(is_active=False)
        with self.assertRaises(PermissionDenied):
            self.lab()
        self.assertFalse(LabRequest.objects.exists())

    def test_owned_pdf_upload_creates_private_result_hash_and_one_doctor_review_task(self):
        request = self.lab()
        result = self.upload(request)
        request.refresh_from_db()
        self.assertEqual((result.company_id, result.patient_id, result.uploaded_by_id), (self.company.pk, self.patient.pk, self.patient_user.pk))
        self.assertEqual(bytes(result.content), PDF)
        self.assertEqual(result.size, len(PDF))
        self.assertEqual(result.sha256, hashlib.sha256(PDF).hexdigest())
        self.assertEqual(request.status, LabRequest.Status.UPLOADED)
        self.assertEqual(request.review_task.assigned_to_id, self.doctor.pk)
        self.assertEqual(request.review_task.status, ClinicalTask.Status.OPEN)
        self.assertTrue(is_clinical_workflow_task(request.review_task))

    def test_identical_pdf_replay_has_one_result_and_task_but_changed_report_is_rejected(self):
        request = self.lab()
        first = self.upload(request)
        before = (LabResult.objects.count(), ClinicalTask.objects.count(), AuditEvent.objects.count())
        repeated = self.upload(request, filename='another-name.pdf')
        self.assertEqual(repeated.pk, first.pk)
        self.assertEqual((LabResult.objects.count(), ClinicalTask.objects.count(), AuditEvent.objects.count()), before)
        with self.assertRaises(ValidationError):
            self.upload(request, content=PDF.replace(b'Catalog', b'Pages'))
        first.refresh_from_db()
        self.assertEqual(bytes(first.content), PDF)

    def test_pdf_upload_rejects_non_pdf_incomplete_and_oversized_content(self):
        request = self.lab()
        for content in (b'', b'<html>not a PDF</html>', b'%PDF-1.4\nno end marker', b'%PDF-' + b'x' * (5 * 1024 * 1024) + b'%%EOF'):
            with self.subTest(size=len(content)):
                with self.assertRaises(ValidationError):
                    self.upload(request, content=content)
        self.assertFalse(LabResult.objects.exists())
        self.assertFalse(ClinicalTask.objects.exists())

    def test_upload_is_limited_to_patient_owner_or_requesting_doctor(self):
        request = self.lab()
        for actor in (self.other_user, self.colleague, self.admin, self.super_admin):
            with self.subTest(actor=actor.email):
                with self.assertRaises(PermissionDenied):
                    self.upload(request, actor=actor)
        result = self.upload(request, actor=self.doctor)
        self.assertEqual(result.uploaded_by_id, self.doctor.pk)

    def test_requesting_doctor_reviews_results_and_completes_review_task(self):
        request = self.lab()
        self.upload(request)
        reviewed = review_lab_request(lab_request=request, actor=self.doctor, review_note='Private clinical interpretation.')
        self.assertEqual(reviewed.status, LabRequest.Status.REVIEWED)
        self.assertEqual(reviewed.reviewed_by_id, self.doctor.pk)
        self.assertIsNotNone(reviewed.reviewed_at)
        self.assertEqual(reviewed.result_summary, 'Private clinical interpretation.')
        self.assertEqual(reviewed.review_task.status, ClinicalTask.Status.DONE)
        self.assertIsNotNone(reviewed.review_task.completed_at)
        visible = PatientEvent.objects.filter(patient=self.patient, is_patient_visible=True)
        self.assertFalse(visible.filter(detail__contains='Private clinical interpretation.').exists())
        self.assertTrue(all('Private clinical interpretation.' not in str(event.metadata) for event in AuditEvent.objects.all()))

    def test_review_requires_report_nonempty_note_and_requesting_doctor(self):
        request = self.lab()
        with self.assertRaises(ValidationError):
            review_lab_request(lab_request=request, actor=self.doctor, review_note='No uploaded report yet.')
        self.upload(request)
        for actor in (self.colleague, self.admin, self.super_admin, self.patient_user):
            with self.assertRaises(PermissionDenied):
                review_lab_request(lab_request=request, actor=actor, review_note='Not the responsible doctor.')
        for review_note in ('', ' ', 'x' * 10001):
            with self.assertRaises(ValidationError):
                review_lab_request(lab_request=request, actor=self.doctor, review_note=review_note)
        request.refresh_from_db()
        self.assertEqual(request.status, LabRequest.Status.UPLOADED)

    def test_generic_complete_cannot_bypass_consultation_signing_or_lab_review(self):
        encounter = self.consultation()
        request = self.lab()
        self.upload(request)
        request.refresh_from_db()
        for task in (encounter.signing_task, request.review_task):
            for actor in (self.doctor, self.admin, self.super_admin):
                with self.subTest(task=task.pk, actor=actor.email):
                    with self.assertRaises((PermissionDenied, ValidationError)):
                        complete_task(task=task, actor=actor)
                    task.refresh_from_db()
                    self.assertEqual(task.status, ClinicalTask.Status.OPEN)

    def test_generic_task_editor_cannot_reassign_or_close_clinical_workflow_tasks(self):
        from portal.task_forms import TaskEditorForm
        from .task_services import save_task

        encounter = self.consultation()
        task = encounter.signing_task
        form = TaskEditorForm({
            'title': 'Bypass clinical signing', 'description': 'Forged task close', 'patient': self.patient.pk,
            'assigned_to': self.admin.pk, 'priority': 'normal', 'status': 'done', 'due_at': '',
        }, company=self.company, instance=task)
        self.assertTrue(form.is_valid(), form.errors)
        with self.assertRaises((PermissionDenied, ValidationError)):
            save_task(company=self.company, actor=self.admin, form=form)
        task.refresh_from_db()
        self.assertEqual(task.status, ClinicalTask.Status.OPEN)
        self.assertEqual(task.assigned_to_id, self.doctor.pk)

    def test_inactive_patient_blocks_new_consultation_and_result_upload(self):
        request = self.lab()
        self.patient.is_active = False
        self.patient.save(update_fields=['is_active'])
        for operation in (lambda: self.consultation(), lambda: self.upload(request)):
            with self.assertRaises((PermissionDenied, ValidationError)):
                operation()
        self.assertFalse(ClinicalEncounter.objects.exists())
        self.assertFalse(LabResult.objects.exists())
