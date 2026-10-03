"""Practice administration must never become a global identity/tenant bypass."""

from django.test import override_settings
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core import mail
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import Client, TestCase
from django.urls import reverse

from care.models import AuditEvent, Lead
from .management_forms import MANAGEMENT_CONTEXT_MAX_AGE
from .management_services import add_staff_user, create_practice, update_membership, update_practice
from .models import Company, CompanyMembership, Patient
from .services import ACTIVE_COMPANY_SESSION_KEY


@override_settings(MULTI_PRACTICE_ENABLED=True)
class ManagementFixture(TestCase):
    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.alpha = Company.objects.create(name='Alpha Care', slug='alpha')
        cls.beta = Company.objects.create(name='Beta Care', slug='beta')
        cls.foreign = Company.objects.create(name='Hidden Practice', slug='hidden')
        cls.owner = User.objects.create_user('owner@example.test', 'Prior-Ocean!8Birds', first_name='Owner')
        cls.doctor = User.objects.create_user('doctor@example.test', 'Prior-Ocean!8Birds', first_name='Sam')
        cls.admin = User.objects.create_user('admin@example.test', 'Prior-Ocean!8Birds')
        cls.patient_user = User.objects.create_user('patient@example.test', 'Prior-Ocean!8Birds')
        cls.owner_alpha = CompanyMembership.objects.create(user=cls.owner, company=cls.alpha, role='super_admin')
        cls.owner_beta = CompanyMembership.objects.create(user=cls.owner, company=cls.beta, role='super_admin')
        cls.doctor_alpha = CompanyMembership.objects.create(user=cls.doctor, company=cls.alpha, role='doctor')
        cls.doctor_beta = CompanyMembership.objects.create(user=cls.doctor, company=cls.beta, role='doctor')
        CompanyMembership.objects.create(user=cls.admin, company=cls.alpha, role='practice_admin')
        cls.patient = Patient.objects.create(company=cls.alpha, user=cls.patient_user, first_name='Patient', last_name='Only')

    def login(self, user=None, company=None, client=None):
        client = client or self.client
        client.force_login(user or self.owner)
        self.select(company or self.alpha, client)

    def select(self, company, client=None):
        client = client or self.client
        session = client.session
        session[ACTIVE_COMPANY_SESSION_KEY] = company.pk
        session.save()

    def token(self, name, args=None, client=None):
        response = (client or self.client).get(reverse(name, args=args))
        self.assertEqual(response.status_code, 200)
        return response.context['management_context']

    def new_staff_data(self, **extra):
        return {'mode': 'create', 'email': 'new.staff@example.test', 'first_name': 'New', 'last_name': 'Doctor',
                'role': 'doctor', 'clinician_type': 'doctor', 'practices': [self.alpha.pk], 'password1': 'Fresh-Forest!7Clouds',
                'password2': 'Fresh-Forest!7Clouds', **extra}


