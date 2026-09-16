"""The technical admin cannot bypass signing, review or report retention."""

from django.test import override_settings
from datetime import timedelta
from uuid import uuid4

from django.contrib import admin
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import RequestFactory, TestCase
from django.urls import reverse
from django.utils import timezone

from practices.models import Company, CompanyMembership, Patient

from .clinical import create_lab_request, review_lab_request, save_consultation, submit_lab_result
from .models import Appointment, AuditEvent, AvailabilitySlot, ClinicalEncounter, ClinicalNote, ClinicalTask, LabRequest, LabResult, PatientEvent


@override_settings(MULTI_PRACTICE_ENABLED=True)
class ClinicalAdminSafeguardTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name='Clinical admin test', slug='clinical-admin-test')
        users = get_user_model().objects
        cls.superuser = users.create_superuser(email='clinical-site-admin@example.test', password='AdminTest!2026')
        cls.doctor = users.create_user(email='admin-test-doctor@example.test')
        cls.patient_user = users.create_user(email='admin-test-patient@example.test')
        CompanyMembership.objects.create(company=cls.company, user=cls.doctor, role=CompanyMembership.Role.DOCTOR)
        cls.patient = Patient.objects.create(company=cls.company, user=cls.patient_user, first_name='Test', last_name='Patient')
        cls.occurred_at = timezone.now() - timedelta(hours=1)
        cls.appointment = Appointment.objects.create(
            company=cls.company, patient=cls.patient, clinician=cls.doctor, starts_at=cls.occurred_at,
        )
        cls.ordinary_appointment = Appointment.objects.create(
            company=cls.company, patient=cls.patient, clinician=cls.doctor,
            starts_at=cls.occurred_at - timedelta(hours=2),
        )
        cls.draft = save_consultation(
            company=cls.company, patient=cls.patient, actor=cls.doctor,
            summary='Private draft body', occurred_at=cls.occurred_at, submission_key=uuid4(),
        )
        cls.signed = save_consultation(
            company=cls.company, patient=cls.patient, actor=cls.doctor, appointment=cls.appointment,
            summary='Private signed body', occurred_at=cls.occurred_at, submission_key=uuid4(), sign=True,
        )
        cls.lab = create_lab_request(
            company=cls.company, patient=cls.patient, actor=cls.doctor,
            panel_name='Private panel wording', submission_key=uuid4(),
        )
        cls.report_bytes = b'%PDF-1.4\nPRIVATE_REPORT_BYTES_SENTINEL\n%%EOF'
        cls.result = submit_lab_result(
            lab_request=cls.lab, actor=cls.patient_user, filename='protected-report.pdf', content=cls.report_bytes,
        )
        cls.lab = review_lab_request(lab_request=cls.lab, actor=cls.doctor, review_note='Private review interpretation')
        cls.ordinary_task = ClinicalTask.objects.create(company=cls.company, title='Ordinary admin task', assigned_to=cls.doctor)
        cls.ordinary_note = ClinicalNote.objects.create(company=cls.company, patient=cls.patient, author=cls.doctor, body='Ordinary legacy note')
        cls.manual_slot = AvailabilitySlot.objects.create(company=cls.company, clinician=cls.doctor,
            starts_at=timezone.now() + timedelta(days=1), ends_at=timezone.now() + timedelta(days=1, minutes=30))

    def setUp(self):
        self.client.force_login(self.superuser)
        self.request = RequestFactory().get('/admin/')
        self.request.user = self.superuser

    def url(self, record_or_model, action='change'):
        meta = record_or_model._meta
        args = [record_or_model.pk] if action in ('change', 'delete') else []
        return reverse(f'admin:{meta.app_label}_{meta.model_name}_{action}', args=args)

    def test_encounters_requests_and_reports_are_readonly_even_for_site_superuser(self):
        for record in (self.draft, self.signed, self.lab, self.result):
            with self.subTest(model=type(record).__name__, pk=record.pk):
                model_admin = admin.site._registry[type(record)]
                self.assertTrue(model_admin.has_view_permission(self.request, record))
                self.assertFalse(model_admin.has_add_permission(self.request))
                self.assertFalse(model_admin.has_change_permission(self.request, record))
                self.assertFalse(model_admin.has_delete_permission(self.request, record))
                response = self.client.get(self.url(record))
                self.assertEqual(response.status_code, 200)
                self.assertNotContains(response, 'name="_save"')
                self.assertNotContains(response, 'class="deletelink"')
                self.assertEqual(self.client.post(self.url(type(record), 'add'), {}).status_code, 403)

    def test_admin_posts_cannot_change_or_delete_protected_workflow_models(self):
        for record in (self.draft, self.signed, self.lab, self.result):
            with self.subTest(model=type(record).__name__, pk=record.pk):
                self.assertEqual(self.client.post(self.url(record), {'status': 'signed', 'content': 'replacement'}).status_code, 403)
                self.assertEqual(self.client.post(self.url(record, 'delete'), {'post': 'yes'}).status_code, 403)
                self.assertTrue(type(record).objects.filter(pk=record.pk).exists())
        self.draft.refresh_from_db()
        self.result.refresh_from_db()
        self.assertEqual(self.draft.status, ClinicalEncounter.Status.DRAFT)
        self.assertEqual(bytes(self.result.content), self.report_bytes)

    def test_lab_admin_renders_only_metadata_and_defers_pdf_bytes(self):
        model_admin = admin.site._registry[LabResult]
        loaded = model_admin.get_queryset(self.request).get(pk=self.result.pk)
        self.assertIn('content', loaded.get_deferred_fields())
        self.assertNotIn('content', model_admin.get_fields(self.request, self.result))
        self.assertNotIn('content', model_admin.get_form(self.request, self.result).base_fields)
        self.assertNotIn('delete_selected', model_admin.get_actions(self.request))
        for url in (self.url(self.result), self.url(LabResult, 'changelist')):
            response = self.client.get(url)
            self.assertContains(response, 'protected-report.pdf')
            self.assertNotContains(response, 'PRIVATE_REPORT_BYTES_SENTINEL')
            self.assertNotContains(response, 'name="content"')
            self.assertNotContains(response, '/media/')

    def test_generated_tasks_and_signed_snapshots_are_readonly(self):
        records = (self.draft.signing_task, self.signed.signing_task, self.lab.review_task, self.signed.signed_note)
        for record in records:
            with self.subTest(model=type(record).__name__, pk=record.pk):
                model_admin = admin.site._registry[type(record)]
                self.assertTrue(model_admin.has_view_permission(self.request, record))
                self.assertFalse(model_admin.has_change_permission(self.request, record))
                self.assertFalse(model_admin.has_delete_permission(self.request, record))
                response = self.client.get(self.url(record))
                self.assertEqual(response.status_code, 200)
                self.assertNotContains(response, 'name="_save"')
                self.assertEqual(self.client.post(self.url(record), {'status': 'done', 'body': 'Replacement'}).status_code, 403)
                self.assertEqual(self.client.post(self.url(record, 'delete'), {'post': 'yes'}).status_code, 403)

    def test_admin_hooks_reject_protected_record_mutation_and_mixed_deletion(self):
        for protected, ordinary in (
            (self.draft.signing_task, self.ordinary_task), (self.signed.signed_note, self.ordinary_note),
            (self.appointment, self.ordinary_appointment),
        ):
            model_admin = admin.site._registry[type(protected)]
            with self.assertRaises(PermissionDenied):
                model_admin.save_model(self.request, protected, None, True)
            with self.assertRaises(PermissionDenied):
                model_admin.delete_model(self.request, protected)
            with self.assertRaises(PermissionDenied):
                model_admin.delete_queryset(self.request, type(protected).objects.filter(pk__in=(protected.pk, ordinary.pk)))
            self.assertEqual(type(protected).objects.filter(pk__in=(protected.pk, ordinary.pk)).count(), 2)

    def test_bulk_admin_action_preserves_mixed_protected_and_ordinary_selection(self):
        for protected, ordinary in (
            (self.draft.signing_task, self.ordinary_task), (self.signed.signed_note, self.ordinary_note),
            (self.appointment, self.ordinary_appointment),
        ):
            response = self.client.post(self.url(type(protected), 'changelist'), {
                'action': 'delete_selected', '_selected_action': [protected.pk, ordinary.pk], 'post': 'yes',
            })
            self.assertIn(response.status_code, (200, 403))
            self.assertEqual(type(protected).objects.filter(pk__in=(protected.pk, ordinary.pk)).count(), 2)

    def test_ordinary_administrative_task_permissions_remain_unchanged(self):
        model_admin = admin.site._registry[ClinicalTask]
        self.assertTrue(model_admin.has_add_permission(self.request))
        self.assertTrue(model_admin.has_change_permission(self.request, self.ordinary_task))
        self.assertTrue(model_admin.has_delete_permission(self.request, self.ordinary_task))
        self.assertContains(self.client.get(self.url(self.ordinary_task)), 'name="_save"')
        self.ordinary_task.title = 'Updated ordinary task'
        model_admin.save_model(self.request, self.ordinary_task, None, True)
        self.ordinary_task.refresh_from_db()
        self.assertEqual(self.ordinary_task.title, 'Updated ordinary task')

    def test_all_appointments_are_protected_and_consultation_history_links_remain_intact(self):
        model_admin = admin.site._registry[Appointment]
        self.assertTrue(model_admin.has_view_permission(self.request, self.appointment))
        self.assertFalse(model_admin.has_change_permission(self.request, self.appointment))
        self.assertFalse(model_admin.has_delete_permission(self.request, self.appointment))
        self.assertNotContains(self.client.get(self.url(self.appointment)), 'name="_save"')
        self.assertEqual(self.client.post(self.url(self.appointment), {'status': 'cancelled'}).status_code, 403)
        self.assertEqual(self.client.post(self.url(self.appointment, 'delete'), {'post': 'yes'}).status_code, 403)
        self.signed.refresh_from_db()
        self.assertEqual(self.signed.appointment_id, self.appointment.pk)
        self.assertFalse(model_admin.has_add_permission(self.request))
        self.assertFalse(model_admin.has_change_permission(self.request, self.ordinary_appointment))
        self.assertFalse(model_admin.has_delete_permission(self.request, self.ordinary_appointment))
        self.assertNotContains(self.client.get(self.url(self.ordinary_appointment)), 'name="_save"')
        self.ordinary_appointment.status = Appointment.Status.CANCELLED
        with self.assertRaises(PermissionDenied):
            model_admin.save_model(self.request, self.ordinary_appointment, None, True)
        self.ordinary_appointment.refresh_from_db()
        self.assertEqual(self.ordinary_appointment.status, Appointment.Status.BOOKED)

    def test_legacy_notes_and_manual_slots_are_inspectable_but_admin_posts_cannot_mutate_them(self):
        for record in (self.ordinary_note, self.manual_slot):
            with self.subTest(model=type(record).__name__):
                model_admin = admin.site._registry[type(record)]
                self.assertTrue(model_admin.has_view_permission(self.request, record))
                self.assertFalse(model_admin.has_add_permission(self.request))
                self.assertFalse(model_admin.has_change_permission(self.request, record))
                self.assertFalse(model_admin.has_delete_permission(self.request, record))
                self.assertEqual(model_admin.get_actions(self.request), {})
                response = self.client.get(self.url(record))
                self.assertEqual(response.status_code, 200)
                self.assertNotContains(response, 'name="_save"')
                self.assertNotContains(response, 'class="deletelink"')
                history_url = reverse(f'admin:care_{record._meta.model_name}_history', args=[record.pk])
                self.assertEqual(self.client.get(history_url).status_code, 200)
                self.assertEqual(self.client.post(self.url(type(record), 'add'), {}).status_code, 403)
                self.assertEqual(self.client.post(self.url(record), {'body': 'Replacement', 'is_booked': 'on'}).status_code, 403)
                self.assertEqual(self.client.post(self.url(record, 'delete'), {'post': 'yes'}).status_code, 403)
        self.ordinary_note.refresh_from_db()
        self.manual_slot.refresh_from_db()
        self.assertEqual(self.ordinary_note.body, 'Ordinary legacy note')
        self.assertFalse(self.manual_slot.is_booked)
        self.assertIsNone(self.manual_slot.appointment_id)

    def test_lab_workflow_task_events_and_audits_do_not_copy_private_clinical_content(self):
        self.assertEqual(self.lab.review_task.description, '')
        self.assertEqual(self.lab.review_task.created_by_id, self.doctor.pk)
        self.assertEqual(self.lab.review_task.assigned_to_id, self.doctor.pk)
        visible = list(PatientEvent.objects.filter(is_patient_visible=True).values_list('title', 'detail'))
        metadata = list(AuditEvent.objects.values_list('metadata', flat=True))
        for private in ('Private panel wording', 'Private review interpretation', 'Private signed body', 'PRIVATE_REPORT_BYTES_SENTINEL'):
            self.assertNotIn(private, str(visible))
            self.assertNotIn(private, str(metadata))
            self.assertNotIn(private, self.lab.review_task.title + self.lab.review_task.description)

    def test_consultation_appointment_link_checks_owner_practice_and_uniqueness(self):
        another_user = get_user_model().objects.create_user(email='another-admin-test-doctor@example.test')
        CompanyMembership.objects.create(company=self.company, user=another_user, role=CompanyMembership.Role.DOCTOR)
        other_patient = Patient.objects.create(company=self.company, first_name='Other', last_name='Patient')
        wrong_doctor = Appointment.objects.create(company=self.company, patient=self.patient, clinician=another_user, starts_at=self.occurred_at)
        wrong_patient = Appointment.objects.create(company=self.company, patient=other_patient, clinician=self.doctor, starts_at=self.occurred_at)
        foreign_company = Company.objects.create(name='Foreign clinical', slug='foreign-clinical-admin')
        CompanyMembership.objects.create(company=foreign_company, user=self.doctor, role=CompanyMembership.Role.DOCTOR)
        foreign_patient = Patient.objects.create(company=foreign_company, first_name='Foreign', last_name='Patient')
        wrong_company = Appointment.objects.create(company=foreign_company, patient=foreign_patient, clinician=self.doctor, starts_at=self.occurred_at)
        before = ClinicalEncounter.objects.count()
        for appointment in (wrong_doctor, wrong_patient, wrong_company, self.appointment):
            with self.subTest(appointment=appointment.pk), self.assertRaises(ValidationError):
                save_consultation(
                    company=self.company, patient=self.patient, actor=self.doctor, appointment=appointment,
                    occurred_at=self.occurred_at, summary='Must not save', submission_key=uuid4(),
                )
        self.assertEqual(ClinicalEncounter.objects.count(), before)
