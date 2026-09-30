"""Admin-created accounts must immediately have their intended practice access."""

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from care.models import AuditEvent
from practices.models import Company, CompanyMembership, Patient


@override_settings(
    MULTI_PRACTICE_ENABLED=False,
    SINGLE_PRACTICE_SLUG='meridian-health',
    PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'],
)
class AdminUserCreationTests(TestCase):
    password = 'River!Mountain9Clouds'

    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name='Meridian Health', slug='meridian-health')
        cls.hidden = Company.objects.create(name='Other private practice', slug='other-private')
        cls.inactive = Company.objects.create(name='Archived practice', slug='archived', is_active=False)
        cls.superuser = get_user_model().objects.create_superuser(
            'technical-admin@example.test', cls.password,
        )
        cls.operator = get_user_model().objects.create_user(
            'account-operator@example.test', cls.password, is_staff=True,
        )
        cls.operator.user_permissions.add(*Permission.objects.filter(
            content_type__app_label='accounts', codename__in=['add_user', 'change_user', 'view_user'],
        ))

    def setUp(self):
        self.url = reverse('admin:accounts_user_add')
        self.client.force_login(self.superuser)

    def data(self, **changes):
        return {
            'email': 'new-account@example.test',
            'first_name': 'Taylor',
            'last_name': 'Example',
            'password1': self.password,
            'password2': self.password,
            'role': 'doctor',
            '_save': 'Save',
            **changes,
        }

    def counts(self):
        return tuple(model.objects.count() for model in (
            get_user_model(), CompanyMembership, Patient, AuditEvent,
        ))

    def grant(self, *codenames):
        self.operator.user_permissions.add(*Permission.objects.filter(
            content_type__app_label='practices', codename__in=codenames,
        ))

    def create(self, **changes):
        data = self.data(**changes)
        response = self.client.post(self.url, data)
        errors = response.context['adminform'].form.errors if response.status_code == 200 else ''
        self.assertEqual(response.status_code, 302, errors)
        return get_user_model().objects.get(email=data['email'])

    def assert_invalid(self, data, field=None):
        before = self.counts()
        response = self.client.post(self.url, data)
        self.assertEqual(response.status_code, 200)
        form = response.context['adminform'].form
        self.assertTrue(form.errors)
        if field:
            self.assertIn(field, form.errors)
        self.assertEqual(self.counts(), before)
        return response

    def assert_permission_rejected(self, data):
        before = self.counts()
        response = self.client.post(self.url, data)
        self.assertIn(response.status_code, (200, 403))
        if response.status_code == 200:
            self.assertTrue(response.context['adminform'].form.errors)
        self.assertEqual(self.counts(), before)

    def test_form_requires_role_and_names_without_selecting_a_practice(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        form = response.context['adminform'].form
        self.assertEqual({value for value, _ in form.fields['role'].choices if value},
                         {'doctor', 'practice_admin', 'super_admin', 'patient'})
        for name in ('email', 'first_name', 'last_name', 'password1', 'password2', 'role'):
            self.assertTrue(form.fields[name].required, name)
        self.assertIn('role', response.context['adminform'].fieldsets[0][1]['fields'])
        self.assertNotContains(response, 'name="practice"')
        self.assertNotContains(response, self.hidden.name)
        self.assertNotContains(response, self.inactive.name)
        self.assertEqual(self.counts(), (2, 0, 0, 0))

    def test_each_staff_role_creates_active_access_and_can_sign_into_desktop(self):
        for role in CompanyMembership.Role.values:
            with self.subTest(role=role):
                before = self.counts()
                user = self.create(email=f'{role}@example.test', role=role)
                self.assertEqual(self.counts(), tuple(a + b for a, b in zip(before, (1, 1, 0, 1))))
                membership = CompanyMembership.objects.get(user=user)
                self.assertEqual(membership.company, self.company)
                self.assertEqual(membership.role, role)
                self.assertTrue(membership.is_active)
                self.assertTrue(user.is_active)
                self.assertFalse(user.is_staff)
                self.assertFalse(user.is_superuser)
                self.assertEqual((user.first_name, user.last_name), ('Taylor', 'Example'))
                self.assertTrue(user.check_password(self.password))
                event = AuditEvent.objects.filter(action='staff.account_created').latest('pk')
                self.assertEqual(event.actor, self.superuser)
                self.assertEqual(event.company, self.company)
                self.assertNotIn(self.password, str(event.metadata))

                new_session = Client()
                login = new_session.post(reverse('accounts:login'), {
                    'email': user.email, 'password': self.password,
                })
                self.assertRedirects(login, reverse('portal:desktop-dashboard'))
                self.assertEqual(int(new_session.session['_auth_user_id']), user.pk)
                self.assertContains(self.client.get(reverse('admin:accounts_user_changelist')), user.email)

    def test_patient_creates_linked_active_record_and_signs_into_patient_portal(self):
        user = self.create(role='patient')
        self.assertEqual(self.counts(), (3, 0, 1, 1))
        patient = Patient.objects.get(user=user)
        self.assertEqual(patient.company, self.company)
        self.assertEqual((patient.first_name, patient.last_name), ('Taylor', 'Example'))
        self.assertTrue(patient.is_active)
        self.assertFalse(user.is_staff)
        self.assertFalse(user.is_superuser)
        event = AuditEvent.objects.get()
        self.assertEqual(event.action, 'patient.account_created')
        self.assertEqual(event.actor, self.superuser)
        self.assertEqual(event.company, self.company)
        self.assertContains(self.client.get(reverse('admin:accounts_user_changelist')), user.email)

        new_session = Client()
        response = new_session.post(reverse('accounts:login'), {
            'email': user.email, 'password': self.password,
        }, follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.redirect_chain[-1], (reverse('portal:patient-dashboard'), 302))
        self.assertEqual(int(new_session.session['_auth_user_id']), user.pk)

    def test_mixed_case_email_can_log_in_as_entered_and_cannot_be_duplicated(self):
        entered_email = 'Taylor.Example@EXAMPLE.TEST'
        response = self.client.post(self.url, self.data(email=entered_email))
        self.assertEqual(response.status_code, 302)
        user = get_user_model().objects.get(email='Taylor.Example@example.test')
        self.assertEqual(CompanyMembership.objects.get(user=user).company, self.company)

        new_session = Client()
        login = new_session.post(reverse('accounts:login'), {
            'email': entered_email, 'password': self.password,
        })
        self.assertRedirects(login, reverse('portal:desktop-dashboard'))
        self.assertEqual(int(new_session.session['_auth_user_id']), user.pk)

        self.assert_invalid(self.data(email=entered_email.lower(), role='patient'), 'email')
        self.assertFalse(Patient.objects.filter(user=user).exists())

    def test_missing_names_missing_role_and_unknown_role_write_nothing(self):
        for changes, field in (({'first_name': ''}, 'first_name'), ({'last_name': ''}, 'last_name'),
                               ({'role': ''}, 'role'), ({'role': 'root'}, 'role')):
            with self.subTest(changes=changes):
                self.assert_invalid(self.data(**changes), field)

    def test_duplicate_email_weak_and_mismatched_passwords_write_nothing(self):
        for changes, field in (
            ({'email': self.superuser.email}, 'email'),
            ({'email': self.superuser.email.upper()}, 'email'),
            ({'password1': '123', 'password2': '123'}, 'password2'),
            ({'password2': 'Other!Mountain9Clouds'}, 'password2'),
        ):
            with self.subTest(field=field, changes=changes):
                self.assert_invalid(self.data(**changes), field)

    def test_missing_configured_practice_reports_validation_error_without_orphan(self):
        with override_settings(SINGLE_PRACTICE_SLUG='not-configured'):
            self.assert_invalid(self.data())

    def test_inactive_configured_practice_reports_validation_error_without_orphan(self):
        Company.objects.filter(pk=self.company.pk).update(is_active=False)
        self.assert_invalid(self.data())

    def test_forged_practice_selection_cannot_link_a_hidden_practice(self):
        for role in ('doctor', 'patient'):
            with self.subTest(role=role):
                user = self.create(email=f'{role}@example.test', role=role, practice=self.hidden.pk)
                records = Patient.objects.filter(user=user) if role == 'patient' else CompanyMembership.objects.filter(user=user)
                self.assertEqual(list(records.values_list('company_id', flat=True)), [self.company.pk])
        self.assertFalse(AuditEvent.objects.filter(company=self.hidden).exists())

    def test_user_add_and_change_permissions_alone_cannot_grant_any_practice_role(self):
        self.client.force_login(self.operator)
        for role in ('doctor', 'practice_admin', 'super_admin', 'patient'):
            with self.subTest(role=role):
                self.assert_permission_rejected(self.data(role=role))

    def test_membership_permission_allows_staff_but_does_not_allow_patient_creation(self):
        self.grant('add_companymembership')
        self.client.force_login(self.operator)
        self.create(role='doctor')
        self.assert_permission_rejected(self.data(email='patient@example.test', role='patient'))

    def test_patient_permission_allows_patient_but_does_not_allow_staff_creation(self):
        self.grant('add_patient')
        self.client.force_login(self.operator)
        self.create(role='patient')
        for role in CompanyMembership.Role.values:
            with self.subTest(role=role):
                self.assert_permission_rejected(self.data(email='staff@example.test', role=role))

    def test_non_superuser_cannot_expose_or_forge_technical_admin_flags(self):
        self.grant('add_companymembership')
        self.client.force_login(self.operator)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'name="is_staff"')
        self.assertNotContains(response, 'name="is_superuser"')
        self.assert_invalid(self.data(is_staff='on', is_superuser='on'))

    def test_technical_superuser_can_explicitly_enable_advanced_admin_access(self):
        response = self.client.get(self.url)
        self.assertContains(response, 'name="is_staff"')
        self.assertContains(response, 'name="is_superuser"')
        user = self.create(is_staff='on', is_superuser='on')
        self.assertTrue(user.is_staff)
        self.assertTrue(user.is_superuser)
        self.assertEqual(CompanyMembership.objects.get(user=user).role, 'doctor')

    def test_audit_failure_rolls_back_account_and_practice_record_together(self):
        for role in ('doctor', 'patient'):
            with self.subTest(role=role):
                before = self.counts()
                with patch.object(AuditEvent.objects, 'create', side_effect=RuntimeError('Audit unavailable')):
                    with self.assertRaisesMessage(RuntimeError, 'Audit unavailable'):
                        self.client.post(self.url, self.data(role=role))
                self.assertEqual(self.counts(), before)

    def test_edit_existing_user_preserves_membership_and_patient_records(self):
        user = self.create()
        patient = Patient.objects.create(
            company=self.company, user=user, first_name='Recorded', last_name='Patient',
        )
        other_membership = CompanyMembership.objects.create(
            company=self.hidden, user=user, role='practice_admin',
        )
        membership = CompanyMembership.objects.get(user=user, company=self.company)
        before = self.counts()
        url = reverse('admin:accounts_user_change', args=[user.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        form = response.context['adminform'].form
        self.assertNotIn('role', form.fields)
        data = {
            'email': user.email, 'first_name': 'Updated', 'last_name': 'Display', 'is_active': 'on',
            'date_joined_0': user.date_joined.strftime('%Y-%m-%d'),
            'date_joined_1': user.date_joined.strftime('%H:%M:%S'),
            'role': 'super_admin', 'practice': self.hidden.pk, '_save': 'Save',
        }
        response = self.client.post(url, data)
        self.assertEqual(response.status_code, 302,
                         response.context['adminform'].form.errors if response.status_code == 200 else '')
        user.refresh_from_db()
        membership.refresh_from_db()
        other_membership.refresh_from_db()
        patient.refresh_from_db()
        self.assertEqual(user.first_name, 'Updated')
        self.assertEqual(membership.role, 'doctor')
        self.assertEqual(other_membership.role, 'practice_admin')
        self.assertEqual((patient.first_name, patient.last_name), ('Recorded', 'Patient'))
        self.assertEqual(self.counts(), before)

    @override_settings(MULTI_PRACTICE_ENABLED=True)
    def test_multi_practice_form_requires_an_active_practice_selection(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        practice = response.context['adminform'].form.fields['practice']
        self.assertTrue(practice.required)
        self.assertEqual(set(practice.queryset), {self.company, self.hidden})
        self.assert_invalid(self.data(), 'practice')
        self.assert_invalid(self.data(practice=self.inactive.pk), 'practice')
        user = self.create(practice=self.hidden.pk)
        self.assertEqual(CompanyMembership.objects.get(user=user).company, self.hidden)
        self.assertEqual(AuditEvent.objects.get().company, self.hidden)
