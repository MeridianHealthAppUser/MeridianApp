from io import StringIO
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings
from django.urls import reverse

from care.models import AuditEvent
from practices.models import Company, CompanyMembership, Patient


@override_settings(MULTI_PRACTICE_ENABLED=False, SINGLE_PRACTICE_SLUG='meridian-health')
class BootstrapPracticeAdminTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = get_user_model().objects.create_superuser(
            email='operator@example.com', password='Existing-Account!2026',
            first_name='Existing', last_name='Operator',
        )

    def run_command(self, email=None):
        output = StringIO()
        call_command('bootstrap_practice_admin', email=email or self.admin.email, stdout=output)
        return output.getvalue()

    def test_creates_practice_and_explicit_membership_without_demo_data(self):
        account_before = get_user_model().objects.values().get(pk=self.admin.pk)
        output = self.run_command()

        company = Company.objects.get(slug='meridian-health')
        membership = CompanyMembership.objects.get(company=company, user=self.admin)
        self.assertEqual(company.name, 'Meridian Health')
        self.assertTrue(company.is_active)
        self.assertEqual(membership.role, CompanyMembership.Role.SUPER_ADMIN)
        self.assertTrue(membership.is_active)
        self.assertEqual(get_user_model().objects.values().get(pk=self.admin.pk), account_before)
        self.assertEqual(get_user_model().objects.count(), 1)
        self.assertFalse(Patient.objects.exists())
        self.assertIn('meridian-health', output)
        event = AuditEvent.objects.get(action='staff.membership_linked')
        self.assertEqual(event.company, company)
        self.assertEqual(event.actor, self.admin)
        self.assertEqual(event.target_id, str(membership.pk))
        self.assertEqual(event.metadata['source'], 'bootstrap_practice_admin')
        self.assertEqual(event.metadata['role'], CompanyMembership.Role.SUPER_ADMIN)

    def test_preserves_existing_practice_name_and_other_practice_access(self):
        company = Company.objects.create(name='Existing Meridian name', slug='meridian-health')
        other = Company.objects.create(name='Other practice', slug='other')
        other_membership = CompanyMembership.objects.create(
            company=other, user=self.admin, role=CompanyMembership.Role.DOCTOR,
        )

        self.run_command()

        company.refresh_from_db()
        other_membership.refresh_from_db()
        self.assertEqual(company.name, 'Existing Meridian name')
        self.assertEqual(other_membership.role, CompanyMembership.Role.DOCTOR)
        self.assertEqual(Company.objects.count(), 2)
        self.assertEqual(AuditEvent.objects.count(), 1)

    def test_rerunning_does_not_change_membership_or_duplicate_audits(self):
        self.run_command()
        membership_before = CompanyMembership.objects.values().get(user=self.admin)
        audit_count = AuditEvent.objects.count()

        output = self.run_command()

        self.assertIn('Already has', output)
        self.assertEqual(Company.objects.count(), 1)
        self.assertEqual(CompanyMembership.objects.count(), 1)
        self.assertEqual(CompanyMembership.objects.values().get(user=self.admin), membership_before)
        self.assertEqual(AuditEvent.objects.count(), audit_count)

    def test_missing_inactive_or_non_superuser_never_creates_practice(self):
        inactive = get_user_model().objects.create_superuser(
            'inactive@example.com', 'Test-password!2026', is_active=False,
        )
        regular = get_user_model().objects.create_user('regular@example.com', 'Test-password!2026')
        for email in ('missing@example.com', inactive.email, regular.email):
            with self.subTest(email=email), self.assertRaisesMessage(CommandError, 'active Django superuser'):
                self.run_command(email)
            self.assertFalse(Company.objects.exists())
            self.assertFalse(CompanyMembership.objects.exists())
            self.assertFalse(AuditEvent.objects.exists())

    def test_ambiguous_email_is_rejected_instead_of_selecting_an_identity(self):
        get_user_model().objects.create_superuser('OPERATOR@example.com', 'Different-password!2026')

        with self.assertRaisesMessage(CommandError, 'active Django superuser'):
            self.run_command()

        self.assertFalse(Company.objects.exists())

    def test_email_matching_is_case_insensitive_when_unambiguous(self):
        self.run_command(' OPERATOR@EXAMPLE.COM ')
        self.assertEqual(CompanyMembership.objects.get().user, self.admin)

    def test_inactive_practice_is_not_reactivated(self):
        company = Company.objects.create(name='Inactive Meridian', slug='meridian-health', is_active=False)

        with self.assertRaisesMessage(CommandError, 'practice is inactive'):
            self.run_command()

        company.refresh_from_db()
        self.assertFalse(company.is_active)
        self.assertFalse(CompanyMembership.objects.exists())
        self.assertFalse(AuditEvent.objects.exists())

    def test_existing_active_supported_role_is_preserved(self):
        company = Company.objects.create(name='Meridian Health', slug='meridian-health')
        membership = CompanyMembership.objects.create(
            company=company, user=self.admin, role=CompanyMembership.Role.DOCTOR,
        )
        for role in CompanyMembership.Role.values:
            CompanyMembership.objects.filter(pk=membership.pk).update(role=role)
            before = CompanyMembership.objects.values().get(pk=membership.pk)
            with self.subTest(role=role):
                output = self.run_command()
                self.assertIn('Already has', output)
                self.assertEqual(CompanyMembership.objects.values().get(pk=membership.pk), before)
                self.assertFalse(AuditEvent.objects.exists())

    def test_inactive_or_unsupported_existing_membership_is_never_replaced(self):
        company = Company.objects.create(name='Meridian Health', slug='meridian-health')
        membership = CompanyMembership.objects.create(
            company=company, user=self.admin, role=CompanyMembership.Role.DOCTOR,
        )
        for role, active in (('unsupported_role', True),
                             (CompanyMembership.Role.SUPER_ADMIN, False)):
            CompanyMembership.objects.filter(pk=membership.pk).update(role=role, is_active=active)
            with self.subTest(role=role, active=active), self.assertRaisesMessage(CommandError, 'will not replace'):
                self.run_command()
            membership.refresh_from_db()
            self.assertEqual((membership.role, membership.is_active), (role, active))
            self.assertFalse(AuditEvent.objects.exists())

    @override_settings(MULTI_PRACTICE_ENABLED=True)
    def test_multi_practice_mode_is_refused(self):
        with self.assertRaisesMessage(CommandError, 'single-practice mode'):
            self.run_command()
        self.assertFalse(Company.objects.exists())

    @override_settings(SINGLE_PRACTICE_SLUG='configured-practice')
    def test_only_explicitly_configured_slug_is_created(self):
        self.run_command()
        self.assertEqual(list(Company.objects.values_list('slug', flat=True)), ['configured-practice'])

    @override_settings(SINGLE_PRACTICE_SLUG='')
    def test_empty_configured_slug_is_refused(self):
        with self.assertRaisesMessage(CommandError, 'valid, non-empty practice slug'):
            self.run_command()
        self.assertFalse(Company.objects.exists())

    def test_audit_failure_rolls_back_practice_and_membership(self):
        with patch('practices.management.commands.bootstrap_practice_admin.record_audit',
                   side_effect=RuntimeError('Audit storage failed')):
            with self.assertRaisesMessage(RuntimeError, 'Audit storage failed'):
                self.run_command()
        self.assertFalse(Company.objects.exists())
        self.assertFalse(CompanyMembership.objects.exists())

    def test_admin_session_sign_in_requires_membership_then_reaches_workspace(self):
        self.client.force_login(self.admin)
        login_url = reverse('accounts:login')
        dashboard_url = reverse('portal:desktop-dashboard')
        response = self.client.get(login_url, follow=True)
        self.assertEqual(response.redirect_chain, [(dashboard_url, 302)])
        self.assertEqual(response.status_code, 403)

        self.run_command()

        response = self.client.get(login_url, follow=True)
        self.assertEqual(response.redirect_chain, [(dashboard_url, 302)])
        self.assertEqual(response.status_code, 200)
