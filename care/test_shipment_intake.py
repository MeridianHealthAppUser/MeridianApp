"""Manual shipment intake cannot bypass the same authorization/tenant gates."""

import uuid
from datetime import timedelta

from django.contrib import admin
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import RequestFactory

from practices.models import CompanyMembership, Patient
from . import models
from .models import AuditEvent, MedicationProduct, Payment, Shipment, StockMovement
from .shipment_intake import create_manual_shipment
from .test_operations import OperationsFixture


class ShipmentIntakeTests(OperationsFixture):
    def create(self, **extra):
        values = dict(company=self.company, patient=self.patient, actor=self.admin, product=self.product,
                      quantity=2, scheduled_for=self.today, submission_key=str(uuid.uuid4()))
        values.update(extra)
        return create_manual_shipment(**values)

    def test_creates_draft_only_without_stock_payment_or_user_creation(self):
        before = get_user_model().objects.count()
        shipment = self.create()
        self.assertEqual(shipment.status, 'draft')
        self.assertEqual(shipment.patient, self.patient)
        self.assertEqual(shipment.company, self.company)
        self.assertEqual(shipment.items.get().dose, self.auth.max_dose)
        self.assertIsNone(shipment.items.get().batch_id)
        self.assertFalse(StockMovement.objects.exists())
        self.assertFalse(Payment.objects.exists())
        self.assertEqual(get_user_model().objects.count(), before)
        self.assertEqual(AuditEvent.objects.filter(action='shipment.created').count(), 1)

    def test_identical_submission_replay_returns_same_draft_without_duplicate_audit(self):
        key = str(uuid.uuid4())
        shipment = self.create(submission_key=key)
        replay = self.create(submission_key=key)
        self.assertEqual(shipment.pk, replay.pk)
        self.assertEqual(Shipment.objects.count(), 1)
        self.assertEqual(AuditEvent.objects.count(), 1)

    def test_replay_cannot_change_patient_product_or_actor(self):
        key = str(uuid.uuid4())
        self.create(submission_key=key)
        for extra in ({'patient': self.other_patient}, {'product': self.open_product}, {'actor': self.super_admin}):
            with self.subTest(extra=extra), self.assertRaises(PermissionDenied):
                self.create(submission_key=key, **extra)
        self.assertEqual(Shipment.objects.count(), 1)

    def test_malformed_submission_keys_are_rejected(self):
        for key in ('', None, 'not-a-uuid', 'x' * 500):
            with self.subTest(key=key), self.assertRaises(ValidationError):
                self.create(submission_key=key)
        self.assertFalse(Shipment.objects.exists())

    def test_doctor_patient_foreign_and_inactive_memberships_cannot_create_shipments(self):
        for actor in (self.doctor, self.patient_user, self.beta_admin):
            with self.subTest(actor=actor), self.assertRaises(PermissionDenied):
                self.create(actor=actor)
        CompanyMembership.objects.filter(company=self.company, user=self.admin).update(is_active=False)
        with self.assertRaises(PermissionDenied):
            self.create()

    def test_foreign_inactive_patient_and_product_rejected_without_partial_rows(self):
        for extra in ({'patient': self.beta_patient}, {'product': self.beta_product}):
            with self.subTest(extra=extra), self.assertRaises(ValidationError):
                self.create(**extra)
        Patient.objects.filter(pk=self.patient.pk).update(is_active=False)
        with self.assertRaises(ValidationError):
            self.create()
        Patient.objects.filter(pk=self.patient.pk).update(is_active=True)
        MedicationProduct.objects.filter(pk=self.product.pk).update(is_active=False)
        with self.assertRaises(ValidationError):
            self.create()
        self.assertFalse(Shipment.objects.exists())

    def test_bad_quantity_or_date_are_validation_errors_not_runtime_failures(self):
        for extra in ({'quantity': 0}, {'quantity': True}, {'quantity': 101}, {'scheduled_for': None},
                      {'scheduled_for': 'bad'}, {'scheduled_for': self.today - timedelta(days=1)}):
            with self.subTest(extra=extra), self.assertRaises(ValidationError):
                self.create(**extra)
        self.assertFalse(Shipment.objects.exists())

    def test_no_authorization_or_remaining_allowance_blocks_intake(self):
        self.shipment(quantity=4)
        with self.assertRaises(ValidationError):
            self.create()
        product = MedicationProduct.objects.create(company=self.company, name='Unprescribed product', price=1)
        with self.assertRaises(ValidationError):
            self.create(product=product)
        self.assertEqual(Shipment.objects.count(), 1)


