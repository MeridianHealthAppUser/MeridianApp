from django.test import override_settings
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from practices.models import Company, CompanyMembership, Patient

from .forms import AppointmentForm, ClinicalNoteForm, ClinicalTaskForm, WeightEntryForm
from .models import Appointment, WeightEntry


@override_settings(MULTI_PRACTICE_ENABLED=True)
class PatientWorkflowFormTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name='Meridian', slug='meridian')
        cls.other_company = Company.objects.create(name='Orion', slug='orion')
        users = get_user_model().objects
        cls.doctor = users.create_user(email='doctor@example.com')
        cls.other_doctor = users.create_user(email='other@example.com')
        cls.admin = users.create_user(email='admin@example.com')
        cls.inactive_doctor = users.create_user(email='inactive@example.com', is_active=False)
        cls.patient_user = users.create_user(email='patient@example.com')
        for company, user, role in (
            (cls.company, cls.doctor, CompanyMembership.Role.DOCTOR),
            (cls.other_company, cls.doctor, CompanyMembership.Role.DOCTOR),
            (cls.other_company, cls.other_doctor, CompanyMembership.Role.DOCTOR),
            (cls.company, cls.admin, CompanyMembership.Role.PRACTICE_ADMIN),
            (cls.company, cls.inactive_doctor, CompanyMembership.Role.DOCTOR),
        ):
            CompanyMembership.objects.create(company=company, user=user, role=role)
        cls.patient = Patient.objects.create(
            company=cls.company, user=cls.patient_user, first_name='Nadia', last_name='Mokoena',
        )
        cls.second_patient = Patient.objects.create(
            company=cls.company, first_name='Second', last_name='Patient',
        )
        cls.other_patient = Patient.objects.create(
            company=cls.other_company, first_name='Other', last_name='Patient',
        )

    def task_data(self, **overrides):
        return {
            'patient': self.patient.pk,
            'title': 'Check in after consultation',
            'description': 'Keep this text on validation errors.',
            'assigned_to': self.doctor.pk,
            'priority': 'normal',
            **overrides,
        }

    def appointment_data(self, **overrides):
        return {
            'patient': self.patient.pk,
            'clinician': self.doctor.pk,
            'appointment_type': 'follow_up',
            'starts_at': timezone.now() + timedelta(days=2),
            'duration_minutes': 30,
            **overrides,
        }

    def weight_form(self, **overrides):
        return WeightEntryForm(
            {'recorded_on': timezone.localdate(), 'weight_kg': '85.5', 'note': 'After breakfast', **overrides},
            company=self.company,
            patient=self.patient,
            recorded_by=self.patient_user,
        )

    def test_server_context_is_attached_before_validation_and_saved(self):
        note = ClinicalNoteForm(
            {'note_type': 'consult', 'body': 'Progress reviewed.'},
            company=self.company, patient=self.patient, author=self.doctor,
        )
        self.assertEqual(note.instance.company, self.company)
        self.assertEqual(note.instance.patient, self.patient)
        self.assertEqual(note.instance.author, self.doctor)
        self.assertTrue(note.is_valid(), note.errors)
        self.assertEqual(note.save().author, self.doctor)

        weight = self.weight_form()
        self.assertEqual(weight.instance.company, self.company)
        self.assertEqual(weight.instance.patient, self.patient)
        self.assertEqual(weight.instance.recorded_by, self.patient_user)
        self.assertTrue(weight.is_valid(), weight.errors)
        self.assertEqual(weight.save().patient, self.patient)

    def test_task_rejects_cross_practice_patient_and_assignee(self):
        for field, value in (('patient', self.other_patient.pk), ('assigned_to', self.other_doctor.pk)):
            with self.subTest(field=field):
                form = ClinicalTaskForm(self.task_data(**{field: value}), company=self.company)
                self.assertFalse(form.is_valid())
                self.assertIn(field, form.errors)
                self.assertEqual(form['description'].value(), 'Keep this text on validation errors.')

    def test_bound_patient_is_hidden_and_optional_in_post(self):
        for form_class, data in (
            (ClinicalTaskForm, self.task_data()),
            (AppointmentForm, self.appointment_data()),
        ):
            with self.subTest(form=form_class.__name__):
                data.pop('patient')
                form = form_class(data, company=self.company, patient=self.patient)
                self.assertTrue(form.fields['patient'].widget.is_hidden)
                self.assertTrue(form.is_valid(), form.errors)
                self.assertEqual(form.save(commit=False).patient, self.patient)

    def test_bound_patient_rejects_tampering_even_with_same_practice_patient(self):
        for form_class, data in (
            (ClinicalTaskForm, self.task_data(patient=self.second_patient.pk)),
            (AppointmentForm, self.appointment_data(patient=self.second_patient.pk)),
        ):
            with self.subTest(form=form_class.__name__):
                form = form_class(data, company=self.company, patient=self.patient)
                self.assertFalse(form.is_valid())
                self.assertIn('patient', form.errors)

    def test_invalid_server_patient_is_reported_as_form_error(self):
        for form in (
            ClinicalNoteForm(
                {'note_type': 'consult', 'body': 'Retain note'},
                company=self.company, patient=self.other_patient, author=self.doctor,
            ),
            WeightEntryForm(
                {'recorded_on': timezone.localdate(), 'weight_kg': '90'},
                company=self.company, patient=self.other_patient, recorded_by=self.patient_user,
            ),
        ):
            with self.subTest(form=type(form).__name__):
                self.assertFalse(form.is_valid())
                self.assertIn('same company', str(form.non_field_errors()))

    def test_appointment_rejects_cross_practice_and_non_doctor_clinicians(self):
        for field, value in (
            ('patient', self.other_patient.pk),
            ('clinician', self.other_doctor.pk),
            ('clinician', self.admin.pk),
            ('clinician', self.inactive_doctor.pk),
        ):
            with self.subTest(field=field, value=value):
                form = AppointmentForm(self.appointment_data(**{field: value}), company=self.company)
                self.assertFalse(form.is_valid())
                self.assertIn(field, form.errors)

    def test_appointment_requires_future_start_and_duration_between_five_and_120(self):
        for field, value in (
            ('starts_at', timezone.now() - timedelta(minutes=1)),
            ('duration_minutes', 4),
            ('duration_minutes', 121),
        ):
            with self.subTest(field=field, value=value):
                form = AppointmentForm(self.appointment_data(**{field: value}), company=self.company)
                self.assertFalse(form.is_valid())
                self.assertIn(field, form.errors)

    def test_appointment_detects_overlap_across_practices(self):
        starts_at = timezone.now() + timedelta(days=2)
        Appointment.objects.create(
            company=self.other_company, patient=self.other_patient, clinician=self.doctor,
            starts_at=starts_at - timedelta(minutes=15), duration_minutes=30,
        )
        form = AppointmentForm(self.appointment_data(starts_at=starts_at), company=self.company)
        self.assertFalse(form.is_valid())
        self.assertIn('already has an appointment', str(form.errors['starts_at']))
        self.assertNotIn(self.other_company.name, str(form.errors))

    def test_appointment_detects_earlier_long_appointment_still_in_progress(self):
        starts_at = timezone.now() + timedelta(days=2)
        Appointment.objects.create(
            company=self.company, patient=self.patient, clinician=self.doctor,
            starts_at=starts_at - timedelta(days=1), duration_minutes=1450,
        )
        form = AppointmentForm(self.appointment_data(starts_at=starts_at), company=self.company)
        self.assertFalse(form.is_valid())
        self.assertIn('starts_at', form.errors)

    def test_appointment_detects_overlap_with_later_booking(self):
        starts_at = timezone.now() + timedelta(days=2)
        Appointment.objects.create(
            company=self.company, patient=self.patient, clinician=self.doctor,
            starts_at=starts_at + timedelta(minutes=15), duration_minutes=15,
        )
        form = AppointmentForm(self.appointment_data(starts_at=starts_at), company=self.company)
        self.assertFalse(form.is_valid())
        self.assertIn('starts_at', form.errors)

    def test_appointment_allows_touching_boundaries_and_ignores_cancelled_and_no_show(self):
        starts_at = timezone.now() + timedelta(days=2)
        for offset, duration, status in (
            (-30, 30, Appointment.Status.BOOKED),
            (30, 15, Appointment.Status.BOOKED),
            (0, 30, Appointment.Status.CANCELLED),
            (0, 30, Appointment.Status.NO_SHOW),
        ):
            Appointment.objects.create(
                company=self.company, patient=self.patient, clinician=self.doctor,
                starts_at=starts_at + timedelta(minutes=offset), duration_minutes=duration, status=status,
            )
        form = AppointmentForm(self.appointment_data(starts_at=starts_at), company=self.company)
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.save().company, self.company)

    def test_updating_an_appointment_does_not_conflict_with_itself(self):
        starts_at = timezone.now() + timedelta(days=2)
        appointment = Appointment.objects.create(
            company=self.company, patient=self.patient, clinician=self.doctor,
            starts_at=starts_at, duration_minutes=30,
        )
        form = AppointmentForm(
            self.appointment_data(starts_at=starts_at), company=self.company, instance=appointment,
        )
        self.assertTrue(form.is_valid(), form.errors)

    def test_weight_duplicate_date_is_a_field_error_and_preserves_input(self):
        WeightEntry.objects.create(
            company=self.company, patient=self.patient, recorded_on=timezone.localdate(), weight_kg='85',
        )
        form = self.weight_form()
        self.assertFalse(form.is_valid())
        self.assertIn('already been recorded', str(form.errors['recorded_on']))
        self.assertEqual(form['note'].value(), 'After breakfast')
        self.assertEqual(WeightEntry.objects.count(), 1)

    def test_weight_date_cannot_be_in_future(self):
        form = self.weight_form(recorded_on=timezone.localdate() + timedelta(days=1))
        self.assertFalse(form.is_valid())
        self.assertIn('recorded_on', form.errors)

    def test_weight_range_is_validated_on_server(self):
        for weight in ('19.9', '400.1'):
            with self.subTest(weight=weight):
                form = self.weight_form(weight_kg=weight)
                self.assertFalse(form.is_valid())
                self.assertIn('weight_kg', form.errors)
        for weight in ('20', '400'):
            with self.subTest(weight=weight):
                form = self.weight_form(weight_kg=weight)
                self.assertTrue(form.is_valid(), form.errors)
