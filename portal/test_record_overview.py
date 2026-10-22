"""The clinical Overview of a patient record (Rev 3.10): log, prescription, weight chart and summary data."""

from datetime import date, timedelta

from django.contrib.auth import get_user_model
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from care.clinical_summary import get_summary
from care.models import (
    AuditEvent, ClinicalEncounter, ConsentRecord, LabRequest, MedicationDose, MedicationProduct,
    PatientClinicalSummary, Shipment, ShipmentItem, TreatmentAuthorization, WeightEntry,
)
from practices.models import Company, CompanyMembership, Patient
from practices.services import ACTIVE_COMPANY_SESSION_KEY

from .workflow_context import make_workflow_context


@override_settings(MULTI_PRACTICE_ENABLED=True)
class RecordOverviewTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        users = get_user_model().objects
        cls.company = Company.objects.create(name='Overview Practice', slug='overview-practice')
        cls.doctor = users.create_user(email='overview-doctor@example.test', first_name='Sam', last_name='Marchant')
        cls.dietitian = users.create_user(email='overview-dietitian@example.test', first_name='Dee', last_name='Titian')
        cls.admin = users.create_user(email='overview-admin@example.test', first_name='Lindiwe', last_name='Admin')
        # A Super Admin who is a doctor: every tab, and the only one who exports the record.
        cls.owner = users.create_user(email='overview-owner@example.test', first_name='Joshua', last_name='Czech')
        for user, role, kind in ((cls.doctor, 'doctor', 'doctor'), (cls.dietitian, 'doctor', 'dietitian'), (cls.admin, 'practice_admin', ''),
                                 (cls.owner, 'super_admin', 'doctor')):
            CompanyMembership.objects.create(company=cls.company, user=user, role=role, clinician_type=kind)
        cls.patient = Patient.objects.create(company=cls.company, first_name='Nadia', last_name='Mokoena', city='Cape Town',
                                             assigned_doctor=cls.doctor, id_number='9201015800087',
                                             date_of_birth=date(timezone.localdate().year - 34, 1, 1))
        pen = dict(company=cls.company, price='100.00', category=MedicationProduct.Category.WEIGHT_MANAGEMENT)
        cls.low = MedicationDose.objects.create(
            company=cls.company, pen=MedicationProduct.objects.create(name='Semaglutide', strength='2 mg', **pen), amount_mg='0.5',
            price='1995')
        cls.high = MedicationDose.objects.create(
            company=cls.company, pen=MedicationProduct.objects.create(name='Semaglutide', strength='4 mg', **pen), amount_mg='1',
            price='3295')
        today = timezone.localdate()
        cls.today = today
        cls.prescription = TreatmentAuthorization.objects.create(
            company=cls.company, patient=cls.patient, prescribed_by=cls.doctor, product=cls.low.pen, dose=cls.low,
            max_dose='0.5 mg weekly',
            starts_on=today - timedelta(days=150), expires_on=today + timedelta(days=20),
            log_text='Prescription created: Semaglutide 0.5 mg weekly, 6 months.',
        )
        for weeks, kg in ((20, '94.0'), (12, '90.5'), (4, '86.0')):
            WeightEntry.objects.create(company=cls.company, patient=cls.patient, recorded_on=today - timedelta(weeks=weeks), weight_kg=kg)
        cls.consult = ClinicalEncounter.objects.create(
            company=cls.company, patient=cls.patient, clinician=cls.doctor, status=ClinicalEncounter.Status.SIGNED,
            occurred_at=timezone.now() - timedelta(weeks=8), clinical_summary='Nausea settled. Increased dose to 1.0 mg weekly.',
        )
        ClinicalEncounter.objects.create(company=cls.company, patient=cls.patient, clinician=cls.doctor,
                                         clinical_summary='DRAFT_NOT_FOR_THE_LOG')
        LabRequest.objects.create(company=cls.company, patient=cls.patient, requested_by=cls.doctor, panel_name='HbA1c and lipids')
        shipment = Shipment.objects.create(company=cls.company, patient=cls.patient, cycle_number=4, status=Shipment.Status.DISPATCHED,
                                           scheduled_for=today - timedelta(weeks=6), dispatched_at=timezone.now() - timedelta(weeks=6),
                                           tracking_number='MER-438-771-ZA')
        ShipmentItem.objects.create(company=cls.company, shipment=shipment, product=cls.low.pen, dose='0.5 mg weekly', quantity=4,
                                    batch_note='SG-2604, expiry 04/2027')
        ConsentRecord.objects.create(company=cls.company, patient=cls.patient, consent_type='telehealth', document_version='v1', accepted=True)

    def login(self, user):
        self.client.force_login(user)
        session = self.client.session
        session[ACTIVE_COMPANY_SESSION_KEY] = self.company.pk
        session.save()

    def page(self, **query):
        return self.client.get(reverse('portal:patient-detail', args=[self.patient.pk]), query)

    def token(self, user, kind):
        request = RequestFactory().get('/')
        request.user = user
        return make_workflow_context(request, self.company, kind, patient=self.patient)

    def test_doctor_sees_the_clinical_log_dispensed_shipments_and_header_badges(self):
        self.login(self.doctor)
        response = self.page()
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'data-record-overview')
        self.assertNotIn('overview_cards', response.context)
        kinds = [entry['kind'] for entry in response.context['clinical_log']]
        self.assertEqual(sorted(kinds), ['blood', 'consult', 'prescription'])
        self.assertEqual(response.context['clinical_log'], sorted(response.context['clinical_log'], key=lambda e: e['at'], reverse=True))
        self.assertContains(response, 'Nausea settled. Increased dose to 1.0 mg weekly.')
        self.assertContains(response, 'Blood form for HbA1c and lipids sent.')
        self.assertNotContains(response, 'DRAFT_NOT_FOR_THE_LOG')
        self.assertContains(response, '3 entries')
        # The dose it was sent for, then the pens.
        self.assertContains(response, 'Semaglutide 0.5 mg weekly')
        self.assertContains(response, '4 × 2 mg pens')
        self.assertContains(response, 'Month 4')
        self.assertContains(response, 'courier MER-438-771-ZA')
        # Valid but expiring within 30 days.
        self.assertContains(response, f'Prescribed to {self.prescription.expires_on.day} {self.prescription.expires_on:%b}')
        self.assertContains(response, 'Renewal due')
        self.assertContains(response, 'Cape Town')
        # Title, Age, Location, Prescribing doctor (no pen for a doctor) and ID number.
        for fact in ('<dt>Title</dt>', '<dt>Age</dt><dd>34</dd>', '<dt>Prescribing doctor</dt>', '<dt>ID number</dt><dd>9201015800087</dd>'):
            self.assertContains(response, fact, html=False)
        self.assertNotContains(response, 'Date of birth')
        self.assertNotContains(response, 'Change Prescribing doctor')
        self.assertContains(response, 'Initiate prescription')
        self.assertContains(response, 'Renew prescription')
        self.assertContains(response, 'Change prescription')
        self.assertContains(response, 'Create note')
        self.assertNotContains(response, 'Listed from the main treatments')
        # Only a Super Admin exports the record.
        self.assertNotContains(response, 'Export record')
        self.login(self.owner)
        self.assertContains(self.page(), 'Export record')
        self.assertNotContains(response, 'Quantity')
        chart = response.context['weight_chart']
        self.assertEqual([dot['id'] for dot in chart['consults']], [self.consult.pk])
        self.assertEqual(len(chart['bars']), 1)
        self.assertEqual(chart['goal_pill'], '')

    def test_dispensed_filter_and_history_without_a_consent_card(self):
        self.login(self.doctor)
        response = self.page(log='dispensed')
        self.assertEqual(response.context['log_filter'], 'dispensed')
        self.assertContains(response, '1 shipment')
        self.login(self.owner)
        history = self.page(tab='history')
        self.assertNotContains(history, 'Current consent')

    def test_practice_administrators_never_open_the_record(self):
        # Practice administrators don't open patient records (22 Oct 2026); their Overview layout is gone.
        self.login(self.admin)
        for tab in ('overview', 'weights', 'notes', 'payments'):
            with self.subTest(tab=tab):
                self.assertRedirects(self.page(tab=tab), reverse('portal:desktop-dashboard'), fetch_redirect_response=False)

    def test_doctor_renews_from_the_dialog_and_it_is_logged(self):
        self.login(self.doctor)
        response = self.client.post(reverse('portal:patient-prescription-renew', args=[self.patient.pk]), {
            'workflow_context': self.token(self.doctor, 'patient-prescription'), 'current': self.prescription.pk, 'weeks': '12',
        })
        self.assertEqual(response.status_code, 302)
        renewal = TreatmentAuthorization.objects.get(replaces=self.prescription)
        self.assertEqual(renewal.starts_on, self.today)
        page = self.page()
        self.assertEqual(page.context['clinical_log'][0]['text'], renewal.log_text)
        self.assertNotContains(page, 'Renewal due')

    def test_the_same_dose_is_refused_and_the_dialog_reopens_with_the_reason(self):
        self.login(self.doctor)
        response = self.client.post(reverse('portal:patient-prescription-change', args=[self.patient.pk]), {
            'workflow_context': self.token(self.doctor, 'patient-prescription'), 'current': self.prescription.pk,
            'medicine': 'Semaglutide', 'dose': self.low.pk, 'weeks': '24',
        })
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.context['open_dialog'], 'change')
        self.assertContains(response, 'Use Renew to extend it', status_code=400)
        self.assertEqual(TreatmentAuthorization.objects.count(), 1)

    def test_a_safety_flag_needs_the_tick(self):
        PatientClinicalSummary.objects.create(company=self.company, patient=self.patient, pancreatitis='yes')
        self.login(self.doctor)
        page = self.page()
        self.assertContains(page, 'Safety flag')
        self.assertContains(page, 'name="acknowledge_flags"')
        data = {'workflow_context': self.token(self.doctor, 'patient-prescription'), 'current': self.prescription.pk,
                'medicine': 'Semaglutide', 'dose': self.high.pk, 'weeks': '24', 'reason': 'Tolerating well'}
        url = reverse('portal:patient-prescription-change', args=[self.patient.pk])
        self.assertEqual(self.client.post(url, data).status_code, 400)
        self.assertEqual(self.client.post(url, {**data, 'acknowledge_flags': 'on'}).status_code, 302)
        changed = TreatmentAuthorization.objects.get(replaces=self.prescription)
        self.assertTrue(changed.log_text.startswith('Tolerating well. Prescription changed from Semaglutide 0.5 mg weekly '
                                                    'to Semaglutide 1 mg weekly, 24 weeks'))

    def test_only_doctors_prescribe_and_only_clinical_staff_edit_summary_data(self):
        # The dietitian is Nadia's clinician here, so only being a dietitian stops them prescribing.
        Patient.objects.filter(pk=self.patient.pk).update(assigned_doctor=self.dietitian)
        renew = reverse('portal:patient-prescription-renew', args=[self.patient.pk])
        summary = reverse('portal:patient-summary-edit', args=[self.patient.pk])
        for user in (self.dietitian, self.admin):
            with self.subTest(user=user.email):
                self.login(user)
                self.assertEqual(self.client.post(renew, {'workflow_context': self.token(user, 'patient-prescription'),
                                                          'current': self.prescription.pk, 'weeks': '24'}).status_code, 403)
        self.login(self.dietitian)
        page = self.page()
        self.assertNotContains(page, 'Renew prescription')
        self.assertContains(page, 'Click an underlined value to edit')
        self.login(self.admin)
        self.assertEqual(self.client.post(summary, {'workflow_context': self.token(self.admin, 'patient-summary'),
                                                    'field': 'allergies', 'value': 'Penicillin'}).status_code, 403)
        self.assertEqual(TreatmentAuthorization.objects.count(), 1)
        self.assertFalse(PatientClinicalSummary.objects.exists())

    def test_goal_weight_edit_is_listed_and_draws_the_projection(self):
        self.login(self.doctor)
        url = reverse('portal:patient-summary-edit', args=[self.patient.pk])
        token = self.token(self.doctor, 'patient-summary')
        bad = self.client.post(url, {'workflow_context': token, 'field': 'goal_weight_kg', 'value': '30'})
        self.assertEqual(bad.status_code, 400)
        self.assertContains(bad, 'between 40 and 250', status_code=400)
        response = self.client.post(url, {'workflow_context': token, 'field': 'goal_weight_kg', 'value': '75'})
        self.assertRedirects(response, f'{reverse("portal:patient-detail", args=[self.patient.pk])}?tab=overview#record-summary',
                             fetch_redirect_response=False)
        page = self.page()
        self.assertTrue(page.context['weight_chart']['goal_pill'].startswith('Goal 75 kg · around '))
        self.assertTrue(page.context['weight_chart']['projection'])
        self.assertContains(page, '1 edit since')
        self.assertContains(page, '<s>Not recorded</s> &rarr; 75 kg', html=False)
        self.assertTrue(AuditEvent.objects.filter(action='summary.updated', patient=self.patient).exists())

    def test_height_is_edited_in_summary_data_and_changes_the_bmi(self):
        from decimal import Decimal

        from .patient_summary import medical_information

        self.login(self.doctor)
        url = reverse('portal:patient-summary-edit', args=[self.patient.pk])
        token = self.token(self.doctor, 'patient-summary')
        self.assertContains(self.page(), '<dt>Height</dt>', html=False)
        bad = self.client.post(url, {'workflow_context': token, 'field': 'height_cm', 'value': '16.4'})
        self.assertContains(bad, 'between 80 and 260 cm', status_code=400)
        self.client.post(url, {'workflow_context': token, 'field': 'height_cm', 'value': '164'})
        page = self.page()
        self.assertEqual(page.context['summary'].height_cm, Decimal('164.0'))
        self.assertEqual(page.context['summary_bmi'], {'start': Decimal('34.9'), 'current': Decimal('32.0')})
        self.assertContains(page, '<s>Not recorded</s> &rarr; 164 cm', html=False)
        # The patient's own Medical information card shows the height their clinician recorded.
        rows = medical_information(profile=None, intake={'height_cm': '170'}, weight=None, recorded_height=Decimal('164.0'))
        self.assertEqual(rows[0], ('Height', '164 cm'))

    def test_weight_tab_has_the_overview_chart_beside_the_history(self):
        self.login(self.owner)
        response = self.page(tab='weights')
        self.assertEqual(response.status_code, 200)
        chart = response.context['weight_chart']
        self.assertEqual([dot['id'] for dot in chart['consults']], [self.consult.pk])
        self.assertEqual(len(chart['bars']), 1)
        self.assertContains(response, 'class="record-weight__panes"')
        self.assertContains(response, 'Check-in history')
        self.assertContains(response, 'Since first check-in &middot; 3 check-ins')
        self.assertContains(response, 'class="record-chart__dose record-chart__dose--1"')
        # The key names the medicine shipped.
        self.assertContains(response, 'Dose shipped: Semaglutide (mg, right scale)')
        self.assertContains(response, f'data-consult="{self.consult.pk}"')
        # The dot opens the signed note here; "Show in log" goes to it on the Overview.
        self.assertContains(response, f'<template id="record-consult-{self.consult.pk}"')
        self.assertContains(response, 'Nausea settled. Increased dose to 1.0 mg weekly.')
        overview = f'{reverse("portal:patient-detail", args=[self.patient.pk])}?tab=overview'
        self.assertContains(response, f'href="{overview}" data-consult-show data-log-url="{overview}"')
        self.assertContains(response, 'Set a goal weight in Summary data on the')
        self.assertContains(response, 'js/record_overview.js')
        self.assertNotContains(response, 'DRAFT_NOT_FOR_THE_LOG')

    def test_super_admins_see_the_weight_tab(self):
        summary = get_summary(self.company, self.patient)
        summary.goal_weight_kg = 75
        summary.save()
        self.login(self.owner)
        self.assertTrue(self.page(tab='weights').context['weight_chart']['goal_pill'].startswith('Goal 75 kg · around '))

    def test_an_open_conversation_shows_the_patients_notes_to_clinical_staff_only(self):
        from care.messaging import add_participant
        from care.models import ClinicalNote, MessageThread, PracticeSettings

        PracticeSettings.objects.create(company=self.company, store_view_log=True)

        thread = MessageThread.objects.create(company=self.company, patient=self.patient, subject='Dose question', opened_by=self.doctor)
        for user in (self.doctor, self.dietitian, self.admin):
            add_participant(thread, user)
        ClinicalNote.objects.create(company=self.company, patient=self.patient, author=self.dietitian, note_type='phone',
                                    body='Phoned about meal plan.')
        ClinicalNote.objects.create(company=self.company, patient=self.patient, author=self.dietitian, is_private=True,
                                    body='DIETITIAN_PRIVATE_NOTE')
        ClinicalNote.objects.create(company=self.company, patient=self.patient, author=self.doctor, is_private=True,
                                    body='Doctor private reminder.')
        pages = (reverse('portal:staff-inbox') + f'?thread={thread.pk}',
                 reverse('portal:patient-detail', args=[self.patient.pk]) + f'?tab=messages&thread={thread.pk}')
        self.login(self.doctor)
        for url in pages:
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200)
                notes = response.context['conversation_notes']['entries']
                self.assertEqual(sorted(note.label for note in notes), ['Consult note', 'Consultation note', 'Phone call'])
                self.assertEqual([note.at for note in notes], sorted((note.at for note in notes), reverse=True))
                self.assertContains(response, '<details class="conversation-notes">')
                self.assertContains(response, 'Nausea settled. Increased dose to 1.0 mg weekly.')
                self.assertContains(response, reverse('portal:clinical-consultation-detail', args=[self.consult.pk]))
                self.assertContains(response, 'Phoned about meal plan.')
                self.assertContains(response, 'Doctor private reminder.')
                # A private note is tagged Private, on the right of its line.
                self.assertContains(response, '<span class="console-status note-private" title="Only visible to you">Private</span>', html=True)
                for hidden in ('DIETITIAN_PRIVATE_NOTE', 'DRAFT_NOT_FOR_THE_LOG'):
                    self.assertNotContains(response, hidden)
        self.assertEqual(AuditEvent.objects.filter(action='message.thread_viewed', actor=self.doctor).last().metadata,
                         {'patient_notes': 3})
        self.login(self.admin)
        for url in (pages[0],):
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200)
            self.assertIsNone(response.context['conversation_notes'])
            for hidden in ('conversation-notes', 'Phoned about meal plan.', 'Nausea settled'):
                self.assertNotContains(response, hidden)

    def test_notes_tab_lists_signed_consult_notes_with_the_general_notes_as_the_conversation_does(self):
        from care.models import ClinicalNote

        ClinicalNote.objects.create(company=self.company, patient=self.patient, author=self.owner, note_type='phone',
                                    body='Phoned about meal plan.')
        # Doctors read notes in the Overview's log; the Notes tab is for Super Admins and admins.
        self.login(self.doctor)
        self.assertRedirects(self.page(tab='notes'), f'{reverse("portal:patient-detail", args=[self.patient.pk])}?tab=overview',
                             fetch_redirect_response=False)
        self.login(self.owner)
        response = self.page(tab='notes')
        self.assertEqual(response.status_code, 200)
        self.assertEqual([note.label for note in response.context['notes']], ['Phone call', 'Consult note'])
        self.assertEqual(response.context['workspace_row_count'], 2)
        self.assertContains(response, f'<article class="patient-workspace-note" id="consult-{self.consult.pk}">')
        self.assertContains(response, 'Nausea settled. Increased dose to 1.0 mg weekly.')
        self.assertContains(response, f'href="{reverse("portal:clinical-consultation-detail", args=[self.consult.pk])}">Open consultation</a>')
        self.assertNotContains(response, 'DRAFT_NOT_FOR_THE_LOG')
        # A practice administrator can't open the record at all.
        self.login(self.admin)
        self.assertRedirects(self.page(tab='notes'), reverse('portal:desktop-dashboard'), fetch_redirect_response=False)
