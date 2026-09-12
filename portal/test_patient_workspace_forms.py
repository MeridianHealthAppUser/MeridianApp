"""Opening a patient's clinical item retains their workspace and permissions."""

import json
import os
import shutil
import subprocess
from datetime import timedelta
from unittest import skipUnless
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from care.models import Appointment, ClinicalEncounter, ClinicalTask, CompoundingRecord, LabRequest, MedicationProduct, Shipment, TreatmentAuthorization
from practices.models import Company, CompanyMembership, Patient
from practices.services import ACTIVE_COMPANY_SESSION_KEY, ACTIVE_PATIENT_COMPANY_SESSION_KEY


class PatientWorkspaceFormTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name='Workspace Forms', slug='workspace-forms')
        cls.beta = Company.objects.create(name='Other Workspace', slug='other-workspace-forms')
        cls.doctor = get_user_model().objects.create_user(email='workspace-form-doctor@example.test')
        cls.admin = get_user_model().objects.create_user(email='workspace-form-admin@example.test')
        cls.super_admin = get_user_model().objects.create_user(email='workspace-form-super@example.test')
        cls.patient_user = get_user_model().objects.create_user(email='workspace-form-patient@example.test')
        for actor, role in ((cls.doctor, 'doctor'), (cls.admin, 'practice_admin'), (cls.super_admin, 'super_admin')):
            CompanyMembership.objects.create(company=cls.company, user=actor, role=role)
        CompanyMembership.objects.create(company=cls.beta, user=cls.doctor, role='doctor')
        cls.patient = Patient.objects.create(company=cls.company, user=cls.patient_user, first_name='Example', last_name='Patient')
        cls.encounter = ClinicalEncounter.objects.create(company=cls.company, patient=cls.patient, clinician=cls.doctor,
            clinical_summary='Recorded consultation draft.', occurred_at=timezone.now() - timedelta(hours=1))
        cls.signed = ClinicalEncounter.objects.create(company=cls.company, patient=cls.patient, clinician=cls.doctor,
            clinical_summary='Signed consultation.', status='signed', signed_at=timezone.now(), signed_by=cls.doctor)
        cls.lab = LabRequest.objects.create(company=cls.company, patient=cls.patient, requested_by=cls.doctor, panel_name='Requested panel')
        cls.appointment = Appointment.objects.create(company=cls.company, patient=cls.patient, clinician=cls.doctor,
            starts_at=timezone.now() + timedelta(days=3), duration_minutes=30)

    def login(self, actor=None, company=None):
        self.client.force_login(actor or self.doctor)
        session = self.client.session
        session[ACTIVE_COMPANY_SESSION_KEY] = (company or self.company).pk
        session[ACTIVE_PATIENT_COMPANY_SESSION_KEY] = self.company.pk
        session.save()

    def screens(self):
        return [
            ('new-consultation', 'consultations', reverse('portal:clinical-consultation-create', args=[self.patient.pk])),
            ('draft-consultation', 'consultations', reverse('portal:clinical-consultation-detail', args=[self.encounter.pk])),
            ('signed-consultation', 'consultations', reverse('portal:clinical-consultation-detail', args=[self.signed.pk])),
            ('new-blood-test', 'blood-tests', reverse('portal:clinical-lab-create', args=[self.patient.pk])),
            ('blood-test', 'blood-tests', reverse('portal:clinical-lab-detail', args=[self.lab.pk])),
            ('appointment', 'appointments', reverse('portal:appointment-detail', args=[self.appointment.pk])),
        ]

    def assertWorkspace(self, response, tab, status=200):
        self.assertEqual(response.status_code, status)
        self.assertTemplateUsed(response, 'portal/patient_workspace_base.html')
        self.assertContains(response, 'aria-label="Patient sections"', count=1, status_code=status)
        self.assertContains(response, '<h1>Example Patient</h1>', count=1, status_code=status)
        self.assertContains(response, f'data-patient-section="{tab}"', status_code=status)
        self.assertEqual(response.context['patient'].pk, self.patient.pk)
        self.assertEqual([item['name'] for item in response.context['workspace_tabs'] if item['active']], [tab])
        self.assertIn('no-store', response['Cache-Control'])

    def test_patient_bound_screens_keep_one_heading_and_the_current_tab(self):
        self.login()
        for name, tab, url in self.screens():
            with self.subTest(screen=name):
                response = self.client.get(url)
                self.assertWorkspace(response, tab)
                self.assertContains(response, f'href="{reverse("portal:patient-detail", args=[self.patient.pk])}?tab={tab}"')

    def test_invalid_submissions_preserve_the_patient_workspace_and_errors(self):
        self.login()
        for name, tab, url in self.screens():
            if name == 'signed-consultation':
                continue
            with self.subTest(screen=name):
                response = self.client.post(url, {})
                self.assertWorkspace(response, tab, status=400)
                self.assertContains(response, 'errorlist', status_code=400)
        self.assertEqual(ClinicalEncounter.objects.count(), 2)
        self.assertEqual(LabRequest.objects.count(), 1)
        self.appointment.refresh_from_db()
        self.assertEqual(self.appointment.status, 'booked')

    def test_saving_a_draft_returns_to_the_same_patient_context(self):
        self.login()
        url = reverse('portal:clinical-consultation-detail', args=[self.encounter.pk])
        page = self.client.get(url)
        response = self.client.post(url, {
            'clinical_context': page.context['clinical_context'], 'summary': 'Updated draft in the patient workspace.',
            'occurred_at': self.encounter.occurred_at.isoformat(), 'appointment': '', 'action': 'save',
        }, follow=True)
        self.assertWorkspace(response, 'consultations')
        self.assertEqual(response.redirect_chain, [(url, 302)])

    def test_admin_appointment_workspace_does_not_add_clinical_permissions(self):
        self.login(self.admin)
        response = self.client.get(reverse('portal:appointment-detail', args=[self.appointment.pk]))
        self.assertWorkspace(response, 'appointments')
        tabs = {item['name'] for item in response.context['workspace_tabs']}
        self.assertNotIn('consultations', tabs)
        self.assertNotIn('blood-tests', tabs)
        for _, tab, url in self.screens():
            if tab != 'appointments':
                self.assertEqual(self.client.get(url).status_code, 403)

    def test_super_admin_sees_signed_read_only_note_and_patient_portal_stays_separate(self):
        self.login(self.super_admin)
        response = self.client.get(reverse('portal:clinical-consultation-detail', args=[self.signed.pk]))
        self.assertWorkspace(response, 'consultations')
        self.assertNotContains(response, 'Sign final note')
        self.assertNotContains(response, 'Save draft')
        self.login(self.patient_user)
        response = self.client.get(reverse('portal:patient-appointment-detail', args=[self.appointment.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'portal/patient_page_base.html')
        self.assertNotContains(response, 'aria-label="Patient sections"')

    def test_other_active_practice_cannot_resolve_the_patient_bound_items(self):
        self.login(company=self.beta)
        for name, _, url in self.screens():
            with self.subTest(screen=name):
                self.assertEqual(self.client.get(url).status_code, 404)

    @skipUnless(os.environ.get('MERIDIAN_WORKSPACE_FORMS_BROWSER'), 'Optional Playwright form-layout checks')
    def test_browser_form_layouts(self):
        self.login()
        entries = [{'name': name, 'tab': tab, 'html': self.client.get(url).content.decode()} for name, tab, url in self.screens()]
        result = subprocess.run(
            [os.environ.get('MERIDIAN_NODE') or shutil.which('node'), str(settings.BASE_DIR / 'scripts/test_patient_workspace_forms.cjs')],
            input=json.dumps(entries), text=True, capture_output=True, cwd=settings.BASE_DIR, timeout=120,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('"layouts":18', result.stdout)


class SharedPatientActionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        PatientWorkspaceFormTests.setUpTestData.__func__(cls)
        cls.other_patient = Patient.objects.create(company=cls.company, first_name='Different', last_name='Patient')
        cls.task = ClinicalTask.objects.create(company=cls.company, patient=cls.patient, title='Patient coordination',
            assigned_to=cls.doctor, created_by=cls.doctor)
        cls.product = MedicationProduct.objects.create(company=cls.company, name='Recorded product', price='10.00', is_compounded=True)
        cls.authorization = TreatmentAuthorization.objects.create(company=cls.company, patient=cls.patient, prescribed_by=cls.doctor,
            product=cls.product, max_dose='Recorded dose', quantity_per_cycle=1, expires_on=timezone.localdate() + timedelta(days=90))
        cls.shipment = Shipment.objects.create(company=cls.company, patient=cls.patient, scheduled_for=timezone.localdate())
        from care.compounding import create_compounding_record
        cls.compounding = create_compounding_record(company=cls.company, authorization=cls.authorization,
            actor=cls.doctor, preparation_note='Tracked manual work.')

    login = PatientWorkspaceFormTests.login
    assertWorkspace = PatientWorkspaceFormTests.assertWorkspace

    def screens(self):
        return [
            ('task-editor', 'tasks', reverse('portal:task-edit', args=[self.task.pk])),
            ('authorisation-create', 'treatment', reverse('portal:treatment-authorisation-create', args=[self.patient.pk])),
            ('authorisation-detail', 'treatment', reverse('portal:treatment-authorisation-detail', args=[self.authorization.pk])),
            ('authorisation-renew', 'treatment', reverse('portal:treatment-authorisation-renew', args=[self.authorization.pk])),
            ('shipment-detail', 'deliveries', reverse('portal:ops-shipment-detail', args=[self.shipment.pk])),
            ('compounding-create', 'treatment', reverse('portal:compounding-create', args=[self.authorization.pk])),
            ('compounding-detail', 'treatment', reverse('portal:compounding-detail', args=[self.compounding.pk])),
        ]

    def test_global_entry_keeps_global_shell_patient_entry_keeps_patient_shell(self):
        self.login()
        for name, tab, url in self.screens():
            with self.subTest(screen=name):
                global_page = self.client.get(url)
                self.assertEqual(global_page.status_code, 200)
                self.assertNotContains(global_page, 'aria-label="Patient sections"')
                patient_page = self.client.get(url, {'workspace': 'patient', 'patient': self.other_patient.pk})
                self.assertWorkspace(patient_page, tab)
                self.assertContains(patient_page, f'href="{reverse("portal:patient-detail", args=[self.patient.pk])}?tab={tab}"')
                self.assertEqual(patient_page.context['patient'].pk, self.patient.pk)

    def test_patient_task_editor_locks_association_and_keeps_context_after_save(self):
        self.login()
        url = reverse('portal:task-edit', args=[self.task.pk])
        page = self.client.get(url, {'workspace': 'patient'})
        self.assertTrue(page.context['form'].fields['patient'].disabled)
        self.assertFalse(self.client.get(url).context['form'].fields['patient'].disabled)
        response = self.client.post(url, {
            'workspace': 'patient', 'workflow_context': page.context['workflow_context'],
            'title': 'Updated coordination task', 'description': '', 'patient': self.other_patient.pk,
            'assigned_to': self.doctor.pk, 'priority': 'normal', 'status': 'open', 'due_at': '', 'new_tag': '',
        }, follow=True)
        self.assertWorkspace(response, 'tasks')
        self.assertEqual(response.redirect_chain, [(url + '?workspace=patient', 302)])
        self.task.refresh_from_db()
        self.assertEqual(self.task.patient_id, self.patient.pk)

    def test_authorisation_actions_and_validation_errors_preserve_context(self):
        self.login()
        url = reverse('portal:treatment-authorisation-detail', args=[self.authorization.pk])
        page = self.client.get(url, {'workspace': 'patient'})
        self.assertContains(page, reverse('portal:treatment-authorisation-renew', args=[self.authorization.pk]) + '?workspace=patient')
        self.assertContains(page, reverse('portal:compounding-create', args=[self.authorization.pk]) + '?workspace=patient')
        status_url = reverse('portal:treatment-authorisation-status', args=[self.authorization.pk])
        rejected = self.client.post(status_url, {'workspace': 'patient', 'action': 'pause'})
        self.assertWorkspace(rejected, 'treatment', status=400)
        response = self.client.post(status_url, {'workspace': 'patient', 'treatment_context': page.context['treatment_context'],
            'action': 'pause', 'confirm': 'on'}, follow=True)
        self.assertWorkspace(response, 'treatment')
        self.assertEqual(response.redirect_chain, [(url + '?workspace=patient', 302)])

    def test_operations_permissions_and_redirects_remain_unchanged(self):
        url = reverse('portal:ops-shipment-detail', args=[self.shipment.pk])
        self.login()
        page = self.client.get(url, {'workspace': 'patient'})
        self.assertFalse(page.context['can_edit'])
        self.assertEqual(self.client.post(url, {'workspace': 'patient', 'action': 'hold'}).status_code, 403)
        self.login(self.admin)
        page = self.client.get(url, {'workspace': 'patient'})
        rejected = self.client.post(url, {'workspace': 'patient', 'action': 'hold'})
        self.assertWorkspace(rejected, 'deliveries', status=400)
        response = self.client.post(url, {'workspace': 'patient', 'workflow_context': page.context['workflow_context'],
            'action': 'hold', 'reason': 'Review before dispatch.', 'confirm': 'on'}, follow=True)
        self.assertWorkspace(response, 'deliveries')
        self.assertEqual(response.redirect_chain, [(url + '?workspace=patient', 302)])

    def test_compounding_followup_remains_in_treatment_context(self):
        self.login()
        url = reverse('portal:compounding-detail', args=[self.compounding.pk])
        page = self.client.get(url, {'workspace': 'patient'})
        self.assertContains(page, reverse('portal:treatment-authorisation-detail', args=[self.authorization.pk]) + '?workspace=patient')
        response = self.client.post(url, {'workspace': 'patient', 'clinical_context': page.context['clinical_context'],
            'action': 'save', 'preparation_note': 'Updated manual tracking note.'}, follow=True)
        self.assertWorkspace(response, 'treatment')
        self.assertEqual(response.redirect_chain, [(url + '?workspace=patient', 302)])

    def test_marker_never_bypasses_practice_or_clinical_permissions(self):
        self.login(company=self.beta)
        for name, _, url in self.screens():
            with self.subTest(screen=name):
                self.assertEqual(self.client.get(url, {'workspace': 'patient'}).status_code, 404)
        self.login(self.admin)
        self.assertEqual(self.client.get(reverse('portal:treatment-authorisation-detail', args=[self.authorization.pk]), {'workspace': 'patient'}).status_code, 403)

    def test_generated_task_links_merge_the_workspace_marker_with_existing_query(self):
        from .clinical_tasks import attach_clinical_task_links
        self.login()

        def linked(tasks, actor, membership):
            result = attach_clinical_task_links(tasks, actor, membership)
            for task in result:
                if task.pk == self.task.pk:
                    task.workflow_url = '/authorisations/123/?view=review#decision'
                    task.workflow_label = 'Review authorisation'
            return result

        with patch('portal.patient_workspace.attach_clinical_task_links', side_effect=linked):
            page = self.client.get(reverse('portal:patient-detail', args=[self.patient.pk]), {'tab': 'tasks'})
        self.assertContains(page, '/authorisations/123/?view=review&amp;workspace=patient#decision')
        self.assertContains(page, reverse('portal:compounding-detail', args=[self.compounding.pk]) + '?workspace=patient')

    @skipUnless(os.environ.get('MERIDIAN_WORKSPACE_FORMS_BROWSER'), 'Optional Playwright form-layout checks')
    def test_browser_shared_action_layouts(self):
        self.login()
        entries = [{'name': name, 'tab': tab, 'html': self.client.get(url, {'workspace': 'patient'}).content.decode()}
                   for name, tab, url in self.screens()]
        result = subprocess.run(
            [os.environ.get('MERIDIAN_NODE') or shutil.which('node'), str(settings.BASE_DIR / 'scripts/test_patient_workspace_forms.cjs')],
            input=json.dumps(entries), text=True, capture_output=True, cwd=settings.BASE_DIR, timeout=120,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('"layouts":21', result.stdout)
