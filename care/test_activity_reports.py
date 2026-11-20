"""The monthly doctor activity report: its period, what counts, the email and the daily job (care/activity_reports.py)."""

import csv
import io
from datetime import date, datetime, time, timedelta, timezone as datetime_timezone
from decimal import Decimal
from io import StringIO
from smtplib import SMTPException
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core import mail
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, override_settings

from practices.models import CompanyMembership, DemoDataSet, Patient
from .activity_reports import (current_period, current_period_start, csv_text, due_period, latest_period, period_label,
                               report_doctors, report_rows, send_activity_report)
from .models import ActivityReport, Appointment, AuditEvent, LabRequest, PracticeSettings
from .reporting import doctor_activity, practice_rates
from .test_reporting import ReportingFixture

# A period long past, so every booking in it has started, and the day its report is due.
START, END, DUE = date(2026, 9, 25), date(2026, 10, 24), date(2026, 10, 25)
RECIPIENT = 'drjczech@example.test'


class ActivityReportPeriodTests(SimpleTestCase):
    def test_the_period_turns_over_on_the_25th(self):
        self.assertEqual(latest_period(date(2026, 11, 24)), (date(2026, 9, 25), date(2026, 10, 24)))
        self.assertEqual(latest_period(date(2026, 11, 25)), (date(2026, 10, 25), date(2026, 11, 24)))
        self.assertEqual(latest_period(date(2026, 11, 30)), (date(2026, 10, 25), date(2026, 11, 24)))
        self.assertEqual(current_period_start(date(2026, 11, 24)), date(2026, 10, 25))
        self.assertEqual(current_period_start(date(2026, 11, 25)), date(2026, 11, 25))

    def test_january_wraps_back_to_december(self):
        self.assertEqual(latest_period(date(2027, 1, 25)), (date(2026, 12, 25), date(2027, 1, 24)))
        self.assertEqual(latest_period(date(2027, 1, 3)), (date(2026, 11, 25), date(2026, 12, 24)))
        self.assertEqual(latest_period(date(2027, 3, 1)), (date(2027, 1, 25), date(2027, 2, 24)))
        self.assertEqual(current_period(date(2026, 12, 25)), (date(2026, 12, 25), date(2027, 1, 24)))
        self.assertEqual(current_period(date(2026, 12, 24)), (date(2026, 11, 25), date(2026, 12, 24)))

    def test_labels_name_both_years_only_across_new_year_and_never_a_time_zone(self):
        self.assertEqual(period_label(date(2026, 10, 25), date(2026, 11, 24)), '25 October – 24 November 2026')
        self.assertEqual(period_label(date(2026, 12, 25), date(2027, 1, 24)), '25 December 2026 – 24 January 2027')

    def test_today_is_the_practice_day_not_the_servers(self):
        # 22:30 UTC on the 24th is already 00:30 on the 25th in Johannesburg.
        moment = datetime(2026, 11, 24, 22, 30, tzinfo=datetime_timezone.utc)
        with patch('django.utils.timezone.now', return_value=moment), override_settings(TIME_ZONE='UTC'):
            self.assertEqual(latest_period(), (date(2026, 10, 25), date(2026, 11, 24)))


