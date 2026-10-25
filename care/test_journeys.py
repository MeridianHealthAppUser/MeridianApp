"""The six email journeys: who is in each track, moving between them, sending once, unsubscribing, and the pages."""

from datetime import timedelta
from io import StringIO

from django.contrib.auth import get_user_model
from django.core import mail
from django.core.management import call_command
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from practices.models import CompanyMembership
from practices.services import ACTIVE_COMPANY_SESSION_KEY, ACTIVE_PATIENT_COMPANY_SESSION_KEY

from .journeys import CATCH_UP_DAYS, default_journeys, members, send_due, unsubscribe_token
from .models import Appointment, EmailDelivery, EmailJourney, Lead, PatientCommunicationPreference, PatientSubscription, SubscriptionCycle
from .test_billing import BillingFixture

Track = EmailJourney.Track


class JourneyTests(BillingFixture):
    def tracks(self, today=None):
        return {(member.track, member.entry_key) for member in members(self.company, today)}

    def switch_on(self, track, days=0):
        return EmailJourney.objects.create(company=self.company, track=track, delay_days=days, name=f'{track} {days}',
                                           subject_template='Hello {first_name}', body_template='Para one.\n\nPara two.',
                                           button='treatment', is_active=True)

    def test_the_practice_starts_with_every_track_switched_off(self):
        self.assertEqual(default_journeys(self.company), 11)
        self.assertEqual(default_journeys(self.company), 0)
        self.assertFalse(EmailJourney.objects.filter(is_active=True).exists())
        self.assertEqual({journey.track for journey in EmailJourney.objects.all()}, set(Track.values))

    def test_people_move_between_tracks_as_their_care_changes(self):
        lead = Lead.objects.create(company=self.company, first_name='Lee', last_name='Lead', email='lee@example.test',
                                   screening_status=Lead.ScreeningStatus.CLEARED)
        Lead.objects.create(company=self.company, first_name='Ref', last_name='Erred', email='ref@example.test',
                            screening_status=Lead.ScreeningStatus.REFERRED)
        tracks = self.tracks()
        self.assertIn((Track.SCREENED, f'lead-{lead.pk}'), tracks)
        self.assertEqual(sum(1 for track, _ in tracks if track == Track.SCREENED), 1)  # Not the referred enquiry.
        # The fixture's patient has a subscription: months 1 to 6, and month 5 onwards from day 120.
        self.assertIn((Track.EARLY, f'subscription-{self.plan.pk}'), tracks)
        self.assertNotIn(Track.ONGOING, {track for track, _ in tracks})
        later = self.tracks(self.today + timedelta(days=130))
        self.assertIn((Track.ONGOING, f'subscription-{self.plan.pk}'), later)
        # Cancelling moves them to Cancelled; a failed debit to Failed payment.
        PatientSubscription.objects.filter(pk=self.plan.pk).update(status='cancelled', cancelled_at=timezone.now())
        self.assertEqual({track for track, _ in self.tracks() if track != Track.SCREENED}, {Track.CANCELLED})
        PatientSubscription.objects.filter(pk=self.plan.pk).update(status='active', cancelled_at=None)
        cycle = SubscriptionCycle.objects.create(company=self.company, patient=self.patient, subscription=self.plan, cycle_number=1,
                                                 starts_on=self.today, due_on=self.today, amount='3295', attempts=1)
        self.assertIn((Track.PAYMENT_FAILED, f'cycle-{cycle.pk}'), self.tracks())
        cycle.status = 'paid'
        cycle.save()
        self.assertNotIn(Track.PAYMENT_FAILED, {track for track, _ in self.tracks()})

    def test_a_consultation_without_a_subscription_is_the_consulted_track(self):
        PatientSubscription.objects.all().delete()
        appointment = Appointment.objects.create(company=self.company, patient=self.patient, clinician=self.doctor,
                                                 appointment_type='initial', status='completed',
                                                 starts_at=timezone.now() - timedelta(days=1), duration_minutes=30)
        self.assertIn((Track.CONSULTED, f'consult-{appointment.pk}'), self.tracks())

    def test_due_emails_go_once_logged_against_the_patient_with_an_unsubscribe(self):
        journey = self.switch_on(Track.EARLY)
        self.assertEqual(send_due(self.company), 1)
        self.assertEqual(send_due(self.company), 0)
        message = mail.outbox[-1]
        self.assertEqual(message.subject, 'Hello Nadia')
        self.assertIn('Para two.', message.body)
        self.assertIn('List-Unsubscribe', message.extra_headers)
        delivery = EmailDelivery.objects.get(journey=journey)
        self.assertEqual((delivery.patient, delivery.entry_key, delivery.status), (self.patient, f'subscription-{self.plan.pk}', 'sent'))
        # A step whose day is long past isn't sent as a backlog.
        self.switch_on(Track.EARLY, days=1)
        self.assertEqual(send_due(self.company, today=self.today + timedelta(days=1 + CATCH_UP_DAYS + 1)), 0)

    def test_unsubscribing_stops_journeys_but_not_payment_reminders(self):
        self.switch_on(Track.EARLY)
        self.switch_on(Track.PAYMENT_FAILED)
        SubscriptionCycle.objects.create(company=self.company, patient=self.patient, subscription=self.plan, cycle_number=1,
                                         starts_on=self.today, due_on=self.today, amount='3295', attempts=1)
        url = reverse('portal:email-unsubscribe', args=[unsubscribe_token(self.patient)])
        self.assertContains(self.client.get(url), 'Unsubscribe from these emails?')
        self.assertContains(self.client.post(url), 'You’re unsubscribed')
        self.assertIsNotNone(PatientCommunicationPreference.objects.get(patient=self.patient).journey_emails_off_at)
        self.assertEqual(send_due(self.company), 1)
        self.assertEqual(EmailDelivery.objects.get().journey.track, Track.PAYMENT_FAILED)
        self.assertEqual(self.client.post(reverse('portal:email-unsubscribe', args=['nonsense'])).status_code, 400)

    def test_the_send_journey_emails_command(self):
        self.switch_on(Track.EARLY)
        out = StringIO()
        with override_settings(MULTI_PRACTICE_ENABLED=False, SINGLE_PRACTICE_SLUG='billing-practice'):
            call_command('send_journey_emails', stdout=out)
        self.assertIn('Billing Practice: 1 journey email sent.', out.getvalue())


