"""Reporting pages preserve tenant boundaries and never process payments."""

import csv
import io
import uuid
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core import mail
from django.core.exceptions import ValidationError
from django.test import Client
from django.urls import reverse

from care.followups import append_follow_up
from care.models import AdministrativeFollowUp, AuditEvent, DoctorActivityStatement, Lead, PatientSubscription, Payment
from care.reporting import approve_activity_statement
from care.test_reporting import RATES, ReportingFixture
from practices.models import CompanyMembership
from practices.services import ACTIVE_COMPANY_SESSION_KEY


class ReportingPortalFixture(ReportingFixture):
    def setUp(self):
        self.login(self.super_admin)

    def select(self, company, client=None):
        client = client or self.client
        session = client.session
        session[ACTIVE_COMPANY_SESSION_KEY] = company.pk
        session.save()

    def login(self, actor, company=None, client=None):
        client = client or self.client
        client.force_login(actor)
        self.select(company or self.company, client)

    def url(self, name, record=None):
        return reverse(f'portal:{name}', args=[record.pk] if record else None)

    def get(self, name, record=None, **params):
        return self.client.get(self.url(name, record), params)

    def token(self, name, record=None):
        response = self.get(name, record)
        self.assertEqual(response.status_code, 200)
        return response.context['workflow_context']

    def prepare(self, **extra):
        data = dict(doctor=self.doctor.pk, start=self.start, end=self.end, confirm='on', **RATES)
        data.update(extra)
        data.setdefault('workflow_context', self.token('activity-statement-create'))
        return self.client.post(self.url('activity-statement-create'), data)


class MetricsPortalTests(ReportingPortalFixture):
    def test_separate_page_is_staff_only_current_practice_scoped_and_private(self):
        for actor in (self.super_admin, self.admin, self.doctor):
            self.login(actor)
            response = self.get('metrics', start=self.start, end=self.end)
            self.assertEqual(response.status_code, 200)
            self.assertTemplateUsed(response, 'portal/reporting_metrics.html')
            self.assertEqual(response.context['company_ids'], [self.company.pk])
            self.assertIn('no-store', response['Cache-Control'])
            self.assertNotContains(response, 'name="summary"')
            self.assertNotContains(response, 'name="body"')
        self.login(self.patient_user)
        self.assertEqual(self.get('metrics').status_code, 403)
        self.client.logout()
        self.assertEqual(self.get('metrics').status_code, 302)
        self.assertEqual(self.get('metrics-export').status_code, 302)

    def test_all_scope_combines_only_active_memberships_and_honours_per_practice_roles(self):
        CompanyMembership.objects.create(company=self.beta, user=self.super_admin, role='doctor')
        for company in (self.company, self.beta):
            lead = Lead.objects.create(company=company, first_name='Hidden', last_name='Identity', email='hidden@example.test')
            Lead.objects.filter(pk=lead.pk).update(created_at=self.at(self.start))
        response = self.get('metrics', start=self.start, end=self.end, scope='all', company=99999)
        self.assertEqual(set(response.context['company_ids']), {self.company.pk, self.beta.pk})
        self.assertEqual(dict(response.context['metrics'])['New enquiries in practices you administer'], 1)
        self.assertNotContains(response, 'hidden@example.test')
        CompanyMembership.objects.filter(company=self.beta, user=self.super_admin).update(is_active=False)
        response = self.get('metrics', start=self.start, end=self.end, scope='all')
        self.assertEqual(response.context['company_ids'], [self.company.pk])

    def test_invalid_and_extreme_date_filters_are_errors_not_server_errors_or_unscoped_reports(self):
        for params in ({'start': '9999-12-31', 'end': '9999-12-31'},
                       {'start': '0001-01-01', 'end': '0001-01-01'},
                       {'start': 'bad', 'end': self.end},
                       {'start': self.end, 'end': self.start},
                       {'start': self.start, 'end': self.end, 'scope': 'foreign'},
                       {'start': self.start, 'end': self.start + timedelta(days=367)}):
            with self.subTest(params=params):
                response = self.get('metrics', **params)
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.context['form'].errors)
                self.assertNotIn('metrics', response.context)
                export = self.get('metrics-export', **params)
                self.assertEqual(export.status_code, 400)
                self.assertNotIn('attachment', export.get('Content-Disposition', ''))

    def test_csv_contains_only_aggregates_escapes_formula_names_and_audits_permitted_practices(self):
        self.company.name = '=HYPERLINK("malicious")'
        self.company.save(update_fields=['name'])
        self.appointment()
        message = self.message()
        response = self.get('metrics-export', start=self.start, end=self.end)
        self.assertEqual(response.status_code, 200)
        self.assertIn('no-store', response['Cache-Control'])
        self.assertEqual(response['X-Content-Type-Options'], 'nosniff')
        self.assertIn('attachment;', response['Content-Disposition'])
        text = response.content.decode()
        rows = dict(list(csv.reader(io.StringIO(text)))[1:])
        self.assertEqual(rows['Scope'], "'" + self.company.name)
        self.assertEqual(rows['Completed appointments in period'], '1')
        self.assertNotIn(message.body, text)
        self.assertNotIn(f'{self.patient.first_name} {self.patient.last_name}', text)
        self.assertEqual(AuditEvent.objects.filter(action='metrics.exported', company=self.company).count(), 1)
        self.assertFalse(AuditEvent.objects.filter(action='metrics.exported', company=self.beta).exists())

    def test_head_exports_no_body_no_audit_and_metrics_post_is_disallowed(self):
        before = AuditEvent.objects.count()
        response = self.client.head(self.url('metrics-export'), {'start': self.start, 'end': self.end})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b'')
        self.assertEqual(AuditEvent.objects.count(), before)
        self.assertEqual(self.client.post(self.url('metrics'), {}).status_code, 405)
        self.assertEqual(self.client.post(self.url('metrics-export'), {}).status_code, 405)

    def test_revoked_membership_cannot_read_or_export_metrics(self):
        CompanyMembership.objects.filter(company=self.company, user=self.super_admin).update(is_active=False)
        self.assertEqual(self.get('metrics').status_code, 403)
        self.assertEqual(self.get('metrics-export').status_code, 403)