class ActivityReportFixture(ReportingFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        users = get_user_model().objects
        users.filter(pk=cls.doctor.pk).update(title='DR', first_name='Anna', last_name='Botha')
        users.filter(pk=cls.other_doctor.pk).update(title='DR', first_name='Ben', last_name='Zulu')
        cls.doctor.refresh_from_db()
        cls.other_doctor.refresh_from_db()
        PracticeSettings.objects.filter(company=cls.company).update(activity_report_email=RECIPIENT)
        demo = DemoDataSet.objects.create(company=cls.company, created_by=cls.super_admin)
        cls.demo_patient = Patient.objects.create(company=cls.company, first_name='Synthetic', last_name='Demo',
                                                  assigned_doctor=cls.doctor, demo_data_set=demo)

    def consult(self, day=None, **extra):
        return self.appointment(starts_at=self.at(day or START + timedelta(days=3)), **extra)

    def reviewed_lab(self, when=None, doctor=None, patient=None, **extra):
        doctor = doctor or self.doctor
        return LabRequest.objects.create(company=self.company, patient=patient or self.patient, requested_by=doctor,
                                         panel_name='Six-month bloods', status='reviewed', reviewed_by=doctor,
                                         reviewed_at=when or self.at(START + timedelta(days=4)), **extra)

    def send(self, **extra):
        with patch('care.activity_reports.reporting_today', return_value=DUE):
            return send_activity_report(self.company, START, END, **extra)


class ActivityCountingTests(ActivityReportFixture):
    def test_consults_count_when_they_take_place_unless_cancelled_or_missed(self):
        self.consult(status='completed')
        self.consult(status='booked')  # Took place but wasn't marked completed: still counts.
        self.consult(status='cancelled')
        self.consult(status='no_show')
        self.consult(appointment_type='follow_up', status='completed')
        self.consult(appointment_type='review', status='booked')  # A six-month review is a follow-up consult.
        self.consult(appointment_type='ad_hoc', status='completed')
        # Booked before the period but held in it counts; booked in it but held after doesn't.
        early = self.consult()
        Appointment.objects.filter(pk=early.pk).update(created_at=self.at(START - timedelta(days=20)))
        self.consult(END + timedelta(days=1))
        self.consult(clinician=self.other_doctor)
        self.consult(patient=self.demo_patient)
        self.consult(company=self.beta, patient=self.beta_patient)
        counts, sources = doctor_activity(company=self.company, doctor=self.doctor, start=START, end=END)
        self.assertEqual(counts, {'initial': 3, 'follow_up': 3, 'messages': 0, 'review': 0,
                                  'initial_completed': 2, 'follow_up_completed': 2})
        self.assertEqual(len(sources['initial']), 3)

    def test_period_edges_are_whole_days_in_the_practice_time(self):
        lower = datetime.combine(START, time.min, tzinfo=self.at(START).tzinfo)
        upper = datetime.combine(END + timedelta(days=1), time.min, tzinfo=self.at(START).tzinfo)
        for moment in (lower, upper - timedelta(seconds=1), lower - timedelta(seconds=1), upper):
            self.appointment(starts_at=moment)
            self.reviewed_lab(when=moment)
            self.message(when=moment)
        counts, _ = doctor_activity(company=self.company, doctor=self.doctor, start=START, end=END)
        self.assertEqual((counts['initial'], counts['review'], counts['messages']), (2, 2, 2))

    def test_a_booking_counts_only_once_its_time_has_come(self):
        day = START + timedelta(days=3)
        started = self.appointment(starts_at=self.at(day, hour=10), status='booked')
        self.appointment(starts_at=self.at(day, hour=15), status='booked')
        with patch('django.utils.timezone.now', return_value=self.at(day, hour=12)):
            counts, sources = doctor_activity(company=self.company, doctor=self.doctor, start=START, end=END)
        self.assertEqual(sources['initial'], [started.pk])

    def test_blood_results_count_for_the_doctor_who_marked_them_reviewed_in_the_period(self):
        counted = self.reviewed_lab()
        self.reviewed_lab(when=self.at(START - timedelta(days=1)))
        self.reviewed_lab(doctor=self.other_doctor)
        self.reviewed_lab(patient=self.demo_patient)
        LabRequest.objects.create(company=self.company, patient=self.patient, requested_by=self.doctor, panel_name='Waiting')
        counts, sources = doctor_activity(company=self.company, doctor=self.doctor, start=START, end=END)
        self.assertEqual(counts['review'], 1)
        self.assertEqual(sources['review'], [counted.pk])

    def test_messages_answered_are_the_doctors_own_in_patient_conversations_without_demo_patients(self):
        self.message(when=self.at(START))
        self.message(when=self.at(START), sender=self.patient_user)
        self.message(when=self.at(START), patient=self.demo_patient)
        counts, _ = doctor_activity(company=self.company, doctor=self.doctor, start=START, end=END)
        self.assertEqual(counts['messages'], 1)


class ActivityReportTests(ActivityReportFixture):
    def activity(self):
        """Dr Botha: 2 new consults (1 completed), 1 follow-up (completed), 3 messages, 1 blood result reviewed."""
        self.consult(status='completed')
        self.consult(status='booked')
        self.consult(status='cancelled')
        self.consult(appointment_type='review', status='completed')
        for _ in range(3):
            self.message(when=self.at(START + timedelta(days=2)))
        self.reviewed_lab()

    def test_a_row_for_every_active_clinician_at_the_practice_rates_and_anyone_since_gone_who_did_something(self):
        self.activity()
        PracticeSettings.objects.filter(company=self.company).update(activity_rate_initial=Decimal('350.00'))
        CompanyMembership.objects.filter(company=self.company, user=self.super_admin).update(clinician_type='doctor')
        get_user_model().objects.filter(pk=self.super_admin.pk).update(first_name='Joshua', last_name='Czech', title='DR')
        gone = get_user_model().objects.create_user('gone-doctor@example.test', first_name='Carl', last_name='Gone')
        CompanyMembership.objects.create(company=self.company, user=gone, role='doctor', is_active=False)
        self.consult(clinician=gone)
        idle = get_user_model().objects.create_user('idle-doctor@example.test', first_name='Dan', last_name='Idle', is_active=False)
        CompanyMembership.objects.create(company=self.company, user=idle, role='doctor')
        rates = practice_rates(self.company)
        rows = report_rows(self.company, START, END, rates)
        self.assertEqual([row['doctor'] for row in rows], ['Dr Anna Botha', 'Dr Ben Zulu', 'Carl Gone', 'Dr Joshua Czech'])
        botha = rows[0]
        self.assertEqual(botha['counts'], {'initial': 2, 'follow_up': 1, 'messages': 3, 'review': 1,
                                           'initial_completed': 1, 'follow_up_completed': 1})
        self.assertEqual(botha['amounts'], {'initial': '700.00', 'follow_up': '170.00', 'messages': '30.00', 'review': '10.00'})
        self.assertEqual(botha['total'], '910.00')
        self.assertEqual(rows[1]['total'], '0.00')
        self.assertEqual(rows[2]['total'], '350.00')
        self.assertEqual(rates, {'initial': '350.00', 'follow_up': '170.00', 'messages': '10.00', 'review': '10.00'})

    def test_the_report_is_emailed_with_each_doctors_table_and_a_csv_and_logged(self):
        self.activity()
        report, sent = self.send()
        self.assertTrue(sent)
        self.assertEqual((report.period_start, report.period_end, report.sent_to), (START, END, RECIPIENT))
        self.assertIsNone(report.created_by)
        self.assertEqual(report.total, Decimal('810.00'))
        self.assertEqual(report.rates, {'initial': '300.00', 'follow_up': '170.00', 'messages': '10.00', 'review': '10.00'})
        self.assertEqual(len(mail.outbox), 1)
        email = mail.outbox[0]
        self.assertEqual(email.to, [RECIPIENT])
        self.assertEqual(email.subject, 'Doctor activity report, 25 September – 24 October 2026')
        html = email.alternatives[0][0]
        for body in (email.body, html):
            for text in ('Dr Anna Botha', 'Dr Ben Zulu', 'New consults', 'Follow-up consults', 'Messages answered',
                         'Blood results reviewed', '1 marked completed', 'R600.00', 'R810.00', 'Total payout',
                         'Total for all doctors', '25 September – 24 October 2026'):
                self.assertIn(text, body)
            self.assertNotIn('SAST', body)
            self.assertNotIn(self.patient.first_name, body)
        self.assertIn('2 × R300.00 = R600.00 (1 marked completed)', email.body)
        self.assertIn(f'/activity-statements/monthly/{report.pk}/', email.body)
        (name, content, mimetype), = email.attachments
        self.assertEqual((name, mimetype), ('activity-report-2026-09-25-to-2026-10-24.csv', 'text/csv'))
        rows = list(csv.reader(io.StringIO(content)))
        self.assertEqual(rows[0][:4], ['Doctor', 'New consults', 'New consults marked completed', 'New consults (R)'])
        self.assertEqual(rows[1], ['Dr Anna Botha', '2', '1', '600.00', '1', '1', '170.00', '3', '30.00', '1', '10.00', '810.00'])
        self.assertEqual(rows[2], ['Dr Ben Zulu', '0', '0', '0.00', '0', '0', '0.00', '0', '0.00', '0', '0.00', '0.00'])
        self.assertEqual(rows[-1], ['All doctors', '2', '1', '600.00', '1', '1', '170.00', '3', '30.00', '1', '10.00', '810.00'])
        event = AuditEvent.objects.get(action='activity_report.sent')
        self.assertEqual((event.company, event.actor, event.target_id), (self.company, None, str(report.pk)))

    def test_a_period_is_never_emailed_twice_unless_resent_and_a_resend_counts_afresh(self):
        self.activity()
        first, _ = self.send()
        again, sent = self.send()
        self.assertFalse(sent)
        self.assertEqual((again.pk, len(mail.outbox)), (first.pk, 1))
        Appointment.objects.filter(company=self.company, appointment_type='initial', status='booked').update(status='cancelled')
        resent, sent = self.send(actor=self.super_admin, resend=True)
        self.assertTrue(sent)
        self.assertEqual((resent.pk, resent.created_by, resent.total), (first.pk, self.super_admin, Decimal('510.00')))
        self.assertEqual(len(mail.outbox), 2)
        self.assertEqual(ActivityReport.objects.count(), 1)
        self.assertTrue(AuditEvent.objects.filter(action='activity_report.resent', actor=self.super_admin).exists())

    def test_a_failed_email_saves_nothing_so_the_next_run_tries_again(self):
        with patch('django.core.mail.EmailMultiAlternatives.send', side_effect=SMTPException('refused')), \
                self.assertRaises(SMTPException):
            self.send()
        self.assertFalse(ActivityReport.objects.exists())
        self.assertFalse(AuditEvent.objects.filter(action__startswith='activity_report').exists())
        self.assertTrue(self.send()[1])

    def test_refused_without_an_address_before_the_period_ends_for_odd_periods_and_for_anyone_but_the_super_admin(self):
        for actor in (self.admin, self.doctor, self.beta_admin):
            with self.subTest(actor=actor.email), self.assertRaises(PermissionDenied):
                self.send(actor=actor)
        with patch('care.activity_reports.reporting_today', return_value=END), self.assertRaises(ValidationError):
            send_activity_report(self.company, START, END)
        with patch('care.activity_reports.reporting_today', return_value=DUE), self.assertRaises(ValidationError):
            send_activity_report(self.company, START + timedelta(days=1), END)
        with override_settings(EMAIL_ENABLED=False), self.assertRaises(ValidationError):
            self.send()
        PracticeSettings.objects.filter(company=self.company).update(activity_report_email='')
        with self.assertRaises(ValidationError):
            self.send()
        self.assertFalse(ActivityReport.objects.exists())
        self.assertEqual(len(mail.outbox), 0)

    def test_a_saved_report_that_does_not_add_up_is_refused(self):
        self.activity()
        report, _ = self.send()
        self.assertEqual(len(report_doctors(report)), 2)
        rows = report.rows
        rows[0]['total'] = '9999.00'
        ActivityReport.objects.filter(pk=report.pk).update(rows=rows)
        report.refresh_from_db()
        with self.assertRaises(ValidationError):
            report_doctors(report)
        with self.assertRaises(ValidationError):
            csv_text(report)

    def test_a_doctors_name_cannot_become_a_spreadsheet_formula(self):
        get_user_model().objects.filter(pk=self.other_doctor.pk).update(title='', first_name='=HYPERLINK("x")', last_name='')
        report, _ = self.send()
        self.assertIn('"\'=HYPERLINK(""x"")"', csv_text(report))


class SendActivityReportsCommandTests(ActivityReportFixture):
    def run_command(self, today, *args):
        out, err = StringIO(), StringIO()
        with patch('care.management.commands.send_activity_reports.reporting_today', return_value=today), \
                patch('care.activity_reports.reporting_today', return_value=today), \
                override_settings(MULTI_PRACTICE_ENABLED=True):
            call_command('send_activity_reports', *args, stdout=out, stderr=err)
        return out.getvalue() + err.getvalue()

    def test_it_sends_from_the_25th_once_and_is_safe_to_run_every_day(self):
        self.consult()
        output = self.run_command(END)
        self.assertIn('Operations Alpha: no activity report due.', output)
        self.assertEqual(len(mail.outbox), 0)
        output = self.run_command(DUE)
        self.assertIn(f'Operations Alpha: activity report for 25 September – 24 October 2026 sent to {RECIPIENT}.', output)
        self.assertIn('Operations Beta: no activity report due.', output)  # No one to send it to.
        self.assertEqual(len(mail.outbox), 1)
        for day in (DUE, DUE + timedelta(days=1), date(2026, 10, 31)):
            self.run_command(day)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(ActivityReport.objects.get().created_by, None)

    def test_a_missed_25th_is_caught_up_that_month_but_never_after(self):
        self.run_command(date(2026, 10, 28))
        self.assertEqual(len(mail.outbox), 1)
        ActivityReport.objects.all().delete()
        self.run_command(date(2026, 11, 3))
        self.assertEqual(len(mail.outbox), 1)

    def test_dry_run_and_one_practice_only(self):
        output = self.run_command(DUE, '--dry-run')
        self.assertIn('would send the activity report for 25 September – 24 October 2026', output)
        self.assertEqual(len(mail.outbox), 0)
        self.assertFalse(ActivityReport.objects.exists())
        self.run_command(DUE, '--company', self.beta.slug)
        self.assertEqual(len(mail.outbox), 0)
        self.run_command(DUE, '--company', self.company.slug)
        self.assertEqual(len(mail.outbox), 1)
        with self.assertRaises(CommandError):
            self.run_command(DUE, '--company', 'no-such-practice')

    def test_without_email_or_a_recipient_nothing_is_sent(self):
        with override_settings(EMAIL_ENABLED=False):
            self.assertIn('Email is not set up', self.run_command(DUE))
        PracticeSettings.objects.filter(company=self.company).update(activity_report_email='')
        self.run_command(DUE)
        self.assertEqual(len(mail.outbox), 0)
        self.assertIsNone(due_period(self.company, DUE))

    def test_a_failed_send_is_reported_and_tried_again_the_next_day(self):
        with patch('django.core.mail.EmailMultiAlternatives.send', side_effect=SMTPException('refused')), \
                self.assertRaisesMessage(CommandError, '1 activity report not sent.'):
            self.run_command(DUE)
        self.assertFalse(ActivityReport.objects.exists())
        self.run_command(DUE + timedelta(days=1))
        self.assertEqual(len(mail.outbox), 1)
