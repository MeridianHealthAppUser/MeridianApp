"""Edit profile on the patient record: Super Admins only, audited with field names, the patient told, and issued
invoices keeping who they were billed to."""

from datetime import date, timedelta

from django.core.exceptions import PermissionDenied
from django.urls import reverse
from django.utils import timezone

from care.models import AuditEvent, PatientEvent, SubscriptionCycle
from care.patient_details import update_patient_details
from practices.models import CompanyMembership, Patient

from .patient_workspace import workspace_url
from .test_billing import BillingPageFixture

VALID = {'title': 'MRS', 'first_name': ' Nadia ', 'last_name': 'Dlamini', 'date_of_birth': '1988-04-02',
         'id_number': '8804020000081', 'phone': '082 555 0199', 'city': 'Durban'}


class PatientDetailsTests(BillingPageFixture):
    def setUp(self):
        super().setUp()
        self.url = reverse('portal:patient-details-edit', args=[self.patient.pk])

    def post(self, **values):
        page = self.client.get(self.url)
        return self.client.post(self.url, {**VALID, 'assigned_doctor': page.context['form'].initial['assigned_doctor'] or '',
                                           **values, 'workflow_context': page.context['workflow_context']})

    def test_only_a_super_admin_gets_edit_profile(self):
        record = reverse('portal:patient-detail', args=[self.patient.pk])
        self.login(self.super_admin)
        self.assertContains(self.client.get(record), f'href="{self.url}"')
        self.assertContains(self.client.get(self.url), 'Save profile')
        # The patient's own doctor reads the record but goes back to it from Edit profile, with nothing saved.
        self.login(self.doctor)
        self.assertNotContains(self.client.get(record), f'href="{self.url}"')
        for response in (self.client.get(self.url), self.client.post(self.url, VALID)):
            self.assertRedirects(response, workspace_url(self.patient), fetch_redirect_response=False)
        # Practice administrators don't open records at all.
        self.login(self.admin)
        self.assertRedirects(self.client.get(self.url), reverse('portal:desktop-dashboard'), fetch_redirect_response=False)
        self.patient.refresh_from_db()
        self.assertEqual(self.patient.last_name, 'Mokoena')

    def test_saving_audits_the_fields_tells_the_patient_and_renames_their_login(self):
        self.login(self.super_admin)
        self.assertRedirects(self.post(), workspace_url(self.patient), fetch_redirect_response=False)
        self.patient.refresh_from_db()
        self.assertEqual((self.patient.title, self.patient.first_name, self.patient.last_name, self.patient.date_of_birth,
                          self.patient.id_number, self.patient.phone, self.patient.city),
                         ('MRS', 'Nadia', 'Dlamini', date(1988, 4, 2), '8804020000081', '082 555 0199', 'Durban'))
        audit = AuditEvent.objects.get(action='patient.details_updated')
        self.assertEqual(audit.metadata, {'changed_fields': ['title', 'last_name', 'date_of_birth', 'id_number', 'phone', 'city']})
        self.assertEqual(audit.actor, self.super_admin)
        event = PatientEvent.objects.get(source_type='practices.patient')
        self.assertEqual(event.detail, 'Your practice updated your title, name, date of birth, ID or passport number, '
                                       'phone number and city. Check them on My profile.')
        self.assertTrue(event.is_patient_visible)
        self.patient_user.refresh_from_db()
        self.assertEqual((self.patient_user.first_name, self.patient_user.last_name), ('Nadia', 'Dlamini'))
        # Saving the same details again changes nothing and isn't logged.
        response = self.post()
        self.assertEqual([str(message) for message in response.wsgi_request._messages], ['Nothing changed.'])
        self.assertEqual(AuditEvent.objects.filter(action='patient.details_updated').count(), 1)

    def test_bad_details_are_refused_and_nothing_is_saved(self):
        Patient.objects.create(company=self.company, first_name='Other', last_name='Patient', id_number='9001015000088')
        self.login(self.super_admin)
        tomorrow = (timezone.localdate() + timedelta(days=1)).isoformat()
        for bad, field in (({'id_number': '9001015000088'}, 'id_number'), ({'date_of_birth': tomorrow}, 'date_of_birth'),
                           ({'phone': 'call me'}, 'phone'), ({'last_name': ' '}, 'last_name'), ({'title': 'SIR'}, 'title')):
            with self.subTest(field=field):
                response = self.post(**bad)
                self.assertEqual(response.status_code, 400)
                self.assertTrue(response.context['form'].errors.get(field))
        self.patient.refresh_from_db()
        self.assertEqual((self.patient.last_name, self.patient.id_number), ('Mokoena', ''))

    def test_a_change_made_in_another_tab_is_caught(self):
        self.login(self.super_admin)
        page = self.client.get(self.url)
        self.patient.title = 'DR'
        self.patient.save()
        response = self.client.post(self.url, {**VALID, 'workflow_context': page.context['workflow_context']})
        self.assertContains(response, 'This record changed in another tab', status_code=400)
        self.patient.refresh_from_db()
        self.assertEqual(self.patient.last_name, 'Mokoena')

    def test_only_super_admins_save_and_a_staff_login_keeps_its_own_name(self):
        data = {**VALID, 'first_name': 'Nadia', 'date_of_birth': date(1988, 4, 2)}
        with self.assertRaises(PermissionDenied):
            update_patient_details(company=self.company, patient=self.patient, actor=self.doctor, data=data)
        CompanyMembership.objects.create(company=self.company, user=self.patient_user, role='practice_admin')
        update_patient_details(company=self.company, patient=self.patient, actor=self.super_admin, data=data)
        self.patient_user.refresh_from_db()
        self.assertEqual(self.patient_user.last_name, '')

    def test_an_issued_invoice_keeps_who_it_was_billed_to(self):
        self.approved()
        invoice = SubscriptionCycle.objects.get().invoice
        self.assertEqual(invoice.billed_to, {'name': 'Nadia Mokoena', 'id_number': ''})
        update_patient_details(company=self.company, patient=self.patient, actor=self.super_admin,
                               data={**VALID, 'date_of_birth': None})
        self.login(self.patient_user)
        page = self.client.get(reverse('portal:patient-invoice', args=[invoice.pk]))
        self.assertContains(page, '<dt>Billed to</dt><dd>Nadia Mokoena</dd>', html=True)
        self.assertNotContains(page, '8804020000081')
        # An invoice from before invoices were stamped shows the details as they are now.
        type(invoice).objects.filter(pk=invoice.pk).update(billed_to={})
        page = self.client.get(reverse('portal:patient-invoice', args=[invoice.pk]))
        self.assertContains(page, '<dt>Billed to</dt><dd>Mrs Nadia Dlamini</dd>', html=True)
