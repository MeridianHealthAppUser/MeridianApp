"""Dr Czech's 14 Oct record edits: Create note and blood results in the log, Blood results recorded by hand,
the Prescribing doctor following the prescriber, and the weight chart's dose key by medicine."""

from datetime import date, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import RequestFactory, SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from care.models import AuditEvent, BloodResult, ClinicalNote, ClinicianAssignment, MedicationProduct, TreatmentAuthorization
from practices.models import Company, CompanyMembership, Patient
from practices.services import ACTIVE_COMPANY_SESSION_KEY

from .record_chart import doctor_weight_chart
from .workflow_context import make_workflow_context

PDF = b'%PDF-1.4\n1 0 obj << >> endobj\ntrailer << >>\n%%EOF\n'


@override_settings(MULTI_PRACTICE_ENABLED=True)
class RecordEditsTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        users = get_user_model().objects
        cls.company = Company.objects.create(name='Edits Practice', slug='edits-practice')
        cls.doctor = users.create_user(email='edits-doctor@example.test', first_name='Sam', last_name='Marchant')
        cls.admin = users.create_user(email='edits-admin@example.test', first_name='Lindiwe', last_name='Admin')
        cls.owner = users.create_user(email='edits-owner@example.test', first_name='Joshua', last_name='Czech')
        for user, role, kind in ((cls.doctor, 'doctor', 'doctor'), (cls.admin, 'practice_admin', ''), (cls.owner, 'super_admin', 'doctor')):
            CompanyMembership.objects.create(company=cls.company, user=user, role=role, clinician_type=kind)
        cls.patient = Patient.objects.create(company=cls.company, first_name='Nadia', last_name='Mokoena', assigned_doctor=cls.doctor)
        cls.product = MedicationProduct.objects.create(company=cls.company, name='Semaglutide', strength='0.5 mg', price='100.00',
                                                       category=MedicationProduct.Category.WEIGHT_MANAGEMENT)
        today = timezone.localdate()
        cls.prescription = TreatmentAuthorization.objects.create(
            company=cls.company, patient=cls.patient, prescribed_by=cls.doctor, product=cls.product, max_dose='0.5 mg weekly',
            starts_on=today - timedelta(days=30), expires_on=today + timedelta(days=120))

    def login(self, user):
        self.client.force_login(user)
        session = self.client.session
        session[ACTIVE_COMPANY_SESSION_KEY] = self.company.pk
        session.save()

    def record(self, **query):
        return self.client.get(reverse('portal:patient-detail', args=[self.patient.pk]), query)

    def test_create_note_puts_a_note_in_the_log_where_it_leads(self):
        url = reverse('portal:patient-note-create', args=[self.patient.pk])
        self.login(self.doctor)
        empty = self.client.post(url, {'return_to': 'overview', 'body': ''})
        self.assertEqual(empty.status_code, 400)
        self.assertEqual(empty.context['workspace_tab'], 'overview')
        self.assertContains(empty, '<dialog class="record-dialog" id="record-note" aria-labelledby="record-note-title" open>',
                            status_code=400)
        response = self.client.post(url, {'return_to': 'overview', 'body': 'Asked about travelling with pens.'})
        note = ClinicalNote.objects.get()
        self.assertRedirects(response, f'{reverse("portal:patient-detail", args=[self.patient.pk])}?tab=overview#log-note-{note.pk}',
                             fetch_redirect_response=False)
        self.assertEqual((note.note_type, note.author, note.is_private), (ClinicalNote.NoteType.GENERAL, self.doctor, False))
        page = self.record()
        entry = next(entry for entry in page.context['clinical_log'] if entry['kind'] == 'note')
        self.assertEqual((entry['text'], entry['tag'], entry['lead']), ('Asked about travelling with pens.', '', True))
        self.assertContains(page, f'class="record-log__entry record-log__entry--note" id="log-note-{note.pk}"')
        # A prescription is a smaller grey line.
        self.assertContains(page, 'record-log__entry--prescription record-log__entry--minor')
        # Practice administrators don't write clinical notes.
        self.login(self.admin)
        self.assertRedirects(self.client.post(url, {'return_to': 'overview', 'body': 'Admin note'}),
                             reverse('portal:desktop-dashboard'), fetch_redirect_response=False)

    def test_a_doctor_records_a_blood_result_with_its_report(self):
        url = reverse('portal:patient-blood-result-create', args=[self.patient.pk])
        self.login(self.doctor)
        tab = self.record(tab='blood-tests')
        self.assertEqual(tab.status_code, 200)
        self.assertContains(tab, '<h2>Blood results</h2>', html=True)
        self.assertContains(tab, 'Record blood result')
        future = self.client.post(url, {'taken_on': timezone.localdate() + timedelta(days=1), 'lab_name': 'Lancet', 'results': 'HbA1c 6.1%'})
        self.assertEqual(future.status_code, 400)
        self.assertContains(future, 'can’t be in the future', status_code=400)
        taken = timezone.localdate() - timedelta(days=3)
        response = self.client.post(url, {'taken_on': taken, 'lab_name': 'Lancet', 'results': 'HbA1c 6.1%, ALT 32 U/L',
                                          'report': SimpleUploadedFile('lancet.pdf', PDF, content_type='application/pdf')})
        self.assertRedirects(response, f'{reverse("portal:patient-detail", args=[self.patient.pk])}?tab=blood-tests',
                             fetch_redirect_response=False)
        result = BloodResult.objects.get()
        self.assertEqual((result.taken_on, result.lab_name, result.recorded_by), (taken, 'Lancet', self.doctor))
        self.assertIsNotNone(result.file_id)
        report = reverse('portal:patient-blood-result-report', args=[self.patient.pk, result.pk])
        self.assertContains(self.record(tab='blood-tests'), 'HbA1c 6.1%, ALT 32 U/L')
        log = self.record()
        entry = next(entry for entry in log.context['clinical_log'] if entry['kind'] == 'result')
        self.assertFalse(entry['lead'])
        self.assertTrue(entry['text'].startswith('Lancet, taken '))
        self.assertContains(log, report)
        pdf = self.client.get(report)
        self.assertEqual((pdf.status_code, pdf['Content-Type'], pdf.content), (200, 'application/pdf', PDF))
        # Blood results are clinical: a practice administrator can't read or record them.
        self.login(self.admin)
        self.assertEqual(self.client.get(report).status_code, 403)
        self.assertEqual(self.client.post(url, {'taken_on': taken, 'lab_name': 'Lancet', 'results': 'x'}).status_code, 403)

    def test_writing_a_prescription_makes_that_doctor_the_prescribing_doctor(self):
        request = RequestFactory().get('/')
        request.user = self.owner
        self.login(self.owner)
        response = self.client.post(reverse('portal:patient-prescription-renew', args=[self.patient.pk]), {
            'workflow_context': make_workflow_context(request, self.company, 'patient-prescription', patient=self.patient),
            'current': self.prescription.pk, 'weeks': '12'})
        self.assertEqual(response.status_code, 302)
        self.patient.refresh_from_db()
        self.assertEqual(self.patient.assigned_doctor, self.owner)
        audit = AuditEvent.objects.get(action='patient.doctor_assigned')
        self.assertEqual(audit.metadata, {'previous_doctor_id': self.doctor.pk, 'doctor_id': self.owner.pk, 'by_prescription': True})
        # The previous doctor stays a past clinician and can still read the record.
        self.assertTrue(ClinicianAssignment.objects.filter(patient=self.patient, clinician=self.doctor, ended_at__isnull=False).exists())
        self.login(self.doctor)
        self.assertEqual(self.record().status_code, 200)