class ManagementServiceTests(ManagementFixture):
    def add(self, **extra):
        return add_staff_user(actor=self.owner, source_company=self.alpha, companies=[self.alpha, self.beta],
            mode='create', email='NEW.STAFF@EXAMPLE.TEST', role='doctor', first_name='New', last_name='Doctor',
            password='Fresh-Forest!7Clouds', **extra)

    def test_new_staff_login_has_multiple_practices_but_no_patient_or_global_privileges(self):
        user = self.add()
        self.assertEqual(user.email, 'new.staff@example.test')
        self.assertTrue(user.check_password('Fresh-Forest!7Clouds'))
        self.assertFalse(user.is_staff)
        self.assertFalse(user.is_superuser)
        self.assertEqual(set(user.company_memberships.values_list('company_id', flat=True)), {self.alpha.pk, self.beta.pk})
        self.assertEqual(Patient.objects.count(), 1)
        self.assertEqual(Lead.objects.count(), 0)
        events = AuditEvent.objects.filter(action='staff.account_created')
        self.assertEqual(events.count(), 2)
        for event in events:
            self.assertEqual(event.actor, self.owner)
            self.assertNotIn('password', str(event.metadata))
            self.assertNotIn(user.email, str(event.metadata))
        self.assertEqual(len(mail.outbox), 0)

    def test_link_existing_case_insensitive_never_changes_identity(self):
        original = (self.doctor.password, self.doctor.email, self.doctor.first_name, self.doctor.last_name)
        CompanyMembership.objects.filter(pk=self.doctor_beta.pk).delete()  # Fixture only: leave the target unlinked.
        result = add_staff_user(actor=self.owner, source_company=self.beta, companies=[self.beta],
            mode='link', email='DOCTOR@EXAMPLE.TEST', role='practice_admin')
        self.assertEqual(result.pk, self.doctor.pk)
        result.refresh_from_db()
        self.assertEqual((result.password, result.email, result.first_name, result.last_name), original)
        self.doctor_alpha.refresh_from_db()
        self.assertEqual(self.doctor_alpha.role, 'doctor')
        self.assertEqual(get_user_model().objects.count(), 4)

    def test_link_rejects_password_or_profile_overwrite(self):
        for kwargs in ({'password': 'Forced-Reset!8Cloud'}, {'first_name': 'Hijacked'}, {'last_name': 'Renamed'}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValidationError):
                add_staff_user(actor=self.owner, source_company=self.alpha, companies=[self.alpha],
                    mode='link', email=self.patient_user.email, role='doctor', **kwargs)
        self.assertFalse(CompanyMembership.objects.filter(user=self.patient_user).exists())
        self.assertEqual(AuditEvent.objects.count(), 0)

    def test_existing_account_duplicate_creation_does_not_reset_password(self):
        with self.assertRaises(ValidationError):
            add_staff_user(actor=self.owner, source_company=self.alpha, companies=[self.alpha],
                mode='create', email=self.doctor.email.upper(), role='doctor', first_name='Bad', last_name='Change',
                password='Fresh-Forest!7Clouds')
        self.doctor.refresh_from_db()
        self.assertTrue(self.doctor.check_password('Prior-Ocean!8Birds'))

    def test_weak_password_rejected_even_direct_service(self):
        with self.assertRaises(ValidationError):
            add_staff_user(actor=self.owner, source_company=self.alpha, companies=[self.alpha],
                mode='create', email='weak@example.test', role='doctor', first_name='Weak', last_name='Password', password='123')
        self.assertFalse(get_user_model().objects.filter(email='weak@example.test').exists())

    def test_cannot_attach_to_unauthorized_practice_or_grant_patient_role(self):
        for companies, role in (([self.alpha, self.foreign], 'doctor'), ([self.alpha], 'patient')):
            with self.subTest(role=role), self.assertRaises((PermissionDenied, ValidationError)):
                add_staff_user(actor=self.owner, source_company=self.alpha, companies=companies,
                    mode='create', email='invalid@example.test', role=role, first_name='Invalid', last_name='User',
                    password='Fresh-Forest!7Clouds')
        self.assertFalse(get_user_model().objects.filter(email='invalid@example.test').exists())
        self.assertEqual(AuditEvent.objects.count(), 0)

    def test_admin_and_global_superuser_without_membership_cannot_manage(self):
        global_root = get_user_model().objects.create_superuser('global@example.test', 'Prior-Ocean!8Birds')
        for actor in (self.admin, self.doctor, self.patient_user, global_root):
            with self.subTest(actor=actor), self.assertRaises(PermissionDenied):
                create_practice(actor=actor, source_company=self.alpha, name='Denied', slug='denied')
        self.assertEqual(Company.objects.count(), 3)

    def test_last_active_super_admin_cannot_be_removed_or_demoted(self):
        for role, active in (('doctor', True), ('super_admin', False)):
            with self.subTest(role=role), self.assertRaisesMessage(ValidationError, 'at least one active Super Admin'):
                update_membership(actor=self.owner, company=self.alpha, membership=self.owner_alpha,
                                  role=role, is_active=active)
        self.owner_alpha.refresh_from_db()
        self.assertTrue(self.owner_alpha.is_active)
        self.assertEqual(self.owner_alpha.role, 'super_admin')
        self.assertEqual(AuditEvent.objects.count(), 0)

    def test_inactive_user_super_admin_does_not_satisfy_last_admin_guard(self):
        inactive = get_user_model().objects.create_user('inactive@example.test', 'Prior-Ocean!8Birds', is_active=False)
        CompanyMembership.objects.create(company=self.alpha, user=inactive, role='super_admin')
        with self.assertRaises(ValidationError):
            update_membership(actor=self.owner, company=self.alpha, membership=self.owner_alpha,
                              role='doctor', is_active=True)

    def test_soft_disable_membership_preserves_global_login_other_practices_and_history(self):
        update_membership(actor=self.owner, company=self.alpha, membership=self.doctor_alpha,
                          role='doctor', is_active=False)
        self.doctor.refresh_from_db()
        self.doctor_alpha.refresh_from_db()
        self.doctor_beta.refresh_from_db()
        self.assertTrue(self.doctor.is_active)
        self.assertFalse(self.doctor_alpha.is_active)
        self.assertTrue(self.doctor_beta.is_active)
        self.assertEqual(CompanyMembership.objects.filter(user=self.doctor).count(), 2)
        self.assertEqual(AuditEvent.objects.get().company, self.alpha)

    def test_stale_membership_and_foreign_membership_rejected(self):
        prior = self.doctor_alpha.updated_at.isoformat()
        update_membership(actor=self.owner, company=self.alpha, membership=self.doctor_alpha,
                          role='practice_admin', is_active=True)
        with self.assertRaises(ValidationError):
            update_membership(actor=self.owner, company=self.alpha, membership=self.doctor_alpha,
                              role='super_admin', is_active=True, expected_updated_at=prior)
        with self.assertRaises(PermissionDenied):
            update_membership(actor=self.owner, company=self.alpha, membership=self.doctor_beta,
                              role='super_admin', is_active=True)
        self.assertEqual(AuditEvent.objects.count(), 1)

    def test_new_practice_gets_only_creator_super_admin_and_no_copied_patients(self):
        practice = create_practice(actor=self.owner, source_company=self.alpha, name='Fresh Care', slug='fresh')
        membership = practice.memberships.get()
        self.assertEqual(membership.user, self.owner)
        self.assertEqual(membership.role, 'super_admin')
        self.assertFalse(Patient.objects.filter(company=practice).exists())
        self.assertEqual(AuditEvent.objects.filter(company=practice).count(), 2)

    def test_practice_update_audits_only_changed_fields_and_rejects_stale_update(self):
        original = self.alpha.updated_at.isoformat()
        update_practice(actor=self.owner, company=self.alpha, name='Updated Alpha', slug=self.alpha.slug,
                        expected_updated_at=original)
        self.assertEqual(AuditEvent.objects.get().metadata, {'changed_fields': ['name']})
        with self.assertRaises(ValidationError):
            update_practice(actor=self.owner, company=self.alpha, name='Lost Update', slug='alpha',
                            expected_updated_at=original)
        self.beta.refresh_from_db()
        self.assertEqual(self.beta.name, 'Beta Care')


