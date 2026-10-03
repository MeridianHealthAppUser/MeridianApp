"""The initial-consultation checkout converts a lead only with the test code."""

from datetime import time, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from care.models import (
    Appointment, AuditEvent, ClinicianAssignment, ConsentDocument, ConsentRecord, DoctorWorkingPattern, Invoice, Lead, Payment,
    PatientEvent, PracticeSettings,
)
from practices.models import Company, CompanyMembership, Patient


PASSWORD = 'Checkout-Test-2026!'
EMAIL = 'thandi@checkout.test'


@override_settings(DEBUG=True, MULTI_PRACTICE_ENABLED=False, SINGLE_PRACTICE_SLUG='meridian-health', CHECKOUT_TEST_CODE='devtest')
class QuestionnaireCheckoutTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name='Meridian Health', slug='meridian-health')
        PracticeSettings.objects.create(company=cls.company)
        for kind in (ConsentDocument.Kind.SERVICE, ConsentDocument.Kind.TELEHEALTH):
            ConsentDocument.objects.create(
                company=cls.company, kind=kind, version=f'{kind}-v1', title=f'{kind} notice',
                body=f'Current {kind} notice.', effective_from=timezone.localdate() - timedelta(days=1),
            )
        cls.doctor = get_user_model().objects.create_user(email='doctor@checkout.test', password=PASSWORD,
                                                          first_name='Sam', last_name='Doctor')
        CompanyMembership.objects.create(company=cls.company, user=cls.doctor, role=CompanyMembership.Role.DOCTOR)
        for weekday in range(7):
            DoctorWorkingPattern.objects.create(company=cls.company, clinician=cls.doctor, weekday=weekday,
                                                is_working=True, starts_at=time(9), ends_at=time(12))
        cls.day = timezone.localdate() + timedelta(days=1)
        cls.url = reverse('portal:questionnaire-checkout')

    def submit_enquiry(self, client=None, **changes):
        client = client or self.client
        page = client.get(reverse('portal:questionnaire'))
        data = {
            'practice': self.company.pk, 'submission_token': page.context['form']['submission_token'].value(),
            'first_name': 'Thandi', 'last_name': 'Checkout', 'email': EMAIL, 'phone': '082 000 0000',
            'id_number': '9001015009087', 'height_cm': '168', 'weight_kg': '94', 'adult': 'yes',
            'weight_related_condition': 'no', 'pregnancy': 'no', 'thyroid_history': 'no', 'pancreatitis': 'no',
            'health_context': 'None', 'service_consent': 'on',
        }
        data.update(changes)
        response = client.post(reverse('portal:questionnaire'), data)
        self.assertRedirects(response, reverse('portal:questionnaire-result'), fetch_redirect_response=False)
        return Lead.objects.get(email=data['email'])

    def checkout(self, client=None):
        return (client or self.client).get(self.url, {'date': self.day.isoformat()})

    def pay_data(self, page, **changes):
        data = {'action': 'pay', 'date': self.day.isoformat(), 'slot': page.context['slots'][0]['token'],
                'discount_code': 'devtest', 'password1': PASSWORD, 'password2': PASSWORD}
        data.update(changes)
        return data

    def pay(self, client=None, **changes):
        client = client or self.client
        page = self.checkout(client)
        return client.post(self.url, self.pay_data(page, **changes)), page

    def counts(self):
        return {model._meta.label: model.objects.count()
                for model in (get_user_model(), Patient, Appointment, Invoice, Payment)}

    def test_test_code_converts_the_lead_into_a_booked_signed_in_patient(self):
        lead = self.submit_enquiry()
        response, page = self.pay()
        self.assertRedirects(response, reverse('portal:patient-medical-profile'), fetch_redirect_response=False)

        user = get_user_model().objects.get(email=EMAIL)
        self.assertTrue(user.check_password(PASSWORD))
        self.assertFalse(user.is_staff or user.is_superuser)
        patient = Patient.objects.get(user=user)
        self.assertEqual((patient.company, patient.first_name, patient.last_name, patient.id_number),
                         (self.company, 'Thandi', 'Checkout', '9001015009087'))
        appointment = Appointment.objects.get(patient=patient)
        self.assertEqual(
            (appointment.clinician, appointment.starts_at, appointment.duration_minutes,
             appointment.appointment_type, appointment.status),
            (self.doctor, page.context['slots'][0]['starts_at'], 30, Appointment.Type.INITIAL, Appointment.Status.BOOKED),
        )
        self.assertEqual(patient.assigned_doctor, self.doctor)
        self.assertEqual(list(ClinicianAssignment.objects.filter(patient=patient, ended_at__isnull=True).values_list('clinician', flat=True)), [self.doctor.pk])
        invoice = Invoice.objects.get(patient=patient)
        self.assertEqual((invoice.status, invoice.subtotal, invoice.total), (Invoice.Status.PAID, Decimal('630'), Decimal('0')))
        self.assertEqual(list(invoice.lines.values_list('line_total', flat=True)), [Decimal('630'), Decimal('-630')])
        payment = Payment.objects.get(patient=patient)
        self.assertEqual(
            (payment.status, payment.amount, payment.appointment, payment.invoice, payment.provider_reference),
            (Payment.Status.PAID, Decimal('0'), appointment, invoice, 'test-code:devtest'),
        )
        self.assertIsNotNone(payment.paid_at)
        lead.refresh_from_db()
        self.assertEqual((lead.converted_patient, lead.stage), (patient, Lead.Stage.CONVERTED))
        self.assertEqual(ConsentRecord.objects.filter(lead=lead, patient=patient, user=user).count(), 2)
        self.assertEqual(set(PatientEvent.objects.filter(patient=patient).values_list('category', flat=True)),
                         {PatientEvent.Category.APPOINTMENT, PatientEvent.Category.PAYMENT})
        self.assertEqual(set(AuditEvent.objects.filter(patient=patient).values_list('action', flat=True)),
                         {'patient.account_created', 'appointment.patient_booked', 'patient.doctor_assigned', 'lead.converted'})
        self.assertEqual(AuditEvent.objects.get(patient=patient, action='patient.doctor_assigned').metadata,
                         {'previous_doctor_id': None, 'doctor_id': self.doctor.pk, 'source': 'questionnaire.checkout',
                          'appointment_id': appointment.pk})

        self.assertEqual(int(self.client.session['_auth_user_id']), user.pk)
        profile = self.client.get(reverse('portal:patient-medical-profile'))
        self.assertContains(profile, 'Your initial consultation with Sam Doctor is booked')
        self.assertContains(self.client.get(reverse('portal:patient-appointments')), 'Initial consultation')
        for name in ('questionnaire-checkout', 'questionnaire-result'):
            self.assertRedirects(self.client.get(reverse(f'portal:{name}')), reverse('portal:patient-dashboard'),
                                 fetch_redirect_response=False)

    def test_pay_now_is_disabled_until_the_test_code_is_applied(self):
        self.submit_enquiry()
        page = self.checkout()
        self.assertTrue(page.context['slots'])
        self.assertFalse(page.context['pay_enabled'])
        self.assertContains(page, 'value="pay" disabled')
        before = self.counts()
        applied = self.client.post(self.url, {'action': 'apply', 'date': self.day.isoformat(), 'discount_code': ' DevTest '})
        self.assertTrue(applied.context['code_applied'])
        self.assertEqual(applied.context['quote'].total, 0)
        self.assertTrue(applied.context['pay_enabled'])
        self.assertContains(applied, 'Pay R0.00 and book')
        self.assertEqual(self.counts(), before)

    def test_card_and_eft_details_are_never_submitted(self):
        self.submit_enquiry()
        page = self.checkout()
        self.assertContains(page, 'id="checkout-card-number"')
        self.assertNotContains(page, 'name="card')
        self.assertContains(page, 'Card and EFT payments are not connected yet')

    def test_payment_without_the_test_code_creates_nothing(self):
        lead = self.submit_enquiry()
        before = self.counts()
        for code in ('', 'testpay', 'devtest2'):
            with self.subTest(code=code):
                response, _ = self.pay(discount_code=code)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(self.counts(), before)
        lead.refresh_from_db()
        self.assertIsNone(lead.converted_patient_id)
        self.assertNotIn('_auth_user_id', self.client.session)

    def test_an_unset_test_code_disables_the_discount(self):
        self.submit_enquiry()
        before = self.counts()
        with self.settings(CHECKOUT_TEST_CODE=''):
            response, _ = self.pay()
        self.assertContains(response, 'This code is not valid.')
        self.assertEqual(self.counts(), before)

    def test_weak_or_mismatched_passwords_create_nothing(self):
        self.submit_enquiry()
        before = self.counts()
        for first, second in (('', ''), ('short', 'short'), ('12345678901', '12345678901'), (PASSWORD, PASSWORD + 'x')):
            with self.subTest(password=first):
                response, _ = self.pay(password1=first, password2=second)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(self.counts(), before)

    def test_existing_login_must_sign_in_and_then_gains_a_patient_record(self):
        existing = get_user_model().objects.create_user(email=EMAIL, password=PASSWORD, first_name='Thandi', last_name='Checkout')
        self.submit_enquiry()
        self.assertEqual(self.checkout().context['identity'], 'sign_in_required')
        before = self.counts()
        response, _ = self.pay(password1='Another-Pass-2026!', password2='Another-Pass-2026!')
        self.assertContains(response, 'An account already uses this email address')
        self.assertEqual(self.counts(), before)
        existing.refresh_from_db()
        self.assertTrue(existing.check_password(PASSWORD))

        # Signing in keeps this browser's enquiry; no new password is needed.
        self.client.force_login(existing)
        page = self.checkout()
        self.assertEqual(page.context['identity'], 'signed_in')
        response = self.client.post(self.url, self.pay_data(page, password1='', password2=''))
        self.assertRedirects(response, reverse('portal:patient-medical-profile'), fetch_redirect_response=False)
        patient = Patient.objects.get(user=existing)
        self.assertEqual(get_user_model().objects.filter(email__iexact=EMAIL).count(), 1)
        self.assertTrue(AuditEvent.objects.filter(patient=patient, action='patient.record_created').exists())

    def test_a_different_signed_in_person_cannot_take_over_the_enquiry(self):
        other = get_user_model().objects.create_user(email='someone@checkout.test', password=PASSWORD)
        self.submit_enquiry()
        self.client.force_login(other)
        self.assertEqual(self.checkout().context['identity'], 'other_account')
        before = self.counts()
        response, _ = self.pay(password1='', password2='')
        self.assertContains(response, 'signed in with a different email address')
        self.assertEqual(self.counts(), before)
        self.assertFalse(Patient.objects.filter(user=other).exists())

    def test_an_existing_patient_is_sent_to_the_portal_instead(self):
        existing = get_user_model().objects.create_user(email=EMAIL, password=PASSWORD)
        Patient.objects.create(company=self.company, user=existing, first_name='Thandi', last_name='Checkout')
        self.client.force_login(existing)
        self.submit_enquiry()
        self.assertEqual(self.checkout().context['identity'], 'already_patient')
        before = self.counts()
        response, _ = self.pay(password1='', password2='')
        self.assertContains(response, 'already have a patient record')
        self.assertEqual(self.counts(), before)

    def test_a_time_taken_before_payment_rolls_back_the_new_login(self):
        self.submit_enquiry()
        page = self.checkout()
        other = Patient.objects.create(company=self.company, first_name='Other', last_name='Patient')
        Appointment.objects.create(company=self.company, patient=other, clinician=self.doctor, duration_minutes=30,
                                   starts_at=page.context['slots'][0]['starts_at'], appointment_type=Appointment.Type.INITIAL)
        before = self.counts()
        response = self.client.post(self.url, self.pay_data(page))
        self.assertContains(response, 'no longer available')
        self.assertEqual(self.counts(), before)
        self.assertFalse(get_user_model().objects.filter(email=EMAIL).exists())

    def test_a_duplicate_id_number_rolls_back_the_new_login(self):
        Patient.objects.create(company=self.company, first_name='Existing', last_name='Record', id_number='9001015009087')
        self.submit_enquiry()
        before = self.counts()
        response, _ = self.pay()
        self.assertContains(response, 'already exists')
        self.assertEqual(self.counts(), before)
        self.assertFalse(get_user_model().objects.filter(email=EMAIL).exists())

    def test_a_time_choice_from_another_browser_is_rejected(self):
        self.submit_enquiry()
        token = self.checkout().context['slots'][0]['token']
        browser = Client()
        self.submit_enquiry(browser, email='other@checkout.test', id_number='8001015009087')
        before = self.counts()
        response = browser.post(self.url, self.pay_data(self.checkout(browser), slot=token))
        self.assertContains(response, 'selection has expired')
        self.assertEqual(self.counts(), before)

    def test_resubmitting_after_conversion_does_not_book_twice(self):
        self.submit_enquiry()
        data = self.pay_data(self.checkout())
        self.assertRedirects(self.client.post(self.url, data), reverse('portal:patient-medical-profile'),
                             fetch_redirect_response=False)
        before = self.counts()
        self.assertRedirects(self.client.post(self.url, data), reverse('portal:patient-dashboard'),
                             fetch_redirect_response=False)
        self.assertEqual(self.counts(), before)

    def test_a_lead_needing_review_cannot_use_the_checkout(self):
        self.submit_enquiry(pregnancy='yes')
        before = self.counts()
        response = self.client.post(self.url, {'action': 'pay', 'discount_code': 'devtest'})
        self.assertRedirects(response, reverse('portal:questionnaire-result'), fetch_redirect_response=False)
        self.assertEqual(self.counts(), before)

    def test_an_unconfigured_consultation_fee_blocks_payment(self):
        PracticeSettings.objects.filter(company=self.company).delete()
        self.submit_enquiry()
        page = self.checkout()
        self.assertIsNone(page.context['quote'])
        self.assertFalse(page.context['pay_enabled'])
        before = self.counts()
        response = self.client.post(self.url, self.pay_data(page))
        self.assertContains(response, 'has not set its consultation fee')
        self.assertEqual(self.counts(), before)

    def test_without_a_date_the_first_day_with_free_times_is_shown(self):
        self.submit_enquiry()
        response = self.client.get(self.url)
        self.assertIn(response.context['day'], (timezone.localdate(), self.day))
        self.assertTrue(response.context['slots'])
