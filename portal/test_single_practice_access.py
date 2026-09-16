"""The deployment boundary also protects global reports and direct video URLs."""

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from care.models import Appointment, AuditEvent, PatientEvent
from care.privacy import own_patient, require_privacy_admin
from care.reporting import report_companies
from practices.models import Company, CompanyMembership, Patient
from practices.services import ACTIVE_COMPANY_SESSION_KEY
from video.access import VideoAccessDenied, attach_video_join, resolve_room_access

from .privacy_forms import AccessHistoryFilterForm
from .record_forms import ClinicalRecordFilterForm, ScopedPatientDirectoryFilterForm
from .record_views import scoped_source, staff_memberships
from .reporting_views import ReportPeriodForm


@override_settings(DEBUG=True, MULTI_PRACTICE_ENABLED=False, SINGLE_PRACTICE_SLUG='single-access', VIDEO_ENABLED=True,
                   VIDEO_JOIN_EARLY_MINUTES=5, VIDEO_JOIN_GRACE_MINUTES=0)
class SinglePracticeAccessTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name='Allowed Access', slug='single-access')
        cls.other = Company.objects.create(name='Disabled Access', slug='disabled-access')
        cls.doctor = get_user_model().objects.create_user(email='single-doctor@example.test')
        cls.admin = get_user_model().objects.create_user(email='single-admin@example.test')
        cls.patient_user = get_user_model().objects.create_user(email='single-patient@example.test')
        cls.now = timezone.now()
        for company in (cls.company, cls.other):
            CompanyMembership.objects.create(company=company, user=cls.doctor, role='doctor')
            CompanyMembership.objects.create(company=company, user=cls.admin, role='super_admin')
        cls.patient = Patient.objects.create(company=cls.company, user=cls.patient_user,
            first_name='Allowed', last_name='Patient', assigned_doctor=cls.doctor)
        cls.other_patient = Patient.objects.create(company=cls.other, user=cls.patient_user,
            first_name='Disabled', last_name='Patient', assigned_doctor=cls.doctor)
        cls.event = PatientEvent.objects.create(company=cls.company, patient=cls.patient,
            title='Allowed patient history', category='clinical', is_patient_visible=True)
        cls.other_event = PatientEvent.objects.create(company=cls.other, patient=cls.other_patient,
            title='DISABLED_PRACTICE_HISTORY', category='clinical', is_patient_visible=True)
        cls.audit = AuditEvent.objects.create(company=cls.company, actor=cls.doctor, action='patient.record_viewed')
        cls.other_audit = AuditEvent.objects.create(company=cls.other, actor=cls.doctor, action='patient.record_viewed')
        cls.appointment = Appointment.objects.create(company=cls.company, patient=cls.patient,
            clinician=cls.doctor, starts_at=cls.now, duration_minutes=30)
        cls.other_appointment = Appointment.objects.create(company=cls.other, patient=cls.other_patient,
            clinician=cls.doctor, starts_at=cls.now, duration_minutes=30)

    def setUp(self):
        self.client.force_login(self.doctor)
        session = self.client.session
        session[ACTIVE_COMPANY_SESSION_KEY] = self.company.pk
        session.save()

    def test_memberships_and_timeline_sources_exclude_disabled_practice(self):
        self.assertEqual(set(staff_memberships(self.doctor)), {self.company.pk})
        self.assertEqual(list(scoped_source(PatientEvent, [self.patient, self.other_patient])), [self.event])

    def test_scope_fields_are_hidden_and_forged_all_is_invalid(self):
        today = timezone.localdate().isoformat()
        cases = (
            (ClinicalRecordFilterForm, {'category': 'all', 'reason': 'direct_care'}, {}),
            (ScopedPatientDirectoryFilterForm, {}, {'company': self.company, 'companies': [self.company, self.other]}),
            (ReportPeriodForm, {'start': today, 'end': today}, {}),
            (AccessHistoryFilterForm, {}, {}),
        )
        for form_class, values, kwargs in cases:
            with self.subTest(form=form_class.__name__):
                current = form_class({**values, 'scope': 'current'}, **kwargs)
                self.assertTrue(current.is_valid(), current.errors)
                self.assertTrue(current.fields['scope'].widget.is_hidden)
                forged = form_class({**values, 'scope': 'all'}, **kwargs)
                self.assertFalse(forged.is_valid())
                self.assertIn('scope', forged.errors)

    def test_directory_and_history_do_not_expose_other_practice(self):
        for url in (reverse('portal:patient-list'),
                    reverse('portal:staff-patient-record', args=[self.patient.pk])):
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200)
            self.assertNotContains(response, 'Disabled Access')
            self.assertNotContains(response, 'DISABLED_PRACTICE_HISTORY')

    def test_direct_disabled_patient_records_and_exports_are_rejected(self):
        for name in ('patient-detail', 'staff-patient-record', 'staff-patient-record-export',
                     'staff-patient-record-history', 'staff-patient-record-excel'):
            response = self.client.get(reverse('portal:' + name, args=[self.other_patient.pk]))
            self.assertIn(response.status_code, (404, 409))
            self.assertNotIn(b'DISABLED_PRACTICE_HISTORY', response.content)

    def test_all_scope_export_is_rejected_without_cross_practice_audit(self):
        before = AuditEvent.objects.filter(company=self.other).count()
        response = self.client.get(reverse('portal:staff-patient-record-export', args=[self.patient.pk]),
                                   {'scope': 'all', 'reason': 'direct_care'})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(AuditEvent.objects.filter(company=self.other).count(), before)

    def test_old_multi_practice_history_token_cannot_be_reused(self):
        with override_settings(MULTI_PRACTICE_ENABLED=True):
            response = self.client.get(reverse('portal:staff-patient-record', args=[self.patient.pk]),
                {'scope': 'all', 'reason': 'direct_care'})
            download_url = response.context['timeline_download_url']
        response = self.client.get(download_url)
        self.assertEqual(response.status_code, 409)
        self.assertNotIn(b'DISABLED_PRACTICE_HISTORY', response.content)

    def test_report_service_rejects_all_and_disabled_company(self):
        self.assertEqual(report_companies(self.doctor, self.company, 'current'), [self.company.pk])
        for company, scope in ((self.company, 'all'), (self.other, 'current')):
            with self.subTest(company=company.pk, scope=scope), self.assertRaises(PermissionDenied):
                report_companies(self.doctor, company, scope)

    def test_report_export_rejects_forged_all(self):
        response = self.client.get(reverse('portal:metrics-export'), {'scope': 'all'})
        self.assertEqual(response.status_code, 400)
        self.assertNotContains(response, 'Disabled Access', status_code=400)

    def test_own_access_history_is_pinned(self):
        response = self.client.get(reverse('portal:account-access-history'))
        self.assertEqual(response.status_code, 200)
        self.assertIn(self.audit, list(response.context['page_obj']))
        self.assertNotIn(self.other_audit, list(response.context['page_obj']))
        response = self.client.get(reverse('portal:account-access-history'), {'scope': 'all'})
        self.assertFalse(response.context['filter_form'].is_valid())
        self.assertEqual(list(response.context['page_obj']), [])

    def test_privacy_service_authority_does_not_bypass_deployment_boundary(self):
        require_privacy_admin(self.admin, self.company)
        own_patient(self.patient_user, self.company, self.patient)
        with self.assertRaises(PermissionDenied):
            require_privacy_admin(self.admin, self.other)
        with self.assertRaises(PermissionDenied):
            own_patient(self.patient_user, self.other, self.other_patient)

    def test_video_policy_rejects_disabled_room_for_both_participants(self):
        for actor in (self.doctor, self.patient_user):
            self.assertEqual(resolve_room_access(actor.pk, self.appointment.pk, now=self.now).company_id,
                             self.company.pk)
            for require_window in (True, False):
                with self.subTest(actor=actor.pk, window=require_window), self.assertRaises(VideoAccessDenied):
                    resolve_room_access(actor.pk, self.other_appointment.pk, now=self.now,
                                        require_window=require_window)

    def test_video_links_are_not_added_for_disabled_practice(self):
        rows = attach_video_join([self.appointment, self.other_appointment], self.doctor.pk,
                                  allowed_role='doctor', now=self.now)
        self.assertTrue(rows[0].video_room_url)
        self.assertIsNone(rows[1].video_room_url)
        self.assertEqual(self.client.get(reverse('video:room', args=[self.other_appointment.pk])).status_code, 404)

    @override_settings(MULTI_PRACTICE_ENABLED=True)
    def test_explicitly_reenabling_mode_preserves_existing_access(self):
        self.assertEqual(set(staff_memberships(self.doctor)), {self.company.pk, self.other.pk})
        self.assertEqual(set(report_companies(self.doctor, self.company, 'all')), {self.company.pk, self.other.pk})
        self.assertEqual(resolve_room_access(self.doctor.pk, self.other_appointment.pk, now=self.now).company_id,
                         self.other.pk)
