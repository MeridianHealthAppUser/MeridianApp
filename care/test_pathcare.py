"""PathCare blood-test request forms: filled from the record, flattened, stored and emailed to the patient."""

import io
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core import mail
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase, override_settings
from django.utils import timezone
from pypdf import PdfReader

from practices.models import Company, CompanyMembership, Patient

from .models import AuditEvent, LabRequest, PathCareRequestForm, PharmacyOrder
from .patient_files import read_patient_file
from .pathcare import (
    BLANK_FORM, FIELDS, fill_form, fit_text, form_summary, form_values, request_blood_tests, resend_pathcare_form,
)

DEMO_TEXT = ("Doctor's Name", "pathcare code", 'Patient ID number', 'Text entered into the', 'Patient first name', '5369')


@override_settings(MULTI_PRACTICE_ENABLED=True)
class PathCareFormTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        users = get_user_model().objects
        cls.company = Company.objects.create(name='Meridian Health', slug='pathcare-practice')
        cls.doctor = users.create_user(email='pathcare-doctor@example.test', first_name='Sam', last_name='Marchant',
                                       pathcare_name='Marchant,Dr S', pathcare_code='MARSA0')
        cls.no_code = users.create_user(email='pathcare-nocode@example.test', first_name='Joshua', last_name='Czech')
        cls.dietitian = users.create_user(email='pathcare-dietitian@example.test', pathcare_name='X,DR Y', pathcare_code='XY0')
        cls.admin = users.create_user(email='pathcare-admin@example.test')
        for user, role, kind in ((cls.doctor, 'doctor', 'doctor'), (cls.no_code, 'doctor', 'doctor'),
                                 (cls.dietitian, 'doctor', 'dietitian'), (cls.admin, 'practice_admin', '')):
            CompanyMembership.objects.create(company=cls.company, user=user, role=role, clinician_type=kind)
        cls.patient_user = users.create_user(email='nadia@example.test', first_name='Nadia', last_name='Mokoena')
        cls.patient = Patient.objects.create(company=cls.company, user=cls.patient_user, first_name='Nadia', last_name='Mokoena',
                                             id_number='920415 0123 083', title=Patient.Title.MS)
        PharmacyOrder.objects.create(company=cls.company, patient=cls.patient, status='submitted', submitted_at=timezone.now(),
                                     delivery_address={'line1': '14 Kloof Nek Road', 'line2': 'Tamboerskloof', 'city': 'Cape Town',
                                                       'province': 'Western Cape', 'postal_code': '8001', 'phone': '082'})

    def request(self, **values):
        options = {'company': self.company, 'patient': self.patient, 'actor': self.doctor, 'tests': 'FBC, ferritin, TSH'}
        options.update(values)
        with self.captureOnCommitCallbacks(execute=True):
            return request_blood_tests(**options)

    def test_the_stored_blank_form_has_no_demo_values(self):
        fields = PdfReader(BLANK_FORM).get_fields()
        self.assertEqual(len(fields), 461)
        self.assertEqual([name for name in FIELDS if fields[name].get('/V')], [])

    def test_only_the_listed_fields_are_filled_from_the_record(self):
        values = form_values(company=self.company, patient=self.patient, doctor=self.doctor, tests='FBC, ferritin, TSH')
        self.assertEqual(values, {
            'Ref doctor': 'MARCHANT,DR S', 'Ref doctor Code': 'MARSA0', 'PID': '9204150123083',
            'Patient Surname': 'MOKOENA', 'Patient First Name': 'NADIA', 'Patient Title': 'MS',
            'Postal Address 1': '14 Kloof Nek Road', 'Postal Address 2': 'Tamboerskloof, Cape Town',
            'Postal Address 3': '8001', 'Medical Aid No': '', 'CDD': 'FBC, ferritin, TSH',
        })
        summary = {row['label']: row for row in form_summary(values)}
        self.assertFalse(any(row['missing'] for row in summary.values()))
        bare = Patient.objects.create(company=self.company, first_name='A', last_name='B')
        missing = form_summary(form_values(company=self.company, patient=bare, doctor=self.no_code))
        self.assertEqual([row['label'] for row in missing if row['missing']],
                         ['Referring doctor', 'PathCare code', 'Patient ID', 'Title', 'Postal address'])
        self.assertTrue(all(row['blocking'] for row in missing if row['label'] in ('Referring doctor', 'PathCare code')))

    def test_the_filled_form_is_flattened_with_the_values_and_no_leftover_text(self):
        content = fill_form(form_values(company=self.company, patient=self.patient, doctor=self.doctor, tests='FBC, ferritin, TSH'))
        reader = PdfReader(io.BytesIO(content))
        self.assertIsNone(reader.get_fields())
        self.assertFalse(reader.pages[0].get('/Annots'))
        text = reader.pages[0].extract_text()
        for value in ('MARCHANT,DR S', 'MARSA0', '9204150123083', 'MOKOENA', 'NADIA', '14 Kloof Nek Road', '8001', 'FBC, ferritin, TSH'):
            self.assertIn(value, text)
        for demo in DEMO_TEXT:
            self.assertNotIn(demo, text)

    def test_long_tests_wrap_and_shrink_to_fit_the_box_or_are_refused(self):
        size, laid_out = fit_text('FBC, ferritin, TSH, fasting lipogram, HbA1c, U&E and creatinine. '
                                  'On semaglutide 1.0 mg weekly.', 84, 51, multiline=True)
        self.assertLess(size, 9)
        self.assertGreater(laid_out.count('\n'), 1)
        self.assertIsNone(fit_text('word ' * 80, 84, 51, multiline=True))
        with self.assertRaisesMessage(ValidationError, 'too long'):
            fill_form({'CDD': 'word ' * 59})

    def test_a_request_stores_the_form_logs_it_and_emails_it_to_the_patient(self):
        form = self.request(tests='FBC, ferritin, TSH')
        lab = form.lab_request
        self.assertEqual((lab.requested_by_id, lab.panel_name, lab.patient_id), (self.doctor.pk, 'FBC, ferritin, TSH', self.patient.pk))
        form.refresh_from_db()
        self.assertEqual((form.email_status, form.email_to, form.email_attempts, form.send_count), ('sent', 'nadia@example.test', 1, 1))
        self.assertTrue(read_patient_file(form.file).startswith(b'%PDF-'))
        self.assertEqual(len(mail.outbox), 1)
        email = mail.outbox[0]
        self.assertEqual(email.to, ['nadia@example.test'])
        self.assertEqual(email.subject, 'Your blood test request from Meridian Health')
        self.assertIn('Dr Marchant has requested blood tests', email.body)
        self.assertIn('any PathCare collection point', email.body)
        name, attachment, mimetype = email.attachments[0]
        self.assertEqual((name, mimetype), ('pathcare-blood-test-request.pdf', 'application/pdf'))
        self.assertEqual(attachment, read_patient_file(form.file))
        self.assertEqual(set(AuditEvent.objects.filter(patient=self.patient, action__startswith='pathcare_form.').values_list('action', flat=True)),
                         {'pathcare_form.created', 'pathcare_form.emailed'})

    def test_a_repeated_submission_sends_nothing_twice(self):
        key = '3f1c8c55-1f1a-4b3e-9a1f-5f2d2a6c0b1e'
        first = self.request(submission_key=key)
        second = self.request(submission_key=key)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual((PathCareRequestForm.objects.count(), len(mail.outbox)), (1, 1))

    def test_a_failed_email_is_recorded_and_can_be_resent(self):
        with patch('django.core.mail.EmailMessage.send', side_effect=OSError('mail server down')):
            form = self.request()
        form.refresh_from_db()
        self.assertEqual(form.email_status, 'failed')
        self.assertTrue(AuditEvent.objects.filter(action='pathcare_form.email_failed').exists())
        with self.captureOnCommitCallbacks(execute=True):
            resend_pathcare_form(form=form, actor=self.doctor)
        form.refresh_from_db()
        self.assertEqual((form.email_status, form.send_count, len(mail.outbox)), ('sent', 2, 1))
        self.assertEqual(mail.outbox[0].attachments[0][1], read_patient_file(form.file))
        self.assertTrue(AuditEvent.objects.filter(action='pathcare_form.resent').exists())

    def test_without_email_set_up_the_form_is_kept_marked_not_emailed_and_can_be_resent_later(self):
        with override_settings(EMAIL_ENABLED=False), self.assertLogs('care.pathcare', 'WARNING'):
            form = self.request()
        form.refresh_from_db()
        self.assertEqual((form.email_status, form.email_attempts, len(mail.outbox)), ('failed', 0, 0))
        self.assertTrue(AuditEvent.objects.filter(action='pathcare_form.email_failed').exists())
        with self.captureOnCommitCallbacks(execute=True):
            resend_pathcare_form(form=form, actor=self.doctor)
        form.refresh_from_db()
        self.assertEqual((form.email_status, len(mail.outbox)), ('sent', 1))

    def test_only_a_doctor_with_pathcare_details_can_request_and_the_patient_needs_an_email(self):
        for actor in (self.dietitian, self.admin):
            with self.subTest(actor=actor.email):
                with self.assertRaises(PermissionDenied):
                    self.request(actor=actor)
        with self.assertRaisesMessage(ValidationError, 'Ask the practice to add them'):
            self.request(actor=self.no_code)
        with self.assertRaises(ValidationError):
            self.request(tests='   ')
        no_email = Patient.objects.create(company=self.company, first_name='No', last_name='Email')
        with self.assertRaisesMessage(ValidationError, 'no email address'):
            self.request(patient=no_email)
        self.assertFalse(LabRequest.objects.exists())
        self.assertEqual(len(mail.outbox), 0)
