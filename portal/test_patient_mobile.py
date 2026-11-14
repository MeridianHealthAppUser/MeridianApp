"""The patient portal on a phone (Nov 2026): the phone stylesheet on every patient page, a bottom bar whose labels fit,
My Messages as a list then one conversation, compact weight history, and forms that bring up the right keyboard
and autofill. The layout itself is static/css/patient_mobile.css."""

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from care.forms import EligibilityQuestionnaireForm, WeightEntryForm
from care.models import MessageThread, WeightEntry
from practices.models import Company, CompanyMembership, Patient
from practices.services import ACTIVE_PATIENT_COMPANY_SESSION_KEY

from .intake_views import CallbackForm
from .operations_forms import DeliveryAddressForm


@override_settings(MULTI_PRACTICE_ENABLED=True)
class PatientPhoneLayoutTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name='Phone layout practice', slug='patient-phone-layout')
        cls.user = get_user_model().objects.create_user(email='phone-patient@example.test', first_name='Alice')
        cls.doctor = get_user_model().objects.create_user(email='phone-doctor@example.test', first_name='Doctor')
        CompanyMembership.objects.create(company=cls.company, user=cls.doctor, role=CompanyMembership.Role.DOCTOR)
        cls.patient = Patient.objects.create(company=cls.company, user=cls.user, first_name='Alice', last_name='Phone',
                                             assigned_doctor=cls.doctor)
        cls.thread = MessageThread.objects.create(company=cls.company, patient=cls.patient, subject='About my dose',
                                                  opened_by=cls.user)

    def setUp(self):
        self.client.force_login(self.user)
        session = self.client.session
        session[ACTIVE_PATIENT_COMPANY_SESSION_KEY] = self.company.pk
        session.save()

    def page(self, name, **params):
        return self.client.get(reverse(f'portal:{name}'), params)

    def test_every_patient_page_loads_the_phone_layout(self):
        for name in ('patient-dashboard', 'patient-medical-profile', 'patient-treatment', 'patient-appointments',
                     'patient-messages', 'patient-progress', 'patient-updates', 'patient-profile', 'patient-labs',
                     'patient-billing', 'patient-delivery-address'):
            with self.subTest(page=name):
                response = self.page(name)
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, 'css/patient_mobile.css')
                self.assertContains(response, 'class="patient-navigation__short">info</span>')

    def test_messages_show_the_list_or_one_conversation_with_a_way_back(self):
        listing = self.page('patient-messages')
        self.assertContains(listing, 'class="patient-inbox" aria-label="Secure messages"')
        self.assertNotContains(listing, 'patient-conversation__back')

        for params in ({'thread': self.thread.pk}, {'compose': 1}):
            with self.subTest(**params):
                response = self.page('patient-messages', **params)
                self.assertContains(response, 'class="patient-inbox patient-inbox--open"')
                self.assertContains(response, 'patient-inbox-toolbar patient-inbox-toolbar--open')
                self.assertContains(response, 'class="patient-conversation__back" href="/patient/messages/?status=all&amp;page=1"')
                self.assertContains(response, 'All conversations</a>')

    def test_weight_history_marks_check_ins_without_a_note(self):
        today = timezone.localdate()
        WeightEntry.objects.create(company=self.company, patient=self.patient, recorded_by=self.user, recorded_on=today,
                                   weight_kg='90.00', note='')
        WeightEntry.objects.create(company=self.company, patient=self.patient, recorded_by=self.user,
                                   recorded_on=today - timedelta(days=7), weight_kg='91.00', note='After the holidays')
        response = self.page('patient-progress')
        self.assertContains(response, 'class="patient-table patient-weight-table"')
        self.assertContains(response, '<td data-label="Note" class="patient-table__blank">', count=1)
        self.assertContains(response, 'After the holidays')


class PhoneKeyboardAndAutofillTests(SimpleTestCase):
    def test_the_questionnaire_fills_in_and_shows_the_right_keyboards(self):
        fields = EligibilityQuestionnaireForm.base_fields
        attrs = {name: fields[name].widget.attrs for name in fields}
        self.assertEqual(attrs['first_name']['autocomplete'], 'given-name')
        self.assertEqual(attrs['last_name']['autocomplete'], 'family-name')
        self.assertEqual(attrs['email']['autocomplete'], 'email')
        self.assertEqual(fields['phone'].widget.input_type, 'tel')
        self.assertEqual(attrs['phone']['autocomplete'], 'tel')
        self.assertEqual(attrs['id_number']['autocomplete'], 'off')
        self.assertEqual(attrs['height_cm']['inputmode'], 'decimal')
        self.assertEqual(attrs['weight_kg']['inputmode'], 'decimal')
        self.assertEqual(CallbackForm().fields['phone'].widget.input_type, 'tel')
        self.assertEqual(WeightEntryForm.base_fields['weight_kg'].widget.attrs['inputmode'], 'decimal')

    def test_only_the_patients_own_address_form_offers_autofill(self):
        patient = Patient(phone='082 000 0000', city='Cape Town')
        own = DeliveryAddressForm(patient=patient, for_patient=True)
        practice = DeliveryAddressForm(patient=patient)
        self.assertEqual(own.fields['street'].widget.attrs['autocomplete'], 'shipping address-line1')
        self.assertEqual(own.fields['postal_code'].widget.attrs['autocomplete'], 'shipping postal-code')
        self.assertNotIn('autocomplete', practice.fields['street'].widget.attrs)
        self.assertNotIn('autocomplete', practice.fields['phone'].widget.attrs)
        for form in (own, practice):
            self.assertEqual(form.fields['phone'].widget.input_type, 'tel')
            self.assertEqual(form.fields['postal_code'].widget.attrs['inputmode'], 'numeric')
