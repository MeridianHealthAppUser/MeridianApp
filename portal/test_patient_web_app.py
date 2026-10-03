"""The patient web app's five sections show only what the record holds."""

import re
from datetime import datetime, time, timedelta
from decimal import Decimal
from pathlib import Path

from django.contrib.auth import get_user_model
from django.contrib.staticfiles import finders
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from care.availability import SAST
from care.messaging import add_participant
from care.models import (
    Appointment, AppointmentProposal, AuditEvent, MedicationProduct, MessageThread, PatientEvent, PatientMessage,
    PracticeSettings, TreatmentAuthorization, WeightEntry,
)
from care.scheduling import propose_appointment_time
from care.treatment import create_authorization, enroll_local_subscription
from practices.models import Company, CompanyMembership, Patient
from practices.services import ACTIVE_PATIENT_COMPANY_SESSION_KEY


@override_settings(MULTI_PRACTICE_ENABLED=True)
class PatientWebAppTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        users = get_user_model().objects
        cls.company = Company.objects.create(name='Web App Practice', slug='patient-web-app')
        cls.doctor = users.create_user(email='web-app-doctor@example.test', first_name='Sam', last_name='Doctor')
        CompanyMembership.objects.create(company=cls.company, user=cls.doctor, role=CompanyMembership.Role.DOCTOR)
        cls.user = users.create_user(email='web-app-patient@example.test', first_name='Nadia', last_name='Patient')
        cls.patient = Patient.objects.create(company=cls.company, user=cls.user, first_name='Nadia', last_name='Patient',
                                             assigned_doctor=cls.doctor)
        PracticeSettings.objects.create(company=cls.company)
        cls.product = MedicationProduct.objects.create(company=cls.company, name='Weekly pen', strength='1 mg', price=100)
        cls.thread = MessageThread.objects.create(company=cls.company, patient=cls.patient, subject='My dose',
                                                  opened_by=cls.user)
        add_participant(cls.thread, cls.doctor)

    def setUp(self):
        self.client.force_login(self.user)
        session = self.client.session
        session[ACTIVE_PATIENT_COMPANY_SESSION_KEY] = self.company.pk
        session.save()

    def page(self, name, *args, **params):
        return self.client.get(reverse(f'portal:{name}', args=args), params)

    def authorize(self, product=None, *, days=90):
        return create_authorization(
            company=self.company, patient=self.patient, actor=self.doctor, product=product or self.product,
            max_dose='1 mg weekly', quantity_per_cycle=4, starts_on=timezone.localdate(),
            expires_on=timezone.localdate() + timedelta(days=days), review_interval_days=90,
            instructions='Inject once a week.',
        )

    def book(self, **changes):
        values = dict(company=self.company, patient=self.patient, clinician=self.doctor, duration_minutes=15,
                      starts_at=timezone.now() + timedelta(days=3), appointment_type=Appointment.Type.FOLLOW_UP)
        values.update(changes)
        return Appointment.objects.create(**values)

    def incoming(self, count):
        for number in range(count):
            PatientMessage.objects.create(company=self.company, thread=self.thread, sender=self.doctor, body=f'Reply {number}')

    def test_messages_link_counts_unread_replies_until_the_thread_is_opened(self):
        self.incoming(2)
        PatientMessage.objects.create(company=self.company, thread=self.thread, sender=self.user, body='My own message')
        home = self.page('patient-dashboard')
        self.assertContains(home, '<span class="patient-navigation__badge" aria-hidden="true">2</span>', html=True)
        self.assertContains(home, ', 2 unread')
        opened = self.page('patient-messages', thread=self.thread.pk)
        self.assertEqual(opened.context['patient_unread_messages'], 0)
        self.assertNotContains(opened, 'patient-navigation__badge')
        self.assertNotContains(self.page('patient-dashboard'), 'patient-navigation__badge')
        self.incoming(12)
        self.assertContains(self.page('patient-treatment'), '<span class="patient-navigation__badge" aria-hidden="true">9+</span>', html=True)

    def test_my_messages_opens_with_no_conversation_until_one_is_chosen(self):
        self.incoming(1)
        response = self.page('patient-messages')
        self.assertIsNone(response.context['selected_thread'])
        self.assertContains(response, 'Choose a conversation')
        self.assertContains(response, '<span class="patient-navigation__badge" aria-hidden="true">1</span>', html=True)
        self.assertNotContains(response, f'action="{reverse("portal:patient-message-create", args=[self.thread.pk])}"')
        self.assertEqual(PatientMessage.objects.filter(read_at__isnull=True).count(), 1)
        self.assertFalse(AuditEvent.objects.filter(action='message.thread_viewed').exists())
        opened = self.page('patient-messages', thread=self.thread.pk)
        self.assertEqual(opened.context['selected_thread'].pk, self.thread.pk)
        self.assertFalse(PatientMessage.objects.filter(read_at__isnull=True).exists())

    def test_conversation_list_shows_who_sent_the_latest_message(self):
        self.incoming(1)
        listing = self.page('patient-messages')
        self.assertContains(listing, '<span class="sr-only">Latest message from </span>Sam Doctor', html=False)
        PatientMessage.objects.create(company=self.company, thread=self.thread, sender=self.user, body='Thank you')
        listing = self.page('patient-messages')
        self.assertContains(listing, '<span class="sr-only">Latest message from </span>You', html=False)
        self.assertContains(listing, 'Waiting for a reply')

    def test_my_treatment_tasks_come_from_the_record_and_clear_when_done(self):
        self.authorize(days=20)
        response = self.page('patient-treatment')
        titles = [task['title'] for task in response.context['tasks']]
        self.assertTrue(titles[0].startswith('Book a follow-up consult before'))
        self.assertTrue(response.context['tasks'][0]['urgent'])
        self.assertIn('Complete your medical profile', titles)
        self.assertIn('Log this week’s weight', titles)
        self.assertContains(response, 'Action needed')
        self.book()
        WeightEntry.objects.create(company=self.company, patient=self.patient, weight_kg='90.40', recorded_on=timezone.localdate())
        titles = [task['title'] for task in self.page('patient-treatment').context['tasks']]
        self.assertFalse(any(title.startswith('Book a follow-up consult') for title in titles))
        self.assertNotIn('Log this week’s weight', titles)
        self.assertIn('Complete your medical profile', titles)

    def test_my_treatment_shows_the_plan_and_keeps_history_collapsed(self):
        authorization = self.authorize()
        enroll_local_subscription(company=self.company, patient=self.patient, actor=self.user,
                                  authorization=authorization, confirm=True)
        response = self.page('patient-treatment')
        self.assertContains(response, 'My active subscription')
        self.assertContains(response, 'Inject once a week.')
        self.assertContains(response, 'Cancel my subscription')
        self.assertContains(response, 'Reference amount only. No payment is collected in this app.')
        self.assertContains(response, '<details class="patient-history" id="treatment-history">')
        self.assertContains(self.page('patient-treatment', page=1), '<details class="patient-history" id="treatment-history" open>')
        self.assertContains(response, 'Height')
        self.assertContains(response, 'subject=Change+to+my+medical+information')

    def test_my_medications_lists_authorised_open_and_previous_items_only(self):
        authorization = self.authorize()
        enroll_local_subscription(company=self.company, patient=self.patient, actor=self.user,
                                  authorization=authorization, confirm=True)
        supply = MedicationProduct.objects.create(company=self.company, name='Pen needles', price=50, requires_authorisation=False)
        earlier = MedicationProduct.objects.create(company=self.company, name='Earlier medicine', price=200)
        TreatmentAuthorization.objects.create(
            company=self.company, patient=self.patient, product=earlier, prescribed_by=self.doctor, max_dose='Old dose',
            quantity_per_cycle=2, starts_on=timezone.localdate() - timedelta(days=200),
            expires_on=timezone.localdate() - timedelta(days=100), review_interval_days=90,
            status=TreatmentAuthorization.Status.EXPIRED,
        )
        never = MedicationProduct.objects.create(company=self.company, name='Never prescribed medicine', price=300)
        response = self.page('patient-pharmacy')
        groups = response.context['product_groups']
        self.assertEqual([product.pk for product in groups['authorised']], [self.product.pk])
        self.assertEqual([product.pk for product in groups['open']], [supply.pk])
        self.assertEqual([product.pk for product in groups['previous']], [earlier.pk])
        self.assertIn(never.pk, [product.pk for product in response.context['products']])
        self.assertNotContains(response, 'Never prescribed medicine')
        # The subscribed medicine is managed on My Treatment rather than through the basket.
        self.assertContains(response, 'Manage subscription')
        self.assertNotContains(response, f'action="{reverse("portal:patient-basket-item", args=[self.product.pk])}"')
        self.assertContains(response, f'action="{reverse("portal:patient-basket-item", args=[supply.pk])}"')
        self.assertNotContains(response, f'action="{reverse("portal:patient-basket-item", args=[earlier.pk])}"')

    def test_suggested_time_can_be_answered_from_my_appointments(self):
        appointment = self.book()
        proposed = datetime.combine(timezone.localdate() + timedelta(days=5), time(10), tzinfo=SAST)
        proposal = propose_appointment_time(appointment=appointment, thread=self.thread, actor=self.doctor,
                                            actor_role='doctor', proposed_starts_at=proposed)
        response = self.page('patient-appointments')
        self.assertContains(response, 'Time suggested')
        self.assertContains(response, f'action="{reverse("portal:patient-appointment-respond", args=[proposal.pk])}"')
        answered = self.client.post(reverse('portal:patient-appointment-respond', args=[proposal.pk]),
                                    {'decision': 'decline', 'return_to': 'appointments'})
        self.assertRedirects(answered, reverse('portal:patient-appointments'), fetch_redirect_response=False)
        proposal.refresh_from_db()
        self.assertEqual(proposal.status, AppointmentProposal.Status.DECLINED)
        self.assertNotContains(self.page('patient-appointments'), 'Time suggested')

    def test_weight_logged_on_home_returns_to_home(self):
        home = self.page('patient-dashboard')
        data = {'patient_context': home.context['patient_context'], 'weight_kg': '91.20',
                'recorded_on': timezone.localdate().isoformat(), 'return_to': 'home'}
        saved = self.client.post(reverse('portal:patient-weight-add'), data)
        self.assertRedirects(saved, reverse('portal:patient-dashboard') + '#weight-progress', fetch_redirect_response=False)
        self.assertEqual(WeightEntry.objects.get(patient=self.patient).weight_kg, Decimal('91.20'))
        self.assertContains(self.page('patient-dashboard'), 'Latest 91.2 kg')
        duplicate = self.client.post(reverse('portal:patient-weight-add'), data)
        self.assertTemplateUsed(duplicate, 'portal/patient_dashboard.html')
        self.assertContains(duplicate, 'A weight has already been recorded for this date.')
        self.assertContains(duplicate, '<details class="patient-weight-log" id="weight-log" open>')

    def test_updates_are_grouped_by_month_with_type_links(self):
        for months_ago, title in ((0, 'Recent parcel'), (2, 'Earlier consult')):
            PatientEvent.objects.create(company=self.company, patient=self.patient, category=PatientEvent.Category.DELIVERY,
                                        title=title, occurred_at=timezone.now() - timedelta(days=31 * months_ago))
        response = self.page('patient-updates')
        self.assertContains(response, 'Everything that’s happened')
        self.assertContains(response, 'class="patient-updates__month"', count=2)
        self.assertContains(response, f'href="{reverse("portal:patient-updates")}?category=delivery"')
        filtered = self.page('patient-updates', category='message')
        self.assertNotContains(filtered, 'Recent parcel')
        self.assertContains(filtered, 'Nothing here yet')

    def test_brand_theme_is_loaded_last_on_patient_pages_only(self):
        for name in ('patient-dashboard', 'patient-treatment', 'patient-appointments', 'patient-messages', 'patient-pharmacy', 'patient-account'):
            with self.subTest(page=name):
                html = self.page(name).content.decode()
                self.assertIn('<body class="workspace-page patient-app">', html)
                self.assertGreater(html.index('css/patient_theme.css'), html.index('css/workspace_polish.css'))
        # People with only a patient record get the same look on their account pages.
        self.assertContains(self.client.get(reverse('accounts:password-change')), 'css/patient_theme.css')
        self.client.force_login(self.doctor)
        staff = self.client.get(reverse('accounts:password-change'))
        self.assertNotContains(staff, 'css/patient_theme.css')
        self.assertNotContains(staff, 'patient-app')

    def test_brand_theme_fonts_are_served_from_the_app(self):
        theme = Path(finders.find('css/patient_theme.css')).read_text()
        for colour in ('#48c1c9', '#46afcf', '#1599de', '#29698b', '#0097b2'):
            self.assertIn(colour, theme)
        fonts = re.findall(r"url\('\.\./(fonts/[^']+)'\)", theme)
        self.assertEqual(len(fonts), 2)
        for font in fonts:
            self.assertIsNotNone(finders.find(font), font)
        self.assertNotIn('fonts.googleapis.com', theme)
