import uuid
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.urls import reverse

from care.followups import append_follow_up
from care.models import AdministrativeFollowUp, Lead, PatientSubscription, Payment
from care.test_operations import OperationsFixture
from practices.services import ACTIVE_COMPANY_SESSION_KEY


class FollowUpTests(OperationsFixture):
    def setUp(self):
        self.lead = Lead.objects.create(company=self.company, first_name='Local', last_name='Enquiry', email='enquiry@example.test')
        self.foreign = Lead.objects.create(company=self.beta, first_name='Private', last_name='Enquiry', email='private@example.test')
        self.plan.status = 'cancelled'
        self.plan.save()
        self.login(self.admin)

    def login(self, actor):
        self.client.force_login(actor)
        session = self.client.session
        session[ACTIVE_COMPANY_SESSION_KEY] = self.company.pk
        session.save()

    def url(self, target=None):
        return reverse('portal:lead-follow-up', args=[(target or self.lead).pk])

    def post(self, target=None, **extra):
        target = target or self.lead
        response = self.client.get(self.url(target))
        values = dict(note='Asked us to contact next week.', status='open', stage='booking', assigned_to=self.admin.pk,
                      next_contact_on=self.today + timedelta(days=7), workflow_context=response.context['workflow_context'])
        values.update(extra)
        return self.client.post(self.url(target), values)

    def test_follow_up_is_append_only_and_never_creates_identity_or_payment(self):
        before = get_user_model().objects.count()
        self.assertEqual(self.post().status_code, 302)
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.stage, 'booking')
        self.assertIsNone(self.lead.converted_patient_id)
        self.assertEqual(get_user_model().objects.count(), before)
        self.assertEqual(Payment.objects.count(), 0)
        self.assertEqual(self.post(note='Contact completed.', status='done', next_contact_on='', stage='closed').status_code, 302)
        self.assertEqual(AdministrativeFollowUp.objects.count(), 2)
        self.assertEqual(AdministrativeFollowUp.objects.last().note, 'Asked us to contact next week.')

    def test_clinician_and_patient_cannot_see_administrative_followups(self):
        for actor in (self.doctor, self.patient_user):
            self.login(actor)
            self.assertEqual(self.client.get(self.url()).status_code, 403)
            self.assertEqual(self.client.get(reverse('portal:dropouts')).status_code, 403)
            self.assertEqual(self.client.post(self.url(), {'note': 'Denied'}).status_code, 403)

    def test_foreign_lead_is_not_found(self):
        self.assertEqual(self.client.get(self.url(self.foreign)).status_code, 404)
        self.assertEqual(self.client.post(self.url(self.foreign), {'note': 'Denied'}).status_code, 404)

    def test_doctor_and_foreign_assignee_rejected(self):
        for actor in (self.doctor, self.beta_admin):
            self.assertEqual(self.post(assigned_to=actor.pk).status_code, 400)
        self.assertFalse(AdministrativeFollowUp.objects.exists())

    def test_stale_context_remains_stale_and_no_duplicate_note(self):
        context = self.client.get(self.url()).context['workflow_context']
        data = dict(note='One note', status='open', stage='booking', workflow_context=context)
        self.assertEqual(self.client.post(self.url(), data).status_code, 302)
        denied = self.client.post(self.url(), data)
        self.assertEqual(denied.status_code, 400)
        self.assertEqual(denied.context['workflow_context'], context)
        self.assertEqual(AdministrativeFollowUp.objects.count(), 1)

    def test_conversion_and_screening_override_not_allowed(self):
        self.assertEqual(self.post(stage='converted', screening_status='cleared').status_code, 400)
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.stage, 'questionnaire')
        self.assertEqual(self.lead.screening_status, 'pending')

    def test_direct_service_cannot_convert_or_reuse_context_on_other_record(self):
        with self.assertRaises(ValidationError):
            append_follow_up(target=self.lead, actor=self.admin, note='Attempt', status='open', stage='converted', submission_key=uuid.uuid4(), expected_updated=self.lead.updated_at.isoformat())

    def test_closed_followup_cannot_have_future_contact_date(self):
        self.assertEqual(self.post(status='done').status_code, 400)

    def test_dropouts_list_and_followup_do_not_restart_subscription(self):
        url = reverse('portal:dropout-detail', args=[self.plan.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        saved = self.client.post(url, dict(note='Local follow-up; not restarting.', status='done', workflow_context=response.context['workflow_context']))
        self.assertEqual(saved.status_code, 302)
        self.plan.refresh_from_db()
        self.assertEqual(self.plan.status, 'cancelled')
        self.assertContains(self.client.get(reverse('portal:dropouts') + '?follow_up=done'), self.plan.plan_name)

    def test_active_plans_not_in_dropout_workspace(self):
        self.plan.status = 'active'
        self.plan.save()
        self.assertEqual(self.client.get(reverse('portal:dropout-detail', args=[self.plan.pk])).status_code, 404)

    def test_bad_filters_fail_closed_and_lead_table_shows_followup(self):
        self.post()
        self.assertContains(self.client.get(reverse('portal:staff-leads') + '?follow_up=open'), 'Follow-up: open')
        self.assertNotContains(self.client.get(reverse('portal:staff-leads') + '?follow_up=bad'), 'enquiry@example.test')
        self.assertNotContains(self.client.get(reverse('portal:dropouts') + '?follow_up=bad'), self.plan.plan_name)

    def test_private_cache_headers(self):
        self.assertIn('no-store', self.client.get(self.url())['Cache-Control'])
