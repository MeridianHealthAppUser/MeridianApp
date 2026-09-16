"""Own-account profile edits cannot change access or recorded patient identities."""

from django.test import override_settings
import json
import os
import subprocess
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core import signing
from django.core.exceptions import PermissionDenied
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from care.models import AuditEvent
from practices.models import Company, CompanyMembership, Patient
from practices.services import ACTIVE_COMPANY_SESSION_KEY, ACTIVE_PATIENT_COMPANY_SESSION_KEY

from .profile import PROFILE_CONTEXT_SALT, make_profile_context, save_own_profile


@override_settings(MULTI_PRACTICE_ENABLED=True)
class AccountProfileTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.alpha = Company.objects.create(name='Profile Alpha', slug='profile-alpha')
        cls.beta = Company.objects.create(name='Profile Beta', slug='profile-beta')
        cls.foreign = Company.objects.create(name='PRIVATE OTHER PRACTICE', slug='profile-foreign')
        cls.inactive = Company.objects.create(name='Inactive profile practice', slug='profile-inactive', is_active=False)
        User = get_user_model()
        cls.doctor = User.objects.create_user('profile-doctor@example.test', first_name='Sam', last_name='Example')
        cls.admin = User.objects.create_user('profile-admin@example.test', first_name='Practice', last_name='Admin')
        cls.super_admin = User.objects.create_user('profile-super@example.test', first_name='Super', last_name='Admin')
        cls.patient_user = User.objects.create_user('profile-patient@example.test', first_name='Nadia', last_name='Example')
        cls.unlinked = User.objects.create_user('profile-unlinked@example.test')
        cls.other = User.objects.create_user('private-other@example.test', first_name='PRIVATE OTHER', last_name='PERSON')
        for user, role in ((cls.doctor, 'doctor'), (cls.admin, 'practice_admin'), (cls.super_admin, 'super_admin')):
            CompanyMembership.objects.create(company=cls.alpha, user=user, role=role)
        CompanyMembership.objects.create(company=cls.beta, user=cls.doctor, role='doctor')
        CompanyMembership.objects.create(company=cls.inactive, user=cls.doctor, role='super_admin')
        CompanyMembership.objects.create(company=cls.foreign, user=cls.other, role='super_admin')
        cls.patient = Patient.objects.create(company=cls.alpha, user=cls.patient_user, first_name='Recorded Nadia', last_name='Original', phone='0123456789')
        cls.beta_patient = Patient.objects.create(company=cls.beta, user=cls.patient_user, first_name='Other recorded name', last_name='Original')
        cls.other_patient = Patient.objects.create(company=cls.alpha, user=cls.other, first_name='Private', last_name='Record')

    def setUp(self):
        self.url = reverse('accounts:profile')

    def login(self, user=None):
        self.client.force_login(user or self.doctor)

    def data(self, **changes):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        return {'profile_context': response.context['profile_context'], 'first_name': 'Changed', 'last_name': 'Display', **changes}

    def assert_rejected(self, data):
        users_before = list(get_user_model().objects.order_by('pk').values())
        audit_before = AuditEvent.objects.count()
        response = self.client.post(self.url, data)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context['form'].errors)
        self.assertEqual(list(get_user_model().objects.order_by('pk').values()), users_before)
        self.assertEqual(AuditEvent.objects.count(), audit_before)
        return response

    def test_get_and_post_require_login(self):
        for method in ('get', 'post'):
            response = getattr(self.client, method)(self.url)
            self.assertRedirects(response, f'{reverse("accounts:login")}?next={self.url}', fetch_redirect_response=False)

    def test_get_and_head_are_read_only_and_no_store_for_all_roles(self):
        for user in (self.doctor, self.admin, self.super_admin, self.patient_user, self.unlinked):
            with self.subTest(user=user.pk):
                self.login(user)
                before = list(get_user_model().objects.order_by('pk').values())
                audit_before = AuditEvent.objects.count()
                for method in ('get', 'head'):
                    response = getattr(self.client, method)(self.url)
                    self.assertEqual(response.status_code, 200)
                    self.assertIn('no-store', response.headers['Cache-Control'])
                self.assertEqual(list(get_user_model().objects.order_by('pk').values()), before)
                self.assertEqual(AuditEvent.objects.count(), audit_before)

    def test_form_exposes_only_names_with_read_only_email(self):
        self.login()
        response = self.client.get(self.url)
        self.assertEqual(set(response.context['form'].fields), {'first_name', 'last_name'})
        self.assertContains(response, self.doctor.email)
        self.assertNotContains(response, 'name="email"')
        self.assertNotContains(response, 'name="role"')
        self.assertNotContains(response, 'name="password"')
        self.assertContains(response, 'do not alter recorded patient names')

    def test_membership_list_is_owned_active_and_read_only(self):
        self.login()
        CompanyMembership.objects.create(company=self.foreign, user=self.doctor, role='doctor', is_active=False)
        response = self.client.get(self.url)
        self.assertEqual({row.pk for row in response.context['membership_page']}, {self.alpha.pk, self.beta.pk})
        self.assertNotContains(response, self.foreign.name)
        self.assertNotContains(response, self.inactive.name)
        self.assertNotContains(response, self.other.email)
        self.assertNotContains(response, self.other.first_name)
        self.assertContains(response, 'Doctor')

    def test_patient_and_mixed_roles_use_safe_page_contexts(self):
        self.login(self.patient_user)
        response = self.client.get(self.url)
        self.assertTrue(response.context['is_patient_portal'])
        self.assertFalse(response.context['has_staff_access'])
        self.assertContains(response, reverse('portal:patient-privacy'))
        self.assertNotContains(response, reverse('portal:account-access-history'))
        CompanyMembership.objects.create(company=self.alpha, user=self.patient_user, role='doctor')
        response = self.client.get(self.url)
        self.assertFalse(response.context['is_patient_portal'])
        self.assertTrue(response.context['has_staff_access'])
        self.assertTrue(response.context['has_patient_access'])
        alpha = next(row for row in response.context['membership_page'] if row.pk == self.alpha.pk)
        self.assertEqual(len(alpha.profile_memberships), 1)
        self.assertEqual(len(alpha.profile_patients), 1)

    def test_unlinked_account_can_manage_own_profile_without_practice_access(self):
        self.login(self.unlinked)
        data = self.data()
        self.assertRedirects(self.client.post(self.url, data), self.url)
        self.unlinked.refresh_from_db()
        self.assertEqual(self.unlinked.first_name, 'Changed')
        self.assertFalse(AuditEvent.objects.exists())
        self.assertContains(self.client.get(self.url), 'do not currently have active practice access')

    def test_save_updates_only_own_names_and_minimal_audit(self):
        self.login()
        data = self.data(first_name='  New name  ')
        patient_before = list(Patient.objects.order_by('pk').values())
        memberships_before = list(CompanyMembership.objects.order_by('pk').values())
        other_before = get_user_model().objects.get(pk=self.other.pk).__dict__.copy()
        response = self.client.post(self.url, data)
        self.assertRedirects(response, self.url)
        self.doctor.refresh_from_db()
        self.assertEqual((self.doctor.first_name, self.doctor.last_name), ('New name', 'Display'))
        self.assertEqual(list(Patient.objects.order_by('pk').values()), patient_before)
        self.assertEqual(list(CompanyMembership.objects.order_by('pk').values()), memberships_before)
        self.assertEqual(get_user_model().objects.get(pk=self.other.pk).first_name, other_before['first_name'])
        audits = list(AuditEvent.objects.filter(action='account.profile_updated'))
        self.assertEqual({row.company_id for row in audits}, {self.alpha.pk, self.beta.pk})
        for row in audits:
            self.assertEqual(row.metadata, {'fields': ['first_name', 'last_name']})
            self.assertEqual(row.target_id, str(self.doctor.pk))
            self.assertEqual(row.actor_id, self.doctor.pk)

    def test_patient_name_change_preserves_both_patient_records(self):
        self.login(self.patient_user)
        before = list(Patient.objects.order_by('pk').values())
        self.assertRedirects(self.client.post(self.url, self.data()), self.url)
        self.patient_user.refresh_from_db()
        self.assertEqual(self.patient_user.first_name, 'Changed')
        self.assertEqual(list(Patient.objects.order_by('pk').values()), before)

    def test_submitted_privilege_identity_and_target_fields_cannot_escalate(self):
        self.login()
        original_password = self.doctor.password
        original_email = self.doctor.email
        data = self.data(user_id=self.other.pk, pk=self.other.pk, email='attacker@example.test', password='replace',
            is_superuser='on', is_staff='on', is_active='', role='super_admin', company=self.foreign.pk,
            groups='1', user_permissions='1', phone='999', id_number='REWRITE')
        response = self.client.post(f'{self.url}?user_id={self.other.pk}', data)
        self.assertRedirects(response, self.url)
        self.doctor.refresh_from_db()
        self.other.refresh_from_db()
        self.assertEqual(self.doctor.email, original_email)
        self.assertEqual(self.doctor.password, original_password)
        self.assertFalse(self.doctor.is_staff)
        self.assertFalse(self.doctor.is_superuser)
        self.assertTrue(self.doctor.is_active)
        self.assertFalse(self.doctor.groups.exists())
        self.assertFalse(self.doctor.user_permissions.exists())
        self.assertEqual(self.other.first_name, 'PRIVATE OTHER')
        self.assertEqual(CompanyMembership.objects.get(company=self.alpha, user=self.doctor).role, 'doctor')
        self.assertFalse(CompanyMembership.objects.filter(company=self.foreign, user=self.doctor).exists())
        self.assertEqual(self.client.get(f'{self.url}{self.other.pk}/').status_code, 404)

    def test_super_admin_still_edits_only_their_own_account(self):
        self.login(self.super_admin)
        data = self.data(user_id=self.other.pk)
        self.assertRedirects(self.client.post(self.url, data), self.url)
        self.super_admin.refresh_from_db()
        self.other.refresh_from_db()
        self.assertEqual(self.super_admin.first_name, 'Changed')
        self.assertEqual(self.other.first_name, 'PRIVATE OTHER')

    def test_missing_tampered_wrong_shape_and_expired_tokens_rejected(self):
        self.login()
        data = self.data()
        for token in ('', 'tampered', signing.dumps([], salt=PROFILE_CONTEXT_SALT),
                      signing.dumps({'user_id': self.doctor.pk, 'version': []}, salt=PROFILE_CONTEXT_SALT)):
            with self.subTest(token=token):
                response = self.assert_rejected({**data, 'profile_context': token})
                self.assertTrue(response.context['form'].non_field_errors())
                self.assertContains(response, 'value="Changed"')
        with patch('django.core.signing.time.time', return_value=timezone.now().timestamp() + 13 * 60 * 60):
            self.assert_rejected(data)

    def test_token_from_different_login_cannot_change_new_session_account(self):
        self.login()
        data = self.data()
        self.login(self.patient_user)
        self.assert_rejected(data)

    def test_stale_tab_cannot_overwrite_newer_name(self):
        self.login()
        data = self.data()
        self.assertRedirects(self.client.post(self.url, data), self.url)
        response = self.assert_rejected({**data, 'first_name': 'Stale draft'})
        self.assertContains(response, 'value="Stale draft"')
        self.doctor.refresh_from_db()
        self.assertEqual(self.doctor.first_name, 'Changed')

    def test_global_profile_token_is_not_tied_to_selected_practice(self):
        self.login()
        data = self.data()
        session = self.client.session
        session[ACTIVE_COMPANY_SESSION_KEY] = self.beta.pk
        session[ACTIVE_PATIENT_COMPANY_SESSION_KEY] = self.beta.pk
        session.save()
        self.assertRedirects(self.client.post(self.url, data), self.url)
        self.doctor.refresh_from_db()
        self.assertEqual(self.doctor.first_name, 'Changed')

    def test_unchanged_profile_does_not_create_audit_event(self):
        self.login()
        data = self.data(first_name=self.doctor.first_name, last_name=self.doctor.last_name)
        self.assertRedirects(self.client.post(self.url, data), self.url)
        self.assertFalse(AuditEvent.objects.exists())

    def test_oversized_and_null_names_preserve_form_and_do_not_write(self):
        self.login()
        for first_name in ('x' * 151, 'Invalid\x00name'):
            self.assert_rejected(self.data(first_name=first_name))

    def test_names_are_escaped_when_rendered(self):
        self.login()
        name = '<img src=x onerror=alert(1)>'
        self.assertRedirects(self.client.post(self.url, self.data(first_name=name)), self.url)
        response = self.client.get(self.url)
        self.assertNotContains(response, name)
        self.assertContains(response, '&lt;img')

    def test_post_requires_csrf_even_with_valid_signed_profile_context(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.doctor)
        response = client.get(self.url)
        data = {'profile_context': response.context['profile_context'], 'first_name': 'CSRF safe', 'last_name': 'Example'}
        self.assertEqual(client.post(self.url, data).status_code, 403)
        self.doctor.refresh_from_db()
        self.assertEqual(self.doctor.first_name, 'Sam')
        self.assertFalse(AuditEvent.objects.exists())
        response = client.post(self.url, data, HTTP_X_CSRFTOKEN=client.cookies['csrftoken'].value)
        self.assertEqual(response.status_code, 302)

    def test_service_refreshes_active_state_before_write(self):
        token = make_profile_context(self.doctor)
        get_user_model().objects.filter(pk=self.doctor.pk).update(is_active=False)
        with self.assertRaises(PermissionDenied):
            save_own_profile(actor=self.doctor, context_token=token, first_name='Denied', last_name='Example')
        self.doctor.refresh_from_db()
        self.assertEqual(self.doctor.first_name, 'Sam')
        self.assertFalse(AuditEvent.objects.exists())

    def test_practice_access_is_paginated(self):
        self.login()
        for index in range(23):
            company = Company.objects.create(name=f'Extra {index:02}', slug=f'profile-extra-{index}')
            CompanyMembership.objects.create(company=company, user=self.doctor, role='doctor')
        first = self.client.get(self.url)
        second = self.client.get(self.url, {'page': 2})
        self.assertEqual(len(first.context['membership_page']), 20)
        self.assertEqual(len(second.context['membership_page']), 5)
        self.assertContains(first, 'Practice access pages')
        self.assertEqual(self.client.get(self.url, {'page': 'invalid'}).status_code, 200)

    def test_profile_links_use_existing_security_and_owned_contact_pages(self):
        self.login()
        response = self.client.get(self.url)
        self.assertContains(response, reverse('accounts:password-change'))
        self.assertContains(response, reverse('portal:account-access-history'))
        self.assertContains(response, f'action="{reverse("accounts:logout")}"')
        self.assertNotContains(response, 'href="mailto:')
        self.assertEqual(self.client.get(reverse('accounts:logout')).status_code, 405)

    def test_optional_browser_layout_for_staff_and_patient(self):
        if not os.environ.get('MERIDIAN_PLAYWRIGHT_PATH'):
            self.skipTest('Set MERIDIAN_PLAYWRIGHT_PATH for the optional local browser check.')
        pages = []
        for user, label in ((self.doctor, 'profile-doctor'), (self.patient_user, 'profile-patient'),
                            (self.unlinked, 'profile-no-practice')):
            self.login(user)
            pages.append({'name': label, 'html': self.client.get(self.url).content.decode()})
        root = Path(__file__).resolve().parent.parent
        result = subprocess.run([os.environ.get('MERIDIAN_NODE', 'node'), str(root / 'scripts/operations_layout_smoke.cjs')],
            cwd=root, input=json.dumps(pages), text=True, capture_output=True, timeout=90, check=False)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual(json.loads(result.stdout)['checked'], 9)