class ManagementViewTests(ManagementFixture):
    def setUp(self):
        self.login()

    def test_staff_list_is_separate_paginated_scoped_and_private(self):
        response = self.client.get(reverse('portal:management-users'))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'portal/management_users.html')
        self.assertEqual(response.context['page_obj'].paginator.count, 3)
        self.assertNotContains(response, self.patient_user.email)
        self.assertNotContains(response, self.foreign.name)
        self.assertIn('no-store', response['Cache-Control'])
        self.assertContains(response, reverse('portal:management-user-create'))
        self.assertNotContains(response, 'name="password1"')

    def test_anonymous_redirect_roles_forbidden_and_global_superuser_has_no_bypass(self):
        self.client.logout()
        self.assertEqual(self.client.get(reverse('portal:management-users')).status_code, 302)
        global_root = get_user_model().objects.create_superuser('global@example.test', 'Prior-Ocean!8Birds')
        for user in (self.admin, self.doctor, self.patient_user, global_root):
            self.login(user)
            for name in ('management-users', 'management-user-create', 'management-practices', 'management-practice-create'):
                with self.subTest(user=user, name=name):
                    self.assertEqual(self.client.get(reverse(f'portal:{name}')).status_code, 403)
                    self.assertEqual(self.client.post(reverse(f'portal:{name}'), {}).status_code, 403)

    def test_foreign_records_including_other_authorized_practice_are_404_until_switch(self):
        self.assertEqual(self.client.get(reverse('portal:management-membership-edit', args=[self.doctor_beta.pk])).status_code, 404)
        self.assertEqual(self.client.post(reverse('portal:management-membership-edit', args=[self.doctor_beta.pk]), {}).status_code, 404)
        self.assertEqual(self.client.get(reverse('portal:management-practice-edit', args=[self.beta.pk])).status_code, 404)

    def test_valid_new_staff_post_ignores_global_privilege_and_patient_forgeries(self):
        token = self.token('portal:management-user-create')
        response = self.client.post(reverse('portal:management-user-create'), self.new_staff_data(
            management_context=token, is_staff='on', is_superuser='on', patient=self.patient.pk,
            company=self.foreign.pk, practices=[self.alpha.pk, self.beta.pk]))
        self.assertRedirects(response, reverse('portal:management-users'), fetch_redirect_response=False)
        user = get_user_model().objects.get(email='new.staff@example.test')
        self.assertFalse(user.is_staff)
        self.assertFalse(user.is_superuser)
        self.assertEqual(user.company_memberships.count(), 2)
        self.assertFalse(Patient.objects.filter(user=user).exists())
        self.assertEqual(len(mail.outbox), 0)

    def test_form_cannot_select_unauthorized_practice_or_patient_role(self):
        for extra in ({'practices': [self.foreign.pk]}, {'role': 'patient'}):
            response = self.client.post(reverse('portal:management-user-create'), self.new_staff_data(
                management_context=self.token('portal:management-user-create'), **extra))
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.context['form'].errors)
        self.assertFalse(get_user_model().objects.filter(email='new.staff@example.test').exists())
        self.assertEqual(AuditEvent.objects.count(), 0)

    def test_new_staff_missing_invalid_expired_and_stale_practice_tokens_write_nothing(self):
        name = 'portal:management-user-create'
        for token in ('', 'tampered'):
            response = self.client.post(reverse(name), self.new_staff_data(management_context=token))
            self.assertEqual(response.status_code, 400)
        with patch('django.core.signing.time.time', return_value=1):
            expired = self.token(name)
        with patch('django.core.signing.time.time', return_value=MANAGEMENT_CONTEXT_MAX_AGE + 2):
            response = self.client.post(reverse(name), self.new_staff_data(management_context=expired))
        self.assertEqual(response.status_code, 400)
        token = self.token(name)
        self.select(self.beta)
        response = self.client.post(reverse(name), self.new_staff_data(management_context=token, practices=[self.beta.pk]))
        self.assertEqual(response.status_code, 400)
        self.assertFalse(get_user_model().objects.filter(email='new.staff@example.test').exists())
        self.assertEqual(AuditEvent.objects.count(), 0)

    def test_membership_change_requires_record_bound_context_and_preserves_identity(self):
        url = reverse('portal:management-membership-edit', args=[self.doctor_alpha.pk])
        wrong_token = self.token('portal:management-membership-edit', [self.owner_alpha.pk])
        self.assertEqual(self.client.post(url, {'role': 'super_admin', 'is_active': 'on',
                                               'management_context': wrong_token}).status_code, 400)
        token = self.token('portal:management-membership-edit', [self.doctor_alpha.pk])
        response = self.client.post(url, {'role': 'practice_admin', 'is_active': 'on', 'management_context': token,
            'email': 'overwrite@example.test', 'password': 'Forced-Reset!8Cloud', 'first_name': 'Hijacked',
            'company': self.foreign.pk, 'user': self.owner.pk})
        self.assertEqual(response.status_code, 302)
        self.doctor_alpha.refresh_from_db()
        self.doctor.refresh_from_db()
        self.doctor_beta.refresh_from_db()
        self.assertEqual(self.doctor_alpha.role, 'practice_admin')
        self.assertEqual(self.doctor_beta.role, 'doctor')
        self.assertEqual(self.doctor.email, 'doctor@example.test')
        self.assertEqual(self.doctor.first_name, 'Sam')
        self.assertTrue(self.doctor.check_password('Prior-Ocean!8Birds'))

    def test_last_admin_form_error_and_self_demote_redirects_out_after_second_admin_added(self):
        url = reverse('portal:management-membership-edit', args=[self.owner_alpha.pk])
        token = self.token('portal:management-membership-edit', [self.owner_alpha.pk])
        response = self.client.post(url, {'management_context': token, 'role': 'doctor', 'clinician_type': 'doctor', 'is_active': 'on'})
        self.assertContains(response, 'Keep at least one active Super Admin')
        self.doctor_alpha.role = 'super_admin'
        self.doctor_alpha.save()
        response = self.client.post(url, {'management_context': token, 'role': 'doctor', 'clinician_type': 'doctor', 'is_active': 'on'})
        self.assertRedirects(response, reverse('portal:desktop-dashboard'), fetch_redirect_response=False)
        self.assertEqual(self.client.get(reverse('portal:management-users')).status_code, 403)

    def test_practices_list_excludes_doctor_only_and_unrelated_practice(self):
        self.owner_beta.role = 'doctor'
        self.owner_beta.save()
        response = self.client.get(reverse('portal:management-practices'))
        self.assertEqual(list(response.context['practices']), [self.alpha])
        self.assertNotContains(response, self.foreign.name)

    def test_create_practice_post_does_not_copy_staff_patients_or_activate_email(self):
        response = self.client.post(reverse('portal:management-practice-create'), {
            'management_context': self.token('portal:management-practice-create'),
            'name': 'New Empty Practice', 'slug': 'new-empty', 'is_active': '', 'user': self.doctor.pk})
        self.assertRedirects(response, reverse('portal:management-practices'), fetch_redirect_response=False)
        practice = Company.objects.get(slug='new-empty')
        self.assertEqual(practice.memberships.get().user, self.owner)
        self.assertEqual(Patient.objects.filter(company=practice).count(), 0)
        self.assertEqual(len(mail.outbox), 0)

    def test_practice_edit_only_whitelisted_fields_and_expired_version_rejected(self):
        name = 'portal:management-practice-edit'
        token = self.token(name, [self.alpha.pk])
        url = reverse(name, args=[self.alpha.pk])
        response = self.client.post(url, {'management_context': token, 'name': 'Alpha Updated', 'slug': 'alpha-new',
                                        'is_active': '', 'company': self.foreign.pk})
        self.assertEqual(response.status_code, 302)
        self.alpha.refresh_from_db()
        self.assertTrue(self.alpha.is_active)
        self.assertEqual(self.alpha.name, 'Alpha Updated')
        response = self.client.post(url, {'management_context': token, 'name': 'Stale', 'slug': 'alpha-stale'})
        self.assertContains(response, 'updated in another tab')
        self.alpha.refresh_from_db()
        self.assertEqual(self.alpha.name, 'Alpha Updated')

    def test_required_csrf_and_get_does_not_mutate(self):
        client = Client(enforce_csrf_checks=True)
        self.login(client=client)
        for name in ('management-user-create', 'management-practice-create'):
            response = client.get(reverse(f'portal:{name}'))
            self.assertEqual(response.status_code, 200)
            self.assertEqual(client.post(reverse(f'portal:{name}'), {}).status_code, 403)
        self.assertEqual(AuditEvent.objects.count(), 0)

    def test_invalid_filter_returns_empty_list_and_pagination_reaches_last_member(self):
        for i in range(21):
            user = get_user_model().objects.create_user(f'zz-{i:02d}@example.test', None, last_name=f'ZZ{i:02d}')
            CompanyMembership.objects.create(company=self.alpha, user=user, role='doctor')
        response = self.client.get(reverse('portal:management-users'), {'page': 2, 'role': 'doctor'})
        self.assertEqual(response.context['page_obj'].number, 2)
        self.assertContains(response, 'zz-20@example.test')
        response = self.client.get(reverse('portal:management-users'), {'role': 'root'})
        self.assertTrue(response.context['filter_form'].errors)
        self.assertEqual(response.context['page_obj'].paginator.count, 0)