class JourneyPageTests(BillingFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.super_admin = get_user_model().objects.create_user(email='journey-super@example.test')
        CompanyMembership.objects.create(company=cls.company, user=cls.super_admin, role='super_admin')

    def login(self, user):
        self.client.force_login(user)
        session = self.client.session
        session[ACTIVE_COMPANY_SESSION_KEY] = self.company.pk
        session[ACTIVE_PATIENT_COMPANY_SESSION_KEY] = self.company.pk
        session.save()

    def test_super_admins_edit_and_switch_on_journey_emails(self):
        self.login(self.admin)
        self.assertEqual(self.client.get(reverse('portal:email-journeys')).status_code, 403)
        self.login(self.super_admin)
        page = self.client.get(reverse('portal:email-journeys'))
        self.assertContains(page, 'Completed screening but did not book')
        journey = EmailJourney.objects.filter(track=Track.EARLY).first()
        edit = self.client.get(reverse('portal:email-journey-edit', args=[journey.pk]))
        self.assertContains(edit, 'Preview')
        response = self.client.post(reverse('portal:email-journey-edit', args=[journey.pk]), {
            'name': journey.name, 'track': journey.track, 'delay_days': 0, 'subject_template': 'Welcome, {first_name}',
            'body_template': 'Our own words.', 'button': 'weight', 'is_active': 'on'})
        self.assertRedirects(response, reverse('portal:email-journeys'), fetch_redirect_response=False)
        journey.refresh_from_db()
        self.assertTrue(journey.is_active)

    def test_the_communications_log_and_the_patients_preference(self):
        EmailDelivery.objects.create(company=self.company, patient=self.patient, recipient_email='p@example.test',
                                     kind='payment_failed', subject='We couldn’t collect your payment', status='sent')
        self.login(self.doctor)
        self.assertEqual(self.client.get(reverse('portal:email-log')).status_code, 403)
        self.login(self.admin)
        page = self.client.get(reverse('portal:email-log'), {'kind': 'payment'})
        self.assertContains(page, 'We couldn’t collect your payment')
        self.assertEqual(len(self.client.get(reverse('portal:email-log'), {'kind': 'journey'}).context['page_obj']), 0)
        self.login(self.patient_user)
        self.assertContains(self.client.get(reverse('portal:patient-profile')), 'Send me emails about my care')
        self.client.post(reverse('portal:patient-profile'), {'section': 'journeys'})
        self.assertIsNotNone(PatientCommunicationPreference.objects.get(patient=self.patient).journey_emails_off_at)
        self.client.post(reverse('portal:patient-profile'), {'section': 'journeys', 'journey_emails': 'on'})
        self.assertIsNone(PatientCommunicationPreference.objects.get(patient=self.patient).journey_emails_off_at)