class DoseKeyTests(SimpleTestCase):
    def weights(self):
        start = date(2026, 5, 1)
        return [(start + timedelta(weeks=week), Decimal(94 - week)) for week in range(0, 20, 4)]

    def test_one_medicine_is_named_in_the_key_with_its_scale(self):
        chart = doctor_weight_chart(weights=self.weights(), medicines=[{'name': 'Semaglutide', 'dose_max': Decimal('2.4')}], shipments=[
            {'date': date(2026, 5, 2), 'dose': Decimal('0.25'), 'medicine': 'Semaglutide', 'title': 'a'},
            {'date': date(2026, 6, 2), 'dose': Decimal('0.5'), 'medicine': 'Semaglutide', 'title': 'b'}])
        self.assertEqual(chart['medicines'], [{'name': 'Semaglutide', 'tone': 1}])
        self.assertEqual([tick['label'] for tick in chart['dose_axis']], ['0', '1.2', '2.4'])
        self.assertEqual({bar['tone'] for bar in chart['bars']}, {1})

    def test_each_medicine_has_its_own_colour_and_scale(self):
        chart = doctor_weight_chart(weights=self.weights(), medicines=[
            {'name': 'Semaglutide', 'dose_max': Decimal('1')}, {'name': 'Tirzepatide', 'dose_max': Decimal('5')}], shipments=[
            {'date': date(2026, 5, 2), 'dose': Decimal('1'), 'medicine': 'Semaglutide', 'title': 'a'},
            {'date': date(2026, 6, 2), 'dose': Decimal('5'), 'medicine': 'Tirzepatide', 'title': 'b'}])
        self.assertEqual(chart['medicines'], [{'name': 'Semaglutide', 'tone': 1}, {'name': 'Tirzepatide', 'tone': 2}])
        self.assertEqual([bar['tone'] for bar in chart['bars']], [1, 2])
        # Each at its own top dose, so both bars are the same height; no shared mg scale.
        self.assertEqual(chart['bars'][0]['height'], chart['bars'][1]['height'])
        self.assertEqual(chart['dose_axis'], [])