class OperationsAdminGuardTests(OperationsFixture):
    guarded_models = (
        models.MedicationProduct, models.TreatmentAuthorization, models.PatientSubscription,
        models.MedicationBatch, models.StockMovement, models.Shipment, models.ShipmentItem,
        models.PharmacyOrder, models.PharmacyOrderItem, models.AuditEvent,
        models.AdministrativeFollowUp, models.PatientCommunicationPreference, models.PatientDataRequest,
        models.PatientDataRequestReply, models.DoctorActivityStatement,
        models.CompoundingRecord, models.AuthorizationReviewReminder,
        models.ClinicalNote, models.Appointment, models.AvailabilitySlot,
    )

    def setUp(self):
        self.request = RequestFactory().post('/admin/')
        self.request.user = get_user_model().objects.create_superuser('guard-root@example.test', 'A-Strong!Local-Password')

    def test_even_global_superuser_gets_read_only_workflows_and_no_bulk_actions(self):
        for model in self.guarded_models:
            with self.subTest(model=model.__name__):
                model_admin = admin.site._registry[model]
                self.assertTrue(model_admin.has_view_permission(self.request))
                self.assertFalse(model_admin.has_add_permission(self.request))
                self.assertFalse(model_admin.has_change_permission(self.request))
                self.assertFalse(model_admin.has_delete_permission(self.request))
                self.assertEqual(model_admin.get_actions(self.request), {})
                self.assertEqual(set(model_admin.get_readonly_fields(self.request)),
                                 {field.name for field in model._meta.concrete_fields})

    def test_direct_admin_save_related_delete_and_bulk_handlers_cannot_bypass_guards(self):
        for model in self.guarded_models:
            model_admin = admin.site._registry[model]
            with self.subTest(model=model.__name__):
                for call in (
                    lambda: model_admin.save_model(self.request, model(), None, False),
                    lambda: model_admin.save_model(self.request, model(), None, True),
                    lambda: model_admin.save_related(self.request, None, [], False),
                    lambda: model_admin.delete_model(self.request, model()),
                    lambda: model_admin.delete_queryset(self.request, model.objects.none()),
                ):
                    with self.assertRaises(PermissionDenied):
                        call()

    def test_existing_clinical_workflow_admin_guards_are_retained(self):
        for model in (models.ClinicalEncounter, models.LabRequest, models.LabResult):
            model_admin = admin.site._registry[model]
            self.assertFalse(model_admin.has_add_permission(self.request))
            self.assertFalse(model_admin.has_change_permission(self.request))
            self.assertFalse(model_admin.has_delete_permission(self.request))

    def test_compounding_task_cannot_be_changed_or_deleted_through_legacy_task_admin(self):
        task = models.ClinicalTask.objects.create(company=self.company, patient=self.patient,
            assigned_to=self.doctor, title='Review compounding record')
        models.CompoundingRecord.objects.create(company=self.company, patient=self.patient,
            authorization=self.auth, clinician=self.doctor, task=task, snapshot={})
        model_admin = admin.site._registry[models.ClinicalTask]
        self.assertFalse(model_admin.has_change_permission(self.request, task))
        self.assertFalse(model_admin.has_delete_permission(self.request, task))
        with self.assertRaises(PermissionDenied):
            model_admin.save_model(self.request, task, None, True)
        with self.assertRaises(PermissionDenied):
            model_admin.delete_queryset(self.request, models.ClinicalTask.objects.filter(pk=task.pk))
        self.assertTrue(models.ClinicalTask.objects.filter(pk=task.pk).exists())

    def test_real_admin_post_cannot_edit_stock_or_delete_audit_records(self):
        batch = self.receive()
        self.client.force_login(self.request.user)
        from django.urls import reverse
        edit = reverse('admin:care_medicationbatch_change', args=[batch.pk])
        self.assertEqual(self.client.get(edit).status_code, 200)
        self.assertEqual(self.client.post(edit, {'quantity_on_hand': 999}).status_code, 403)
        event = AuditEvent.objects.get(action='stock.received')
        delete = reverse('admin:care_auditevent_delete', args=[event.pk])
        self.assertEqual(self.client.post(delete, {'post': 'yes'}).status_code, 403)
        self.assertEqual(self.client.post(reverse('admin:care_auditevent_changelist'),
            {'action': 'delete_selected', '_selected_action': event.pk, 'post': 'yes'}).status_code, 200)
        self.assertTrue(AuditEvent.objects.filter(pk=event.pk).exists())
        self.assert_stock(batch, 10)
