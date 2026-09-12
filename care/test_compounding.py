import uuid
from datetime import timedelta
from io import StringIO

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.management import call_command

from practices.models import CompanyMembership

from .compounding import (cancel_compounding_record, create_compounding_record, mark_compounding_submitted,
                          review_compounding_record, update_compounding_draft)
from .models import AuditEvent, AuthorizationReviewReminder, ClinicalTask, CompoundingRecord, LabRequest, Payment, Shipment, TreatmentAuthorization
from .review_automation import due_authorizations, refresh_review_tasks
from .services import complete_task
from .test_operations import OperationsFixture


class CompoundingFixture(OperationsFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.product.is_compounded = True
        cls.product.save(update_fields=('is_compounded',))
        cls.colleague = get_user_model().objects.create_user('compounding-colleague@example.test')
        CompanyMembership.objects.create(company=cls.company, user=cls.colleague, role='doctor')

    def record(self, **overrides):
        values = dict(company=self.company, authorization=self.auth, actor=self.doctor,
                      preparation_note='Private preparation tracking note', submission_key=uuid.uuid4())
        values.update(overrides)
        return create_compounding_record(**values)

    def review_record(self, record=None, **overrides):
        record = record or self.record()
        values = dict(record=record, actor=self.doctor, confirm=True, expected_revision=record.revision)
        values.update(overrides)
        return review_compounding_record(**values)


class CompoundingWorkflowTests(CompoundingFixture):
    def test_create_snapshot_and_doctor_task_without_prescribing_or_payments(self):
        before = TreatmentAuthorization.objects.values().get(pk=self.auth.pk)
        record = self.record()
        self.assertEqual(record.status, 'draft')
        self.assertEqual(record.snapshot['maximum_dose'], self.auth.max_dose)
        self.assertEqual(record.task.assigned_to_id, self.doctor.pk)
        self.assertEqual(record.task.description, '')
        self.assertEqual(TreatmentAuthorization.objects.values().get(pk=self.auth.pk), before)
        self.assertFalse(Payment.objects.exists())
        self.assertFalse(Shipment.objects.exists())

    def test_only_owner_active_doctor_can_create_or_change(self):
        for actor in (self.colleague, self.admin, self.super_admin, self.patient_user):
            with self.assertRaises(PermissionDenied):
                self.record(actor=actor)
        record = self.record()
        for actor in (self.colleague, self.admin, self.super_admin, self.patient_user):
            with self.assertRaises(PermissionDenied):
                self.review_record(record, actor=actor)
        CompanyMembership.objects.filter(company=self.company, user=self.doctor).update(is_active=False)
        with self.assertRaises(PermissionDenied):
            self.review_record(record)

    def test_requires_current_compounded_authorization_in_matching_practice(self):
        with self.assertRaises(PermissionDenied):
            self.record(company=self.beta)
        self.product.is_compounded = False
        self.product.save(update_fields=('is_compounded',))
        with self.assertRaises(ValidationError):
            self.record()
        self.product.is_compounded = True
        self.product.save(update_fields=('is_compounded',))
        TreatmentAuthorization.objects.filter(pk=self.auth.pk).update(expires_on=self.today - timedelta(days=1))
        with self.assertRaises(ValidationError):
            self.record()

    def test_create_uuid_is_idempotent_and_bound_to_owner(self):
        key = uuid.uuid4()
        first = self.record(submission_key=key)
        count = AuditEvent.objects.count()
        self.assertEqual(self.record(submission_key=key).pk, first.pk)
        self.assertEqual(AuditEvent.objects.count(), count)
        self.assertEqual(ClinicalTask.objects.count(), 1)
        with self.assertRaises(PermissionDenied):
            self.record(submission_key=key, actor=self.colleague)

    def test_stale_draft_cannot_overwrite_and_snapshot_never_changes(self):
        record = self.record()
        revision, snapshot = record.revision, record.snapshot.copy()
        updated = update_compounding_draft(record=record, actor=self.doctor, expected_revision=revision, preparation_note='Updated note')
        with self.assertRaises(ValidationError):
            update_compounding_draft(record=record, actor=self.doctor, expected_revision=revision, preparation_note='Stale overwrite')
        updated.refresh_from_db()
        self.assertEqual(updated.preparation_note, 'Updated note')
        self.assertEqual(updated.snapshot, snapshot)

    def test_review_locks_note_and_requires_explicit_confirmation(self):
        record = self.record()
        with self.assertRaises(ValidationError):
            self.review_record(record, confirm=False)
        reviewed = self.review_record(record)
        self.assertEqual(reviewed.status, 'ready')
        self.assertEqual(reviewed.reviewed_by_id, self.doctor.pk)
        with self.assertRaises(ValidationError):
            update_compounding_draft(record=reviewed, actor=self.doctor, expected_revision=reviewed.revision, preparation_note='Overwrite')
        self.assertNotEqual(reviewed.task.status, 'done')

    def test_submission_requires_review_reference_and_confirmation_and_completes_task(self):
        record = self.record()
        with self.assertRaises(ValidationError):
            mark_compounding_submitted(record=record, actor=self.doctor, external_reference='REF-1', confirm=True, expected_revision=record.revision)
        reviewed = self.review_record(record)
        for values in ({'external_reference': ''}, {'confirm': False}):
            data = dict(record=reviewed, actor=self.doctor, external_reference='REF-1', confirm=True, expected_revision=reviewed.revision)
            data.update(values)
            with self.assertRaises(ValidationError):
                mark_compounding_submitted(**data)
        submitted = mark_compounding_submitted(record=reviewed, actor=self.doctor, external_reference='REF-1', confirm=True, expected_revision=reviewed.revision)
        submitted.task.refresh_from_db()
        self.assertEqual((submitted.status, submitted.task.status), ('submitted', 'done'))
        self.assertIsNotNone(submitted.submitted_at)
        count = AuditEvent.objects.count()
        mark_compounding_submitted(record=submitted, actor=self.doctor, external_reference='REF-1', confirm=True, expected_revision=reviewed.revision)
        self.assertEqual(AuditEvent.objects.count(), count)
        with self.assertRaises(ValidationError):
            mark_compounding_submitted(record=submitted, actor=self.doctor, external_reference='CHANGED', confirm=True, expected_revision=submitted.revision)

    def test_reviewed_record_rechecks_authorization_at_submission(self):
        record = self.review_record()
        TreatmentAuthorization.objects.filter(pk=self.auth.pk).update(status='cancelled')
        with self.assertRaises(ValidationError):
            mark_compounding_submitted(record=record, actor=self.doctor, external_reference='REF-2', confirm=True, expected_revision=record.revision)
        record.refresh_from_db()
        self.assertEqual(record.status, 'ready')

    def test_generic_task_completion_cannot_bypass_workflow(self):
        record = self.record()
        for actor in (self.doctor, self.admin, self.super_admin):
            with self.assertRaises(ValidationError):
                complete_task(task=record.task, actor=actor)
        record.task.refresh_from_db()
        self.assertEqual(record.task.status, 'open')

    def test_cancel_keeps_snapshot_and_marks_workflow_task_cancelled(self):
        record = self.record()
        cancelled = cancel_compounding_record(record=record, actor=self.doctor, confirm=True, expected_revision=record.revision)
        cancelled.task.refresh_from_db()
        self.assertEqual((cancelled.status, cancelled.task.status), ('cancelled', 'cancelled'))
        self.assertEqual(cancelled.snapshot, record.snapshot)
        self.assertEqual(CompoundingRecord.objects.count(), 1)


class ReviewRefreshTests(OperationsFixture):
    def due(self, **values):
        fields = dict(expires_on=self.today + timedelta(days=7))
        fields.update(values)
        TreatmentAuthorization.objects.filter(pk=self.auth.pk).update(**fields)
        self.auth.refresh_from_db()
        return self.auth

    def run_check(self, **overrides):
        values = dict(company=self.company, actor=self.super_admin)
        values.update(overrides)
        return refresh_review_tasks(**values)

    def test_due_date_is_earliest_explicit_authorization_or_subscription_review(self):
        self.due(expires_on=self.today + timedelta(days=80), review_interval_days=15)
        row = due_authorizations(company=self.company).get()
        self.assertEqual(row.review_due_date, self.auth.starts_on + timedelta(days=15))
        self.plan.review_due_on = self.today + timedelta(days=2)
        self.plan.save(update_fields=('review_due_on',))
        self.assertEqual(due_authorizations(company=self.company).get().review_due_date, self.plan.review_due_on)

    def test_runner_is_idempotent_and_acknowledgement_does_not_renew_treatment(self):
        self.due()
        before = TreatmentAuthorization.objects.values().get(pk=self.auth.pk)
        first, second = self.run_check(), self.run_check()
        self.assertEqual((first['created'], second['created'], second['existing']), (1, 0, 1))
        reminder = AuthorizationReviewReminder.objects.get()
        self.assertEqual(reminder.task.assigned_to_id, self.doctor.pk)
        complete_task(task=reminder.task, actor=self.doctor)
        self.assertEqual(self.run_check()['created'], 0)
        self.assertEqual(TreatmentAuthorization.objects.values().get(pk=self.auth.pk), before)
        self.assertFalse(LabRequest.objects.exists())
        self.assertFalse(Payment.objects.exists())

    def test_expiry_holds_undispatched_but_preserves_dispatched_record(self):
        draft = self.shipment(quantity=1)
        dispatched = self.shipment(quantity=1, status='dispatched', tracking_number='HISTORY')
        before = Shipment.objects.values().get(pk=dispatched.pk)
        self.due(expires_on=self.today - timedelta(days=1))
        stats = self.run_check()
        draft.refresh_from_db()
        self.assertEqual(stats['shipments_held'], 1)
        self.assertEqual(draft.status, 'held')
        self.assertEqual(Shipment.objects.values().get(pk=dispatched.pk), before)
        self.assertEqual(self.run_check()['shipments_held'], 0)

    def test_current_authorization_is_not_held_merely_because_review_is_due(self):
        draft = self.shipment(quantity=1)
        self.due()
        self.assertEqual(self.run_check()['shipments_held'], 0)
        draft.refresh_from_db()
        self.assertEqual(draft.status, 'draft')

    def test_inactive_doctor_skips_task_but_invalid_authorization_still_holds_shipment(self):
        draft = self.shipment(quantity=1)
        self.due()
        CompanyMembership.objects.filter(company=self.company, user=self.doctor).update(is_active=False)
        stats = self.run_check()
        self.assertEqual((stats['created'], stats['skipped_inactive_doctor'], stats['shipments_held']), (0, 1, 1))

    def test_run_requires_selected_practice_super_admin(self):
        self.due()
        for actor in (self.doctor, self.admin, self.patient_user):
            with self.assertRaises(PermissionDenied):
                self.run_check(actor=actor)
        with self.assertRaises(PermissionDenied):
            self.run_check(company=self.beta)
        self.assertFalse(AuthorizationReviewReminder.objects.exists())

    def test_management_command_is_explicit_local_and_audited(self):
        self.due()
        output = StringIO()
        call_command('refresh_review_tasks', company=self.company.slug, actor_email=self.super_admin.email, stdout=output)
        self.assertIn('created=1', output.getvalue())
        self.assertEqual(AuthorizationReviewReminder.objects.count(), 1)
