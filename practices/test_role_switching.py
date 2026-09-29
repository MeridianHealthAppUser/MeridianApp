"""An explicit technical-admin role switch changes only that person's membership."""

from datetime import time, timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.messages import get_messages
from django.core.exceptions import PermissionDenied
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from care.availability import save_working_pattern
from care.models import (
    Appointment, AppointmentProposal, AuditEvent, ClinicalEncounter, ClinicalTask,
    CompoundingRecord, DoctorWorkingPattern, LabRequest, LabResult, MedicationProduct,
    MessageThread, PatientSubscription, TreatmentAuthorization,
)
from practices.models import Company, CompanyMembership, Patient
from practices.role_switching import ActiveClinicalWorkError, can_switch_practice_role, switch_own_practice_role


@override_settings(MULTI_PRACTICE_ENABLED=False, SINGLE_PRACTICE_SLUG='meridian-health')
class OwnPracticeRoleSwitchTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        users = get_user_model().objects
        cls.admin = users.create_superuser('technical@example.com', 'Preserved-password!2026')
        cls.other_admin = users.create_superuser('another@example.com', 'Preserved-password!2026')
        cls.staff = users.create_user('staff@example.com', 'Preserved-password!2026', is_staff=True)
        cls.company = Company.objects.create(name='Meridian Health', slug='meridian-health')
        cls.other_company = Company.objects.create(name='Another practice', slug='another-practice')
        cls.membership = CompanyMembership.objects.create(
            company=cls.company, user=cls.admin, role=CompanyMembership.Role.SUPER_ADMIN,
        )
        cls.other_membership = CompanyMembership.objects.create(
            company=cls.other_company, user=cls.admin, role=CompanyMembership.Role.DOCTOR,
        )
        cls.staff_membership = CompanyMembership.objects.create(
            company=cls.company, user=cls.staff, role=CompanyMembership.Role.SUPER_ADMIN,
        )
        cls.patient = Patient.objects.create(company=cls.company, first_name='Test', last_name='Patient')

    def setUp(self):
        self.client.force_login(self.admin)
        self.url = reverse('accounts:practice-role')

    def snapshot(self):
        return (
            list(get_user_model().objects.order_by('pk').values()),
            list(CompanyMembership.objects.order_by('pk').values()),
            list(AuditEvent.objects.order_by('pk').values()),
        )

    def test_switch_all_roles_preserves_identity_password_flags_and_other_memberships(self):
        account_before = get_user_model().objects.values().get(pk=self.admin.pk)
        other_before = CompanyMembership.objects.values().get(pk=self.other_membership.pk)
        staff_before = CompanyMembership.objects.values().get(pk=self.staff_membership.pk)
        for role in (CompanyMembership.Role.DOCTOR, CompanyMembership.Role.PRACTICE_ADMIN,
                     CompanyMembership.Role.SUPER_ADMIN):
            with self.subTest(role=role):
                response = self.client.post(self.url, {'role': role})
                self.assertRedirects(response, reverse('portal:desktop-dashboard'), fetch_redirect_response=False)
                self.membership.refresh_from_db()
                self.assertEqual(self.membership.role, role)
                self.assertEqual(int(self.client.session['_auth_user_id']), self.admin.pk)
        self.assertEqual(get_user_model().objects.values().get(pk=self.admin.pk), account_before)
        self.assertEqual(CompanyMembership.objects.values().get(pk=self.other_membership.pk), other_before)
        self.assertEqual(CompanyMembership.objects.values().get(pk=self.staff_membership.pk), staff_before)
        self.assertEqual(AuditEvent.objects.count(), 3)
        for event in AuditEvent.objects.order_by('pk'):
            self.assertEqual(event.actor, self.admin)
            self.assertEqual(event.company, self.company)
            self.assertEqual(event.action, 'account.practice_role_changed')
            self.assertEqual(event.target_id, str(self.membership.pk))
            self.assertEqual(event.metadata['user_id'], self.admin.pk)
        self.assertEqual(AuditEvent.objects.earliest('pk').metadata['previous_role'], 'super_admin')

    def test_same_role_is_a_noop_without_duplicate_audit(self):
        before = self.snapshot()
        response = self.client.post(self.url, {'role': CompanyMembership.Role.SUPER_ADMIN})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.snapshot(), before)

    def test_role_changes_existing_clinical_and_management_permissions(self):
        clinical_url = reverse('portal:clinical-consultations')
        new_consultation_url = reverse('portal:clinical-consultation-create', args=[self.patient.pk])
        management_url = reverse('portal:management-users')
        for role, management_status, clinical_status, create_status in (
            ('super_admin', 200, 200, 403),
            ('doctor', 403, 200, 200),
            ('practice_admin', 403, 403, 403),
            ('super_admin', 200, 200, 403),
        ):
            with self.subTest(role=role):
                self.assertEqual(self.client.post(self.url, {'role': role}).status_code, 302)
                self.assertEqual(self.client.get(management_url).status_code, management_status)
                self.assertEqual(self.client.get(clinical_url).status_code, clinical_status)
                self.assertEqual(self.client.get(new_consultation_url).status_code, create_status)
                self.assertEqual(self.client.get(reverse('admin:index')).status_code, 200)

    def test_real_doctor_membership_passes_existing_orm_and_availability_service_guards(self):
        days = [
            {'weekday': weekday, 'is_working': weekday < 5, 'starts_at': time(9), 'ends_at': time(17)}
            for weekday in range(7)
        ]
        with self.assertRaises(PermissionDenied):
            save_working_pattern(company=self.company, clinician=self.admin, actor=self.admin, days=days)

        self.client.post(self.url, {'role': 'doctor'})
        save_working_pattern(company=self.company, clinician=self.admin, actor=self.admin, days=days)
        self.assertEqual(DoctorWorkingPattern.objects.filter(clinician=self.admin).count(), 7)
        self.patient.assigned_doctor = self.admin
        self.patient.full_clean()
        schedule = self.client.get(reverse('portal:staff-schedule'), {'clinician': self.admin.pk})
        self.assertEqual(schedule.status_code, 200)
        self.assertTrue(schedule.context['can_manage_availability'])

        self.client.post(self.url, {'role': 'practice_admin'})
        with self.assertRaises(PermissionDenied):
            save_working_pattern(company=self.company, clinician=self.admin, actor=self.admin, days=days)
        self.assertEqual(DoctorWorkingPattern.objects.filter(clinician=self.admin).count(), 7)

    def test_switch_applies_across_sessions_without_switching_person(self):
        second_browser = Client()
        second_browser.force_login(self.admin)
        url = reverse('portal:clinical-consultation-create', args=[self.patient.pk])
        self.assertEqual(second_browser.get(url).status_code, 403)

        self.client.post(self.url, {'role': 'doctor'})

        self.assertEqual(second_browser.get(url).status_code, 200)
        self.assertEqual(int(second_browser.session['_auth_user_id']), self.admin.pk)
        self.assertEqual(int(self.client.session['_auth_user_id']), self.admin.pk)

    def test_ordinary_staff_including_practice_super_admin_cannot_switch(self):
        self.client.force_login(self.staff)
        before = self.snapshot()
        self.assertEqual(self.client.post(self.url, {'role': 'doctor'}).status_code, 403)
        self.assertEqual(self.snapshot(), before)

    def test_anonymous_post_cannot_switch(self):
        self.client.logout()
        before = self.snapshot()
        self.assertEqual(self.client.post(self.url, {'role': 'doctor'}).status_code, 403)
        self.assertEqual(self.snapshot(), before)

    def test_only_post_can_change_roles(self):
        before = self.snapshot()
        for method in ('get', 'head', 'put', 'patch', 'delete'):
            with self.subTest(method=method):
                self.assertEqual(getattr(self.client, method)(self.url, {'role': 'doctor'}).status_code, 405)
        self.assertEqual(self.snapshot(), before)

    def test_csrf_is_required_and_valid_token_allows_switch(self):
        browser = Client(enforce_csrf_checks=True)
        browser.force_login(self.admin)
        before = self.snapshot()
        self.assertEqual(browser.post(self.url, {'role': 'doctor'}).status_code, 403)
        self.assertEqual(self.snapshot(), before)
        browser.get(reverse('accounts:profile'))
        token = browser.cookies['csrftoken'].value
        response = browser.post(self.url, {'role': 'doctor', 'csrfmiddlewaretoken': token})
        self.assertEqual(response.status_code, 302)
        self.membership.refresh_from_db()
        self.assertEqual(self.membership.role, 'doctor')

    def test_invalid_ambiguous_or_forged_target_fields_write_nothing(self):
        before = self.snapshot()
        for data in (
            {}, {'role': ''}, {'role': 'is_superuser'}, {'role': ['doctor', 'super_admin']},
            {'role': 'doctor', 'user': self.other_admin.pk},
            {'role': 'doctor', 'company': self.other_company.pk},
            {'role': 'doctor', 'next': 'https://example.com/'},
        ):
            with self.subTest(data=data):
                self.assertEqual(self.client.post(self.url, data).status_code, 400)
                self.assertEqual(self.snapshot(), before)

    @override_settings(MULTI_PRACTICE_ENABLED=True)
    def test_multi_practice_mode_refuses_switching(self):
        before = self.snapshot()
        self.assertEqual(self.client.post(self.url, {'role': 'doctor'}).status_code, 403)
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(can_switch_practice_role(self.admin, self.membership, self.company))

    @override_settings(SINGLE_PRACTICE_SLUG='missing')
    def test_missing_configured_practice_does_not_fall_back_to_other_membership(self):
        before = self.snapshot()
        self.assertEqual(self.client.post(self.url, {'role': 'doctor'}).status_code, 403)
        self.assertEqual(self.snapshot(), before)

    def test_inactive_configured_practice_refuses_switching(self):
        Company.objects.filter(pk=self.company.pk).update(is_active=False)
        before = self.snapshot()
        self.assertEqual(self.client.post(self.url, {'role': 'doctor'}).status_code, 403)
        self.assertEqual(self.snapshot(), before)

    def test_inactive_membership_is_not_reactivated(self):
        CompanyMembership.objects.filter(pk=self.membership.pk).update(is_active=False)
        before = self.snapshot()
        self.assertEqual(self.client.post(self.url, {'role': 'doctor'}).status_code, 403)
        self.assertEqual(self.snapshot(), before)

    def test_foreign_practice_only_membership_does_not_create_access(self):
        self.membership.delete()
        before = self.snapshot()
        self.assertEqual(self.client.post(self.url, {'role': 'doctor'}).status_code, 403)
        self.assertEqual(self.snapshot(), before)

    def test_unassigned_technical_superuser_cannot_create_their_own_membership(self):
        self.client.force_login(self.other_admin)
        before = self.snapshot()
        self.assertEqual(self.client.post(self.url, {'role': 'doctor'}).status_code, 403)
        self.assertEqual(self.snapshot(), before)

    def test_stale_actor_cannot_bypass_lost_privileges_or_disabled_account(self):
        for flag in ('is_staff', 'is_superuser', 'is_active'):
            get_user_model().objects.filter(pk=self.admin.pk).update(
                is_staff=True, is_superuser=True, is_active=True,
            )
            get_user_model().objects.filter(pk=self.admin.pk).update(**{flag: False})
            before = self.snapshot()
            with self.subTest(flag=flag), self.assertRaises(PermissionDenied):
                switch_own_practice_role(actor=self.admin, role='doctor')
            self.assertEqual(self.snapshot(), before)

    def test_audit_failure_rolls_back_the_membership_change(self):
        before = self.snapshot()
        with patch('practices.role_switching.record_audit', side_effect=RuntimeError('Audit failed')):
            with self.assertRaisesMessage(RuntimeError, 'Audit failed'):
                switch_own_practice_role(actor=self.admin, role='doctor')
        self.assertEqual(self.snapshot(), before)

    def test_presentation_helper_uses_loaded_records_and_never_authorizes_another_person(self):
        with self.assertNumQueries(0):
            self.assertTrue(can_switch_practice_role(self.admin, self.membership, self.company))
            self.assertFalse(can_switch_practice_role(self.other_admin, self.membership, self.company))
            self.assertFalse(can_switch_practice_role(self.staff, self.staff_membership, self.company))
            self.assertFalse(can_switch_practice_role(self.admin, self.other_membership, self.other_company))
            self.assertFalse(can_switch_practice_role(self.admin, None, self.company))


