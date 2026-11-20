"""Monthly reports on Insights > Activity statements: Super Admin only, settings, Send now and the log."""

import csv
import io
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.core import mail
from django.urls import reverse

from care.activity_reports import send_activity_report
from care.models import ActivityReport, AuditEvent, PracticeSettings
from care.test_activity_reports import DUE, END, RECIPIENT, START, ActivityReportFixture
from practices.models import CompanyMembership
from .test_reporting import ReportingPortalFixture


class ActivityReportPortalTests(ReportingPortalFixture, ActivityReportFixture):
    def setUp(self):
        super().setUp()
        for target in ('care.activity_reports.reporting_today', 'portal.reporting_views.reporting_today'):
            patcher = patch(target, return_value=DUE)
            patcher.start()
            self.addCleanup(patcher.stop)

    def report(self):
        self.consult(status='completed')
        self.consult(status='booked')
        self.message(when=self.at(START + timedelta(days=2)))
        return send_activity_report(self.company, START, END)[0]

    def page(self):
        response = self.get('activity-statements')
        self.assertEqual(response.status_code, 200)
        return response

    def post_settings(self, **data):
        values = dict(activity_report_email='practice@example.test', activity_rate_initial='320.00', activity_rate_follow_up='180.00',
                      activity_rate_messages='12.50', activity_rate_review='15.00')
        values.update(data)
        values.setdefault('workflow_context', self.page().context['monthly']['settings_context'])
        return self.client.post(reverse('portal:activity-report-settings'), values)

    def post_send(self, **data):
        values = dict(period_end=END.isoformat())
        values.update(data)
        values.setdefault('workflow_context', self.page().context['monthly']['send_context'])
        return self.client.post(reverse('portal:activity-report-send'), values)

    def test_the_super_admin_sees_the_settings_send_now_and_the_log(self):
        report = self.report()
        response = self.page()
        self.assertContains(response, 'Monthly reports')
        self.assertContains(response, f'is emailed to {RECIPIENT}')
        self.assertContains(response, 'The next one, for 25 October – 24 November 2026, goes out on 25 November.')
        self.assertContains(response, 'R300.00 a new consult · R170.00 a follow-up consult · R10.00 a message answered · R10.00 a blood result reviewed')
        self.assertContains(response, 'id="activity-report-settings"')
        self.assertContains(response, 'Send the report again?')
        self.assertContains(response, '<input type="hidden" name="resend" value="1">', html=True)
        self.assertContains(response, reverse('portal:activity-report-detail', args=[report.pk]))
        self.assertContains(response, reverse('portal:activity-report-export', args=[report.pk]))
        self.assertContains(response, 'R610.00')
        self.assertContains(response, 'Automatically')
        self.assertNotContains(response, 'SAST')

    def test_before_any_report_send_now_offers_the_latest_period(self):
        response = self.page()
        self.assertContains(response, 'No reports yet.')
        self.assertContains(response, 'Send the report now?')
        self.assertNotContains(response, 'name="resend"')
        # It's the 25th and nothing has gone yet.
        self.assertContains(response, 'The report for 25 September – 24 October 2026 hasn’t gone out yet.')

    def test_only_the_super_admin_sees_opens_or_sends_reports(self):
        report = self.report()
        for actor in (self.admin, self.doctor, self.patient_user):
            self.login(actor)
            for name in ('activity-report-detail', 'activity-report-export'):
                self.assertEqual(self.get(name, report).status_code, 403, (actor.email, name))
            for name in ('activity-report-settings', 'activity-report-send'):
                self.assertEqual(self.client.post(reverse(f'portal:{name}'), {}).status_code, 403, (actor.email, name))
        self.assertEqual(len(mail.outbox), 1)  # Only the report itself.
        self.login(self.super_admin)
        self.assertEqual(self.get('activity-report-detail', report).status_code, 200)

    def test_another_practices_report_is_not_found(self):
        CompanyMembership.objects.create(company=self.beta, user=self.super_admin, role='super_admin')
        foreign = ActivityReport.objects.create(company=self.beta, period_start=START, period_end=END, sent_to='x@example.test')
        for name in ('activity-report-detail', 'activity-report-export'):
            self.assertEqual(self.get(name, foreign).status_code, 404)

    def test_settings_save_with_a_signed_form_and_are_audited(self):
        response = self.post_settings()
        self.assertRedirects(response, reverse('portal:activity-statements') + '#monthly-reports', fetch_redirect_response=False)
        row = PracticeSettings.objects.get(company=self.company)
        self.assertEqual((row.activity_report_email, row.activity_rate_initial, row.activity_rate_messages),
                         ('practice@example.test', Decimal('320.00'), Decimal('12.50')))
        event = AuditEvent.objects.get(action='activity_report.settings_changed')
        self.assertEqual(set(event.metadata['fields']), {'activity_report_email', 'activity_rate_initial', 'activity_rate_follow_up',
                                                         'activity_rate_messages', 'activity_rate_review'})
        # New statements start from these rates.
        self.assertEqual(self.get('activity-statement-create').context['form'].fields['initial'].initial, '320.00')

    def test_invalid_settings_reopen_the_dialog_and_save_nothing(self):
        for data in ({'activity_report_email': 'not-an-email'}, {'activity_rate_initial': '-1'},
                     {'activity_rate_review': '100000.01'}, {'activity_rate_follow_up': '1.234'}, {'workflow_context': 'forged'}):
            with self.subTest(data=data):
                response = self.post_settings(**data)
                self.assertEqual(response.status_code, 400)
                self.assertContains(response, 'id="activity-report-settings" aria-labelledby="activity-report-settings-title" open', status_code=400)
        row = PracticeSettings.objects.get(company=self.company)
        self.assertEqual((row.activity_report_email, row.activity_rate_initial), (RECIPIENT, Decimal('300.00')))
        self.assertFalse(AuditEvent.objects.filter(action='activity_report.settings_changed').exists())

    def test_send_now_sends_the_latest_period_once_and_again_only_when_confirmed(self):
        self.consult()
        response = self.post_send()
        report = ActivityReport.objects.get()
        self.assertRedirects(response, reverse('portal:activity-report-detail', args=[report.pk]), fetch_redirect_response=False)
        self.assertEqual((report.period_start, report.period_end, report.created_by), (START, END, self.super_admin))
        self.assertEqual(len(mail.outbox), 1)
        self.post_send()
        self.assertEqual(len(mail.outbox), 1)
        self.post_send(resend='1')
        self.assertEqual(len(mail.outbox), 2)
        self.assertEqual(ActivityReport.objects.count(), 1)

    def test_send_now_refuses_a_stale_page_or_form(self):
        for data in ({'period_end': (END - timedelta(days=31)).isoformat()}, {'workflow_context': 'forged'}):
            with self.subTest(data=data):
                response = self.post_send(**data)
                self.assertRedirects(response, reverse('portal:activity-statements') + '#monthly-reports', fetch_redirect_response=False)
        self.assertEqual(len(mail.outbox), 0)
        self.assertFalse(ActivityReport.objects.exists())

    def test_the_report_page_and_its_csv(self):
        report = self.report()
        response = self.get('activity-report-detail', report)
        for text in ('Monthly activity report', '25 September – 24 October 2026', 'Dr Anna Botha', 'Dr Ben Zulu',
                     '2 × R300.00', 'R600.00', '1 marked completed', 'R610.00', 'All doctors'):
            self.assertContains(response, text)
        self.assertNotContains(response, 'SAST')
        self.assertIn('no-store', response['Cache-Control'])
        response = self.get('activity-report-export', report)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Disposition'], 'attachment; filename="activity-report-2026-09-25-to-2026-10-24.csv"')
        self.assertIn('no-store', response['Cache-Control'])
        self.assertEqual(response['X-Content-Type-Options'], 'nosniff')
        rows = list(csv.reader(io.StringIO(response.content.decode())))
        self.assertEqual(rows[1][0], 'Dr Anna Botha')
        self.assertEqual(rows[-1][-1], '610.00')
        self.assertNotIn(self.patient.first_name, response.content.decode())

    def test_head_is_empty_and_views_are_logged_only_with_the_view_log_on(self):
        report = self.report()
        before = AuditEvent.objects.count()
        for name in ('activity-report-detail', 'activity-report-export'):
            response = self.client.head(reverse(f'portal:{name}', args=[report.pk]))
            self.assertEqual((response.status_code, response.content), (200, b''))
            self.get(name, report)
        self.assertEqual(AuditEvent.objects.count(), before)
        PracticeSettings.objects.filter(company=self.company).update(store_view_log=True)
        self.get('activity-report-export', report)
        self.assertTrue(AuditEvent.objects.filter(action='activity_report.exported').exists())

    def test_a_report_that_does_not_add_up_is_a_controlled_error(self):
        report = self.report()
        ActivityReport.objects.filter(pk=report.pk).update(total=Decimal('1.00'))
        for name in ('activity-report-detail', 'activity-report-export'):
            response = self.get(name, report)
            self.assertEqual(response.status_code, 400)
            self.assertNotIn('attachment', response.get('Content-Disposition', ''))
