"""Independent, retryable reminders just before an appointment starts."""

from datetime import timedelta
from io import StringIO
from queue import Queue
from threading import Barrier, Thread
from unittest import skipUnless
from unittest.mock import patch

from django.core import mail
from django.core.mail import EmailMultiAlternatives
from django.core.management import call_command
from django.db import connection, connections
from django.test import TransactionTestCase, override_settings
from django.utils import timezone

from practices.models import Company, CompanyMembership, Patient
from .models import Appointment, AppointmentStartReminder
from .notifications import _send_start_reminder, send_due_reminders
from .test_notifications import NotificationTestCase, SITE


class StartReminderTests(NotificationTestCase):
    def setUp(self):
        self.now = timezone.now().replace(second=0, microsecond=0)

    def appointment(self, **changes):
        values = dict(
            company=self.company, patient=self.patient, clinician=self.doctor,
            starts_at=self.now + timedelta(minutes=15), reminder_sent_at=self.now - timedelta(hours=5),
        )
        values.update(changes)
        return Appointment.objects.create(**values)

    def run_reminders(self, **kwargs):
        with patch('care.notifications.timezone.now', return_value=self.now):
            return send_due_reminders(**kwargs)

    def test_prior_confirmation_does_not_suppress_start_reminders_and_both_links_work(self):
        appointment = self.appointment()
        earlier_confirmation = appointment.reminder_sent_at
        self.assertEqual(self.run_reminders(), 1)
        sent = {email.to[0]: email for email in mail.outbox}
        self.assertEqual(set(sent), {self.patient_user.email, self.doctor.email})
        self.assertTrue(all(email.subject.startswith('Starting soon:') for email in sent.values()))
        self.assertIn(f'{SITE}/patient/appointments/{appointment.pk}/', sent[self.patient_user.email].body)
        self.assertIn(f'{SITE}/schedule/appointments/{appointment.pk}/', sent[self.doctor.email].body)
        self.assertEqual(set(AppointmentStartReminder.objects.values_list('recipient_id', flat=True)),
                         {self.patient_user.pk, self.doctor.pk})
        appointment.refresh_from_db()
        self.assertEqual(appointment.reminder_sent_at, earlier_confirmation)
        self.assertEqual(self.run_reminders(), 0)
        self.assertEqual(len(mail.outbox), 2)

    def test_day_before_reminder_is_preserved_and_separate_from_start_reminder(self):
        appointment = self.appointment(starts_at=self.now + timedelta(hours=24), reminder_sent_at=None)
        self.assertEqual(self.run_reminders(), 1)
        self.assertEqual(len(mail.outbox), 2)
        self.assertTrue(all(email.subject.startswith('Reminder:') for email in mail.outbox))
        self.assertFalse(AppointmentStartReminder.objects.exists())
        self.now = appointment.starts_at - timedelta(minutes=15)
        self.assertEqual(self.run_reminders(), 1)
        self.assertEqual(len(mail.outbox), 4)
        self.assertEqual(AppointmentStartReminder.objects.count(), 2)

    def test_imported_booking_inside_start_window_gets_only_one_reminder_per_person(self):
        self.appointment(reminder_sent_at=None)
        self.assertEqual(self.run_reminders(), 1)
        self.assertEqual(len(mail.outbox), 2)
        self.assertTrue(all(email.subject.startswith('Starting soon:') for email in mail.outbox))

    def test_early_started_past_and_cancelled_appointments_are_excluded(self):
        for changes in (
            {'starts_at': self.now + timedelta(minutes=15, seconds=1)},
            {'starts_at': self.now}, {'starts_at': self.now - timedelta(minutes=1)},
            {'status': Appointment.Status.CANCELLED},
        ):
            with self.subTest(changes=changes):
                self.appointment(**changes)
        self.assertEqual(self.run_reminders(), 0)
        self.assertEqual(len(mail.outbox), 0)
        self.assertFalse(AppointmentStartReminder.objects.exists())

    def test_partial_failure_retries_only_failed_recipient_and_marks_only_success(self):
        appointment = self.appointment()
        real_send = EmailMultiAlternatives.send

        def fail_patient(email, **kwargs):
            if email.to == [self.patient_user.email]:
                raise OSError('private smtp credentials')
            return real_send(email, **kwargs)

        with patch.object(EmailMultiAlternatives, 'send', fail_patient):
            with self.assertLogs('care.notifications', level='WARNING') as logs:
                self.assertEqual(self.run_reminders(), 1)
        self.assertNotIn('private smtp credentials', '\n'.join(logs.output))
        self.assertEqual(list(AppointmentStartReminder.objects.values_list('recipient_id', flat=True)), [self.doctor.pk])
        self.assertEqual([email.to for email in mail.outbox], [[self.doctor.email]])
        self.assertEqual(self.run_reminders(), 1)
        self.assertEqual([email.to for email in mail.outbox], [[self.doctor.email], [self.patient_user.email]])
        self.assertEqual(AppointmentStartReminder.objects.filter(appointment=appointment).count(), 2)

    def test_scheduled_command_retries_transient_failure_without_worker(self):
        self.appointment()
        real_send = EmailMultiAlternatives.send
        attempts = 0

        def fail_patient_once(email, **kwargs):
            nonlocal attempts
            if email.to == [self.patient_user.email]:
                attempts += 1
                if attempts == 1:
                    raise OSError('temporary smtp failure')
            return real_send(email, **kwargs)

        with patch.object(EmailMultiAlternatives, 'send', fail_patient_once), patch('care.notifications.time.sleep') as sleep:
            with patch('care.notifications.timezone.now', return_value=self.now), self.assertLogs('care.notifications', level='WARNING'):
                output = StringIO()
                call_command('send_appointment_reminders', stdout=output)
        sleep.assert_called_once_with(60)
        self.assertEqual(attempts, 2)
        self.assertEqual(len(mail.outbox), 2)
        self.assertEqual(AppointmentStartReminder.objects.count(), 2)
        self.assertEqual(output.getvalue(), 'Sent reminders for 1 appointment.\n')

    def test_all_failed_sends_remain_retryable_and_retries_are_bounded(self):
        self.appointment()
        with patch.object(EmailMultiAlternatives, 'send', side_effect=OSError('temporary')) as send:
            with patch('care.notifications.time.sleep') as sleep, self.assertLogs('care.notifications', level='WARNING'):
                self.assertEqual(self.run_reminders(retry_delays=(60, 120)), 0)
        self.assertEqual(send.call_count, 6)
        self.assertEqual([call.args for call in sleep.call_args_list], [(60,), (120,)])
        self.assertFalse(AppointmentStartReminder.objects.exists())
        self.assertEqual(self.run_reminders(), 1)

    def test_cancellation_or_move_before_locked_send_prevents_stale_email(self):
        appointment = self.appointment()
        for changes in ({'status': Appointment.Status.CANCELLED},
                        {'status': Appointment.Status.BOOKED, 'starts_at': self.now + timedelta(days=1)}):
            with self.subTest(changes=changes):
                Appointment.objects.filter(pk=appointment.pk).update(**changes)
                with patch('care.notifications.timezone.now', return_value=self.now):
                    self.assertEqual(_send_start_reminder(appointment.pk), (False, False))
        self.assertEqual(len(mail.outbox), 0)

    def test_rescheduling_gets_a_new_reminder_for_new_start(self):
        appointment = self.appointment()
        self.assertEqual(self.run_reminders(), 1)
        new_start = appointment.starts_at + timedelta(hours=1)
        Appointment.objects.filter(pk=appointment.pk).update(starts_at=new_start)
        self.assertEqual(self.run_reminders(), 0)
        self.now = new_start - timedelta(minutes=15)
        self.assertEqual(self.run_reminders(), 1)
        self.assertEqual(AppointmentStartReminder.objects.count(), 4)
        self.assertEqual(len(mail.outbox), 4)

    def test_reassigned_clinician_is_reloaded_and_patient_is_not_reminded_twice(self):
        appointment = self.appointment()
        self.assertEqual(self.run_reminders(), 1)
        Appointment.objects.filter(pk=appointment.pk).update(clinician=self.colleague)
        self.assertEqual(self.run_reminders(), 1)
        self.assertEqual([email.to for email in mail.outbox].count([self.patient_user.email]), 1)
        self.assertEqual([email.to for email in mail.outbox].count([self.doctor.email]), 1)
        self.assertEqual(mail.outbox[-1].to, [self.colleague.email])

    def test_retry_stops_when_appointment_is_cancelled(self):
        appointment = self.appointment()
        def cancel_while_waiting(_):
            Appointment.objects.filter(pk=appointment.pk).update(status=Appointment.Status.CANCELLED)
        with patch.object(EmailMultiAlternatives, 'send', side_effect=OSError('temporary')) as send:
            with patch('care.notifications.time.sleep', side_effect=cancel_while_waiting) as sleep:
                with self.assertLogs('care.notifications', level='WARNING'):
                    self.assertEqual(self.run_reminders(retry_delays=(60, 120)), 0)
        self.assertEqual(send.call_count, 2)
        sleep.assert_called_once_with(60)
        self.assertFalse(AppointmentStartReminder.objects.exists())

    def test_inactive_patient_and_staff_do_not_receive_start_reminders(self):
        self.appointment()
        CompanyMembership.objects.filter(company=self.company, user=self.doctor).update(is_active=False)
        self.patient_user.is_active = False
        self.patient_user.save(update_fields=['is_active'])
        self.assertEqual(self.run_reminders(), 0)
        self.assertEqual(len(mail.outbox), 0)

    @override_settings(EMAIL_ENABLED=False)
    def test_disabled_email_does_not_use_up_start_reminders(self):
        self.appointment()
        self.assertEqual(self.run_reminders(), 0)
        self.assertFalse(AppointmentStartReminder.objects.exists())


