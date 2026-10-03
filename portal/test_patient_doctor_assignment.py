"""The care team assigns a patient's clinician from the workspace Overview without losing history."""

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from care.models import Appointment, AuditEvent, ClinicalEncounter, ClinicalNote, ClinicalTask, LabRequest
from practices.models import Company, CompanyMembership, Patient
from practices.services import ACTIVE_COMPANY_SESSION_KEY


@override_settings(MULTI_PRACTICE_ENABLED=True)
class PatientDoctorAssignmentTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.company = Company.objects.create(name='Assignment Practice', slug='assignment-practice')
        cls.other_company = Company.objects.create(name='Other Assignment Practice', slug='other-assignment-practice')
        cls.doctor = User.objects.create_user(email='assign-doctor@example.test', first_name='Sam', last_name='Doctor')
        cls.second_doctor = User.objects.create_user(email='assign-second@example.test', first_name='Tess', last_name='Doctor')
        cls.inactive_doctor = User.objects.create_user(email='assign-inactive@example.test', first_name='Ina', last_name='Doctor')
        cls.foreign_doctor = User.objects.create_user(email='assign-foreign@example.test', first_name='Fay', last_name='Doctor')
        cls.admin = User.objects.create_user(email='assign-admin@example.test')
        cls.super_admin = User.objects.create_user(email='assign-super@example.test')
        for user, role in ((cls.doctor, 'doctor'), (cls.second_doctor, 'doctor'), (cls.admin, 'practice_admin'), (cls.super_admin, 'super_admin')):
            CompanyMembership.objects.create(company=cls.company, user=user, role=role)
        CompanyMembership.objects.create(company=cls.company, user=cls.inactive_doctor, role='doctor', is_active=False)
        CompanyMembership.objects.create(company=cls.other_company, user=cls.foreign_doctor, role='doctor')
        CompanyMembership.objects.create(company=cls.other_company, user=cls.admin, role='practice_admin')
        cls.patient = Patient.objects.create(company=cls.company, first_name='Dana', last_name='Patient')
        cls.foreign_patient = Patient.objects.create(company=cls.other_company, first_name='Eli', last_name='Patient')

    def login(self, actor, company=None):
        self.client.force_login(actor)
        session = self.client.session
        session[ACTIVE_COMPANY_SESSION_KEY] = (company or self.company).pk
        session.save()

    def overview(self, patient=None):
        return self.client.get(reverse('portal:patient-detail', args=[(patient or self.patient).pk]), {'tab': 'overview'})

    def assign(self, doctor, patient=None, token=None):
        patient = patient or self.patient
        if token is None:
            token = self.overview(patient).context['doctor_assignment_context']
        return self.client.post(reverse('portal:patient-doctor-assign', args=[patient.pk]),
                                {'doctor': doctor.pk if doctor else '', 'workflow_context': token})

    def test_care_team_sees_only_active_clinicians_in_this_practice(self):
        for actor in (self.doctor, self.admin, self.super_admin):
            with self.subTest(actor=actor.email):
                self.login(actor)
                response = self.overview()
                self.assertContains(response, '<dt>Assigned clinician</dt>')
                self.assertContains(response, 'Assign a clinician')
                self.assertEqual(list(response.context['doctor_form'].fields['doctor'].queryset), [self.doctor, self.second_doctor])

    def test_doctor_can_hand_a_patient_to_a_colleague(self):
        self.login(self.doctor)
        self.assign(self.doctor)
        self.assign(self.second_doctor)
        self.patient.refresh_from_db()
        self.assertEqual(self.patient.assigned_doctor, self.second_doctor)
        self.assertEqual(AuditEvent.objects.filter(action='patient.doctor_assigned', actor=self.doctor).count(), 2)

    def test_admin_assigns_changes_and_clears_the_doctor_with_an_audit_trail(self):
        self.login(self.admin)
        response = self.assign(self.doctor)
        self.assertRedirects(response, reverse('portal:patient-detail', args=[self.patient.pk]) + '?tab=overview', fetch_redirect_response=False)
        self.patient.refresh_from_db()
        self.assertEqual(self.patient.assigned_doctor, self.doctor)
        self.assertContains(self.overview(), 'Sam Doctor')

        self.assign(self.second_doctor)
        self.assign(None)
        self.patient.refresh_from_db()
        self.assertIsNone(self.patient.assigned_doctor)
        events = AuditEvent.objects.filter(company=self.company, patient=self.patient, action='patient.doctor_assigned').order_by('pk')
        self.assertEqual([(event.actor, event.metadata) for event in events], [
            (self.admin, {'previous_doctor_id': None, 'doctor_id': self.doctor.pk}),
            (self.admin, {'previous_doctor_id': self.doctor.pk, 'doctor_id': self.second_doctor.pk}),
            (self.admin, {'previous_doctor_id': self.second_doctor.pk, 'doctor_id': None}),
        ])
        history = self.client.get(reverse('portal:patient-detail', args=[self.patient.pk]), {'tab': 'history'})
        self.assertContains(history, 'Assigned clinician changed', count=3)

    def test_saving_the_same_doctor_records_nothing(self):
        self.login(self.admin)
        self.assign(self.doctor)
        self.assign(self.doctor)
        self.assertEqual(AuditEvent.objects.filter(action='patient.doctor_assigned').count(), 1)

    def test_a_deactivated_team_member_cannot_assign_with_an_open_form(self):
        self.login(self.doctor)
        token = self.overview().context['doctor_assignment_context']
        CompanyMembership.objects.filter(company=self.company, user=self.doctor).update(is_active=False)
        response = self.assign(self.second_doctor, token=token)
        self.assertEqual(response.status_code, 403)
        self.patient.refresh_from_db()
        self.assertIsNone(self.patient.assigned_doctor)

    def test_reassignment_keeps_history_and_the_new_clinician_sees_it(self):
        Patient.objects.filter(pk=self.patient.pk).update(assigned_doctor=self.doctor)
        now = timezone.now()
        consultation = ClinicalEncounter.objects.create(company=self.company, patient=self.patient, clinician=self.doctor,
            clinical_summary='Signed by the previous clinician.', status='signed', signed_at=now, signed_by=self.doctor,
            occurred_at=now - timedelta(days=7))
        note = ClinicalNote.objects.create(company=self.company, patient=self.patient, author=self.doctor, body='PREVIOUS_CLINICIAN_NOTE')
        appointment = Appointment.objects.create(company=self.company, patient=self.patient, clinician=self.doctor,
            starts_at=now - timedelta(days=7), duration_minutes=30)
        lab = LabRequest.objects.create(company=self.company, patient=self.patient, requested_by=self.doctor, panel_name='PREVIOUS_PANEL')
        task = ClinicalTask.objects.create(company=self.company, patient=self.patient, title='PREVIOUS_TASK',
            assigned_to=self.doctor, created_by=self.doctor)

        self.login(self.admin)
        self.assign(self.second_doctor)
        self.login(self.second_doctor)
        detail = reverse('portal:patient-detail', args=[self.patient.pk])
        for tab, record in (('consultations', consultation), ('notes', note), ('appointments', appointment),
                            ('blood-tests', lab), ('tasks', task)):
            with self.subTest(tab=tab):
                response = self.client.get(detail, {'tab': tab})
                self.assertIn(record.pk, [row.pk for row in response.context['rows']])
        self.assertContains(self.client.get(detail, {'tab': 'notes'}), 'PREVIOUS_CLINICIAN_NOTE')

        # Records stay attributed to whoever created them.
        for record, field in ((consultation, 'clinician'), (note, 'author'), (appointment, 'clinician'),
                              (lab, 'requested_by'), (task, 'assigned_to')):
            record.refresh_from_db()
            self.assertEqual(getattr(record, field), self.doctor)

    def test_inactive_and_other_practice_doctors_are_rejected(self):
        self.login(self.admin)
        for doctor in (self.inactive_doctor, self.foreign_doctor):
            with self.subTest(doctor=doctor.email):
                response = self.assign(doctor)
                self.assertEqual(response.status_code, 400)
                self.assertContains(response, 'errorlist', status_code=400)
                self.assertTrue(response.context['doctor_form'].errors)
        self.patient.refresh_from_db()
        self.assertIsNone(self.patient.assigned_doctor)

    def test_patient_in_another_practice_is_not_found(self):
        self.login(self.admin)
        response = self.client.post(reverse('portal:patient-doctor-assign', args=[self.foreign_patient.pk]),
                                    {'doctor': self.doctor.pk, 'workflow_context': 'x'})
        self.assertEqual(response.status_code, 404)

    def test_stale_form_does_not_overwrite_a_newer_assignment(self):
        self.login(self.admin)
        stale = self.overview().context['doctor_assignment_context']
        self.assign(self.doctor)
        response = self.assign(self.second_doctor, token=stale)
        self.assertEqual(response.status_code, 400)
        self.assertContains(response, 'changed in another tab', status_code=400)
        self.patient.refresh_from_db()
        self.assertEqual(self.patient.assigned_doctor, self.doctor)

    def test_missing_or_tampered_context_is_rejected(self):
        self.login(self.admin)
        response = self.assign(self.doctor, token='tampered')
        self.assertEqual(response.status_code, 400)
        self.patient.refresh_from_db()
        self.assertIsNone(self.patient.assigned_doctor)
