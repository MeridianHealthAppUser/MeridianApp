"""Patient care pages retain tenant, identity, availability and version boundaries."""

from django.test import override_settings
from datetime import datetime, time, timedelta
from html.parser import HTMLParser

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from care.availability import SAST
from care.models import (
    Appointment, AuditEvent, AvailabilitySlot, DoctorTimeOff, DoctorWorkingPattern,
    PatientEvent, PatientMedicalProfile, PatientMedicalProfileRevision, WeightEntry,
)
from care.patient_care import book_patient_appointment
from practices.models import Company, CompanyMembership, Patient
from practices.services import ACTIVE_COMPANY_SESSION_KEY, ACTIVE_PATIENT_COMPANY_SESSION_KEY


@override_settings(MULTI_PRACTICE_ENABLED=True)
class PatientCareTests(TestCase):
    pages = ('patient-appointments', 'patient-medical-profile', 'patient-updates')

    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name='Care Alpha', slug='patient-care-alpha')
        cls.beta = Company.objects.create(name='Care Beta', slug='patient-care-beta')
        cls.user = get_user_model().objects.create_user(email='care-patient@example.test', first_name='Alice')
        cls.other_user = get_user_model().objects.create_user(email='care-other@example.test')
        cls.doctor = get_user_model().objects.create_user(email='care-doctor@example.test', first_name='Doctor', last_name='One')
        cls.other_doctor = get_user_model().objects.create_user(email='care-doctor-two@example.test', first_name='Doctor', last_name='Two')
        cls.beta_doctor = get_user_model().objects.create_user(email='care-beta-doctor@example.test', first_name='Beta Doctor')
        cls.admin = get_user_model().objects.create_user(email='care-admin@example.test')
        for doctor in (cls.doctor, cls.other_doctor):
            for company in (cls.company, cls.beta):
                CompanyMembership.objects.create(company=company, user=doctor, role=CompanyMembership.Role.DOCTOR)
        CompanyMembership.objects.create(company=cls.beta, user=cls.beta_doctor, role=CompanyMembership.Role.DOCTOR)
        CompanyMembership.objects.create(company=cls.company, user=cls.admin, role=CompanyMembership.Role.PRACTICE_ADMIN)
        cls.patient = Patient.objects.create(company=cls.company, user=cls.user, first_name='Alice', last_name='Alpha', assigned_doctor=cls.doctor)
        cls.beta_patient = Patient.objects.create(company=cls.beta, user=cls.user, first_name='Alice', last_name='Beta', assigned_doctor=cls.doctor)
        cls.other_patient = Patient.objects.create(company=cls.company, user=cls.other_user, first_name='Other', last_name='Patient')
        cls.day = timezone.localdate() + timedelta(days=2)
        cls.starts_at = datetime.combine(cls.day, time(10), tzinfo=SAST)
        cls.slot = AvailabilitySlot.objects.create(company=cls.company, clinician=cls.doctor, starts_at=cls.starts_at, ends_at=cls.starts_at + timedelta(minutes=30))
        cls.later_slot = AvailabilitySlot.objects.create(company=cls.company, clinician=cls.doctor, starts_at=cls.starts_at + timedelta(hours=1), ends_at=cls.starts_at + timedelta(hours=1, minutes=30))

    def login(self, user=None, company=None):
        self.client.force_login(user or self.user)
        session = self.client.session
        session[ACTIVE_PATIENT_COMPANY_SESSION_KEY] = (company or self.company).pk
        session[ACTIVE_COMPANY_SESSION_KEY] = self.company.pk
        session.save()

    def url(self, name, **kwargs):
        return reverse(f'portal:{name}', kwargs=kwargs or None)

    def booking_page(self, **changes):
        data = {'book': 1, 'date': self.day.isoformat(), 'clinician': self.doctor.pk, 'appointment_type': 'follow_up'}
        data.update(changes)
        return self.client.get(self.url('patient-appointments'), data)

    def times(self, **changes):
        """What Book appointment's Time list offers."""
        return self.client.get(self.url('patient-booking-times'), {'clinician': self.doctor.pk, 'date': self.day.isoformat(), **changes})

    def booking_data(self, **changes):
        response = self.booking_page()
        self.assertEqual(response.status_code, 200)
        self.assertIn('10:00', self.times().json()['times'])
        return {'clinician': self.doctor.pk, 'appointment_type': 'follow_up', 'date': self.day.isoformat(), 'time': '10:00',
                'patient_context': response.context['patient_context'], **changes}

    def profile_data(self, **answers):
        response = self.client.get(self.url('patient-medical-profile'))
        self.assertEqual(response.status_code, 200)
        return {'clinical_context': response.context['clinical_context'], **answers}

    def create_appointment(self, **changes):
        values = dict(company=self.company, patient=self.patient, clinician=self.doctor,
                      starts_at=self.starts_at, duration_minutes=15, appointment_type=Appointment.Type.REVIEW)
        values.update(changes)
        return Appointment.objects.create(**values)

    def event(self, title='Visible care update', **changes):
        values = dict(company=self.company, patient=self.patient, category=PatientEvent.Category.APPOINTMENT,
                      title=title, detail='Shared safely with the patient', is_patient_visible=True)
        values.update(changes)
        return PatientEvent.objects.create(**values)

    def counts(self):
        return {model.__name__: model.objects.count() for model in (
            Appointment, AuditEvent, PatientEvent, PatientMedicalProfile, PatientMedicalProfileRevision, get_user_model(),
        )}

    def test_pages_require_authenticated_patient(self):
        for name in self.pages:
            with self.subTest(name=name):
                self.assertEqual(self.client.get(self.url(name)).status_code, 302)
        for user in (self.doctor, self.admin):
            self.login(user)
            for name in self.pages:
                with self.subTest(user=user.pk, name=name):
                    self.assertEqual(self.client.get(self.url(name)).status_code, 403)

    def test_read_pages_are_private_and_have_no_incidental_writes(self):
        self.login()
        before = self.counts()
        for name in self.pages:
            for method in ('get', 'head'):
                with self.subTest(name=name, method=method):
                    response = getattr(self.client, method)(self.url(name))
                    self.assertEqual(response.status_code, 200)
                    self.assertIn('no-store', response.headers['Cache-Control'])
        self.assertEqual(before, self.counts())

    def test_book_appointment_is_a_button_that_opens_the_booking_dialog(self):
        self.login()
        page = self.client.get(self.url('patient-appointments'))
        self.assertContains(page, f'<a class="console-button console-button--primary" href="{self.url("patient-appointments")}?book=1" '
                                  'data-modal-open="book-appointment" aria-haspopup="dialog">Book Appointment</a>', html=True)
        self.assertContains(page, '<dialog class="app-dialog" id="book-appointment" aria-labelledby="book-appointment-title">')
        self.assertNotContains(page, 'Book a consult')
        form = page.context['booking_form']
        # The type is fixed; patients choose a clinician, date and time.
        self.assertEqual(list(form.fields), ['clinician', 'appointment_type', 'date', 'time'])
        self.assertEqual(list(form.fields['appointment_type'].choices), [('follow_up', 'Follow-up · 15 minutes')])
        self.assertTrue(form['appointment_type'].is_hidden)
        self.assertEqual(form['appointment_type'].value(), 'follow_up')
        self.assertContains(page, 'Follow-up · 15 minutes')
        self.assertNotContains(page, '<select name="appointment_type"')
        self.assertEqual(form['clinician'].value(), self.doctor.pk)
        self.assertContains(page, f'data-booking-form data-times-url="{self.url("patient-booking-times")}"')
        self.assertContains(page, f'action="{self.url("patient-book-appointment")}"')
        # Links from other pages, and the old booking address, open it.
        self.assertContains(self.booking_page(), 'aria-labelledby="book-appointment-title" open>')
        response = self.client.get(self.url('patient-book-appointment'), {'date': self.day.isoformat(), 'clinician': self.doctor.pk})
        self.assertRedirects(response, f"{self.url('patient-appointments')}?book=1&date={self.day.isoformat()}&clinician={self.doctor.pk}",
                             fetch_redirect_response=False)

    def test_a_title_typed_into_the_name_is_not_doubled(self):
        get_user_model().objects.filter(pk=self.doctor.pk).update(title='DR', first_name='Dr Sam')
        self.login()
        page = self.client.get(self.url('patient-appointments'))
        self.assertContains(page, f'<option value="{self.doctor.pk}" selected>Dr Sam One</option>', html=True)
        self.assertNotContains(page, 'Dr Dr')

    def test_the_booked_banner_shows_once_after_booking(self):
        self.login()
        self.create_appointment(starts_at=self.starts_at + timedelta(days=1))
        self.assertIsNone(self.client.get(self.url('patient-appointments')).context['just_booked'])
        self.assertNotContains(self.client.get(self.url('patient-appointments')), 'id="next-consult-title"')
        self.client.post(self.url('patient-book-appointment'), self.booking_data())
        appointment = Appointment.objects.get(starts_at=self.starts_at)
        page = self.client.get(self.url('patient-appointments'))
        self.assertEqual(page.context['just_booked'], appointment)
        self.assertContains(page, '<p class="patient-eyebrow">Booked</p>', html=True)
        self.assertContains(page, f'{self.url("patient-appointment-detail", pk=appointment.pk)}')
        # Gone when they come back.
        self.assertNotContains(self.client.get(self.url('patient-appointments')), 'id="next-consult-title"')

    def test_appointments_filter_by_status_date_clinician_and_type(self):
        self.login()
        now = timezone.localtime()
        # Today and still upcoming whatever the time: it started a few minutes ago (never before midnight) and its
        # 15 minutes haven't ended. Half an hour ahead would be tomorrow when the tests run after 23:30 SAST.
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        today = self.create_appointment(starts_at=max(now - timedelta(minutes=5), midnight), appointment_type='follow_up')
        later = self.create_appointment(starts_at=now + timedelta(days=40), clinician=self.other_doctor)
        past = self.create_appointment(starts_at=now - timedelta(days=3), status='completed')

        def listed(**query):
            return self.client.get(self.url('patient-appointments'), query).context['appointments']

        self.assertEqual(listed(), [today, later])
        self.assertEqual(listed(status='history'), [past])
        self.assertEqual(listed(status='all', when='today'), [today])
        self.assertEqual(listed(when='month'), [today] if later.starts_at.month != now.month else [today, later])
        self.assertEqual(listed(clinician=self.other_doctor.pk), [later])
        self.assertEqual(listed(type='follow_up'), [today])
        self.assertEqual(listed(type=['follow_up', 'review']), [today, later])
        page = self.client.get(self.url('patient-appointments'), {'clinician': self.other_doctor.pk, 'type': 'review'})
        self.assertContains(page, '<h2 id="patient-appointments-heading">My Appointments</h2>', html=True)
        self.assertNotContains(page, 'All your consults')
        self.assertNotContains(page, 'View appointments')
        menu = page.context['filter_menu']
        self.assertEqual([section['label'] for section in menu['sections']], ['Status', 'Date', 'Clinician', 'Appointment type'])
        self.assertEqual([option['label'] for option in menu['sections'][1]['options']], ['Any time', 'Today', 'This week', 'This month'])
        # Only the clinicians they've had appointments with; Upcoming is the default, so it isn't counted.
        self.assertEqual([option['label'] for option in menu['sections'][2]['options']], ['Doctor One', 'Doctor Two'])
        self.assertEqual(menu['count'], 2)
        self.assertContains(page, 'class="filter-menu filter-menu--compact"')

    def test_csrf_protects_booking_and_profile(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.user)
        for name in ('patient-book-appointment', 'patient-medical-profile'):
            self.assertEqual(client.post(self.url(name), {}).status_code, 403)

    def test_booking_shows_only_active_practice_doctors(self):
        self.login()
        response = self.booking_page()
        self.assertNotContains(response, 'Beta Doctor')
        self.assertEqual(self.times(clinician=self.beta_doctor.pk).status_code, 400)
        response = self.client.post(self.url('patient-book-appointment'), self.booking_data(clinician=self.beta_doctor.pk))
        self.assertTrue(response.context['booking_form'].errors['clinician'])
        self.assertFalse(Appointment.objects.exists())

    def test_invalid_dates_offer_no_times(self):
        self.login()
        for date in ('invalid', '9999-12-31', (self.day - timedelta(days=5)).isoformat(), (self.day + timedelta(days=91)).isoformat()):
            with self.subTest(date=date):
                self.assertEqual(self.times(date=date).status_code, 400)
                response = self.client.post(self.url('patient-book-appointment'), self.booking_data(date=date))
                self.assertTrue(response.context['booking_form'].errors['date'])
        self.assertFalse(Appointment.objects.exists())

    def test_booking_needs_a_time(self):
        self.login()
        before = self.counts()
        response = self.client.post(self.url('patient-book-appointment'), self.booking_data(time=''))
        self.assertEqual(response.context['booking_form'].errors['time'], ['Choose a time.'])
        # The dialog stays open to show it.
        self.assertContains(response, 'aria-labelledby="book-appointment-title" open>')
        self.assertEqual(before, self.counts())

    def test_booking_creates_one_owned_appointment_and_event_without_account(self):
        self.login()
        data = self.booking_data()
        users = get_user_model().objects.count()
        response = self.client.post(self.url('patient-book-appointment'), data)
        self.assertRedirects(response, self.url('patient-appointments'))
        appointment = Appointment.objects.get()
        self.assertEqual((appointment.company_id, appointment.patient_id, appointment.clinician_id), (self.company.pk, self.patient.pk, self.doctor.pk))
        self.assertEqual(appointment.duration_minutes, 15)
        self.assertEqual(appointment.appointment_type, Appointment.Type.FOLLOW_UP)
        self.assertEqual(appointment.starts_at, self.starts_at)
        self.assertEqual(appointment.video_link, '')
        self.slot.refresh_from_db()
        self.assertEqual(self.slot.appointment_id, appointment.pk)
        self.assertEqual(get_user_model().objects.count(), users)
        self.assertEqual(PatientEvent.objects.filter(is_patient_visible=True).count(), 1)
        self.assertEqual(AuditEvent.objects.filter(action='appointment.patient_booked').count(), 1)

    def test_duplicate_booking_is_idempotent(self):
        self.login()
        data = self.booking_data()
        self.client.post(self.url('patient-book-appointment'), data)
        before = self.counts()
        response = self.client.post(self.url('patient-book-appointment'), data)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(before, self.counts())

    def test_only_offered_times_can_be_booked(self):
        self.login()
        # 10:15 is inside the clinician's 10:00 slot but isn't a time it offers; 10:03 isn't a quarter hour.
        self.assertNotIn('10:15', self.times().json()['times'])
        response = self.client.post(self.url('patient-book-appointment'), self.booking_data(time='10:15'))
        self.assertTrue(response.context['booking_form'].non_field_errors())
        response = self.client.post(self.url('patient-book-appointment'), self.booking_data(time='10:03'))
        self.assertTrue(response.context['booking_form'].errors['time'])
        self.assertFalse(Appointment.objects.exists())

    def test_stale_practice_booking_cannot_write_other_practice(self):
        self.login()
        data = self.booking_data()
        self.login(company=self.beta)
        before = self.counts()
        response = self.client.post(self.url('patient-book-appointment'), data)
        self.assertTrue(response.context['booking_form'].non_field_errors())
        self.assertEqual(before, self.counts())

    def test_foreign_patient_cannot_replay_booking(self):
        self.login()
        data = self.booking_data()
        self.login(self.other_user)
        response = self.client.post(self.url('patient-book-appointment'), data)
        self.assertTrue(response.context['booking_form'].non_field_errors())
        self.assertFalse(Appointment.objects.exists())

    def test_booking_rechecks_clinician_occupancy_across_practices(self):
        self.login()
        data = self.booking_data()
        self.create_appointment(company=self.beta, patient=self.beta_patient, starts_at=self.starts_at - timedelta(minutes=5), duration_minutes=30, outcome_notes='Private beta appointment outcome')
        response = self.client.post(self.url('patient-book-appointment'), data)
        self.assertTrue(response.context['booking_form'].non_field_errors())
        self.assertEqual(Appointment.objects.count(), 1)
        self.assertNotContains(response, 'Private beta appointment outcome')

    def test_booking_rechecks_patient_occupancy_with_another_doctor(self):
        self.login()
        data = self.booking_data()
        self.create_appointment(company=self.beta, patient=self.beta_patient, clinician=self.other_doctor)
        response = self.client.post(self.url('patient-book-appointment'), data)
        self.assertTrue(response.context['booking_form'].non_field_errors())
        self.assertEqual(Appointment.objects.count(), 1)

    def test_busy_patient_slots_are_not_advertised(self):
        self.login()
        self.create_appointment(company=self.beta, patient=self.beta_patient, clinician=self.other_doctor)
        self.assertNotIn('10:00', self.times().json()['times'])

    def test_booking_rechecks_global_time_off_without_revealing_reason(self):
        self.login()
        data = self.booking_data()
        DoctorTimeOff.objects.create(company=self.beta, clinician=self.doctor, starts_at=self.starts_at,
                                     ends_at=self.starts_at + timedelta(hours=1), reason='personal')
        response = self.client.post(self.url('patient-book-appointment'), data)
        self.assertTrue(response.context['booking_form'].non_field_errors())
        self.assertNotContains(response, 'Personal')
        self.assertFalse(Appointment.objects.exists())

    def test_booking_rechecks_changed_working_pattern(self):
        self.login()
        data = self.booking_data()
        DoctorWorkingPattern.objects.create(company=self.company, clinician=self.doctor, weekday=self.day.weekday(), is_working=False)
        response = self.client.post(self.url('patient-book-appointment'), data)
        self.assertTrue(response.context['booking_form'].non_field_errors())
        self.assertFalse(Appointment.objects.exists())

    def test_booking_uses_generated_working_pattern_slot(self):
        self.login()
        DoctorWorkingPattern.objects.create(company=self.company, clinician=self.doctor, weekday=self.day.weekday(),
                                            is_working=True, starts_at=time(10), ends_at=time(11))
        data = self.booking_data()
        self.assertEqual(self.client.post(self.url('patient-book-appointment'), data).status_code, 302)
        self.assertEqual(Appointment.objects.count(), 1)
        self.slot.refresh_from_db()
        self.assertFalse(self.slot.is_booked)

    def test_booking_rechecks_inactive_doctor_and_membership(self):
        self.login()
        data = self.booking_data()
        CompanyMembership.objects.filter(company=self.company, user=self.doctor).update(is_active=False)
        response = self.client.post(self.url('patient-book-appointment'), data)
        self.assertTrue(response.context['booking_form'].errors)
        self.assertFalse(Appointment.objects.exists())

    def test_patient_booking_rejects_other_types_even_when_the_form_is_forged(self):
        self.login()
        data = self.booking_data()
        before = self.counts()
        for appointment_type in ('initial', 'review', 'ad_hoc'):
            with self.subTest(appointment_type=appointment_type):
                response = self.client.post(self.url('patient-book-appointment'), {**data, 'appointment_type': appointment_type})
                self.assertIn('appointment_type', response.context['booking_form'].errors)
                self.assertContains(response, 'aria-labelledby="book-appointment-title" open>')
        self.assertEqual(before, self.counts())
        # A stale or hand-edited booking link cannot change the fixed form value.
        form = self.booking_page(appointment_type='review').context['booking_form']
        self.assertEqual(form['appointment_type'].value(), 'follow_up')

    def test_booking_service_rejects_unoffered_time_and_non_follow_up_types(self):
        for values in (
            {'starts_at': self.starts_at + timedelta(minutes=3), 'appointment_type': 'follow_up'},
            {'starts_at': self.starts_at, 'appointment_type': 'initial'},
            {'starts_at': self.starts_at, 'appointment_type': 'review'},
            {'starts_at': self.starts_at, 'appointment_type': 'ad_hoc'},
        ):
            with self.assertRaises(ValidationError):
                book_patient_appointment(company=self.company, patient=self.patient, actor=self.user, clinician_id=self.doctor.pk, **values)
        self.assertFalse(Appointment.objects.exists())

    def test_stale_booking_type_can_retry_with_returned_follow_up_field(self):
        class HiddenFields(HTMLParser):
            def __init__(self):
                super().__init__()
                self.values = {}

            def handle_starttag(self, tag, attrs):
                attrs = dict(attrs)
                if tag == 'input' and attrs.get('type') == 'hidden' and attrs.get('name') in ('appointment_type', 'patient_context'):
                    self.values[attrs['name']] = attrs.get('value', '')

        self.login()
        data = self.booking_data(appointment_type='review')
        before = self.counts()
        response = self.client.post(self.url('patient-book-appointment'), data)
        form = response.context['booking_form']
        self.assertIn('appointment_type', form.errors)
        self.assertEqual(form['appointment_type'].value(), 'review')
        self.assertEqual(self.counts(), before)
        for name in ('clinician', 'date', 'time'):
            self.assertEqual(str(form[name].value()), str(data[name]))
            self.assertContains(response, form[name].as_widget(), html=True)
        fields = HiddenFields()
        fields.feed(response.content.decode())
        self.assertEqual(fields.values['appointment_type'], 'follow_up')
        retry = self.client.post(self.url('patient-book-appointment'), {**data, **fields.values})
        self.assertRedirects(retry, self.url('patient-appointments'), fetch_redirect_response=False)
        appointment = Appointment.objects.get()
        self.assertEqual((appointment.clinician_id, appointment.starts_at, appointment.appointment_type, appointment.duration_minutes),
                         (self.doctor.pk, self.starts_at, Appointment.Type.FOLLOW_UP, 15))

    def test_profile_saves_snapshot_and_id_only_audit(self):
        self.login()
        data = self.profile_data(medications='Private medicine context', allergies='Private allergy answer')
        response = self.client.post(self.url('patient-medical-profile'), data)
        self.assertRedirects(response, self.url('patient-medical-profile'))
        profile = PatientMedicalProfile.objects.get()
        self.assertEqual(profile.answers['medications'], 'Private medicine context')
        self.assertEqual(profile.revision, 1)
        self.assertEqual(profile.saved_by_id, self.user.pk)
        self.assertEqual(profile.history.get().answers, profile.answers)
        audit = AuditEvent.objects.get(action='patient.medical_profile_updated')
        self.assertNotIn('Private', str(audit.metadata))
        self.assertEqual(audit.metadata['revision'], 1)
        self.assertFalse(PatientEvent.objects.exists())

    def test_once_a_clinician_starts_summary_data_they_keep_the_shared_answers_with_every_version(self):
        from care.clinical_summary import edit_summary

        CompanyMembership.objects.filter(company=self.company, user=self.doctor).update(clinician_type='doctor')
        get_user_model().objects.filter(pk=self.doctor.pk).update(title='DR')
        doctor = get_user_model().objects.get(pk=self.doctor.pk)
        self.login()
        # Before a clinician starts Summary data, the patient fills in every answer themselves.
        self.client.post(self.url('patient-medical-profile'), self.profile_data(allergies='None known', goals='Lose 10 kg'))
        self.assertEqual(PatientMedicalProfile.objects.get().answers['allergies'], 'None known')
        # The clinician records an allergy: it goes into the profile as a new version, saved by them.
        edit_summary(company=self.company, patient=self.patient, actor=doctor, field='allergies', value='Bees')
        profile = PatientMedicalProfile.objects.get()
        self.assertEqual((profile.answers['allergies'], profile.answers['goals'], profile.revision, profile.saved_by_id),
                         ('Bees', 'Lose 10 kg', 2, self.doctor.pk))
        self.assertEqual([(row.revision, row.answers['allergies'], row.saved_by_id) for row in profile.history.order_by('revision')],
                         [(1, 'None known', self.user.pk), (2, 'Bees', self.doctor.pk)])
        # The patient sees it read-only, with who saved it, and a way to ask for a correction; their other answers
        # are still theirs to change.
        page = self.client.get(self.url('patient-medical-profile'))
        self.assertContains(page, 'Kept up to date by your care team')
        self.assertContains(page, '<div><dt>Allergies and previous reactions</dt><dd>Bees</dd></div>', html=True)
        self.assertContains(page, 'by Dr One')
        self.assertContains(page, f"{self.url('patient-profile')}?request=1&amp;kind=correction#profile-requests")
        self.assertNotIn('allergies', page.context['profile_form'].fields)
        self.assertIn('goals', page.context['profile_form'].fields)
        self.assertEqual(self.client.get(self.url('patient-profile'), {'request': 1, 'kind': 'correction'})
                         .context['request_form']['kind'].value(), 'correction')
        # A post that tries to change it anyway leaves it as the clinician saved it.
        self.client.post(self.url('patient-medical-profile'), self.profile_data(allergies='None', goals='Lose 12 kg'))
        profile.refresh_from_db()
        self.assertEqual((profile.answers['allergies'], profile.answers['goals'], profile.revision), ('Bees', 'Lose 12 kg', 3))

    def test_profile_updates_keep_original_snapshot(self):
        self.login()
        self.client.post(self.url('patient-medical-profile'), self.profile_data(medications='Original answer'))
        self.client.post(self.url('patient-medical-profile'), self.profile_data(medications='Updated answer'))
        profile = PatientMedicalProfile.objects.get()
        self.assertEqual(profile.revision, 2)
        self.assertEqual(profile.history.get(revision=1).answers['medications'], 'Original answer')
        self.assertEqual(profile.history.get(revision=2).answers['medications'], 'Updated answer')

    def test_unchanged_or_empty_profile_does_not_create_revision(self):
        self.login()
        self.client.post(self.url('patient-medical-profile'), self.profile_data())
        self.assertFalse(PatientMedicalProfile.objects.exists())
        self.client.post(self.url('patient-medical-profile'), self.profile_data(medications='Same answer'))
        before = self.counts()
        self.client.post(self.url('patient-medical-profile'), self.profile_data(medications='Same answer'))
        self.assertEqual(before, self.counts())

    def test_profile_stale_revision_does_not_overwrite_and_preserves_form(self):
        self.login()
        self.client.post(self.url('patient-medical-profile'), self.profile_data(medications='Original'))
        stale = self.profile_data(medications='Stale unsaved answer')
        self.client.post(self.url('patient-medical-profile'), self.profile_data(medications='Latest saved'))
        response = self.client.post(self.url('patient-medical-profile'), stale)
        self.assertTrue(response.context['profile_form'].non_field_errors())
        self.assertContains(response, 'Stale unsaved answer')
        self.assertEqual(PatientMedicalProfile.objects.get().answers['medications'], 'Latest saved')
        self.assertEqual(PatientMedicalProfileRevision.objects.count(), 2)

    def test_profile_stale_create_and_practice_tokens_cannot_overwrite(self):
        self.login()
        stale = self.profile_data(medications='Stale answer')
        self.client.post(self.url('patient-medical-profile'), self.profile_data(medications='Saved answer'))
        response = self.client.post(self.url('patient-medical-profile'), stale)
        self.assertTrue(response.context['profile_form'].non_field_errors())
        self.login(company=self.beta)
        response = self.client.post(self.url('patient-medical-profile'), stale)
        self.assertTrue(response.context['profile_form'].non_field_errors())
        self.assertEqual(PatientMedicalProfile.objects.count(), 1)

    def test_invalid_profile_keeps_answers_without_write(self):
        self.login()
        response = self.client.post(self.url('patient-medical-profile'), self.profile_data(medications='a' * 2001, goals='Keep this draft'))
        self.assertTrue(response.context['profile_form'].errors)
        self.assertContains(response, 'Keep this draft')
        self.assertFalse(PatientMedicalProfile.objects.exists())

    def test_profile_missing_or_tampered_context_is_rejected(self):
        self.login()
        for token in ('', 'invalid-signature'):
            response = self.client.post(self.url('patient-medical-profile'), {'clinical_context': token, 'allergies': 'No known allergies'})
            self.assertTrue(response.context['profile_form'].non_field_errors())
        self.assertFalse(PatientMedicalProfile.objects.exists())

    def test_profile_scopes_other_practice_and_patient(self):
        self.login()
        self.client.post(self.url('patient-medical-profile'), self.profile_data(medications='Alpha private answer'))
        self.login(company=self.beta)
        self.assertNotContains(self.client.get(self.url('patient-medical-profile')), 'Alpha private answer')
        self.login(self.other_user)
        self.assertNotContains(self.client.get(self.url('patient-medical-profile')), 'Alpha private answer')

    def test_updates_show_only_explicitly_visible_owned_events(self):
        self.login()
        self.event('Owned event')
        self.event('Internal clinical note', is_patient_visible=False)
        self.event('Other patient event', patient=self.other_patient)
        self.event('Other practice event', company=self.beta, patient=self.beta_patient)
        response = self.client.get(self.url('patient-updates'))
        self.assertContains(response, 'Owned event')
        for text in ('Internal clinical note', 'Other patient event', 'Other practice event'):
            self.assertNotContains(response, text)

    def test_updates_support_category_sast_dates_and_sort(self):
        self.login()
        midnight = datetime.combine(self.day, time.min, tzinfo=SAST)
        first = self.event('First', occurred_at=midnight, category='message')
        second = self.event('Second', occurred_at=midnight + timedelta(hours=23, minutes=59), category='message')
        self.event('Excluded category', occurred_at=midnight)
        self.event('Excluded previous day', occurred_at=midnight - timedelta(seconds=1), category='message')
        data = {'category': 'message', 'date_from': self.day.isoformat(), 'date_to': self.day.isoformat(), 'sort': 'oldest'}
        response = self.client.get(self.url('patient-updates'), data)
        self.assertEqual([event.pk for event in response.context['events']], [first.pk, second.pk])
        response = self.client.get(self.url('patient-updates'), {**data, 'sort': 'newest'})
        self.assertEqual([event.pk for event in response.context['events']], [second.pk, first.pk])

    def test_invalid_updates_filters_never_broaden_results(self):
        self.login()
        self.event()
        for filters in ({'category': 'private'}, {'sort': 'arbitrary'}, {'date_from': 'invalid'}, {'date_from': '2026-12-01', 'date_to': '2026-01-01'}):
            response = self.client.get(self.url('patient-updates'), filters)
            self.assertTrue(response.context['filter_form'].errors)
            self.assertEqual(response.context['page_obj'].paginator.count, 0)

    def test_updates_pagination_keeps_filters(self):
        self.login()
        for index in range(23):
            self.event(f'Event {index}', category='message')
        response = self.client.get(self.url('patient-updates'), {'category': 'message', 'sort': 'oldest', 'page': 2})
        self.assertEqual(len(response.context['events']), 3)
        self.assertEqual(response.context['page_obj'].number, 2)
        self.assertIn('category=message', response.context['pagination_query'])
        self.assertIn('sort=oldest', response.context['pagination_query'])

    def test_calendar_download_is_private_owned_utc_and_read_only(self):
        self.login()
        appointment = self.create_appointment(outcome_notes='Private clinical outcome', video_link='https://example.test/private-zoom')
        before = self.counts()
        response = self.client.get(self.url('patient-appointment-calendar', pk=appointment.pk))
        self.assertEqual(response.status_code, 200)
        self.assertIn('text/calendar', response.headers['Content-Type'])
        self.assertIn('attachment', response.headers['Content-Disposition'])
        self.assertIn('no-store', response.headers['Cache-Control'])
        self.assertIn('private', response.headers['Cache-Control'])
        self.assertIn('DTSTART:' + self.starts_at.astimezone(__import__('datetime').timezone.utc).strftime('%Y%m%dT%H%M%SZ'), response.content.decode())
        self.assertNotIn('Private clinical outcome', response.content.decode())
        self.assertNotIn('private-zoom', response.content.decode())
        self.assertEqual(self.client.head(self.url('patient-appointment-calendar', pk=appointment.pk)).status_code, 200)
        self.assertEqual(self.counts(), before)

    def test_calendar_denies_other_patient_and_other_practice(self):
        self.login()
        for changes in ({'patient': self.other_patient}, {'company': self.beta, 'patient': self.beta_patient}):
            appointment = self.create_appointment(**changes)
            self.assertEqual(self.client.get(self.url('patient-appointment-calendar', pk=appointment.pk)).status_code, 404)

    def test_calendar_marks_cancelled_appointments(self):
        self.login()
        appointment = self.create_appointment(status=Appointment.Status.CANCELLED)
        response = self.client.get(self.url('patient-appointment-calendar', pk=appointment.pk))
        self.assertIn('STATUS:CANCELLED', response.content.decode())

    def test_calendar_escapes_injection_and_folds_utf8_safely(self):
        self.login()
        self.company.name = 'Practice; north, south\\east\r\nATTENDEE:mailto:attacker@example.test ' + 'é' * 70
        self.company.save()
        appointment = self.create_appointment()
        response = self.client.get(self.url('patient-appointment-calendar', pk=appointment.pk))
        lines = response.content.decode().split('\r\n')
        self.assertTrue(all(len(line.encode('utf-8')) <= 75 for line in lines))
        self.assertFalse(any(line.startswith('ATTENDEE:') for line in lines))
        unfolded = response.content.decode().replace('\r\n ', '')
        self.assertIn(r'Practice\; north\, south\\east\nATTENDEE', unfolded)

    def weight(self, day, weight='90.0', **changes):
        values = dict(company=self.company, patient=self.patient, recorded_on=day,
                      weight_kg=weight, recorded_by=self.user)
        values.update(changes)
        return WeightEntry.objects.create(**values)

    def test_progress_chart_contains_only_owned_actual_weights(self):
        self.login()
        day = timezone.localdate()
        self.weight(day - timedelta(days=2), '94.0')
        self.weight(day, '90.0')
        self.weight(day, '130.0', company=self.beta, patient=self.beta_patient)
        self.weight(day, '150.0', patient=self.other_patient)
        response = self.client.get(self.url('patient-progress'))
        chart = response.context['weight_chart']
        self.assertEqual(chart['count'], 2)
        self.assertEqual([str(point['weight']) for point in chart['points']], ['94.00', '90.00'])
        self.assertContains(response, 'weight-chart-description')
        self.assertContains(response, 'Recorded measurements only')

    def test_progress_date_range_filters_chart_history_but_not_overall_summary(self):
        self.login()
        day = timezone.localdate()
        oldest = self.weight(day - timedelta(days=30), '94.0')
        latest = self.weight(day, '90.0')
        response = self.client.get(self.url('patient-progress'), {'date_from': day.isoformat(), 'date_to': day.isoformat()})
        self.assertEqual(response.context['page_obj'].paginator.count, 1)
        self.assertEqual(response.context['weight_chart']['count'], 1)
        self.assertEqual(response.context['first_weight'].pk, oldest.pk)
        self.assertEqual(response.context['latest_weight'].pk, latest.pk)
        self.assertEqual(str(response.context['weight_change']), '-4.00')

    def test_invalid_progress_range_has_no_chart_or_history(self):
        self.login()
        self.weight(timezone.localdate())
        response = self.client.get(self.url('patient-progress'), {'date_from': '2026-12-01', 'date_to': '2026-01-01'})
        self.assertTrue(response.context['weight_filter_form'].errors)
        self.assertIsNone(response.context['weight_chart'])
        self.assertEqual(response.context['page_obj'].paginator.count, 0)

    def test_progress_chart_is_bounded_and_paginated_history_remains_complete(self):
        self.login()
        day = timezone.localdate()
        WeightEntry.objects.bulk_create([
            WeightEntry(company=self.company, patient=self.patient, recorded_on=day - timedelta(days=index), weight_kg='90.00')
            for index in range(305)
        ])
        response = self.client.get(self.url('patient-progress'), {'page': 16, 'date_to': day.isoformat()})
        self.assertEqual(response.context['weight_chart']['count'], 300)
        self.assertTrue(response.context['chart_is_limited'])
        self.assertEqual(response.context['page_obj'].paginator.count, 305)
        self.assertEqual(len(response.context['weights']), 5)
        self.assertIn('date_to=', response.context['pagination_query'])