class PasswordChangeTests(ManagementFixture):
    def setUp(self):
        self.login()

    def password_data(self, **extra):
        return {'old_password': 'Prior-Ocean!8Birds', 'new_password1': 'Next-Forest!9Clouds',
                'new_password2': 'Next-Forest!9Clouds', **extra}

    def test_only_own_password_changes_shared_session_stays_active_and_other_session_expires(self):
        other = Client()
        other.force_login(self.owner)
        response = self.client.post(reverse('accounts:password-change'), self.password_data(
            user=self.doctor.pk, email=self.doctor.email, is_superuser='on'))
        self.assertRedirects(response, reverse('accounts:password-change-done'))
        self.owner.refresh_from_db()
        self.doctor.refresh_from_db()
        self.assertTrue(self.owner.check_password('Next-Forest!9Clouds'))
        self.assertTrue(self.doctor.check_password('Prior-Ocean!8Birds'))
        self.assertFalse(self.owner.is_superuser)
        self.assertEqual(int(self.client.session['_auth_user_id']), self.owner.pk)
        self.assertEqual(other.get(reverse('accounts:password-change')).status_code, 302)
        events = AuditEvent.objects.filter(action='account.password_changed')
        self.assertEqual(set(events.values_list('company_id', flat=True)), {self.alpha.pk, self.beta.pk})
        self.assertTrue(all(event.metadata == {} for event in events))
        self.assertEqual(len(mail.outbox), 0)

    def test_wrong_current_weak_or_mismatched_password_no_writes(self):
        for extra in ({'old_password': 'Wrong'}, {'new_password1': '123', 'new_password2': '123'},
                      {'new_password2': 'Different!8Clouds'}):
            response = self.client.post(reverse('accounts:password-change'), self.password_data(**extra))
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.context['form'].errors)
        self.owner.refresh_from_db()
        self.assertTrue(self.owner.check_password('Prior-Ocean!8Birds'))
        self.assertEqual(AuditEvent.objects.count(), 0)

    def test_patient_can_change_own_password_without_staff_access(self):
        self.login(self.patient_user)
        response = self.client.post(reverse('accounts:password-change'), self.password_data())
        self.assertEqual(response.status_code, 302)
        self.patient_user.refresh_from_db()
        self.assertTrue(self.patient_user.check_password('Next-Forest!9Clouds'))
        self.assertEqual(AuditEvent.objects.get().company, self.alpha)
        self.assertEqual(CompanyMembership.objects.filter(user=self.patient_user).count(), 0)

    def test_password_views_require_login_csrf_and_private_cache(self):
        self.client.logout()
        for name in ('password-change', 'password-change-done'):
            self.assertEqual(self.client.get(reverse(f'accounts:{name}')).status_code, 302)
        client = Client(enforce_csrf_checks=True)
        self.login(client=client)
        response = client.get(reverse('accounts:password-change'))
        self.assertIn('no-store', response['Cache-Control'])
        self.assertEqual(client.post(reverse('accounts:password-change'), self.password_data()).status_code, 403)
        self.assertEqual(AuditEvent.objects.count(), 0)