@override_settings(MULTI_PRACTICE_ENABLED=False, SINGLE_PRACTICE_SLUG='meridian-health')
class DoctorRoleContinuityTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = get_user_model().objects.create_superuser('doctor-admin@example.com', 'Unchanged!2026')
        cls.patient_user = get_user_model().objects.create_user('patient@example.com')
        cls.company = Company.objects.create(name='Meridian Health', slug='meridian-health')
        cls.membership = CompanyMembership.objects.create(company=cls.company, user=cls.admin, role='doctor')
        cls.patient = Patient.objects.create(
            company=cls.company, user=cls.patient_user, first_name='Test', last_name='Patient',
        )
        cls.product = MedicationProduct.objects.create(company=cls.company, name='Test product', price=100)

    def setUp(self):
        self.client.force_login(self.admin)
        self.url = reverse('accounts:practice-role')

    def snapshot(self):
        return {
            model._meta.label: list(model.objects.order_by('pk').values())
            for model in (
                get_user_model(), CompanyMembership, Patient, Appointment, AppointmentProposal,
                ClinicalEncounter, CompoundingRecord, LabRequest, LabResult, ClinicalTask,
                TreatmentAuthorization, PatientSubscription, AuditEvent,
            )
        }

    def authorization(self, **overrides):
        data = dict(
            company=self.company, patient=self.patient, product=self.product, prescribed_by=self.admin,
            max_dose='Test instructions', starts_on=timezone.localdate(),
            expires_on=timezone.localdate() + timedelta(days=90),
        )
        data.update(overrides)
        return TreatmentAuthorization.objects.create(**data)

    def assert_departure_blocked(self, category):
        before = self.snapshot()
        for role in ('super_admin', 'practice_admin'):
            with self.subTest(role=role), self.assertRaisesMessage(ActiveClinicalWorkError, category):
                switch_own_practice_role(actor=self.admin, role=role)
            self.assertEqual(self.snapshot(), before)
        response = self.client.post(self.url, {'role': 'super_admin'})
        self.assertRedirects(response, reverse('portal:desktop-dashboard'), fetch_redirect_response=False)
        message = ' '.join(str(item) for item in get_messages(response.wsgi_request))
        self.assertIn('Your role remains Doctor', message)
        self.assertIn(category, message)
        self.assertIn('technical administration area is still available', message)
        self.assertEqual(self.snapshot(), before)

    def test_active_authorization_keeps_care_plan_eligible_after_blocked_switch(self):
        from care.treatment import authorization_is_current, ensure_subscription_eligible

        authorization = self.authorization()
        plan = PatientSubscription.objects.create(
            company=self.company, patient=self.patient, authorization=authorization,
            plan_name='Test care plan', monthly_amount=100,
        )
        self.assertTrue(authorization_is_current(authorization))
        self.assert_departure_blocked('active treatment authorisations')
        self.assertTrue(authorization_is_current(authorization))
        self.assertEqual(ensure_subscription_eligible(plan).pk, authorization.pk)

    def test_future_start_authorization_is_also_a_commitment(self):
        self.authorization(starts_on=timezone.localdate() + timedelta(days=10))
        self.assert_departure_blocked('active treatment authorisations')

    def test_requested_and_uploaded_labs_keep_patient_upload_and_doctor_review_working(self):
        from care.clinical import create_lab_request, review_lab_request, submit_lab_result

        lab = create_lab_request(
            company=self.company, patient=self.patient, actor=self.admin, panel_name='Test panel',
        )
        self.assert_departure_blocked('outstanding laboratory requests')
        result = submit_lab_result(
            lab_request=lab, actor=self.patient_user, filename='result.pdf',
            content=b'%PDF-1.4\n1 0 obj << /Type /Catalog >> endobj\n%%EOF',
        )
        self.assertEqual(result.uploaded_by, self.patient_user)
        self.assert_departure_blocked('outstanding laboratory requests')
        reviewed = review_lab_request(lab_request=lab, actor=self.admin, review_note='Test review complete.')
        self.assertEqual(reviewed.status, LabRequest.Status.REVIEWED)
        changed = switch_own_practice_role(actor=self.admin, role='super_admin')
        self.assertEqual(changed.role, 'super_admin')

    @override_settings(VIDEO_ENABLED=True)
    def test_booked_future_ongoing_and_unrecorded_past_visits_block_and_video_stays_available(self):
        from video.access import resolve_room_access

        now = timezone.now()
        appointment = Appointment.objects.create(
            company=self.company, patient=self.patient, clinician=self.admin,
            starts_at=now - timedelta(minutes=5), duration_minutes=30,
        )
        self.assertEqual(resolve_room_access(self.patient_user.pk, appointment.pk, now=now).role, 'patient')
        self.assert_departure_blocked('booked appointments')
        self.assertEqual(resolve_room_access(self.patient_user.pk, appointment.pk, now=now).role, 'patient')
        self.assertEqual(resolve_room_access(self.admin.pk, appointment.pk, now=now).role, 'doctor')
        for start in (now + timedelta(days=1), now - timedelta(days=1)):
            Appointment.objects.filter(pk=appointment.pk).update(starts_at=start)
            self.assert_departure_blocked('booked appointments')

    def test_unsigned_draft_and_completed_consultations_block(self):
        encounter = ClinicalEncounter.objects.create(company=self.company, patient=self.patient, clinician=self.admin)
        for status in (ClinicalEncounter.Status.DRAFT, ClinicalEncounter.Status.COMPLETED):
            ClinicalEncounter.objects.filter(pk=encounter.pk).update(status=status)
            self.assert_departure_blocked('unsigned consultations')

    def test_draft_and_ready_compounding_remain_owned_by_doctor_even_after_authorization_expires(self):
        authorization = self.authorization(expires_on=timezone.localdate() - timedelta(days=1))
        record = CompoundingRecord.objects.create(
            company=self.company, patient=self.patient, clinician=self.admin, authorization=authorization,
        )
        self.assert_departure_blocked('unfinished compounding records')
        CompoundingRecord.objects.filter(pk=record.pk).update(
            status='ready', reviewed_by=self.admin, reviewed_at=timezone.now(),
        )
        self.assert_departure_blocked('unfinished compounding records')

    def test_active_patient_assignment_remains_a_valid_doctor_assignment(self):
        self.patient.assigned_doctor = self.admin
        self.patient.save(update_fields=('assigned_doctor',))
        self.assert_departure_blocked('active patient assignments')
        self.patient.refresh_from_db()
        self.patient.full_clean()

    def test_pending_rebook_on_cancelled_appointment_also_blocks(self):
        appointment = Appointment.objects.create(
            company=self.company, patient=self.patient, clinician=self.admin,
            starts_at=timezone.now() - timedelta(days=1), status='cancelled',
        )
        thread = MessageThread.objects.create(company=self.company, patient=self.patient, subject='Test booking')
        AppointmentProposal.objects.create(
            company=self.company, patient=self.patient, appointment=appointment, thread=thread,
            proposed_by=self.admin, recipient=self.patient_user, proposer_role='doctor', kind='rebook',
            original_clinician=self.admin, original_duration_minutes=15,
            original_starts_at=appointment.starts_at, original_status='cancelled',
            proposed_starts_at=timezone.now() + timedelta(days=1),
        )
        self.assert_departure_blocked('pending appointment changes')

    def test_historical_closed_records_and_ordinary_tasks_do_not_prevent_clean_departure(self):
        self.authorization(expires_on=timezone.localdate() - timedelta(days=1))
        cancelled = self.authorization(status='cancelled')
        LabRequest.objects.create(company=self.company, patient=self.patient, requested_by=self.admin,
                                  panel_name='Reviewed panel', status='reviewed')
        Appointment.objects.create(company=self.company, patient=self.patient, clinician=self.admin,
                                   starts_at=timezone.now() - timedelta(days=1), status='completed')
        ClinicalEncounter.objects.create(company=self.company, patient=self.patient, clinician=self.admin,
                                         status='signed', signed_at=timezone.now(), signed_by=self.admin)
        CompoundingRecord.objects.create(company=self.company, patient=self.patient, clinician=self.admin,
                                        authorization=cancelled, status='cancelled')
        ClinicalTask.objects.create(company=self.company, assigned_to=self.admin, title='Ordinary staff task')
        self.assertEqual(switch_own_practice_role(actor=self.admin, role='super_admin').role, 'super_admin')

    def test_another_doctors_care_does_not_block_this_users_role(self):
        other = get_user_model().objects.create_user('other-doctor@example.com')
        CompanyMembership.objects.create(company=self.company, user=other, role='doctor')
        self.authorization(prescribed_by=other)
        LabRequest.objects.create(company=self.company, patient=self.patient, requested_by=other, panel_name='Test')
        Appointment.objects.create(company=self.company, patient=self.patient, clinician=other,
                                   starts_at=timezone.now() + timedelta(days=1))
        self.assertEqual(switch_own_practice_role(actor=self.admin, role='super_admin').role, 'super_admin')

    def test_current_doctor_can_select_doctor_again_without_disturbing_active_care(self):
        self.authorization()
        before = self.snapshot()
        self.assertEqual(switch_own_practice_role(actor=self.admin, role='doctor').role, 'doctor')
        self.assertEqual(self.snapshot(), before)
