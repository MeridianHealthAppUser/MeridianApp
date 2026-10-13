"""Emails about new messages, appointments and new accounts: who gets them, how often, and what they say."""

from datetime import datetime, time, timedelta
from io import StringIO

from django.contrib.auth import get_user_model
from django.core import mail
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone

from practices.models import Company, CompanyMembership, Patient

from .appointment_lifecycle import book_staff_appointment, change_appointment_status
from .availability import SAST
from .messaging import add_participant, mark_read_by_staff
from .models import Appointment, DoctorWorkingPattern, MessageThread, PatientMessage
from .notifications import paused
from .patient_care import book_patient_appointment
from .services import post_patient_message
from .team_messaging import mark_team_thread_read, post_team_message, start_team_conversation

SITE = 'https://app.meridian.test'


@override_settings(MULTI_PRACTICE_ENABLED=True, EMAIL_ENABLED=True, SITE_URL=SITE,
                   EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend')
class NotificationTestCase(TestCase):
    """Nadia, with Dr Sam Marchant and Dr Joshua Czech in her conversation; Lindiwe is a practice admin."""

    @classmethod
    def setUpTestData(cls):
        users = get_user_model().objects
        cls.company = Company.objects.create(name='Meridian Health', slug='notify-practice')
        cls.doctor = users.create_user(email='sam@notify.test', first_name='Sam', last_name='Marchant', title='DR')
        cls.colleague = users.create_user(email='joshua@notify.test', first_name='Joshua', last_name='Czech', title='DR')
        cls.admin = users.create_user(email='lindiwe@notify.test', first_name='Lindiwe', last_name='Mahlangu')
        for user, role in ((cls.doctor, 'doctor'), (cls.colleague, 'doctor'), (cls.admin, 'practice_admin')):
            CompanyMembership.objects.create(company=cls.company, user=user, role=role)
        cls.patient_user = users.create_user(email='nadia@notify.test', first_name='Nadia', last_name='Mokoena')
        cls.patient = Patient.objects.create(company=cls.company, user=cls.patient_user, first_name='Nadia',
                                             last_name='Mokoena', assigned_doctor=cls.doctor)
        cls.thread = MessageThread.objects.create(company=cls.company, patient=cls.patient, subject='My dose',
                                                  opened_by=cls.patient_user)
        for user in (cls.doctor, cls.colleague):
            add_participant(cls.thread, user)

    def emails(self, action):
        """The emails sent once action's changes are saved, by recipient."""
        before = len(mail.outbox)
        with self.captureOnCommitCallbacks(execute=True):
            action()
        sent = mail.outbox[before:]
        self.assertEqual(len(sent), len({email.to[0] for email in sent}), 'Someone was emailed twice.')
        return {email.to[0]: email for email in sent}

    def html(self, email):
        content, mimetype = email.alternatives[0]
        self.assertEqual(mimetype, 'text/html')
        return content

    def post(self, sender, body='Is my dose changing?'):
        return self.emails(lambda: post_patient_message(thread=self.thread, sender=sender, body=body))

    @staticmethod
    def when(starts_at):
        starts = timezone.localtime(starts_at)
        return f'{starts:%A} {starts.day} {starts:%B} at {starts:%H:%M}'


class MessageNotificationTests(NotificationTestCase):
    def test_a_patient_message_emails_the_staff_in_it_but_never_the_message(self):
        sent = self.post(self.patient_user, body='My nausea is worse this week')
        self.assertEqual(set(sent), {'sam@notify.test', 'joshua@notify.test'})
        email = sent['sam@notify.test']
        self.assertEqual(email.subject, 'New message from Nadia Mokoena')
        self.assertEqual(email.from_email, 'Meridian Health <no-reply@meridianhealth.co.za>')
        html = self.html(email)
        for part in (email.body, html):
            self.assertIn('Hello Dr Marchant,', part)
            self.assertIn('You have a new message from Nadia Mokoena.', part)
            self.assertNotIn('nausea', part)
            self.assertNotIn('My dose', part)
            self.assertIn('Please do not reply to this email. If you need help, please email', part)
        self.assertIn(f'{SITE}/messages/?thread={self.thread.pk}', email.body)
        self.assertIn(f'<a href="{SITE}/messages/?thread={self.thread.pk}"', html)
        self.assertIn('>Read message</a>', html)
        # The platform's look: Roboto, the teal and the logo under the signature.
        self.assertIn("font-family:Roboto,", html)
        self.assertIn('background:#087e75', html)
        self.assertRegex(html, rf'<img src="{SITE}/static/images/meridian-logo[^"]*\.png" alt="Meridian Health - Medical Weight Loss"')

    def test_a_staff_message_emails_the_patient_and_the_other_clinicians(self):
        sent = self.post(self.doctor)
        self.assertEqual(set(sent), {'nadia@notify.test', 'joshua@notify.test'})
        patient = sent['nadia@notify.test']
        self.assertEqual(patient.subject, 'New message from Meridian Health')
        self.assertIn('Hello Nadia,', patient.body)
        self.assertIn('You have a new message from Dr Sam Marchant in the Meridian Health app.', patient.body)
        self.assertIn(f'{SITE}/patient/messages/?thread={self.thread.pk}#patient-conversation', patient.body)
        colleague = sent['joshua@notify.test']
        self.assertEqual(colleague.subject, 'New message in your conversation with Nadia Mokoena')
        self.assertIn('Dr Sam Marchant sent a new message in your conversation with Nadia Mokoena.', colleague.body)

    def test_one_email_per_conversation_until_they_have_read_it(self):
        self.assertEqual(set(self.post(self.patient_user)), {'sam@notify.test', 'joshua@notify.test'})
        self.assertEqual(self.post(self.patient_user), {})
        mark_read_by_staff(self.thread, self.doctor)
        # Sam has read it since, so the next message emails him again; Joshua still hasn't.
        self.assertEqual(set(self.post(self.patient_user)), {'sam@notify.test'})
        # The patient likewise, once they've opened the conversation.
        self.assertEqual(set(self.post(self.doctor)), {'nadia@notify.test'})
        self.assertEqual(set(self.post(self.doctor)), set())
        PatientMessage.objects.filter(thread=self.thread).exclude(sender=self.patient_user).update(read_at=timezone.now())
        self.assertEqual(set(self.post(self.doctor)), {'nadia@notify.test'})

    def test_staff_who_left_the_practice_and_patients_without_a_login_are_not_emailed(self):
        CompanyMembership.objects.filter(user=self.colleague).update(is_active=False)
        self.assertEqual(set(self.post(self.patient_user)), {'sam@notify.test'})
        get_user_model().objects.filter(pk=self.patient_user.pk).update(is_active=False)
        self.assertEqual(self.post(self.doctor), {})

    def test_a_team_message_emails_the_other_members_once_until_read(self):
        sent = self.emails(lambda: start_team_conversation(
            company=self.company, actor=self.doctor, members=[self.colleague, self.admin], subject='Rota', body='Who covers Friday?'))
        self.assertEqual(set(sent), {'joshua@notify.test', 'lindiwe@notify.test'})
        email = sent['lindiwe@notify.test']
        self.assertEqual(email.subject, 'New team message from Dr Sam Marchant')
        self.assertIn('Hello Lindiwe,', email.body)
        self.assertIn('Hello Dr Czech,', sent['joshua@notify.test'].body)
        self.assertIn('Dr Sam Marchant sent you a new team message.', email.body)
        self.assertNotIn('Friday', email.body + self.html(email))
        thread = self.doctor.team_threads.get()
        self.assertIn(f'{SITE}/messages/team/?thread={thread.pk}', email.body)
        mark_team_thread_read(thread, self.colleague)
        sent = self.emails(lambda: post_team_message(thread=thread, sender=self.colleague, body='I can'))
        # Sam hasn't been emailed about it yet; Lindiwe hasn't read the first one.
        self.assertEqual(set(sent), {'sam@notify.test'})

    def test_nothing_is_sent_without_email_set_up_or_while_paused(self):
        with override_settings(EMAIL_ENABLED=False):
            self.assertEqual(self.post(self.patient_user), {})
        with paused():
            self.assertEqual(self.post(self.patient_user), {})
        self.assertEqual(set(self.post(self.patient_user)), {'sam@notify.test', 'joshua@notify.test'})


class AppointmentNotificationTests(NotificationTestCase):
    def setUp(self):
        self.starts_at = timezone.localtime(timezone.now() + timedelta(days=3)).replace(hour=10, minute=0, second=0, microsecond=0)

    def book(self, actor, **changes):
        values = dict(company=self.company, patient=self.patient, actor=actor, clinician=self.doctor,
                      starts_at=self.starts_at, duration_minutes=30, appointment_type='follow_up')
        values.update(changes)
        booked = []
        sent = self.emails(lambda: booked.append(book_staff_appointment(**values)))
        return booked[0], sent

    def test_a_booking_by_the_practice_confirms_to_the_patient_and_the_doctor(self):
        appointment, sent = self.book(self.admin)
        self.assertEqual(set(sent), {'nadia@notify.test', 'sam@notify.test'})
        when = self.when(self.starts_at)
        starts = timezone.localtime(self.starts_at)
        patient = sent['nadia@notify.test']
        self.assertEqual(patient.subject, f'Appointment confirmed: {when}')
        self.assertIn('Your appointment is confirmed.', patient.body)
        self.assertIn('Appointment: Follow-up\n'
                      f'Date: {starts:%A} {starts.day} {starts:%B %Y}\n'
                      f'Time: 10:00 SAST · 30 minutes\n'
                      'With: Dr Sam Marchant', patient.body)
        self.assertIn(f'{SITE}/patient/appointments/', patient.body)
        html = self.html(patient)
        self.assertIn('>View my appointments</a>', html)
        self.assertIn('background:#eff8f6', html)
        doctor = sent['sam@notify.test']
        self.assertEqual(doctor.subject, f'New booking: Nadia Mokoena on {when}')
        self.assertIn('Hello Dr Marchant,\n\nA new appointment has been booked with you. Booked by Lindiwe Mahlangu.', doctor.body)
        self.assertIn('Patient: Nadia Mokoena\nAppointment: Follow-up', doctor.body)
        self.assertIn(f'{SITE}/schedule/?view=table&date={starts:%Y-%m-%d}', doctor.body)
        self.assertIn('>Open Calendar &amp; Bookings</a>', self.html(doctor))

    def test_a_doctor_booking_their_own_appointment_only_emails_the_patient(self):
        _, sent = self.book(self.doctor)
        self.assertEqual(set(sent), {'nadia@notify.test'})

    def test_a_patient_booking_confirms_to_them_and_their_doctor(self):
        for weekday in range(7):
            DoctorWorkingPattern.objects.create(company=self.company, clinician=self.doctor, weekday=weekday,
                                                is_working=True, starts_at=time(9), ends_at=time(12))
        starts_at = datetime.combine(timezone.localdate() + timedelta(days=3), time(9), tzinfo=SAST)
        sent = self.emails(lambda: book_patient_appointment(
            company=self.company, patient=self.patient, actor=self.patient_user, clinician_id=self.doctor.pk,
            starts_at=starts_at, appointment_type='follow_up'))
        self.assertEqual(set(sent), {'nadia@notify.test', 'sam@notify.test'})
        self.assertIn('Booked by Nadia Mokoena, the patient.', sent['sam@notify.test'].body)
        # Sending the form twice books once and emails once.
        self.assertEqual(self.emails(lambda: book_patient_appointment(
            company=self.company, patient=self.patient, actor=self.patient_user, clinician_id=self.doctor.pk,
            starts_at=starts_at, appointment_type='follow_up')), {})

    def cancel(self, appointment, actor, reason, **options):
        appointment.refresh_from_db()
        return self.emails(lambda: change_appointment_status(
            appointment=appointment, actor=actor, status='cancelled', expected_updated=appointment.updated_at.isoformat(),
            confirm=True, reason=reason, **options))

    def test_a_cancellation_by_the_practice_tells_the_patient_why_and_the_doctor_who(self):
        appointment, _ = self.book(self.admin)
        sent = self.cancel(appointment, self.admin, 'Dr Marchant is away that day. Please book another time.')
        self.assertEqual(set(sent), {'nadia@notify.test', 'sam@notify.test'})
        when = self.when(self.starts_at)
        patient = sent['nadia@notify.test']
        self.assertEqual(patient.subject, f'Appointment cancelled: {when}')
        html = self.html(patient)
        for part in (patient.body, html):
            self.assertIn('Your appointment has been cancelled by the practice.', part)
            self.assertIn('Dr Marchant is away that day. Please book another time.', part)
            self.assertIn("If you'd like a new time, you can book one or message your care team", part)
        self.assertIn('Appointment: Follow-up', patient.body)
        self.assertIn(f'{SITE}/patient/appointments/', patient.body)
        doctor = sent['sam@notify.test']
        self.assertEqual(doctor.subject, f'Appointment cancelled: Nadia Mokoena on {when}')
        self.assertIn('An appointment with you has been cancelled. Cancelled by Lindiwe Mahlangu.', doctor.body)
        self.assertIn('The time is free again in your calendar', doctor.body)
        self.assertNotIn('away that day', doctor.body + self.html(doctor))
        appointment.refresh_from_db()
        self.assertEqual(appointment.cancellation_reason, 'Dr Marchant is away that day. Please book another time.')

    def test_a_patient_cancelling_is_told_it_is_done_and_their_reason_is_never_emailed(self):
        appointment, _ = self.book(self.admin)
        sent = self.cancel(appointment, self.patient_user, 'Hospital admission for my gallbladder', patient_portal=True)
        self.assertEqual(set(sent), {'nadia@notify.test', 'sam@notify.test'})
        self.assertIn('Your appointment has been cancelled, as you asked.', sent['nadia@notify.test'].body)
        self.assertIn('Cancelled by Nadia Mokoena, the patient.', sent['sam@notify.test'].body)
        for email in sent.values():
            self.assertNotIn('gallbladder', email.body + self.html(email))

    def test_a_doctor_cancelling_their_own_appointment_only_emails_the_patient(self):
        appointment, _ = self.book(self.doctor)
        self.assertEqual(set(self.cancel(appointment, self.doctor, 'Clinic closed')), {'nadia@notify.test'})

    def remind(self):
        before = len(mail.outbox)
        out = StringIO()
        with self.captureOnCommitCallbacks(execute=True):
            call_command('send_appointment_reminders', stdout=out)
        return {email.to[0]: email for email in mail.outbox[before:]}, out.getvalue()

    def test_reminders_go_once_to_the_patient_and_doctor_the_day_before(self):
        soon = timezone.now() + timedelta(hours=23, minutes=50)
        appointment = Appointment.objects.create(company=self.company, patient=self.patient, clinician=self.doctor,
                                                 starts_at=soon, duration_minutes=15, appointment_type='review')
        Appointment.objects.create(company=self.company, patient=self.patient, clinician=self.doctor,
                                   starts_at=timezone.now() + timedelta(hours=26), duration_minutes=15)
        Appointment.objects.create(company=self.company, patient=self.patient, clinician=self.colleague,
                                   starts_at=soon, duration_minutes=15, status='cancelled')
        updated = appointment.updated_at
        sent, out = self.remind()
        self.assertEqual(out, 'Sent reminders for 1 appointment.\n')
        self.assertEqual(set(sent), {'nadia@notify.test', 'sam@notify.test'})
        when = self.when(soon)
        patient = sent['nadia@notify.test']
        self.assertEqual(patient.subject, f'Reminder: your appointment on {when}')
        self.assertIn('This is a reminder of your upcoming appointment.', patient.body)
        self.assertIn('Appointment: Six-month review', patient.body)
        self.assertIn(f'{SITE}/patient/appointments/{appointment.pk}/', patient.body)
        self.assertIn('>View appointment</a>', self.html(patient))
        doctor = sent['sam@notify.test']
        self.assertEqual(doctor.subject, f'Reminder: Nadia Mokoena on {when}')
        self.assertIn(f'{SITE}/schedule/appointments/{appointment.pk}/', doctor.body)
        # Never twice, and the reminder doesn't count as a change to the booking.
        self.assertEqual(self.remind()[0], {})
        appointment.refresh_from_db()
        self.assertEqual(appointment.updated_at, updated)

    def test_a_booking_within_a_day_has_its_confirmation_as_the_reminder(self):
        self.starts_at = timezone.now() + timedelta(hours=5)
        appointment, sent = self.book(self.admin)
        self.assertEqual(set(sent), {'nadia@notify.test', 'sam@notify.test'})
        self.assertEqual(self.remind()[0], {})
        appointment.refresh_from_db()
        self.assertIsNotNone(appointment.reminder_sent_at)

    @override_settings(EMAIL_ENABLED=False)
    def test_without_email_no_reminders_are_sent_or_used_up(self):
        appointment = Appointment.objects.create(company=self.company, patient=self.patient, clinician=self.doctor,
                                                 starts_at=timezone.now() + timedelta(hours=2), duration_minutes=15)
        sent, out = self.remind()
        self.assertEqual((sent, out), ({}, 'Email is not set up, so no reminders were sent.\n'))
        appointment.refresh_from_db()
        self.assertIsNone(appointment.reminder_sent_at)