class StatementPortalTests(ReportingPortalFixture):
    def test_create_is_explicit_super_admin_action_with_no_payment_identity_or_email_side_effects(self):
        self.appointment()
        users = get_user_model().objects.count()
        response = self.prepare(company=self.beta.pk, approved_at='2020-01-01', amount='90000')
        statement = DoctorActivityStatement.objects.get()
        self.assertRedirects(response, self.url('activity-statement-detail', statement), fetch_redirect_response=False)
        self.assertEqual(statement.company, self.company)
        self.assertIsNone(statement.approved_at)
        self.assertEqual(statement.amount, Decimal('100.00'))
        self.assertEqual(get_user_model().objects.count(), users)
        self.assertFalse(Payment.objects.exists())
        self.assertEqual(len(mail.outbox), 0)

    def test_practice_admin_and_patients_have_no_statement_access_doctor_has_read_only_own(self):
        own = self.statement()
        other = self.statement(doctor=self.other_doctor)
        for actor in (self.admin, self.patient_user):
            self.login(actor)
            for name, record in (('activity-statements', None), ('activity-statement-detail', own),
                                 ('activity-statement-export', own), ('activity-statement-create', None)):
                self.assertEqual(self.get(name, record).status_code, 403)
        self.login(self.doctor)
        response = self.get('activity-statements')
        self.assertEqual(list(response.context['page_obj']), [own])
        self.assertFalse(self.get('activity-statement-detail', own).context['can_edit'])
        self.assertEqual(self.get('activity-statement-export', own).status_code, 200)
        self.assertEqual(self.get('activity-statement-detail', other).status_code, 404)
        self.assertEqual(self.get('activity-statement-export', other).status_code, 404)
        self.assertEqual(self.get('activity-statement-create').status_code, 403)
        self.assertEqual(self.client.post(self.url('activity-statement-detail', own), {'action': 'approve'}).status_code, 403)

    def test_foreign_statements_are_not_found_even_for_a_shared_super_admin(self):
        CompanyMembership.objects.create(company=self.beta, user=self.super_admin, role='super_admin')
        foreign = self.statement(company=self.beta)
        for name in ('activity-statement-detail', 'activity-statement-export'):
            self.assertEqual(self.get(name, foreign).status_code, 404)
        self.assertEqual(self.client.post(self.url('activity-statement-detail', foreign), {}).status_code, 404)
        self.select(self.beta)
        self.assertEqual(self.get('activity-statement-detail', foreign).status_code, 200)

    def test_missing_invalid_and_expired_creation_context_cannot_write(self):
        for invalid in ('', 'not-signed'):
            response = self.prepare(workflow_context=invalid)
            self.assertEqual(response.status_code, 400)
            self.assertEqual(response.context['workflow_context'], invalid)
        with patch('django.core.signing.time.time', return_value=1):
            expired = self.token('activity-statement-create')
        self.assertEqual(self.prepare(workflow_context=expired).status_code, 400)
        self.assertFalse(DoctorActivityStatement.objects.exists())

    def test_second_tab_practice_switch_and_other_actor_invalidate_context(self):
        token = self.token('activity-statement-create')
        CompanyMembership.objects.create(company=self.beta, user=self.super_admin, role='super_admin')
        self.select(self.beta)
        self.assertEqual(self.prepare(workflow_context=token).status_code, 400)
        self.login(self.admin)
        CompanyMembership.objects.filter(company=self.company, user=self.admin).update(role='super_admin')
        self.assertEqual(self.prepare(workflow_context=token).status_code, 400)
        self.assertFalse(DoctorActivityStatement.objects.exists())

    def test_csrf_is_enforced_on_create_and_approval(self):
        client = Client(enforce_csrf_checks=True)
        self.login(self.super_admin, client=client)
        response = client.get(self.url('activity-statement-create'))
        self.assertEqual(client.post(self.url('activity-statement-create'), {
            'workflow_context': response.context['workflow_context'], 'doctor': self.doctor.pk,
            'start': self.start, 'end': self.end, 'confirm': 'on', **RATES}).status_code, 403)
        statement = self.statement()
        response = client.get(self.url('activity-statement-detail', statement))
        self.assertEqual(client.post(self.url('activity-statement-detail', statement), {
            'workflow_context': response.context['workflow_context'], 'action': 'approve', 'confirm': 'on'}).status_code, 403)
        statement.refresh_from_db()
        self.assertIsNone(statement.approved_at)

    def test_validation_requires_confirmation_rates_and_nonoverlapping_past_period(self):
        for extra in ({'confirm': ''}, {'initial': ''}, {'initial': '-1'}, {'messages': 'NaN'},
                      {'end': self.today}, {'doctor': self.beta_admin.pk}, {'end': '9999-12-31'}):
            with self.subTest(extra=extra):
                self.assertEqual(self.prepare(**extra).status_code, 400)
        self.assertFalse(DoctorActivityStatement.objects.exists())
        self.assertEqual(self.prepare().status_code, 302)
        self.assertEqual(self.prepare().status_code, 400)
        self.assertEqual(DoctorActivityStatement.objects.count(), 1)

    def test_approval_requires_current_source_and_refresh_preserves_explicit_rates(self):
        statement = self.statement()
        self.appointment()
        url = self.url('activity-statement-detail', statement)
        response = self.client.post(url, {'workflow_context': self.token('activity-statement-detail', statement),
                                         'action': 'approve', 'confirm': 'on'})
        self.assertEqual(response.status_code, 400)
        self.assertContains(response, 'Source activity changed', status_code=400)
        response = self.client.post(url, {'workflow_context': self.token('activity-statement-detail', statement),
                                         'action': 'refresh', 'confirm': 'on', 'initial': '999'})
        self.assertEqual(response.status_code, 302)
        statement.refresh_from_db()
        self.assertEqual(statement.rates, RATES)
        self.assertEqual(statement.amount, Decimal('100.00'))
        response = self.client.post(url, {'workflow_context': self.token('activity-statement-detail', statement),
                                         'action': 'approve', 'confirm': 'on'})
        self.assertEqual(response.status_code, 302)
        statement.refresh_from_db()
        self.assertEqual(statement.approved_by, self.super_admin)
        self.assertFalse(self.get('activity-statement-detail', statement).context['can_edit'])

    def test_record_bound_and_stale_revision_tokens_cannot_approve_other_drafts(self):
        first = self.statement()
        second = self.statement(doctor=self.other_doctor)
        token = self.token('activity-statement-detail', first)
        response = self.client.post(self.url('activity-statement-detail', second),
                                    {'workflow_context': token, 'action': 'approve', 'confirm': 'on'})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.context['workflow_context'], token)
        first.save()
        response = self.client.post(self.url('activity-statement-detail', first),
                                    {'workflow_context': token, 'action': 'approve', 'confirm': 'on'})
        self.assertEqual(response.status_code, 400)
        self.assertFalse(DoctorActivityStatement.objects.filter(approved_at__isnull=False).exists())

    def test_approved_statement_stays_fixed_and_exports_no_source_patient_or_message_text(self):
        appointment = self.appointment()
        message = self.message()
        statement = self.statement()
        statement = approve_activity_statement(statement=statement, actor=self.super_admin,
            expected_updated=statement.updated_at.isoformat(), confirm=True)
        appointment.status = 'cancelled'
        appointment.save()
        response = self.get('activity-statement-export', statement)
        self.assertEqual(response.status_code, 200)
        rows = list(csv.reader(io.StringIO(response.content.decode())))
        self.assertIn(['Completed initial consultations', '1', '100.00', '100.00'], rows)
        self.assertIn(['Total', '', '', '102.00'], rows)
        self.assertNotIn(f'{self.patient.first_name} {self.patient.last_name}', response.content.decode())
        self.assertNotIn(message.body, response.content.decode())
        self.assertIn('no-store', response['Cache-Control'])
        self.assertEqual(response['X-Content-Type-Options'], 'nosniff')
        self.assertFalse(Payment.objects.exists())

    def test_statement_head_is_read_only_empty_and_private(self):
        statement = self.statement()
        before = AuditEvent.objects.count()
        for name in ('activity-statement-detail', 'activity-statement-export'):
            response = self.client.head(self.url(name, statement))
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.content, b'')
            self.assertIn('no-store', response['Cache-Control'])
        self.assertEqual(AuditEvent.objects.count(), before)

    def test_malformed_imported_snapshot_is_a_controlled_error_not_a_false_csv_or_server_error(self):
        statement = self.statement()
        token = self.token('activity-statement-detail', statement)
        for fields in ({'rates': {'initial': 'invalid'}}, {'rates': RATES, 'counts': ['bad']},
                       {'counts': {'initial': 0, 'review': 0, 'follow_up': 0, 'messages': 0}, 'amount': '999.00'}):
            with self.subTest(fields=fields):
                DoctorActivityStatement.objects.filter(pk=statement.pk).update(**fields)
                for name in ('activity-statement-detail', 'activity-statement-export'):
                    response = self.get(name, statement)
                    self.assertEqual(response.status_code, 400)
                    self.assertIn('no-store', response['Cache-Control'])
                    self.assertNotIn('attachment', response.get('Content-Disposition', ''))
        DoctorActivityStatement.objects.filter(pk=statement.pk).update(rates={'initial': 'invalid'})
        response = self.client.post(self.url('activity-statement-detail', statement),
            {'workflow_context': token, 'action': 'refresh', 'confirm': 'on'})
        self.assertEqual(response.status_code, 400)
        self.assertFalse(AuditEvent.objects.filter(action='activity_statement.exported').exists())
        statement.refresh_from_db()
        self.assertIsNone(statement.approved_at)

    def test_statement_list_paginates_and_filters_draft_approved_without_cross_practice_rows(self):
        records = []
        for number in range(21):
            day = self.end - timedelta(days=number)
            records.append(self.statement(start=day, end=day))
        page = self.get('activity-statements', status='draft')
        self.assertEqual(page.context['page_obj'].paginator.count, 21)
        self.assertEqual(len(page.context['page_obj']), 20)
        second = self.get('activity-statements', status='draft', page=2)
        self.assertEqual(list(second.context['page_obj']), [records[-1]])
        self.assertEqual(second.context['pagination_query'], 'status=draft')
        record = approve_activity_statement(statement=records[0], actor=self.super_admin,
            expected_updated=records[0].updated_at.isoformat(), confirm=True)
        self.assertEqual(list(self.get('activity-statements', status='approved').context['page_obj']), [record])
        self.assertEqual(self.get('activity-statements', status='invalid').context['page_obj'].paginator.count, 0)


