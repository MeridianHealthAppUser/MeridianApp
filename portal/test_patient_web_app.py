"""The patient web app's five sections show only what the record holds."""

import re
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.staticfiles import finders
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from care.messaging import add_participant
from care.models import (
    Appointment, AuditEvent, MedicationProduct, MessageThread, PatientEvent, PatientMessage,
    PracticeSettings, TreatmentAuthorization, WeightEntry,
)
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
        self.assertContains(home, '<span class="message-unread-dot" data-live-unread="patient" aria-hidden="true">2</span>', html=True)
        self.assertContains(home, ', 2 unread')
        opened = self.page('patient-messages', thread=self.thread.pk)
        self.assertEqual(opened.context['patient_unread_messages'], 0)
        hidden = '<span class="message-unread-dot" data-live-unread="patient" hidden aria-hidden="true">0</span>'
        self.assertContains(opened, hidden, html=True)
        self.assertContains(self.page('patient-dashboard'), hidden, html=True)
        self.incoming(100)
        self.assertContains(self.page('patient-treatment'), '<span class="message-unread-dot" data-live-unread="patient" aria-hidden="true">99+</span>', html=True)

    def test_my_messages_opens_with_no_conversation_until_one_is_chosen(self):
        self.incoming(1)
        response = self.page('patient-messages')
        self.assertIsNone(response.context['selected_thread'])
        self.assertContains(response, 'Choose a conversation')
        self.assertContains(response, '<span class="message-unread-dot" data-live-unread="patient" aria-hidden="true">1</span>', html=True)
        self.assertNotContains(response, f'action="{reverse("portal:patient-message-create", args=[self.thread.pk])}"')
        self.assertEqual(PatientMessage.objects.filter(read_at__isnull=True).count(), 1)
        self.assertFalse(AuditEvent.objects.filter(action='message.thread_viewed').exists())
        opened = self.page('patient-messages', thread=self.thread.pk)
        self.assertEqual(opened.context['selected_thread'].pk, self.thread.pk)
        self.assertFalse(PatientMessage.objects.filter(read_at__isnull=True).exists())

    def test_my_messages_search_finds_message_text_subjects_and_clinicians(self):
        self.incoming(1)
        delivery = MessageThread.objects.create(company=self.company, patient=self.patient, subject='Delivery', opened_by=self.user)
        add_participant(delivery, self.doctor)
        PatientMessage.objects.create(company=self.company, thread=delivery, sender=self.user, body='The parcel was late')

        def found(search):
            return [thread.pk for thread in self.page('patient-messages', q=search).context['threads']]

        self.assertEqual(found('parcel'), [delivery.pk])  # message text
        self.assertEqual(found('DOSE'), [self.thread.pk])  # subject, any case
        self.assertEqual(set(found('sam doctor')), {self.thread.pk, delivery.pk})  # the clinician in both
        response = self.page('patient-messages', q='parcel')
        self.assertContains(response, '1 conversation matching “parcel”')
        self.assertContains(response, 'value="parcel"')
        self.assertContains(self.page('patient-messages', q='nothing here'), 'No conversations match “nothing here”.')
        # Searching opens nothing, so replies stay unread.
        self.assertTrue(PatientMessage.objects.filter(read_at__isnull=True, sender=self.doctor).exists())

    def test_conversation_list_rows_look_like_the_practice_consoles(self):
        self.incoming(1)
        listing = self.page('patient-messages')
        # Who it's with stays; the latest message sits beside its time today (or its date), as in the console.
        self.assertContains(listing, '<span class="patient-inbox-thread__with">With Sam Doctor</span>', html=True)
        sent = timezone.localtime(listing.context['threads'][0].last_activity)
        self.assertRegex(listing.content.decode(), rf'class="patient-inbox-thread__last">\s*<span class="patient-inbox-thread__preview">Reply 0</span>\s*<time [^>]*>{sent:%H:%M}</time>')
        self.assertNotContains(listing, 'patient-inbox-thread__meta')
        PatientMessage.objects.create(company=self.company, thread=self.thread, sender=self.user, body='Thank you')
        listing = self.page('patient-messages')
        # Your own last message reads "You: …"; there's no waiting-for-a-reply tag.
        self.assertContains(listing, '<strong>You:</strong> Thank you')
        self.assertNotContains(listing, 'Waiting for a reply')

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

    def test_my_treatment_shows_the_plan_without_history(self):
        authorization = self.authorize()
        enroll_local_subscription(company=self.company, patient=self.patient, actor=self.user,
                                  authorization=authorization, confirm=True)
        response = self.page('patient-treatment')
        self.assertContains(response, 'My active subscription')
        self.assertContains(response, 'Inject once a week.')
        self.assertContains(response, 'Cancel my subscription')
        # Payments are on Billing and payments (24 Oct 2026).
        self.assertContains(response, 'Billing and payments')
        self.assertNotContains(response, 'Treatment and delivery history')
        self.assertNotContains(response, 'id="treatment-history"')
        self.assertNotContains(response, 'id="medical-information-heading"')
        self.assertNotContains(response, 'Tell my doctor something has changed')
        medical_info = self.page('patient-medical-profile')
        self.assertContains(medical_info, 'Height')
        self.assertContains(medical_info, 'subject=Change+to+my+medical+information')

    def test_my_treatment_keeps_previous_prescriptions_below_subscription_without_a_shop(self):
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
        response = self.page('patient-treatment')
        self.assertContains(response, 'Previously prescribed')
        self.assertContains(response, earlier.name)
        self.assertContains(response, 'My active subscription')
        self.assertLess(response.content.index(b'My active subscription'), response.content.index(b'Previously prescribed'))
        self.assertNotContains(response, supply.name)
        self.assertNotContains(response, never.name)
        self.assertNotContains(response, 'Add to basket')
        self.assertNotContains(response, reverse('portal:patient-basket'))

    def test_previous_prescriptions_never_cross_patient_or_practice(self):
        other_user = get_user_model().objects.create_user(email='previous-other@example.test')
        other = Patient.objects.create(company=self.company, user=other_user, first_name='Other', last_name='Patient')
        beta = Company.objects.create(name='Previous Beta', slug='previous-beta')
        beta_patient = Patient.objects.create(company=beta, user=self.user, first_name='Nadia', last_name='Beta')
        for company, patient, name in ((self.company, other, 'Other patient medicine'),
                                       (beta, beta_patient, 'Other practice medicine')):
            product = MedicationProduct.objects.create(company=company, name=name, price=50)
            TreatmentAuthorization.objects.create(
                company=company, patient=patient, product=product, prescribed_by=self.doctor, max_dose='Private dose',
                quantity_per_cycle=1, starts_on=timezone.localdate() - timedelta(days=90),
                expires_on=timezone.localdate() - timedelta(days=1), review_interval_days=90,
                status=TreatmentAuthorization.Status.EXPIRED,
            )
        response = self.page('patient-treatment')
        self.assertNotContains(response, 'Other patient medicine')
        self.assertNotContains(response, 'Other practice medicine')
        self.assertNotContains(response, 'Previously prescribed')

    def test_previous_prescriptions_keep_latest_record_and_include_upcoming_and_inactive_products(self):
        self.authorize()
        today = timezone.localdate()
        earlier = MedicationProduct.objects.create(company=self.company, name='Archived medicine', price=50, is_active=False)
        future = MedicationProduct.objects.create(company=self.company, name='Future medicine', price=50)

        def recorded(product, *, starts, ends, dose, status=TreatmentAuthorization.Status.EXPIRED):
            return TreatmentAuthorization.objects.create(
                company=self.company, patient=self.patient, product=product, prescribed_by=self.doctor,
                max_dose=dose, quantity_per_cycle=1, starts_on=today + timedelta(days=starts),
                expires_on=today + timedelta(days=ends), review_interval_days=90, status=status,
            )

        recorded(self.product, starts=-200, ends=-100, dose='Older dose of current medicine')
        recorded(earlier, starts=-200, ends=-100, dose='Older archived dose')
        latest = recorded(earlier, starts=-90, ends=-1, dose='Most recent archived dose')
        upcoming = recorded(future, starts=10, ends=90, dose='Future dose', status=TreatmentAuthorization.Status.ACTIVE)
        response = self.page('patient-treatment')
        self.assertEqual({row.pk for row in response.context['previous_prescriptions']}, {latest.pk, upcoming.pk})
        self.assertContains(response, 'Archived medicine')
        self.assertContains(response, 'Most recent archived dose')
        self.assertNotContains(response, 'Older archived dose')
        self.assertNotContains(response, 'Older dose of current medicine')
        self.assertContains(response, 'Future medicine')
        from django.utils.dateformat import format as format_date

        self.assertContains(response, f'Starts {format_date(upcoming.starts_on, "j M Y")}')

    def test_previous_prescriptions_paginate_without_losing_subscription_context(self):
        authorization = self.authorize()
        enroll_local_subscription(company=self.company, patient=self.patient, actor=self.user,
                                  authorization=authorization, confirm=True)
        today = timezone.localdate()
        for number in range(13):
            product = MedicationProduct.objects.create(company=self.company, name=f'Previous medicine {number}', price=50)
            TreatmentAuthorization.objects.create(
                company=self.company, patient=self.patient, product=product, prescribed_by=self.doctor,
                max_dose='Previously recorded dose', quantity_per_cycle=1, starts_on=today - timedelta(days=90),
                expires_on=today - timedelta(days=1), review_interval_days=90, status=TreatmentAuthorization.Status.EXPIRED,
            )
        first = self.page('patient-treatment')
        second = self.page('patient-treatment', prescription_page=2)
        self.assertEqual(len(first.context['previous_prescriptions']), 12)
        self.assertEqual(len(second.context['previous_prescriptions']), 1)
        self.assertContains(first, 'prescription_page=2')
        self.assertContains(second, 'My active subscription')
        self.assertContains(second, 'Previously prescribed')

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
        for name in ('patient-dashboard', 'patient-treatment', 'patient-appointments', 'patient-messages', 'patient-medical-profile', 'patient-profile'):
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
        # Patient pages use the requested dark blue and pale cyan palette.
        for colour in ('#1D4A5E', '#A5E3F0'):
            self.assertIn(colour, theme)
        for colour in ('#29698b', '#0097b2', '#1599de', '#1072aa'):
            self.assertNotIn(f'--c-{colour[1:]}', theme)
        # Roboto is self-hosted and loaded on every page, the practice console's included.
        faces = Path(finders.find('css/fonts.css')).read_text()
        fonts = re.findall(r"url\('\.\./(fonts/[^']+)'\)", faces)
        self.assertEqual(len(fonts), 2)
        for font in fonts:
            self.assertIsNotNone(finders.find(font), font)
        for stylesheet in (theme, faces):
            self.assertNotIn('fonts.googleapis.com', stylesheet)
        base = Path(settings.BASE_DIR / 'templates' / 'base.html').read_text()
        self.assertIn("css/fonts.css", base)
