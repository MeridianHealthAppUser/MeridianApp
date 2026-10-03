"""Access role and clinician type are separate: a Super Admin can be a doctor, a dietitian cannot prescribe."""

from datetime import date
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.test import TestCase, override_settings
from django.urls import reverse

from care.checkout import consultation_slots
from care.models import AuditEvent, ClinicalTask
from care.patient_assignment import active_doctors
from care.task_services import visible_tasks
from practices.management_forms import MembershipForm
from practices.management_services import update_membership
from practices.models import Company, CompanyMembership, Patient
from practices.services import ACTIVE_COMPANY_SESSION_KEY


@override_settings(MULTI_PRACTICE_ENABLED=True)
class ClinicianTypeTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.company = Company.objects.create(name='Clinician Types', slug='clinician-types')
        people = {
            'doctor': ('Ada', 'doctor', 'doctor'),
            'dietitian': ('Bea', 'doctor', 'dietitian'),
            'super_doctor': ('Cal', 'super_admin', 'doctor'),
            'super_admin': ('Dee', 'super_admin', ''),
            'admin': ('Eve', 'practice_admin', ''),
        }
        for name, (first_name, role, clinician_type) in people.items():
            user = User.objects.create_user(email=f'{name}@clinician-types.test', first_name=first_name, last_name='Staff')
            setattr(cls, name, user)
            setattr(cls, f'{name}_membership', CompanyMembership.objects.create(
                company=cls.company, user=user, role=role, clinician_type=clinician_type))
        cls.patient = Patient.objects.create(company=cls.company, first_name='Pat', last_name='Patient')

    def login(self, user):
        self.client.force_login(user)
        session = self.client.session
        session[ACTIVE_COMPANY_SESSION_KEY] = self.company.pk
        session.save()

    def test_titles_and_capabilities_follow_role_and_type(self):
        expected = {
            'doctor': ('Doctor', True, True, True),
            'dietitian': ('Dietitian', True, False, True),
            'super_doctor': ('Super admin · Doctor', True, True, True),
            'super_admin': ('Super admin', False, False, True),
            'admin': ('Practice administrator', False, False, False),
        }
        for name, (title, clinician, prescriber, clinical_access) in expected.items():
            with self.subTest(name=name):
                membership = getattr(self, f'{name}_membership')
                self.assertEqual((membership.title, membership.is_clinician, membership.is_prescriber, membership.has_clinical_access),
                                 (title, clinician, prescriber, clinical_access))

    def test_clinician_access_defaults_to_doctor_and_practice_admins_are_never_clinicians(self):
        user = get_user_model().objects.create_user(email='legacy@clinician-types.test')
        membership = CompanyMembership.objects.create(company=self.company, user=user, role='doctor')
        self.assertEqual(membership.clinician_type, 'doctor')
        membership.role = 'practice_admin'
        membership.save(update_fields=('role', 'updated_at'))
        membership.refresh_from_db()
        self.assertEqual(membership.clinician_type, '')
        with self.assertRaises(IntegrityError), transaction.atomic():
            CompanyMembership.objects.filter(pk=membership.pk).update(clinician_type='dietitian')

    def test_every_clinician_can_be_assigned_including_a_super_admin_doctor(self):
        self.assertEqual(list(active_doctors(self.company)), [self.doctor, self.dietitian, self.super_doctor])

    def test_super_admin_doctor_does_clinical_work_and_keeps_admin_visibility(self):
        task = ClinicalTask.objects.create(company=self.company, patient=self.patient, title='Someone else’s task', assigned_to=self.doctor)
        self.assertIn(task, visible_tasks(self.company, self.super_doctor, self.super_doctor_membership))
        self.login(self.super_doctor)
        for name in ('clinical-consultation-create', 'clinical-lab-create', 'treatment-authorisation-create'):
            with self.subTest(page=name):
                self.assertEqual(self.client.get(reverse(f'portal:{name}', args=[self.patient.pk])).status_code, 200)
        self.assertContains(self.client.get(reverse('portal:desktop-dashboard')), 'Super admin · Doctor')

    def test_super_admin_without_a_type_reads_but_does_not_write_clinical_records(self):
        self.login(self.super_admin)
        for name in ('clinical-consultation-create', 'clinical-lab-create', 'treatment-authorisation-create'):
            with self.subTest(page=name):
                self.assertEqual(self.client.get(reverse(f'portal:{name}', args=[self.patient.pk])).status_code, 403)
        self.assertEqual(self.client.get(reverse('portal:clinical-consultations')).status_code, 200)

    def test_dietitian_keeps_clinical_records_but_cannot_do_doctor_only_work(self):
        self.login(self.dietitian)
        self.assertEqual(self.client.get(reverse('portal:clinical-consultation-create', args=[self.patient.pk])).status_code, 200)
        for name in ('clinical-lab-create', 'treatment-authorisation-create'):
            with self.subTest(page=name):
                self.assertEqual(self.client.get(reverse(f'portal:{name}', args=[self.patient.pk])).status_code, 403)
        workspace = self.client.get(reverse('portal:patient-detail', args=[self.patient.pk]), {'tab': 'blood-tests'})
        self.assertNotContains(workspace, reverse('portal:clinical-lab-create', args=[self.patient.pk]))

    def test_initial_consultations_at_checkout_are_offered_by_doctors_only(self):
        with patch('care.checkout.open_slots_for_day', return_value=[]) as slots:
            consultation_slots(self.company, date(2030, 1, 7))
        self.assertEqual(set(slots.call_args.kwargs['clinicians']), {self.doctor, self.super_doctor})

    def test_staff_form_requires_a_type_for_clinician_access_and_none_for_practice_admins(self):
        cases = (
            ({'role': 'doctor', 'clinician_type': '', 'is_active': 'on'}, False),
            ({'role': 'doctor', 'clinician_type': 'dietitian', 'is_active': 'on'}, True),
            ({'role': 'practice_admin', 'clinician_type': 'doctor', 'is_active': 'on'}, False),
            ({'role': 'super_admin', 'clinician_type': 'doctor', 'is_active': 'on'}, True),
            ({'role': 'super_admin', 'clinician_type': '', 'is_active': 'on'}, True),
        )
        for data, valid in cases:
            with self.subTest(data=data):
                self.assertEqual(MembershipForm(data).is_valid(), valid)

    def test_super_admin_can_be_made_a_doctor_with_an_audit_trail(self):
        updated = update_membership(actor=self.super_doctor, company=self.company, membership=self.super_admin_membership,
                                    role='super_admin', is_active=True, clinician_type='doctor')
        self.assertEqual((updated.role, updated.clinician_type), ('super_admin', 'doctor'))
        event = AuditEvent.objects.get(action='staff.membership_updated', target_id=str(updated.pk))
        self.assertEqual((event.metadata['changed_fields'], event.metadata['previous_clinician_type'], event.metadata['clinician_type']),
                         (['clinician_type'], '', 'doctor'))
        self.assertIn(self.super_admin, active_doctors(self.company))