class FollowUpImportSafetyTests(ReportingPortalFixture):
    """Adjacent follow-up review: legacy/imported foreign FKs must not leak."""

    def test_foreign_patient_import_cannot_appear_in_dropouts_or_accept_followup(self):
        malformed = PatientSubscription.objects.create(company=self.company, patient=self.beta_patient,
            status='cancelled', plan_name='Foreign patient plan', monthly_amount=1)
        response = self.get('dropouts')
        self.assertNotContains(response, 'Foreign patient plan')
        self.assertNotContains(response, f'{self.beta_patient.first_name} {self.beta_patient.last_name}')
        self.assertEqual(self.get('dropout-detail', malformed).status_code, 404)
        self.assertEqual(self.client.post(self.url('dropout-detail', malformed), {'note': 'Denied'}).status_code, 404)
        with self.assertRaises(ValidationError):
            append_follow_up(target=malformed, actor=self.admin, note='Denied', status='open',
                submission_key=uuid.uuid4(), expected_updated=malformed.updated_at.isoformat())
        self.assertFalse(AdministrativeFollowUp.objects.exists())

    def test_foreign_imported_followup_is_not_used_for_latest_state_defaults_or_history(self):
        lead = Lead.objects.create(company=self.company, first_name='Safe', last_name='Enquiry', email='safe@example.test')
        AdministrativeFollowUp.objects.create(company=self.beta, lead=lead, author=self.beta_admin,
            assigned_to=self.beta_admin, note='Foreign administrative text', status='done', submission_key=uuid.uuid4())
        response = self.get('lead-follow-up', lead)
        self.assertEqual(response.context['form'].initial['status'], 'open')
        self.assertIsNone(response.context['form'].initial['assigned_to'])
        self.assertEqual(response.context['page_obj'].paginator.count, 0)
        self.assertNotContains(response, 'Foreign administrative text')
        response = self.get('staff-leads', follow_up='none')
        self.assertContains(response, 'safe@example.test')
        self.assertNotContains(self.get('staff-leads', follow_up='done'), 'safe@example.test')

    def test_malformed_direct_contact_dates_and_targets_are_validation_errors_with_no_writes(self):
        lead = Lead.objects.create(company=self.company, first_name='Safe', last_name='Enquiry', email='safe@example.test')
        for date in ('not-a-date', self.at(self.today), 123):
            with self.subTest(date=date), self.assertRaises(ValidationError):
                append_follow_up(target=lead, actor=self.admin, note='Test', status='open', next_contact_on=date,
                    submission_key=uuid.uuid4(), expected_updated=lead.updated_at.isoformat())
        with self.assertRaises(ValidationError):
            append_follow_up(target=None, actor=self.admin, note='Test', status='open',
                submission_key=uuid.uuid4(), expected_updated='')
        self.assertFalse(AdministrativeFollowUp.objects.exists())