@skipUnless(connection.vendor == 'postgresql', 'Requires real PostgreSQL row locks')
@override_settings(MULTI_PRACTICE_ENABLED=True, EMAIL_ENABLED=True,
                   EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend')
class StartReminderConcurrencyTests(TransactionTestCase):
    def test_overlapping_runs_send_each_recipient_once(self):
        from django.contrib.auth import get_user_model

        company = Company.objects.create(name='Reminder race', slug='reminder-race')
        patient_user = get_user_model().objects.create_user(email='reminder-patient@example.test')
        doctor = get_user_model().objects.create_user(email='reminder-doctor@example.test')
        CompanyMembership.objects.create(company=company, user=doctor, role='doctor')
        patient = Patient.objects.create(company=company, user=patient_user, first_name='Pat', last_name='Example')
        appointment = Appointment.objects.create(company=company, patient=patient, clinician=doctor,
            starts_at=timezone.now() + timedelta(minutes=10), reminder_sent_at=timezone.now())
        gate = Barrier(2, timeout=10)
        outcomes = Queue()
        database_name = connection.settings_dict['NAME']

        def run():
            db = connections['default']
            try:
                db.close()
                with db.cursor() as cursor:
                    cursor.execute("SET statement_timeout = '10s'")
                    cursor.execute("SET lock_timeout = '5s'")
                    cursor.execute('SELECT current_database()')
                    self.assertEqual(cursor.fetchone()[0], database_name)
                gate.wait()
                outcomes.put(_send_start_reminder(appointment.pk))
            except Exception as error:
                outcomes.put(error)
            finally:
                db.close()

        workers = [Thread(target=run, daemon=True) for _ in range(2)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=15)
        self.assertFalse(any(worker.is_alive() for worker in workers))
        results = [outcomes.get_nowait() for _ in range(outcomes.qsize())]
        self.assertCountEqual(results, [(True, False), (False, False)])
        self.assertEqual(AppointmentStartReminder.objects.count(), 2)
        self.assertEqual(len(mail.outbox), 2)
