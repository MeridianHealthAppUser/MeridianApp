"""Export record: the record as an Excel workbook, one sheet per tab, with the record's export limits."""

from datetime import date, timedelta
from io import BytesIO
from xml.etree import ElementTree
from zipfile import ZipFile

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from care.messaging import add_participant
from care.models import (
    Appointment, AuditEvent, ClinicalEncounter, ClinicalNote, MessageThread, Payment, PatientMessage, PracticeSettings,
    WeightEntry,
)
from practices.models import Company, CompanyMembership, Patient
from practices.services import ACTIVE_COMPANY_SESSION_KEY


NAMESPACE = {'x': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
SHEETS = ['Overview', 'History', 'Appointments', 'Consultations', 'Blood tests', 'Notes', 'Weight', 'Messages',
          'Tasks', 'Payments', 'Treatment', 'Deliveries', 'Export details']


@override_settings(MULTI_PRACTICE_ENABLED=True)
class RecordWorkbookTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.company = Company.objects.create(name='Workbook Practice', slug='workbook-practice')
        cls.doctor = User.objects.create_user('wb-doctor@example.test', first_name='Sam', last_name='Marchant')
        cls.colleague = User.objects.create_user('wb-colleague@example.test', first_name='Josh', last_name='Czech')
        cls.admin = User.objects.create_user('wb-admin@example.test', first_name='Pat', last_name='Admin')
        for user, role in ((cls.doctor, 'doctor'), (cls.colleague, 'doctor'), (cls.admin, 'practice_admin')):
            CompanyMembership.objects.create(company=cls.company, user=user, role=role)
        patient_user = User.objects.create_user('wb-patient@example.test', first_name='Nadia', last_name='Mokoena')
        cls.patient = Patient.objects.create(company=cls.company, user=patient_user, first_name='Nadia', last_name='Mokoena',
                                             medical_record_number='MH-001', assigned_doctor=cls.doctor)
        now = timezone.now()
        ClinicalEncounter.objects.create(company=cls.company, patient=cls.patient, clinician=cls.doctor, status='signed',
                                         occurred_at=now - timedelta(days=2), clinical_summary='SIGNED_CONSULT_NOTE',
                                         signed_at=now - timedelta(days=2), signed_by=cls.doctor)
        ClinicalEncounter.objects.create(company=cls.company, patient=cls.patient, clinician=cls.doctor,
                                         clinical_summary='UNSIGNED_DRAFT_NOTE')
        ClinicalNote.objects.create(company=cls.company, patient=cls.patient, author=cls.doctor, body='SHARED_GENERAL_NOTE')
        ClinicalNote.objects.create(company=cls.company, patient=cls.patient, author=cls.doctor, body='PRIVATE_NOTE', is_private=True)
        WeightEntry.objects.create(company=cls.company, patient=cls.patient, recorded_on=date(2026, 9, 1), weight_kg='92.40')
        WeightEntry.objects.create(company=cls.company, patient=cls.patient, recorded_on=date(2026, 10, 1), weight_kg='89.10')
        Appointment.objects.create(company=cls.company, patient=cls.patient, clinician=cls.doctor, starts_at=now + timedelta(days=1))
        Payment.objects.create(company=cls.company, patient=cls.patient, amount='630.00', due_on=date(2026, 10, 1),
                               provider_reference='PROVIDER_REFERENCE')
        mine = MessageThread.objects.create(company=cls.company, patient=cls.patient, subject='Your care plan', last_message_at=now)
        add_participant(mine, cls.doctor)
        PatientMessage.objects.create(company=cls.company, thread=mine, sender=patient_user, body='MY_CONVERSATION_MESSAGE')
        theirs = MessageThread.objects.create(company=cls.company, patient=cls.patient, subject='Private to Josh', last_message_at=now)
        add_participant(theirs, cls.colleague)
        PatientMessage.objects.create(company=cls.company, thread=theirs, sender=patient_user, body='SOMEONE_ELSES_MESSAGE')

    def login(self, user):
        self.client.force_login(user)
        session = self.client.session
        session[ACTIVE_COMPANY_SESSION_KEY] = self.company.pk
        session.save()

    def url(self):
        return reverse('portal:staff-patient-record-workbook', args=[self.patient.pk])

    def workbook(self):
        response = self.client.get(self.url())
        self.assertEqual(response.status_code, 200, response.content[:300])
        self.assertEqual(response.headers['Content-Type'], 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
        self.assertEqual(response.headers['Content-Disposition'], f'attachment; filename="patient-{self.patient.pk}-record.xlsx"')
        archive = ZipFile(BytesIO(response.content))
        for name in archive.namelist():
            ElementTree.fromstring(archive.read(name))  # every part is well-formed XML
        names = [sheet.get('name') for sheet in ElementTree.fromstring(archive.read('xl/workbook.xml')).iter(f'{{{NAMESPACE["x"]}}}sheet')]
        sheets = {}
        for number, name in enumerate(names, 1):
            root = ElementTree.fromstring(archive.read(f'xl/worksheets/sheet{number}.xml'))
            sheets[name] = [[''.join(cell.itertext()) for cell in row.findall('x:c', NAMESPACE)] for row in root.iter(f'{{{NAMESPACE["x"]}}}row')]
        return names, sheets, archive

    def test_export_record_button_downloads_the_workbook(self):
        self.login(self.doctor)
        overview = self.client.get(reverse('portal:patient-detail', args=[self.patient.pk]))
        self.assertContains(overview, f'href="{self.url()}">Export record</a>')

    def test_each_tab_is_a_sheet_starting_with_the_overview(self):
        # With the practice's view log on, the download is recorded like the other record exports.
        PracticeSettings.objects.update_or_create(company=self.company, defaults={'store_view_log': True})
        self.login(self.doctor)
        names, sheets, archive = self.workbook()
        self.assertEqual(names, SHEETS)
        overview = [row for row in sheets['Overview'] if row]
        titles = [row[0] for row in overview if row[0] in ('Clinical history', 'Dispensed history', 'Current prescription',
                                                           'Weight over time', 'Summary data')]
        self.assertEqual(titles, ['Clinical history', 'Dispensed history', 'Current prescription', 'Weight over time', 'Summary data'])
        self.assertIn(['Date (SAST)', 'Who', 'Type', 'Entry'], overview)
        consult = next(row for row in overview if 'SIGNED_CONSULT_NOTE' in row)
        self.assertEqual(consult[1:3], ['Sam Marchant', 'Consult note'])
        # Weights are number cells, oldest first, so Excel can chart them.
        weights = overview.index(['Date', 'Weight (kg)'])
        self.assertEqual(overview[weights + 1:weights + 3], [['2026-09-01', '92.40'], ['2026-10-01', '89.10']])
        self.assertIn('<v>92.40</v>', archive.read('xl/worksheets/sheet1.xml').decode())
        self.assertEqual(sheets['Weight'][0], ['Date', 'Weight (kg)', 'Note', 'Recorded by'])
        self.assertEqual(sheets['Appointments'][0], ['Date and time (SAST)', 'Appointment', 'Duration (minutes)', 'Doctor', 'Status'])
        self.assertEqual(sheets['Payments'][1][:2], ['2026-10-01', '630.00'])
        self.assertIn(['Your care plan', 'Open'], [row[:2] for row in sheets['Messages']])
        self.assertEqual(AuditEvent.objects.filter(action='patient.clinical_record_exported', patient=self.patient).count(), 1)

    def test_workbook_keeps_the_record_export_limits(self):
        self.login(self.doctor)
        _names, _sheets, archive = self.workbook()
        content = ''.join(archive.read(name).decode() for name in archive.namelist())
        self.assertIn('SIGNED_CONSULT_NOTE', content)
        self.assertIn('SHARED_GENERAL_NOTE', content)
        self.assertIn('MY_CONVERSATION_MESSAGE', content)
        for excluded in ('UNSIGNED_DRAFT_NOTE', 'PRIVATE_NOTE', 'SOMEONE_ELSES_MESSAGE', 'PROVIDER_REFERENCE', '<f>'):
            self.assertNotIn(excluded, content)

    def test_only_clinical_staff_can_export(self):
        self.login(self.admin)
        self.assertEqual(self.client.get(self.url()).status_code, 403)
        self.assertFalse(AuditEvent.objects.filter(action='patient.clinical_record_exported').exists())
