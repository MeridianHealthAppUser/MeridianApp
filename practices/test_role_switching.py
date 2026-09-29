"""An explicit technical-admin role switch changes only that person's membership."""

from datetime import time
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from care.availability import save_working_pattern
from care.models import AuditEvent, DoctorWorkingPattern
from practices.models import Company, CompanyMembership, Patient
from practices.role_switching import can_switch_practice_role, switch_own_practice_role


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
